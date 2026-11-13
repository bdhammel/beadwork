# beadwork

**Summary.** beadwork holds the rules for writing beads, issues in the
[beads](https://github.com/gastownhall/beads) tracker (`bd`), together with the terminal board
that reads them. The rules in `rules/beads.md` fix the format of a bead: its labels, its
`## Summary` and `## If skipped` sections, its dated notes and its close reason. `bd-tui`
parses that format to show the beads that Claude is working on and the beads that need you,
in a tmux pane beside `claude agents`. Both live in one repo, so that a change to the format and
the board that reads it is one commit. It is a working personal tool.

## What you can do with it

- Write beads that an agent can act on and the board can read. See `rules/beads.md`.
- Write or re-judge a bead's `## If skipped` section and priority. See [Commands](#commands).
- See what is in progress and what needs you, and mark results reviewed. See [Keys](#keys).
- Check the board against a synthetic export or your own. See [Test it](#test-it).

## What is in this directory

| Path | What it is |
|---|---|
| `rules/beads.md` | The rules for writing a bead, loaded into every Claude Code session from `~/.claude/rules` |
| `bd-tui` | The board, a Textual app that runs as a `uv` script |
| `bead_model.py` | A standard-library module that derives each bead's fields from `bd export` and makes every `bd` and `claude` call |
| `if_skipped.py` | Writes a bead's `## If skipped` section and sets the priority that its lines justify |
| `test_bd_tui.py` | Headless checks on a synthetic export, and on your own export with `--live` |
| `install.sh` | Links `rules/beads.md` into `~/.claude/rules` and `bd-tui` into `~/.local/bin` |

## Before you begin

- **Tools.** You need `uv` and `bd`. The `s` and `a` keys and `--window` also need `tmux` and
  the `claude` CLI, and Enter needs `nvim`.
- **Database.** bd-tui uses `BEADS_DIR` when it is set. Otherwise it uses the database that
  `bd` finds, and failing that the `BEADS_DIR` in the `env` block of `~/.claude/settings.json`.
- **Python.** You need no venv, because `uv` installs Python 3.12 or later and Textual 8.2.8
  from the script header on the first run.

## Quick start

1. Clone the repo and link the rules and the board.

   ```bash
   git clone git@github.com:bdhammel/beadwork.git ~/dev/beadwork
   ~/dev/beadwork/install.sh
   ```

   Expected: one `Linked:` line for `~/.claude/rules/beads.md` and one for `~/.local/bin/bd-tui`.

2. Inside tmux, open the board beside `claude agents`.

   ```bash
   bd-tui --window
   ```

   Expected: a new tmux window named `beads`, with `claude agents` in the left 60% and the
   board in the right 40%.

## Commands

| Do this | Run |
|---|---|
| Open the board in this terminal | `bd-tui` |
| Open a tmux window with `claude agents` on the left and the board on the right | `bd-tui --window` |
| Read a saved `bd export` instead of running `bd`, which `r` rereads | `bd-tui --export FILE` |
| Write a bead's `## If skipped` section and priority, or print it without writing with `--dry-run` | `~/dev/beadwork/if_skipped.py <id> --today <YYYY-MM-DD> --priority <0-3> --why "<reason>"` |

## Keys

No key uses Ctrl-h, Ctrl-j, Ctrl-k or Ctrl-l, because vim-tmux-navigator moves between panes
with them.

| Key | What it does |
|---|---|
| `j` `k` | Move up and down the tree |
| `h` `l` | Fold and unfold an area or epic |
| `o` `c` `v` | Add every open bead, the closed beads or the unreviewed results to the view |
| `p` | Show only P0 beads, the ones the epic's MVP needs |
| `g` | Show the epic's open beads in dependency order in the detail pane |
| `/` | Search ids and titles, where Enter keeps the search and Esc clears it |
| Enter | Open the bead's full `bd show` text, read-only, in nvim |
| `m` | Mark a closed bead's result reviewed, and press it again to undo |
| `s` | Start a background Claude session on the bead, after you type what it should do |
| `a` | Attach to the bead's newest session in a new tmux window |
| `r` `q` | Reload, and quit |

Only you press `m`, because the rules reserve the `reviewed` label for the person who reviews
the result.

## What the board reads from a bead

The board reads these conventions from `rules/beads.md`. Its detail pane also marks a bead with
⚠ when the bead lacks a `## Summary`, a `## If skipped` section, acceptance, a `Priority:`
line, or a `ben:` or `kind:` label.

| Convention | What the board does with it |
|---|---|
| An `area:<name>` label on the epic | Groups the tree by area |
| A `needs:ben` label, a `Needs from Ben:` list in the newest dated note, or an open `kind:decision` bead | Marks the bead `?` and lists it |
| A `## If skipped` section with `Goal date:`, `Goal result:`, `Worst case:` and `Priority:` lines | Opens the detail pane |
| A priority from P0 to P3, which the `Priority:` line in `## If skipped` justifies | Sets what `p` keeps, and is flagged when it disagrees with the goal lines on P0 |
| A `mode:ultrathink` or `mode:ultracode` label | Starts the bead's session with that keyword |
| Notes that start with a `YYYY-MM-DD:` date | Shows the newest as the Latest note |
| Acceptance lines, with `[x]` on a finished one | Shows the Done when checklist |
| A `Follow-ups:` list in the close reason or notes | Lists each follow-up, with ⚑ on one that names no bead |
| A closed bead without the `reviewed` label | Marks it `◇` and lists it under `v` until `m` adds the label |
| A Claude session named `<bead id> <title>` | Marks the bead `●`, checked every 30 s |

## Test it

Run the checks from the repo directory. Expected: `all checks passed` and exit code 0.

```bash
cd ~/dev/beadwork
./test_bd_tui.py          # 30 checks on a synthetic export
./test_bd_tui.py --live   # also checks the tree and renders every bead in your own export
```

## What this does not do

- **Start sessions in another directory.** `s`, `a` and `--window` start in `~/dev`. Change
  `SESSION_CWD` in `bead_model.py` to use another directory.
- **Use another person's name.** The `needs:ben` label and the `Needs from Ben:` heading are
  fixed strings. Change `NEEDS` and `ASK_HEADER` in `bead_model.py` to use your own.
