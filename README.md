# claude-rc-keeper

Keeps Claude Code remote-control sessions (`claude rc`) running across reboots.

Kept sessions run in windows of a private tmux server (`tmux -L crc`) owned by a
systemd user unit. When the unit starts, it resumes each one. It handles two
kinds:

- **rc**: a `claude rc` host, resumed with `claude rc --session-id`.
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

## Install

    make install                      # to ~/.local; no root
    systemctl --user daemon-reload
    systemctl --user enable claude-rc-keeper.service
    crc doctor

To resume at boot before you log in, it also needs `loginctl enable-linger`.
Without it, restore runs when you log in.

## Use

    crc new [DIR] [--name N] [-- FLAGS]  start `claude rc` in DIR and keep it
    crc new -i [DIR] [--name N]          same, as a terminal `claude --remote-control`
    crc adopt [DIR] [--pid N]            keep a session already running there
    crc ls                               state of each kept folder
    crc attach                           look at the sessions (tmux -L crc)
    crc rm DIR                           stop keeping DIR; stops it and releases it
    crc release [DIR]                    just the stale-registration fix
    crc restore                          what the unit runs at start

There is one kept session per folder, because the server allows only one rc
host per folder. `adopt` prefers an rc host and otherwise takes a terminal
session with remote control on. An interactive session is never relaunched
while its conversation is already open somewhere else.

FLAGS after `--` are passed to `claude rc` (or `claude`, with `-i`) and reused
on every resume. Spawn flags (`--spawn`, `--capacity`) cannot be combined with
`--session-id`, so don't pass them here.

Sessions you start from claude.ai/code on a kept host are not tracked one by
one. The host's own session is.

## Safety

The only destructive step is deleting a server registration. Restore does it
only when **all** of these hold:

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
`~/.claude` layout, the bridge-pointer schema, the OAuth credentials file, and
the environments API with its `anthropic-beta` header. They are all in that
one file, so an update that changes them breaks releasing, not resuming. An
expired login token makes release fail until `claude` has been run once to
refresh it. `crc` never refreshes the token itself, because doing so rotates
the refresh token behind the client's back.

## Tests

    make test

These cover pure logic only: no tmux, no network, no real sessions.
