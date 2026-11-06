#!/usr/bin/env python3
"""Tests for the restore policy and the session list.

plan() is the only place that decides to delete a server registration. The
cases below are the ones where getting it wrong either deletes something live
or deletes the environment the kept session is about to be resumed on.
"""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from importlib.machinery import SourceFileLoader

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lib"))
import bridge          # noqa: E402
import keeper          # noqa: E402
import tmuxctl         # noqa: E402

ENTRY = {"session_id": "session_KEEP", "environment_id": "env_KEEP"}


def ptr(session="session_KEEP", env="env_KEEP"):
    return bridge.Pointer(session, env, 1234, "99", "/p")


def win(dead=False):
    return tmuxctl.Window("@1", "/d", 1234, dead, "d")


class Plan(unittest.TestCase):

    def test_live_window_is_left_alone(self):
        self.assertEqual(keeper.plan(ENTRY, ptr("other", "env_other"), False, win())[0],
                         keeper.RUNNING)

    def test_nothing_recorded_launches(self):
        self.assertEqual(keeper.plan(ENTRY, None, False, None), (keeper.LAUNCH, None))

    def test_dead_window_is_relaunched(self):
        self.assertEqual(keeper.plan(ENTRY, None, False, win(dead=True))[0], keeper.LAUNCH)

    def test_own_session_alive_elsewhere_is_left_alone(self):
        self.assertEqual(keeper.plan(ENTRY, ptr(), True, None), (keeper.ELSEWHERE, 1234))

    def test_own_environment_alive_elsewhere_is_left_alone(self):
        """Its pointer may name another of our sessions, e.g. after the first ended."""
        self.assertEqual(keeper.plan(ENTRY, ptr("session_OTHER"), True, None)[0],
                         keeper.ELSEWHERE)

    def test_our_own_restore_pointer_launches(self):
        """The pointer restore writes has no pid; it must not read as stale."""
        p = bridge.parse_pointer('{"sessionId":"session_KEEP","environmentId":"env_KEEP",'
                                 '"source":"standalone"}')
        self.assertEqual(keeper.plan(ENTRY, p, bridge.host_alive(p), None),
                         (keeper.LAUNCH, None))

    def test_live_foreign_host_is_never_released(self):
        """A different session that is actually running: refuse, never delete."""
        self.assertEqual(keeper.plan(ENTRY, ptr("other", "env_other"), True, None)[0],
                         keeper.OCCUPIED)

    def test_dead_foreign_host_is_released(self):
        """Today's bug: an accidental second rc, Ctrl-C'd or crashed."""
        self.assertEqual(keeper.plan(ENTRY, ptr("other", "env_other"), False, None),
                         (keeper.RELEASE, "env_other"))

    def test_own_environment_is_never_released(self):
        """After a plain reboot the pointer is our own dead host. Resume reuses
        that environment, so it must be launched, not released."""
        self.assertEqual(keeper.plan(ENTRY, ptr(), False, None), (keeper.LAUNCH, None))

    def test_entry_without_recorded_environment_still_releases_foreign(self):
        e = {"session_id": "session_KEEP"}
        self.assertEqual(keeper.plan(e, ptr("other", "env_other"), False, None)[0],
                         keeper.RELEASE)


IENTRY = {"kind": "interactive", "session_id": "session_RC",
          "transcript_id": "4e605284-c166-4a59-a5f9-6b5649b23c01"}


def live(transcript="4e605284-c166-4a59-a5f9-6b5649b23c01", cwd="/d",
         bridge_id="session_RC", entrypoint="cli", pid=555):
    return bridge.LiveSession(pid, transcript, cwd, bridge_id, entrypoint)


class PlanInteractive(unittest.TestCase):

    def test_entries_without_kind_are_rc(self):
        self.assertEqual(keeper.kind(ENTRY), "rc")
        self.assertEqual(keeper.kind(IENTRY), "interactive")

    def test_live_window_is_left_alone(self):
        self.assertEqual(keeper.plan_interactive(IENTRY, [], win())[0], keeper.RUNNING)

    def test_same_conversation_running_elsewhere_is_left_alone(self):
        """Never launch a second copy of a conversation that is already open."""
        self.assertEqual(keeper.plan_interactive(IENTRY, [live()], None),
                         (keeper.ELSEWHERE, 555))

    def test_other_conversations_do_not_count(self):
        self.assertEqual(keeper.plan_interactive(IENTRY, [live(transcript="x")], None),
                         (keeper.LAUNCH, None))

    def test_dead_window_is_relaunched(self):
        self.assertEqual(keeper.plan_interactive(IENTRY, [], win(dead=True))[0],
                         keeper.LAUNCH)

    def test_resume_argv(self):
        self.assertEqual(keeper.resume_argv(IENTRY),
                         ["claude", "--resume", IENTRY["transcript_id"],
                          "--remote-control"])


class AdoptCandidates(unittest.TestCase):

    def test_only_terminal_sessions_with_remote_control_in_that_folder(self):
        sessions = [live(pid=1),
                    live(pid=2, cwd="/elsewhere"),
                    live(pid=3, bridge_id=""),               # remote control off
                    live(pid=4, entrypoint="sdk-cli")]       # an rc host's child
        self.assertEqual([s.pid for s in keeper.interactive_in("/d", sessions)], [1])


class Sync(unittest.TestCase):
    """sync_one against a fake host: this process, with a real child."""

    def setUp(self):
        import subprocess
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = self.tmp.name
        self.child = subprocess.Popen(["sleep", "30"])
        me = os.getpid()
        d = os.path.join(self.tmp.name, "projects", "-d")
        os.makedirs(d)
        with open(os.path.join(d, bridge.POINTER), "w") as f:
            json.dump({"sessionId": "session_KEEP", "environmentId": "env_KEEP",
                       "source": "standalone", "pid": me,
                       "procStart": bridge.proc_start(me)}, f)
        self.live = [live(pid=self.child.pid, bridge_id="cse_EXTRA", entrypoint="sdk-cli"),
                     live(pid=self.child.pid, bridge_id="session_KEEP", entrypoint="sdk-cli"),
                     live(pid=1, bridge_id="session_NOTOURS", entrypoint="sdk-cli")]

    def tearDown(self):
        self.child.kill()
        self.child.wait()
        if self.old is None:
            del os.environ["CLAUDE_CONFIG_DIR"]
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self.old
        self.tmp.cleanup()

    def test_adds_children_of_live_host_only(self):
        e = dict(ENTRY, sessions=[])
        self.assertEqual(keeper.sync_one("/d", e, self.live), (["session_EXTRA"], []))
        self.assertEqual(e["sessions"], ["session_EXTRA"])
        self.assertEqual(e["synced_from"], "processes")

    def test_never_removes_without_the_server(self):
        e = dict(ENTRY, sessions=["session_IDLE"])
        keeper.sync_one("/d", e, self.live)
        self.assertEqual(e["sessions"], ["session_EXTRA", "session_IDLE"])

    def test_host_on_another_environment_is_not_ours(self):
        e = dict(ENTRY, environment_id="env_ELSE", sessions=[])
        self.assertEqual(keeper.sync_one("/d", e, self.live), ([], []))

    def test_interactive_entries_are_skipped(self):
        self.assertEqual(keeper.sync_one("/d", dict(IENTRY), self.live), ([], []))

    def test_server_adds_idle_sessions_on_our_environment(self):
        e = dict(ENTRY, sessions=[])
        server = [srv("session_KEEP"), srv("session_PARKED", status="idle"),
                  srv("session_OTHER", env="env_OTHER")]
        self.assertEqual(keeper.sync_one("/d", e, [], server), (["session_PARKED"], []))
        self.assertEqual(e["sessions"], ["session_PARKED"])
        self.assertEqual(e["synced_from"], "server")

    def test_server_drops_archived(self):
        e = dict(ENTRY, sessions=["session_GONE", "session_UNSEEN"])
        server = [srv("session_GONE", status="archived")]
        self.assertEqual(keeper.sync_one("/d", e, [], server), ([], ["session_GONE"]))
        # Not listed at all is not proof of anything: kept.
        self.assertEqual(e["sessions"], ["session_UNSEEN"])

    def test_archived_child_is_not_added(self):
        e = dict(ENTRY, sessions=[])
        server = [srv("session_EXTRA", status="archived")]
        self.assertEqual(keeper.sync_one("/d", e, self.live, server), ([], []))

    def test_server_works_with_the_host_down(self):
        e = dict(ENTRY, sessions=[])
        os.unlink(os.path.join(self.tmp.name, "projects", "-d", bridge.POINTER))
        self.assertEqual(keeper.sync_one("/d", e, self.live, [srv("session_X")]),
                         (["session_X"], []))


def srv(sid, env="env_KEEP", status="idle"):
    return bridge.ServerSession(sid, env, status, "t")


class DryRun(unittest.TestCase):
    """restore_one(dry_run=True) must touch nothing."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = self.tmp.name
        self.saved = (tmuxctl.window_for, keeper.start, bridge.deregister)
        tmuxctl.window_for = lambda d: None
        keeper.start = bridge.deregister = self.fail

    def tearDown(self):
        tmuxctl.window_for, keeper.start, bridge.deregister = self.saved
        if self.old is None:
            del os.environ["CLAUDE_CONFIG_DIR"]
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self.old
        self.tmp.cleanup()

    def fail(self, *a, **k):
        raise AssertionError("dry run acted")

    def test_release_and_launch_are_only_described(self):
        d = os.path.join(self.tmp.name, "projects", "-d")
        os.makedirs(d)
        with open(os.path.join(d, bridge.POINTER), "w") as f:
            json.dump({"sessionId": "session_OLD", "environmentId": "env_OLD",
                       "pid": 0}, f)
        e = dict(ENTRY, sessions=["session_TWO"])
        before = json.dumps(e, sort_keys=True)
        lines = []
        self.assertTrue(keeper.restore_one("/d", e, log=lines.append, dry_run=True))
        self.assertEqual(json.dumps(e, sort_keys=True), before)
        text = "\n".join(lines)
        self.assertIn("would release stale registration env_OLD", text)
        self.assertIn("would re-queue session_TWO", text)
        with open(os.path.join(d, bridge.POINTER)) as f:
            self.assertIn("env_OLD", f.read())

    def test_interactive(self):
        lines = []
        keeper.restore_interactive("/d", dict(IENTRY), lines.append, dry_run=True)
        self.assertIn("would run claude --resume", lines[0])


class MoveCheck(unittest.TestCase):
    """move_check decides whether crc move may stop and relaunch anything."""

    def check(self, entry=ENTRY, old_alive=False, new_kept=False, new_ptr=None,
              new_alive=False, old="/old", new="/new"):
        return keeper.move_check(old, new, entry, old_alive, new_kept, new_ptr, new_alive)

    def test_plain_move_is_allowed(self):
        self.assertIsNone(self.check())

    def test_same_folder_is_refused(self):
        self.assertIsNotNone(self.check(new="/old"))

    def test_nothing_to_move_is_refused(self):
        self.assertIsNotNone(self.check(entry=None))

    def test_host_running_outside_crc_is_never_stopped(self):
        self.assertIn("outside crc", self.check(old_alive=True))

    def test_kept_destination_is_refused(self):
        self.assertIsNotNone(self.check(new_kept=True))

    def test_live_host_in_destination_is_refused(self):
        self.assertIsNotNone(self.check(new_ptr=ptr("other", "env_other"), new_alive=True))

    def test_dead_host_in_destination_is_left_to_restore(self):
        """restore releases it, under the same rules as any kept folder."""
        self.assertIsNone(self.check(new_ptr=ptr("other", "env_other")))


class Registry(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = self.tmp.name

    def tearDown(self):
        if self.old is None:
            del os.environ["XDG_STATE_HOME"]
        else:
            os.environ["XDG_STATE_HOME"] = self.old
        self.tmp.cleanup()

    def test_empty_when_missing(self):
        self.assertEqual(keeper.load(), {})

    def test_round_trip(self):
        keeper.save({"/d": dict(ENTRY)})
        self.assertEqual(keeper.load(), {"/d": ENTRY})
        with open(keeper.state_path()) as f:
            self.assertEqual(json.load(f)["version"], 1)

    def test_save_leaves_no_temp_files(self):
        keeper.save({"/d": dict(ENTRY)})
        keeper.save({})
        self.assertEqual(os.listdir(keeper.state_dir()), ["sessions.json"])

    def test_resume_argv_is_plain_rc(self):
        """--session-id would force single-session mode and strand the rest."""
        e = dict(ENTRY, args=["--permission-mode", "auto"])
        self.assertEqual(keeper.resume_argv(e),
                         ["claude", "rc", "--spawn=same-dir", "--permission-mode", "auto"])


class Prompts(unittest.TestCase):
    """A host left at a question never registers; crc must say so, not "running"."""

    SPAWN = ("Spawn mode for this project:\n  [1] same-dir\n  [2] worktree\n"
             "Choose [1/2] (default: 1):")

    def test_rc_argv_spells_out_spawn(self):
        self.assertEqual(keeper.rc_argv([], "n"),
                         ["claude", "rc", "--name", "n", "--spawn=same-dir"])

    def test_rc_argv_keeps_a_chosen_spawn(self):
        for args in (["--spawn", "worktree"], ["--spawn=session"]):
            self.assertEqual(keeper.rc_argv(args), ["claude", "rc", *args])

    def test_waiting_at(self):
        self.assertIn("spawn-mode", keeper.waiting_at(self.SPAWN))
        self.assertIn("trust", keeper.waiting_at("Quick safety check: ...\nTrust /d? [y/N]"))
        self.assertIn("[y/N]", keeper.waiting_at("Overwrite it? [y/N]"))
        self.assertIsNone(keeper.waiting_at("Remote Control connected\nsession_01abc"))
        self.assertIsNone(keeper.waiting_at(""))

    def _stuck(self, screen, pointer_pid):
        saved = tmuxctl.capture, bridge.read_pointer
        tmuxctl.capture = lambda wid, lines=40: screen
        bridge.read_pointer = lambda d: bridge.Pointer("s", "e", pointer_pid, "1", "/p")
        try:
            return keeper.stuck("/d", ENTRY, win())
        finally:
            tmuxctl.capture, bridge.read_pointer = saved

    def test_registered_window_is_not_stuck(self):
        self.assertIsNone(self._stuck(self.SPAWN, 1234))

    def test_unregistered_window_at_prompt_is_stuck(self):
        self.assertIn("spawn-mode", self._stuck(self.SPAWN, 999))

    def test_unregistered_window_without_prompt_is_stuck(self):
        self.assertIn("not registered", self._stuck("starting...", 999))

    def test_dead_or_missing_window_is_not_stuck(self):
        self.assertIsNone(keeper.stuck("/d", ENTRY, None))
        self.assertIsNone(keeper.stuck("/d", ENTRY, win(dead=True)))

    def test_untrusted_ends_with_the_fix(self):
        err = keeper.untrusted("/d", "Error: Workspace not trusted.")
        self.assertTrue(err.splitlines()[-1].startswith("untrusted: "))
        self.assertIn("Yes, I trust this folder", err.splitlines()[-1])
        e = dict(ENTRY)
        keeper.finish("/d", e, "@1", err, lambda m: None)
        self.assertEqual(e["last_status"], "untrusted")
        keeper.finish("/d", e, "@1", "stuck: claude stopped at x", lambda m: None)
        self.assertEqual(e["last_status"], "stuck")


class Channels(unittest.TestCase):
    """crc answers the development-channel warning only for the channels it
    was told to, and checks they came up."""

    WARNING = ("WARNING: Loading development channels\n"
               "Channels: server:handback\n"
               "  ❯ 1. I am using this for local development\n    2. Exit\n")

    def test_resume_argv_puts_the_list_flag_last(self):
        e = {"kind": "interactive", "transcript_id": "t", "args": ["--model", "x"],
             "channels": ["server:a", "server:b"]}
        self.assertEqual(keeper.resume_argv(e)[-5:],
                         ["--model", "x", keeper.CHANNEL_FLAG, "server:a", "server:b"])
        del e["channels"]
        self.assertNotIn(keeper.CHANNEL_FLAG, keeper.resume_argv(e))

    def test_entries(self):
        for ok in ("server:handback", "plugin:fakechat@claude-plugins-official"):
            self.assertTrue(keeper.CHANNEL_ENTRY.match(ok), ok)
        for bad in ("server:", "server:a b", "--model", "plugin:x", "server:a;rm"):
            self.assertFalse(keeper.CHANNEL_ENTRY.match(bad), bad)

    def test_listed(self):
        self.assertEqual(keeper.channels_listed("Channels: server:a, server:b\n"),
                         {"server:a", "server:b"})
        self.assertIn("development-channel", keeper.waiting_at(self.WARNING))
        self.assertIn("enabledMcpjsonServers",
                      keeper.waiting_at("New MCP server found in this project: x"))

    def _answer(self, channels, screen=None):
        sent, saved = [], (tmuxctl.tmux, keeper.TRUST_DELAY)
        tmuxctl.tmux = lambda *a, **k: sent.append(a)
        keeper.TRUST_DELAY = 0
        try:
            state, logs = {}, []
            errs = [keeper.answer_channels("/d", "@1", screen or self.WARNING, channels,
                                           state, logs.append) for _ in range(2)]
            return errs, sent, logs
        finally:
            tmuxctl.tmux, keeper.TRUST_DELAY = saved

    def test_not_opted_in_is_never_answered(self):
        errs, sent, _ = self._answer(None)
        self.assertEqual(sent, [])
        self.assertTrue(errs[0].startswith("stuck: "))
        self.assertIn("crc channel", errs[0])

    def test_unexpected_channel_is_never_answered(self):
        errs, sent, _ = self._answer(["server:other"])
        self.assertEqual(sent, [])
        self.assertIn("server:handback", errs[0])

    def test_pinned_channels_are_answered_once_and_logged(self):
        errs, sent, logs = self._answer(["server:handback"])
        self.assertEqual(errs, [None, None])
        self.assertEqual(sent, [("send-keys", "-t", "@1", "Enter")])
        self.assertIn("accepted the development-channel warning", logs[0])

    def test_wrong_default_option_is_never_answered(self):
        screen = self.WARNING.replace("❯ 1.", "  1.").replace("    2. Exit", "  ❯ 2. Exit")
        errs, sent, _ = self._answer(["server:handback"], screen)
        self.assertEqual(sent, [])
        self.assertIsNotNone(errs[0])

    def test_check_finds_the_server_process(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, ".mcp.json"), "w") as f:
                json.dump({"mcpServers": {"up": {"command": "sleep"},
                                          "down": {"command": "/no/such/server"},
                                          "other": {"command": "sleep", "args": ["x.py"]}}}, f)
            child = subprocess.Popen(["sleep", "30"])
            try:
                c = keeper.check_channels(d, os.getpid(), ["server:up", "server:down",
                                                            "server:other", "server:absent",
                                                            "plugin:p@m"], timeout=1)
            finally:
                child.kill()
                child.wait()
        self.assertEqual(c["registered"], ["server:up"])
        self.assertIn("not running", c["missing"]["server:down"])
        self.assertIn("not running", c["missing"]["server:other"])   # sleep, but not x.py
        self.assertIn(".mcp.json", c["missing"]["server:absent"])
        self.assertEqual(c["unchecked"], ["plugin:p@m"])


class Revive(unittest.TestCase):
    """The timer starts again what Claude Code gave up on, and nothing else."""

    def _reason(self, entry, state, age=None):
        saved = keeper.state_of, tmuxctl.window_for, keeper.process_age
        keeper.state_of = lambda d, e: state
        tmuxctl.window_for = lambda d: win()
        keeper.process_age = lambda pid: age
        try:
            return keeper.revive_reason("/d", entry)
        finally:
            keeper.state_of, tmuxctl.window_for, keeper.process_age = saved

    def test_stopped_hosts_are_revived(self):
        self.assertIsNotNone(self._reason(dict(ENTRY), (keeper.LAUNCH, None)))
        self.assertIsNotNone(self._reason(dict(ENTRY), (keeper.RELEASE, "env_X")))

    def test_running_or_foreign_hosts_are_not(self):
        for st in ((keeper.RUNNING, "@1"), (keeper.ELSEWHERE, 9), (keeper.OCCUPIED, 9)):
            self.assertIsNone(self._reason(dict(ENTRY), st), st)

    def test_unregistered_window_only_after_the_grace(self):
        st = (keeper.STUCK, keeper.UNREGISTERED)
        self.assertIsNone(self._reason(dict(ENTRY), st, age=60))
        self.assertIn("minutes", self._reason(dict(ENTRY), st, age=keeper.REVIVE_GRACE + 1))

    def test_window_at_a_question_is_left_for_a_person(self):
        st = (keeper.STUCK, "waiting at the development-channel warning")
        self.assertIsNone(self._reason(dict(ENTRY), st, age=10_000))

    def test_last_outcome_a_person_must_fix_is_not_repeated(self):
        for status in keeper.NEEDS_PERSON:
            e = dict(ENTRY, last_status=status)
            self.assertIsNone(self._reason(e, (keeper.LAUNCH, None)), status)

    def test_no_network_leaves_them_for_the_next_try(self):
        saved = (keeper.revive_reason, keeper.wait_network, keeper.restore_one,
                 tmuxctl.window_for, bridge.live_sessions)
        started, logs = [], []
        keeper.revive_reason = lambda d, e, *rest: "it is not running"
        tmuxctl.window_for = lambda d: None
        bridge.live_sessions = lambda: []
        keeper.wait_network = lambda timeout: False
        keeper.restore_one = lambda d, e, log: started.append(d)
        try:
            with tempfile.TemporaryDirectory() as d:
                self.assertFalse(keeper.revive_all({d: dict(ENTRY)}, logs.append))
                self.assertEqual(started, [])
                keeper.wait_network = lambda timeout: True
                self.assertTrue(keeper.revive_all({d: dict(ENTRY)}, logs.append))
                self.assertEqual(started, [d])
        finally:
            (keeper.revive_reason, keeper.wait_network, keeper.restore_one,
             tmuxctl.window_for, bridge.live_sessions) = saved

    def _gone(self, entry, connection="disconnected", local="idle", t=1000.0):
        srv = {"session_KEEP": bridge.ServerSession("session_KEEP", "", "idle", "", connection)}
        live = [bridge.LiveSession(1234, "t", "/d", "session_KEEP", "cli", "", local)]
        return keeper.disconnected(entry, win(), srv, live, clock=lambda: t)

    def test_disconnected_terminal_host_is_timed_across_runs(self):
        e = dict(ENTRY)
        self.assertEqual(self._gone(e, t=1000.0), 0)
        self.assertEqual(self._gone(e, t=1000.0 + keeper.REVIVE_GRACE), keeper.REVIVE_GRACE)
        self.assertIsNone(self._gone(e, connection="connected"))
        self.assertNotIn("disconnected_at", e)        # reconnecting resets the clock

    def test_offline_time_does_not_count(self):
        e = dict(ENTRY, disconnected_at=1.0)
        srv = {"session_KEEP": bridge.ServerSession("session_KEEP", "", "idle", "",
                                                    "disconnected")}
        self.assertIsNone(keeper.disconnected(e, win(), srv, [], net_ok=False))
        self.assertNotIn("disconnected_at", e)

    def test_busy_session_is_never_counted_down(self):
        e = dict(ENTRY)
        self.assertIsNone(self._gone(e, local="busy"))
        self.assertNotIn("disconnected_at", e)

    def test_unknown_server_state_counts_as_connected(self):
        e = dict(ENTRY, disconnected_at=1.0)
        self.assertIsNone(keeper.disconnected(e, win(), None, []))
        self.assertNotIn("disconnected_at", e)

    def test_lock_is_exclusive(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.environ.get("XDG_STATE_HOME")
            os.environ["XDG_STATE_HOME"] = d
            try:
                with keeper.locked() as a:
                    self.assertTrue(a)
                    with keeper.locked(block=False) as b:
                        self.assertFalse(b)
                with keeper.locked(block=False) as c:
                    self.assertTrue(c)
            finally:
                if old is None:
                    del os.environ["XDG_STATE_HOME"]
                else:
                    os.environ["XDG_STATE_HOME"] = old


class Pause(unittest.TestCase):
    """A paused host stays stopped until crc resume, and pausing never cuts
    off a turn unasked."""

    def test_paused_hosts_are_never_revived(self):
        e = dict(ENTRY, paused="2026-11-06T10:00:00-0800")
        saved = keeper.state_of, tmuxctl.window_for, keeper.process_age
        tmuxctl.window_for = lambda d: win()
        keeper.process_age = lambda pid: 10_000
        try:
            for st in ((keeper.LAUNCH, None), (keeper.RELEASE, "env_X"),
                       (keeper.STUCK, keeper.UNREGISTERED)):
                keeper.state_of = lambda d, e, st=st: st
                self.assertIsNone(keeper.revive_reason("/d", e), st)
        finally:
            keeper.state_of, tmuxctl.window_for, keeper.process_age = saved

    def _state(self, entry, live=()):
        saved = tmuxctl.window_for, bridge.live_sessions
        tmuxctl.window_for = lambda d: None
        bridge.live_sessions = lambda: list(live)
        try:
            return keeper.state_of("/d", entry)
        finally:
            tmuxctl.window_for, bridge.live_sessions = saved

    def test_stopped_and_paused_shows_paused(self):
        e = dict(IENTRY, paused="T")
        self.assertEqual(self._state(e), (keeper.PAUSED, "T"))
        self.assertEqual(self._state(dict(IENTRY)), (keeper.LAUNCH, None))

    def test_paused_but_running_elsewhere_shows_running(self):
        live = [bridge.LiveSession(77, IENTRY["transcript_id"], "/d", "session_RC", "cli")]
        self.assertEqual(self._state(dict(IENTRY, paused="T"), live), (keeper.ELSEWHERE, 77))

    def test_busy_terminal_session(self):
        live = [bridge.LiveSession(1234, "t", "/d", "session_RC", "cli", "", "busy"),
                bridge.LiveSession(99, "u", "/d", "session_X", "cli", "", "busy")]
        self.assertEqual([s.pid for s in keeper.busy(IENTRY, win(), live)], [1234])
        idle = [live[0]._replace(status="idle")]
        self.assertEqual(keeper.busy(IENTRY, win(), idle), [])
        self.assertEqual(keeper.busy(IENTRY, None, live), [])
        self.assertEqual(keeper.busy(IENTRY, win(dead=True), live), [])

    def test_busy_rc_children(self):
        live = [bridge.LiveSession(10, "a", "/d", "session_A", "sdk-cli", "", "waiting"),
                bridge.LiveSession(11, "b", "/d", "session_B", "sdk-cli", "", "idle"),
                bridge.LiveSession(12, "c", "/e", "session_C", "sdk-cli", "", "busy")]
        saved = bridge.parent_pid
        bridge.parent_pid = lambda pid: {10: 1234, 11: 1234, 12: 555}.get(pid)
        try:
            self.assertEqual([s.pid for s in keeper.busy(ENTRY, win(), live)], [10])
        finally:
            bridge.parent_pid = saved

    def test_targets_by_prefix(self):
        path = os.path.join(HERE, "..", "bin", "crc")
        spec = importlib.util.spec_from_loader("crc", SourceFileLoader("crc", path))
        crc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(crc)
        entries = {"/mnt/tank/a": {}, "/mnt/tank/b/c": {}, "/mnt/tankette": {}, "/home/x": {}}

        class A:
            dirs, under = [], "/mnt/tank/"
        self.assertEqual(crc.targets(A, entries), ["/mnt/tank/a", "/mnt/tank/b/c"])
        A.under = "/"
        self.assertEqual(len(crc.targets(A, entries)), 4)
        A.dirs, A.under = ["/mnt/tank/a"], None
        self.assertEqual(crc.targets(A, entries), ["/mnt/tank/a"])
        for A.dirs, A.under in (([], "/srv"), (["/mnt/tank"], None), ([], None),
                                (["/mnt/tank/a"], "/mnt")):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                crc.targets(A, entries)


if __name__ == "__main__":
    unittest.main()


class Socket(unittest.TestCase):
    """The unit and an interactive crc must name the same socket."""

    def test_runtime_dir_ignores_tmux_tmpdir(self):
        with tempfile.TemporaryDirectory() as d:
            env = {"XDG_RUNTIME_DIR": d, "TMUX_TMPDIR": "/elsewhere"}
            self.assertEqual(tmuxctl.runtime_dir(env, 1000), d)

    def test_missing_runtime_dir_falls_back(self):
        got = tmuxctl.runtime_dir({"XDG_RUNTIME_DIR": "/no/such/dir"}, os.getuid())
        self.assertIn(got, (f"/run/user/{os.getuid()}", f"/tmp/tmux-{os.getuid()}"))

    def test_legacy_sockets_cover_unit_and_shell(self):
        env = {"TMUX_TMPDIR": "/run/user/1000", "XDG_RUNTIME_DIR": "/run/user/1000"}
        self.assertEqual(tmuxctl.legacy_sockets(env, 1000),
                         ["/run/user/1000/tmux-1000/crc", "/tmp/tmux-1000/crc"])
