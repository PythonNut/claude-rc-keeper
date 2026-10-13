#!/usr/bin/env python3
"""Tests for reading what Claude Code records about remote-control hosts.

The liveness check decides whether a registration may be deleted, so it must
never call a different process -- one that reused the pid -- the same host.
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "lib"))
import bridge          # noqa: E402

REAL = ('{"sessionId":"session_016y65XhVKRe26QStSeVCAC8",'
        '"environmentId":"env_0196MW6MqMpxgVgW61PBGTEe","source":"standalone",'
        '"pid":7674,"procStart":"124430"}')


class ProjectKey(unittest.TestCase):

    def test_matches_client_layout(self):
        self.assertEqual(bridge.project_key("/mnt/backup/Backup/Hyak"),
                         "-mnt-backup-Backup-Hyak")

    def test_non_alphanumerics_all_become_dashes(self):
        self.assertEqual(bridge.project_key("/a b/c.d_e"), "-a-b-c-d-e")


class Pointer(unittest.TestCase):

    def test_parses_real_pointer(self):
        p = bridge.parse_pointer(REAL, "/x")
        self.assertEqual(p.session_id, "session_016y65XhVKRe26QStSeVCAC8")
        self.assertEqual(p.environment_id, "env_0196MW6MqMpxgVgW61PBGTEe")
        self.assertEqual(p.pid, 7674)
        self.assertEqual(p.proc_start, "124430")

    def test_garbage_is_none_not_an_exception(self):
        for text in ("", "{", "[]", '{"sessionId":"s"}', '{"pid":"x"}'):
            self.assertIsNone(bridge.parse_pointer(text))

    def test_read_pointer_uses_config_dir(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "projects", "-srv-x"))
            with open(os.path.join(d, "projects", "-srv-x", bridge.POINTER), "w") as f:
                f.write(REAL)
            old = os.environ.get("CLAUDE_CONFIG_DIR")
            os.environ["CLAUDE_CONFIG_DIR"] = d
            try:
                self.assertEqual(bridge.read_pointer("/srv/x").pid, 7674)
                self.assertIsNone(bridge.read_pointer("/srv/y"))
            finally:
                if old is None:
                    del os.environ["CLAUDE_CONFIG_DIR"]
                else:
                    os.environ["CLAUDE_CONFIG_DIR"] = old


class Liveness(unittest.TestCase):

    def test_proc_start_survives_hostile_comm(self):
        # comm may contain spaces and ')' -- fields must be counted from the last ')'
        line = "42 (a) b (c)) S " + " ".join(str(n) for n in range(4, 53))
        self.assertEqual(bridge.parse_proc_start(line), "22")

    def test_self_is_alive(self):
        me = bridge.Pointer("s", "env_x", os.getpid(), bridge.proc_start(os.getpid()), "")
        self.assertTrue(bridge.host_alive(me))

    def test_reused_pid_is_not_alive(self):
        """Same pid, different start time: a different process."""
        other = bridge.Pointer("s", "env_x", os.getpid(), "1", "")
        self.assertFalse(bridge.host_alive(other))

    def test_missing_pid_is_not_alive(self):
        self.assertFalse(bridge.host_alive(bridge.Pointer("s", "env_x", 2**22 + 7, "1", "")))


class Sessions(unittest.TestCase):

    REAL = ('{"pid":662484,"sessionId":"4e605284-c166-4a59-a5f9-6b5649b23c01",'
            '"cwd":"/home/u/x","startedAt":1,"procStart":"777","kind":"interactive",'
            '"entrypoint":"cli","bridgeSessionId":"session_01BnBGAf6HuFGdg1EdD3Lt39"}')

    def test_parses_real_session_file(self):
        s, start = bridge.parse_session(self.REAL)
        self.assertEqual(s.pid, 662484)
        self.assertEqual(s.transcript_id, "4e605284-c166-4a59-a5f9-6b5649b23c01")
        self.assertEqual(s.bridge_session_id, "session_01BnBGAf6HuFGdg1EdD3Lt39")
        self.assertEqual(s.entrypoint, "cli")
        self.assertEqual(start, "777")

    def test_no_remote_control_is_empty_not_missing(self):
        s, _ = bridge.parse_session('{"pid":1,"sessionId":"t","cwd":"/","procStart":"1"}')
        self.assertEqual(s.bridge_session_id, "")

    def test_stale_files_are_not_live(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "sessions"))
            me = os.getpid()
            for pid, start in ((me, bridge.proc_start(me)), (me + 1, "1")):
                with open(os.path.join(d, "sessions", f"{pid}.json"), "w") as f:
                    f.write(f'{{"pid":{pid},"sessionId":"t{pid}","cwd":"/",'
                            f'"procStart":"{start}"}}')
            old = os.environ.get("CLAUDE_CONFIG_DIR")
            os.environ["CLAUDE_CONFIG_DIR"] = d
            try:
                self.assertEqual([s.pid for s in bridge.live_sessions()], [me])
            finally:
                if old is None:
                    del os.environ["CLAUDE_CONFIG_DIR"]
                else:
                    os.environ["CLAUDE_CONFIG_DIR"] = old


class RestorePointer(unittest.TestCase):

    def test_write_then_read_round_trips_without_pid(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.environ.get("CLAUDE_CONFIG_DIR")
            os.environ["CLAUDE_CONFIG_DIR"] = d
            try:
                bridge.write_pointer("/srv/x", "session_A", "env_E")
                p = bridge.read_pointer("/srv/x")
                self.assertEqual((p.session_id, p.environment_id, p.pid), ("session_A", "env_E", 0))
                self.assertFalse(bridge.host_alive(p))
                self.assertEqual(os.listdir(os.path.dirname(p.path)), [bridge.POINTER])
            finally:
                if old is None:
                    del os.environ["CLAUDE_CONFIG_DIR"]
                else:
                    os.environ["CLAUDE_CONFIG_DIR"] = old


class Ids(unittest.TestCase):

    def test_canonical(self):
        self.assertEqual(bridge.canonical("cse_01abc"), "session_01abc")
        self.assertEqual(bridge.canonical("session_01abc"), "session_01abc")

    def test_parent_pid_survives_hostile_comm(self):
        self.assertEqual(bridge.parse_parent_pid("42 (a) b (c)) S 777 1 1"), 777)

    def test_parent_of_child_is_us(self):
        import subprocess
        c = subprocess.Popen(["sleep", "5"])
        try:
            self.assertEqual(bridge.parent_pid(c.pid), os.getpid())
        finally:
            c.kill()
            c.wait()


class Deregister(unittest.TestCase):

    def test_refuses_malformed_id_before_any_request(self):
        for bad in ("", "env_", "env_x/../y", "session_01abc", "env_a b"):
            with self.assertRaises(bridge.BridgeError):
                bridge.deregister(bad, token="t", base="http://127.0.0.1:9")

    def test_reconnect_refuses_malformed_ids_before_any_request(self):
        for env, sid in (("env_x/..", "session_a"), ("env_a", "session_a/../b"),
                         ("env_a", "env_b"), ("env_a", "")):
            with self.assertRaises(bridge.BridgeError):
                bridge.reconnect(env, sid, token="t", base="http://127.0.0.1:9")


if __name__ == "__main__":
    unittest.main()
