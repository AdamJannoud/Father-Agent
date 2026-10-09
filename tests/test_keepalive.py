"""The keepalive pinger: it wakes the free host, and it fails loudly when the bot is down.

Run every five minutes by `.github/workflows/keepalive.yml`. Offline, like the
rest of the suite: the retry logic is tested against an injected request, the
transport against an injected `urlopen`, and the wiring by reading the workflow
file. Nothing here opens a socket — `tests/conftest.py` forbids it, and the one
thing that must really reach the internet (the ping itself) is the script's own
live run.
"""

from __future__ import annotations

import importlib.util
import io
import urllib.error
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "keepalive.yml"
IDLE_TIMEOUT_MINUTES = 15  # Render's free web services sleep after this long idle
HEALTHY_BODY = b'{"status": "ok", "mode": "polling"}'


def load_pinger():
    spec = importlib.util.spec_from_file_location("fa_keepalive", ROOT / "scripts" / "keepalive.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pinger = load_pinger()


def run(answers, attempts=4):
    """Run the pinger over a scripted sequence; return (exit code, sleeps, logs)."""
    sleeps: list[float] = []
    logs: list[str] = []
    remaining = list(answers)

    def request(url, timeout):
        return remaining.pop(0) if remaining else answers[-1]

    status = pinger.keepalive(
        url="https://example.test/healthz",
        attempts=attempts,
        timeout=1.0,
        interval=7.0,
        request=request,
        sleep=sleeps.append,
        log=logs.append,
    )
    return status, sleeps, logs


def test_healthy_endpoint_exits_zero_without_retrying():
    status, sleeps, logs = run([(200, '{"status": "ok"}')])
    assert status == 0
    assert sleeps == []
    assert "200 OK" in logs[0] and '"status": "ok"' in logs[0]


def test_a_cold_start_is_not_an_outage():
    # A slept instance answers 503 while it boots, then 200: that is a wake-up,
    # not an incident, so the pinger waits it out instead of alerting.
    status, sleeps, logs = run([(503, "starting"), (200, '{"status": "ok"}')])
    assert status == 0
    assert sleeps == [7.0]
    assert len(logs) == 2


def test_never_answering_is_unhealthy_after_every_attempt():
    status, sleeps, logs = run([(503, "starting")])
    assert status == 1
    assert sleeps == [7.0, 7.0, 7.0]  # no sleep after the final attempt
    assert logs[-1].startswith("UNHEALTHY")


def test_a_dead_host_is_not_a_crash():
    # A refused connection, a DNS failure and a timeout all arrive as no status.
    status, sleeps, logs = run([(None, "URLError: connection refused")])
    assert status == 1
    assert "no response (URLError" in logs[0]


class FakeResponse:
    def __init__(self, status, body=HEALTHY_BODY):
        self.status = status
        self._body = body

    def read(self, _limit=None):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_transport_reads_a_200(monkeypatch):
    monkeypatch.setattr(
        pinger.urllib.request, "urlopen", lambda request, timeout: FakeResponse(200)
    )
    status, body = pinger.request_once("https://example.test/healthz", timeout=5.0)
    assert status == 200
    assert '"status": "ok"' in body


def test_transport_reports_503_instead_of_raising(monkeypatch):
    def raise_503(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 503, "starting", {}, io.BytesIO(b'{"status": "starting"}')
        )

    monkeypatch.setattr(pinger.urllib.request, "urlopen", raise_503)
    status, body = pinger.request_once("https://example.test/healthz", timeout=5.0)
    assert status == 503
    assert "starting" in body  # an error answer is still read, not discarded


def test_transport_turns_a_transport_failure_into_no_status(monkeypatch):
    def refuse(request, timeout):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(pinger.urllib.request, "urlopen", refuse)
    status, body = pinger.request_once("https://example.test/healthz", timeout=5.0)
    assert status is None
    assert "connection refused" in body


def workflow_triggers(data):
    """`on:` is YAML 1.1 boolean true, so the key is not the string 'on'."""
    return data.get(True) if True in data else data.get("on")


def test_the_workflow_pings_often_enough_to_beat_the_idle_timeout():
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    crons = [entry["cron"] for entry in workflow_triggers(data)["schedule"]]
    assert crons == ["*/5 * * * *"]
    assert 5 < IDLE_TIMEOUT_MINUTES, "a ping every 5 minutes must land inside the idle window"


def test_the_workflow_runs_the_pinger_and_can_raise_the_alert():
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = workflow_triggers(data)
    assert "workflow_dispatch" in triggers, "it must be startable by hand"
    assert any("paths" in trigger for trigger in triggers.get("push") or []), "it must prove itself"
    assert data["permissions"]["issues"] == "write", "the alert issue could not be opened"
    commands = [step.get("run", "") for job in data["jobs"].values() for step in job["steps"]]
    assert any("scripts/keepalive.py" in command for command in commands)
    assert any("gh issue" in command for command in commands)
