# claude-rc-keeper

Keeps Claude Code remote-control sessions (`claude rc`) running across reboots.

Kept sessions run in windows of a private tmux server (`tmux -L crc`) owned by a
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

## Install

    make install                      # to ~/.local; no root
    systemctl --user daemon-reload
    systemctl --user enable claude-rc-keeper.service claude-rc-keeper-sync.timer
    crc doctor

To resume at boot before you log in, it also needs `loginctl enable-linger`.
Without it, restore runs when you log in.

## Use

    crc new [DIR] [--name N] [-- FLAGS]  start `claude rc` in DIR and keep it
    crc new -i [DIR] [--name N]          same, as a terminal `claude --remote-control`
    crc adopt [DIR] [--pid N]            keep a session already running there
    crc ls [--json]                      state of each kept folder; --json adds
                                         session titles, status and pids
    crc sync                             record sessions opened on kept hosts
    crc attach                           look at the sessions (tmux -L crc)
    crc rm DIR                           stop keeping DIR; stops it and releases it
    crc release [DIR]                    just the stale-registration fix
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

FLAGS after `--` are passed to `claude rc` (or `claude`, with `-i`) and reused
on every resume. Don't pass `--spawn=session`: it makes the host
single-session, so the other sessions can't be re-queued onto it.

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
