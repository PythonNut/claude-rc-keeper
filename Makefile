# Everything here runs as you: no root, nothing outside $(PREFIX) and the
# systemd user unit directory. The default installs to ~/.local; a package
# build passes PREFIX=/usr DESTDIR=... UNITDIR=/usr/lib/systemd/user.
PREFIX  ?= $(HOME)/.local
DESTDIR ?=
BINDIR  := $(PREFIX)/bin
LIBDIR  ?= $(PREFIX)/lib/claude-rc-keeper
# $(XDG_DATA_HOME)/systemd/user is on systemd's user unit search path, so the
# ~/.local default needs no extra configuration.
UNITDIR ?= $(PREFIX)/share/systemd/user
# The units run with the systemd user manager's PATH, which on most systems
# has /usr/bin and so python3 and tmux. Where it doesn't -- on NixOS it holds
# only systemd itself -- pass the PATH they should use instead, e.g.
#   make install UNITPATH="$$HOME/.local/bin:/run/wrappers/bin:/etc/profiles/per-user/$$USER/bin:/run/current-system/sw/bin"
UNITPATH ?=
# With UNITPATH, fill it in (% escaped for systemd); without, drop the line.
ifeq ($(strip $(UNITPATH)),)
PATHSED := -e '/@PATH@/d'
else
PATHSED := -e 's|@PATH@|$(subst %,%%,$(UNITPATH))|g'
endif
UNIT    := claude-rc-keeper.service
SYNC    := claude-rc-keeper-sync
MODULES := bridge.py keeper.py tmuxctl.py

.PHONY: all test install uninstall

all:
	@echo "targets: test, install, uninstall  (PREFIX=$(PREFIX))"

test:
	python3 -m unittest discover -s tests -p "test_*.py"

install:
	@if [ -n "$(UNITPATH)" ]; then \
	    for t in python3 tmux systemctl; do \
	        PATH="$(UNITPATH)" command -v $$t >/dev/null || \
	            { echo "UNITPATH has no $$t: $(UNITPATH)" >&2; exit 1; }; \
	    done; \
	fi
	install -d -m 0755 $(DESTDIR)$(BINDIR) $(DESTDIR)$(LIBDIR) $(DESTDIR)$(UNITDIR)
	install -m 0644 $(addprefix lib/,$(MODULES)) $(DESTDIR)$(LIBDIR)/
	install -m 0755 bin/crc $(DESTDIR)$(BINDIR)/crc
	@# bin/crc finds its modules relative to itself in the repo; installed,
	@# they are in $(LIBDIR), so pin that path.
	sed -i 's|^LIBDIR = .*|LIBDIR = "$(LIBDIR)"|' $(DESTDIR)$(BINDIR)/crc
	@# tmux by absolute path: a user unit runs with systemd's minimal PATH.
	@tmux_path=$$(command -v tmux || echo /usr/bin/tmux); \
	sed -e 's|@BINDIR@|$(BINDIR)|g' -e "s|@TMUX@|$$tmux_path|g" $(PATHSED) \
	    systemd/$(UNIT).in > $(DESTDIR)$(UNITDIR)/$(UNIT)
	sed -e 's|@BINDIR@|$(BINDIR)|g' $(PATHSED) systemd/$(SYNC).service.in \
	    > $(DESTDIR)$(UNITDIR)/$(SYNC).service
	install -m 0644 systemd/$(SYNC).timer $(DESTDIR)$(UNITDIR)/$(SYNC).timer
	@if [ -z "$(DESTDIR)" ]; then \
	    echo; echo "installed. next:"; \
	    echo "  systemctl --user daemon-reload"; \
	    echo "  systemctl --user enable $(UNIT) $(SYNC).timer"; \
	    echo "  crc doctor"; \
	fi

# Does not stop the unit: stopping it ends every kept session. Disable it
# yourself first if that is what you want.
uninstall:
	@if [ -n "$(UNITPATH)" ]; then \
	    for t in python3 tmux systemctl; do \
	        PATH="$(UNITPATH)" command -v $$t >/dev/null || \
	            { echo "UNITPATH has no $$t: $(UNITPATH)" >&2; exit 1; }; \
	    done; \
	fi
	rm -f $(BINDIR)/crc $(UNITDIR)/$(UNIT) $(UNITDIR)/$(SYNC).service $(UNITDIR)/$(SYNC).timer
	rm -rf $(LIBDIR)
	@echo "removed. the session list in ~/.local/state/claude-rc-keeper was left in place."
