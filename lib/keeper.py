"""The list of kept sessions, and what to do about each one.

One entry per folder. Two kinds:

  rc           a `claude rc` host, which may serve several sessions. The entry
               remembers its environment, its first session, and every other
               session seen on it (sync). Restore points the folder's bridge
               pointer at that environment, starts a plain multi-session
               `claude rc` -- which reuses the environment and adopts the
               first session -- then re-queues the others onto it. See
               bridge.py for why `--session-id` cannot do this. The server
               lets one host serve a folder at a time, and a dead one from
               another environment blocks the next until it is released.
  interactive  a terminal `claude` with remote control turned on. It never
               registers an environment, so `claude rc` cannot reattach it and
               nothing can block it; it comes back with
               `claude --resume <transcript> --remote-control`, which keeps
               the same claude.ai session.

`plan` is the whole restore policy as a pure function, so it can be tested
without tmux, processes, or the network.
"""
import json
import os
import re
import socket
import tempfile
import time

import bridge
import tmuxctl

# Output from the client when the server still has another host registered
# for the folder.
ALREADY_SERVED = "already served"
# The client's prompt in a folder it has not been told to trust; it waits for
# a keypress, so a launch there never registers.
TRUST_PROMPT = "Quick safety check"
TRUST_MENU_YES = "Yes, I trust this folder"
TRUST_DELAY = 2.0
# What `claude rc` prints instead of the prompt where it cannot ask: with no
# terminal, and on some versions (2.1.281) even in one. It then exits.
NOT_TRUSTED = "Workspace not trusted"
# `claude rc` in a git repo with no --spawn asks this, and waits forever.
SPAWN_PROMPT = "Spawn mode for this project"
SPAWN_DEFAULT = "--spawn=same-dir"
# The last line of a screen waiting on a question: "[y/N]", "Choose [1/2] ...".
PROMPT_LINE = re.compile(r"\[\w+(?:/\w+)+\][^\[\]]{0,40}$")


def state_dir():
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "claude-rc-keeper")


def state_path():
    return os.path.join(state_dir(), "sessions.json")


def load():
    try:
        with open(state_path()) as f:
            return json.load(f).get("sessions", {})
    except FileNotFoundError:
        return {}


def save(entries):
    """Write atomically: a torn write would lose every session we keep."""
    d = state_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".sessions.")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"version": 1, "sessions": entries}, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, state_path())
    except BaseException:
        os.unlink(tmp)
        raise


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# Actions returned by plan().
RUNNING = "running"          # our window has it; nothing to do
ELSEWHERE = "elsewhere"      # its host is alive outside crc; leave it alone
OCCUPIED = "occupied"        # a different live host serves the folder
RELEASE = "release"          # a dead host's registration blocks it; release, then launch
STUCK = "stuck"              # our window is up but never registered (at a prompt?)
LAUNCH = "launch"            # nothing in the way


def kind(entry):
    return entry.get("kind", "rc")


def plan(entry, pointer, pointer_alive, window):
    """Decide what restore should do for one kept folder.

    Returns (action, detail). The one destructive outcome, RELEASE, needs all
    of: a pointer for the folder, its host provably dead, and an environment
    other than the one our session lives on -- resuming reuses that one, so
    deleting it would be releasing the thing we came to resume.
    """
    if window is not None and not window.dead:
        return RUNNING, window.id
    if pointer is not None and pointer_alive:
        if (pointer.session_id == entry["session_id"]
                or pointer.environment_id == entry.get("environment_id")):
            return ELSEWHERE, pointer.pid
        return OCCUPIED, pointer.pid
    if pointer is not None and pointer.environment_id != entry.get("environment_id"):
        return RELEASE, pointer.environment_id
    return LAUNCH, None


def plan_interactive(entry, live, window):
    """The interactive counterpart of plan(): nothing here is ever released."""
    if window is not None and not window.dead:
        return RUNNING, window.id
    for s in live:
        if s.transcript_id == entry["transcript_id"]:
            return ELSEWHERE, s.pid
    return LAUNCH, None


def move_check(old, new, entry, old_alive, new_kept, new_pointer, new_alive):
    """Why a kept session cannot move from old to new, or None if it can.

    A host running under crc is stopped by the move itself; one running
    outside crc is not ours to stop. A live host in new would make the server
    refuse the one we start there; a dead one is released by the restore that
    finishes the move, as for any kept folder.
    """
    if old == new:
        return "old and new are the same folder"
    if entry is None:
        return f"{old} is not kept and has no bridge pointer; nothing to move"
    if old_alive:
        return (f"a claude in {old} is still running outside crc; exit it first "
                f"(Ctrl-C an rc host, /exit a terminal session)")
    if new_kept:
        return f"{new} is already kept; `crc rm` it first"
    if new_pointer is not None and new_alive:
        # One folder, one host: the client refuses a second `claude rc` there
        # even for another environment (and through a symlink), and the server
        # will not re-queue a session onto an environment it was not born on.
        return (f"a claude rc (pid {new_pointer.pid}, session {new_pointer.session_id}) "
                f"is serving {new}, and a folder can have only one host, so "
                f"{old}'s sessions cannot join it. Either stop that host first "
                f"(its own sessions then stay behind on {new_pointer.environment_id}), "
                f"or move {old} somewhere else; or continue one of its "
                f"conversations in {new} with `claude --resume <id> "
                f"--remote-control`, which gets a new claude.ai link")
    return None


def waiting_at(screen):
    """The question a pane is sitting at, or None."""
    lines = [ln.strip() for ln in screen.splitlines() if ln.strip()]
    if TRUST_PROMPT in screen or TRUST_MENU_YES in screen:
        return "Claude Code's trust prompt"
    if SPAWN_PROMPT in screen:
        return "claude rc's spawn-mode prompt"
    if lines and PROMPT_LINE.search(lines[-1]):
        return f"a prompt: {lines[-1]}"
    return None


def stuck(directory, entry, window):
    """Why a live crc window is not serving, or None if it is (or is not live).
    A host waiting at a question is up but never registered, and nobody can
    reach it from claude.ai."""
    if window is None or window.dead:
        return None
    if kind(entry) == "interactive":
        if interactive_started(window) is not None:
            return None
    elif rc_started(directory)(window) is not None:
        return None
    q = waiting_at(tmuxctl.capture(window.id, lines=20))
    return f"waiting at {q}" if q else "up but not registered with the server"


def unstick(directory, entry, log, dry_run=False):
    """The folder's crc window, or None after replacing one that is stuck:
    restore starts afresh rather than call it running."""
    w = tmuxctl.window_for(directory)
    why = stuck(directory, entry, w)
    if why is None:
        return w
    log(f"{directory}: window {w.id} is {why}; {'would replace' if dry_run else 'replacing'} it")
    if not dry_run:
        tmuxctl.kill(w.id)
    return None


def untrusted(directory, said=None):
    """The error for a folder Claude Code has not been told to trust. The fix
    is the last line, which is what `crc ls` shows."""
    return ((f"claude said: {said}\n" if said else "")
            + f"untrusted: Claude Code does not trust {directory} yet. Fix: run "
              f"`claude` there, choose \"{TRUST_MENU_YES}\", exit it, then "
              f"`crc restore` (crc new/move --trust do this themselves)")


def state_of(directory, entry):
    """(action, detail) for one kept folder, as things stand right now."""
    window = tmuxctl.window_for(directory)
    why = stuck(directory, entry, window)
    if why is not None:
        return STUCK, why
    if kind(entry) == "interactive":
        return plan_interactive(entry, bridge.live_sessions(), window)
    pointer = bridge.read_pointer(directory)
    alive = pointer is not None and bridge.host_alive(pointer)
    return plan(entry, pointer, alive, window)


def interactive_in(directory, live):
    """Terminal sessions in directory with remote control on: what adopt can take."""
    return [s for s in live if s.cwd == directory and s.entrypoint == "cli"
            and s.bridge_session_id]


def wait_network(timeout=120):
    """At boot a user service can start before the network is up, and it cannot
    order itself after the system's network-online.target."""
    deadline = time.monotonic() + timeout
    delay = 1
    while True:
        try:
            socket.create_connection(("api.anthropic.com", 443), timeout=5).close()
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(delay)
            delay = min(delay * 2, 15)


def rc_started(directory):
    """Confirm an rc launch: the client writes a bridge pointer with its pid
    once the server has accepted the host."""
    def check(w):
        p = bridge.read_pointer(directory)
        return p if p is not None and p.pid == w.pid else None
    return check


def interactive_started(w):
    """Confirm an interactive launch: its session file shows remote control up."""
    for s in bridge.live_sessions():
        if s.pid == w.pid and s.bridge_session_id:
            return s
    return None


def answer_trust(window_id, screen):
    """Accept the trust prompt on screen: `claude rc` asks [y/N]; an
    interactive `claude` shows a menu whose default is "No, exit"."""
    if TRUST_MENU_YES in screen:
        tmuxctl.tmux("send-keys", "-t", window_id, "Down", check=False)
        time.sleep(0.3)
        tmuxctl.tmux("send-keys", "-t", window_id, "Enter", check=False)
    else:
        tmuxctl.tmux("send-keys", "-t", window_id, "y", "Enter", check=False)


def start(directory, name, argv, confirm, timeout=60, trust=False):
    """Launch argv in a crc window and wait until confirm(window) says it is up.

    Returns (window_id, result, error), where result is whatever confirm
    returned and error is the pane's last output on failure. If Claude Code
    asks whether to trust the folder, trust=True answers yes, once; otherwise
    the launch fails at once rather than waiting out the timeout. Where
    `claude rc` refuses outright instead of asking, trust=True accepts the
    prompt an interactive `claude` still shows, then launches again.
    """
    wid, r, err = launch_and_wait(directory, name, argv, confirm, timeout, trust)
    if err is not None and trust and NOT_TRUSTED in err:
        err = pretrust(directory, name)
        if err is None:
            wid, r, err = launch_and_wait(directory, name, argv, confirm, timeout, trust)
    return wid, r, err


def launch_and_wait(directory, name, argv, confirm, timeout, trust):
    tmuxctl.ensure_session()
    old = tmuxctl.window_for(directory)
    if old is not None:
        tmuxctl.kill(old.id)
    wid = tmuxctl.launch(directory, name, argv)
    deadline = time.monotonic() + timeout
    answered = seen = waiting = None
    while time.monotonic() < deadline:
        time.sleep(0.5)
        w = tmuxctl.get(wid)
        if w is None:
            return wid, None, "window vanished"
        if w.dead:
            out = tmuxctl.capture(wid)
            if NOT_TRUSTED in out:
                said = next(ln.strip() for ln in out.splitlines() if NOT_TRUSTED in ln)
                return wid, None, untrusted(directory, said)
            return wid, None, out or "claude exited"
        r = confirm(w)
        if r is not None:
            return wid, r, None
        screen = tmuxctl.capture(wid, lines=20)
        if TRUST_PROMPT not in screen:
            q = waiting_at(screen)
            if q is None:
                waiting = None
                continue
            # A question we have no answer for: it would wait forever, and
            # a host waiting there serves nothing.
            waiting = waiting or time.monotonic()
            if time.monotonic() - waiting < 5:
                continue
            tmuxctl.kill(wid)
            return wid, None, f"stuck: claude stopped at {q}, which crc cannot answer:\n{screen}"
        if trust and answered is None:
            # The client ignores an answer that comes too soon after the
            # question (keys typed ahead of it), so let it sit a moment.
            seen = seen or time.monotonic()
            if time.monotonic() - seen >= TRUST_DELAY:
                answer_trust(wid, screen)
                answered = time.monotonic()
            continue
        if answered is not None and time.monotonic() - answered < 5:
            continue                    # give the answer time to take
        # Whether to trust a folder is the user's call: only with trust=True
        # (crc new/move --trust) do we answer for them.
        tmuxctl.kill(wid)
        if answered is not None:
            return wid, None, f"answered Claude Code's trust prompt, but it stayed:\n{screen}"
        return wid, None, untrusted(directory)
    return wid, None, f"no registration after {timeout}s:\n{tmuxctl.capture(wid)}"


def pretrust(directory, name, timeout=30):
    """Accept the trust prompt of an interactive `claude` in directory, then
    close it. Returns an error, or None once the prompt has gone."""
    tmuxctl.ensure_session()
    old = tmuxctl.window_for(directory)
    if old is not None:
        tmuxctl.kill(old.id)
    wid = tmuxctl.launch(directory, name, ["claude"])
    try:
        deadline = time.monotonic() + timeout
        answered = seen = None
        while time.monotonic() < deadline:
            time.sleep(0.5)
            w = tmuxctl.get(wid)
            if w is None or w.dead:
                return untrusted(directory, "claude exited at its trust prompt")
            screen = tmuxctl.capture(wid, lines=30)
            if TRUST_MENU_YES not in screen:
                if answered is not None:
                    return None
                continue
            if answered is None:
                seen = seen or time.monotonic()
                if time.monotonic() - seen >= TRUST_DELAY:
                    answer_trust(wid, screen)
                    answered = time.monotonic()
            elif time.monotonic() - answered > 5:
                return f"answered Claude Code's trust prompt, but it stayed:\n{screen}"
        return untrusted(directory, f"no trust prompt from `claude` within {timeout}s")
    finally:
        tmuxctl.kill(wid)


def resume_argv(entry):
    """Plain `claude rc` for a host: it reuses the environment named by the
    pointer we write first, and stays multi-session, which --session-id would
    not."""
    if kind(entry) == "interactive":
        return ["claude", "--resume", entry["transcript_id"], "--remote-control",
                *entry.get("args", [])]
    return rc_argv(entry.get("args", []))


def rc_argv(args, name=None):
    """`claude rc` with --spawn spelled out unless args already choose one:
    in a git repo it otherwise stops to ask, and nobody is there to answer."""
    spawn = [] if any(a == "--spawn" or a.startswith("--spawn=") for a in args) \
        else [SPAWN_DEFAULT]
    return ["claude", "rc", *(["--name", name] if name else []), *spawn, *args]


def sync_one(directory, entry, live=None, server=None):
    """Bring the entry's list of sessions up to date. Returns (added, dropped).

    server is the account's session list (bridge.server_sessions), or None if
    it could not be fetched. With it, every session the server has on our
    environment is kept and every one it reports archived is dropped; that
    covers sessions parked idle, which have no process. Without it, only the
    children of a live host are added, and nothing is dropped, since absence
    there proves nothing -- the entry is marked so `crc ls` can say its list
    may be short.
    """
    if kind(entry) != "rc":
        return [], []
    env = entry.get("environment_id")
    first = entry["session_id"]
    have = set(entry.get("sessions", []))
    seen = set()
    p = bridge.read_pointer(directory)
    if p is not None and bridge.host_alive(p) and p.environment_id == env:
        seen |= set(bridge.host_sessions(p.pid, live))
    archived = set()
    if server is not None:
        for s in server:
            if env and s.environment_id == env:
                (archived if s.status == "archived" else seen).add(s.id)
        entry["synced_from"] = "server"
    else:
        entry["synced_from"] = "processes"
    added = sorted(seen - archived - have - {first})
    dropped = sorted(have & archived)
    if added or dropped:
        entry["sessions"] = sorted((have | set(added)) - archived)
    return added, dropped


def fetch_server_sessions(log=None):
    """The server's session list, or None (logged) if it cannot be had."""
    try:
        return bridge.server_sessions()
    except bridge.BridgeError as e:
        if log:
            log(f"could not list sessions from the server ({e}); "
                f"using local processes only")
        return None


def sync_all(entries, log=print):
    """sync_one over every entry. Returns True if anything changed."""
    live = bridge.live_sessions()
    server = fetch_server_sessions(log)
    changed = False
    for d, e in sorted(entries.items()):
        before = e.get("synced_from")
        added, dropped = sync_one(d, e, live, server)
        for sid in added:
            log(f"{d}: now also keeping {sid}")
        for sid in dropped:
            log(f"{d}: dropped {sid}, archived")
        changed |= bool(added or dropped) or before != e.get("synced_from")
    return changed


def requeue(directory, entry, log):
    """Hand every remembered session but the first back to the restored host.
    The first is adopted by the client itself, from the pointer."""
    keep = []
    for sid in entry.get("sessions", []):
        try:
            bridge.reconnect(entry["environment_id"], sid)
            log(f"{directory}: re-queued {sid}")
            keep.append(sid)
        except bridge.Refused as e:
            log(f"{directory}: dropped {sid}, the server will not resume it: {e}")
        except bridge.BridgeError as e:
            log(f"{directory}: could not re-queue {sid} yet: {e}")
            keep.append(sid)
    entry["sessions"] = keep


def release(environment_id, pointer=None):
    """Deregister one environment and drop the pointer that named it."""
    bridge.deregister(environment_id)
    if pointer is not None and pointer.environment_id == environment_id:
        bridge.remove_pointer(pointer)


def finish(directory, entry, wid, err, log):
    if err is None:
        entry["last_status"], entry["last_error"] = "running", None
        log(f"{directory}: running in {wid}")
        return True
    status = ("blocked" if ALREADY_SERVED in err else
              "untrusted" if "untrusted: " in err else
              "stuck" if err.startswith("stuck: ") else "failed")
    entry["last_status"], entry["last_error"] = status, err.strip()[-2000:]
    log(f"{directory}: {status}:\n{err}")
    return False


def restore_interactive(directory, entry, log, dry_run=False, trust=False):
    action, detail = plan_interactive(entry, bridge.live_sessions(),
                                      unstick(directory, entry, log, dry_run))
    if dry_run:
        if action == RUNNING:
            log(f"{directory}: running in {detail}; nothing to do")
        elif action == ELSEWHERE:
            log(f"{directory}: running outside crc (pid {detail}); would leave it alone")
        else:
            log(f"{directory}: would run {' '.join(resume_argv(entry))}")
        return True
    entry["updated"] = now()
    if action == RUNNING:
        entry["last_status"], entry["last_error"] = "running", None
        return True
    if action == ELSEWHERE:
        log(f"{directory}: running outside crc (pid {detail}); left alone")
        entry["last_status"], entry["last_error"] = "elsewhere", None
        return True
    log(f"{directory}: resuming conversation {entry['transcript_id']}")
    wid, s, err = start(directory, entry.get("name") or os.path.basename(directory),
                        resume_argv(entry), interactive_started, trust=trust)
    if err is None and s.bridge_session_id != entry["session_id"]:
        log(f"{directory}: came back as {s.bridge_session_id}, not "
            f"{entry['session_id']}; the claude.ai link changed")
        entry["session_id"] = s.bridge_session_id
    return finish(directory, entry, wid, err, log)


def describe(directory, entry, action, detail, pointer, log):
    """What restore_one would do, for --dry-run. Changes nothing."""
    if action == RUNNING:
        log(f"{directory}: running in {detail}; nothing to do")
        return
    if action == ELSEWHERE:
        log(f"{directory}: running outside crc (pid {detail}); would leave it alone")
        return
    if action == OCCUPIED:
        log(f"{directory}: blocked by another live claude rc (pid {detail}, "
            f"session {pointer.session_id}); would do nothing")
        return
    if action == RELEASE:
        log(f"{directory}: would release stale registration {detail} "
            f"(session {pointer.session_id}, host pid {pointer.pid} is gone)")
    log(f"{directory}: would point the folder at {entry['environment_id']}, "
        f"run {' '.join(resume_argv(entry))} adopting {entry['session_id']}")
    for sid in entry.get("sessions", []):
        log(f"{directory}: would re-queue {sid}")


def restore_one(directory, entry, log=print, dry_run=False, trust=False):
    """Bring one kept folder back. Updates entry in place; returns True if it
    is running (here or elsewhere) afterwards. With dry_run, only logs what it
    would do."""
    if kind(entry) == "interactive":
        return restore_interactive(directory, entry, log, dry_run, trust)
    pointer = bridge.read_pointer(directory)
    alive = pointer is not None and bridge.host_alive(pointer)
    action, detail = plan(entry, pointer, alive, unstick(directory, entry, log, dry_run))
    if dry_run:
        describe(directory, entry, action, detail, pointer, log)
        return action != OCCUPIED
    entry["updated"] = now()

    if action == RUNNING:
        entry["last_status"], entry["last_error"] = "running", None
        return True
    if action == ELSEWHERE:
        log(f"{directory}: running outside crc (pid {detail}); left alone")
        entry["last_status"], entry["last_error"] = "elsewhere", None
        return True
    if action == OCCUPIED:
        msg = (f"another claude rc (pid {detail}, session {pointer.session_id}) "
               f"is serving this folder; stop it, then `crc restore`")
        log(f"{directory}: {msg}")
        entry["last_status"], entry["last_error"] = "blocked", msg
        return False
    if action == RELEASE:
        log(f"{directory}: releasing stale registration {detail} "
            f"(session {pointer.session_id}, host pid {pointer.pid} is gone)")
        try:
            release(detail, pointer)
        except bridge.BridgeError as e:
            log(f"{directory}: release failed: {e}; trying to resume anyway")

    log(f"{directory}: resuming {entry['environment_id']} with "
        f"{1 + len(entry.get('sessions', []))} session(s)")
    bridge.write_pointer(directory, entry["session_id"], entry["environment_id"])
    wid, p, err = start(directory, entry.get("name") or os.path.basename(directory),
                        resume_argv(entry), rc_started(directory), trust=trust)
    if err is None:
        if p.environment_id != entry["environment_id"]:
            # The server would not give the environment back, so nothing on it
            # can return. Keep what the new host has, so the next restore works.
            log(f"{directory}: came back on a new environment {p.environment_id}; "
                f"the {1 + len(entry.get('sessions', []))} session(s) on "
                f"{entry['environment_id']} cannot follow it")
            entry.update(environment_id=p.environment_id, session_id=p.session_id,
                         sessions=[])
        else:
            requeue(directory, entry, log)
    return finish(directory, entry, wid, err, log)
