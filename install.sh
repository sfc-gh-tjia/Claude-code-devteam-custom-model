#!/usr/bin/env bash
# Install the DevTeam harness into a repo without overwriting anything that already exists.
#   ./install.sh /path/to/repo                      # dry run: show what would be copied or skipped
#   ./install.sh /path/to/repo --apply
#   ./install.sh /path/to/repo --apply --with-gateway   # also give the repo its own gateway copy
#
# By default the repo uses the SHARED gateway that lives in this template clone (one gateway, one
# set of keys and models, for every project and for claude-custom). The repo gets a .devteam-home
# file naming this clone, which `make gateway|probe|profile|routes|tokens` follow.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DST=""; APPLY=0; WITH_GW=0
for a in "$@"; do
  case "$a" in
    --apply) APPLY=1 ;;
    --with-gateway) WITH_GW=1 ;;
    -*) echo "error: unknown option $a" >&2; exit 2 ;;
    *) DST="$a" ;;
  esac
done
[ -n "$DST" ] || { echo "usage: install.sh /path/to/repo [--apply] [--with-gateway]" >&2; exit 2; }
[ -d "$DST/.git" ] || { echo "error: $DST is not a git repository root" >&2; exit 1; }
DST="$(cd "$DST" && pwd)"
[ "$DST" != "$SRC" ] || { echo "error: that is the template itself" >&2; exit 1; }

if [ -d "$DST/gateway" ] && [ "$WITH_GW" = 0 ]; then
  cat >&2 <<WARN
warning: $DST already has its own gateway/ (from an older install). Nothing will be removed.
  Only one gateway can serve port 4000; whichever was started last is the one every project uses.
  To move to the shared gateway at $SRC/gateway: copy any custom tiers/profiles there, move the keys
  into $SRC/gateway/.env, then delete $DST/gateway. Its Makefile targets keep using the local copy
  until you do.
WARN
fi

copied=0; skipped=0
while IFS= read -r f; do
  rel="${f#"$SRC"/}"
  case "$rel" in
    install.sh|README.md|RESEARCH.md|harness/tests/*|.git/*|.loop/*|standalone/*) continue ;;
    gateway/tests/*|gateway/.env|gateway/.litellm.*|gateway/service/*) continue ;;
    gateway/*) [ "$WITH_GW" = 1 ] || continue ;;
  esac
  if [ -e "$DST/$rel" ]; then
    echo "skip (exists): $rel"; skipped=$((skipped+1))
  else
    echo "copy: $rel"; copied=$((copied+1))
    if [ "$APPLY" = 1 ]; then mkdir -p "$(dirname "$DST/$rel")"; cp -p "$f" "$DST/$rel"; fi
  fi
done < <(find "$SRC" -type f ! -name '.DS_Store' ! -path '*/__pycache__/*' ! -path '*/.pytest_cache/*' | sort)

if [ "$APPLY" = 1 ]; then
  mkdir -p "$DST/harness/tests" && cp -pn "$SRC"/harness/tests/* "$DST/harness/tests/" 2>/dev/null || true
  [ "$WITH_GW" = 1 ] || [ -e "$DST/.devteam-home" ] || echo "$SRC" > "$DST/.devteam-home"
  touch "$DST/.gitignore"
  for line in ".loop/" ".worktrees/" "__pycache__/" ".pytest_cache/" ".devteam-home" "gateway/.env" "gateway/.litellm.*.yaml"; do
    grep -qxF "$line" "$DST/.gitignore" || echo "$line" >> "$DST/.gitignore"
  done
  chmod +x "$DST/harness/"*.py
  [ "$WITH_GW" = 0 ] || chmod +x "$DST/gateway/start.sh" "$DST/gateway/probe.py" "$DST/gateway/render.py"
  echo; echo "copied $copied, skipped $skipped. Next:"
else
  echo; echo "dry run: would copy $copied, skip $skipped. Re-run with --apply. Then:"
fi
if [ "$WITH_GW" = 1 ]; then GWNOTE="this repo's own gateway/ (cp gateway/.env.example gateway/.env, add keys)"
else GWNOTE="the shared gateway in $SRC (keys in $SRC/gateway/.env)"; fi
cat <<NEXT
  0. Inference goes through $GWNOTE.
     make gateway && make doctor        # doctor proves the gateway, token and this repo's profile
  1. Edit harness/project.toml (setup, doctor, gates, lanes) and the CLAUDE.md placeholders.
     If CLAUDE.md or .claude/settings.json were skipped, merge them by hand.
     This repo's profile: x-devteam-profile in .claude/settings.json (default baseline).
  2. make harness-test
  3. Write work/spec.md and tasks (or ask Claude: "use spec-author to plan <idea>").
  4. In Claude Code: "run phase 01"  (the lead launches the build-phase workflow)
  5. After the run: make tokens (this repo) · make routes (the gateway, all clients)
NEXT
