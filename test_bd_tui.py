#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["textual==8.2.8"]
# ///
"""Check bd-tui headless on a synthetic bead export, and optionally on the live one.

  ./test_bd_tui.py          the synthetic export below; exits 1 on any failed check
  ./test_bd_tui.py --live   also checks the open, now and closed lists on today's `bd export` (needs BEADS_DIR)
  ./test_bd_tui.py --bd     also closes, reopens, defers and undefers a bead in a throwaway bd database

Runs from any directory with no venv, because uv installs Textual from the script header.
The fixture is synthetic, so that the repo holds no real beads. --live only reads, and --bd
writes only to a database it creates in a temporary directory, never to the one in BEADS_DIR.
"""
import asyncio
import copy
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable

from rich.console import Console

import bead_model as bm

HERE = os.path.dirname(os.path.abspath(__file__))
_loader = importlib.machinery.SourceFileLoader("bd_tui", os.path.join(HERE, "bd-tui"))
tui = importlib.util.module_from_spec(importlib.util.spec_from_loader("bd_tui", _loader))
_loader.exec_module(tui)

SKIPPED = ("## Summary\nOne line.\n\n## If skipped\n- Goal date: slips: ent-aaa.4 waits on the load test\n"
           "- Goal result: changes: the latency covers one export size\n- Worst case: a fast small export reads as\n"
           "  fast at every size\n- Priority: P0, because the load test waits on it\n\n## Context\nDetail.")


def bead(bid: str, **kw) -> dict:
    row = {"id": bid, "title": f"Title of {bid}", "status": "open", "priority": 2, "issue_type": "task",
           "created_at": "2026-11-01T00:00:00Z", "updated_at": "2026-11-02T00:00:00Z", "labels": [],
           "description": "## Summary\nx", "dependencies": []}
    row.update(kw)
    return row


def child(of: str) -> dict:
    return {"depends_on_id": of, "type": "parent-child"}


def blocked_by(of: str) -> dict:
    return {"depends_on_id": of, "type": "blocks"}


ROWS = [
    bead("ent-aaa", issue_type="epic", title="Alpha epic", labels=["area:alpha"], acceptance_criteria="the goal holds"),
    bead("ent-aaa.1", status="in_progress", priority=0, labels=["area:alpha"], dependencies=[child("ent-aaa")]),
    bead("ent-aaa.2", priority=1, labels=["area:alpha", "needs:ben"], dependencies=[child("ent-aaa")]),
    bead("ent-aaa.3", status="in_progress", labels=["area:alpha"], dependencies=[child("ent-aaa")],
         notes="2026-11-03: Needs from Ben:\n- pick an export size"),
    bead("ent-aaa.4", priority=0, labels=["area:alpha"], dependencies=[child("ent-aaa")]),
    bead("ent-aaa.5", status="closed", closed_at="2026-11-04T00:00:00Z", labels=["area:alpha"],
         close_reason="Done 3.\nFollow-ups:\n- ent-aaa.4 load test at full size\n- a doc for the export\n"
                      "Needs from Ben:\n- ship A or B", dependencies=[child("ent-aaa")]),
    bead("ent-aaa.6", status="closed", closed_at="2026-11-04T00:00:00Z", labels=["area:alpha", "reviewed"],
         close_reason="Done 4.", dependencies=[child("ent-aaa")]),
    bead("ent-aaa.7", labels=["area:alpha", "kind:decision"], dependencies=[child("ent-aaa")]),
    bead("ent-aaa.8", labels=["area:alpha", "kind:decision"], dependencies=[child("ent-aaa"), blocked_by("ent-aaa.4")]),
    bead("ent-aaa.9", priority=0, labels=["area:alpha", "needs:ben", "ben:plan", "kind:experiment"], description=SKIPPED,
         acceptance_criteria="- three export sizes timed\n- [x] p95 per size\n- one size run twice",
         notes="2026-11-05: Plan written; Ben picks the sizes.\n2026-11-06: If skipped written, P0 (was P2).",
         dependencies=[child("ent-aaa"), blocked_by("ent-aaa.1")]),
    bead("ent-aaa.11", status="deferred", labels=["area:alpha", "kind:decision", "needs:ben"],
         dependencies=[child("ent-aaa")]),
    bead("ent-aaa.10", issue_type="epic", title="Nested epic", labels=["area:alpha"], dependencies=[child("ent-aaa")]),
    bead("ent-aaa.10.1", status="in_progress", labels=["area:alpha"], dependencies=[child("ent-aaa.10")]),
    bead("ent-bbb", issue_type="epic", status="in_progress", title="Epic with no area"),
    bead("ent-bbb.1", status="in_progress", dependencies=[child("ent-bbb")]),
    bead("ent-ccc", status="in_progress", labels=["area:alpha"]),
]
OPEN = {"ent-aaa.1", "ent-aaa.2", "ent-aaa.3", "ent-aaa.4", "ent-aaa.7", "ent-aaa.8", "ent-aaa.9", "ent-aaa.10.1",
        "ent-bbb.1", "ent-ccc"}
NOW = {"ent-aaa.1", "ent-aaa.2", "ent-aaa.3", "ent-aaa.7", "ent-aaa.9", "ent-aaa.10.1", "ent-bbb.1", "ent-ccc"}
NOW_CLOSED = {"ent-aaa.5"}  # the unreviewed result whose close reason asks Ben to ship A or B
SESSIONS = [{"name": "ent-aaa.1 build it", "state": "working"}, {"name": "ent-aaa.10 other", "state": "working"}]
failures: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("PASS " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)


def plain(renderable) -> str:
    """What a renderable shows on a 200-column terminal, without colour."""
    console = Console(width=200, color_system=None, file=io.StringIO())
    with console.capture() as captured:
        console.print(renderable)
    return captured.get()


def bead_nodes(app) -> dict:
    return {n.data[1]: n for n in app.walk(app.query_one(tui.BeadTree).root) if n.data and n.data[0] == "bead"}


async def settle(app, pilot) -> None:
    await app.workers.wait_for_complete()
    await pilot.pause()
    await pilot.pause()


async def until(pilot, cond, limit_s: float = 2.0) -> bool:
    """Wait for a condition that a timer makes true, so a check never depends on how ticks line up."""
    end = time.monotonic() + limit_s
    while not cond() and time.monotonic() < end:
        await pilot.pause(0.05)
    return cond()


def banned_bindings(app) -> set:
    """Every key that a binding on the focused chain or any mounted widget answers, intersected with the ban."""
    keys = set(app.active_bindings)
    for screen in app.screen_stack:
        for node in screen.walk_children(with_self=True):
            keys |= set(node._bindings.key_to_bindings)
    keys |= set(app._bindings.key_to_bindings)
    return keys & tui.BANNED


async def check_lists(rows, sessions, expected: tuple[set, set] | None, label: str) -> None:
    """The board opens on every open bead, n narrows it to the now rule, and n on the closed list keeps open questions."""
    app = tui.BeadBoard(load_rows=lambda: rows, load_sessions=lambda: sessions)
    async with app.run_test(size=(200, 60)) as pilot:
        await settle(app, pilot)
        beads = app.beads
        beads_only = {i for i, b in beads.items() if b.get("issue_type") != "epic"}
        opened = {i for i in beads_only if beads[i]["status"] not in ("closed", "deferred")}
        nodes = bead_nodes(app)
        check(set(nodes) == opened, f"{label}: the board opens on all {len(opened)} open beads, no deferred, closed or epic row")
        grouped = all(n.parent.data[1] == bm.epic_of(beads, i)
                      and n.parent.parent.data[1] == (bm.area_of(beads, i) or tui.NO_AREA) for i, n in nodes.items())
        check(grouped, f"{label}: every bead sits under its top epic, under that epic's area")
        rule = {i for i in opened if bm.needs_ben_now(beads[i])}
        asks = {i for i in beads_only if beads[i]["status"] == "closed" and beads[i]["awaiting"] and beads[i]["needsYou"]}
        if expected is not None:
            check((rule, asks) == expected, f"{label}: the now rule picks the hand-listed open beads and unreviewed questions")
        await pilot.press("n")
        await settle(app, pilot)
        check(set(bead_nodes(app)) == rule, f"{label}: n narrows the open list to the {len(rule)} beads in progress or needing Ben")
        await pilot.press("c")
        await settle(app, pilot)
        check(set(bead_nodes(app)) == asks,
              f"{label}: on the closed list, n keeps the {len(asks)} unreviewed results that ask Ben a question")
        if expected is None:
            lists = [set(app.funnel(s, {})[-1][1]) for s in tui.SCOPES]
            check(sum(map(len, lists)) == len(beads_only) and set().union(*lists) == beads_only,
                  f"{label}: the open, deferred and closed lists hold each of the {len(beads_only)} beads exactly once")
            # Every bead's detail, every epic's and every thread render on the real export, closed ones too.
            broken = []
            for bid, b in beads.items():
                try:
                    plain(app.epic_text(bid) if b.get("issue_type") == "epic" else app.bead_text(bid))
                    plain(app.thread_text(bm.epic_of(beads, bid)))
                except Exception as e:  # noqa: BLE001 - report every bead that fails, not the first
                    broken.append(f"{bid}: {type(e).__name__} {e}")
            check(not broken, f"{label}: the detail and thread of all {len(beads)} beads render" + "".join(f"\n     {x}" for x in broken[:5]))
            print(f"     open {len(opened)} › now {len(rule)}: {sum(beads[i]['status'] == 'in_progress' for i in rule)} in progress, "
                  f"{sum(bm.asks_ben_now(beads[i]) for i in rule)} need Ben · closed › now {len(asks)}")


async def check_synthetic() -> None:
    await check_lists(ROWS, SESSIONS, (NOW, NOW_CLOSED), "synthetic")
    app = tui.BeadBoard(load_rows=lambda: ROWS, load_sessions=lambda: SESSIONS)
    async with app.run_test(size=(200, 60)) as pilot:
        await settle(app, pilot)
        tree = app.query_one(tui.BeadTree)
        areas = [n.data[1] for n in tree.root.children]
        check(areas == ["alpha", tui.NO_AREA], f"areas run busiest first, no area last: {areas}")
        nodes = bead_nodes(app)
        check(nodes["ent-aaa.1"].label.plain.startswith("●"), "a bead with a working session shows the live mark")
        check(not nodes["ent-aaa.2"].label.plain.startswith("●"), "a bead without a session shows no live mark")

        # The detail of a selected bead keeps the approved order.
        tree.move_cursor(nodes["ent-aaa.9"])
        await pilot.pause()
        body = plain(app.detail)
        marks = ["ent-aaa.9 · blocked · needs you · P0", "If skipped", " date ", "slips: ent-aaa.4",
                 " result ", " worst ", "fast small export reads as fast at every size", " priority ",
                 "P0, because the load test waits on it", "ben:plan · kind:experiment",
                 "Done when", "☐ three export sizes", "☑ p95 per size", "Thread ent-aaa · blocked by .1 · blocks –",
                 "Latest 2026-11-05", "Plan written"]
        where = [body.find(m) for m in marks]
        check(all(i >= 0 for i in where) and where == sorted(where),
              "the detail runs: why listed, If skipped, labels, Done when, thread, latest note")
        check("If skipped written" not in body, "the Latest line skips the bookkeeping note")
        b9 = app.beads["ent-aaa.9"]
        no_why = {**b9, "ifSkipped": {k: v for k, v in b9["ifSkipped"].items() if k != "why"}}
        check(not bm.priority_flags(b9) and bm.priority_flags({**b9, "priority": 2}) == ["lines say P0"]
              and bm.priority_flags(no_why) == ["no Priority line"],
              "a priority that its If skipped lines do not support, or no Priority line, is flagged")

        # Ctrl-n and Ctrl-p move the cursor like j and k.
        line = tree.cursor_line
        await pilot.press("ctrl+n")
        down = tree.cursor_line
        await pilot.press("ctrl+p")
        check(down == line + 1 and tree.cursor_line == line, "Ctrl-n moves the cursor down a line and Ctrl-p moves it back")

        # g swaps the detail for the thread in dependency order.
        await pilot.press("g")
        body = plain(app.detail)
        check(body.startswith("Thread ent-aaa") and body.find(".1 ") < body.find(".9 ") and body.find(".4 ") < body.find(".8 "),
              "g shows the thread with each bead after the beads that block it")
        await pilot.press("g")

        # o, d and c pick a list; n, v and p only narrow it; the title counts every step.
        title = lambda: app.title_text.plain
        check("Open 10 · " in title() and "n now 8" in app.keys_text.plain and "p P0 3" in app.keys_text.plain,
              f"the title counts the open list and the strip counts what n and p would leave: {title()!r}")
        await pilot.press("n", "p")
        await settle(app, pilot)
        check(set(bead_nodes(app)) == {"ent-aaa.1", "ent-aaa.9"} and "Open 10 › now 8 › P0 2 · " in title(),
              f"n then p narrow the open list, and the title counts each step: {title()!r}")
        await pilot.press("c")
        await settle(app, pilot)
        check(not bead_nodes(app) and "o shows 2 open" in plain(app.detail),
              "c keeps n and p, and an empty list names the list where they find beads")
        await pilot.press("n", "p", "v")
        await settle(app, pilot)
        check(set(bead_nodes(app)) == {"ent-aaa.5"} and "Closed 2 › unreviewed 1 · " in title(),
              "v narrows the closed list to the result not yet reviewed")
        await pilot.press("d")
        await settle(app, pilot)
        check(set(bead_nodes(app)) == {"ent-aaa.11"} and not app.filters_on["unreviewed"] and "v unreviewed" not in app.keys_text.plain,
              "d lists the deferred bead, and leaving the closed list drops v from the filters and the strip")
        await pilot.press("v")
        await settle(app, pilot)
        check(set(bead_nodes(app)) == {"ent-aaa.11"}, "v outside the closed list changes nothing")
        await pilot.press("o")
        await settle(app, pilot)
        check(set(bead_nodes(app)) == OPEN, "o returns to every open bead")

        # / searches, and letters typed into the box never reach the tree's keys.
        check(not banned_bindings(app), "no binding uses Ctrl-h/j/k/l with the tree focused")
        await pilot.press("slash", *"bbb")
        await settle(app, pilot)
        check(set(bead_nodes(app)) == {"ent-bbb.1"} and "Open 10 › “bbb” 1" in title(), "/ narrows the list to matching beads")
        check(app.query_one("#search").value == "bbb", "letters typed in the search box stay in the box")
        check(not banned_bindings(app), "no binding uses Ctrl-h/j/k/l with the search box focused")
        await pilot.press("escape")
        await settle(app, pilot)
        check(set(bead_nodes(app)) == OPEN, "Esc clears the search")

        # s asks for one line; Esc cancels without starting anything.
        tree.move_cursor(bead_nodes(app)["ent-aaa.1"])
        await pilot.press("s")
        await pilot.pause()
        check(isinstance(app.screen, tui.AskScreen), "s opens the one-line prompt")
        await pilot.press(*"jk")
        check(app.screen.query_one("#ask").value == "jk", "j and k type into the prompt")
        check(not banned_bindings(app), "no binding uses Ctrl-h/j/k/l with the session prompt open")
        # The session poll and a resize rebuild the tree while Ben types; the prompt must survive both.
        app.set_sessions({"ent-aaa.2": "working"})
        await pilot.resize_terminal(180, 50)
        await pilot.pause()
        check(isinstance(app.screen, tui.AskScreen) and app.screen.query_one("#ask").value == "jk"
              and bead_nodes(app)["ent-aaa.2"].label.plain.startswith("●"),
              "a session poll and a resize rebuild the tree under the open prompt, which keeps its text")
        await pilot.press("escape")
        await pilot.pause()
        check(not isinstance(app.screen, tui.AskScreen), "Esc closes the prompt")

    # m adds or removes the reviewed label through a fake bd call that edits a private copy of the rows.
    rows, calls = copy.deepcopy(ROWS), []

    def fake_set_reviewed(bid: str, reviewed: bool) -> None:
        calls.append((bid, reviewed))
        labels = next(r for r in rows if r["id"] == bid)["labels"]
        labels.append("reviewed") if reviewed else labels.remove("reviewed")

    app = tui.BeadBoard(load_rows=lambda: rows, load_sessions=None, set_reviewed=fake_set_reviewed)
    async with app.run_test(size=(200, 60)) as pilot:
        await settle(app, pilot)
        tree = app.query_one(tui.BeadTree)
        tree.move_cursor(bead_nodes(app)["ent-aaa.1"])
        await pilot.press("m")
        await settle(app, pilot)
        check(calls == [], "m on an open bead changes no label")
        await pilot.press("c")
        await settle(app, pilot)
        tree.move_cursor(bead_nodes(app)["ent-aaa.5"])
        await pilot.pause()
        body = plain(app.detail)
        check("Follow-ups  1 unfiled" in body and re.search(r"\.4\s+load test at full size", body) is not None
              and re.search(r"⚑ unfiled\s+a doc for the export", body) is not None,
              "the detail lists follow-ups, with the filed one's id and the unfiled one marked")
        await pilot.press("m")
        await settle(app, pilot)
        check(calls == [("ent-aaa.5", True)] and not app.beads["ent-aaa.5"]["awaiting"],
              "m on a result to review adds the reviewed label, and the reload shows it reviewed")
        await pilot.press("n")
        await settle(app, pilot)
        # Before the review .2, .5, .7 and .9 need Ben; after it, the three open ones.
        check("ent-aaa.5" not in bead_nodes(app) and " 3 need you" in app.title_text.plain,
              f"a reviewed result leaves n and the need-you count, because review answers its question: {app.title_text.plain!r}")
        await pilot.press("n")
        await settle(app, pilot)
        tree.move_cursor(bead_nodes(app)["ent-aaa.5"])
        await pilot.press("m")
        await settle(app, pilot)
        check(calls[-1] == ("ent-aaa.5", False) and app.beads["ent-aaa.5"]["awaiting"],
              "m on a reviewed result removes the label again")


def fake_bd(rows: list[dict], calls: list) -> Callable[[list[str]], None]:
    """A stand-in for bd that applies close, reopen, defer, undefer, notes and labels to a private copy of the rows.

    A defer until "notadate" fails the way bd 1.3.4 does, so the board's failure path runs too.
    """
    def run(args: list[str]) -> None:
        calls.append(args)
        if args[0] == "label":
            op, bid, name = args[1:]
            labels = next(r for r in rows if r["id"] == bid)["labels"]
            if op == "add" and name not in labels:
                labels.append(name)
            elif op == "remove" and name in labels:
                labels.remove(name)
            return
        verb, bid = args[:2]
        opts = dict(a[2:].split("=", 1) for a in args[2:])
        row = next(r for r in rows if r["id"] == bid)
        note = opts.get("reason") if verb == "defer" else opts.get("append-notes")
        if verb == "defer" and opts.get("until") == "notadate":
            raise subprocess.CalledProcessError(1, ["bd", *args], stderr='Error: invalid --until format "notadate"')
        if verb == "close":
            row.update(status="closed", close_reason=opts["reason"])
        elif verb == "reopen":
            row.update(status="open", close_reason=None)
        elif verb == "defer":
            row.update(status="deferred", defer_until="2026-12-01T12:00:00Z" if "until" in opts else None)
        elif verb == "undefer":
            row.update(status="open", defer_until=None)
        if note:
            row["notes"] = (row.get("notes") + "\n" if row.get("notes") else "") + note
    return run


async def check_actions() -> None:
    """Shift-C and Shift-D ask in a prompt, then write the right bd calls; refusals and failures write nothing wrong."""
    rows, calls = copy.deepcopy(ROWS) + [bead("ent-ccc.1", dependencies=[child("ent-ccc")])], []
    row = lambda bid: next(r for r in rows if r["id"] == bid)
    today = time.strftime("%Y-%m-%d")
    app = tui.BeadBoard(load_rows=lambda: copy.deepcopy(rows), load_sessions=None, run_bd=fake_bd(rows, calls), refresh_s=0)
    async with app.run_test(size=(200, 60)) as pilot:
        await settle(app, pilot)
        tree = app.query_one(tui.BeadTree)
        prompt = lambda: isinstance(app.screen, tui.ActScreen)
        field = lambda name: app.screen.query_one(f"#act-{name}")

        # C on a bead with an open child, on an epic row, and D on a closed bead open no prompt.
        tree.move_cursor(bead_nodes(app)["ent-ccc"])
        await pilot.press("C")
        await pilot.pause()
        epic = next(n for n in app.walk(tree.root) if n.data and n.data[:2] == ("epic", "ent-aaa"))
        tree.move_cursor(epic)
        await pilot.press("C", "D")
        await pilot.pause()
        check(not prompt() and calls == [], "C on a bead with an open child or on an epic row, and D on an epic row, open no prompt")

        # C on an open bead: an empty result keeps the prompt open, letters stay in the box, and Esc writes nothing.
        seen = [n.data[1] for n in app.walk(tree.root) if n.data and n.data[0] == "bead" and all(p.is_expanded for p in app.ancestors(n))]
        after = seen[seen.index("ent-aaa.4") + 1]
        tree.move_cursor(bead_nodes(app)["ent-aaa.4"])
        await pilot.press("C")
        await pilot.pause()
        check(prompt() and app.screen.act == "close" and not banned_bindings(app),
              "C on an open bead opens the close prompt, and no binding uses Ctrl-h/j/k/l with it open")
        await pilot.press("enter")
        await pilot.pause()
        check(prompt() and "Type the result first" in str(app.screen.query_one("#act-error").content) and calls == [],
              "an empty result keeps the close prompt open and calls no bd")
        await pilot.press(*"Done: CD export")
        app.set_sessions({"ent-aaa.2": "working"})
        await pilot.resize_terminal(180, 50)
        await pilot.pause()
        check(prompt() and field("reason").value == "Done: CD export" and tree.cursor_node.data == ("bead", "ent-aaa.4"),
              "C and D typed in the prompt stay in it, and a session poll and a resize under it keep the text and the cursor")
        await pilot.press("escape")
        await pilot.pause()
        check(not prompt() and calls == [], "Esc closes the prompt and calls no bd")

        await pilot.press("C")
        await pilot.pause()
        await pilot.press(*"Done: CD export", "enter")
        await settle(app, pilot)
        check(calls == [["close", "ent-aaa.4", "--reason=Done: CD export"], ["label", "add", "ent-aaa.4", "reviewed"]],
              f"Enter closes the bead with the typed result and marks it reviewed: {calls}")
        check("ent-aaa.4" not in bead_nodes(app) and tree.cursor_node.data == ("bead", after) and after != seen[0],
              f"the closed bead leaves the open list, and the cursor moves to the bead after it, {after}")

        # C on the closed bead reopens it, removes the review mark and keeps a typed reason as a dated note.
        calls.clear()
        await pilot.press("c")
        await settle(app, pilot)
        tree.move_cursor(bead_nodes(app)["ent-aaa.4"])
        await pilot.press("C")
        await pilot.pause()
        await pilot.press(*"not done", "enter")
        await settle(app, pilot)
        check(calls == [["reopen", "ent-aaa.4"], ["label", "remove", "ent-aaa.4", "reviewed"],
                        ["update", "ent-aaa.4", f"--append-notes={today}: Reopened: not done"]]
              and app.beads["ent-aaa.4"]["status"] == "open" and "reviewed" not in app.beads["ent-aaa.4"]["labels"],
              f"C on a closed bead reopens it, removes reviewed and dates the reason: {calls}")

        # D defers: Enter moves from Until to Why, a failed bd call keeps the text, and D again brings it back.
        calls.clear()
        await pilot.press("o")
        await settle(app, pilot)
        tree.move_cursor(bead_nodes(app)["ent-aaa.2"])
        await pilot.press("D")
        await pilot.pause()
        await pilot.press(*"notadate", "enter")
        await pilot.pause()
        check(prompt() and app.screen.focused is field("why"), "Enter in the Until field moves to the Why field")
        await pilot.press(*"waits on the API key", "enter")
        await settle(app, pilot)
        check(not prompt() and app.beads["ent-aaa.2"]["status"] == "open" and app.drafts,
              "a defer that bd refuses leaves the bead open and keeps what was typed")
        tree.move_cursor(bead_nodes(app)["ent-aaa.2"])
        await pilot.press("D")
        await pilot.pause()
        check(prompt() and field("until").value == "notadate" and field("why").value == "waits on the API key",
              "D again opens the prompt with the text of the failed defer")
        field("until").value = "2026-12-01"
        field("why").focus()
        await pilot.press("enter")
        await settle(app, pilot)
        check(calls[-1] == ["defer", "ent-aaa.2", "--until=2026-12-01", f"--reason={today}: Deferred: waits on the API key"]
              and not app.drafts and "ent-aaa.2" not in bead_nodes(app),
              f"Enter on the Why field defers the bead until the date, with the reason as a dated note: {calls[-1]}")
        await pilot.press("d")
        await settle(app, pilot)
        tree.move_cursor(bead_nodes(app)["ent-aaa.2"])
        await pilot.pause()
        body = plain(app.detail)
        check("deferred until 2026-12-01" in body and "Latest " + today in body and "Deferred: waits on the API key" in body,
              "the deferred list shows the bead with its date and its reason as the Latest note")

        # D on the deferred bead undefers it, and an empty reason writes no note.
        calls.clear()
        await pilot.press("D")
        await pilot.pause()
        await pilot.press("enter")
        await settle(app, pilot)
        check(calls == [["undefer", "ent-aaa.2"]] and app.beads["ent-aaa.2"]["status"] == "open",
              f"D on a deferred bead undefers it, and an empty reason writes no note: {calls}")
    check(len(tui.wrap_keys([[(k, w, False) for k, w in tui.KEYS]], 93)) == 1,
          "the key strip's second row still fits one line of a 95-column pane")


def check_bd_scratch() -> None:
    """Run the four writes through the real bd on a throwaway database, because a fake bd cannot catch a wrong flag."""
    with tempfile.TemporaryDirectory() as d:
        env = {**os.environ, "BEADS_DIR": os.path.join(d, ".beads")}  # passed on every call, so the real database is never touched

        def run(args: list[str]) -> None:
            subprocess.run(["bd", *args], cwd=d, env=env, capture_output=True, text=True, timeout=60, check=True)

        def show(bid: str) -> dict:
            out = subprocess.run(["bd", "show", bid, "--json"], cwd=d, env=env, capture_output=True, text=True, check=True)
            return json.loads(out.stdout)[0]

        subprocess.run(["git", "init", "-q"], cwd=d, check=True)
        run(["init", "--prefix", "tst", "-q"])
        bid = subprocess.run(["bd", "create", "Scratch bead", "-p", "2", "--silent"], cwd=d, env=env,
                             capture_output=True, text=True, check=True).stdout.strip()
        today = time.strftime("%Y-%m-%d")
        bm.close_bead(bid, "-Done: a reason that starts with a dash", run)
        s = show(bid)
        check(s["status"] == "closed" and s.get("close_reason") == "-Done: a reason that starts with a dash"
              and "reviewed" in (s.get("labels") or []), f"bd: close_bead closes with the reason and adds reviewed: {s.get('close_reason')!r}")
        bm.reopen_bead(bid, "not done", run)
        s = show(bid)
        check(s["status"] == "open" and "reviewed" not in (s.get("labels") or [])
              and (s.get("notes") or "").endswith(f"{today}: Reopened: not done"),
              "bd: reopen_bead reopens, removes reviewed and appends the dated reason")
        bm.defer_bead(bid, "+2w", "waits on the API key", run)
        s = show(bid)
        check(s["status"] == "deferred" and s.get("defer_until") and s["notes"].endswith(f"\n{today}: Deferred: waits on the API key"),
              f"bd: defer_bead defers until a date and appends the dated reason on its own line: {s.get('defer_until')}")
        bm.undefer_bead(bid, "", run)
        s = show(bid)
        check(s["status"] == "open" and not s.get("defer_until") and s["notes"].endswith("Deferred: waits on the API key"),
              "bd: undefer_bead reopens the bead, and an empty reason writes no note")
        try:
            bm.defer_bead(bid, "notadate", "", run)
            refused = False
        except subprocess.CalledProcessError as e:
            refused = "invalid --until" in e.stderr
        check(refused, "bd: a date bd cannot read fails with bd's own message")


async def check_refresh() -> None:
    """The timer rereads the beads: a change rebuilds the tree in place, and no change rebuilds nothing."""
    rows, loads, fail, gate = copy.deepcopy(ROWS), [], [], threading.Event()
    gate.set()

    def load() -> list[dict]:
        loads.append(1)
        gate.wait(5)
        if fail:
            fail.pop()
            raise subprocess.CalledProcessError(1, ["bd", "export"], stderr="database is locked")
        return copy.deepcopy(rows)

    def rename(bid: str) -> str:
        title = f"{bid} renamed by another session"
        next(r for r in rows if r["id"] == bid)["title"] = title
        return title

    app = tui.BeadBoard(load_rows=load, load_sessions=None, refresh_s=0.1)
    async with app.run_test(size=(120, 18)) as pilot:
        await settle(app, pilot)
        tree, detail = app.query_one(tui.BeadTree), app.query_one("#detail")
        # The last bead under the open area, so the tree has scrolled to show it.
        last = [n for n in app.walk(tree.root) if n.data and n.data[0] == "bead" and all(p.is_expanded for p in app.ancestors(n))][-1]
        tree.move_cursor(last)
        await pilot.pause()
        detail.scroll_to(y=3, animate=False)
        await pilot.pause()
        before = (tree.cursor_node.data, tree.scroll_y, detail.scroll_y)
        title = rename("ent-aaa.2")
        changed = await until(pilot, lambda: title in bead_nodes(app)["ent-aaa.2"].label.plain)
        await pilot.pause()
        after = (tree.cursor_node.data, tree.scroll_y, detail.scroll_y)
        check(changed and before[1] > 0 and before[2] > 0 and after == before,
              f"a reread that changes another row rebuilds the tree, keeping the cursor and both scroll positions: {before} -> {after}")

        epic = next(n for n in app.walk(tree.root) if n.data and n.data[:2] == ("epic", "ent-aaa"))
        tree.move_cursor(epic)
        await pilot.pause()
        title = rename("ent-aaa.3")
        changed = await until(pilot, lambda: title in bead_nodes(app)["ent-aaa.3"].label.plain)
        await pilot.pause()
        check(changed and tree.cursor_node.data[:2] == ("epic", "ent-aaa"), "a rebuild keeps the cursor on an epic row")

        root, read, n = tree.root, app.read_at, len(loads)
        quiet = await until(pilot, lambda: len(loads) >= n + 2 and app.read_at > read)
        check(quiet and tree.root is root, "a reread that finds no change moves the read time and rebuilds nothing")

        fail.append(1)
        failed = await until(pilot, lambda: "database is locked" in app.error)
        cleared = await until(pilot, lambda: not app.error)
        check(failed and cleared and tree.root is root, "a failed reread shows its error, and the next good one clears it")

        gate.clear()
        n = len(loads)
        await until(pilot, lambda: len(loads) == n + 1)
        await pilot.pause(0.4)  # about four ticks while the export hangs
        started = len(loads) - n
        gate.set()
        await settle(app, pilot)
        check(started == 1, f"ticks while an export hangs start no second export ({started} started)")


async def main() -> int:
    await check_synthetic()
    await check_actions()
    await check_refresh()
    if "--bd" in sys.argv:
        check_bd_scratch()
    if "--live" in sys.argv:
        bm.default_beads_dir()
        await check_lists(bm.export_beads(), [], None, "live export")
    print(f"\n{len(failures)} failed" if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
