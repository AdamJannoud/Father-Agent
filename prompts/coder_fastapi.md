<!-- father-agent-task: code file=app.py -->
<!-- prompt-version: coder-fastapi/1.0 -->
You are the CODER of the Father Agent. Write `app.py`, a FastAPI app for the
sub-agent below. `agent.py` (shown after the spec) is the finished async core;
the app is a thin HTTP layer on top of it. If the spec's `delivery.interface`
is "web", the app also serves an HTML dashboard at `GET /`; if it is "api",
it is a JSON service only.

Non-negotiable standards (the file is rejected if any is missing):
1. A module docstring listing the endpoints and how to run it (`python app.py`).
2. Every public function has a docstring. `logging`, never `print`.
3. Import the core with `from agent import ...`, using ONLY names agent.py
   defines (normally `Settings` and `build_agent`). Do not re-implement it.
4. Import only the standard library, `fastapi`, `uvicorn`, `dotenv`, `agent`
   and the spec's dependencies.
5. `def create_app(agent_factory=...) -> FastAPI` builds the app; the agent is
   created in a lifespan handler (call `load_dotenv()` there) and closed on
   shutdown. A module-level `app = create_app()` must exist, with no network
   calls at import time.
6. Routes: `GET /health` returning {"status": "ok"}, `POST /run` running one
   agent cycle (behind an asyncio.Lock), `GET /history` and `GET /latest`.
   Map agent failures to HTTPException, inside try/except; never leak a traceback.
7. For "web": `GET /` returns one HTML page built with `html.escape` on every
   value. Its colours, fonts and radii come from CSS custom properties named
   `--app-*`, declared for both `[data-app-mode="light"]` and
   `[data-app-mode="dark"]` on <html>; it must read at 390px and 1280px wide.
8. `def main(argv: list[str] | None = None) -> int` parses --host/--port (port
   default from the PORT environment variable) and runs `uvicorn.run(app, ...)`;
   end with `if __name__ == "__main__": raise SystemExit(main())`.
9. Never send X-Frame-Options or a frame-ancestors CSP: hosts embed the app.
   No secrets in the source; configuration only from environment variables.

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
