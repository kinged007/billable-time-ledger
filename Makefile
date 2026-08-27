.PHONY: install venv link-bin daemon bashrc-hook uninstall

VENV      := .venv
BIN       := $(VENV)/bin
LOCAL_BIN := $(HOME)/.local/bin
BASHRC    := $(HOME)/.bashrc
HOOK_SRC  := scripts/btl-autostart.sh
BEGIN     := \# >>> btl autostart >>>
END       := \# <<< btl autostart <<<

install: venv link-bin daemon
	@echo ""
	@echo "btl installed."
	@echo ""
	@echo "Next steps:"
	@echo "  btl watch add <name> <path>          # start tracking a project folder"
	@echo "  btl watch list                       # see configured watches"
	@echo "  btl status                           # daemon health"
	@echo "  btl report --day $$(date +%F)             # today's report"
	@echo "  btl report --month $$(date +%Y-%m)         # this month's report"
	@echo "  btl export --since <date> --until <date> --format md"

venv:
	@[ -x "$(BIN)/python" ] || uv venv $(VENV)
	uv pip install --python $(BIN)/python -e .

link-bin: venv
	@mkdir -p $(LOCAL_BIN)
	ln -sf "$(abspath $(BIN)/btl)" "$(LOCAL_BIN)/btl"
	ln -sf "$(abspath $(BIN)/btl-daemon)" "$(LOCAL_BIN)/btl-daemon"
	@case ":$$PATH:" in \
		*":$(LOCAL_BIN):"*) ;; \
		*) echo "warning: $(LOCAL_BIN) is not on PATH — add it to your shell rc" ;; \
	esac
	@if [ -f "$(LOCAL_BIN)/btl-root-start" ]; then \
		sed -i "s|^BTL_BIN=.*|BTL_BIN=$(abspath $(BIN))/btl-daemon|" "$(LOCAL_BIN)/btl-root-start"; \
		echo "repointed $(LOCAL_BIN)/btl-root-start -> $(abspath $(BIN))/btl-daemon"; \
	fi

# Prefer a real init-managed autostart when one is available (systemd --user
# on Linux, launchd on macOS). Only fall back to a shell-hook if there's no
# startup option — this container has no systemd, so that's the common case.
daemon: link-bin
	@if [ -n "$$XDG_RUNTIME_DIR" ] && command -v systemctl >/dev/null 2>&1 && systemctl --user status >/dev/null 2>&1; then \
		echo "systemd user session detected — installing native autostart"; \
		"$(LOCAL_BIN)/btl" autostart on; \
	elif [ "$$(uname)" = "Darwin" ]; then \
		echo "macOS detected — installing launchd autostart"; \
		"$(LOCAL_BIN)/btl" autostart on; \
	else \
		echo "no startup option available (no systemd user session) — using shell-hook autostart"; \
		$(MAKE) bashrc-hook; \
	fi
	@"$(LOCAL_BIN)/btl" start || true

bashrc-hook:
	@touch "$(BASHRC)"; \
	awk -v b="$(BEGIN)" -v e="$(END)" '$$0==b{skip=1} $$0==e{skip=0; next} !skip' "$(BASHRC)" > "$(BASHRC).tmp"; \
	mv "$(BASHRC).tmp" "$(BASHRC)"; \
	{ echo ""; echo "$(BEGIN)"; cat $(HOOK_SRC); echo "$(END)"; } >> "$(BASHRC)"; \
	echo "autostart hook installed in $(BASHRC) — new terminals will show daemon status"

uninstall:
	@awk -v b="$(BEGIN)" -v e="$(END)" '$$0==b{skip=1} $$0==e{skip=0; next} !skip' "$(BASHRC)" > "$(BASHRC).tmp" && mv "$(BASHRC).tmp" "$(BASHRC)"
	rm -f "$(LOCAL_BIN)/btl" "$(LOCAL_BIN)/btl-daemon"
	rm -rf $(VENV)
	@echo "btl uninstalled (config in ~/.config/btl left untouched)"
