#!/usr/bin/env bash
# Put `claude-custom` on your PATH (a symlink into this template clone). Never touches ~/.claude.
#   standalone/install.sh                 # dry run: show what it would do
#   standalone/install.sh --apply         # link into ~/.local/bin
#   standalone/install.sh --apply DIR     # link into DIR
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
APPLY=0; [ "${1:-}" = "--apply" ] && { APPLY=1; shift; }
BIN="${1:-$HOME/.local/bin}"
SRC="$HERE/claude-custom"; DST="$BIN/claude-custom"
if [ -e "$DST" ] && [ "$(readlink "$DST" 2>/dev/null || true)" != "$SRC" ]; then
  echo "error: $DST exists and is not this template's launcher; remove it or pass another DIR" >&2; exit 1
fi
echo "link: $DST -> $SRC"
if [ "$APPLY" = 1 ]; then
  mkdir -p "$BIN"; chmod +x "$SRC"; ln -sfn "$SRC" "$DST"; echo "done."
else
  echo "dry run; re-run with --apply"
fi
case ":$PATH:" in *":$BIN:"*) ;; *) echo "note: $BIN is not on your PATH; add it, e.g. export PATH=\"$BIN:\$PATH\"" ;; esac
cat <<NEXT
Next:
  make -C "$(dirname "$HERE")" gateway      # once per boot (or install the macOS service: make service)
  claude-custom                             # in any repo; /model opus for hard problems
  claude-custom --selfcheck                 # prove it works with no Anthropic account
NEXT
