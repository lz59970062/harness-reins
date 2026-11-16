"""Tests for the Textual Reins panel (commands/tui/panel.py) against FakeBackend.

Skipped when textual is not installed. Run with a fresh HOME, never the real one:

    env -i HOME=$(mktemp -d) PATH=<python>:/usr/bin:/bin PYTHONPATH=src \
        python -m unittest discover -s tests -p test_tui_panel.py -v
"""
from __future__ import annotations

import asyncio
import os
import time
import unittest

try:
    import textual  # noqa: F401
    from textual.widgets import DataTable, Input, Static, TabbedContent

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


def make_backend() -> "FakeBackend":
    home = os.path.expanduser("~")
    return FakeBackend(
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


@unittest.skipUnless(HAVE_TEXTUAL, "textual is not installed")
class ReinsPanelTests(unittest.IsolatedAsyncioTestCase):
    async def test_rows_render_and_sort(self):
        backend = make_backend()
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            table = app.query_one("#sessions-table", DataTable)
            self.assertEqual(table.row_count, 4)
            # Process-bearing sessions first, newest first; closed last.
            titles = [str(table.get_row_at(i)[5]) for i in range(4)]
            self.assertEqual(
                titles, ["attached one", "ppt draft", "motion graphs", "old callback"]
            )
            wheres = [str(table.get_row_at(i)[1]) for i in range(4)]
            self.assertEqual(wheres, ["here", "Desktop", "background", "closed"])
            marks = [str(table.get_row_at(i)[0]) for i in range(4)]
            self.assertEqual(marks, ["\u25cf", "\u25cb", "\u25cf", "\u25cc"])
            # cwd under HOME shortens to ~, an absolute path stays.
            self.assertEqual(str(table.get_row_at(0)[7]), "~/att")
            self.assertEqual(str(table.get_row_at(2)[7]), "/home/u/motion")
            self.assertEqual(str(table.get_row_at(2)[3]), "2")   # running tasks
            self.assertEqual(str(table.get_row_at(2)[4]), "1")   # pending callbacks

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

    async def test_open_foreign_session_shows_refusal(self):
        backend = make_backend()
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            table = app.query_one("#sessions-table", DataTable)
            table.focus()
            table.move_cursor(row=1)  # "ppt draft", open on the Desktop
            await pilot.press("enter")
            await settle(app, pilot)
            self.assertIn(("open", SESSION_B), backend.calls)
            self.assertIn("another window", status_text(app))
            self.assertTrue(status_is_error(app))

    async def test_stop_session_requires_confirmation(self):
        backend = make_backend()
        app = make_app(backend)
        async with app.run_test() as pilot:
            await settle(app, pilot)
            table = app.query_one("#sessions-table", DataTable)
            table.focus()
            table.move_cursor(row=2)  # "motion graphs", running
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
            table = app.query_one("#sessions-table", DataTable)
            table.focus()
            table.move_cursor(row=2)  # cwd /home/u/motion
            await pilot.press("n")
            await pilot.pause()
            self.assertIsInstance(app.screen, panel.TextPromptScreen)
            box = app.screen.query_one("#dialog-input", Input)
            self.assertEqual(box.value, "/home/u/motion")
            # Escape cancels without calling the backend.
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, panel.TextPromptScreen)
            self.assertEqual(backend.calls, [])
            # Enter submits the directory with the fixed "claude" agent.
            await pilot.press("n")
            await pilot.pause()
            await pilot.press("enter")
            await settle(app, pilot)
            self.assertIn(("new", "claude", "/home/u/motion"), backend.calls)
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
            table = app.query_one("#sessions-table", DataTable)
            table.focus()
            table.move_cursor(row=2)  # "motion graphs": two running tasks
            await pilot.press("l")
            await settle(app, pilot)
            # The newest running task wins.
            self.assertIn(("log", TASK_2), backend.calls)
            # A session without a running task gets a status message instead.
            table.move_cursor(row=3)  # "old callback", closed
            calls_before = len(backend.calls)
            await pilot.press("l")
            await pilot.pause()
            self.assertEqual(len(backend.calls), calls_before)
            self.assertIn("has no running task", status_text(app))

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
            table = app.query_one("#sessions-table", DataTable)
            table.focus()
            table.move_cursor(row=2)  # "motion graphs"
            key_before = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            self.assertEqual(key_before, SESSION_A)
            await pilot.press("r")
            await settle(app, pilot)
            key_after = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            self.assertEqual(key_after, SESSION_A)
            self.assertEqual(table.cursor_row, 2)

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
            self.assertEqual(app.query_one("#sessions-table", DataTable).row_count, 4)
            await pilot.press("2")
            await pilot.pause()
            self.assertEqual(app.query_one("#tabs", TabbedContent).active, panel.TASKS_TAB)
            self.assertTrue(app.is_running)

    async def test_raising_backend_action_shows_error_and_app_lives(self):
        class RaisingOpenBackend(FakeBackend):
            def open_session(self, session):
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
