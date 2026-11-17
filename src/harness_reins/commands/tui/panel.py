"""The Reins panel: the left pane of `reins tui` (docs/design/sessions-tui.md section 7).

The panel only draws rows and calls a ``Backend`` (backend.py); it never
touches Claude, the queue or Zellij itself. Every backend call may block for
seconds, so reads and actions run in thread workers and the result comes back
to the UI thread with ``call_from_thread``. A backend failure shows in the
one-line status bar and never crashes the panel.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from rich.cells import cell_len
from rich.text import Text
from textual.coordinate import Coordinate
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual import events
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Input,
    Label,
    OptionList,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets.option_list import Option
from textual.worker import get_current_worker

from ...core import sessions
from .backend import BELOW, BESIDE, ActionResult, Backend, GoalRow, MessageRow, SessionRow, TaskRow

SESSIONS_TAB = "tab-sessions"
TASKS_TAB = "tab-tasks"
GOALS_TAB = "tab-goals"
MESSAGES_TAB = "tab-messages"

_TAB_TABLES = {
    SESSIONS_TAB: "sessions-table",
    TASKS_TAB: "tasks-table",
    GOALS_TAB: "goals-table",
    MESSAGES_TAB: "messages-table",
}

# A filled mark for a process on the Reins side, an empty one for a process
# elsewhere, a dotted one when no process runs (as in design section 7.1).
_MARKS = {
    sessions.ATTACHED: "\u25cf",
    sessions.RUNNING: "\u25cf",
    sessions.FOREIGN: "\u25cb",
    sessions.HEADLESS: "\u25cb",
    sessions.STOPPED: "\u25cc",
    sessions.CLOSED: "\u25cc",
}

_PROCESS_STATES = (sessions.ATTACHED, sessions.RUNNING, sessions.FOREIGN, sessions.HEADLESS)


def _where(row: SessionRow) -> str:
    """State and host in plain words: here, background, Desktop, terminal, ..."""
    if row.state == sessions.ATTACHED:
        return "here"
    if row.state == sessions.RUNNING:
        return "background"
    if row.state == sessions.FOREIGN:
        if row.host == sessions.HOST_DESKTOP:
            return "Desktop"
        if row.host == sessions.HOST_TERMINAL:
            return "terminal"
        return "foreign"
    if row.state == sessions.HEADLESS:
        return "headless"
    if row.state == sessions.STOPPED:
        return "stopped"
    if row.state == sessions.CLOSED:
        return "closed"
    return row.state


def _shorten_cwd(cwd: Optional[str]) -> str:
    if not cwd:
        return ""
    home = os.path.expanduser("~").rstrip(os.sep)
    if cwd == home:
        return "~"
    if home and cwd.startswith(home + os.sep):
        return "~" + cwd[len(home):]
    return cwd


def _time_of(timestamp: Optional[float]) -> str:
    if not timestamp:
        return ""
    return time.strftime("%H:%M:%S", time.localtime(timestamp))


def _session_sort_key(row: SessionRow) -> Tuple[bool, bool, float]:
    # Pinned sessions first, then sessions with a process, then newest first.
    return (not row.pinned, row.state not in _PROCESS_STATES, -(row.started_at or 0.0))


def _fit(text: str, width: int) -> str:
    """At most ``width`` terminal cells (CJK takes two), so one long title cannot push the other columns away."""
    if cell_len(text) <= width:
        return text
    while text and cell_len(text) > width - 1:
        text = text[:-1]
    return text + "…"


def _session_cells(row: SessionRow) -> List[Any]:
    # Which session, where it runs and what it is doing come first: the panel is narrow.
    cells: List[Any] = [
        _MARKS.get(row.state, "?"),
        _fit(("\u2605 " if row.pinned else "") + ("@%s " % row.address if row.address else "") + row.title,
             TITLE_CELLS),
        "archived" if row.archived else _where(row),
        # Claude reports "blocked" both for a session waiting on a denied tool call and for one
        # waiting for its first prompt; either way it waits for a person.
        "waiting" if row.activity == sessions.BLOCKED else (row.activity or ""),
        str(row.tasks_running) if row.tasks_running else "",
        str(row.callbacks_pending) if row.callbacks_pending else "",
        row.agent,
        _fit(_shorten_cwd(row.cwd), DIRECTORY_CELLS),
    ]
    if row.state == sessions.CLOSED or row.archived:
        return [Text(str(cell), style="dim") for cell in cells]
    return cells


def _session_menu(row: SessionRow) -> List[Tuple[str, str]]:
    """What a right click (or o) on a session offers, in the order shown."""
    running = row.state in (sessions.ATTACHED, sessions.RUNNING)
    elsewhere = row.state in (sessions.FOREIGN, sessions.HEADLESS)
    options: List[Tuple[str, str]] = []
    if row.state != sessions.HEADLESS:
        options += [("open-below", "Open below"), ("open-right", "Open to the right")]
    if row.cwd:
        options += [("fork", "Fork into a new session"),
                    ("shell", "Terminal in its directory")]
    options.append(("message", "Send a message..."))
    options.append(("unpin", "Unpin") if row.pinned else ("pin", "Pin to the top"))
    if row.address:
        options += [("copy-address", "Copy address  @%s" % row.address), ("rename", "Rename address...")]
    else:
        options.append(("name", "Name it and copy its address..."))
    options.append(("copy-id", "Copy Claude session id"))
    if running:
        options.append(("stop", "Stop..."))
    if row.archived:
        options.append(("unarchive", "Unarchive"))
    elif not elsewhere:
        options.append(("archive", "Stop and archive..." if running else "Archive"))
    return options


def _message_cells(row: MessageRow) -> List[Any]:
    return [
        row.message[:8],
        row.sender,
        row.recipient,
        _fit(row.subject, TITLE_CELLS),
        _time_of(row.sent_at),
        "" if row.read else "new",
    ]


TITLE_CELLS = 26
HEALTH_SECONDS = 60.0
DIRECTORY_CELLS = 30

# Task states that mean it has not ended yet (the same set `reins log` follows).
RUNNING_TASK_STATES = frozenset({"submitting", "submitted", "launching", "running"})

class ConfirmScreen(ModalScreen[bool]):
    """A yes/no question; dismisses True for yes, False for no."""

    BINDINGS = [
        Binding("y", "confirm", "Yes"),
        Binding("n", "cancel", "No"),
        Binding("escape", "cancel", show=False),
    ]

    def __init__(self, prompt: str) -> None:
        super().__init__()
        self._prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._prompt, id="dialog-title")
            with Horizontal(id="dialog-buttons"):
                yield Button("Yes", id="yes", variant="error")
                yield Button("No", id="no", variant="primary")

    def on_mount(self) -> None:
        # Default focus on No: a destructive action should take a deliberate key.
        self.query_one("#no", Button).focus()

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#yes")
    def _yes_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def _no_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(False)


class TextPromptScreen(ModalScreen[Optional[str]]):
    """Ask for one line of text; dismisses the text, or None when canceled."""

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, prompt: str, initial: str = "") -> None:
        super().__init__()
        self._prompt = prompt
        self._initial = initial

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._prompt, id="dialog-title")
            yield Input(value=self._initial, id="dialog-input")
            with Horizontal(id="dialog-buttons"):
                yield Button("OK", id="ok", variant="primary")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        input_widget = self.query_one("#dialog-input", Input)
        input_widget.focus()
        input_widget.cursor_position = len(self._initial)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted, "#dialog-input")
    def _submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    @on(Button.Pressed, "#ok")
    def _ok_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(self.query_one("#dialog-input", Input).value)

    @on(Button.Pressed, "#cancel")
    def _cancel_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)


class MenuScreen(ModalScreen[Optional[str]]):
    """A right-click menu: dismisses the chosen option's id, or None."""

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, title: str, options: Sequence[Tuple[str, str]]) -> None:
        super().__init__()
        self._title = title
        self._options = options

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._title, id="dialog-title")
            yield OptionList(*(Option(label, id=key) for key, label in self._options), id="menu")

    def on_mount(self) -> None:
        self.query_one("#menu", OptionList).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(OptionList.OptionSelected, "#menu")
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        # A click outside the menu closes it.
        if not self.query_one("#dialog").region.contains(event.screen_x, event.screen_y):
            self.dismiss(None)


DOUBLE_CLICK_SECONDS = 0.5


class PanelTable(DataTable):
    """A table where a click only selects a row; a double click (or Enter) acts on it.

    A plain DataTable treats a click on the highlighted row as Enter, so a
    single click opened a pane, and a few quick clicks opened several.
    """

    _clicked_once = False
    _last_click: Tuple[float, int] = (0.0, -1)

    def _post_selected_message(self) -> None:
        # DataTable's own hook for "the highlighted row was chosen" (checked against Textual 8.2.8); if a
        # Textual release renames it, the double-click tests fail.
        if not self._clicked_once:
            super()._post_selected_message()

    async def _on_click(self, event: events.Click) -> None:
        chain = getattr(event, "chain", None)
        if chain is None:  # Textual without Click.chain: time it here.
            row = event.style.meta.get("row", -1) if event.style else -1
            now = time.monotonic()
            last, last_row = self._last_click
            chain = 2 if row == last_row and now - last < DOUBLE_CLICK_SECONDS else 1
            self._last_click = (0.0, -1) if chain > 1 else (now, row)
        # Textual also runs each base class's own click handler unless this stops it; DataTable's would select.
        event.prevent_default()
        meta = event.style.meta if event.style else {}
        if chain >= 2 and meta.get("row", -1) >= 0 and self.row_count:
            event.stop()
            self.cursor_coordinate = Coordinate(min(meta["row"], self.row_count - 1), max(meta.get("column", 0), 0))
            super()._post_selected_message()
            return
        self._clicked_once = True
        try:
            await super()._on_click(event)
        finally:
            self._clicked_once = False


class SessionTable(PanelTable):
    """The sessions table: a right click opens the session's menu instead of selecting the row."""

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button == 3:
            event.stop()
            row = int(event.y + self.scroll_y) - (1 if self.show_header else 0)
            self.app.call_later(self.app.session_right_click, row)  # type: ignore[attr-defined]

    async def _on_click(self, event: events.Click) -> None:
        if event.button == 3:
            event.prevent_default()
            event.stop()
            return
        await super()._on_click(event)


def _suggested_name(row: SessionRow) -> str:
    """An address-book name from the session's title: lowercase letters, digits and '-'."""
    name = "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in row.title.lower())
    name = "-".join(part for part in name.split("-") if part)[:32].strip("-")
    return name if name and not name.startswith("-") else "s-" + row.session[:8]


class ReinsPanel(App[None]):
    """Tables of sessions, tasks, goals and messages, driven by a Backend."""

    CSS = """
    #tabs, TabPane, DataTable {
        height: 1fr;
    }
    #health {
        height: auto;
        padding: 0 1;
        color: $text-muted;
    }
    #health.ok {
        color: $success;
    }
    #health.problem {
        color: $warning;
    }
    #status {
        height: 1;
        padding: 0 1;
        color: $text-muted;
    }
    #status.error {
        color: $text-error;
    }
    ConfirmScreen, TextPromptScreen, MenuScreen {
        align: center middle;
    }
    #dialog {
        width: 64;
        max-width: 100%;
        max-height: 100%;
        height: auto;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    #dialog-title {
        margin-bottom: 1;
    }
    #dialog-input {
        width: 100%;
    }
    #dialog-buttons {
        height: auto;
        align: right middle;
        margin-top: 1;
    }
    #dialog-buttons Button {
        margin-left: 2;
    }
    """

    BINDINGS = [
        # Tabs are clickable and listed in the hint, so the narrow footer keeps the actions.
        Binding("1", "show_tab('%s')" % SESSIONS_TAB, "Sessions", show=False),
        Binding("2", "show_tab('%s')" % TASKS_TAB, "Tasks", show=False),
        Binding("3", "show_tab('%s')" % GOALS_TAB, "Goals", show=False),
        Binding("4", "show_tab('%s')" % MESSAGES_TAB, "Messages", show=False),
        Binding("enter", "activate", "Open"),
        Binding("n", "new_session", "New"),
        Binding("s", "stop_session", "Stop"),
        Binding("l", "show_log", "Log"),
        Binding("t", "open_terminal", "Shell"),
        Binding("m", "send_message", "Msg"),
        Binding("c", "cancel_callback", "Cancel"),
        Binding("o", "session_menu", "Menu", show=False),
        Binding("a", "toggle_archived", "Archived", show=False),
        # One key for where Enter, l and t put their pane; the footer shows the current choice.
        Binding("d", "split('right')", "Split ↓"),
        Binding("d", "split('down')", "Split →"),
    ]

    def __init__(self, backend: Backend, refresh_seconds: float = 3.0) -> None:
        super().__init__()
        self._backend = backend
        self._direction = BELOW
        # Actions still running, so a second click while a pane is on its way does not open another.
        self._pending: set = set()
        self._refresh_seconds = max(0.5, float(refresh_seconds))
        self._main_screen: Optional[Any] = None
        self._session_rows: List[SessionRow] = []  # The rows shown, in table order.
        self._show_archived = False
        self._archived_count = 0
        self._task_rows: List[TaskRow] = []
        self._goal_rows: List[GoalRow] = []
        self._message_rows: List[MessageRow] = []
        self._session_titles: Dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Static("checking Reins...", id="health")
        with TabbedContent(id="tabs"):
            with TabPane("Sessions", id=SESSIONS_TAB):
                yield SessionTable(id="sessions-table", cursor_type="row", zebra_stripes=True)
            with TabPane("Tasks", id=TASKS_TAB):
                yield PanelTable(id="tasks-table", cursor_type="row", zebra_stripes=True)
            with TabPane("Goals", id=GOALS_TAB):
                yield PanelTable(id="goals-table", cursor_type="row", zebra_stripes=True)
            with TabPane("Messages", id=MESSAGES_TAB):
                yield PanelTable(id="messages-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self._main_screen = self.screen
        self.query_one("#sessions-table", DataTable).add_columns(
            "", "Session", "Where", "Activity", "Tasks", "Callbacks", "Agent", "Directory"
        )
        self.query_one("#tasks-table", DataTable).add_columns(
            "Task", "Title", "State", "Session", "Callback"
        )
        self.query_one("#goals-table", DataTable).add_columns(
            "Goal", "Session", "Task", "State", "Progress"
        )
        self.query_one("#messages-table", DataTable).add_columns(
            "Message", "From", "To", "Subject", "Sent", "Status"
        )
        self.query_one("#sessions-table", DataTable).focus()
        self.set_interval(self._refresh_seconds, self.refresh_data)
        # `reins doctor` at every start, then now and again: a broken daemon shows here, not as silence.
        self.set_interval(HEALTH_SECONDS, self.check_health)
        self.check_health()
        self.refresh_data()

    # ------------------------------------------------------------------ data

    def check_health(self) -> None:
        self._health_worker()

    @work(thread=True, exclusive=True, group="health")
    def _health_worker(self) -> None:
        try:
            problems = list(self._backend.health())
        except Exception as exc:  # the panel shows it; it never crashes
            problems = ["the check itself failed: %s" % exc]
        if not get_current_worker().is_cancelled:
            self.call_from_thread(self._show_health, problems)

    def _show_health(self, problems: List[str]) -> None:
        bar = self.query_one("#health", Static)
        if problems:
            bar.update("\n".join("! " + problem for problem in problems))
        else:
            bar.update("Reins is ready")
        bar.set_class(not problems, "ok")
        bar.set_class(bool(problems), "problem")

    def refresh_data(self) -> None:
        """Read every backend list off the UI thread, then redraw."""
        self._refresh_worker()

    @work(thread=True, exclusive=True, group="refresh")
    def _refresh_worker(self) -> None:
        worker = get_current_worker()
        backend = self._backend
        data: Dict[str, Optional[Sequence[Any]]] = {}
        error: Optional[str] = None
        for name, read in (
            ("sessions", backend.sessions),
            ("tasks", backend.tasks),
            ("goals", backend.goals),
            ("messages", backend.messages),
        ):
            try:
                data[name] = list(read())
            except Exception as exc:  # the panel shows it; it never crashes
                data[name] = None
                if error is None:
                    error = "%s: %s" % (name, exc)
        if worker.is_cancelled:  # a newer refresh is on its way
            return
        self.call_from_thread(self._render, data, error)

    def _render(self, data: Dict[str, Optional[Sequence[Any]]], error: Optional[str]) -> None:
        fresh = data.get("sessions")
        if fresh is not None:
            every = sorted(fresh, key=_session_sort_key)
            self._session_titles = {row.session: row.title for row in every}
            self._archived_count = sum(1 for row in every if row.archived)
            self._session_rows = [row for row in every if self._show_archived or not row.archived]
            self._fill(
                self.query_one("#sessions-table", DataTable),
                [(row.session, _session_cells(row)) for row in self._session_rows],
            )
        fresh = data.get("tasks")
        if fresh is not None:
            self._task_rows = list(fresh)  # type: List[TaskRow]
            self._fill(
                self.query_one("#tasks-table", DataTable),
                [(row.task, self._task_cells(row)) for row in self._task_rows],
            )
        fresh = data.get("goals")
        if fresh is not None:
            self._goal_rows = list(fresh)  # type: List[GoalRow]
            self._fill(
                self.query_one("#goals-table", DataTable),
                [(row.goal, self._goal_cells(row)) for row in self._goal_rows],
            )
        fresh = data.get("messages")
        if fresh is not None:
            self._message_rows = list(fresh)  # type: List[MessageRow]
            self._fill(
                self.query_one("#messages-table", DataTable),
                [(row.message, _message_cells(row)) for row in self._message_rows],
            )
        if error is not None:
            self._show_status("refresh failed: %s" % error, ok=False)

    @staticmethod
    def _fill(table: DataTable, rows: List[Tuple[str, List[Any]]]) -> None:
        """Replace the rows, keeping the cursor on the same key if it survives."""
        selected: Optional[str] = None
        if table.row_count:
            selected = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        table.clear(columns=False)
        for key, cells in rows:
            table.add_row(*cells, key=key)
        if selected is not None and selected in table.rows:
            table.move_cursor(row=table.get_row_index(selected), animate=False)

    def _task_cells(self, row: TaskRow) -> List[Any]:
        return [
            row.task[:8],
            _fit(row.title, TITLE_CELLS),
            row.state,
            self._session_display(row.session),
            row.callback_state or "",
        ]

    def _goal_cells(self, row: GoalRow) -> List[Any]:
        return [
            row.goal[:8],
            self._session_display(row.session),
            row.task[:8],
            row.state,
            row.progress or "",
        ]

    def _session_display(self, session: Optional[str]) -> str:
        if not session:
            return ""
        return self._session_titles.get(session, session[:8])

    def _show_status(self, message: str, ok: bool = True) -> None:
        bar = self.query_one("#status", Static)
        bar.update(message)
        bar.set_class(not ok, "error")

    # -------------------------------------------------------------- helpers

    def _modal_open(self) -> bool:
        return self._main_screen is not None and self.screen is not self._main_screen

    def _active_tab(self) -> str:
        return self.query_one("#tabs", TabbedContent).active

    def _selected_key(self, table_id: str) -> Optional[str]:
        table = self.query_one("#" + table_id, DataTable)
        if table.row_count == 0:
            return None
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    def _selected_session(self) -> Optional[SessionRow]:
        key = self._selected_key("sessions-table")
        if key is None:
            return None
        for row in self._session_rows:
            if row.session == key:
                return row
        return None

    def _selected_task(self) -> Optional[TaskRow]:
        key = self._selected_key("tasks-table")
        if key is None:
            return None
        for row in self._task_rows:
            if row.task == key:
                return row
        return None

    def _newest_running_task(self, session: str) -> Optional[TaskRow]:
        running = [
            row for row in self._task_rows if row.session == session and row.state in RUNNING_TASK_STATES
        ]
        if not running:
            return None
        return max(running, key=lambda row: row.started_at or 0.0)

    def _run_action(self, label: str, action: Callable[..., ActionResult], *args: Any,
                    once: Optional[Tuple[str, str]] = None, busy: str = "") -> None:
        """Run ``action`` off the UI thread; with ``once``, ignore the same action while it is still running."""
        if once is not None:
            if once in self._pending:
                self._show_status(busy or "still working on it...")
                return
            self._pending.add(once)
            if busy:
                self._show_status(busy)
        self._action_worker(label, action, once, *args)

    @work(thread=True, group="actions")
    def _action_worker(self, label: str, action: Callable[..., ActionResult], once: Optional[Tuple[str, str]],
                       *args: Any) -> None:
        worker = get_current_worker()
        try:
            result = action(*args)
        except Exception as exc:  # the panel shows it; it never crashes
            result = ActionResult(False, "%s failed: %s" % (label, exc))
        if worker.is_cancelled:
            self._pending.discard(once)  # No UI left to tell; still let the same action run again.
            return
        self.call_from_thread(self._finish_action, result, once)

    def _finish_action(self, result: ActionResult, once: Optional[Tuple[str, str]] = None) -> None:
        self._pending.discard(once)
        self._show_status(result.message, ok=result.ok)
        self.refresh_data()

    def _open(self, session: str, title: str, direction: str) -> None:
        self._run_action("open", self._backend.open_session, session, direction,
                         once=("open", session), busy="opening %s..." % title)

    # -------------------------------------------------------------- actions

    def action_show_tab(self, tab_id: str) -> None:
        if self._modal_open():
            return
        self.query_one("#tabs", TabbedContent).active = tab_id
        table_id = _TAB_TABLES.get(tab_id)
        if table_id is not None:
            self.query_one("#" + table_id, DataTable).focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Enter on a focused table; the app-level `enter` binding covers the rest.
        if self._modal_open():
            return
        self._open_selected(self._direction)

    def check_action(self, action: str, parameters: Tuple[object, ...]) -> Optional[bool]:
        if action == "split":
            return bool(parameters) and parameters[0] != self._direction
        return True

    def action_split(self, direction: str) -> None:
        if self._modal_open():
            return
        self._direction = direction
        self.refresh_bindings()
        self._show_status("new panes open " + ("below" if direction == BELOW else "to the right"))

    def action_activate(self) -> None:
        if self._modal_open():
            return
        self._open_selected(self._direction)

    def _open_selected(self, direction: str = BELOW) -> None:
        tab = self._active_tab()
        if tab == SESSIONS_TAB:
            row = self._selected_session()
            if row is None:
                self._show_status("no session selected", ok=False)
                return
            self._open(row.session, row.title, direction)
        elif tab == TASKS_TAB:
            row = self._selected_task()
            if row is None:
                self._show_status("no task selected", ok=False)
                return
            if not row.session:
                self._show_status("task %s has no session" % row.task[:8], ok=False)
                return
            self._open(row.session, row.session[:8], direction)
        else:
            self._show_status("enter opens a session from the Sessions or Tasks tab")

    def action_new_session(self) -> None:
        if self._modal_open():
            return
        row = self._selected_session() if self._active_tab() == SESSIONS_TAB else None
        initial = row.cwd if row is not None and row.cwd else os.getcwd()
        self.push_screen(
            TextPromptScreen("Directory for the new session (agent: claude)", initial=initial),
            self._new_session_ready,
        )

    def _new_session_ready(self, directory: Optional[str]) -> None:
        directory = (directory or "").strip()
        if not directory:
            return
        self._run_action("new session", self._backend.new_session, "claude", directory, once=("new", directory),
                         busy="starting a session in %s..." % directory)

    def action_stop_session(self) -> None:
        if self._modal_open():
            return
        if self._active_tab() != SESSIONS_TAB:
            self._show_status("s stops a session from the Sessions tab (press 1)")
            return
        row = self._selected_session()
        if row is None:
            self._show_status("no session selected", ok=False)
            return
        self._confirm_stop(row)

    def _confirm_stop(self, row: SessionRow) -> None:
        self.push_screen(
            ConfirmScreen("Stop session %s? The conversation is kept." % row.title),
            lambda yes: self._stop_confirmed(row, yes),
        )

    def _stop_confirmed(self, row: SessionRow, yes: bool) -> None:
        if yes:
            self._run_action("stop", self._backend.stop_session, row.session)

    def action_show_log(self) -> None:
        direction = self._direction
        if self._modal_open():
            return
        tab = self._active_tab()
        if tab == TASKS_TAB:
            row = self._selected_task()
            if row is None:
                self._show_status("no task selected", ok=False)
                return
            self._run_action("log", self._backend.show_log, row.task, direction, once=("log", row.task),
                             busy="opening the log of %s..." % row.task[:8])
        elif tab == SESSIONS_TAB:
            row = self._selected_session()
            if row is None:
                self._show_status("no session selected", ok=False)
                return
            task = self._newest_running_task(row.session)
            if task is None:
                self._show_status("%s has no running task" % row.title)
                return
            self._run_action("log", self._backend.show_log, task.task, direction, once=("log", task.task),
                             busy="opening the log of %s..." % task.task[:8])
        else:
            self._show_status("l follows a task from the Sessions or Tasks tab")

    def action_open_terminal(self) -> None:
        """A shell in the selected session's directory (or the task's session's), else here."""
        if self._modal_open():
            return
        direction = self._direction
        cwd = None
        tab = self._active_tab()
        if tab == SESSIONS_TAB:
            row = self._selected_session()
            cwd = row.cwd if row is not None else None
        elif tab == TASKS_TAB:
            task = self._selected_task()
            session = next((row for row in self._session_rows if task is not None and row.session == task.session), None)
            cwd = session.cwd if session is not None else None
        self._run_action("terminal", self._backend.open_terminal, cwd or os.getcwd(), direction)

    def action_send_message(self) -> None:
        if self._modal_open():
            return
        if self._active_tab() != SESSIONS_TAB:
            self._show_status("m messages a session from the Sessions tab (press 1)")
            return
        row = self._selected_session()
        if row is None:
            self._show_status("no session selected", ok=False)
            return
        self._ask_message(row)

    def _ask_message(self, row: SessionRow) -> None:
        self.push_screen(
            TextPromptScreen("Message for %s" % row.title),
            lambda text: self._message_ready(row, text),
        )

    def _message_ready(self, row: SessionRow, text: Optional[str]) -> None:
        text = (text or "").strip()
        if not text:
            return
        self._run_action("message", self._backend.send_message, row.session, text)

    def action_cancel_callback(self) -> None:
        if self._modal_open():
            return
        if self._active_tab() != TASKS_TAB:
            self._show_status("c cancels a callback from the Tasks tab (press 2)")
            return
        row = self._selected_task()
        if row is None:
            self._show_status("no task selected", ok=False)
            return
        if not row.callback or row.callback_state != "pending":
            self._show_status("task %s has no pending callback" % row.task[:8])
            return
        self.push_screen(
            ConfirmScreen(
                "Cancel the pending callback %s of task %s?" % (row.callback[:8], row.task[:8])
            ),
            lambda yes: self._cancel_confirmed(row, yes),
        )

    # ------------------------------------------------------------ right click

    def session_right_click(self, row_index: int) -> None:
        """Right click on a session: its menu (open, fork, pin, name, copy, stop, archive)."""
        if self._modal_open() or not 0 <= row_index < len(self._session_rows):
            return
        table = self.query_one("#sessions-table", DataTable)
        table.focus()
        table.move_cursor(row=row_index)
        self.open_session_menu(self._session_rows[row_index])

    def action_session_menu(self) -> None:
        if self._modal_open():
            return
        if self._active_tab() != SESSIONS_TAB:
            self._show_status("o opens a session's menu from the Sessions tab (press 1)")
            return
        row = self._selected_session()
        if row is None:
            self._show_status("no session selected", ok=False)
            return
        self.open_session_menu(row)

    def action_toggle_archived(self) -> None:
        if self._modal_open():
            return
        self._show_archived = not self._show_archived
        if self._show_archived:
            self._show_status("showing archived sessions (a hides them)" if self._archived_count
                              else "no archived sessions")
        else:
            self._show_status("archived sessions hidden (a shows them)")
        self.refresh_data()

    def open_session_menu(self, row: SessionRow) -> None:
        self.push_screen(MenuScreen(row.title, _session_menu(row)), lambda choice: self._menu_chosen(row, choice))

    def _menu_chosen(self, row: SessionRow, choice: Optional[str]) -> None:
        backend = self._backend
        if choice in ("open-below", "open-right"):
            self._open(row.session, row.title, BELOW if choice == "open-below" else BESIDE)
        elif choice == "fork":
            self._run_action("fork", backend.fork_session, row.session, self._direction, once=("fork", row.session),
                             busy="forking %s..." % row.title)
        elif choice == "shell":
            self._run_action("terminal", backend.open_terminal, row.cwd or os.getcwd(), self._direction)
        elif choice == "message":
            self._ask_message(row)
        elif choice in ("pin", "unpin"):
            self._run_action("pin", backend.pin_session, row.session, choice == "pin")
        elif choice == "copy-address" and row.address:
            self._copy(row.address, "address")
        elif choice == "copy-id":
            self._copy(row.session, "Claude session id")
        elif choice == "name":
            self.push_screen(
                TextPromptScreen("Name for %s (lowercase letters, digits, - or _)" % row.title,
                                 initial=_suggested_name(row)),
                lambda name: self._name_given(row, name),
            )
        elif choice == "rename":
            self.push_screen(
                TextPromptScreen("New name for @%s (lowercase letters, digits, - or _)" % row.address,
                                 initial=row.address or ""),
                lambda name: self._name_given(row, name, rename=True),
            )
        elif choice == "stop":
            self._confirm_stop(row)
        elif choice == "archive":
            if row.state in (sessions.ATTACHED, sessions.RUNNING):
                self.push_screen(
                    ConfirmScreen("Stop and archive %s? The conversation is kept." % row.title),
                    lambda yes: yes and self._run_action("archive", backend.archive_session, row.session, True),
                )
            else:
                self._run_action("archive", backend.archive_session, row.session, True)
        elif choice == "unarchive":
            self._run_action("unarchive", backend.archive_session, row.session, False)

    def _name_given(self, row: SessionRow, name: Optional[str], rename: bool = False) -> None:
        name = (name or "").strip()
        if name and not (rename and name == row.address):
            self._name_worker(row.session, name, rename)

    @work(thread=True, group="actions")
    def _name_worker(self, session: str, name: str, rename: bool = False) -> None:
        try:
            result = self._backend.name_session(session, name, rename)
        except Exception as exc:
            self.call_from_thread(self._show_status, "name: %s" % exc, False)
            return
        if result.ok:
            self.call_from_thread(self._copy, name, "address")
            self.call_from_thread(self.refresh_data)
        else:
            self.call_from_thread(self._show_status, result.message, False)

    def _copy(self, text: str, what: str) -> None:
        # OSC 52: Zellij passes it to the terminal, which puts it on the clipboard if it allows that.
        self.copy_to_clipboard(text)
        self._show_status("copied %s %s (if paste is empty, your terminal blocks clipboard access)" % (what, text))

    def _cancel_confirmed(self, row: TaskRow, yes: bool) -> None:
        if yes and row.callback:
            self._run_action("cancel", self._backend.cancel_callback, row.callback)

    def action_refresh_now(self) -> None:
        if self._modal_open():
            return
        self.refresh_data()


def run_panel(backend: Backend, refresh_seconds: float = 3.0) -> int:
    """Run the panel until the user quits (ctrl+q); return a process exit code."""
    result = ReinsPanel(backend, refresh_seconds=refresh_seconds).run()
    return result if isinstance(result, int) else 0
