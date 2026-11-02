"""Session ids as people see and type them. Kept free of imports so the CLI starts fast."""


def short_id(sid: str) -> str:
    """8 chars to show and type. opencode ids all start with "ses_", so skip that."""
    return sid.removeprefix("ses_")[:8]
