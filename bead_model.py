"""Derive the fields that bd-tui shows, from one `bd export`.

`enrich(rows)` returns every bead keyed by id, with the export's own fields plus derived ones,
such as `needsYou`, `flags` and `ifSkipped`. The rules for each come from rules/beads.md:

  - kind: a `kind:<x>` label, then bd's issue type, then a guess from the title.
  - needsYou: a Needs from Ben list in an open bead's newest dated note or description, or in
    an unreviewed closed bead's close reason; a `needs:ben` label; or an open, unblocked
    decision bead. A sentence that asks Ben in prose counts when no list exists. A deferred
    bead never needs Ben, because deferring it parks the question with the work.
  - flags: the structural checks from the rules, such as a missing `## Summary`.

The module also holds every bd and claude call that bd-tui makes: the export, the reviewed
label, closing, reopening, deferring and undeferring a bead, and the start and listing of
Claude sessions named after a bead. It uses only the standard library, so that any script
can import it without a venv.

The regular expressions treat \\w, \\b and \\d as ASCII-only, so they are spelled out as ASCII
classes, while \\s stays Unicode.
"""
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable

REVIEWED = "reviewed"
NEEDS = "needs:ben"
BEAD_ID = re.compile(r"^[a-z][a-z0-9]*-[a-z0-9]+(\.\d+)*$")
# A bead id inside prose.
BEAD_RE = re.compile(r"(?<![A-Za-z0-9_/-])([a-z][a-z0-9]*-[a-z0-9]{2,}(?:\.[0-9]+)*)(?![A-Za-z0-9_/-])")
DATE_RE = re.compile(r"^\s*\**\s*([0-9]{4}-[0-9]{2}-[0-9]{2})")
ITEM_RE = re.compile(r"^(?:[-*+]|[0-9]+[.)])\s+")
BULLET_RE = re.compile(r"^\s*(?:[-*]|[0-9]+\.)\s+(.*)$")
FOLLOWUPS = r"follow[- ]?ups?"
ASK_HEADER = r"needs from ben|needs ben|open for ben|ben to decide"
W = r"(?<![A-Za-z0-9_])"  # an ASCII-only \b before a word character
E = r"(?![A-Za-z0-9_])"  # an ASCII-only \b after a word character
# Beads written before the Needs from Ben convention ask in prose, so sentences that ask Ben count too.
ASK_RE = re.compile(W + r"(open for ben|ben'?s (call|confirmation|decision|approval|review)|ben to (decide|confirm|approve|"
                    r"review|merge)|needs? (ben|[^.;]{0,20}approval|sign-?off)|awaits? ben|(spend|budget) approval)", re.I)
SUMMARY_DETAIL = re.compile(r"`|--[a-z]|" + W + r"[0-9a-f]{7,40}" + E + r"|[A-Za-z0-9_]/[A-Za-z0-9_.-]+\.[A-Za-z0-9_]{1,5}" + E)
# The `bead` skill's if_skipped.py appends this note to every bead it writes, so it says nothing about progress.
BOOKKEEPING = re.compile(r"^[:\s*]*If skipped written\b")
IF_SKIPPED = re.compile(r"^## If skipped\n(.*?)(?=^#{1,3} |\Z)", re.M | re.S)
IF_SKIPPED_LINE = re.compile(r"^- (Goal date|Goal result|Worst case|Priority):\s*(.*)$")
TYPE_KIND = {"bug": "bugfix", "feature": "feature", "decision": "decision", "spike": "analysis", "chore": "ops",
             "gate": "decision"}
RANK = {"prog": 0, "ready": 1, "blocked": 2, "deferred": 3, "closed": 4}
SETTINGS = os.path.expanduser("~/.claude/settings.json")
SESSION_CWD = os.path.expanduser("~/dev")
EXPORT_TIMEOUT_S = 30  # a hung `bd export` fails the board's reread instead of holding its worker forever
# A bead's mode: label names the keyword its session starts with (beads.md, "Run labels").
SESSION_KEYWORDS = ("ultrathink", "ultracode")
PROMPT_MAX = 20000
# `claude --bg` prints "backgrounded · <id> · <name>" and then the commands that take the id.
BACKGROUNDED = re.compile(r"backgrounded · (\S+)")


# ---------- bd and claude ----------

def default_beads_dir() -> None:
    """Set BEADS_DIR from ~/.claude/settings.json when bd finds no database on its own.

    Claude Code sets BEADS_DIR from that file's env block, so a board started from a plain
    terminal would otherwise fail its export and show no beads.
    """
    if os.environ.get("BEADS_DIR"):
        return
    try:
        if subprocess.run(["bd", "where"], capture_output=True).returncode == 0:
            return
        with open(SETTINGS) as f:
            beads_dir = json.load(f).get("env", {}).get("BEADS_DIR")
    except (OSError, ValueError, AttributeError):
        return
    if beads_dir:
        os.environ["BEADS_DIR"] = os.path.expanduser(beads_dir)
        print(f"{os.path.basename(sys.argv[0])}: BEADS_DIR={os.environ['BEADS_DIR']} (from {SETTINGS})", file=sys.stderr)


def export_beads() -> list[dict]:
    out = subprocess.run(["bd", "export"], capture_output=True, text=True, timeout=EXPORT_TIMEOUT_S, check=True).stdout
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def set_reviewed(bead_id: str, reviewed: bool) -> None:
    """Add a closed bead's reviewed label, or remove it again.

    Only Ben's m key in bd-tui calls this, because the rules reserve the reviewed label for Ben.
    close_bead adds the label too, because Ben closes a bead from the board with a result he wrote.
    """
    if not BEAD_ID.match(bead_id):
        raise ValueError(f"not a bead id: {bead_id!r}")
    subprocess.run(["bd", "label", "add" if reviewed else "remove", bead_id, REVIEWED],
                   capture_output=True, text=True, check=True)


def bd_write(args: list[str]) -> None:
    """Run one bd write. A refusal raises CalledProcessError with bd's message in stderr, and a hang raises TimeoutExpired.

    stdin is closed, because bd behaves differently when stdin is a terminal, and the board's terminal is in raw mode.
    """
    subprocess.run(["bd", *args], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=EXPORT_TIMEOUT_S,
                   check=True)


def dated(text: str) -> str:
    """One line of Ben's text as a dated note, so that the board shows it as the bead's newest state."""
    return f"{time.strftime('%Y-%m-%d')}: {' '.join(text.split())}"


# The four writes below run from Ben's Shift-C and Shift-D keys in bd-tui. Each passes a value as --flag=value, so
# that a reason starting with "-" never reads as a flag, and each writes a dated note only from text Ben typed,
# because the board reads an open bead's Needs from Ben list from its newest note alone.

def close_bead(bead_id: str, reason: str, run: Callable[[list[str]], None] = bd_write) -> None:
    """Close a bead with Ben's reason as its result, and mark the result reviewed, because Ben wrote it."""
    if not BEAD_ID.match(bead_id):
        raise ValueError(f"not a bead id: {bead_id!r}")
    # bd closes a bead with an empty reason as "Closed", which leaves the board a result that says nothing.
    if not reason.strip():
        raise ValueError("type the result, because bd would record an empty reason as \"Closed\"")
    run(["close", bead_id, f"--reason={reason.strip()}"])
    run(["label", "add", bead_id, REVIEWED])


def reopen_bead(bead_id: str, why: str, run: Callable[[list[str]], None] = bd_write) -> None:
    """Reopen a closed bead and remove its reviewed label, so that its next result waits for review again.

    bd reopen clears the close reason and keeps the labels, and it stores no reason, so a typed reason
    becomes a dated note instead.
    """
    if not BEAD_ID.match(bead_id):
        raise ValueError(f"not a bead id: {bead_id!r}")
    run(["reopen", bead_id])
    run(["label", "remove", bead_id, REVIEWED])
    if why.strip():
        run(["update", bead_id, f"--append-notes={dated('Reopened: ' + why)}"])


def defer_bead(bead_id: str, until: str, why: str, run: Callable[[list[str]], None] = bd_write) -> None:
    """Defer a bead, until a date when one is given, such as 2026-12-01, +2w or next monday.

    bd defer appends its --reason to the notes without a date, so the reason goes in already dated.
    """
    if not BEAD_ID.match(bead_id):
        raise ValueError(f"not a bead id: {bead_id!r}")
    args = ["defer", bead_id]
    if until.strip():
        args.append(f"--until={until.strip()}")
    if why.strip():
        args.append(f"--reason={dated('Deferred: ' + why)}")
    run(args)


def undefer_bead(bead_id: str, why: str, run: Callable[[list[str]], None] = bd_write) -> None:
    """Return a deferred bead to the open list. bd undefer takes no reason, so a typed one becomes a dated note."""
    if not BEAD_ID.match(bead_id):
        raise ValueError(f"not a bead id: {bead_id!r}")
    run(["undefer", bead_id])
    if why.strip():
        run(["update", bead_id, f"--append-notes={dated('Undeferred: ' + why)}"])


def child_env() -> dict:
    """Return this process's environment without the variables of the Claude session that started it.

    A board started from a Claude job inherits that job's session id, job directory and messaging
    socket, and a session the board launches must not start with them. Claude Code sets its own
    variables again and re-applies the env block of ~/.claude/settings.json.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE") and k != "AI_AGENT"}


def launch_prompt(bead: dict, ask: str, keywords: list[str]) -> str:
    """Put the bead first as context, so that Ben's text stays the instruction."""
    title = " ".join(bead["title"].split())
    context = (f'This session concerns bead {bead["id"]}: "{title}". Run `bd show {bead["id"]}` first, '
               "and read the bead as the context for the request that follows.")
    return "\n\n".join([*keywords, context, ask.strip()])


def launch_session(bead_id: str, ask: str, keywords: list[str], cwd: str = SESSION_CWD) -> dict:
    """Start a background Claude session on one bead and return its short id and name."""
    if not BEAD_ID.match(bead_id):
        raise ValueError(f"not a bead id: {bead_id!r}")
    # An empty ask would start a full work session, or a multi-agent run on a mode:ultracode bead.
    if not ask.strip():
        raise ValueError("type what Claude should do with the bead")
    if len(ask) > PROMPT_MAX:
        raise ValueError(f"the prompt is {len(ask)} characters, and the limit is {PROMPT_MAX}")
    shown = subprocess.run(["bd", "show", bead_id, "--json"], capture_output=True, text=True, check=True)
    bead = json.loads(shown.stdout)[0]
    name = f"{bead_id} {' '.join(bead['title'].split())}"[:80]
    prompt = launch_prompt(bead, ask, [k for k in SESSION_KEYWORDS if k in keywords])
    # The prompt always starts with a keyword or "This session", so claude never reads it as a flag.
    out = subprocess.run(["claude", "--bg", "--agent", "claude", "--name", name, prompt], cwd=cwd,
                         env=child_env(), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                         timeout=60, check=True)
    m = BACKGROUNDED.search(out.stdout)
    if not m:
        raise RuntimeError(f"claude --bg printed no session id:\n{out.stdout}{out.stderr}")
    return {"session": m.group(1), "name": name}


def list_sessions(include_finished: bool) -> list[dict]:
    """Return `claude agents --json`, with finished background sessions when asked."""
    cmd = ["claude", "agents", "--json", *(["--all"] if include_finished else [])]
    out = subprocess.run(cmd, env=child_env(), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                         timeout=20, check=True).stdout
    return json.loads(out)


def session_bead(name: str | None, bead_id: str) -> bool:
    """A session belongs to a bead when its name is the id, or starts with the id and a space.

    So ent-aaa does not claim the sessions of ent-aaa.22.
    """
    name = name or ""
    return name == bead_id or name.startswith(bead_id + " ")


def bead_sessions(bead_id: str) -> list[dict]:
    """List the sessions named after a bead, newest first, finished ones included."""
    mine = [{k: a.get(k) for k in ("id", "name", "state", "startedAt")} for a in list_sessions(True)
            if session_bead(a.get("name"), bead_id)]
    return sorted(mine, key=lambda a: a["startedAt"] or 0, reverse=True)


# ---------- derived fields ----------

def kind_of(b: dict) -> dict:
    """The kind comes from a kind:<x> label, then bd's own issue type, then a guess from the text."""
    if b.get("issue_type") == "epic":
        return {"name": "epic", "inferred": False}
    label = next((l for l in b["labels"] if l.startswith("kind:")), None)
    if label:
        return {"name": label[5:], "inferred": False}
    by_type = TYPE_KIND.get(b.get("issue_type"))
    if by_type:
        return {"name": by_type, "inferred": False}
    t, d = b.get("title") or "", (b.get("description") or "")[:200]
    has = lambda pattern, s=t: re.search(pattern, s, re.I) is not None
    if re.match(r"(?:gate|human)" + E, d, re.I) or has(W + r"(?:decision|decide|approv|is merged|merged to main|get ben)"):
        guess = "decision"
    elif has(W + r"label"):
        guess = "labeling"
    elif has(W + r"(?:figure|white paper|doc|tab|write-?up|report|readme)" + E):
        guess = "doc"
    elif has(W + r"(?:run|rerun|sweep|pretrain|re-?train|trained|scored?|smoke|arm|fold|baseline|beats?|seed)" + E):
        guess = "experiment"
    elif has(W + r"(?:why|investigat|audit|review|analy[sz]|measure|compare|probe)"):
        guess = "analysis"
    elif has(W + r"(?:deploy|volume|GiB|pod|k8s|infra|cluster|ECR|promote)" + E):
        guess = "ops"
    elif has(W + r"(?:fix|bug|leak|broken|crash)" + E):
        guess = "bugfix"
    else:
        guess = "feature"
    return {"name": guess, "inferred": True}


def split_description(desc: str) -> dict:
    """Summary is the "## Summary" section; older beads fall back to Objective, Goal or the first paragraph."""
    parts = re.split(r"^(#{1,3}\s+.+)$", desc, flags=re.M)
    secs = [{"title": "", "body": parts[0]}]
    for i in range(1, len(parts), 2):
        secs.append({"title": re.sub(r"^#+\s+", "", parts[i], count=1).strip(),
                     "body": parts[i + 1] if i + 1 < len(parts) else ""})

    def join(xs):
        return "".join((f"## {x['title']}\n" if x["title"] else "") + x["body"] for x in xs).strip()

    for name in ("summary", "objective", "goal"):
        s = next((x for x in secs if x["title"].lower() == name and x["body"].strip()), None)
        if s:
            return {"summary": s["body"].strip(), "summaryFrom": name, "rest": join([x for x in secs if x is not s])}
    first = next((x for x in secs if x["body"].strip()), None)
    if not first:
        return {"summary": "", "summaryFrom": "", "rest": ""}
    paras = re.split(r"\n\s*\n", first["body"].strip())
    rest = "\n".join(["\n\n".join(paras[1:]), join([x for x in secs if x is not first])]).strip()
    return {"summary": paras[0], "summaryFrom": "paragraph", "rest": rest}


def dated_notes(notes: str | None) -> list[dict]:
    """Split the notes into dated entries ("2026-11-06: ..."), oldest first; undated lines before the first are dropped."""
    if not notes:
        return []
    lines, out = notes.split("\n"), []
    starts = [i for i, line in enumerate(lines) if DATE_RE.match(line)]
    for k, start in enumerate(starts):
        end = starts[k + 1] if k + 1 < len(starts) else len(lines)
        out.append({"date": DATE_RE.match(lines[start]).group(1), "text": "\n".join(lines[start:end]).strip()})
    return out


def latest_note(notes: str | None) -> dict | None:
    """The last dated entry in the notes, bookkeeping included; an open bead's Needs from Ben list is read from it."""
    entries = dated_notes(notes)
    return entries[-1] if entries else None


def current_note(notes: str | None) -> dict | None:
    """The bead's current state: the newest dated entry other than an "If skipped written" bookkeeping note.

    Both boards show it. The Needs from Ben list still comes from latest_note, because reading this
    note instead moves beads in and out of Decide.
    """
    for entry in reversed(dated_notes(notes)):
        if not BOOKKEEPING.match(DATE_RE.sub("", entry["text"], count=1)):
            return entry
    return None


def split_checks(line: str) -> list[str]:
    """Split on "; " outside inline code, so that a command such as `cd x; make test` stays one check."""
    out = [""]
    for i, part in enumerate(re.split(r"(`[^`\n]*`)", line)):
        bits = [part] if i % 2 else part.split("; ")
        out[-1] += bits[0]
        out.extend(bits[1:])
    return [s.strip() for s in out if s.strip()]


def checks_of(text: str | None) -> dict:
    """The acceptance as checks: one per line, or split on "; " when it is one line.

    A line that ends in ":" and comes before a bullet introduces the bullets, so it is a heading
    and not a check. "[x]" ticks a check and "[ ]" leaves it open; done is None when unmarked.
    """
    lines = [l.strip() for l in (text or "").split("\n") if l.strip()]
    raw = split_checks(lines[0]) if len(lines) == 1 else lines
    items = []
    for i, l in enumerate(raw):
        nxt = raw[i + 1] if i + 1 < len(raw) else ""
        if not ITEM_RE.match(l) and l.endswith(":") and ITEM_RE.match(nxt):
            items.append({"text": l, "head": True})
            continue
        t = ITEM_RE.sub("", l, count=1)
        m = re.match(r"^\[([ xX])\]\s*(.*)\Z", t)
        items.append({"text": m.group(2), "done": m.group(1) != " "} if m else {"text": t, "done": None})
    checks = [x for x in items if not x.get("head")]
    return {"items": items, "n": len(checks), "ticked": sum(1 for x in checks if x["done"]),
            "marked": any(x["done"] is not None for x in checks)}


def parse_list(texts: list, header: str, known: set) -> list[dict]:
    """Read a header line, such as "Follow-ups:" or "Needs from Ben:", with inline text and/or bullets under it."""
    out, head = [], re.compile(r"^\s*(?:#+\s*)?\**(?:" + header + r")\**\s*:?\s*(.*)$", re.I)

    def item(t):
        return {"text": t, "ids": [m.group(1) for m in BEAD_RE.finditer(t) if m.group(1) in known]}

    for text in texts:
        if not text:
            continue
        lines = text.split("\n")
        for i, line in enumerate(lines):
            m = head.match(line)
            if not m:
                continue
            inline = m.group(1).strip()
            if inline and not re.fullmatch(r"none\.?", inline, re.I):
                out.append(item(inline))
            for j in range(i + 1, len(lines)):
                bm = BULLET_RE.match(lines[j])
                if bm:
                    out.append(item(bm.group(1)))
                elif lines[j].strip() == "" and BULLET_RE.match(lines[j + 1] if j + 1 < len(lines) else ""):
                    continue
                else:
                    break
    return out


def asks_of(b: dict, known: set) -> list[dict]:
    """Open questions for Ben: an open bead's newest note, or a closed bead's result until he reviews it."""
    closed = b["status"] == "closed"
    if closed:
        texts = [b.get("close_reason")] if b["awaiting"] else []
    else:
        texts = [b["latest"]["text"] if b["latest"] else b.get("notes")]
    listed = parse_list(texts if closed else [*texts, b.get("description")], ASK_HEADER, known)
    if listed:
        return listed
    prose = []
    for t in texts:
        for s in re.split(r"(?<=[.;])\s+|\n", t or ""):
            if ASK_RE.search(s):
                text = re.sub(r"^[:\s*-]+", "", DATE_RE.sub("", s, count=1), count=1).strip()
                prose.append({"text": text, "ids": [], "prose": True})
    return prose


def flags_of(b: dict) -> list[str]:
    """Structural checks from the writing rules: the brief exists and stays free of agent detail."""
    f, is_epic, is_open = [], b.get("issue_type") == "epic", b["status"] != "closed"
    if b["summaryFrom"] != "summary":
        if is_open:
            f.append("no ## Summary")
    elif SUMMARY_DETAIL.search(b["summary"]):
        f.append("summary holds paths, flags or commit ids")
    if not is_epic and not b.get("acceptance_criteria"):
        f.append("no Done when")
    if is_open and not is_epic and not b["ben"] and b["kind"]["name"] != "decision":
        f.append("no ben: label")
    if is_open and not is_epic:
        if not re.search(r"^## If skipped$", b.get("description") or "", re.M):
            f.append("no ## If skipped")
        f += priority_flags(b)
    if is_open and is_epic and not b.get("acceptance_criteria"):
        f.append("epic has no goal check")
    if is_open and any(re.fullmatch(r"size-[0-9]", l) for l in b["labels"]):
        f.append("retired size label")
    if b["kind"]["inferred"]:
        f.append("no kind label")
    if b["status"] == "closed" and not is_epic:
        if not b.get("close_reason"):
            f.append("no result")
        elif b["kind"]["name"] in ("experiment", "analysis") and not re.search(r"[0-9]", b["close_reason"].split("\n")[0]):
            f.append("result headline has no number")
    return f


def priority_flags(b: dict) -> list[str]:
    """An open bead's priority against its If skipped lines: P0 exactly when a goal line changes.

    The goal lines read against the epic's acceptance check, its MVP, so a change on either one
    means the MVP needs the bead. Without a section, "no ## If skipped" already covers it.
    """
    s, p = b.get("ifSkipped"), b.get("priority")
    if s is None:
        return []
    if "why" not in s:
        return ["no Priority line"]
    moves = s.get("date", "").startswith("slips:") or s.get("result", "").startswith("changes:")
    if p not in (0, 1, 2, 3):
        return [f"priority P{p}"]
    if moves and p != 0:
        return ["lines say P0"]
    if not moves and p == 0:
        return ["lines say not P0"]
    return []


def if_skipped(desc: str | None) -> dict | None:
    """The four lines of the "## If skipped" section, keyed date, result, worst and why; None without one.

    A line's indented continuation joins it, because the bead skill wraps a long worst case.
    """
    m = IF_SKIPPED.search(desc or "")
    if not m:
        return None
    keys = {"Goal date": "date", "Goal result": "result", "Worst case": "worst", "Priority": "why"}
    out, last = {}, None
    for line in m.group(1).split("\n"):
        lm = IF_SKIPPED_LINE.match(line)
        if lm:
            last = keys[lm.group(1)]
            out[last] = lm.group(2).strip()
        elif last and line.startswith((" ", "\t")) and line.strip():
            out[last] += " " + line.strip()
        elif line.strip():
            last = None
    return out


def bucket(b: dict) -> str:
    if b["status"] == "closed":
        return "closed"
    if b["status"] == "in_progress":
        return "prog"
    if b["status"] == "deferred":
        return "deferred"
    return "blocked" if b["isBlocked"] else "ready"


def enrich(rows: list[dict]) -> dict[str, dict]:
    """Return every bead keyed by id, in export order, with the derived fields both boards read."""
    beads = {r["id"]: {**r, "children": [], "blockers": [], "blocks": [], "discovered": [], "sources": [],
                       "related": [], "parent": None} for r in rows}
    for b in beads.values():
        for d in b.get("dependencies") or []:
            other = beads.get(d.get("depends_on_id"))
            if not other:
                continue
            if d.get("type") == "parent-child":
                b["parent"] = other["id"]
                other["children"].append(b["id"])
            elif d.get("type") == "blocks":
                b["blockers"].append(other["id"])
                other["blocks"].append(b["id"])
            elif d.get("type") == "discovered-from":
                other["discovered"].append(b["id"])
                b["sources"].append(other["id"])
            else:
                b["related"].append(other["id"])
    known = set(beads)
    for b in beads.values():
        b["labels"] = b.get("labels") or []
        b["awaiting"] = b["status"] == "closed" and b.get("issue_type") != "epic" and REVIEWED not in b["labels"]
        b["isBlocked"] = b["status"] != "closed" and any(beads[x]["status"] != "closed" for x in b["blockers"])
        b["kind"] = kind_of(b)
        run = lambda k: next((l for l in b["labels"] if l.startswith(k + ":")), "")[len(k) + 1:]
        b["ben"], b["wait"], b["mode"] = run("ben"), run("wait"), run("mode")
        b["oneshot"] = b["ben"] == "none" and not b["wait"] and not b["mode"]
        b.update(split_description(b.get("description") or ""))
        b["latest"] = latest_note(b.get("notes"))
        b["current"] = current_note(b.get("notes"))
        b["ifSkipped"] = if_skipped(b.get("description"))
        b["checks"] = checks_of(b.get("acceptance_criteria"))
        b["followups"] = parse_list([b.get("close_reason"), b.get("notes")], FOLLOWUPS, known)
        b["unfiled"] = sum(1 for f in b["followups"] if not f["ids"])
        b["asks"] = asks_of(b, known)
        b["needsYou"] = b["status"] != "deferred" and (
            bool(b["asks"]) or NEEDS in b["labels"]
            or (b["status"] != "closed" and not b["isBlocked"] and b["kind"]["name"] == "decision"))
        b["flags"] = flags_of(b)
        b["bucket"] = bucket(b)
    return beads


# ---------- the thread tree ----------

def epic_of(beads: dict, bead_id: str) -> str | None:
    """The top epic above a bead, or None for a bead outside every epic."""
    b = beads.get(bead_id)
    while b and b["parent"]:
        b = beads.get(b["parent"])
    return b["id"] if b and b.get("issue_type") == "epic" else None


def descendants(beads: dict, bead_id: str) -> list[str]:
    out, stack = [], list(beads[bead_id]["children"])
    while stack:
        c = stack.pop()
        out.append(c)
        stack.extend(beads[c]["children"])
    return out


def area_of(beads: dict, bead_id: str) -> str:
    """A bead's area: its top epic's area: label, else its own, else ""."""
    epic = epic_of(beads, bead_id)
    for b in (beads[epic] if epic else None, beads[bead_id]):
        area = next((l[5:] for l in (b or {}).get("labels") or [] if l.startswith("area:")), "")
        if area:
            return area
    return ""


def asks_ben_now(b: dict) -> bool:
    """A bead with a question Ben has not answered yet: needsYou on a bead that is not an epic.

    A closed bead asks only while its result awaits review, because marking the result reviewed
    answers the Needs from Ben list in its close reason.
    """
    return b["needsYou"] and b.get("issue_type") != "epic" and (b["status"] != "closed" or b["awaiting"])


def needs_ben_now(b: dict) -> bool:
    """The terminal board's now filter: a bead that is not an epic, and is in progress or asks Ben a question."""
    return b.get("issue_type") != "epic" and (b["status"] == "in_progress" or asks_ben_now(b))


def id_key(bead_id: str) -> list:
    """Sort ids the way a person reads them, so ent-aaa.9 comes before ent-aaa.10."""
    return [int(p) if p.isdigit() else p for p in re.split(r"([0-9]+)", bead_id)]


def thread_order(beads: dict, epic_id: str) -> list[str]:
    """An epic's open beads in dependency order: a bead follows the open beads that block it.

    Ties go by status (in progress, ready, blocked, deferred) and then by id. A cycle cannot be
    ordered, so its beads follow the rest in id order.
    """
    ids = [i for i in descendants(beads, epic_id) if beads[i]["status"] != "closed" and beads[i].get("issue_type") != "epic"]
    todo = set(ids)
    waits = {i: {x for x in beads[i]["blockers"] if x in todo} for i in ids}
    key = lambda i: (RANK[beads[i]["bucket"]], id_key(i))
    out = []
    while todo:
        ready = sorted((i for i in todo if not waits[i] & todo), key=key)
        if not ready:
            out.extend(sorted(todo, key=id_key))
            break
        out.append(ready[0])
        todo.discard(ready[0])
    return out
