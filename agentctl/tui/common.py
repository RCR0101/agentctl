"""Small pieces the dashboard's screens share."""

import dataclasses
from collections.abc import Awaitable, Callable
from typing import Any

from textual.binding import Binding, BindingType

from agentctl.messages import message_key

Rpc = Callable[..., Awaitable[Any]]


def nocase(bindings: list[Binding]) -> list[BindingType]:
    """Letter keys also work shifted or with caps lock on, and after a phone keyboard
    capitalises the first letter."""
    out: list[BindingType] = []
    for b in bindings:
        keys = b.key.split(",")
        upper = [k.upper() for k in keys if len(k) == 1 and k.islower() and k.upper() not in keys]
        out.append(dataclasses.replace(b, key=",".join(keys + upper)) if upper else b)
    return out


def sent_key(text: str) -> str:
    """Match the entire message, allowing terminal whitespace normalization."""
    return message_key(text)
