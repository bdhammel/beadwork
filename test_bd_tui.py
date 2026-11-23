#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["textual==8.2.8"]
# ///
"""Check bd-tui headless on a synthetic bead export, and optionally on the live one.

  ./test_bd_tui.py          the synthetic export below; exits 1 on any failed check
  ./test_bd_tui.py --live   also checks the open, now and closed lists on today's `bd export` (needs BEADS_DIR)
  ./test_bd_tui.py --bd     also moves a bead through every status change in a throwaway bd database

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
        marks = ["ent-aaa.9 · blocked · needs you · P0", "ben:plan · xhigh effort · kind:experiment",
                 "Summary", "One line.", "Needs You", " ? ",
                 "If skipped", " date ", "slips: ent-aaa.4",
                 " result ", " worst ", "fast small export reads as fast at every size", " priority ",
                 "P0, because the load test waits on it",
                 "Done when", "☐ three export sizes", "☑ p95 per size", "Thread ent-aaa · blocked by .1 · blocks –",
                 "Latest 2026-11-05", "Plan written"]
        where = [body.find(m) for m in marks]
        check(all(i >= 0 for i in where) and where == sorted(where),
              "the detail runs: why listed, run labels, Summary, Needs You, If skipped, Done when, thread, latest note")
        check("If skipped written" not in body, "the Latest line skips the bookkeeping note")
        check("Summary\nx\n\nGoal" in plain(app.epic_text("ent-aaa")), "an epic's detail shows its summary above its goal")
        stand_in = bm.enrich([bead("ent-x.1", description="## Summary\nThe export times\nevery size.\n\n## Context\nx"),
                              bead("ent-x.2", description="## Objective\nEvery export\nis timed."),
                              bead("ent-x.3", description=""),
                              bead("ent-x.4", description="Export times\nby size:\n| rows | s |\n| 100k | 2 |")])
        shown = {bid: "".join(plain(p) for p in tui.BeadBoard.summary_part(b)) for bid, b in stand_in.items()}
        check("Summary\nThe export times every size.\n" in shown["ent-x.1"]
              and "Summary  from ## Objective\nEvery export is timed.\n" in shown["ent-x.2"]
              and "no ## Summary section" in shown["ent-x.3"]
              and "Summary  from the first paragraph\nExport times by size:\n| rows | s |\n| 100k | 2 |\n" in shown["ent-x.4"],
              f"the summary shows with its hard wraps joined, and a stand-in section says where it came from: {shown}")
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

        # An epic row measures its P0 beads: under ent-aaa, .1 is in progress, .4 ready and .9 blocked, so 0 of 3 are done.
        # Then closed of all its beads but the deferred .11: .5 and .6 of ten, so open beads below P0 show too.
        rows = {n.data[1]: n.label.plain for n in app.walk(tree.root) if n.data and n.data[0] == "epic" and n.data[1]}
        check("▒▒▒░░░░░░░ 0/3 · 2/10" in rows["ent-aaa"] and "no P0 beads · 0/1" in rows["ent-bbb"]
              and "2/10 closed" in plain(app.epic_text("ent-aaa")) and "1 deferred" in plain(app.epic_text("ent-aaa")),
              f"an epic row shows a P0 bar (done █, in progress ▒, other ░) and count, then closed of every bead not deferred, "
              f"as its detail does: {rows['ent-aaa'][-30:]!r}")
        check(tui.split_cells([19, 0, 1]) == [9, 0, 1] and tui.split_cells([1, 1, 7]) == [1, 1, 8]
              and tui.split_cells([3, 0, 0]) == [10, 0, 0],
              "a P0 bead still open never rounds the bar up to full, and one bead in progress always gets a cell")
        p0 = plain(app.epic_text("ent-aaa")).split("Open P0 beads", 1)[-1]
        check(p0.startswith("  3 of 3") and p0.find(".1 ") < p0.find(".4 ") < p0.find(".9 ")
              and re.search(r"\.9\s.*waits on \.1", p0) is not None,
              "the epic detail lists its open P0 beads in dependency order, with what each waits on")

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
        check(calls[-1] == ("ent-aaa.5", False) and app.beads["ent-aaa.5"]["awaiting"] and not app.marks,
              "m on a reviewed result removes the label again, and m marks no row as changed")


async def check_label_picker() -> None:
    """Shift-L narrows the list to one label picked from a menu, whose counts match the list, and Shift-L removes it."""
    app = tui.BeadBoard(load_rows=lambda: copy.deepcopy(ROWS), load_sessions=None, refresh_s=0)
    async with app.run_test(size=(95, 40)) as pilot:
        await settle(app, pilot)

        def menu() -> list[tuple[str, str]]:
            options = app.screen.query_one("#label-menu").options
            return [(o.id, " ".join(str(o.prompt).split())) for o in options]

        app.expanded = set()  # every area folded, so a pick must open the areas its beads sit in
        app.rebuild()
        await pilot.press("L")
        await pilot.pause()
        check(isinstance(app.screen, tui.LabelScreen) and [i for i, _ in menu()] ==
              ["ben:plan", "kind:decision", "kind:experiment", "needs:ben", "area:alpha"],
              f"Shift-L lists the open list's labels, run-label families first and area last: {menu()}")
        await pilot.press(*"dec")
        await pilot.pause()
        check(menu() == [("kind:decision", "2 kind:decision")], f"typing narrows the menu to matching labels: {menu()}")
        await pilot.press("enter")
        await settle(app, pilot)
        nodes = bead_nodes(app)
        shown = {b for b, n in nodes.items() if all(p.is_expanded for p in app.ancestors(n))}
        check(not isinstance(app.screen, tui.LabelScreen) and set(nodes) == shown == {"ent-aaa.7", "ent-aaa.8"}
              and "Open 10 › kind:decision 2" in app.title_text.plain and "L kind:decision" in app.keys_text.plain,
              f"Enter keeps the two beads with the label, open in a folded area, as a step in the title: {app.title_text.plain!r}")
        await pilot.press("L")
        await settle(app, pilot)
        check(set(bead_nodes(app)) == OPEN and "kind:decision" not in app.title_text.plain,
              "Shift-L again removes the label")

        await pilot.press("n", "L")
        await pilot.pause()
        check(("kind:decision", "1 kind:decision") in menu(),
              f"the menu counts labels over the narrowed list, so .8, blocked, drops out under n: {menu()}")
        await pilot.press(*"zzz", "enter")
        await pilot.pause()
        check(isinstance(app.screen, tui.LabelScreen) and "No label contains" in menu()[0][1],
              "a text that matches no label says so, and Enter picks nothing")
        await pilot.press("escape")
        await settle(app, pilot)
        check(not isinstance(app.screen, tui.LabelScreen) and app.label_on == "" and set(bead_nodes(app)) == NOW,
              "Esc closes the menu and leaves the list as it was")
        await pilot.press("L")
        await pilot.pause()
        await pilot.press("down", "ctrl+n", "ctrl+p", "enter")
        await settle(app, pilot)
        check(app.label_on == "kind:decision" and set(bead_nodes(app)) == {"ent-aaa.7"},
              f"down and Ctrl-n move the menu and Ctrl-p moves it back, so Enter picks the second label: {app.label_on!r}")


def fake_bd(rows: list[dict], calls: list) -> Callable[[list[str]], None]:
    """A stand-in for bd that applies close, reopen, defer, undefer, claims, status, notes and labels to the rows.

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
        opts = {k: v for k, _, v in (a[2:].partition("=") for a in args[2:])}
        row = next(r for r in rows if r["id"] == bid)
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
        elif "claim" in opts:
            row.update(status="in_progress", assignee="ben")
        elif verb == "update":
            row.update({k: v or None for k, v in opts.items() if k in ("status", "assignee")})
        note = opts.get("reason") if verb == "defer" else opts.get("append-notes")
        if note:
            row["notes"] = (row.get("notes") + "\n" if row.get("notes") else "") + note
    return run


async def check_status_menu() -> None:
    """Shift-S opens the status menu, and each status writes the right bd calls; refusals and failures write nothing wrong."""
    rows, calls = copy.deepcopy(ROWS) + [bead("ent-ccc.1", dependencies=[child("ent-ccc")])], []
    today = time.strftime("%Y-%m-%d")
    app = tui.BeadBoard(load_rows=lambda: copy.deepcopy(rows), load_sessions=None, run_bd=fake_bd(rows, calls), refresh_s=0)
    async with app.run_test(size=(200, 60)) as pilot:
        await settle(app, pilot)
        tree = app.query_one(tui.BeadTree)
        menu = lambda: isinstance(app.screen, tui.StatusScreen)
        prompt = lambda: isinstance(app.screen, tui.ActScreen)
        field = lambda name: app.screen.query_one(f"#act-{name}")

        async def press(*keys: str) -> None:
            """Each key, then a pause, because a letter sent before the menu or prompt takes focus reaches the tree."""
            for key in keys:
                await pilot.press(key)
                await pilot.pause()

        async def on(bid: str, *keys: str) -> None:
            """S on a bead, then the keys, then wait for any bd write to finish and the board to reread."""
            tree.move_cursor(bead_nodes(app)[bid])
            await press("S", *keys)
            await settle(app, pilot)

        # S on an epic row opens no menu. On a working bead with an open child, the menu greys out in progress and closed.
        tree.move_cursor(next(n for n in app.walk(tree.root) if n.data and n.data[:2] == ("epic", "ent-aaa")))
        await press("S")
        check(not menu() and calls == [], "S on an epic row opens no menu")
        await on("ent-ccc")
        greyed = [s for s in bm.STATUSES if app.screen.query_one(tui.MenuList).get_option(s).disabled] if menu() else None
        check(greyed == ["in_progress", "closed"] and not banned_bindings(app),
              f"the menu greys out the current status, and closed while the bead has an open child: {greyed}")
        await press("c", "w", "escape")
        check(not menu() and calls == [], "the letters of greyed-out rows pick nothing, and Esc closes the menu without a bd call")

        # c on an open bead opens the close prompt: an empty result keeps it open, and a rebuild under it keeps text and cursor.
        seen = [n.data[1] for n in app.walk(tree.root) if n.data and n.data[0] == "bead" and all(p.is_expanded for p in app.ancestors(n))]
        after = seen[seen.index("ent-aaa.4") + 1]
        await on("ent-aaa.4", "c", "enter")
        check(prompt() and app.screen.target == "closed" and "Type the result first" in str(app.screen.query_one("#act-error").content)
              and not banned_bindings(app) and calls == [],
              "c opens the close prompt, where an empty result keeps it open and calls no bd")
        await pilot.press(*"Done: So we close it")
        app.set_sessions({"ent-aaa.2": "working"})
        await pilot.resize_terminal(180, 50)
        await pilot.pause()
        check(prompt() and field("reason").value == "Done: So we close it" and tree.cursor_node.data == ("bead", "ent-aaa.4"),
              "letters typed in the prompt stay in it, and a session poll and a resize under it keep the text and the cursor")
        await press("enter")
        await settle(app, pilot)
        check(calls == [["close", "ent-aaa.4", "--reason=Done: So we close it"], ["label", "add", "ent-aaa.4", "reviewed"]],
              f"Enter closes the bead with the typed result and marks it reviewed: {calls}")
        check("ent-aaa.4" not in bead_nodes(app) and tree.cursor_node.data == ("bead", after) and after != seen[0],
              f"the closed bead leaves the open list, and the cursor moves to the bead after it, {after}")

        # o on a closed bead reopens it and removes the review mark. Enter on the first open row claims it, and o releases it.
        calls.clear()
        await press("c")
        await on("ent-aaa.4", "o")
        reopened = calls[:]
        await press("o")
        await on("ent-aaa.4", "enter")
        claimed = app.beads["ent-aaa.4"]["status"]
        await on("ent-aaa.4", "o")
        check(reopened == [["reopen", "ent-aaa.4"], ["label", "remove", "ent-aaa.4", "reviewed"]]
              and calls[2:] == [["update", "ent-aaa.4", "--claim"], ["update", "ent-aaa.4", "--status=open", "--assignee="]]
              and claimed == "in_progress" and app.beads["ent-aaa.4"]["status"] == "open",
              f"o reopens a closed bead without its review mark, Enter on the first row claims it, and o releases it: {calls}")
        check(not app.marks, f"the status changes Ben makes from the board mark no row as changed: {sorted(app.marks)}")

        # j and k move the menu, and Enter on deferred opens the defer prompt. A failed defer keeps the text for the next try.
        calls.clear()
        await on("ent-aaa.2", "j", "k", "j", "enter")
        check(prompt() and app.screen.target == "deferred", "j and k move the menu, and Enter on deferred opens the defer prompt")
        await press(*"notadate", "enter")
        check(prompt() and app.screen.focused is field("why"), "Enter in the Until field moves to the Why field")
        await pilot.press(*"waits on the API key", "enter")
        await settle(app, pilot)
        check(not prompt() and app.beads["ent-aaa.2"]["status"] == "open" and app.drafts,
              "a defer that bd refuses leaves the bead open and keeps what was typed")
        await on("ent-aaa.2", "d")
        check(prompt() and field("until").value == "notadate" and field("why").value == "waits on the API key",
              "S and d again open the prompt with the text of the failed defer")
        field("until").value = "2026-12-01"
        field("why").focus()
        await press("enter")
        await settle(app, pilot)
        check(calls[-1] == ["defer", "ent-aaa.2", "--until=2026-12-01", f"--reason={today}: Deferred: waits on the API key"]
              and not app.drafts and "ent-aaa.2" not in bead_nodes(app),
              f"the defer runs with the date, and the reason as a dated note: {calls[-1]}")
        await press("d")
        tree.move_cursor(bead_nodes(app)["ent-aaa.2"])
        await pilot.pause()
        body = plain(app.detail)
        check("deferred until 2026-12-01" in body and "Latest " + today in body and "Deferred: waits on the API key" in body,
              "the deferred list shows the bead with its return date and its reason as the Latest note")

        # w on a deferred bead undefers it before it claims it, so no return date is left behind.
        calls.clear()
        await on("ent-aaa.2", "w")
        check(calls == [["undefer", "ent-aaa.2"], ["update", "ent-aaa.2", "--claim"]] and app.beads["ent-aaa.2"]["status"] == "in_progress",
              f"w on a deferred bead undefers it, then claims it: {calls}")
    check(len(tui.wrap_keys([[(k, w, False) for k, w in tui.KEYS]], 93)) == 1,
          "the key strip's second row still fits one line of a 95-column pane")


def check_bd_scratch() -> None:
    """Move one bead through every status change on the real bd and a throwaway database, because a fake bd cannot catch a wrong flag."""
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
        reviewed = lambda s: "reviewed" in (s.get("labels") or [])
        # Each move, from the bead's status at that point, with what bd must show after it.
        moves = [
            ("open", "in_progress", {}, lambda s: s["status"] == "in_progress" and s.get("assignee")),
            ("in_progress", "closed", {"reason": "-Done: a reason that starts with a dash"},
             lambda s: s["status"] == "closed" and s.get("close_reason") == "-Done: a reason that starts with a dash" and reviewed(s)),
            ("closed", "in_progress", {}, lambda s: s["status"] == "in_progress" and not reviewed(s) and s.get("assignee")),
            ("in_progress", "deferred", {"until": "+2w", "why": "waits on the API key"},
             lambda s: s["status"] == "deferred" and s.get("defer_until") and not s.get("assignee")
             and s["notes"].endswith(f"{today}: Deferred: waits on the API key")),
            ("deferred", "open", {}, lambda s: s["status"] == "open" and not s.get("defer_until")),
            ("open", "deferred", {}, lambda s: s["status"] == "deferred" and not s.get("defer_until")),
            ("deferred", "closed", {"reason": "Not needed"}, lambda s: s["status"] == "closed" and reviewed(s)),
            ("closed", "open", {}, lambda s: s["status"] == "open" and not reviewed(s) and not s.get("close_reason")),
        ]
        wrong = []
        for current, target, values, ok in moves:
            bm.set_status(bid, current, target, values, run)
            s = show(bid)
            if not ok(s):
                wrong.append(f"{current} -> {target}: {({k: s.get(k) for k in ('status', 'assignee', 'defer_until', 'labels')})}")
        check(not wrong, "bd: set_status makes all eight moves, with labels, claims and return dates right"
              + "".join(f"\n     {w}" for w in wrong))
        try:
            bm.set_status(bid, "open", "deferred", {"until": "notadate"}, run)
            refused = False
        except subprocess.CalledProcessError as e:
            refused = "invalid --until" in e.stderr
        check(refused and show(bid)["status"] == "open", "bd: a date bd cannot read fails with bd's own message and leaves the bead open")


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

        mark = tui.CHANGE_MARK
        check(set(app.marks) == {"ent-aaa.2"} and mark in bead_nodes(app)["ent-aaa.2"].label.plain
              and f"{mark} 1 changed" in app.title_text.plain,
              f"the reread marks only the renamed row, which the title counts, and the first read marked none: {sorted(app.marks)}")
        tree.move_cursor(bead_nodes(app)["ent-aaa.2"])
        await pilot.pause()
        said = re.search(rf"{mark} changed \d\d:\d\d: title", plain(app.detail)) is not None
        tree.move_cursor(bead_nodes(app)["ent-aaa.1"])
        await pilot.pause()
        check(said and not app.marks and mark not in bead_nodes(app)["ent-aaa.2"].label.plain
              and "changed" not in app.title_text.plain,
              "the detail names the changed field, and the mark clears when the cursor leaves the row")

        tree.move_cursor(bead_nodes(app)["ent-aaa.4"])
        await pilot.pause()
        beat = "2026-11-19T10:00:00Z"
        next(r for r in rows if r["id"] == "ent-aaa.4").update(status="in_progress", notes="2026-11-19: started", heartbeat_at=beat)
        next(r for r in rows if r["id"] == "ent-aaa.1")["heartbeat_at"] = beat
        marked = await until(pilot, lambda: "ent-aaa.4" in app.marks)
        await pilot.pause()
        said = f"{mark} changed" in plain(app.detail) and "open → in progress, note" in plain(app.detail)
        tree.move_cursor(bead_nodes(app)["ent-aaa.1"])
        await pilot.pause()
        kept = set(app.marks) == {"ent-aaa.4"}
        tree.move_cursor(bead_nodes(app)["ent-aaa.4"])
        await pilot.pause()
        tree.move_cursor(bead_nodes(app)["ent-aaa.1"])
        await pilot.pause()
        check(marked and said and kept and not app.marks,
              "a change under the cursor shows the status move, stays marked after the cursor leaves, and clears after "
              "the next visit; a heartbeat alone marks nothing")

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


def check_session_effort() -> None:
    """Record the claude command that s runs, because its --effort decides how hard the session reasons."""
    commands = []

    def fake_run(cmd, **kw):
        commands.append(cmd)
        out = json.dumps([bead("ent-aaa.1")]) if cmd[0] == "bd" else "backgrounded · 0123abcd · name\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    real, bm.subprocess.run = bm.subprocess.run, fake_run
    try:
        started = {}
        for mode in ("low", "", "ultrathink"):
            bm.launch_session("ent-aaa.1", "Do it.", mode)
            started[mode] = commands[-1]
    finally:
        bm.subprocess.run = real
    effort = {mode: cmd[cmd.index("--effort") + 1] for mode, cmd in started.items()}
    check(effort == {"low": "low", "": "xhigh", "ultrathink": "max"}
          and started["ultrathink"][-1].startswith("ultrathink\n") and started["low"][-1].startswith("This session"),
          "s starts a mode:low bead at --effort low, a mode:ultrathink bead at max with its keyword, and any other at xhigh")
    beads = bm.enrich([bead("ent-aaa.1", labels=["ben:none", "mode:low"]), bead("ent-aaa.2", labels=["ben:none"]),
                       bead("ent-aaa.3", labels=["ben:none", "mode:ultrathink"])])
    lines = {bid: plain(tui.BeadBoard.labels_line(b)) for bid, b in beads.items()}
    check([beads[i]["oneshot"] for i in beads] == [True, True, False]
          and "mode:low" in lines["ent-aaa.1"] and "xhigh effort" in lines["ent-aaa.2"]
          and "xhigh effort" not in lines["ent-aaa.1"],
          "a ben:none bead is a one-shot with no mode or mode:low, and a bead with no mode shows xhigh effort")


async def check_empty_start() -> None:
    """s with an empty line starts the bead at its mode's effort, and refuses where the rules forbid starting it.

    A fake subprocess.run records the bd and claude commands, so no session starts.
    """
    rows = copy.deepcopy(ROWS) + [bead("ent-aaa.12", status="deferred", labels=["area:alpha"], dependencies=[child("ent-aaa")])]
    extra = {"ent-aaa.1": ["ben:none", "mode:low"], "ent-aaa.2": ["ben:pair"], "ent-aaa.4": ["ben:review", "mode:ultracode"]}
    for r in rows:
        r["labels"] = r["labels"] + extra.get(r["id"], [])
    beads = bm.enrich(rows)
    refused = {bid for bid, b in beads.items() if bm.start_refusal(b)}
    check(refused == {"ent-aaa", "ent-aaa.10", "ent-bbb", "ent-aaa.5", "ent-aaa.6", "ent-aaa.12", "ent-aaa.7", "ent-aaa.8",
                      "ent-aaa.11"},
          f"an empty line refuses every epic, closed, deferred and decision bead, and starts the rest: {sorted(refused)}")

    commands, notes = [], []

    def fake_run(cmd, **kw):
        commands.append(cmd)
        out = json.dumps([bead(cmd[2])]) if cmd[0] == "bd" else "backgrounded · 0123abcd · name\n"
        return subprocess.CompletedProcess(cmd, 0, out, "")

    real, bm.subprocess.run = bm.subprocess.run, fake_run
    try:
        app = tui.BeadBoard(load_rows=lambda: rows, load_sessions=lambda: SESSIONS, refresh_s=0)
        app.notify = lambda message, **kw: notes.append(message)
        async with app.run_test(size=(200, 60)) as pilot:
            await settle(app, pilot)
            tree = app.query_one(tui.BeadTree)

            async def press_on(bid: str, *keys: str) -> None:
                tree.move_cursor(bead_nodes(app)[bid])
                await pilot.pause()
                await pilot.press(keys[0])
                await pilot.pause()
                await pilot.press(*keys[1:])
                await settle(app, pilot)

            def launched(since: int) -> tuple[str, str]:
                """The effort and prompt of the background claude command recorded after `since`, or two empty strings."""
                cmd = next((c for c in commands[since:] if c[0] == "claude"), [])
                return (cmd[cmd.index("--effort") + 1], cmd[-1]) if "--bg" in cmd and "--effort" in cmd else ("", "")

            tree.move_cursor(bead_nodes(app)["ent-aaa.1"])
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            lead = app.screen.lead if isinstance(app.screen, tui.AskScreen) else ""
            check("ben:none" in lead and "low reasoning effort" in lead and "is already working" in lead
                  and "empty line starts the bead" in lead,
                  "s's prompt names the ben: label and the effort, says an empty line starts the bead, and warns of a working session")
            before = len(commands)
            await pilot.press("enter")
            await settle(app, pilot)
            effort, prompt = launched(before)
            check(effort == "low" and prompt.startswith("This session") and prompt.endswith("\n\n" + bm.START_ASK),
                  "s and Enter on an empty line start a mode:low bead in the background at --effort low, with the start instruction")

            before = len(commands)
            await press_on("ent-aaa.4", "s", "enter")
            empty = launched(before)
            before = len(commands)
            await press_on("ent-aaa.4", "s", *"Use the dev split.", "enter")
            typed = launched(before)
            check(empty[0] == typed[0] == "xhigh" and empty[1].startswith("ultracode\n") and empty[1].endswith(bm.START_ASK)
                  and typed[1].endswith("\n\nUse the dev split.") and bm.START_ASK not in typed[1],
                  "on a mode:ultracode bead both start at xhigh with the keyword, and typed text replaces the start instruction")

            before = len(commands)
            tree.move_cursor(bead_nodes(app)["ent-aaa.2"])
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            lead = app.screen.lead if isinstance(app.screen, tui.AskScreen) else ""
            await pilot.press("enter")
            await settle(app, pilot)
            effort, prompt = launched(before)
            check("opening questions" in lead and effort == "xhigh" and prompt.endswith("\n\n" + bm.PAIR_ASK)
                  and "needs input:" in bm.PAIR_ASK,
                  "on a ben:pair bead an empty line starts a background session that asks its questions and waits as needs input")

            before = len(commands)
            await press_on("ent-aaa.3", "s", "escape")
            check(not isinstance(app.screen, tui.AskScreen) and len(commands) == before, "Esc in the s prompt starts nothing")
    finally:
        bm.subprocess.run = real


async def main() -> int:
    check_session_effort()
    await check_empty_start()
    await check_synthetic()
    await check_label_picker()
    await check_status_menu()
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
