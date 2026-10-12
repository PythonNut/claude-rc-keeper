#!/usr/bin/env python3
"""Tests for the restore policy and the session list.

plan() is the only place that decides to delete a server registration. The
cases below are the ones where getting it wrong either deletes something live
or deletes the environment the kept session is about to be resumed on.
"""
import json
import os
import sys
import tempfile
import unittest

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

    def test_resume_argv_carries_extra_flags(self):
        e = dict(ENTRY, args=["--permission-mode", "auto"])
        self.assertEqual(keeper.resume_argv(e),
                         ["claude", "rc", "--session-id", "session_KEEP",
                          "--permission-mode", "auto"])


if __name__ == "__main__":
    unittest.main()
