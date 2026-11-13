#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["textual==8.2.8"]
# ///
"""Check bd-tui headless on a synthetic bead export, and optionally on the live one.

  ./test_bd_tui.py          the synthetic export below; exits 1 on any failed check
  ./test_bd_tui.py --live   also checks the Now tree on today's `bd export` (needs BEADS_DIR)

Runs from any directory with no venv, because uv installs Textual from the script header.
The fixture is synthetic, so that the repo holds no real beads.
"""
import asyncio
import copy
import importlib.machinery
import importlib.util
import io
import os
import re
import sys

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
    bead("ent-aaa.10", issue_type="epic", title="Nested epic", labels=["area:alpha"], dependencies=[child("ent-aaa")]),
    bead("ent-aaa.10.1", status="in_progress", labels=["area:alpha"], dependencies=[child("ent-aaa.10")]),
    bead("ent-bbb", issue_type="epic", status="in_progress", title="Epic with no area"),
    bead("ent-bbb.1", status="in_progress", dependencies=[child("ent-bbb")]),
    bead("ent-ccc", status="in_progress", labels=["area:alpha"]),
]
NOW = {"ent-aaa.1", "ent-aaa.2", "ent-aaa.3", "ent-aaa.7", "ent-aaa.9", "ent-aaa.10.1", "ent-bbb.1", "ent-ccc"}
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


def banned_bindings(app) -> set:
    """Every key that a binding on the focused chain or any mounted widget answers, intersected with the ban."""
    keys = set(app.active_bindings)
    for screen in app.screen_stack:
        for node in screen.walk_children(with_self=True):
            keys |= set(node._bindings.key_to_bindings)
    keys |= set(app._bindings.key_to_bindings)
    return keys & tui.BANNED


async def check_now_tree(rows, sessions, expected: set | None, label: str) -> None:
    app = tui.BeadBoard(load_rows=lambda: rows, load_sessions=lambda: sessions)
    async with app.run_test(size=(200, 60)) as pilot:
        await settle(app, pilot)
        beads = app.beads
        rule = {b["id"] for b in beads.values() if bm.needs_ben_now(b)}
        nodes = bead_nodes(app)
        if expected is not None:
            check(rule == expected, f"{label}: the shared rule picks the hand-listed Now beads")
        check(set(nodes) == rule, f"{label}: the Now tree lists exactly the rule's {len(rule)} beads (tree has {len(nodes)})")
        check(all(beads[i]["status"] != "closed" for i in nodes), f"{label}: no closed bead in the Now tree")
        check(all(beads[i].get("issue_type") != "epic" for i in nodes), f"{label}: no epic as a bead row")
        grouped = all(n.parent.data[1] == bm.epic_of(beads, i)
                      and n.parent.parent.data[1] == (bm.area_of(beads, i) or tui.NO_AREA) for i, n in nodes.items())
        check(grouped, f"{label}: every bead sits under its top epic, under that epic's area")
        if expected is None:
            # Every bead's detail, every epic's and every thread render on the real export, closed ones too.
            broken = []
            for bid, b in beads.items():
                try:
                    plain(app.epic_text(bid) if b.get("issue_type") == "epic" else app.bead_text(bid))
                    plain(app.thread_text(bm.epic_of(beads, bid)))
                except Exception as e:  # noqa: BLE001 - report every bead that fails, not the first
                    broken.append(f"{bid}: {type(e).__name__} {e}")
            check(not broken, f"{label}: the detail and thread of all {len(beads)} beads render" + "".join(f"\n     {x}" for x in broken[:5]))
            await pilot.press("o", "c", "v")
            await settle(app, pilot)
            check(len(bead_nodes(app)) == sum(1 for b in beads.values() if b.get("issue_type") != "epic"),
                  f"{label}: o, c and v together list every bead that is not an epic")
            print(f"     {len(rule)} beads: {sum(beads[i]['status'] == 'in_progress' for i in rule)} in progress, "
                  f"{sum(beads[i]['needsYou'] for i in rule)} need Ben")


async def check_synthetic() -> None:
    await check_now_tree(ROWS, SESSIONS, NOW, "synthetic")
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

        # g swaps the detail for the thread in dependency order.
        await pilot.press("g")
        body = plain(app.detail)
        check(body.startswith("Thread ent-aaa") and body.find(".1 ") < body.find(".9 ") and body.find(".4 ") < body.find(".8 "),
              "g shows the thread with each bead after the beads that block it")
        await pilot.press("g")

        # o, c, v and p change what the tree lists.
        await pilot.press("c")
        await settle(app, pilot)
        check({"ent-aaa.5", "ent-aaa.6"} <= set(bead_nodes(app)), "c adds closed beads")
        await pilot.press("c", "v")
        await settle(app, pilot)
        shown = set(bead_nodes(app))
        check("ent-aaa.5" in shown and "ent-aaa.6" not in shown, "v adds unreviewed results only")
        await pilot.press("v", "o")
        await settle(app, pilot)
        check({"ent-aaa.4", "ent-aaa.8"} <= set(bead_nodes(app)), "o adds every open bead")
        await pilot.press("o", "p")
        await settle(app, pilot)
        check(set(bead_nodes(app)) == {"ent-aaa.1", "ent-aaa.9"}, "p keeps only P0 beads")
        await pilot.press("p")
        await settle(app, pilot)

        # / searches, and letters typed into the box never reach the tree's keys.
        check(not banned_bindings(app), "no binding uses Ctrl-h/j/k/l with the tree focused")
        await pilot.press("slash", *"bbb")
        await settle(app, pilot)
        check(set(bead_nodes(app)) == {"ent-bbb.1"}, "/ narrows the tree to matching beads")
        check(app.query_one("#search").value == "bbb", "letters typed in the search box stay in the box")
        check(not banned_bindings(app), "no binding uses Ctrl-h/j/k/l with the search box focused")
        await pilot.press("escape")
        await settle(app, pilot)
        check(set(bead_nodes(app)) == NOW, "Esc clears the search")

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
        tree.move_cursor(bead_nodes(app)["ent-aaa.5"])
        await pilot.press("m")
        await settle(app, pilot)
        check(calls[-1] == ("ent-aaa.5", False) and app.beads["ent-aaa.5"]["awaiting"],
              "m on a reviewed result removes the label again")


async def main() -> int:
    await check_synthetic()
    if "--live" in sys.argv:
        bm.default_beads_dir()
        await check_now_tree(bm.export_beads(), [], None, "live export")
    print(f"\n{len(failures)} failed" if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
