#!/usr/bin/env bash
# Link the bead rules into ~/.claude/rules and the board onto PATH, from wherever this repo is cloned.
# Run it again after moving the clone. A real file in a link's place is kept as <name>.bak.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
links=(
    "rules/beads.md:.claude/rules/beads.md"
    "bd-tui:.local/bin/bd-tui"
)

for item in "${links[@]}"; do
    source="$REPO/${item%%:*}"
    target="$HOME/${item#*:}"
    if [[ -L "$target" && "$(readlink "$target")" == "$source" ]]; then
        continue
    fi
    if [[ -e "$target" && ! -L "$target" ]]; then
        mv "$target" "$target.bak"
        echo "Backed up: $target"
    fi
    [[ -L "$target" ]] && rm "$target"
    mkdir -p "$(dirname "$target")"
    ln -s "$source" "$target"
    echo "Linked: $target -> $source"
done
