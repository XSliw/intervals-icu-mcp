"""Tests for the Telegram bridge's local security boundaries."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator

import jwt
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def telegram_bot(monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    """Import the bridge with non-secret, deterministic test configuration."""

    values = {
        "TELEGRAM_BOT_TOKEN": "123456:bot-token-for-tests-only",
        "TELEGRAM_WEBHOOK_SECRET": "telegram-webhook-secret-for-tests-only",
        "ALLOWED_TELEGRAM_USER_IDS": "12345, 67890",
        "AGENTROUTER_API_KEY": "agentrouter-test-key",
        "AGENTROUTER_MODEL": "test-model",
        "MCP_URL": "https://mcp.example.test/mcp",
        "MCP_JWT_SECRET": "mcp-jwt-secret-for-tests-only-must-be-long",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AUTO_SET_WEBHOOK", raising=False)
    sys.modules.pop("intervals_icu_mcp.telegram_bot", None)
    module = importlib.import_module("intervals_icu_mcp.telegram_bot")
    yield module
    sys.modules.pop("intervals_icu_mcp.telegram_bot", None)


def test_allowed_ids_are_parsed(telegram_bot: object) -> None:
    assert telegram_bot.settings.allowed_user_ids == {12345, 67890}


def test_mcp_service_token_has_expected_claims(telegram_bot: object) -> None:
    token = telegram_bot._make_mcp_token()
    claims = jwt.decode(
        token,
        telegram_bot.settings.mcp_jwt_secret,
        algorithms=["HS256"],
        issuer="telegram-intervals-bot",
        audience="intervals-icu-mcp",
    )
    assert claims["sub"] == "telegram-bot-service"


def test_only_read_tools_are_exposed(telegram_bot: object) -> None:
    assert "icu_get_recent_activities" in telegram_bot.READ_ONLY_TOOLS
    assert all(
        not any(marker in tool_name for marker in ("create", "update", "delete", "bulk"))
        for tool_name in telegram_bot.READ_ONLY_TOOLS
    )


def test_webhook_rejects_wrong_secret(telegram_bot: object) -> None:
    with TestClient(telegram_bot.app) as client:
        response = client.post(
            "/telegram/webhook",
            headers={"X-Telegram-Bot-Api-Secret-Token": "incorrect-secret"},
            json={"update_id": 1},
        )
    assert response.status_code == 403
