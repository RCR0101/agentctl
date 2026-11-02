"""Approval rules: answer matching permission prompts automatically, as you would.

A rule is one line: `allow` or `deny`, a pattern for what the agent wants to run, and
optionally `in <folder>`:

    allow Read*
    allow Bash: npm test*  in ~/src/api
    deny  Bash: rm -rf *

The pattern is a shell-style glob (`*`, `?`, `[...]`) matched, ignoring case, against the
request as agentctl shows it, e.g. "Bash: npm test -- --watch" or "Edit: src/app.py". A
folder matches that folder and everything under it, and may itself be a glob. Deny rules win
over allow rules. Questions (AskUserQuestion forms) are never answered by a rule.

Rules are stored in the daemon's state and changed only through `agentctl rules`, which the
daemon refuses to agents: an agent can't write itself a rule that approves its own prompts.
"""

import fnmatch
import os
from dataclasses import asdict, dataclass
from typing import Any, Literal

from agentctl.daemon.registry import Store

Action = Literal["allow", "deny"]
KEY = "approval_rules"


@dataclass(frozen=True)
class Rule:
    action: Action
    tool: str
    folder: str | None = None

    def __str__(self) -> str:
        return f"{self.action} {self.tool}" + (f"  in {self.folder}" if self.folder else "")

    def matches(self, tool: str, cwd: str) -> bool:
        if not fnmatch.fnmatchcase(tool.casefold(), self.tool.casefold()):
            return False
        if not self.folder:
            return True
        folder = os.path.expanduser(self.folder).rstrip("/") or "/"
        if any(c in folder for c in "*?["):
            return fnmatch.fnmatchcase(cwd, folder) or fnmatch.fnmatchcase(cwd, folder + "/*")
        return cwd == folder or cwd.startswith(folder.rstrip("/") + "/")


def parse(line: str) -> Rule:
    """`allow Read*` or `deny Bash: rm *  in ~/src`. Raises ValueError on anything else."""
    action, _, rest = line.strip().partition(" ")
    if action not in ("allow", "deny"):
        raise ValueError("a rule starts with allow or deny, e.g. `allow Read*`")
    tool, sep, folder = rest.strip().rpartition(" in ")
    if not sep or not folder.strip().startswith(("~", "/", ".", "*")):  # "in" inside a command
        tool, folder = rest.strip(), ""
    tool, folder = tool.strip(), folder.strip()
    if not tool:
        raise ValueError("say what the rule matches, e.g. `allow Read*` or `deny Bash: rm *`")
    if action == "allow" and tool.strip("*? ") == "":
        raise ValueError("an allow rule must name a tool: `allow *` would approve everything")
    return Rule(action, tool, folder or None)  # type: ignore[arg-type]


def decide(rules: list[Rule], tool: str, cwd: str) -> Rule | None:
    """The rule that answers this request: the first matching deny, else the first allow."""
    for action in ("deny", "allow"):
        for r in rules:
            if r.action == action and r.matches(tool, cwd):
                return r
    return None


class Rules:
    def __init__(self, store: Store | None) -> None:
        self.store = store
        saved: list[dict[str, Any]] = (store.get_json(KEY) if store else None) or []
        self.rules = [Rule(**r) for r in saved]

    def save(self) -> None:
        if self.store:
            self.store.set_json(KEY, [asdict(r) for r in self.rules])

    def rpc(self, p: dict[str, Any]) -> dict[str, Any]:
        op = p.get("op", "list")
        if op == "add":
            rule = parse(str(p.get("rule", "")))
            if rule not in self.rules:
                self.rules.append(rule)
                self.save()
        elif op == "remove":
            index = int(p["index"])
            if not 1 <= index <= len(self.rules):
                raise ValueError(f"no rule {index}; there are {len(self.rules)}")
            del self.rules[index - 1]
            self.save()
        elif op == "test":
            hit = decide(self.rules, str(p["tool"]), str(p.get("cwd") or ""))
            return {"rules": [str(r) for r in self.rules], "decided_by": str(hit) if hit else None}
        elif op != "list":
            raise ValueError(f"unknown rules op {op!r}")
        return {"rules": [str(r) for r in self.rules]}
