#!/usr/bin/env bash
# Copy the DevTeam template into a repo without overwriting anything that already exists.
#   ./install.sh /path/to/repo            # dry run: show what would be copied or skipped
#   ./install.sh /path/to/repo --apply
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DST="${1:?usage: install.sh /path/to/repo [--apply]}"
APPLY="${2:-}"
[ -d "$DST/.git" ] || { echo "error: $DST is not a git repository root" >&2; exit 1; }
copied=0; skipped=0
while IFS= read -r f; do
  rel="${f#"$SRC"/}"
  case "$rel" in install.sh|README.md|RESEARCH.md|harness/tests/*|gateway/tests/*|gateway/.env|gateway/.litellm.*|.git/*|.loop/*) continue ;; esac
  if [ -e "$DST/$rel" ]; then
    echo "skip (exists): $rel"; skipped=$((skipped+1))
  else
    echo "copy: $rel"; copied=$((copied+1))
    if [ "$APPLY" = "--apply" ]; then mkdir -p "$(dirname "$DST/$rel")"; cp -p "$f" "$DST/$rel"; fi
  fi
done < <(find "$SRC" -type f ! -name '.DS_Store' ! -path '*/__pycache__/*' | sort)
if [ "$APPLY" = "--apply" ]; then
  mkdir -p "$DST/harness/tests" && cp -pn "$SRC"/harness/tests/* "$DST/harness/tests/" 2>/dev/null || true
  mkdir -p "$DST/gateway/tests" && cp -pn "$SRC"/gateway/tests/*.py "$DST/gateway/tests/" 2>/dev/null || true
  touch "$DST/.gitignore"
  for line in ".loop/" ".worktrees/" "__pycache__/" ".pytest_cache/" "gateway/.env" "gateway/.litellm.*.yaml"; do
    grep -qxF "$line" "$DST/.gitignore" || echo "$line" >> "$DST/.gitignore"
  done
  chmod +x "$DST/harness/"*.py "$DST/gateway/start.sh" "$DST/gateway/probe.py" "$DST/gateway/render.py"
  echo; echo "copied $copied, skipped $skipped. Next:"
else
  echo; echo "dry run: would copy $copied, skip $skipped. Re-run with --apply. Then:"
fi
cat <<'NEXT'
  0. cp gateway/.env.example gateway/.env and add the keys your tiers use (gateway/tiers.toml),
     pip install -r gateway/requirements.txt, then: make gateway && make probe
  1. Edit harness/project.toml (setup, doctor, gates, lanes) and the CLAUDE.md placeholders.
     If CLAUDE.md or .claude/settings.json were skipped, merge them by hand.
  2. make harness-test && make doctor
  3. Write work/spec.md and tasks (or ask Claude: "use spec-author to plan <idea>").
  4. In Claude Code: "run phase 01"  (the lead launches the build-phase workflow)
  5. After the run: make tokens
NEXT
