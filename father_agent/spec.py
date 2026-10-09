"""The JSON contract between the Planner and the Coder.

The planner's model must answer with JSON matching :class:`SubAgentSpec`.
The answer is parsed and checked against this schema before any code is
written, so the Coder always starts from a complete, well-formed plan.
"""

from __future__ import annotations

import json
import keyword
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from .errors import SpecError

SPEC_VERSION = "2"

#: How a sub-agent reaches its user. ``cli`` is the default and today's shape.
INTERFACES = ("cli", "web", "telegram", "api")
#: Frameworks each interface may use; the first is the default.
FRAMEWORKS: dict[str, tuple[str, ...]] = {
    "cli": ("argparse",),
    "web": ("streamlit", "fastapi"),
    "telegram": ("aiogram",),
    "api": ("fastapi",),
}
#: Hosting targets each interface can be prepared for; docker applies to all.
DEPLOY_TARGETS: dict[str, tuple[str, ...]] = {
    "cli": ("docker",),
    "web": ("docker", "hf-spaces", "render"),
    "telegram": ("docker", "render"),
    "api": ("docker", "hf-spaces", "render"),
}
_TARGET_ALIASES = {"huggingface": "hf-spaces", "hf": "hf-spaces", "spaces": "hf-spaces",
                   "hf_spaces": "hf-spaces", "hugging-face": "hf-spaces",
                   "huggingface-spaces": "hf-spaces", "dockerfile": "docker"}

#: SDKs for paid inference APIs. A generated sub-agent may not depend on them.
PAID_SDKS = frozenset({"openai", "anthropic", "cohere", "mistralai", "google.generativeai",
                       "google.genai", "together", "replicate", "voyageai"})

#: pip distribution name -> import name, for packages where the two differ.
IMPORT_NAMES: dict[str, str] = {
    "beautifulsoup4": "bs4",
    "scikit-learn": "sklearn",
    "python-dotenv": "dotenv",
    "pyyaml": "yaml",
    "pillow": "PIL",
    "opencv-python": "cv2",
    "python-dateutil": "dateutil",
    "solana": "solana",
    "solders": "solders",
    "web3": "web3",
    "python-telegram-bot": "telegram",
    "discord.py": "discord",
    "huggingface-hub": "huggingface_hub",
    "pytest-asyncio": "pytest_asyncio",
}

_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
_CLASS_RE = re.compile(r"^[A-Z][A-Za-z0-9]{1,60}$")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,60}$")


def import_name_for(package: str) -> str:
    """Return the import name for a pip package (``scikit-learn`` -> ``sklearn``)."""
    base = re.split(r"[<>=!~\[; ]", package.strip(), maxsplit=1)[0].lower()
    return IMPORT_NAMES.get(base, base.replace("-", "_"))


class Dependency(BaseModel):
    """A third-party package the sub-agent needs."""

    package: str = Field(min_length=1, description="pip distribution name, e.g. 'pandas'")
    import_name: str = Field(default="", description="top-level import, e.g. 'sklearn'")
    purpose: str = Field(default="", description="why the sub-agent needs it")

    @model_validator(mode="after")
    def _fill_import_name(self) -> Dependency:
        """Derive the import name when the planner omitted it, and refuse paid SDKs."""
        if not self.import_name:
            self.import_name = import_name_for(self.package)
        if self.import_name in PAID_SDKS or import_name_for(self.package) in PAID_SDKS:
            raise ValueError(f"{self.package!r} is a paid-API SDK; only free services are allowed")
        self.import_name = self.import_name.split(".")[0]
        if not _IDENT_RE.match(self.import_name):
            raise ValueError(f"invalid import name {self.import_name!r}")
        return self


class EnvVar(BaseModel):
    """An environment variable the sub-agent reads (an API key, an address...)."""

    name: str
    purpose: str = ""
    required: bool = False

    @field_validator("name")
    @classmethod
    def _upper_snake(cls, value: str) -> str:
        """Environment variable names must be UPPER_SNAKE_CASE."""
        if not _ENV_RE.match(value):
            raise ValueError(f"env var {value!r} must be UPPER_SNAKE_CASE")
        return value


class ClassSpec(BaseModel):
    """One class the sub-agent's agent.py must define."""

    name: str
    responsibility: str = ""
    methods: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _camel(cls, value: str) -> str:
        """Class names must be CamelCase identifiers."""
        if not _CLASS_RE.match(value):
            raise ValueError(f"class name {value!r} must be CamelCase")
        return value

    @field_validator("methods")
    @classmethod
    def _idents(cls, value: list[str]) -> list[str]:
        """Method names must be valid identifiers."""
        bad = [m for m in value if not _IDENT_RE.match(m) or keyword.iskeyword(m)]
        if bad:
            raise ValueError(f"invalid method name(s): {', '.join(bad)}")
        return value


class Delivery(BaseModel):
    """How the sub-agent is delivered: its interface, framework and hosting targets."""

    interface: Literal["cli", "web", "telegram", "api"] = Field(
        default="cli", description="cli (default), web dashboard, telegram bot or JSON api")
    framework: str = Field(default="", description="streamlit or fastapi for web; aiogram "
                                                   "for telegram; fastapi for api; empty "
                                                   "for the default")
    deploy: list[str] = Field(default_factory=list,
                              description="hosting targets: docker, hf-spaces, render "
                                          "(empty for every target the interface supports)")

    @model_validator(mode="after")
    def _resolve(self) -> Delivery:
        """Fill the default framework and targets, and refuse combinations that cannot work."""
        allowed = FRAMEWORKS[self.interface]
        framework = self.framework.strip().lower()
        if framework in ("", "none", "default") or (self.interface == "cli"
                                                    and framework in ("cli", "stdlib")):
            framework = allowed[0]
        if framework not in allowed:
            raise ValueError(f"framework {self.framework!r} does not fit interface "
                             f"{self.interface!r}; choose one of: {', '.join(allowed)}")
        self.framework = framework
        targets = DEPLOY_TARGETS[self.interface]
        wanted = [_TARGET_ALIASES.get(t.strip().lower(), t.strip().lower()) for t in self.deploy]
        bad = [t for t in wanted if t not in targets]
        if bad:
            raise ValueError(f"deploy target(s) {', '.join(bad)} do not fit interface "
                             f"{self.interface!r}; choose from: {', '.join(targets)}")
        chosen = set(wanted) or set(targets)
        chosen.add("docker")
        self.deploy = [t for t in targets if t in chosen]
        return self

    @property
    def label(self) -> str:
        """``telegram · aiogram`` for progress lines."""
        return f"{self.interface} · {self.framework}"


class SubAgentSpec(BaseModel):
    """Everything the Coder needs to write one sub-agent."""

    spec_version: str = SPEC_VERSION
    name: str = Field(min_length=2, max_length=80)
    slug: str
    summary: str = Field(min_length=10)
    command: str = Field(min_length=3)
    domain: str = Field(min_length=2)
    dependencies: list[Dependency] = Field(default_factory=list)
    env_vars: list[EnvVar] = Field(default_factory=list)
    classes: list[ClassSpec] = Field(min_length=1)
    entrypoint: str = "main"
    schedule_seconds: int | None = Field(default=None, ge=1)
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    run_example: str = ""
    notes: list[str] = Field(default_factory=list)
    delivery: Delivery = Field(default_factory=Delivery)
    planned_by: str = ""

    @field_validator("slug")
    @classmethod
    def _slug(cls, value: str) -> str:
        """Slugs become folder and module names, so keep them snake_case."""
        value = value.strip().lower().replace("-", "_")
        if not _SLUG_RE.match(value) or keyword.iskeyword(value):
            raise ValueError(f"slug {value!r} must match {_SLUG_RE.pattern}")
        return value

    @model_validator(mode="after")
    def _consistency(self) -> SubAgentSpec:
        """Reject duplicate classes/dependencies and fill the run example."""
        names = [c.name for c in self.classes]
        if len(names) != len(set(names)):
            raise ValueError("class names must be unique")
        packages = [d.import_name for d in self.dependencies]
        if len(packages) != len(set(packages)):
            raise ValueError("dependencies must be unique")
        if not _IDENT_RE.match(self.entrypoint):
            raise ValueError("entrypoint must be a function name")
        if not self.run_example:
            self.run_example = f"python -m subagents.{self.slug}.agent --once"
        return self

    @property
    def has_delivery_block(self) -> bool:
        """False for a spec.json written before the delivery layer existed."""
        return "delivery" in self.model_fields_set

    @property
    def import_names(self) -> set[str]:
        """Top-level import names of every declared dependency."""
        return {d.import_name for d in self.dependencies}

    def to_json(self) -> str:
        """Serialise to pretty JSON for spec.json."""
        return self.model_dump_json(indent=2) + "\n"


def extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model reply.

    Models often wrap JSON in prose or a ```json fence; this tolerates both.

    Raises:
        SpecError: If no JSON object can be decoded.
    """
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.S)
    candidates = [fence.group(1)] if fence else []
    start = text.find("{")
    if start != -1:
        candidates.append(text[start:text.rfind("}") + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    raise SpecError("planner reply did not contain a JSON object")


def parse_spec(text: str, *, command: str, planned_by: str = "") -> SubAgentSpec:
    """Parse and validate a planner reply into a :class:`SubAgentSpec`.

    Raises:
        SpecError: With a readable list of schema violations.
    """
    data = extract_json(text)
    data.setdefault("command", command)
    data["planned_by"] = planned_by
    try:
        return SubAgentSpec.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'spec'}: {err['msg']}"
            for err in exc.errors()
        )
        raise SpecError(f"spec does not match the contract: {problems}") from exc


def spec_schema_json() -> str:
    """Return the JSON schema of the spec, embedded in the planner prompt."""
    schema = SubAgentSpec.model_json_schema()
    for key in ("planned_by", "spec_version"):
        schema.get("properties", {}).pop(key, None)
    return json.dumps(schema, indent=1)
