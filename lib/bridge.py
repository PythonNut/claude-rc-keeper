"""What Claude Code records about remote-control hosts, and the one server call
needed to clean up after a host that died.

Everything here depends on Claude Code internals that are not a documented
interface: the layout of ~/.claude, the bridge-pointer.json schema, the OAuth
credentials file, and the environments API. It is kept in one module so that
when an update changes any of them, this is the only file to fix -- and so the
rest of the tool degrades to "cannot release" rather than "cannot resume".

Background. `claude remote-control` registers an *environment* with the server
for (machine, directory), and the server allows one live environment per
folder. A host that exits without deregistering -- a crash, a reboot, and also
Ctrl-C, which deliberately keeps the registration so `claude rc -c` can come
back to it -- leaves that environment in place, and every later `claude rc` in
the folder is refused with "This folder is already served ...". Nothing on the
server expires it. The client keeps a note of the last host it ran in each
folder, ~/.claude/projects/<key>/bridge-pointer.json, and that note is how we
find what to release.
"""
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from typing import NamedTuple, Optional

API = "https://api.anthropic.com"
# The client sends this beta header on every environments call; without it the
# endpoint is not routed.
ENVIRONMENTS_BETA = "environments-2025-11-01"
POINTER = "bridge-pointer.json"


class BridgeError(Exception):
    pass


class Pointer(NamedTuple):
    session_id: str
    environment_id: str
    pid: int
    proc_start: str
    path: str


def claude_dir():
    return os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")


def project_key(directory):
    """The directory name Claude Code uses under ~/.claude/projects.

    Every non-alphanumeric character becomes '-', so /mnt/backup/Triage is
    -mnt-backup-Triage. Very long paths are shortened by the client in a way
    not reproduced here; for those, read_pointer finds nothing and the folder
    simply gets no automatic release.
    """
    return re.sub(r"[^a-zA-Z0-9]", "-", directory)


def pointer_path(directory):
    return os.path.join(claude_dir(), "projects", project_key(directory), POINTER)


def parse_pointer(text, path=""):
    """Parse a bridge-pointer.json body, or None if it is not one we understand."""
    try:
        d = json.loads(text)
        return Pointer(str(d["sessionId"]), str(d["environmentId"]),
                       int(d["pid"]), str(d["procStart"]), path)
    except (ValueError, KeyError, TypeError):
        return None


def read_pointer(directory):
    path = pointer_path(directory)
    try:
        with open(path) as f:
            return parse_pointer(f.read(), path)
    except OSError:
        return None


def remove_pointer(pointer):
    """Delete a pointer whose environment has been released.

    Left behind, it would send `claude rc -c` to an environment that no longer
    exists, and would make us try to release it again.
    """
    try:
        os.unlink(pointer.path)
    except FileNotFoundError:
        pass


def parse_proc_start(stat_line):
    """Field 22 of /proc/<pid>/stat: start time in clock ticks since boot.

    This is what the client stores as procStart. Fields are counted after the
    last ')' because the command name in field 2 may itself contain spaces and
    parentheses.
    """
    rest = stat_line[stat_line.rindex(")") + 2:].split()
    return rest[22 - 3]


def proc_start(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            return parse_proc_start(f.read())
    except (OSError, ValueError, IndexError):
        return None


def host_alive(pointer):
    """True if the process that wrote the pointer is still the one running.

    The pid alone is not enough: after a reboot the same number is soon reused
    by something else. The start time pins it to one process lifetime, which is
    the same check the client itself makes.
    """
    return proc_start(pointer.pid) == pointer.proc_start


class LiveSession(NamedTuple):
    """One running Claude Code process, from ~/.claude/sessions/<pid>.json."""
    pid: int
    transcript_id: str          # the conversation id `claude --resume` takes
    cwd: str
    bridge_session_id: str      # its remote-control session, or "" if none
    entrypoint: str             # "cli" for a terminal session; rc children are "sdk-cli"


def parse_session(text):
    try:
        d = json.loads(text)
        return (LiveSession(int(d["pid"]), str(d["sessionId"]), str(d["cwd"]),
                            str(d.get("bridgeSessionId") or ""),
                            str(d.get("entrypoint") or "")),
                str(d["procStart"]))
    except (ValueError, KeyError, TypeError):
        return None


def live_sessions():
    """Every Claude Code process still running, per its own registry.

    A process that dies uncleanly leaves its file behind, so each entry is
    checked against /proc the same way a bridge pointer is.
    """
    d = os.path.join(claude_dir(), "sessions")
    out = []
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, n)) as f:
                parsed = parse_session(f.read())
        except OSError:
            continue
        if parsed is not None and proc_start(parsed[0].pid) == parsed[1]:
            out.append(parsed[0])
    return out


def access_token():
    """The OAuth access token Claude Code is logged in with.

    We never refresh it ourselves: refreshing rotates the refresh token, and
    doing that behind the client's back could log it out. An expired token
    means release is skipped until `claude` has been run and refreshed it.
    """
    path = os.path.join(claude_dir(), ".credentials.json")
    try:
        with open(path) as f:
            oauth = json.load(f)["claudeAiOauth"]
    except (OSError, ValueError, KeyError) as e:
        raise BridgeError(f"cannot read Claude Code credentials at {path}: {e}")
    if oauth.get("expiresAt", 0) / 1000 < time.time() + 60:
        raise BridgeError("Claude Code's login token has expired; run `claude` "
                          "once to refresh it, then retry")
    return oauth["accessToken"]


def claude_version():
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True,
                             text=True, timeout=30).stdout
        return out.split()[0]
    except (OSError, IndexError, subprocess.TimeoutExpired):
        return "unknown"


def deregister(environment_id, token=None, base=API):
    """Delete one environment registration on the server.

    The same call the client makes on a clean shutdown. Returns True if the
    server deleted it or no longer has it.
    """
    if not re.fullmatch(r"env_[A-Za-z0-9]+", environment_id):
        raise BridgeError(f"refusing malformed environment id {environment_id!r}")
    req = urllib.request.Request(
        f"{base}/v1/environments/bridge/{environment_id}", method="DELETE",
        headers={"Authorization": f"Bearer {token or access_token()}",
                 "anthropic-version": "2023-06-01",
                 "anthropic-beta": ENVIRONMENTS_BETA,
                 "x-environment-runner-version": claude_version()})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return True
        body = e.read().decode(errors="replace")[:300]
        raise BridgeError(f"deregister {environment_id}: HTTP {e.code} {body}")
    except urllib.error.URLError as e:
        raise BridgeError(f"deregister {environment_id}: {e.reason}")
