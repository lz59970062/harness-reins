"""Tests for the Textual Reins panel (commands/tui/panel.py) against FakeBackend.

Skipped when textual is not installed. Run with a fresh HOME, never the real one:

    env -i HOME=$(mktemp -d) PATH=<python>:/usr/bin:/bin PYTHONPATH=src \
        python -m unittest discover -s tests -p test_tui_panel.py -v
"""
from __future__ import annotations

import asyncio
import os
import time
import threading
import unittest
from dataclasses import replace

try:
    import textual  # noqa: F401
    from textual import events
    from textual.widgets import DataTable, Input, OptionList, Static, TabbedContent

    from harness_reins.commands.tui import panel
    from harness_reins.commands.tui.backend import (
        FakeBackend,
        GoalRow,
        MessageRow,
        SessionRow,
        TaskRow,
    )
    from harness_reins.core import sessions

    HAVE_TEXTUAL = True
except ImportError:
    HAVE_TEXTUAL = False


SESSION_A = "aaaaaaaa-0000-4000-8000-000000000001"  # running, hosted by Reins
SESSION_B = "bbbbbbbb-0000-4000-8000-000000000002"  # foreign, open on the Desktop
SESSION_C = "cccccccc-0000-4000-8000-000000000003"  # closed
SESSION_D = "dddddddd-0000-4000-8000-000000000004"  # attached in a Reins pane
TASK_1 = "1a1a1a1a-0000-4000-8000-000000000001"   # running, from A, older
TASK_2 = "2b2b2b2b-0000-4000-8000-000000000002"   # running, from A, newest
TASK_3 = "3c3c3c3c-0000-4000-8000-000000000003"   # succeeded, from B, pending callback
CALLBACK_3 = "cbcbcbcb-0000-4000-8000-000000000003"
GOAL_1 = "9090190-0000-4000-8000-00000000000a"
MSG_1 = "m5m5m5m5-0000-4000-8000-00000000000b"


def make_backend(cls: type = None) -> "FakeBackend":
    home = os.path.expanduser("~")
    return (cls or FakeBackend)(
        session_rows=[
            SessionRow(
                session=SESSION_A, agent="claude", state=sessions.RUNNING,
                title="motion graphs", cwd="/home/u/motion",
                activity=sessions.WORKING, host=sessions.HOST_CLAUDE_BACKGROUND,
                tasks_running=2, callbacks_pending=1, started_at=1000.0,
            ),
            SessionRow(
                session=SESSION_B, agent="claude", state=sessions.FOREIGN,
                title="ppt draft", cwd="/home/u/ppt",
                activity=sessions.IDLE, host=sessions.HOST_DESKTOP, started_at=2000.0,
            ),
            SessionRow(
                session=SESSION_C, agent="claude", state=sessions.CLOSED,
                title="old callback", cwd="/home/u/old", started_at=500.0,
            ),
            SessionRow(
                session=SESSION_D, agent="claude", state=sessions.ATTACHED,
                title="attached one", cwd=os.path.join(home, "att"),
                activity=sessions.IDLE, host=sessions.HOST_CLAUDE_BACKGROUND,
                started_at=3000.0,
            ),
        ],
        task_rows=[
            TaskRow(task=TASK_1, title="build index", state="running",
                    session=SESSION_A, started_at=100.0),
            TaskRow(task=TASK_2, title="write tests", state="running",
                    session=SESSION_A, started_at=200.0),
            TaskRow(task=TASK_3, title="summarize", state="succeeded",
                    session=SESSION_B, started_at=50.0, ended_at=90.0,
                    callback=CALLBACK_3, callback_state="pending"),
        ],
        goal_rows=[
            GoalRow(goal=GOAL_1, session=SESSION_A, task=TASK_1,
                    state="active", progress="1/3 steps"),
        ],
        message_rows=[
            MessageRow(message=MSG_1, sender="motion graphs", recipient="ppt draft",
                       subject="index is ready", sent_at=1700000000.0, read=False),
        ],
    )


def make_app(backend: "FakeBackend") -> "panel.ReinsPanel":
    # A long interval keeps the periodic timer out of the tests; refresh is
    # triggered explicitly with `r`.
    return panel.ReinsPanel(backend, refresh_seconds=3600.0)


async def settle(app: "panel.ReinsPanel", pilot, rounds: int = 6) -> None:
    """Let the thread workers finish and the UI thread apply their results."""
    for _ in range(rounds):
        await app.workers.wait_for_complete()
        await pilot.pause()
        await asyncio.sleep(0.02)


def status_text(app: "panel.ReinsPanel") -> str:
    return str(app.query_one("#status", Static).content)


def status_is_error(app: "panel.ReinsPanel") -> bool:
    return app.query_one("#status", Static).has_class("error")


def select(app: "panel.ReinsPanel", key: str) -> "DataTable":
    """Put the sessions table's cursor on the row with ``key`` (a session, or a folder's key)."""
    table = app.query_one("#sessions-table", DataTable)
    table.focus()
    table.move_cursor(row=table.get_row_index(key))
    return table


@unittest.skipUnless(HAVE_TEXTUAL, "textual is not installed")
class ReinsPanelTests(unittest.IsolatedAsyncioTestCase):
    async def test_rows_render_and_sort(self):
        backend = make_backend()
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            table = app.query_one("#sessions-table", DataTable)
            # Each folder's row, then its sessions under it: process-bearing sessions first, newest first;
            # closed last. Each session here has a folder of its own.
            self.assertEqual(self.shown(app), [
                panel.FOLDER_KEY + os.path.join(os.path.expanduser("~"), "att"), SESSION_D,
                panel.FOLDER_KEY + "/home/u/ppt", SESSION_B,
                panel.FOLDER_KEY + "/home/u/motion", SESSION_A,
                panel.FOLDER_KEY + "/home/u/old", SESSION_C,
            ])
            rows = [table.get_row(key) for key in (SESSION_D, SESSION_B, SESSION_A, SESSION_C)]
            self.assertEqual([str(row[1]) for row in rows],
                             ["attached one", "ppt draft", "motion graphs", "old callback"])
            self.assertEqual([str(row[2]) for row in rows], ["here", "Desktop", "background", "closed"])
            # The mark sits indented under the folder's row.
            self.assertEqual([str(row[0]) for row in rows], ["  \u25cf ", "  \u25cb ", "  \u25cf ", "  \u25cc "])
            # cwd under HOME shortens to ~, an absolute path stays.
            self.assertEqual(str(table.get_row_at(0)[0]), "\u25be ~/att")
            self.assertEqual(str(table.get_row_at(0)[1]), "1 session")
            self.assertEqual(str(table.get_row_at(4)[0]), "\u25be /home/u/motion")
            motion = table.get_row(SESSION_A)
            self.assertEqual(str(motion[3]), "claude")  # the harness, beside where it runs
            self.assertEqual(str(motion[5]), "2")   # running tasks
            self.assertEqual(str(motion[6]), "1")   # pending callbacks

            tasks = app.query_one("#tasks-table", DataTable)
            self.assertEqual(tasks.row_count, 3)
            row0 = [str(cell) for cell in tasks.get_row_at(0)]
            self.assertEqual(
                row0, [TASK_1[:8], "build index", "running", "motion graphs", ""]
            )
            row2 = [str(cell) for cell in tasks.get_row_at(2)]
            self.assertEqual(row2, [TASK_3[:8], "summarize", "succeeded", "ppt draft", "pending"])

            goals = app.query_one("#goals-table", DataTable)
            self.assertEqual(goals.row_count, 1)
            self.assertEqual(
                [str(cell) for cell in goals.get_row_at(0)],
                [GOAL_1[:8], "motion graphs", TASK_1[:8], "active", "1/3 steps"],
            )

            messages = app.query_one("#messages-table", DataTable)
            self.assertEqual(messages.row_count, 1)
            sent = time.strftime("%H:%M:%S", time.localtime(1700000000.0))
            self.assertEqual(
                [str(cell) for cell in messages.get_row_at(0)],
                [MSG_1[:8], "motion graphs", "ppt draft", "index is ready", sent, "new"],
            )

    async def test_enter_opens_selected_session(self):
        backend = make_backend()
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            app.query_one("#sessions-table", DataTable).focus()
            await pilot.press("enter")  # cursor sits on the first row, "attached one"
            await settle(app, pilot)
            self.assertIn(("open", SESSION_D), backend.calls)
            self.assertEqual(status_text(app), "opened attached one")
            self.assertFalse(status_is_error(app))

    async def test_clicking_again_while_a_session_opens_does_not_open_it_twice(self):
        release = threading.Event()

        class SlowBackend(FakeBackend):
            def open_session(self, session, direction="down"):
                release.wait(5)
                return super().open_session(session, direction)

        backend = make_backend(SlowBackend)
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            app.query_one("#sessions-table", DataTable).focus()
            for _ in range(4):
                await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(status_text(app), "opening attached one...")
            release.set()
            await settle(app, pilot)
            self.assertEqual(backend.calls.count(("open", SESSION_D)), 1)
            self.assertEqual(status_text(app), "opened attached one")
            await pilot.press("enter")  # Once it is done, opening again works (it jumps to the pane).
            await settle(app, pilot)
            self.assertEqual(backend.calls.count(("open", SESSION_D)), 2)

    async def test_a_click_selects_and_a_double_click_opens(self):
        backend = make_backend()
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            table = app.query_one("#sessions-table", DataTable)
            for _ in range(2):  # Clicking the highlighted row again still only selects it.
                await pilot.click("#sessions-table", offset=(4, 2))
                await pilot.pause(0.6)
            await settle(app, pilot)
            self.assertEqual(backend.calls, [])
            await pilot.click("#sessions-table", offset=(4, 4))  # Row 3: the second folder's session.
            await pilot.pause(0.6)
            self.assertEqual(table.cursor_row, 3)
            self.assertEqual(backend.calls, [])
            await pilot.double_click("#sessions-table", offset=(4, 4))
            await settle(app, pilot)
            opened = [call for call in backend.calls if call[0] == "open"]
            self.assertEqual(len(opened), 1)
            self.assertEqual(opened[0][1], app._session_rows[1].session)

    async def test_open_foreign_session_shows_refusal(self):
        backend = make_backend()
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            select(app, SESSION_B)  # "ppt draft", open on the Desktop
            await pilot.press("enter")
            await settle(app, pilot)
            self.assertIn(("open", SESSION_B), backend.calls)
            self.assertIn("another window", status_text(app))
            self.assertTrue(status_is_error(app))
            # Not only the status line: a window says why, and Enter closes it.
            self.assertIsInstance(app.screen, panel.AlertScreen)
            self.assertIn("another window", str(app.screen.query_one("#alert-text", panel.Label).content))
            await pilot.press("enter")
            await settle(app, pilot)
            self.assertNotIsInstance(app.screen, panel.AlertScreen)

    async def test_stop_session_requires_confirmation(self):
        backend = make_backend()
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            select(app, SESSION_A)  # "motion graphs", running
            await pilot.press("s")
            await pilot.pause()
            self.assertIsInstance(app.screen, panel.ConfirmScreen)
            self.assertEqual(backend.calls, [])
            # "no" closes the dialog without stopping anything.
            await pilot.press("n")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, panel.ConfirmScreen)
            self.assertEqual(backend.calls, [])
            # "yes" stops it.
            await pilot.press("s")
            await pilot.pause()
            self.assertIsInstance(app.screen, panel.ConfirmScreen)
            await pilot.press("y")
            await settle(app, pilot)
            self.assertIn(("stop", SESSION_A), backend.calls)
            self.assertEqual(status_text(app), "stopped motion graphs")
            self.assertFalse(status_is_error(app))

    async def test_new_session_modal_prefills_and_submits(self):
        backend = make_backend()
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            select(app, SESSION_A)  # cwd /home/u/motion
            await pilot.press("n")
            await settle(app, pilot)
            self.assertIsInstance(app.screen, panel.TextPromptScreen)
            box = app.screen.query_one("#dialog-input", Input)
            self.assertEqual(box.value, "/home/u/motion")
            # Escape cancels without calling the backend.
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, panel.TextPromptScreen)
            self.assertEqual(backend.calls, [])
            # With only Claude Code installed there is nothing to choose: Enter submits the directory, then
            # the name, offered from the directory and the harness.
            await pilot.press("n")
            await settle(app, pilot)
            await pilot.press("enter")
            await settle(app, pilot)
            self.assertIsInstance(app.screen, panel.TextPromptScreen)
            self.assertEqual(app.screen.query_one("#dialog-input", Input).value, "motion-claude")
            await pilot.press("enter")
            await settle(app, pilot)
            self.assertIn(("new", "claude", "/home/u/motion", "motion-claude"), backend.calls)
            self.assertFalse(status_is_error(app))

    async def test_log_on_tasks_tab(self):
        backend = make_backend()
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await pilot.press("2")
            await pilot.pause()
            await pilot.press("l")  # first task row: TASK_1
            await settle(app, pilot)
            self.assertIn(("log", TASK_1), backend.calls)

    async def test_log_on_sessions_tab(self):
        backend = make_backend()
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            table = select(app, SESSION_A)  # "motion graphs": two running tasks
            await pilot.press("l")
            await settle(app, pilot)
            # The newest running task wins.
            self.assertIn(("log", TASK_2), backend.calls)
            # A session without a running task gets a status message instead.
            table.move_cursor(row=table.get_row_index(SESSION_C))  # "old callback", closed
            calls_before = len(backend.calls)
            await pilot.press("l")
            await pilot.pause()
            self.assertEqual(len(backend.calls), calls_before)
            self.assertIn("has no running task", status_text(app))

    async def test_d_switches_where_panes_open_and_t_opens_a_terminal(self):
        backend = make_backend()
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            app.query_one("#sessions-table", DataTable).focus()
            await pilot.press("t")  # below, in the selected session's directory
            await settle(app, pilot)
            await pilot.press("d")
            await pilot.pause()
            self.assertIn("to the right", status_text(app))
            await pilot.press("enter")  # first row, "attached one"
            await settle(app, pilot)
            self.assertIn(("open", SESSION_D, "right"), backend.calls)
            await pilot.press("t")
            await settle(app, pilot)
            terminals = [call for call in backend.calls if call[0] == "terminal"]
            self.assertEqual([call[2] for call in terminals], ["down", "right"])
            self.assertTrue(all(call[1].endswith("att") for call in terminals))
            await pilot.press("2")
            await pilot.pause()
            await pilot.press("l")
            await settle(app, pilot)
            self.assertIn(("log", TASK_1, "right"), backend.calls)
            await pilot.press("d")  # and back
            await pilot.press("l")
            await settle(app, pilot)
            self.assertIn(("log", TASK_1), backend.calls)

    async def test_the_health_line_shows_doctor_problems_or_ready(self):
        backend = make_backend()
        backend.problems = ["the Reins daemon is not running: reins setup --enable --now"]
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            health = app.query_one("#health", Static)
            self.assertIn("daemon is not running", str(health.render()))
            self.assertTrue(health.has_class("problem"))
            backend.problems = []
            app.check_health()
            await settle(app, pilot)
            self.assertIn("Reins is ready", str(health.render()))
            self.assertTrue(health.has_class("ok"))

    async def right_click_first_session(self, app, pilot):
        # A real right click through Textual's mouse routing: the first session, under the header and its folder.
        await pilot.click("#sessions-table", offset=(4, 2), button=3)
        await settle(app, pilot)

    async def choose(self, app, pilot, option):
        """Pick ``option`` in the open menu, as arrows and Enter would."""
        self.assertIsInstance(app.screen, panel.MenuScreen)
        menu = app.screen.query_one("#menu", OptionList)
        menu.highlighted = menu.get_option_index(option)
        await pilot.press("enter")
        await settle(app, pilot)

    def first_row(self, app):
        table = app.query_one("#sessions-table", DataTable)
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value if table.row_count else None

    def shown(self, app, folders: bool = True):
        table = app.query_one("#sessions-table", DataTable)
        return [key.value for key in table.rows if folders or not key.value.startswith(panel.FOLDER_KEY)]

    async def test_right_click_copies_a_named_sessions_address(self):
        backend = make_backend()
        backend.session_rows = [replace(row, address="lab") if row.session == SESSION_D else row
                                for row in backend.session_rows]
        app = make_app(backend)
        copied = []
        app.copy_to_clipboard = copied.append
        async with app.run_test() as pilot:
            await settle(app, pilot)
            row = app.query_one("#sessions-table", DataTable).get_row(SESSION_D)
            # The name given in Reins, and beside it the title the harness gave the session.
            self.assertEqual((str(row[0]), str(row[1])), ("  \u25cf @lab", "attached one"))
            await self.right_click_first_session(app, pilot)
            await self.choose(app, pilot, "copy-address")
            self.assertEqual(copied, ["lab"])
            self.assertIn("copied address lab", status_text(app))
            # Terminals that ignore the copy (macOS Terminal) get the text on a line of its own.
            self.assertIsInstance(app.screen, panel.CopiedScreen)
            self.assertEqual(str(app.screen.query_one("#copied-text", Static).content), "lab")
            await pilot.press("enter")
            await settle(app, pilot)
            self.assertNotIsInstance(app.screen, panel.CopiedScreen)

    async def test_right_click_names_an_unnamed_session_then_copies(self):
        backend = make_backend()
        app = make_app(backend)
        copied = []
        app.copy_to_clipboard = copied.append
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await self.right_click_first_session(app, pilot)
            await self.choose(app, pilot, "name")
            self.assertIsInstance(app.screen, panel.TextPromptScreen)
            await pilot.press("enter")  # accept the suggested name
            await settle(app, pilot)
            self.assertIn(("name", SESSION_D, "attached-one"), backend.calls)
            self.assertEqual(copied, ["attached-one"])

    def test_the_menu_offers_what_fits_the_session(self):
        rows = {row.session: row for row in make_backend().session_rows}
        ids = lambda row: [key for key, _ in panel._session_menu(row)]
        running = ids(rows[SESSION_A])
        self.assertEqual(running[:4], ["open-below", "open-right", "fork", "shell"])
        self.assertIn("stop", running)
        self.assertIn("archive", running)
        self.assertIn("Stop and archive...", dict(panel._session_menu(rows[SESSION_A])).values())
        foreign = ids(rows[SESSION_B])  # Open in the desktop app: fork it, but neither stop nor archive it here.
        self.assertIn("fork", foreign)
        self.assertNotIn("stop", foreign)
        self.assertNotIn("archive", foreign)
        closed = ids(rows[SESSION_C])
        self.assertIn("archive", closed)
        self.assertNotIn("stop", closed)
        named = ids(replace(rows[SESSION_C], address="lab", pinned=True, archived=True))
        self.assertIn("rename", named)
        self.assertIn("unpin", named)
        self.assertIn("unarchive", named)
        self.assertNotIn("name", named)
        self.assertNotIn("fork", ids(replace(rows[SESSION_C], cwd=None)))
        self.assertIn("Copy Claude session id", dict(panel._session_menu(rows[SESSION_C])).values())

    def test_each_harness_gets_the_menu_it_can_use(self):
        codex = SessionRow(session=SESSION_A, agent="codex", state=sessions.RUNNING, title="t", cwd="/w")
        ids = lambda row: [key for key, _ in panel._session_menu(row)]
        # Codex: open and fork go through its daemon; it unloads idle threads itself, so there is no stop.
        self.assertEqual(ids(codex)[:4], ["open-below", "open-right", "fork", "shell"])
        self.assertNotIn("stop", ids(codex))
        self.assertIn(("archive", "Archive"), panel._session_menu(codex))
        self.assertIn("Copy Codex thread id", dict(panel._session_menu(codex)).values())
        # Pi: no fork Reins can start, and an open session ends with its pane rather than by stop or archive.
        pi = replace(codex, agent="pi", session="/p/s_x.jsonl", state=sessions.ATTACHED)
        self.assertNotIn("fork", ids(pi))
        self.assertNotIn("stop", ids(pi))
        self.assertNotIn("archive", ids(pi))
        self.assertIn("archive", ids(replace(pi, state=sessions.CLOSED)))
        # DeepSeek Harness: nothing to open in a terminal; messages and names still work.
        dsh = replace(codex, agent="dsh", state=sessions.CLOSED)
        self.assertNotIn("open-below", ids(dsh))
        self.assertNotIn("fork", ids(dsh))
        self.assertIn("message", ids(dsh))
        self.assertEqual(panel._where(dsh), "DSH app")

    async def test_new_asks_which_harness_when_there_are_several(self):
        backend = make_backend()
        backend.agents = ["claude", "codex", "pi"]
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await pilot.press("n")
            await settle(app, pilot)
            await self.choose(app, pilot, "codex")
            self.assertIsInstance(app.screen, panel.TextPromptScreen)
            self.assertIn("Codex", app.screen._prompt)
            await pilot.press("enter")
            await settle(app, pilot)
            self.assertEqual([call[:2] for call in backend.calls if call[0] == "new"], [("new", "codex")])

    async def test_s_on_a_codex_session_explains_instead_of_stopping(self):
        backend = make_backend()
        backend.session_rows = [replace(row, agent="codex") if row.session == SESSION_D else row
                                for row in backend.session_rows]
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await pilot.press("s")
            await settle(app, pilot)
            self.assertNotIsInstance(app.screen, panel.ConfirmScreen)
            self.assertIn("unloads an idle thread", status_text(app))
            self.assertNotIn(("stop", SESSION_D), backend.calls)

    async def test_o_opens_the_menu_and_open_to_the_right_opens_there(self):
        backend = make_backend()
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await pilot.press("o")
            await settle(app, pilot)
            await self.choose(app, pilot, "open-right")
            self.assertIn(("open", SESSION_D, "right"), backend.calls)

    async def test_pinning_moves_a_session_to_the_top(self):
        backend = make_backend()
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            self.assertEqual(self.shown(app)[-1], SESSION_C)
            table = app.query_one("#sessions-table", DataTable)
            table.move_cursor(row=table.get_row_index(SESSION_C))
            await pilot.press("o")
            await settle(app, pilot)
            await self.choose(app, pilot, "pin")
            # Its folder moves to the top with it.
            self.assertEqual(self.shown(app)[:2], [panel.FOLDER_KEY + "/home/u/old", SESSION_C])
            self.assertIn("\u2605", str(table.get_row(SESSION_C)[0]))

    async def test_archive_hides_a_session_and_a_shows_archived_ones(self):
        backend = make_backend()
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            table = app.query_one("#sessions-table", DataTable)
            table.move_cursor(row=table.get_row_index(SESSION_C))
            await pilot.press("o")
            await settle(app, pilot)
            await self.choose(app, pilot, "archive")  # Closed: no question asked.
            self.assertIn(("archive", SESSION_C), backend.calls)
            self.assertNotIn(SESSION_C, self.shown(app))
            await pilot.press("a")
            await settle(app, pilot)
            self.assertIn(SESSION_C, self.shown(app))
            self.assertEqual(str(table.get_row_at(table.get_row_index(SESSION_C))[2]), "archived")
            table.move_cursor(row=table.get_row_index(SESSION_C))
            await pilot.press("o")
            await settle(app, pilot)
            await self.choose(app, pilot, "unarchive")
            self.assertIn(("unarchive", SESSION_C), backend.calls)
            await pilot.press("a")
            await settle(app, pilot)
            self.assertIn(SESSION_C, self.shown(app))

    async def test_archiving_a_running_session_asks_first(self):
        backend = make_backend()
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await pilot.press("o")  # First row: the attached session D.
            await settle(app, pilot)
            await self.choose(app, pilot, "archive")
            self.assertIsInstance(app.screen, panel.ConfirmScreen)
            await pilot.press("n")
            await settle(app, pilot)
            self.assertNotIn(("archive", SESSION_D), backend.calls)
            await pilot.press("o")
            await settle(app, pilot)
            await self.choose(app, pilot, "archive")
            await pilot.press("y")
            await settle(app, pilot)
            self.assertIn(("archive", SESSION_D), backend.calls)
            self.assertNotIn(SESSION_D, self.shown(app))

    async def test_fork_opens_the_copy_where_panes_go(self):
        backend = make_backend()
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await pilot.press("d")  # Later panes to the right.
            await pilot.press("o")
            await settle(app, pilot)
            await self.choose(app, pilot, "fork")
            self.assertIn(("fork", SESSION_D, "right"), backend.calls)
            self.assertIn("forked attached one", status_text(app))

    async def test_a_collaborator_takes_harness_directory_and_task_from_one_menu_item(self):
        backend = make_backend()
        backend.agents = ["claude", "codex"]
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await pilot.press("o")
            await settle(app, pilot)
            await self.choose(app, pilot, "collaborate")
            await self.choose(app, pilot, "codex")
            self.assertIsInstance(app.screen, panel.TextPromptScreen)
            self.assertEqual(app.screen.query_one(Input).value, os.path.join(os.path.expanduser("~"), "att"))
            await pilot.press("enter")
            await settle(app, pilot)
            # Escape on the task asks nothing of the backend.
            await pilot.press("escape")
            await settle(app, pilot)
            self.assertEqual([call for call in backend.calls if call[0] == "collaborate"], [])
            await pilot.press("o")
            await settle(app, pilot)
            await self.choose(app, pilot, "collaborate")
            await self.choose(app, pilot, "codex")
            await pilot.press("enter")
            await settle(app, pilot)
            app.screen.query_one(Input).value = "  check the plots  "
            await pilot.press("enter")
            await settle(app, pilot)
            self.assertIn(("collaborate", SESSION_D, "codex", os.path.join(os.path.expanduser("~"), "att"),
                           "check the plots", "down"), backend.calls)
            self.assertIn("to work with attached one", status_text(app))

    async def test_sessions_of_one_folder_sit_together_under_its_row(self):
        backend = make_backend()
        backend.session_rows = [replace(row, cwd="/home/u/motion") if row.session == SESSION_C else
                                replace(row, cwd=None, agent="dsh") if row.session == SESSION_B else row
                                for row in backend.session_rows]
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            # A folder sorts where its first session would; the closed session joins its folder's running one.
            self.assertEqual(self.shown(app), [
                panel.FOLDER_KEY + os.path.join(os.path.expanduser("~"), "att"), SESSION_D,
                panel.FOLDER_KEY, SESSION_B,
                panel.FOLDER_KEY + "/home/u/motion", SESSION_A, SESSION_C,
            ])
            table = app.query_one("#sessions-table", DataTable)
            self.assertEqual(str(table.get_row(panel.FOLDER_KEY + "/home/u/motion")[1]), "2 sessions")
            self.assertEqual(str(table.get_row(panel.FOLDER_KEY)[0]), "\u25be (directory unknown)")
            # The cursor starts on a session, never on a folder's row.
            self.assertEqual(table.cursor_row, 1)

    async def test_a_folder_row_opens_nothing_and_starts_new_work_there(self):
        backend = make_backend()
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            folder = panel.FOLDER_KEY + "/home/u/motion"
            select(app, folder)
            await pilot.press("enter")
            await settle(app, pilot)
            self.assertEqual(backend.calls, [])
            self.assertIn("a folder", status_text(app))
            self.assertFalse(status_is_error(app))
            # n starts in that folder.
            await pilot.press("n")
            await settle(app, pilot)
            self.assertEqual(app.screen.query_one("#dialog-input", Input).value, "/home/u/motion")
            await pilot.press("escape")
            await settle(app, pilot)
            # A right click on it offers new work there, not a session's menu.
            await pilot.click("#sessions-table", offset=(4, 1 + app.query_one("#sessions-table", DataTable)
                                                    .get_row_index(folder)), button=3)
            await settle(app, pilot)
            self.assertIsInstance(app.screen, panel.MenuScreen)
            await self.choose(app, pilot, "shell")
            self.assertIn(("terminal", "/home/u/motion", "down"), backend.calls)

    async def test_a_new_session_takes_a_free_name_or_none(self):
        backend = make_backend()
        backend.session_rows = [replace(row, address="motion-claude") if row.session == SESSION_A else row
                                for row in backend.session_rows]
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            select(app, SESSION_A)
            await pilot.press("n")
            await settle(app, pilot)
            await pilot.press("enter")
            await settle(app, pilot)
            # The name another session holds is not offered again.
            self.assertEqual(app.screen.query_one("#dialog-input", Input).value, "motion-claude-2")
            # Escape on the name starts nothing.
            await pilot.press("escape")
            await settle(app, pilot)
            self.assertEqual([call for call in backend.calls if call[0] == "new"], [])
            # A cleared name starts it without one.
            await pilot.press("n")
            await settle(app, pilot)
            await pilot.press("enter")
            await settle(app, pilot)
            app.screen.query_one("#dialog-input", Input).value = ""
            await pilot.press("enter")
            await settle(app, pilot)
            self.assertIn(("new", "claude", "/home/u/motion", ""), backend.calls)

    async def test_a_new_codex_session_is_not_asked_for_a_name(self):
        backend = make_backend()
        backend.agents = ["claude", "codex"]
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            select(app, SESSION_A)
            await pilot.press("n")
            await settle(app, pilot)
            await self.choose(app, pilot, "codex")
            await pilot.press("enter")  # the directory
            await settle(app, pilot)
            self.assertIn(("new", "codex", "/home/u/motion", ""), backend.calls)

    async def test_a_name_that_is_refused_says_so_in_a_window(self):
        backend = make_backend()
        backend.session_rows = [replace(row, address="lab") if row.session == SESSION_B else row
                                for row in backend.session_rows]
        app = make_app(backend)
        copied = []
        app.copy_to_clipboard = copied.append
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await self.right_click_first_session(app, pilot)
            await self.choose(app, pilot, "name")
            app.screen.query_one("#dialog-input", Input).value = "lab"  # another session's
            await pilot.press("enter")
            await settle(app, pilot)
            self.assertIsInstance(app.screen, panel.AlertScreen)
            self.assertTrue(status_is_error(app))
            self.assertEqual(copied, [])

    async def test_a_failure_while_a_menu_is_open_still_shows_its_window(self):
        backend = make_backend()
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            select(app, SESSION_B)  # open on the Desktop: opening it fails
            await pilot.press("o")
            await settle(app, pilot)
            self.assertIsInstance(app.screen, panel.MenuScreen)
            app._finish_action(backend.open_session(SESSION_B), None, "open")
            await settle(app, pilot)
            self.assertIsInstance(app.screen, panel.AlertScreen)
            await pilot.press("enter")
            await settle(app, pilot)
            self.assertIsInstance(app.screen, panel.MenuScreen)  # back where the user was

    async def test_rename_an_address_and_a_taken_name_is_refused(self):
        backend = make_backend()
        backend.session_rows = [replace(row, address="lab") if row.session == SESSION_D
                                else replace(row, address="ppt") if row.session == SESSION_B else row
                                for row in backend.session_rows]
        app = make_app(backend)
        copied = []
        app.copy_to_clipboard = copied.append
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await pilot.press("o")
            await settle(app, pilot)
            await self.choose(app, pilot, "rename")
            self.assertIsInstance(app.screen, panel.TextPromptScreen)
            app.screen.query_one(Input).value = "lab2"
            await pilot.press("enter")
            await settle(app, pilot)
            self.assertIn(("rename", SESSION_D, "lab2"), backend.calls)
            self.assertEqual(copied, ["lab2"])
            self.assertIsInstance(app.screen, panel.CopiedScreen)
            await pilot.press("escape")
            await settle(app, pilot)
            await pilot.press("o")
            await settle(app, pilot)
            await self.choose(app, pilot, "rename")
            app.screen.query_one(Input).value = "ppt"
            await pilot.press("enter")
            await settle(app, pilot)
            self.assertIn("already names another session", status_text(app))
            self.assertTrue(status_is_error(app))

    async def test_copying_a_session_id_shows_it_and_never_reads_it_as_markup(self):
        backend = make_backend()
        app = make_app(backend)
        copied = []
        app.copy_to_clipboard = copied.append
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await pilot.press("o")
            await settle(app, pilot)
            await self.choose(app, pilot, "copy-id")
            self.assertEqual(copied, [SESSION_D])
            self.assertEqual(str(app.screen.query_one("#copied-text", Static).content), SESSION_D)
            self.assertIn("Claude session id", str(app.screen.query_one("#dialog-title").content))
            await pilot.click("#ok")
            await settle(app, pilot)
            self.assertNotIsInstance(app.screen, panel.CopiedScreen)
        screen = panel.CopiedScreen("address", "[bold]x[/]")
        async with panel.ReinsPanel(FakeBackend(), refresh_seconds=3600.0).run_test() as pilot:
            pilot.app.push_screen(screen)
            await pilot.pause()
            self.assertEqual(str(screen.query_one("#copied-text", Static).render()), "[bold]x[/]")

    def test_suggested_names_fit_the_address_book(self):
        row = make_backend().session_rows[0]
        self.assertEqual(panel._suggested_name(replace(row, title="Motion Plots: v2")), "motion-plots-v2")
        self.assertEqual(panel._suggested_name(replace(row, title="嵌入空间运动图")), "s-" + row.session[:8])

    async def test_send_message(self):
        backend = make_backend()
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            app.query_one("#sessions-table", DataTable).focus()  # row 0: "attached one"
            await pilot.press("m")
            await pilot.pause()
            self.assertIsInstance(app.screen, panel.TextPromptScreen)
            await pilot.press(*list("hello"))
            await pilot.press("enter")
            await settle(app, pilot)
            self.assertIn(("send", SESSION_D, "hello"), backend.calls)
            self.assertFalse(status_is_error(app))

    async def test_cancel_callback_requires_confirmation(self):
        backend = make_backend()
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await pilot.press("2")
            await pilot.pause()
            tasks = app.query_one("#tasks-table", DataTable)
            tasks.move_cursor(row=2)  # TASK_3 has a pending callback
            await pilot.press("c")
            await pilot.pause()
            self.assertIsInstance(app.screen, panel.ConfirmScreen)
            self.assertEqual(backend.calls, [])
            await pilot.press("y")
            await settle(app, pilot)
            self.assertIn(("cancel", CALLBACK_3), backend.calls)
            # A task without a pending callback: no dialog, a status message.
            tasks.move_cursor(row=0)
            calls_before = len(backend.calls)
            await pilot.press("c")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, panel.ConfirmScreen)
            self.assertEqual(len(backend.calls), calls_before)
            self.assertIn("no pending callback", status_text(app))

    async def test_enter_on_task_opens_its_session(self):
        backend = make_backend()
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await pilot.press("2")
            await pilot.pause()
            await pilot.press("enter")  # first task row belongs to SESSION_A
            await settle(app, pilot)
            self.assertIn(("open", SESSION_A), backend.calls)

    async def test_selection_survives_refresh(self):
        backend = make_backend()
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            table = select(app, SESSION_A)  # "motion graphs"
            row_before = table.cursor_row
            app.refresh_data()
            await settle(app, pilot)
            key_after = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            self.assertEqual(key_after, SESSION_A)
            self.assertEqual(table.cursor_row, row_before)

    async def test_raising_backend_read_shows_error_and_app_lives(self):
        class RaisingTasksBackend(FakeBackend):
            def tasks(self):
                raise RuntimeError("queue is locked")

        backend = RaisingTasksBackend(session_rows=make_backend().session_rows)
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            self.assertIn("queue is locked", status_text(app))
            self.assertTrue(status_is_error(app))
            # The sessions that did read still render, and the app keeps running.
            self.assertEqual(len(self.shown(app, folders=False)), 4)
            await pilot.press("2")
            await pilot.pause()
            self.assertEqual(app.query_one("#tabs", TabbedContent).active, panel.TASKS_TAB)
            self.assertTrue(app.is_running)

    async def test_raising_backend_action_shows_error_and_app_lives(self):
        class RaisingOpenBackend(FakeBackend):
            def open_session(self, session, direction="down"):
                self.calls.append(("open", session))
                raise RuntimeError("zellij is gone")

        backend = RaisingOpenBackend(session_rows=make_backend().session_rows)
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            app.query_one("#sessions-table", DataTable).focus()
            await pilot.press("enter")
            await settle(app, pilot)
            self.assertIn(("open", SESSION_D), backend.calls)
            self.assertIn("zellij is gone", status_text(app))
            self.assertTrue(status_is_error(app))
            self.assertTrue(app.is_running)

    async def test_ctrl_q_offers_to_leave_instead_of_closing_the_panel(self):
        backend = make_backend()
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await pilot.press("ctrl+q")
            await settle(app, pilot)
            self.assertTrue(app.is_running)
            await self.choose(app, pilot, "end")
            self.assertIn(("end",), backend.calls)
            await pilot.press("ctrl+q")
            await settle(app, pilot)
            await pilot.press("escape")
            await settle(app, pilot)
            self.assertTrue(app.is_running)
            self.assertNotIn(("detach",), backend.calls)

    async def test_q_does_not_quit(self):
        backend = make_backend()
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            await pilot.press("q")
            await pilot.pause()
            self.assertTrue(app.is_running)
            self.assertNotIsInstance(
                app.screen, (panel.ConfirmScreen, panel.TextPromptScreen)
            )
            # And the panel still responds afterwards.
            await pilot.press("2")
            await pilot.pause()
            self.assertEqual(app.query_one("#tabs", TabbedContent).active, panel.TASKS_TAB)



@unittest.skipUnless(HAVE_TEXTUAL, "textual is not installed")
class FitTests(unittest.TestCase):
    def test_a_long_title_is_cut_to_its_column(self):
        self.assertEqual(panel._fit("short", 10), "short")
        cut = panel._fit("做下运动人体生成加渲染的调研，尝试寻找问题缺口", 12)
        self.assertTrue(cut.endswith("…"))
        self.assertLessEqual(panel.cell_len(cut), 12)

if __name__ == "__main__":
    unittest.main()
