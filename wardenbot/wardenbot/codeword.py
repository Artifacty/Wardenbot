"""One-time code words for verification screenshots."""

from __future__ import annotations

import secrets

# No 0/O/1/I/L to avoid ambiguity on screenshots.
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def generate_code(length: int = 4, prefix: str = "WH") -> str:
    body = "".join(secrets.choice(_ALPHABET) for _ in range(length))
    return f"{prefix}-{body}"
