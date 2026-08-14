"""Telegram webhook bridge for the Intervals.icu MCP server.

The bridge is deliberately read-only. It lets an approved Telegram user ask a
model through AgentRouter to analyse Intervals.icu data, but it never exposes
create, update, delete, or bulk tools to the model. This keeps a lost Telegram
session or an ambiguous prompt from modifying training data.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import jwt
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
logging.getLogger("httpx" ).setLevel(logging.WARNING)

# These tools do not change Intervals.icu data. New write support must be added
# separately behind an explicit, one-time confirmation flow.
READ_ONLY_TOOLS = {
    "icu_get_recent_activities",
    "icu_get_activities_by_date",
    "icu_get_activity_details",
    "icu_search_activities",
    "icu_search_activities_full",
    "icu_get_activities_around",
    "icu_get_activity_intervals",
    "icu_get_activity_streams",
    "icu_get_best_efforts",
    "icu_search_intervals",
    "icu_get_power_histogram",
    "icu_get_hr_histogram",
    "icu_get_pace_histogram",
    "icu_get_gap_histogram",
    "icu_list_athletes",
    "icu_get_athlete_profile",
    "icu_get_fitness_summary",
    "icu_get_fitness_chart",
    "icu_get_wellness_data",
    "icu_get_wellness_for_date",
    "icu_get_calendar_events",
    "icu_get_upcoming_workouts",
    "icu_get_event",
    "icu_get_annual_training_plan",
    "icu_get_power_curves",
    "icu_get_hr_curves",
    "icu_get_pace_curves",
    "icu_get_workout_library",
    "icu_get_workouts_in_folder",
    "icu_get_gear_list",
    "icu_get_sport_settings",
    "icu_get_activity_messages",
    "icu_get_custom_items",
    "icu_get_custom_item",
}

SYSTEM_PROMPT = """You are Sliwai Coach, a personal endurance-training assistant.
Reply in the same language as the user, preferably concise and practical.
Use the provided Intervals.icu tools when current training data is required.
The available tools are strictly read-only: never claim that you created,
updated, deleted, or scheduled anything in Intervals.icu. You may propose a
workout or calendar change as a draft, but must clearly say it is only a draft.
Do not diagnose medical conditions. When a user reports pain, illness, injury,
or unusual symptoms, recommend appropriate professional medical advice rather
than making a diagnosis. Be transparent when data is unavailable."""


class Settings(BaseSettings):
    """Runtime configuration loaded only from environment variables."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str
    telegram_webhook_secret: str = Field(min_length=16)
    allowed_telegram_user_ids: str = Field(min_length=1)

    agentrouter_api_key: str
    # Keys from agentrouter.org/console/token use the documented OpenAI-compatible route.
    agentrouter_base_url: str = "https://agentrouter.org/v1"
    agentrouter_model: str
    agentrouter_user_agent: str = "SliwaiCoach-Telegram/1.0"

    mcp_url: str
    mcp_jwt_secret: str = Field(min_length=32)
    mcp_jwt_issuer: str = "telegram-intervals-bot"
    mcp_jwt_audience: str = "intervals-icu-mcp"

    public_base_url: str | None = None
    auto_set_webhook: bool = False
    request_timeout_seconds: float = 45.0
    conversation_max_messages: int = Field(default=12, ge=2, le=30)

    @property
    def allowed_user_ids(self) -> set[int]:
        values: set[int] = set()
        for raw_value in self.allowed_telegram_user_ids.split(","):
            raw_value = raw_value.strip()
            if raw_value:
                values.add(int(raw_value))
        return values

    @property
    def agentrouter_chat_url(self) -> str:
        return f"{self.agentrouter_base_url.rstrip('/')}/chat/completions"

    @property
    def webhook_url(self) -> str:
        if not self.public_base_url:
            raise ValueError("PUBLIC_BASE_URL is required when AUTO_SET_WEBHOOK=true")
        return f"{self.public_base_url.rstrip('/')}/telegram/webhook"


settings = Settings()
conversation_history: dict[int, deque[dict[str, Any]]] = defaultdict(deque)
user_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
recent_update_ids: deque[int] = deque(maxlen=1_000)


def _make_mcp_token() -> str:
    """Issue a short-lived service token used only between the bot and MCP."""

    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": settings.mcp_jwt_issuer,
            "aud": settings.mcp_jwt_audience,
            "sub": "telegram-bot-service",
            "client_id": "telegram-bot-service",
            "scope": "mcp:read",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        settings.mcp_jwt_secret,
        algorithm="HS256",
    )


def _mcp_client() -> Client:
    transport = StreamableHttpTransport(
        url=settings.mcp_url,
        headers={"Authorization": f"Bearer {_make_mcp_token()}"},
    )
    return Client(transport, timeout=settings.request_timeout_seconds)


def _as_openai_tool(tool: Any) -> dict[str, Any]:
    """Convert an MCP tool schema into OpenAI-compatible function-call schema."""

    data = tool.model_dump(by_alias=True, mode="json")
    return {
        "type": "function",
        "function": {
            "name": data["name"],
            "description": data.get("description") or "",
            "parameters": data.get("inputSchema") or {"type": "object", "properties": {}},
        },
    }


def _serialise_mcp_result(result: Any) -> str:
    """Return a compact JSON result that AgentRouter can pass to the model."""

    if getattr(result, "structured_content", None) is not None:
        payload = result.structured_content
    elif hasattr(result, "model_dump"):
        payload = result.model_dump(by_alias=True, mode="json")
    else:
        payload = str(result)
    return json.dumps(payload, ensure_ascii=False, default=str)


def _message_content(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()
    return "Не получил текстового ответа от модели. Попробуйте сформулировать вопрос иначе."


def _parse_agentrouter_body(response: httpx.Response) -> dict[str, Any]:
    """Parse a normal JSON response or a single event-stream JSON payload."""

    raw = response.text.strip()
    if not raw:
        raise RuntimeError("AgentRouter returned an empty response")
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        event_payloads = [
            line.removeprefix("data:").strip()
            for line in raw.splitlines()
            if line.startswith("data:") and line.removeprefix("data:").strip() != "[DONE]"
        ]
        if not event_payloads:
            content_type = response.headers.get("content-type", "unknown")
            raise RuntimeError(f"AgentRouter returned non-JSON content ({content_type})") from None
        try:
            body = json.loads(event_payloads[-1])
        except json.JSONDecodeError as exc:
            raise RuntimeError("AgentRouter returned an invalid event-stream payload") from exc
    if not isinstance(body, dict):
        raise RuntimeError("AgentRouter returned a non-object JSON response")
    return body


def _trim_history(user_id: int) -> None:
    history = conversation_history[user_id]
    while len(history) > settings.conversation_max_messages:
        history.popleft()


async def _call_agentrouter(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
    payload = {
        "model": settings.agentrouter_model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
        "stream": False,
        "temperature": 0.25,
        "max_tokens": 1_200,
    }
    headers = {
        "Authorization": f"Bearer {settings.agentrouter_api_key}",
        "User-Agent": settings.agentrouter_user_agent,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        response = await client.post(settings.agentrouter_chat_url, headers=headers, json=payload)
        if response.is_error:
            logger.warning(
                "AgentRouter request failed: status=%s model=%s",
                response.status_code,
                settings.agentrouter_model,
            )
        response.raise_for_status()
        body = _parse_agentrouter_body(response)
    choices = body.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise RuntimeError("AgentRouter returned no choices")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise RuntimeError("AgentRouter returned an invalid message")
    return message


async def _answer_user(user_id: int, user_text: str) -> str:
    """Run a bounded, read-only tool-calling loop for one Telegram message."""

    async with _mcp_client() as mcp_client:
        mcp_tools = await mcp_client.list_tools()
        openai_tools = [_as_openai_tool(tool) for tool in mcp_tools if tool.name in READ_ONLY_TOOLS]
        messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
        messages.extend(conversation_history[user_id])
        messages.append({"role": "user", "content": user_text})

        for _ in range(4):
            assistant_message = await _call_agentrouter(messages, openai_tools)
            messages.append(assistant_message)
            tool_calls = assistant_message.get("tool_calls") or []
            if not tool_calls:
                answer = _message_content(assistant_message)
                conversation_history[user_id].append({"role": "user", "content": user_text})
                conversation_history[user_id].append({"role": "assistant", "content": answer})
                _trim_history(user_id)
                return answer

            for call in tool_calls:
                function = call.get("function") if isinstance(call, dict) else None
                name = function.get("name") if isinstance(function, dict) else None
                raw_arguments = function.get("arguments", "{}") if isinstance(function, dict) else "{}"
                try:
                    arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
                    if not isinstance(arguments, dict):
                        raise ValueError("tool arguments must be an object")
                    if name not in READ_ONLY_TOOLS:
                        raise PermissionError("This tool is not allowed in the Telegram bot")
                    result = await mcp_client.call_tool(name, arguments)
                    tool_content = _serialise_mcp_result(result)
                except Exception as exc:  # A tool error should remain visible to the model, not crash the webhook.
                    logger.warning("MCP tool call failed: %s", type(exc).__name__)
                    tool_content = json.dumps({"error": "Tool call failed", "type": type(exc).__name__})

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", "unknown"),
                        "content": tool_content,
                    }
                )

    return "Не удалось завершить анализ за допустимое число шагов. Попробуйте задать более узкий вопрос."


async def _diagnose_integrations() -> str:
    """Test MCP and AgentRouter connectivity without exposing credentials."""

    report: list[str] = []
    try:
        async with _mcp_client() as mcp_client:
            tools = await mcp_client.list_tools()
        allowed_count = sum(tool.name in READ_ONLY_TOOLS for tool in tools)
        report.append(f"MCP: подключён, доступно инструментов только для чтения: {allowed_count}.")
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        logger.warning("MCP diagnostic failed: status=%s", status)
        if status == 401:
            report.append(
                "MCP: HTTP 401 — JWT-секрет в сервисах MCP и Telegram не совпадает."
            )
        elif status == 403:
            report.append("MCP: HTTP 403 — JWT принят, но доступ к MCP отклонён сервером.")
        elif status == 404:
            report.append("MCP: не найден по MCP_URL. Проверьте путь /mcp.")
        else:
            report.append(f"MCP: HTTP-ошибка {status}.")
    except Exception as exc:
        logger.exception("MCP diagnostic failed: %s", type(exc).__name__)
        report.append(f"MCP: ошибка подключения ({type(exc).__name__}).")

    try:
        response = await _call_agentrouter(
            [
                {"role": "system", "content": "Reply with exactly OK."},
                {"role": "user", "content": "Connection check"},
            ],
            [],
        )
        report.append(f"AgentRouter: подключён, ответ: {_message_content(response)[:60]}.")
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        logger.warning("AgentRouter diagnostic failed: status=%s", status)
        if status in {401, 403}:
            report.append("AgentRouter: ключ отклонён или не имеет доступа к выбранной модели.")
        elif status == 404:
            report.append("AgentRouter: не найден API-адрес или выбранная модель.")
        elif status == 400:
            report.append("AgentRouter: отклонил параметры или идентификатор модели.")
        else:
            report.append(f"AgentRouter: HTTP-ошибка {status}.")
    except RuntimeError as exc:
        logger.warning("AgentRouter diagnostic failed: %s", exc)
        report.append(f"AgentRouter: {exc}.")
    except Exception as exc:
        logger.exception("AgentRouter diagnostic failed: %s", type(exc).__name__)
        report.append(f"AgentRouter: ошибка подключения ({type(exc).__name__}).")
    return "\n".join(report)


async def _telegram_api(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/{method}"
    async with httpx.AsyncClient(timeout=settings.request_timeout_seconds) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
        body = response.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram API {method} rejected the request")
    return body


async def _send_text(chat_id: int, text: str) -> None:
    # Telegram messages are limited in size. Splitting at 3,500 characters leaves
    # room for formatting without splitting most normal replies.
    for index in range(0, len(text), 3_500):
        await _telegram_api("sendMessage", {"chat_id": chat_id, "text": text[index : index + 3_500]})


async def _handle_message(message: dict[str, Any]) -> None:
    sender = message.get("from") or {}
    user_id = sender.get("id")
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = message.get("text")

    if not isinstance(user_id, int) or not isinstance(chat_id, int):
        return
    if user_id not in settings.allowed_user_ids:
        logger.warning("Rejected Telegram message from unauthorized user_id=%s", user_id)
        return
    if not isinstance(text, str) or not text.strip():
        await _send_text(chat_id, "Пожалуйста, отправьте текстовый вопрос о тренировках или данных Intervals.icu.")
        return

    cleaned = text.strip()
    if cleaned in {"/start", "/help"}:
        await _send_text(
            chat_id,
            "Я Sliwai Coach. Могу анализировать ваши активности, CTL/ATL/TSB, wellness и план тренировок. "
            "Сейчас работаю в режиме только чтение: предложу план, но ничего не изменю в Intervals.icu. "
            "Команда /diagnose безопасно проверит подключения без вывода секретов.",
        )
        return

    if cleaned == "/diagnose":
        async with user_locks[user_id]:
            await _telegram_api("sendChatAction", {"chat_id": chat_id, "action": "typing"})
            await _send_text(chat_id, await _diagnose_integrations())
        return

    async with user_locks[user_id]:
        try:
            await _telegram_api("sendChatAction", {"chat_id": chat_id, "action": "typing"})
            answer = await _answer_user(user_id, cleaned)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            logger.warning("Upstream HTTP error: status=%s", status)
            if status in {401, 403}:
                answer = "AgentRouter отклонил ключ или доступ к модели. Отправьте /diagnose."
            elif status == 404:
                answer = "AgentRouter не нашёл API-адрес или модель. Отправьте /diagnose."
            elif status == 400:
                answer = "AgentRouter отклонил параметры или ID модели. Отправьте /diagnose."
            else:
                answer = f"Внешний сервис вернул HTTP {status}. Отправьте /diagnose."
        except Exception as exc:
            logger.exception("Unexpected Telegram bot error: %s", type(exc).__name__)
            answer = "Не удалось обработать запрос. Попробуйте ещё раз чуть позже."
        await _send_text(chat_id, answer)


async def _process_update(update: dict[str, Any]) -> None:
    message = update.get("message")
    if isinstance(message, dict):
        await _handle_message(message)


async def _configure_webhook() -> None:
    if not settings.auto_set_webhook:
        return
    await _telegram_api(
        "setWebhook",
        {
            "url": settings.webhook_url,
            "secret_token": settings.telegram_webhook_secret,
            "allowed_updates": ["message"],
            "drop_pending_updates": False,
        },
    )
    logger.info("Telegram webhook configured for %s", settings.webhook_url)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logging.getLogger("httpx" ).disabled = True
    await _configure_webhook()
    yield


app = FastAPI(title="Sliwai Coach Telegram Bridge", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "mode": "read-only"}


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    if not x_telegram_bot_api_secret_token or not hmac.compare_digest(
        x_telegram_bot_api_secret_token,
        settings.telegram_webhook_secret,
    ):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    update = await request.json()
    if not isinstance(update, dict):
        raise HTTPException(status_code=400, detail="Invalid Telegram update")

    update_id = update.get("update_id")
    if isinstance(update_id, int):
        if update_id in recent_update_ids:
            return {"ok": True}
        recent_update_ids.append(update_id)

    background_tasks.add_task(_process_update, update)
    return {"ok": True}


def main() -> None:
    """Run the Telegram bridge as a Render web service."""

    import uvicorn

    uvicorn.run("intervals_icu_mcp.telegram_bot:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))


if __name__ == "__main__":
    main()
