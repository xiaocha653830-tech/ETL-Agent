"""快照脱敏测试（SPEC §8：证据落库前必过）。"""

from app.services.redaction import (
    _MAX_DEPTH,
    _MAX_ITEMS,
    _MAX_TEXT,
    REDACTED,
    TRUNCATED,
    is_secret_key,
    redact_snapshot,
)


def test_secret_keys_are_recognized():
    for key in (
        "password",
        "passwd",
        "db_pwd",
        "secret",
        "secret_key",
        "access_key",
        "api_key",
        "apikey",
        "private_key",
        "signing_key",
        "token",
        "credential",
        "Authorization",
        "jdbc_connection_string",
    ):
        assert is_secret_key(key), key


def test_ordinary_keys_are_not_over_redacted():
    """误脱敏会让快照失去排障价值——它不是免费的。"""
    for key in (
        "idempotency_key",
        "operation_key",
        "primary_key",
        "target_table",
        "user_name",
        "host",
        "database",
        "artifact",
    ):
        assert not is_secret_key(key), key


def test_redacts_by_key_not_by_value():
    """按值脱敏挡不住千变万化的口径；键名是写入方自己起的，可控。"""
    snapshot = redact_snapshot({"password": "looks-innocent", "host": "10.0.0.1"})
    assert snapshot == {"password": REDACTED, "host": "10.0.0.1"}


def test_recurses_into_nested_structures():
    snapshot = redact_snapshot(
        {
            "config": {"url": "jdbc:mysql://u:p@h/db", "auth": {"token": "t"}},
            "steps": [{"api_key": "k"}, {"name": "ok"}],
        }
    )
    assert snapshot["config"]["auth"]["token"] == REDACTED
    assert snapshot["steps"][0]["api_key"] == REDACTED
    assert snapshot["steps"][1]["name"] == "ok"


def test_does_not_mutate_the_input():
    """调用方常直接把业务对象传进来，就地改写会把影响扩散出去。"""
    original = {"password": "p", "nested": {"token": "t"}}
    redact_snapshot(original)
    assert original == {"password": "p", "nested": {"token": "t"}}


def test_depth_is_bounded():
    deep: dict = {"leaf": 1}
    for _ in range(_MAX_DEPTH + 5):
        deep = {"next": deep}

    redacted = redact_snapshot(deep)
    cursor = redacted
    for _ in range(_MAX_DEPTH):
        cursor = cursor["next"]
    assert cursor["next"] == TRUNCATED


def test_collections_are_bounded():
    redacted = redact_snapshot([{"i": index} for index in range(_MAX_ITEMS + 10)])
    assert len(redacted) == _MAX_ITEMS + 1
    assert TRUNCATED in str(redacted[-1])


def test_long_strings_are_truncated():
    redacted = redact_snapshot("x" * (_MAX_TEXT + 100))
    assert len(redacted) == _MAX_TEXT + len(TRUNCATED)


def test_scalars_pass_through():
    assert redact_snapshot({"n": 1, "b": True, "none": None}) == {
        "n": 1,
        "b": True,
        "none": None,
    }
