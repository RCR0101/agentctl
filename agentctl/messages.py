"""Message identity shared by local delivery tracking and the UI."""

import hashlib


def message_key(text: str) -> str:
    """Terminals may normalize whitespace; retain the whole message when matching."""
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()
