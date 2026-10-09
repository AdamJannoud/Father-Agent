"""The planner/coder JSON contract."""

from __future__ import annotations

import json

import pytest

from father_agent.errors import SpecError
from father_agent.heuristics import plan_spec
from father_agent.spec import SubAgentSpec, import_name_for, parse_spec, spec_schema_json

from .conftest import SAMPLE_COMMAND


def _minimal(**overrides: object) -> dict:
    data = {"name": "Demo", "slug": "demo_agent", "summary": "A demo sub-agent for tests.",
            "command": "demo", "domain": "web_api",
            "classes": [{"name": "DemoAgent", "methods": ["run_once"]}]}
    data.update(overrides)
    return data


def test_parse_fenced_json_reply() -> None:
    """A ```json fenced reply with prose around it parses."""
    reply = "Here you go:\n```json\n" + json.dumps(_minimal()) + "\n```\nEnjoy."
    spec = parse_spec(reply, command="demo", planned_by="mock/x")
    assert spec.slug == "demo_agent" and spec.planned_by == "mock/x"
    assert spec.run_example == "python -m subagents.demo_agent.agent --once"


def test_non_json_reply_is_a_spec_error() -> None:
    """A reply with no JSON object is rejected clearly."""
    with pytest.raises(SpecError, match="JSON"):
        parse_spec("I cannot help with that.", command="demo")


def test_schema_violations_are_listed() -> None:
    """Bad slug and class names are reported with their location."""
    bad = _minimal(slug="Bad Slug!", classes=[{"name": "lowercase"}])
    with pytest.raises(SpecError) as info:
        parse_spec(json.dumps(bad), command="demo")
    assert "slug" in str(info.value) and "classes" in str(info.value)


@pytest.mark.parametrize("package", ["openai", "anthropic", "google.generativeai"])
def test_paid_sdk_dependencies_are_refused(package: str) -> None:
    """A spec may not depend on a paid inference SDK."""
    with pytest.raises(SpecError, match="paid"):
        parse_spec(json.dumps(_minimal(dependencies=[{"package": package}])), command="d")


def test_import_names_are_derived() -> None:
    """pip names map to their import names."""
    assert import_name_for("scikit-learn") == "sklearn"
    assert import_name_for("beautifulsoup4>=4.12") == "bs4"
    assert import_name_for("python-dotenv") == "dotenv"
    assert import_name_for("web3") == "web3"


def test_heuristic_spec_satisfies_contract() -> None:
    """The offline planner's output validates against the contract."""
    spec = SubAgentSpec.model_validate(plan_spec(SAMPLE_COMMAND))
    assert spec.slug == "wallet_watcher"
    assert {"httpx", "pandas", "matplotlib"} <= spec.import_names
    assert spec.schedule_seconds == 60
    assert json.loads(spec.to_json())["slug"] == "wallet_watcher"


def test_schema_is_embeddable() -> None:
    """The schema shown to the planner is JSON and names the key fields."""
    schema = json.loads(spec_schema_json())
    assert {"slug", "classes", "dependencies"} <= set(schema["properties"])
