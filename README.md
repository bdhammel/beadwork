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
- See what is in progress and what needs you, mark results reviewed, and change a bead's
  status. See [Keys](#keys).
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
| Read a saved `bd export` instead of running `bd` | `bd-tui --export FILE` |
| Reread the beads every 120 s instead of every 60 s, or only on `r` with `0` | `bd-tui --refresh 120` |
| Write a bead's `## If skipped` section and priority, or print it without writing with `--dry-run` | `~/dev/beadwork/if_skipped.py <id> --today <YYYY-MM-DD> --priority <0-3> --why "<reason>"` |

## Keys

No key uses Ctrl-h, Ctrl-j, Ctrl-k or Ctrl-l, because vim-tmux-navigator moves between panes
with them.

| Key | What it does |
|---|---|
| `j` `k`, Ctrl-n Ctrl-p | Move down and up the tree |
| `h` `l` | Fold and unfold an area or epic |
| `o` `d` `c` | Show the open, the deferred or the closed beads, where the board opens on the open beads |
| `n` | Narrow the list to beads in progress or needing you, which on the closed list are the results that still ask you a question |
| `v` | Narrow the closed list to the results you have not marked reviewed |
| `p` | Narrow the list to P0 beads, the ones the epic's MVP needs |
| Shift-`L` | Open a menu of the labels on the listed beads, each with the number of beads that carry it, and narrow the list to the label you pick. Typing narrows the menu, the arrows move, Enter picks and Esc cancels |
| `/` | Narrow the list to beads whose id or title matches, where Enter keeps the search and Esc clears it |
| `g` | Show the epic's open beads in dependency order in the detail pane |
| Enter | Open the bead's full `bd show` text, read-only, in nvim |
| `m` | Mark a closed bead's result reviewed, and press it again to undo |
| Shift-`S` | Open the status menu, which moves the bead to open, in progress, deferred or closed |
| `s` | Start a background Claude session on the bead at the reasoning effort its `mode:` label sets. Type what it should do, or press Enter on an empty line to start the bead: the session claims it and works it to its acceptance check as its run labels say. On a `ben:pair` bead the empty line starts a session that asks its opening questions and waits for you as needs input in the agents view, where `a` attaches to it. An empty line starts nothing on an epic, a closed or deferred bead or a decision bead, and the prompt warns when a session named after the bead is already running |
| `a` | Attach to the bead's newest session in a new tmux window |
| `r` `q` | Reread the beads now, and quit |

The keys `n`, `v`, `p`, Shift-`L` and `/` only ever narrow the list, and pressing one again
removes its filter. The title shows each step with the number of beads left, such as
`Open 127 › now 42 › P0 18 › mode:ultracode 3`, so you can read the list as a funnel. The
label menu counts each label over the list as it stands, so the number beside a label is the
number of beads the tree lists after you pick it. A label or a search opens every area and epic,
so each bead it finds shows. The key strip shows how many
beads the tree would list after you press each list or filter key, and it highlights the keys
that are on. The title's need-you count covers every list, so it also counts a closed result
that still asks you a question.

Only you press `m`, because the rules reserve the `reviewed` label for the person who reviews
the result. Closing a bead from the status menu adds the label too, because a result that you
close the bead with is one you wrote, and reopening the bead removes the label, so that its
next result waits for your review again.

The status menu offers the four statuses the board reads, with the bead's current status
marked. You pick one by its letter, or with the arrows or `j` `k` and Enter, and Esc cancels.

| Pick | Key | What happens |
|---|---|---|
| open | `o` | Reopens a closed bead, undefers a deferred one, or releases the claim on one in progress |
| in progress | `w` | Claims the bead, after reopening or undeferring it first |
| deferred | `d` | Asks for an optional date, such as `2026-12-01`, `+2w` or `next monday`, and an optional reason, which is kept as a dated note |
| closed | `c` | Asks for the result in one line, which is required and counts as reviewed |

The menu greys out the current status, and it greys out closed while the bead has open
children, because bd closes a bead only after its children. It leaves out bd's other statuses:
a stored `blocked` drops the bead from both `bd ready` and `bd blocked` until someone changes
it by hand, so the board reads blocked from the bead's dependencies instead, and nothing here
uses `pinned` or `hooked`. When bd refuses a change, such as a date it cannot read, picking the
same status for the same bead brings back what you typed. After a bead leaves the list, the
cursor moves to the bead after it.

The board rereads the beads every 60 s, so a bead that another session claims or closes moves
without a key press, and the title bar shows the time of the last read. A reread that finds a
change rebuilds the tree but keeps your cursor, your folds and your place in the detail pane.
A reread that finds no change only updates the time.

A reread also marks each bead or epic whose row changed with `Δ` before its id, and the title
counts the marked beads across every list, such as `Δ 3 changed`. The count leaves epics out,
like the title's other counts. The detail pane names what
changed and when, such as `Δ changed 16:42: open → in progress, note`. A mark clears when the
cursor leaves a row that the cursor reached after the change, so the detail pane keeps naming
the change while you read. A change made under a cursor that nobody moved stays marked, because
nobody saw the change. The first read marks nothing, and neither does a change you make with
`m` or Shift-`S`. The marks ignore timestamps, counts and the heartbeat of a claim, because bd
moves those without an edit anyone reads. A bead that another session closed leaves the open
list, so its mark shows on the closed list, `c`.

## What the board reads from a bead

The board reads these conventions from `rules/beads.md`. Its detail pane also marks a bead with
⚠ when the bead lacks a `## Summary`, a `## If skipped` section, acceptance, a `Priority:`
line, or a `ben:` or `kind:` label.

| Convention | What the board does with it |
|---|---|
| An `area:<name>` label on the epic | Groups the tree by area |
| A `needs:ben` label, a `Needs from Ben:` list in the newest dated note, or an open `kind:decision` bead | Marks the bead `?` and lists it |
| The `ben:`, `mode:` and `kind:` labels | Shows them on one line under the bead's title, at the top of the detail pane, with `xhigh effort` when the bead has no `mode:` label and `one-shot` when the bead is one |
| A `## Summary` section | Shows it in full under the run labels of a bead, and above the goal of an epic, with hard-wrapped lines joined so the pane wraps it to its width. A bead without one shows its `## Objective`, `## Goal` or first paragraph and says which |
| A `## If skipped` section with `Goal date:`, `Goal result:`, `Worst case:` and `Priority:` lines | Shows the section after the summary and any question for Ben |
| A priority from P0 to P3, which the `Priority:` line in `## If skipped` justifies | Sets what `p` keeps, and is flagged when it disagrees with the goal lines on P0 |
| The P0 beads under an epic, the ones its MVP needs | Fill the epic row's bar, `█` done, `▒` in progress and `░` the rest, with the count closed of total. The epic's detail lists the P0 beads still open, in dependency order |
| Every bead under an epic that is not deferred | Gives the epic row's second count, closed of total after a `·`, such as `9/9 · 28/29`, so a P1-or-lower bead still open shows after a full P0 bar |
| A `mode:` label | Sets how `s` starts the bead's session: `mode:low` at `claude --effort low`, `mode:ultrathink` at `max` with the `ultrathink` keyword, `mode:ultracode` at `xhigh` with the `ultracode` keyword, and a bead with no mode at `xhigh` |
| Notes that start with a `YYYY-MM-DD:` date | Shows the newest as the Latest note |
| Acceptance lines, with `[x]` on a finished one | Shows the Done when checklist |
| A `Follow-ups:` list in the close reason or notes | Lists each follow-up, with ⚑ on one that names no bead |
| A closed bead without the `reviewed` label | Marks it `◇` and lists it under `v` until `m` adds the label |
| A Claude session named `<bead id> <title>` | Marks the bead `●`, checked every 30 s |

## Test it

Run the checks from the repo directory. Expected: `all checks passed` and exit code 0.

```bash
cd ~/dev/beadwork
./test_bd_tui.py          # 81 checks on a synthetic export, with a fake bd for the status menu
./test_bd_tui.py --live   # also checks the tree and renders every bead in your own export, read-only
./test_bd_tui.py --bd     # also moves a bead through every status change in a throwaway bd database
```

## What this does not do

- **Start sessions in another directory.** `s`, `a` and `--window` start in `~/dev`. Change
  `SESSION_CWD` in `bead_model.py` to use another directory.
- **Use another person's name.** The `needs:ben` label and the `Needs from Ben:` heading are
  fixed strings. Change `NEEDS` and `ASK_HEADER` in `bead_model.py` to use your own.
