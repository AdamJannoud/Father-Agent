# The Father Agent's Telegram bot as a container (Koyeb, or any Docker host).
# Build from the repository root:  docker build -f deploy/Dockerfile -t father-agent-bot .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app
COPY requirements.txt requirements-bot.txt ./
# Two steps: aiogram 3.31.0 caps pydantic below the core's pin (see requirements-bot.txt).
# --only-binary: aiohttp's C stack comes from wheels or the build fails, never compiled
# from an sdist (the same guard as render.yaml).
RUN pip install -r requirements.txt \
 && pip install --only-binary=aiohttp,multidict,yarl,frozenlist,aiosignal,propcache \
      -r requirements-bot.txt

COPY . .
RUN useradd --create-home father && chown -R father /app
USER father

EXPOSE 8000
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz', timeout=4)"
CMD ["python", "main.py", "bot"]
