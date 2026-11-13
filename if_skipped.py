#!/usr/bin/env python3
"""Write a bead's `## If skipped` section and set the priority that its lines justify.

Run from any directory; needs `bd` on PATH and BEADS_DIR set. Example:

    if_skipped.py ent-aaa.4 --today 2026-11-12 --priority 0 \
        --why "the acceptance check cannot pass until the table exists" \
        --date "slips: ent-aaa.5 cannot run until the table exists" --result unchanged \
        --worst "the load test never runs"

Goal date and Goal result read against the epic's acceptance check, which states its MVP, so
the bead is P0 exactly when either line changes. A line that is not passed keeps its current
value, so `--priority` and `--why` alone re-judge a bead. The section goes directly after
`## Summary`, replacing an older one, and any `path:on` or `path:off` label is removed. The
bead is re-read immediately before the write, because other sessions edit beads at the same
time. `--dry-run` prints the section and the priority without writing.
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile

# The heading and its bullet lines only, so that a body with no later heading is never removed.
SECTION = re.compile(r"^## If skipped\n(?:[ \t]*\n)*(?:- .*\n(?:[ \t]+\S.*\n)*)*\n*", re.M)
BODY = re.compile(r"^## If skipped\n(.*?)(?=^#{1,3} |\Z)", re.M | re.S)
LINE = re.compile(r"^- (Goal date|Goal result|Worst case|Priority):\s*(.*)$")
BEAD_ID = re.compile(r"\b[a-z]+-[a-z0-9]{3}(?:\.\d+)?\b")
PATH_LABELS = ("path:on", "path:off")


def bd(*args: str) -> str:
    return subprocess.run(["bd", *args], capture_output=True, text=True, check=True).stdout


def show(bead_id: str) -> dict:
    return json.loads(bd("show", bead_id, "--json"))[0]


def current_lines(desc: str) -> dict[str, str]:
    """The section's lines keyed by name; an indented continuation joins its line."""
    m = BODY.search(desc)
    out: dict[str, str] = {}
    last = None
    for line in (m.group(1).split("\n") if m else []):
        lm = LINE.match(line)
        if lm:
            last = lm.group(1)
            out[last] = lm.group(2).strip()
        elif last and line.startswith((" ", "\t")) and line.strip():
            out[last] += " " + line.strip()
        elif line.strip():
            last = None
    return out


def check_line(name: str, value: str, changed_word: str) -> bool:
    """Return True when the line says the MVP changes; exit on a malformed line."""
    if value == "unchanged":
        return False
    if not value.startswith(changed_word + ": ") or len(value) < len(changed_word) + 12:
        sys.exit(f"{name} must be 'unchanged' or '{changed_word}: <what>', got {value!r}")
    return True


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bead_id")
    ap.add_argument("--priority", required=True, type=int, choices=range(4), help="0 to 3")
    ap.add_argument("--why", required=True, help="the reason for the priority, in one clause")
    ap.add_argument("--date", help="'unchanged' or 'slips: <P0 bead or the epic acceptance check that waits>'")
    ap.add_argument("--result", help="'unchanged' or 'changes: <the number or claim that moves>'")
    ap.add_argument("--worst", help="largest harm; add its condition and 'true today: yes|no' when conditional")
    ap.add_argument("--note", default="", help="extra text for the dated note")
    ap.add_argument("--today", required=True, help="date for the note, YYYY-MM-DD")
    ap.add_argument("--dry-run", action="store_true", help="print the section and priority, write nothing")
    a = ap.parse_args()

    bead = show(a.bead_id)
    if bead["issue_type"] == "epic":
        sys.exit("an epic states its MVP in --acceptance and carries no If skipped section")
    cur = current_lines(bead.get("description") or "")
    lines = {"Goal date": a.date, "Goal result": a.result, "Worst case": a.worst}
    for name, flag in (("Goal date", "--date"), ("Goal result", "--result"), ("Worst case", "--worst")):
        lines[name] = lines[name] or cur.get(name)
        if not lines[name]:
            sys.exit(f"{a.bead_id} has no {name} line, so pass {flag}")

    slips = check_line("Goal date", lines["Goal date"], "slips")
    changes = check_line("Goal result", lines["Goal result"], "changes")
    if (slips or changes) and a.priority != 0:
        sys.exit("a Goal line changes, so the MVP needs this bead and it is P0; pass --priority 0 or rewrite the line")
    if not (slips or changes) and a.priority == 0:
        sys.exit("both Goal lines read unchanged, so the MVP does not need this bead; pick P1 to P3 or rewrite a line")
    if slips and "acceptance" not in lines["Goal date"]:
        prefix = a.bead_id.split("-")[0] + "-"  # "sign-off" also has the shape of an id
        named = [i for i in BEAD_ID.findall(lines["Goal date"]) if i != a.bead_id and i.startswith(prefix)]
        if not named:
            sys.exit("Goal date slips: must name the P0 bead that waits, or the epic acceptance check")
        for i in named:
            waiting = show(i)
            if waiting["status"] == "closed":
                sys.exit(f"Goal date names {i}, which is closed, so nothing waits")
            # path:on is the retired label for P0, still read until the migration removes it.
            if waiting.get("priority") != 0 and "path:on" not in (waiting.get("labels") or []):
                sys.exit(f"Goal date names {i}, which is P{waiting.get('priority')}, so the MVP does not wait on it")

    why = re.sub(r"^because\s+", "", a.why.strip())
    section = (f"## If skipped\n- Goal date: {lines['Goal date']}\n- Goal result: {lines['Goal result']}\n"
               f"- Worst case: {lines['Worst case']}\n- Priority: P{a.priority}, because {why}\n\n")

    bead = show(a.bead_id)  # re-read just before the write
    desc = SECTION.sub("", (bead.get("description") or "").rstrip("\n") + "\n")
    m = re.search(r"^## Summary\n.*?(?=^## |\Z)", desc, re.M | re.S)
    if m:
        desc = desc[:m.end()].rstrip("\n") + "\n\n" + section + desc[m.end():].lstrip("\n")
    else:
        desc = section + desc.lstrip("\n")
    stale = [l for l in (bead.get("labels") or []) if l in PATH_LABELS]
    old = bead.get("priority")
    if a.dry_run:
        print(section.rstrip("\n"))
        print(f"\n{a.bead_id}: P{old} -> P{a.priority}" + (f", remove {', '.join(stale)}" if stale else ""))
        return
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
        f.write(desc.rstrip("\n") + "\n")
    note = f"{a.today}: If skipped written, P{a.priority} (was P{old}). {a.note}".strip()
    args = ["update", a.bead_id, "--body-file", f.name, "--priority", str(a.priority), "--append-notes", note]
    for label in stale:
        args += ["--remove-label", label]
    bd(*args)
    print(f"{a.bead_id} P{a.priority}")


if __name__ == "__main__":
    main()
