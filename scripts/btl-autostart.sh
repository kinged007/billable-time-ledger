if command -v btl >/dev/null 2>&1; then
  if ! btl status >/dev/null 2>&1; then
    if command -v btl-root-start >/dev/null 2>&1 && sudo -n "$HOME/.local/bin/btl-root-start" >/dev/null 2>&1; then
      sleep 2
    fi
    if ! btl status >/dev/null 2>&1; then
      if ! btl start >/dev/null 2>&1; then
        echo "btl: autostart failed — run 'btl start' to see the error" >&2
      fi
    fi
  fi
  if btl status >/dev/null 2>&1; then
    printf '\033[1;32m\xe2\x9c\x93 btl daemon running\033[0m — %s\n' "$(btl status 2>/dev/null | sed -n '1s/^btl daemon: running //p')"
  else
    printf '\033[1;31m\xe2\x9c\x97 btl daemon NOT running\033[0m — billable time is not being recorded. Run: btl start\n'
  fi
fi
