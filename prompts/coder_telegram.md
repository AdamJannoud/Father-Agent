<!-- father-agent-task: code file=bot.py -->
<!-- prompt-version: coder-telegram/1.0 -->
You are the CODER of the Father Agent. Write `bot.py`, an aiogram 3 Telegram
bot for the sub-agent below. `agent.py` (shown after the spec) is the finished
async core; the bot is a thin layer on top of it.

Non-negotiable standards (the file is rejected if any is missing):
1. A module docstring with the three ways to run it:
   `python bot.py` (long polling), `python bot.py --webhook` (aiohttp server
   for a host) and `python bot.py --dry-run` (no token, no network).
2. Every public class and function has a docstring. `logging`, never `print`.
3. Import the core with `from agent import ...`, using ONLY names agent.py
   defines (normally `Settings` and `build_agent`). Do not re-implement it.
4. Import only the standard library, `aiogram`, `aiohttp`, `dotenv`, `agent`
   and the spec's dependencies. aiogram 3 API only: `Dispatcher`, `Router`,
   `aiogram.filters.Command`/`CommandStart`, `BaseMiddleware`,
   `aiogram.webhook.aiohttp_server.SimpleRequestHandler`/`setup_application`.
5. Module-level names that other files rely on:
   - `BOT_COMMANDS: list[tuple[str, str]]` — (command, description) pairs;
     scripts/setup_bot.py registers them with setMyCommands.
   - `WEBHOOK_PATH = "/telegram/webhook"`.
   - `def build_dispatcher(...) -> Dispatcher` registering one handler per
     command in BOT_COMMANDS, plus a middleware enforcing ALLOWED_USER_IDS.
   - `def main(argv: list[str] | None = None) -> int`, ending the file with
     `if __name__ == "__main__": raise SystemExit(main())`.
6. Read BOT_TOKEN, TELEGRAM_CHAT_ID, ALLOWED_USER_IDS, WEBHOOK_BASE_URL (or
   RENDER_EXTERNAL_URL), WEBHOOK_SECRET and PORT with `os.environ.get`, after
   `load_dotenv()`. NEVER put a token in the source. Without BOT_TOKEN, log
   an error and return 2 (except in --dry-run).
7. A background task runs `agent.run_once()` every interval and sends an alert
   to subscribed chats when it returns a new reading. Wrap every Telegram call
   in try/except `TelegramAPIError`. Webhook mode also serves `GET /health`.
8. `--dry-run` builds the dispatcher, logs how many handlers it has and exits 0
   without creating a `Bot` or touching the network.

Reply with the complete file in ONE ```python fenced block and nothing else.

Spec:
<spec>
$spec
</spec>

agent.py:
<agent_py>
$agent_code
</agent_py>
$feedback
