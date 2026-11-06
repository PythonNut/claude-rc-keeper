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
import contextlib
import fcntl
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
# Development channels: MCP servers that push messages into a conversation.
# A terminal session can load them; `claude rc` rejects the flag. Each launch
# shows a warning whose default answer accepts it.
CHANNEL_FLAG = "--dangerously-load-development-channels"
CHANNEL_WARNING = "WARNING: Loading development channels"
CHANNEL_ACCEPT = re.compile(r"❯\s*1\.\s*I am using this for local development")
CHANNEL_LIST = re.compile(r"^\s*Channels: (.+?)\s*$", re.M)
CHANNEL_ENTRY = re.compile(r"^(server:[\w.-]+|plugin:[\w.-]+@[\w.-]+)$")
# A .mcp.json server the folder has not approved yet; it can be pre-approved
# with enabledMcpjsonServers in the folder's .claude/settings.local.json.
MCP_CONSENT = "New MCP server found in this project"
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


@contextlib.contextmanager
def locked(block=True):
    """Hold crc's lock while starting or stopping hosts, so the timer never
    races a boot restore or a crc command. Yields False if block is False and
    another crc holds it."""
    os.makedirs(state_dir(), mode=0o700, exist_ok=True)
    with open(os.path.join(state_dir(), "lock"), "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if block else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True


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
PAUSED = "paused"            # stopped on purpose (crc pause); nothing starts it but crc resume
UNREGISTERED = "up but not registered with the server"

# Claude Code stops trying to reach the server after 10 minutes without it
# (claude rc exits; a terminal session turns remote control off). The sync
# timer starts such hosts again once the server is reachable, and does not
# touch a window younger than this, which may still be starting or retrying.
REVIVE_GRACE = 600
# Last outcomes a person has to fix first; the timer would only repeat them.
NEEDS_PERSON = ("untrusted", "stuck")
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
    if CHANNEL_WARNING in screen:
        return "the development-channel warning"
    if MCP_CONSENT in screen:
        return ("the new-MCP-server prompt (pre-approve the server with "
                "enabledMcpjsonServers in the folder's .claude/settings.local.json)")
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
    return f"waiting at {q}" if q else UNREGISTERED


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
    """(action, detail) for one kept folder, as things stand right now. A
    paused one that is somehow running anyway shows as running."""
    window = tmuxctl.window_for(directory)
    why = stuck(directory, entry, window)
    if why is not None:
        return STUCK, why
    if kind(entry) == "interactive":
        action, detail = plan_interactive(entry, bridge.live_sessions(), window)
    else:
        pointer = bridge.read_pointer(directory)
        alive = pointer is not None and bridge.host_alive(pointer)
        action, detail = plan(entry, pointer, alive, window)
    if entry.get("paused") and action in (LAUNCH, RELEASE):
        return PAUSED, entry["paused"]
    return action, detail


def busy(entry, window, live):
    """The sessions in a crc window that are mid-turn (or waiting on a
    permission answer), which stopping the window would cut off: the
    terminal session itself, or an rc host's children."""
    if window is None or window.dead:
        return []
    if kind(entry) == "interactive":
        mine = [s for s in live if s.pid == window.pid]
    else:
        mine = [s for s in live if bridge.parent_pid(s.pid) == window.pid]
    return [s for s in mine if s.status not in ("", "idle")]


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


def start(directory, name, argv, confirm, timeout=60, trust=False, channels=None,
          log=print):
    """Launch argv in a crc window and wait until confirm(window) says it is up.

    Returns (window_id, result, error), where result is whatever confirm
    returned and error is the pane's last output on failure. If Claude Code
    asks whether to trust the folder, trust=True answers yes, once; otherwise
    the launch fails at once rather than waiting out the timeout. Where
    `claude rc` refuses outright instead of asking, trust=True accepts the
    prompt an interactive `claude` still shows, then launches again.

    channels, if given, are the development channels crc may accept the
    warning for: it answers only when the warning lists exactly those.
    Otherwise the window is left at the warning, for someone to answer.
    """
    wid, r, err = launch_and_wait(directory, name, argv, confirm, timeout, trust,
                                  channels, log)
    if err is not None and trust and NOT_TRUSTED in err:
        err = pretrust(directory, name)
        if err is None:
            wid, r, err = launch_and_wait(directory, name, argv, confirm, timeout, trust,
                                          channels, log)
    return wid, r, err


def channels_listed(screen):
    """The channels a development-channel warning names."""
    m = CHANNEL_LIST.search(screen)
    return set(m.group(1).split(", ")) if m else set()


def answer_channels(directory, wid, screen, channels, state, log):
    """Deal with the development-channel warning on screen. Returns an error
    to give up with, or None to keep waiting. state remembers, across calls,
    when the warning was first seen and when it was answered."""
    listed = channels_listed(screen)
    if channels is None:
        why = "crc answers it only for hosts set with `crc channel DIR ... --accept`"
    elif listed != set(channels):
        why = (f"it lists {', '.join(sorted(listed)) or 'nothing crc can read'}, "
               f"but crc may accept only {', '.join(channels)}")
    elif not CHANNEL_ACCEPT.search(screen):
        why = "its first option is not the one crc knows how to accept"
    else:
        now_ = time.monotonic()
        if "answered" not in state:
            state.setdefault("seen", now_)
            if now_ - state["seen"] >= TRUST_DELAY:     # see TRUST_DELAY
                tmuxctl.tmux("send-keys", "-t", wid, "Enter", check=False)
                state["answered"] = now_
                log(f"{directory}: accepted the development-channel warning "
                    f"for {', '.join(channels)}")
            return None
        if now_ - state["answered"] < 5:
            return None
        why = "crc accepted it, but it stayed"
    # Left open: whoever set the channels can answer it in `crc attach`.
    return (f"stuck: claude is waiting at its development-channel warning; {why}. "
            f"Answer it in `crc attach`, or `crc rm` the host")


def mcp_servers(directory):
    """The folder's .mcp.json servers, {name: config}."""
    try:
        with open(os.path.join(directory, ".mcp.json")) as f:
            servers = json.load(f).get("mcpServers", {})
        return servers if isinstance(servers, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def descendant_pids(pid):
    """Every process under pid."""
    kids = {}
    for d in os.listdir("/proc"):
        if d.isdigit():
            pp = bridge.parent_pid(int(d))
            if pp is not None:
                kids.setdefault(pp, []).append(int(d))
    out, todo = [], list(kids.get(pid, []))
    while todo:
        p = todo.pop()
        out.append(p)
        todo.extend(kids.get(p, []))
    return out


def cmdline(pid):
    """argv of pid, or None if it is gone."""
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
    except OSError:
        return None


def descendants(pid):
    """argv of every process under pid."""
    return [a for a in map(cmdline, descendant_pids(pid)) if a is not None]


def check_channels(directory, pid, channels, timeout=20):
    """Whether each channel's server came up under the session.

    The notice that names registered channels is drawn under the banner,
    and a resumed conversation scrolls it away before anything can read it.
    What can be seen is the server process: a server: channel is running if
    the command its .mcp.json entry names is a child of the session. Only
    the folder's .mcp.json is consulted; plugin: channels are not checked.
    Returns {"at", "registered", "missing": {channel: reason}, "unchecked"}.
    """
    servers = mcp_servers(directory)
    want, missing, unchecked = {}, {}, []
    for ch in channels:
        kind_, _, name = ch.partition(":")
        if kind_ != "server":
            unchecked.append(ch)
        elif not isinstance(servers.get(name), dict) or not servers[name].get("command"):
            missing[ch] = f"no {name!r} server with a command in {directory}/.mcp.json"
        else:
            # The command, and the script it runs if args name one: an
            # interpreter alone (python3, bun) would match any server.
            args = servers[name].get("args") or []
            want[ch] = [os.path.basename(servers[name]["command"])] + \
                ([os.path.basename(str(args[0]))] if args else [])
    found = set()
    deadline = time.monotonic() + timeout
    while want.keys() - found and time.monotonic() < deadline:
        procs = descendants(pid)
        for ch, names in want.items():
            if any(all(n in [os.path.basename(a) for a in argv[:3]] for n in names)
                   for argv in procs):
                found.add(ch)
        if want.keys() - found:
            time.sleep(1)
    for ch in want.keys() - found:
        missing[ch] = f"its server ({servers[ch.partition(':')[2]]['command']}) is not running under the session"
    return {"at": now(), "registered": [c for c in channels if c in found],
            "missing": missing, "unchecked": unchecked}


def launch_and_wait(directory, name, argv, confirm, timeout, trust, channels=None,
                    log=print):
    tmuxctl.ensure_session()
    old = tmuxctl.window_for(directory)
    if old is not None:
        tmuxctl.kill(old.id)
    wid = tmuxctl.launch(directory, name, argv)
    deadline = time.monotonic() + timeout
    answered = seen = waiting = None
    warned = {}
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
        if CHANNEL_WARNING in screen:
            err = answer_channels(directory, wid, screen, channels, warned, log)
            if err is not None:
                return wid, None, f"{screen}\n{err}"
            continue
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
            return wid, None, f"{screen}\nstuck: claude stopped at {q}, which crc cannot answer"
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
        # The channel flag takes a list, so it goes last.
        channels = entry.get("channels") or []
        return ["claude", "--resume", entry["transcript_id"], "--remote-control",
                *entry.get("args", []), *([CHANNEL_FLAG, *channels] if channels else [])]
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


def process_age(pid):
    """Seconds since pid started, or None."""
    start = bridge.proc_start(pid)
    try:
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        return up - int(start) / os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError, TypeError):
        return None


def disconnected(entry, window, server, live, net_ok=True, clock=time.time):
    """For a terminal host running in our window: how long claude.ai has
    seen it disconnected while the server was reachable, or None if it is
    connected (or that is unknown).

    A safety net: a terminal session keeps retrying (2.1.286 came back by
    itself, about 3 minutes after an 11-minute outage ended). If one did stop,
    its session file would still name the remote session, so only the
    server's connection_status shows it. Time spent offline does not count,
    so a session that is about to reconnect is not restarted. Records when it
    was first seen down in entry["disconnected_at"], across timer runs.
    """
    s = server.get(bridge.canonical(entry["session_id"])) if server else None
    if (not net_ok or s is None or s.connection != "disconnected"
            or s.status == "archived"):
        entry.pop("disconnected_at", None)
        return None
    # Never cut off a turn in progress, whatever the server thinks.
    if any(x.pid == window.pid and x.status not in ("", "idle") for x in live):
        return None
    since = entry.setdefault("disconnected_at", clock())
    return clock() - since


def revive_reason(directory, entry, server=None, live=(), net_ok=True):
    """Why the timer should start this kept host again, or None. server maps
    canonical session ids to the server's list (None if unavailable)."""
    if entry.get("paused") or entry.get("last_status") in NEEDS_PERSON:
        return None
    action, detail = state_of(directory, entry)
    if action in (LAUNCH, RELEASE):
        return "it is not running"
    w = tmuxctl.window_for(directory)
    if action == STUCK and detail == UNREGISTERED:
        age = process_age(w.pid) if w else None
        if age is not None and age >= REVIVE_GRACE:
            return f"it has not been connected for {int(age // 60)}+ minutes"
    if action == RUNNING and kind(entry) == "interactive" and w is not None:
        gone = disconnected(entry, w, server, live, net_ok)
        if gone is not None and gone >= REVIVE_GRACE:
            return f"claude.ai has not reached it for {int(gone // 60)}+ minutes"
    return None


def revive_all(entries, log=print):
    """Start again every kept host that stopped or lost the server, if the
    server is reachable now; otherwise leave them for the next try. Returns
    True if any entry changed."""
    server = None
    if any(kind(e) == "interactive" for e in entries.values()):
        listed = fetch_server_sessions()
        server = {s.id: s for s in listed} if listed is not None else None
    live = bridge.live_sessions()
    net_ok = wait_network(timeout=10)
    before = {d: e.get("disconnected_at") for d, e in entries.items()}
    due = [(d, e, why) for d, e in sorted(entries.items())
           if os.path.isdir(d) for why in [revive_reason(d, e, server, live, net_ok)] if why]
    changed = any(e.get("disconnected_at") != before[d] for d, e in entries.items())
    if not due:
        return changed
    if not net_ok:
        log(f"api.anthropic.com unreachable; {len(due)} host(s) left for the next try")
        return changed
    for d, e, why in due:
        log(f"{d}: starting it again: {why}")
        w = tmuxctl.window_for(d)
        if w is not None and not w.dead:
            tmuxctl.kill(w.id)          # alive but unreachable: replace it
        e.pop("disconnected_at", None)
        restore_one(d, e, log)
    return True


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
              "stuck" if re.search(r"^stuck: ", err, re.M) else "failed")
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
            if entry.get("channels") and entry.get("accept_channels"):
                log(f"{directory}: would accept the development-channel warning "
                    f"for {', '.join(entry['channels'])}")
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
    channels = entry.get("channels") or []
    wid, s, err = start(directory, entry.get("name") or os.path.basename(directory),
                        resume_argv(entry), interactive_started, trust=trust,
                        channels=channels if entry.get("accept_channels") else None,
                        log=log)
    if err is None and s.bridge_session_id != entry["session_id"]:
        log(f"{directory}: came back as {s.bridge_session_id}, not "
            f"{entry['session_id']}; the claude.ai link changed")
        entry["session_id"] = s.bridge_session_id
    if err is None and channels:
        entry["channel_check"] = c = check_channels(directory, s.pid, channels)
        for ch, why in c["missing"].items():
            log(f"{directory}: channel {ch} is not up: {why}")
        if c["registered"]:
            log(f"{directory}: channel servers running: {', '.join(c['registered'])}")
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
