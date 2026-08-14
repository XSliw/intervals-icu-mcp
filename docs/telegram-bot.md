# Sliwai Coach: Telegram + AgentRouter + Intervals.icu

This guide deploys two services from the same repository.

```text
Telegram → Sliwai Coach Telegram Bridge → AgentRouter → protected Intervals.icu MCP → Intervals.icu API
```

The Telegram bridge is deliberately **read-only**. It can inspect training, wellness, fitness and calendar data, but it never gives the model create, update, delete or bulk-write tools. It can suggest a training plan as a draft only.

## 1. Generate two independent secrets

Generate the values locally and store them only in Render environment variables. Do not commit them and do not paste them into GitHub issues or chat messages.

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Create one secret for `MCP_JWT_SECRET` and a different secret for `TELEGRAM_WEBHOOK_SECRET`. The MCP secret must be at least 32 characters.

## 2. Protect the existing `intervals-icu-mcp` service

In the Render service that already runs the MCP, add these environment variables:

| Variable | Value |
| --- | --- |
| `INTERVALS_ICU_API_KEY` | Your existing Intervals.icu key |
| `INTERVALS_ICU_ATHLETE_ID` | Your existing athlete ID, for example `i123456` |
| `MCP_JWT_SECRET` | First generated secret |
| `MCP_JWT_ISSUER` | `telegram-intervals-bot` |
| `MCP_JWT_AUDIENCE` | `intervals-icu-mcp` |

The service keeps its existing Docker command. After the deploy, its remote MCP URL remains:

```text
https://<your-mcp-service>.onrender.com/mcp
```

The updated HTTP server fails closed if `MCP_JWT_SECRET` is absent. A client must send a short-lived signed Bearer JWT, which the Telegram bridge generates automatically. A browser visit to `/mcp` is not a valid health test because an MCP endpoint requires protocol negotiation. Use `/health` only on the Telegram bridge for its simple health check.

## 3. Create a second Render Web Service

Create a new **Web Service** from the same GitHub repository. Its settings are:

| Field | Value |
| --- | --- |
| Name | `sliwai-coach-telegram` |
| Runtime | Docker |
| Branch | `main` |
| Start Command override | `intervals-icu-telegram-bot` |
| Health Check Path | `/health` |

The `Start Command` must replace the default MCP command for this second service only. Do not configure both services to use the Telegram command.

Add these environment variables to the second service:

| Variable | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Token issued by BotFather for `@sliwaicoachbot` |
| `TELEGRAM_WEBHOOK_SECRET` | Second generated secret |
| `ALLOWED_TELEGRAM_USER_IDS` | Your numeric Telegram user ID only |
| `AGENTROUTER_API_KEY` | API key from AgentRouter.org |
| `AGENTROUTER_BASE_URL` | `https://agentrouter.org/v1` |
| `AGENTROUTER_MODEL` | Exact enabled model ID from the AgentRouter dashboard |
| `MCP_URL` | The protected URL of the first service ending in `/mcp` |
| `MCP_JWT_SECRET` | The same first secret used by the MCP service |
| `MCP_JWT_ISSUER` | `telegram-intervals-bot` |
| `MCP_JWT_AUDIENCE` | `intervals-icu-mcp` |
| `PUBLIC_BASE_URL` | The public URL of this second service, without a trailing slash |
| `AUTO_SET_WEBHOOK` | `true` |

`ALLOWED_TELEGRAM_USER_IDS` is required. Retrieve your numeric ID from a Telegram ID bot you trust, then remove that bot from your chat if desired. The bridge silently ignores every other sender.

## 4. Deploy and verify

After the second service receives a public URL, set `PUBLIC_BASE_URL` and `AUTO_SET_WEBHOOK=true`, then redeploy it. The service will call Telegram `setWebhook` itself with the configured webhook secret.

Open `https://<your-telegram-service>.onrender.com/health`. It should return:

```json
{"status":"ok","mode":"read-only"}
```

Then send `/start` to [@sliwaicoachbot](https://t.me/sliwaicoachbot) and try a harmless question such as:

> Show my current CTL, ATL and TSB, then summarise my last three activities.

The first request after an inactive period may take longer if either Render free service has spun down.

## Security notes

- Keep the Telegram bot private: the service only accepts messages from `ALLOWED_TELEGRAM_USER_IDS`.
- Never expose `TELEGRAM_BOT_TOKEN`, `AGENTROUTER_API_KEY`, `INTERVALS_ICU_API_KEY`, `MCP_JWT_SECRET` or `TELEGRAM_WEBHOOK_SECRET` in source code or screenshots.
- `MCP_JWT_SECRET` must be the same for the MCP and Telegram services, but it must not be reused for Telegram’s webhook secret or any other system.
- The current bot has no write tools by design. Implementing calendar writes must use a new approval workflow with a persistent store and an explicit `/confirm` command.
- AgentRouter model availability and IDs depend on the account. Copy the exact enabled model ID from its dashboard rather than guessing.
