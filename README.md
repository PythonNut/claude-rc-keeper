# claude-rc-keeper

Keeps Claude Code remote-control sessions (`claude rc`) running across reboots.

Kept sessions run in windows of a private tmux server (socket
`$XDG_RUNTIME_DIR/crc.tmux`; `crc attach` opens it) owned by a
systemd user unit. When the unit starts, it resumes each one. It handles two
kinds:

- **rc**: a `claude rc` host, resumed on its original environment with every
  session it was serving (see below).
- **interactive**: a terminal `claude` with remote control turned on (via
  `/remote-control` or `--remote-control`), resumed with
  `claude --resume <conversation> --remote-control`. That keeps the same
  claude.ai link. `claude rc --session-id` can't take these over: they never
  register an environment, and it fails with "has no environment_id".

Before resuming an rc host, it releases any server registration a dead host
left on the folder, which is what otherwise causes:

    Error: This folder is already served by a terminal `claude remote-control` on this device. Stop it first.

## Why that error happens

`claude rc` registers an *environment* for (machine, folder) with the server,
which allows one per folder. A host that exits without deregistering leaves its
environment there, and the server never expires it. That includes a crash, a
reboot, and Ctrl-C, which keeps the registration on purpose so `claude rc -c`
can reattach. If you ever started a second `claude rc` in a folder by accident,
that leftover blocks `--session-id` from resuming the session you wanted, and
`-c` only reattaches to the accidental one.

The client records the last host per folder in
`~/.claude/projects/<folder-key>/bridge-pointer.json`. `crc` uses that file to
find the stale environment and makes the same `DELETE` call the client makes on
a clean shutdown.

## How a host comes back with all its sessions

One `claude rc` host can serve many sessions, and each session is bound to the
host's *environment*. The server won't hand a session to any other
environment ("Session does not belong to this environment"). So the host has
to come back on the same one. Here is what the client offers:

- `claude rc --session-id X` and `claude rc -c` reuse the environment, but
  always start in **single-session** mode ("Single session · exits when
  complete"), so the host's other sessions can't come back through them.
- A plain `claude rc` reads the folder's bridge pointer, asks to reuse that
  environment, and stays **multi-session**. The server grants the reuse even
  for an environment that was deregistered. The client adopts the session the
  pointer names.

So restore:

1. writes a fresh pointer naming the environment and the first session (the
   client ignores pointers older than a few hours, and a long outage would
   otherwise lose the environment),
2. starts a plain `claude rc`,
3. once it has registered, re-queues every other remembered session onto the
   same environment with `POST /v1/environments/<env>/bridge/reconnect`. That's
   the call the client uses to adopt its own sessions.

The pointer only ever names one session, so crc remembers the rest itself.
`crc sync` asks the server for the account's sessions (`GET /v1/sessions`, the
list `claude --teleport` uses), each of which names the environment it is
bound to. It keeps every session on a kept host's environment, including ones
parked idle, and drops the ones the server reports archived. It also adds the
sessions a live host's child processes are serving. A user timer runs it every
3 minutes, and `adopt` and `restore` run it too (restore before it starts
anything).

If the server can't be reached, sync falls back to child processes only and
removes nothing, since an idle session has no process. `crc ls` then flags the
host as possibly missing sessions. A session also leaves the list when the
server refuses to re-queue it.

## Moving a folder

`crc move OLD NEW` carries a kept session to a folder its files were moved
to. It copies the conversations to NEW's project key, stops a crc-run host in
OLD, and restores in NEW. The server lets an environment be reused from a
different folder (tested on 2.1.283), so an rc host keeps its environment and
every claude.ai link; the old folder's pointer is removed so it can't claim
the environment back. A folder that isn't kept but has a pointer is moved too,
with only the session the pointer names. A host still running outside crc is
refused: exit it first.

## Staying connected

`claude rc` gives up on the server after 10 minutes without it (a hardcoded
limit in 2.1.286) and exits. The sync timer runs `crc sync --revive`, which
keeps trying instead. Every 3 minutes, if api.anthropic.com is reachable, it
starts again any kept host that has stopped, and any whose window has been up
for 10 minutes without registering with the server. A terminal session
keeps retrying by itself (it came back about 3 minutes after an 11-minute
outage); as a safety net, one that claude.ai reports disconnected for 10
minutes while the server is reachable, and that is not in the middle of a
turn, is restarted too. It leaves alone a host at a
question a person has to answer (trust, a new MCP server, an unaccepted
channel warning), and one whose last start failed for that reason, until
`crc restore` is run by hand.

Sessions can also drop while their host stays up: an rc host serves each
one from a child process, and if the children exit (on 2026-11-05 a failed
token refresh did that to every host on one machine), claude.ai shows the
sessions idle and disconnected and nothing brings them back until someone
messages them. `crc poke DIR...` (or `--under PATH`, `--dry-run`) re-queues
every session on a live kept rc host's environment that the server shows
disconnected and no child is serving, with the call restore uses; the host
then starts a child for each, and poke waits up to 30 seconds to see it. A
session nobody has opened since its host started looks the same and just
gets its child early. crc never pokes on its own, so a session that keeps
dying can't make it loop; `crc ls --json` gives each session's
`connection`.

To stop a host for a while, `crc pause` it;
to stop it for good, `crc rm` it. One stopped by hand in `crc attach` is
started again by the next timer run.

## Pausing

    crc pause DIR...  |  crc pause --under PATH     stop them; keep them stopped
    crc resume DIR... |  crc resume --under PATH    start them again

`crc pause` stops kept hosts the way `crc rm` does (Ctrl-C an rc host,
`/exit` a terminal one) but keeps their entries, environments and claude.ai
links. The timer and the restore at boot skip a paused host, so it stays
stopped across reboots, and `crc ls` shows it as paused. `--under PATH` takes
every kept folder at or below PATH, e.g. all of them on a disk about to be
unmounted. A host with a session mid-turn, or waiting on a permission answer,
is left running and reported (exit status 1); wait and pause again, or pass
`--force` to abandon the turn. Pause also reports any process from a stopped
window that outlived it, and a host running outside crc, which it marks
paused but does not stop.

`crc resume` starts paused hosts the way restore does, so an rc host comes
back on its environment with every session it was serving. A folder that is
missing (unmounted) stays paused. `crc move` starts a paused host in its new
folder, and `crc new` refuses a paused one.

## Development channels

A channel is an MCP server that pushes messages into a conversation. Only a
terminal (`-i`) host can load one: `claude rc` rejects the
`--dangerously-load-development-channels` flag. For such a host,

    crc channel DIR server:NAME           crc starts it with that channel
    crc channel DIR server:NAME --accept  ...and answers the launch warning
    crc channel DIR --clear               no channels

`crc new -i DIR --channel server:NAME [--accept-channels]` sets the same at
the first start, and `crc new DIR --resume ID` continues an existing
conversation there as a terminal host, copying its transcript to DIR's
project key if it lives under another folder. It refuses a conversation that
is open in a running process. A conversation born on a `claude rc` host gets
a new claude.ai link the first time it is resumed this way.

`server:NAME` must be a server in DIR's `.mcp.json` (found by walking up from
DIR, so a parent folder's servers load too); `--mcp-config` servers
don't resolve (2.1.286). Pre-approve it with `"enabledMcpjsonServers":
["NAME"]` in DIR's `.claude/settings.local.json`, or the first start stops at
the new-MCP-server prompt. Every launch shows a full-screen warning. Without
`--accept`, crc leaves the host there (STUCK in `crc ls`) for you to answer in
`crc attach`. With it, crc answers only a warning that lists exactly the
stored channels, logs each acceptance, and forgets the consent when the list
changes. After each start crc checks that each `server:` channel's command is
running under the session, and `crc ls` and `crc doctor` report it. Changes
apply from the host's next start.

## Install

    make install                      # to ~/.local; no root
    systemctl --user daemon-reload
    systemctl --user enable claude-rc-keeper.service claude-rc-keeper-sync.timer
    crc doctor

The units run with the systemd user manager's PATH, which normally includes
`/usr/bin`. On NixOS it has only systemd itself, so give them one explicitly:

    nix-shell -p gnumake --run 'make install UNITPATH="$HOME/.local/bin:/run/wrappers/bin:/etc/profiles/per-user/$USER/bin:/run/current-system/sw/bin"'

`make install` checks that `python3`, `tmux` and `systemctl` are on it, and
`crc doctor` checks whatever the installed units use. Pass the same UNITPATH
on every reinstall.

To resume at boot before you log in, it also needs `loginctl enable-linger`.
Without it, restore runs when you log in.

## Use

    crc new [DIR] [--name N] [-- FLAGS]  start `claude rc` in DIR and keep it
    crc new -i [DIR] [--name N]          same, as a terminal `claude --remote-control`
    crc new DIR --resume ID [--channel CH] [--accept-channels]
                                         a terminal host continuing conversation ID
    crc adopt [DIR] [--pid N]            keep a session already running there
    crc ls [--json]                      state of each kept folder; --json adds
                                         session titles, status and pids
    crc sync [--revive]                  record sessions opened on kept hosts;
                                         --revive (the timer) also restarts
                                         hosts that stopped or lost the server
    crc channel DIR [CH...] [--accept]   development channels for a terminal host
    crc pause DIR... [--force]           stop kept hosts and keep them stopped;
    crc pause --under PATH               --under: every kept folder below PATH
    crc resume DIR... | --under PATH     start paused hosts again
    crc poke DIR... | --under PATH       re-queue sessions a live rc host has
                                         stopped serving; --dry-run to look
    crc attach                           look at the sessions (the crc tmux server)
    crc rm DIR                           stop keeping DIR; stops it and releases it
    crc release [DIR]                    just the stale-registration fix
    crc move OLD NEW [--trust]           carry a kept session to a moved folder
    crc stale                            list stale registrations in every folder
    crc restore [--dry-run]              what the unit runs at start; --dry-run
                                         shows what it would start, re-queue and
                                         release, and changes nothing
    crc doctor                           check what restore depends on, including
                                         PATH in a clean login shell

There is one kept session per folder, because the server allows only one rc
host per folder. `adopt` prefers an rc host and otherwise takes a terminal
session with remote control on. An interactive session is never relaunched
while its conversation is already open somewhere else.

Claude Code has to be told once per folder that you trust it. Depending on
the version, `claude rc` either asks and waits, or exits with "Workspace not
trusted". Either way, `crc new`, `crc move` and `crc restore` fail right away
in an untrusted folder, and `crc ls`, `crc doctor` and the restore output give
the fix: run `claude` there and choose "Yes, I trust this folder". Pass
`--trust` to `crc new` or `crc move` to have crc answer yes for you; when
`claude rc` will not ask, crc answers the prompt of an interactive `claude`
first. `crc restore` never answers it.

A host waiting at any question never registers, so nobody can reach it.
`crc ls` shows such a window as STUCK, and `crc restore` replaces it.

FLAGS after `--` are passed to `claude rc` (or `claude`, with `-i`) and reused
on every resume. crc adds `--spawn=same-dir` unless FLAGS choose a spawn mode,
because in a git repo `claude rc` otherwise stops to ask. Don't pass
`--spawn=session`: it makes the host single-session, so the other sessions
can't be re-queued onto it.

Sessions you start from claude.ai/code on a kept host are picked up by the
next sync, or at the latest by the sync restore runs first.

## Safety

The only destructive step is deleting a server registration. Restore does it
only when **all** of these hold (`crc restore --dry-run` shows whether it
would):

- the folder's bridge pointer names it,
- the process that registered it is gone, checked by pid *and* start time, so a
  reused pid doesn't count as alive,
- it is not the environment the kept session lives on. Resuming reuses that
  one, so deleting it would delete what we came to resume.

A different `claude rc` that is still running in the folder is reported as
blocked and never touched. `crc release` refuses a live host, and refuses the
kept session's own environment without `--force`.

## Fragility

`lib/bridge.py` depends on undocumented Claude Code internals: the
`~/.claude` layout (including the per-process `sessions/` registry), the
bridge-pointer schema and the client's choice to reuse its environment, the
OAuth credentials file, and the environments API (deregister and
`bridge/reconnect`) with its `anthropic-beta` header, and the session list
(`/v1/sessions`, with its own beta header). They are all in that
one file. If an update changes them, the likely failure is a host that comes
back with only its first session, or on a new environment, which `crc
restore` logs. An
expired login token makes release fail until `claude` has been run once to
refresh it. `crc` never refreshes the token itself, because doing so rotates
the refresh token behind the client's back.

## Tests

    make test

These cover pure logic only: no tmux, no network, no real sessions.
