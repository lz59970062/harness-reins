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
from .backend import (AGENT_TITLES, BELOW, BESIDE, ActionResult, Backend, GoalRow, MessageRow, SessionRow, TaskRow,
                      new_session_name, short_id, suggested_name)

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


AGENT_NAMES = AGENT_TITLES
# Harnesses a new session can be named in at once; a Codex thread exists only after its first message.
NAMED_AT_START = ("claude", "pi")
COPY_ID_LABELS = {"claude": "Claude session id", "codex": "Codex thread id", "pi": "Pi session file",
                  "dsh": "DeepSeek Harness session id"}


def _where(row: SessionRow) -> str:
    """State and host in plain words: here, background, Desktop, terminal, ..."""
    if row.agent == "dsh" and row.state == sessions.CLOSED:
        return "DSH app"
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


def _fit_tail(text: str, width: int) -> str:
    """Like ``_fit``, but keeps the end: the last folders of a path tell projects apart."""
    if cell_len(text) <= width:
        return text
    while text and cell_len(text) > width - 1:
        text = text[1:]
    return "…" + text


def _session_cells(row: SessionRow) -> List[Any]:
    # Which session, where it runs and what it is doing come first: the panel is narrow. The first cell sits
    # indented under its folder's row and holds the name given in Reins; the harness's own title is beside it.
    cells: List[Any] = [
        _fit("  %s %s%s" % (_MARKS.get(row.state, "?"), "\u2605 " if row.pinned else "",
                            "@" + row.address if row.address else ""), TREE_CELLS),
        _fit(row.title, TITLE_CELLS),
        "archived" if row.archived else _where(row),
        row.agent,
        # Claude reports "blocked" both for a session waiting on a denied tool call and for one
        # waiting for its first prompt; either way it waits for a person.
        "waiting" if row.activity == sessions.BLOCKED else (row.activity or ""),
        str(row.tasks_running) if row.tasks_running else "",
        str(row.callbacks_pending) if row.callbacks_pending else "",
    ]
    if row.state == sessions.CLOSED or row.archived:
        return [Text(str(cell), style="dim") for cell in cells]
    return cells


def _folder_cells(cwd: str, count: int) -> List[Any]:
    """The row above a folder's sessions: where they run, and how many there are."""
    label = _fit_tail(_shorten_cwd(cwd), TREE_CELLS - 2) if cwd else "(directory unknown)"
    return [Text("\u25be " + label, style="bold"),
            Text("%d session%s" % (count, "" if count == 1 else "s"), style="dim"), "", "", "", "", ""]


def _by_folder(rows: Sequence[SessionRow]) -> List[Tuple[str, List[SessionRow]]]:
    """Sessions grouped by directory; a folder sorts where its first session would (pinned, running, newest)."""
    groups: Dict[str, List[SessionRow]] = {}
    for row in sorted(rows, key=_session_sort_key):
        groups.setdefault(row.cwd or "", []).append(row)
    return list(groups.items())  # dicts keep insertion order: each folder in the order its first session came


def _folder_menu() -> List[Tuple[str, str]]:
    return [("new", "New session here..."), ("shell", "Terminal here")]


def _session_menu(row: SessionRow) -> List[Tuple[str, str]]:
    """What a right click (or o) on a session offers, in the order shown; it depends on the harness.

    Only Claude sessions are stopped from here: Codex unloads idle threads itself and a Pi session ends with
    its pane. DeepSeek Harness sessions have no terminal window to open. Pi has no fork Reins can start.
    """
    running = row.state in (sessions.ATTACHED, sessions.RUNNING)
    elsewhere = row.state in (sessions.FOREIGN, sessions.HEADLESS)
    options: List[Tuple[str, str]] = []
    if row.state != sessions.HEADLESS and row.agent != "dsh":
        options += [("open-below", "Open below"), ("open-right", "Open to the right")]
    if row.agent in ("claude", "codex") and (row.cwd or row.agent == "codex"):
        options.append(("fork", "Fork into a new session"))
    if row.cwd:
        options.append(("shell", "Terminal in its directory"))
    options.append(("message", "Send a message..."))
    options.append(("collaborate", "Start a collaborator..."))
    options.append(("unpin", "Unpin") if row.pinned else ("pin", "Pin to the top"))
    if row.address:
        options += [("copy-address", "Copy address  @%s" % row.address), ("rename", "Rename address...")]
    else:
        options.append(("name", "Name it and copy its address..."))
    options.append(("copy-id", "Copy " + COPY_ID_LABELS.get(row.agent, "session id")))
    stops = running and row.agent == "claude"
    if stops:
        options.append(("stop", "Stop..."))
    if row.archived:
        options.append(("unarchive", "Unarchive"))
    elif not elsewhere and not (row.agent == "pi" and running):
        options.append(("archive", "Stop and archive..." if stops else "Archive"))
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
TREE_CELLS = 24
HEALTH_SECONDS = 60.0
# A folder's row in the sessions table has this key and its directory after it; no session id starts so.
FOLDER_KEY = "folder:"
ALERT_FLASHES = 3
ALERT_FLASH_SECONDS = 0.18

# What a failed action's window is titled; the message under it says why and what to do.
FAILED_TITLES = {
    "open": "Could not open the session",
    "new session": "Could not start the session",
    "collaborator": "Could not start the collaborator",
    "fork": "Could not fork the session",
    "stop": "Could not stop the session",
    "log": "Could not open the log",
    "terminal": "Could not open a terminal",
    "message": "The message was not sent",
    "name": "Could not name the session",
}

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


COPY_HELP = """On the clipboard already? Press Enter. If not, copy the line above by hand:
macOS Terminal: \u2318R, drag over it, \u2318C, \u2318R again
iTerm2: hold \u2325 and drag, then \u2318C"""


class CopiedScreen(ModalScreen[None]):
    """What was just copied, on a line of its own, for terminals that ignore the copy (OSC 52)."""

    BINDINGS = [Binding("escape", "close", show=False), Binding("enter", "close", show=False)]

    def __init__(self, what: str, text: str) -> None:
        super().__init__()
        self._what = what
        self._text = text

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Copied %s:" % self._what, id="dialog-title")
            # A Text, not a string: an address or id must never be read as markup.
            yield Static(Text(self._text, style="bold"), id="copied-text")
            yield Static(Text(COPY_HELP), id="copied-help")
            with Horizontal(id="dialog-buttons"):
                yield Button("OK", id="ok", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#ok", Button).focus()

    def action_close(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#ok")
    def _ok_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(None)


class AlertScreen(ModalScreen[None]):
    """Why an action did not work, in a window that waits for OK, not a line at the bottom one can miss."""

    BINDINGS = [Binding("escape", "close", "Close"), Binding("enter", "close", "Close")]

    def __init__(self, title: str, message: str) -> None:
        super().__init__()
        self._title = title
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="alert"):
            yield Label(Text(self._title, style="bold"), id="dialog-title")
            yield Label(self._message, id="alert-text")
            with Horizontal(id="dialog-buttons"):
                yield Button("OK", id="ok", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#ok", Button).focus()
        # A few quick flashes of the frame, so it is seen even when the eyes are on another pane.
        dialog = self.query_one("#dialog", Vertical)
        for step in range(1, 2 * ALERT_FLASHES + 1):  # On, off, ... ending off: a red frame again.
            self.set_timer(step * ALERT_FLASH_SECONDS, lambda on=step % 2 == 1: dialog.set_class(on, "flash"))

    def action_close(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#ok")
    def _ok_pressed(self, event: Button.Pressed) -> None:
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
    return suggested_name(row.title, row.session)


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
        color: $text;
        background: $error;
        text-style: bold;
    }
    #status.busy {
        color: $text-accent;
        text-style: bold;
    }
    ConfirmScreen, TextPromptScreen, MenuScreen, CopiedScreen, AlertScreen {
        align: center middle;
    }
    #copied-text {
        padding: 1 0;
    }
    #copied-help {
        color: $text-muted;
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
        width: 100%;
        margin-bottom: 1;
    }
    #dialog.alert {
        border: thick $error;
    }
    #alert-text {
        width: 100%;
    }
    #dialog.alert.flash {
        border: thick $warning;
        background: $error 30%;
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
        # Textual's own Ctrl+Q would quit the panel alone and leave the screen without it.
        Binding("ctrl+q", "leave", "Leave", show=False, priority=True),
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
        self._session_rows: List[SessionRow] = []  # The sessions shown, in table order.
        self._table_rows: List[Tuple[str, Optional[SessionRow]]] = []  # Every table row: key, session or None.
        self._taken_names: set = set()
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
            "Folder / name", "Title", "Where", "Agent", "Activity", "Tasks", "Callbacks"
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
            self._taken_names = {row.address for row in every if row.address}
            self._archived_count = sum(1 for row in every if row.archived)
            shown = [row for row in every if self._show_archived or not row.archived]
            table_rows: List[Tuple[str, List[Any]]] = []
            self._table_rows, self._session_rows = [], []
            for cwd, group in _by_folder(shown):
                table_rows.append((FOLDER_KEY + cwd, _folder_cells(cwd, len(group))))
                self._table_rows.append((FOLDER_KEY + cwd, None))
                for row in group:
                    table_rows.append((row.session, _session_cells(row)))
                    self._table_rows.append((row.session, row))
                    self._session_rows.append(row)
            self._fill(self.query_one("#sessions-table", DataTable), table_rows)
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
        """Replace the rows, keeping the cursor on the same key if it survives, else on the first session."""
        selected: Optional[str] = None
        if table.row_count:
            selected = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        table.clear(columns=False)
        for key, cells in rows:
            table.add_row(*cells, key=key)
        if selected is not None and selected in table.rows:
            table.move_cursor(row=table.get_row_index(selected), animate=False)
        else:
            first = next((index for index, (key, _) in enumerate(rows) if not key.startswith(FOLDER_KEY)), 0)
            table.move_cursor(row=first, animate=False)

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
        return self._session_titles.get(session, short_id(session))

    def _show_status(self, message: str, ok: bool = True, busy: bool = False) -> None:
        bar = self.query_one("#status", Static)
        bar.update(message)
        bar.set_class(not ok, "error")
        bar.set_class(ok and busy, "busy")

    def _show_failure(self, label: str, message: str) -> None:
        """A failed action: red in the status line, and a flashing window that says why."""
        self._show_status(message, ok=False)
        # Over a menu or prompt the user opened meanwhile, too: OK goes back to it.
        self.push_screen(AlertScreen(FAILED_TITLES.get(label, "Could not %s" % label), message))

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

    def _selected_folder(self) -> Optional[str]:
        """The directory of the folder row under the cursor ("" when unknown), None on a session or elsewhere."""
        key = self._selected_key("sessions-table")
        if key is None or not key.startswith(FOLDER_KEY):
            return None
        return key[len(FOLDER_KEY):]

    def _selected_directory(self) -> Optional[str]:
        """The selected session's directory, or the selected folder's."""
        row = self._selected_session()
        return row.cwd if row is not None else (self._selected_folder() or None)

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
                self._show_status(busy, busy=True)
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
        self.call_from_thread(self._finish_action, result, once, label)

    def _finish_action(self, result: ActionResult, once: Optional[Tuple[str, str]] = None, label: str = "") -> None:
        self._pending.discard(once)
        if result.ok:
            self._show_status(result.message)
        else:
            self._show_failure(label, result.message)
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
                folder = self._selected_folder() is not None
                self._show_status("a folder: open a session under it, or press n or t to start one here"
                                  if folder else "no session selected", ok=folder)
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
            self._open(row.session, self._session_display(row.session), direction)
        else:
            self._show_status("enter opens a session from the Sessions or Tasks tab")

    def action_new_session(self) -> None:
        if self._modal_open():
            return
        directory = self._selected_directory() if self._active_tab() == SESSIONS_TAB else None
        self._new_session_worker(directory or os.getcwd())

    @work(thread=True, group="actions")
    def _new_session_worker(self, initial: str, partner: Optional[SessionRow] = None) -> None:
        try:
            agents = list(self._backend.new_agents())
        except Exception as exc:
            self.call_from_thread(self._show_failure, "new session" if partner is None else "collaborator", str(exc))
            return
        self.call_from_thread(self._choose_new_agent, agents, initial, partner)

    def _choose_new_agent(self, agents: List[str], initial: str, partner: Optional[SessionRow] = None) -> None:
        """Which harness the new session runs in; with ``partner``, the session is started to work with it."""
        if self._modal_open():
            return
        if not agents:
            self._show_failure("new session" if partner is None else "collaborator",
                               "no Claude Code, Codex or Pi found on PATH")
        elif len(agents) == 1:
            self._ask_new_directory(agents[0], initial, partner)
        else:
            title = "New session in" if partner is None else "Collaborator for %s in" % partner.title
            self.push_screen(MenuScreen(title, [(agent, AGENT_NAMES.get(agent, agent)) for agent in agents]),
                             lambda agent: agent and self._ask_new_directory(agent, initial, partner))

    def _ask_new_directory(self, agent: str, initial: str, partner: Optional[SessionRow] = None) -> None:
        name = AGENT_NAMES.get(agent, agent)
        prompt = ("Directory for the new %s session" % name if partner is None
                  else "Directory for %s's %s collaborator" % (partner.title, name))
        self.push_screen(TextPromptScreen(prompt, initial=initial),
                         lambda directory: self._new_session_ready(agent, directory, partner))

    def _new_session_ready(self, agent: str, directory: Optional[str], partner: Optional[SessionRow] = None) -> None:
        directory = (directory or "").strip()
        if not directory:
            return
        if partner is not None:
            self.push_screen(
                TextPromptScreen("Its first task (optional; Enter alone leaves that to %s)" % partner.title),
                lambda task: self._collaborator_ready(partner, agent, directory, task),
            )
            return
        if agent in NAMED_AT_START:
            self.push_screen(
                TextPromptScreen("Name for the new %s session (its Reins address; clear it for none)"
                                 % AGENT_NAMES.get(agent, agent),
                                 initial=new_session_name(directory, agent, self._taken_names)),
                lambda name: name is not None and self._start_new_session(agent, directory, name.strip()),
            )
            return
        self._start_new_session(agent, directory, "")

    def _start_new_session(self, agent: str, directory: str, name: str) -> None:
        self._run_action("new session", self._backend.new_session, agent, directory, name, once=("new", directory),
                         busy="starting a %s session in %s..." % (AGENT_NAMES.get(agent, agent), directory))

    def _collaborator_ready(self, partner: SessionRow, agent: str, directory: str, task: Optional[str]) -> None:
        if task is None:
            return
        self._run_action("collaborator", self._backend.start_collaborator, partner.session, agent, directory,
                         task.strip(), self._direction, once=("collaborate", partner.session),
                         busy="starting a %s session to work with %s..." % (AGENT_NAMES.get(agent, agent),
                                                                              partner.title))

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
        if row.agent == "codex":
            self._show_status("Codex unloads an idle thread by itself; close its pane with Alt+x")
            return
        if row.agent == "pi":
            self._show_status("a Pi session ends with its pane: close the pane with Alt+x")
            return
        if row.agent == "dsh":
            self._show_status("stop a DeepSeek Harness session in its own app")
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
            cwd = self._selected_directory()
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
        """Right click on a session: its menu (open, fork, pin, name, copy, stop, archive); on a folder, its own."""
        if self._modal_open() or not 0 <= row_index < len(self._table_rows):
            return
        table = self.query_one("#sessions-table", DataTable)
        table.focus()
        table.move_cursor(row=row_index)
        key, row = self._table_rows[row_index]
        if row is None:
            self.open_folder_menu(key[len(FOLDER_KEY):])
        else:
            self.open_session_menu(row)

    def action_session_menu(self) -> None:
        if self._modal_open():
            return
        if self._active_tab() != SESSIONS_TAB:
            self._show_status("o opens a session's menu from the Sessions tab (press 1)")
            return
        row = self._selected_session()
        if row is None:
            folder = self._selected_folder()
            if folder is None:
                self._show_status("no session selected", ok=False)
            else:
                self.open_folder_menu(folder)
            return
        self.open_session_menu(row)

    def action_leave(self) -> None:
        """Ctrl+Q: leave or end the whole screen, for keyboards where Alt+d / Alt+q do not arrive."""
        if self._modal_open():
            return
        self.push_screen(
            MenuScreen("Leave Reins", [
                ("detach", "Leave; it keeps running, reins tui comes back"),
                ("end", "End the screen; Claude and Codex keep running, Pi in it ends"),
            ]),
            lambda choice: choice and self._run_action("leave", self._backend.leave, choice == "end"),
        )

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

    def open_folder_menu(self, cwd: str) -> None:
        title = _shorten_cwd(cwd) or "(directory unknown)"
        self.push_screen(MenuScreen(title, _folder_menu()), lambda choice: self._folder_chosen(cwd, choice))

    def _folder_chosen(self, cwd: str, choice: Optional[str]) -> None:
        if choice == "new":
            self._new_session_worker(cwd or os.getcwd())
        elif choice == "shell":
            self._run_action("terminal", self._backend.open_terminal, cwd or os.getcwd(), self._direction)

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
        elif choice == "collaborate":
            self._new_session_worker(row.cwd or os.getcwd(), row)
        elif choice in ("pin", "unpin"):
            self._run_action("pin", backend.pin_session, row.session, choice == "pin")
        elif choice == "copy-address" and row.address:
            self._copy(row.address, "address")
        elif choice == "copy-id":
            self._copy(row.session, COPY_ID_LABELS.get(row.agent, "session id"))
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
            if row.agent == "claude" and row.state in (sessions.ATTACHED, sessions.RUNNING):
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
            self.call_from_thread(self._show_failure, "name", result.message)

    def _copy(self, text: str, what: str) -> None:
        # OSC 52: Zellij passes it to the terminal, which puts it on the clipboard if it allows that. Nothing
        # says whether it did, and macOS Terminal never does, so the text is also shown to copy by hand.
        self.copy_to_clipboard(text)
        self._show_status("copied %s %s" % (what, text))
        self.push_screen(CopiedScreen(what, text))

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
