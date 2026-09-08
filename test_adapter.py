"""Unit tests for the MAX platform adapter.

Run from the Hermes project root with:

    /opt/hermes/.venv/bin/python -m pytest plugins/platforms/max/test_adapter.py -q
"""

import os
import pytest

# Ensure the plugin can import gateway modules when pytest runs from /opt/hermes.


@pytest.fixture(autouse=True)
def _clear_token_env():
    """Keep tests isolated from the real MAX_BOT_TOKEN env var."""
    old = os.environ.pop("MAX_BOT_TOKEN", None)
    yield
    if old is not None:
        os.environ["MAX_BOT_TOKEN"] = old


@pytest.fixture
def adapter():
    from gateway.config import PlatformConfig
    from plugins.platforms.max.adapter import MaxAdapter

    os.environ["MAX_BOT_TOKEN"] = "dummy-token"
    config = PlatformConfig(enabled=True, extra={})
    return MaxAdapter(config)


def test_callback_payload_to_command(adapter):
    assert adapter._callback_payload_to_command("cl:abc:2") == "/clarify abc 2"
    assert adapter._callback_payload_to_command("cl:abc:other") == "/clarify abc other"
    assert adapter._callback_payload_to_command("appr:id:approve") == "/approve"
    assert adapter._callback_payload_to_command("appr:id:session") == "/approve session"
    assert adapter._callback_payload_to_command("appr:id:always") == "/approve always"
    assert adapter._callback_payload_to_command("appr:id:deny") == "/deny"
    assert adapter._callback_payload_to_command("sc:once:id") == "/approve"
    assert adapter._callback_payload_to_command("sc:always:id") == "/approve always"
    assert adapter._callback_payload_to_command("sc:cancel:id") == "/deny"
    assert adapter._callback_payload_to_command("unknown") is None


def test_split_message(adapter):
    short = "hello"
    assert adapter._split_message(short) == [short]

    long_text = "a" * (adapter.MAX_MESSAGE_LENGTH + 100)
    parts = adapter._split_message(long_text)
    assert len(parts) == 2
    assert all(len(p) <= adapter.MAX_MESSAGE_LENGTH for p in parts)
    assert "".join(parts) == long_text


def test_is_duplicate_update(adapter):
    assert adapter._is_duplicate_update("u1") is False
    assert adapter._is_duplicate_update("u1") is True
    assert adapter._is_duplicate_update("u2") is False


def test_check_requirements():
    from plugins.platforms.max.adapter import check_requirements

    os.environ["MAX_BOT_TOKEN"] = "dummy"
    assert check_requirements() is True

    del os.environ["MAX_BOT_TOKEN"]
    assert check_requirements() is False


def test_is_user_allowed():
    from gateway.config import PlatformConfig
    from plugins.platforms.max.adapter import MaxAdapter

    os.environ["MAX_BOT_TOKEN"] = "dummy"

    # Secure default: no allowlist and no allow-all → deny.
    adapter = MaxAdapter(PlatformConfig(enabled=True, extra={}))
    assert adapter._is_user_allowed(12345) is False
    assert adapter._is_user_allowed(None) is False

    # Explicit allowlist.
    adapter = MaxAdapter(
        PlatformConfig(enabled=True, extra={"allow_from": "12345,67890"})
    )
    assert adapter._is_user_allowed(12345) is True
    assert adapter._is_user_allowed("67890") is True
    assert adapter._is_user_allowed(99999) is False
    assert adapter._is_user_allowed(None) is False

    # Wildcard.
    adapter = MaxAdapter(PlatformConfig(enabled=True, extra={"allow_from": "*"}))
    assert adapter._is_user_allowed(12345) is True

    # Allow-all flag.
    adapter = MaxAdapter(
        PlatformConfig(enabled=True, extra={"allow_all": "true"})
    )
    assert adapter._is_user_allowed(12345) is True
    assert adapter._is_user_allowed(None) is True

    # Env vars take precedence over empty config.
    os.environ["MAX_ALLOWED_USERS"] = "11111"
    os.environ["MAX_ALLOW_ALL_USERS"] = ""
    adapter = MaxAdapter(PlatformConfig(enabled=True, extra={}))
    assert adapter._is_user_allowed(11111) is True
    assert adapter._is_user_allowed(12345) is False

    os.environ["MAX_ALLOW_ALL_USERS"] = "1"
    adapter = MaxAdapter(PlatformConfig(enabled=True, extra={}))
    assert adapter._is_user_allowed(12345) is True

    del os.environ["MAX_ALLOWED_USERS"]
    del os.environ["MAX_ALLOW_ALL_USERS"]


def test_env_enablement():
    from plugins.platforms.max.adapter import _env_enablement

    assert _env_enablement() is None

    os.environ["MAX_BOT_TOKEN"] = "dummy"
    os.environ["MAX_HOME_CHANNEL"] = "12345"
    os.environ["MAX_HOME_CHANNEL_NAME"] = "Home"
    os.environ["MAX_POLL_TIMEOUT"] = "45"
    extra = _env_enablement()
    assert extra is not None
    assert extra["token"] == "dummy"
    assert extra["home_channel"]["chat_id"] == "12345"
    assert extra["home_channel"]["name"] == "Home"
    assert extra["poll_timeout"] == 45

    del os.environ["MAX_BOT_TOKEN"]
    del os.environ["MAX_HOME_CHANNEL"]
    del os.environ["MAX_HOME_CHANNEL_NAME"]
    del os.environ["MAX_POLL_TIMEOUT"]
