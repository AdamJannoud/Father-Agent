#!/usr/bin/env python3
"""Ping the free-host web service so it never sleeps, and check the bot is alive.

Render's free web services spin down after 15 minutes *without inbound traffic*
(Koyeb's free ones sleep the same way), and a Telegram bot in long-polling mode
makes only outbound calls. Nothing inside the service keeps it up, so something
outside has to request it on a schedule: that is this script, run every five
minutes by `.github/workflows/keepalive.yml`.

`/healthz` answers `200` only while the poll loop is alive, and `503` while the
process is still starting, so a non-zero exit means the bot is down rather than
merely that the host had slept: the retry loop below absorbs a cold start
(about a minute) before it decides.

    python scripts/keepalive.py
    KEEPALIVE_URL=https://other-host.example/healthz python scripts/keepalive.py
    python scripts/keepalive.py --url ... --attempts 3 --interval 20 --timeout 20

Exit status: 0 the endpoint answered 200, 1 it never did.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "https://father-agent.onrender.com/healthz"
DEFAULT_ATTEMPTS = 4
DEFAULT_TIMEOUT = 45.0
DEFAULT_INTERVAL = 20.0
USER_AGENT = "father-agent-keepalive"


def request_once(url: str, timeout: float) -> tuple[int | None, str]:
    """One request. Returns (status, body); status is None if it never answered."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(4096).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # 503 while the process starts is the expected answer after a sleep.
        body = exc.read(4096).decode("utf-8", "replace")
        return exc.code, body or str(exc)
    except OSError as exc:
        # DNS failure, connection refused, timeout, TLS: not up.
        return None, f"{type(exc).__name__}: {exc}"


def keepalive(
    url: str = DEFAULT_URL,
    attempts: int = DEFAULT_ATTEMPTS,
    timeout: float = DEFAULT_TIMEOUT,
    interval: float = DEFAULT_INTERVAL,
    request=request_once,
    sleep=time.sleep,
    log=print,
) -> int:
    """Request `url` until it answers 200 or the attempts run out. 0 when healthy."""
    last = "no attempt was made"
    for attempt in range(1, attempts + 1):
        status, body = request(url, timeout)
        if status == 200:
            log(f"attempt {attempt}/{attempts}: 200 OK  {body.strip()[:400]}")
            return 0
        last = f"HTTP {status}" if status is not None else f"no response ({body})"
        log(f"attempt {attempt}/{attempts}: {last}")
        if attempt < attempts:
            sleep(interval)
    log(f"UNHEALTHY after {attempts} attempts against {url}: {last}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Keep a free-host web service awake and check it answers."
    )
    parser.add_argument("--url", default=os.environ.get("KEEPALIVE_URL") or DEFAULT_URL)
    parser.add_argument("--attempts", type=int, default=DEFAULT_ATTEMPTS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    args = parser.parse_args(argv)
    if args.attempts < 1:
        parser.error("--attempts must be at least 1")
    return keepalive(args.url, args.attempts, args.timeout, args.interval)


if __name__ == "__main__":
    sys.exit(main())
