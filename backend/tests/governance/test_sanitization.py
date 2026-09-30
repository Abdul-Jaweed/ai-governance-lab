"""Tests for argument sanitization (spec section 13)."""

from __future__ import annotations

from app.governance.sanitization import (
    REDACTED,
    hash_arguments,
    is_sensitive_key,
    sanitize_arguments,
)


def test_flat_sensitive_key_is_redacted():
    result = sanitize_arguments({"customer_id": "123", "api_key": "sk-live-abc"})
    assert result == {"customer_id": "123", "api_key": REDACTED}


def test_header_style_and_case_variants_are_redacted():
    assert sanitize_arguments({"X-API-Key": "abc"}) == {"X-API-Key": REDACTED}
    assert sanitize_arguments({"Authorization": "Bearer x"}) == {
        "Authorization": REDACTED
    }
    assert sanitize_arguments({"DB_PASSWORD": "hunter2"}) == {"DB_PASSWORD": REDACTED}


def test_nested_sensitive_key_is_redacted():
    result = sanitize_arguments(
        {"user": {"email": "a@b.com", "password": "hunter2"}}
    )
    assert result == {"user": {"email": "a@b.com", "password": REDACTED}}


def test_lists_of_dicts_are_traversed():
    result = sanitize_arguments(
        {"items": [{"name": "a", "secret": "s1"}, {"name": "b"}]}
    )
    assert result == {
        "items": [{"name": "a", "secret": REDACTED}, {"name": "b"}]
    }


def test_redaction_not_hashing_for_sensitive_values():
    result = sanitize_arguments({"access_token": "abcdef"})
    assert result["access_token"] == REDACTED
    assert "abcdef" not in str(result)


def test_non_sensitive_values_are_preserved_untouched():
    payload = {"count": 3, "flag": True, "name": "ok", "nested": [1, 2, "x"]}
    assert sanitize_arguments(payload) == payload


def test_is_sensitive_key_matches_known_tokens_only():
    for key in ("password", "api_key", "refresh_token", "private_key", "cookie"):
        assert is_sensitive_key(key)
    for key in ("customer_id", "email", "amount", "token_count"):
        assert not is_sensitive_key(key)


def test_hash_is_deterministic_and_key_order_independent():
    a = hash_arguments({"b": 2, "a": 1})
    b = hash_arguments({"a": 1, "b": 2})
    assert a == b
    assert len(a) == 64  # sha-256 hex digest
    assert hash_arguments({"a": 1}) != hash_arguments({"a": 2})


def test_hash_handles_non_json_scalars_without_raising():
    digest = hash_arguments({"when": object()})
    assert isinstance(digest, str) and len(digest) == 64
