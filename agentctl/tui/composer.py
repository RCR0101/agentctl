"""Message editing and per-request delivery results for the dashboard."""

from dataclasses import dataclass

from textual import events
from textual.binding import Binding
from textual.message import Message
from textual.widgets import TextArea


@dataclass(frozen=True)
class ControlResult:
    outcome: str
    error: str = ""
    command_id: str | None = None

    def __bool__(self) -> bool:
        return self.outcome != "failed"


class Prompt(TextArea):
    """Message box. Enter submits; a paste keeps its newlines (Textual's Input keeps only the
    first line of a paste, which silently cut multi-line messages). New line: shift/alt+enter,
    ctrl+j, or `\\` then enter (phones have no shift+enter). ↑ on the first line / ↓ on the
    last recall what you sent this session, like a shell. TextArea brings the rest: ctrl+a/e/
    w/u/k, undo, selection."""

    sends: list[str] = []  # what was sent to this session, set when the box opens
    recall = 0  # how far back ↑ has gone; 0 is the draft
    draft = ""
    token = 0
    submitted = False

    BINDINGS = [
        Binding("ctrl+enter,ctrl+s", "submit", "send", show=False),
        Binding("tab", "indent", show=False),
        Binding("shift+tab", "keep_focus", show=False),
        Binding("shift+enter,alt+enter,ctrl+j", "newline", show=False),
        Binding("alt+left,alt+b", "cursor_word_left", show=False),
        Binding("alt+right,alt+f", "cursor_word_right", show=False),
        Binding("super+left", "cursor_line_start", show=False),
        Binding("super+right", "cursor_line_end", show=False),
    ]

    class Submitted(Message):
        def __init__(self, value: str, token: int) -> None:
            super().__init__()
            self.value, self.token = value, token

    def action_submit(self) -> None:
        if not self.submitted and not self.read_only:
            self.submitted = True
            self.post_message(self.Submitted(self.text, self.token))

    def action_indent(self) -> None:
        if not self.read_only:
            self.insert(" " * self.indent_width)

    def action_keep_focus(self) -> None:
        self.focus()

    def action_newline(self) -> None:
        if not self.read_only:
            self.insert("\n")

    def recall_step(self, step: int) -> None:
        pos = min(max(self.recall + step, 0), len(self.sends))
        if pos == self.recall:
            return
        if self.recall == 0:
            self.draft = self.text
        self.recall = pos
        self.load_text(self.sends[-pos] if pos else self.draft)
        self.move_cursor(self.document.end)

    async def _on_key(self, event: events.Key) -> None:
        if self.read_only:
            event.prevent_default()
            event.stop()
            return
        row = self.cursor_location[0]
        if event.key in ("up", "down") and self.sends:
            if (event.key == "up" and row == 0) or (
                event.key == "down" and row == self.document.line_count - 1 and self.recall
            ):
                event.prevent_default()
                event.stop()
                self.recall_step(1 if event.key == "up" else -1)
                return
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            row, col = self.cursor_location
            if col and self.document.get_line(row)[col - 1] == "\\":
                self.replace("\n", (row, col - 1), (row, col))
            else:
                self.action_submit()
        else:
            await super()._on_key(event)
