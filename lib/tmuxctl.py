"""The tmux server that hosts kept sessions.

A private server on its own socket, so nothing here can touch the tmux you
work in, and so the systemd unit can own the whole server: every `claude rc`
it hosts lands in the unit's cgroup and is stopped with it.

The socket is named by full path ($XDG_RUNTIME_DIR/crc.tmux, %t/crc.tmux in
the unit), not with `-L`: `-L` puts it under $TMUX_TMPDIR, which login shells
may set (NixOS does) while systemd units don't, and then crc and the unit
would each be looking at a different server.

Windows are identified by a user option, @crc_dir, holding the folder they
serve -- not by name, which the user is free to change.
"""
import os
import pwd
import shutil
import subprocess
from typing import NamedTuple

SOCKET_NAME = "crc.tmux"
LEGACY_NAME = "crc"         # `tmux -L crc`, which crc used up to db169ec
SESSION = "crc"
UNIT = "claude-rc-keeper.service"


class Window(NamedTuple):
    id: str
    dir: str
    pid: int
    dead: bool
    name: str


def runtime_dir(env=None, uid=None):
    """Where the socket lives: what systemd calls %t for the user manager."""
    env = os.environ if env is None else env
    uid = os.getuid() if uid is None else uid
    d = env.get("XDG_RUNTIME_DIR")
    if d and os.path.isdir(d):
        return d
    d = f"/run/user/{uid}"
    if os.path.isdir(d):
        return d
    d = f"/tmp/tmux-{uid}"
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def legacy_sockets(env=None, uid=None):
    """Where `tmux -L crc` may have put the socket, in a unit or a shell."""
    env = os.environ if env is None else env
    uid = os.getuid() if uid is None else uid
    dirs = [env.get("TMUX_TMPDIR") or "/tmp", "/tmp", env.get("XDG_RUNTIME_DIR")]
    out = []
    for d in dirs:
        if d:
            p = os.path.join(d, f"tmux-{uid}", LEGACY_NAME)
            if p not in out:
                out.append(p)
    return out


def _serving(path):
    if not os.path.exists(path):
        return False
    r = subprocess.run(["tmux", "-S", path, "has-session", "-t", f"={SESSION}"],
                       capture_output=True, text=True)
    return r.returncode == 0


_socket = None


def socket():
    """The socket to use. A server still running on an old `-L crc` socket is
    used until it next starts, rather than leaving its sessions unseen."""
    global _socket
    if _socket is None:
        new = os.path.join(runtime_dir(), SOCKET_NAME)
        _socket = new
        if not _serving(new):
            for old in legacy_sockets():
                if _serving(old):
                    _socket = old
                    break
    return _socket


def tmux(*args, check=True):
    r = subprocess.run(["tmux", "-S", socket(), *args],
                       capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"tmux {' '.join(args)}: {r.stderr.strip()}")
    return r


def session_up():
    return tmux("has-session", "-t", f"={SESSION}", check=False).returncode == 0


def unit_installed():
    if not shutil.which("systemctl"):
        return False
    r = subprocess.run(["systemctl", "--user", "cat", UNIT],
                       capture_output=True, text=True)
    return r.returncode == 0


def ensure_session(via_systemd=True):
    """Make sure the crc tmux session exists.

    If the unit is installed, start it rather than tmux directly, so the server
    is always the unit's and `systemctl --user stop` really stops everything.
    Starting the unit also runs a restore, which is harmless: it only launches
    what is not already running.
    """
    if not session_up() and via_systemd and unit_installed():
        subprocess.run(["systemctl", "--user", "start", UNIT])
    if not session_up():
        tmux("new-session", "-d", "-s", SESSION, "-n", "shell",
             "-c", os.path.expanduser("~"))
    # Keep a window whose claude exited, so the reason stays readable and
    # restore can tell "failed" from "never started".
    tmux("set-option", "-gw", "remain-on-exit", "on")


def windows():
    if not session_up():
        return []
    r = tmux("list-windows", "-t", f"={SESSION}", "-F",
             "#{window_id}\t#{@crc_dir}\t#{pane_pid}\t#{pane_dead}\t#{window_name}")
    out = []
    for line in r.stdout.splitlines():
        wid, d, pid, dead, name = line.split("\t", 4)
        if d:
            out.append(Window(wid, d, int(pid or 0), dead == "1", name))
    return out


def window_for(directory):
    for w in windows():
        if w.dir == directory:
            return w
    return None


def get(window_id):
    for w in windows():
        if w.id == window_id:
            return w
    return None


def login_shell():
    return os.environ.get("SHELL") or pwd.getpwuid(os.getuid()).pw_shell or "/bin/sh"


def launch(directory, name, argv):
    """Open a window running argv in directory, through a login shell.

    The login shell gives claude the PATH and environment a terminal would;
    a user service otherwise starts it with systemd's bare one. It execs, so
    the pane's pid is claude's own -- the pid the client writes into its
    bridge pointer, which is how a launch is confirmed.
    """
    r = tmux("new-window", "-d", "-t", f"={SESSION}:", "-n", name,
             "-c", directory, "-P", "-F", "#{window_id}", "--",
             login_shell(), "-lc", 'exec "$0" "$@"', *argv)
    wid = r.stdout.strip()
    tmux("set-option", "-w", "-t", wid, "@crc_dir", directory)
    return wid


def capture(window_id, lines=40):
    r = tmux("capture-pane", "-p", "-t", window_id, "-S", f"-{lines}", check=False)
    return r.stdout.strip()


def interrupt(window_id):
    tmux("send-keys", "-t", window_id, "C-c", check=False)


def kill(window_id):
    tmux("kill-window", "-t", window_id, check=False)
