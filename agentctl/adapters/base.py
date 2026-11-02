from typing import Any, Protocol


class ControlError(Exception):
    """A control action (send/approve/...) can't be done safely; message is user-facing."""


Plan = list[tuple[int, list[int], str]]  # per question: options, picks (1-based), typed text


def plan_answers(questions: list[dict[str, Any]], answers: list[dict[str, Any]]) -> Plan:
    """Check answers to an agent's questions: picks in range, one pick or a typed answer
    unless the question takes several, typing only where the question allows it."""
    if len(answers) != len(questions):
        raise ControlError(f"{len(questions)} questions, {len(answers)} answers")
    plan: Plan = []
    for q, a in zip(questions, answers, strict=True):
        n, multi = len(q.get("options") or []), bool(q.get("multiSelect"))
        picks = sorted({int(c) for c in a.get("choices") or []})
        typed = " ".join(str(a.get("text") or "").split())  # a newline would submit the menu
        if any(not 1 <= c <= n for c in picks):
            raise ControlError(f"choices go from 1 to {n}: {q.get('question')}")
        if typed and q.get("isOther") is False:
            raise ControlError(f"this one takes no typed answer: {q.get('question')}")
        if not picks and not typed or not multi and len(picks) + bool(typed) > 1:
            raise ControlError(f"pick {'any' if multi else 'one'} or type: {q.get('question')}")
        plan.append((n, picks, typed))
    return plan


def answer_summary(questions: list[dict[str, Any]], plan: Plan) -> str:
    return "; ".join(
        ", ".join([str(q["options"][c - 1].get("label")) for c in picks] + [typed] * bool(typed))
        for q, (_, picks, typed) in zip(questions, plan, strict=True)
    )


class Adapter(Protocol):
    source: str

    async def discover(self) -> None: ...
    async def watch(self) -> None: ...
    async def send(self, session_id: str, text: str) -> None: ...
    async def approve(self, session_id: str, choice: int | None, allow: bool) -> str: ...
    async def interrupt(self, session_id: str) -> None: ...
    async def verified(self, session_id: str) -> tuple[str | None, str]: ...
    def holder(self, session_id: str) -> int | None: ...
    def hydrate(self, session_id: str) -> None: ...  # load full recent history on demand
