"""Local review of recorded turn activity and shared Git changes."""

from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Collapsible, Markdown, Static


class Review(ModalScreen[None]):
    """Recorded turn evidence and the project's current working tree, without inference."""

    DEFAULT_CSS = """
    Review { align: center middle; }
    Review > VerticalScroll { width: 95%; max-width: 110; height: 90%;
        border: round $accent; padding: 1 2; background: $surface; }
    Review .section { margin-top: 1; color: $text-muted; text-style: bold; }
    Review Markdown { margin: 0; padding: 0; }
    """
    BINDINGS = [Binding("escape", "dismiss", "close")]

    def __init__(self, title: str, report: dict[str, Any]) -> None:
        super().__init__()
        self.title_text, self.report = title, report

    def compose(self) -> ComposeResult:
        r = self.report
        with VerticalScroll():
            yield Static(Text(f"Review · {self.title_text}", style="bold"))
            yield Static("LATEST OBSERVED REQUEST", classes="section")
            yield Static(Text(r.get("request") or "Request outside the recent activity window"))
            yield Static("AGENT REPLY", classes="section")
            yield Markdown(r.get("reply") or "No reply recorded for this turn yet.")
            yield Static("RECORDED TOOL OUTCOMES", classes="section")
            yield Static(
                Text(
                    f"{r.get('tool_calls', 0)} tool calls · "
                    f"{r.get('tool_failures', 0)} failed results"
                )
            )
            for result in r.get("tool_results") or []:
                label = f"{result['name']} {result['summary']}".splitlines()[0]
                outcome = " · failed" if result["is_error"] else ""
                with Collapsible(title=label + outcome, collapsed=True):
                    yield Static(Text(result["text"], style="red" if result["is_error"] else ""))
            yield Static("FILES EDITED WITH TOOLS IN THIS TURN", classes="section")
            yield Static(Text("\n".join(r.get("files") or []) or "No file edits recorded"))
            yield Static("SHARED PROJECT CHANGES", classes="section")
            yield Static(Text("Current Git changes across all sessions and manual edits.", "dim"))
            yield Static(Text(r.get("project_changes") or "Working tree is clean"))
            if r.get("project_diff_stat"):
                yield Static(Text(r["project_diff_stat"]))
