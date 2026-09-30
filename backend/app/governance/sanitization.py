"""Argument sanitization and hashing (spec section 13).

Sensitive values are redacted with ``[REDACTED]`` before an event is
persisted; we do not retain hashes of secrets because there is no forensic
need for them in the MVP.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

REDACTED = "[REDACTED]"

# Keys (normalized: lowercased, non-alphanumerics stripped) that contain any
# of these tokens are treated as sensitive. Over-redaction is fail-safe.
SENSITIVE_KEY_TOKENS: frozenset[str] = frozenset(
    {
        "password",
        "apikey",
        "authorization",
        "accesstoken",
        "refreshtoken",
        "secret",
        "privatekey",
        "cookie",
    }
)

_NON_ALNUM = re.compile(r"[^a-z0-9]")


def _normalize_key(key: str) -> str:
    return _NON_ALNUM.sub("", key.lower())


def is_sensitive_key(key: str) -> bool:
    """Return True if *key* names a value that must never be persisted raw."""

    normalized = _normalize_key(key)
    return any(token in normalized for token in SENSITIVE_KEY_TOKENS)


def sanitize_arguments(value: Any) -> Any:
    """Recursively redact sensitive values in dicts, lists and tuples."""

    if isinstance(value, dict):
        return {
            key: (REDACTED if is_sensitive_key(str(key)) else sanitize_arguments(val))
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_arguments(item) for item in value]
    return value


def hash_arguments(arguments: dict[str, Any]) -> str:
    """Return a stable SHA-256 hex digest of *arguments*.

    Key order does not affect the digest; non-JSON values are stringified.
    """

    canonical = json.dumps(
        arguments,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
