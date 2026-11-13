# bd-tui

**Summary.** bd-tui is a terminal board for the [beads](https://github.com/gastownhall/beads)
issue tracker (`bd`). It opens on the beads that Claude is working on and the beads that need
you, grouped by area and then by epic, and it runs in a tmux pane beside `claude agents`, so
that one window shows both your beads and your Claude Code sessions. It is a working personal
tool, and it assumes the label and note conventions in
[beads.md](https://github.com/bdhammel/dotfiles/blob/master/claude/rules/beads.md).

## What you can do with it

- See what is in progress and what needs you. See [Quick start](#quick-start).
- Open a bead's full text, start a Claude session on it, or attach to its session. See [Keys](#keys).
- Check the board against a synthetic export or your own. See [Test it](#test-it).

## What is in this directory

| Path | What it is |
|---|---|
| `bd-tui` | The board, a Textual app that runs as a `uv` script |
| `bead_model.py` | A standard-library module that derives each bead's fields from `bd export` |
| `test_bd_tui.py` | Headless checks on a synthetic export, and on your own export with `--live` |

`bead_model.py` derives the fields that the board shows, such as whether a bead needs you, its
warning flags and its If skipped lines. A browser board, bd-board, keeps an identical copy, so
a change to the module goes into both copies.

## Before you begin

- **Tools.** You need `uv` and `bd`. The `s` and `a` keys and `--window` also need `tmux` and
  the `claude` CLI, and Enter needs `nvim`.
- **Database.** bd-tui uses `BEADS_DIR` when it is set. Otherwise it uses the database that
  `bd` finds, and failing that the `BEADS_DIR` in the `env` block of `~/.claude/settings.json`.
- **Python.** You need no venv, because `uv` installs Python 3.12 or later and Textual 8.2.8
  from the script header on the first run.

## Quick start

1. Clone the repo and link the script onto your PATH.

   ```bash
   git clone git@github.com:bdhammel/bd-tui.git ~/dev/bd-tui
   ln -s ~/dev/bd-tui/bd-tui ~/.local/bin/bd-tui
   ```

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

## Keys

No key uses Ctrl-h, Ctrl-j, Ctrl-k or Ctrl-l, because vim-tmux-navigator moves between panes
with them.

| Key | What it does |
|---|---|
| `j` `k` | Move up and down the tree |
| `h` `l` | Fold and unfold an area or epic |
| `o` `c` `v` | Add every open bead, the closed beads or the unreviewed results to the view |
| `p` | Show only `path:on` beads |
| `g` | Show the epic's open beads in dependency order in the detail pane |
| `/` | Search ids and titles, where Enter keeps the search and Esc clears it |
| Enter | Open the bead's full `bd show` text, read-only, in nvim |
| `s` | Start a background Claude session on the bead, after you type what it should do |
| `a` | Attach to the bead's newest session in a new tmux window |
| `r` `q` | Reload, and quit |

## What the board reads from a bead

The board reads these conventions. Its detail pane also marks a bead with ⚠ when the bead
lacks a `## Summary`, a `## If skipped` section, acceptance, or a `ben:`, `kind:` or `path:`
label.

| Convention | What the board does with it |
|---|---|
| An `area:<name>` label on the epic | Groups the tree by area |
| A `needs:ben` label, a `Needs from Ben:` list in the newest dated note, or an open `kind:decision` bead | Marks the bead `?` and lists it |
| A `## If skipped` section with `Goal date:`, `Goal result:` and `Worst case:` lines | Opens the detail pane |
| A `path:on` or `path:off` label | Sets what `p` keeps |
| A `mode:ultrathink` or `mode:ultracode` label | Starts the bead's session with that keyword |
| Notes that start with a `YYYY-MM-DD:` date | Shows the newest as the Latest note |
| Acceptance lines, with `[x]` on a finished one | Shows the Done when checklist |
| A Claude session named `<bead id> <title>` | Marks the bead `●`, checked every 30 s |

## Test it

Run the checks from the repo directory. Expected: `all checks passed` and exit code 0.

```bash
cd ~/dev/bd-tui
./test_bd_tui.py          # 25 checks on a synthetic export
./test_bd_tui.py --live   # also checks the tree and renders every bead in your own export
```

## What this does not do

- **Mark a result reviewed.** The board never adds the `reviewed` label. Add it with
  `bd label add <id> reviewed`.
- **Start sessions in another directory.** `s`, `a` and `--window` start in `~/dev`. Change
  `SESSION_CWD` in `bead_model.py` to use another directory.
- **Use another person's name.** The `needs:ben` label and the `Needs from Ben:` heading are
  fixed strings. Change `NEEDS` and `ASK_HEADER` in `bead_model.py` to use your own.
