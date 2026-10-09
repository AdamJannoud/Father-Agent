"""The Father Agent: an open-source factory for professional Python sub-agents.

Type one line of English; the factory plans a spec, writes the code, puts
every file through a validator gate and only then writes it to
``subagents/<slug>/``. Inference is free only (Groq free tier, Hugging Face
Serverless Inference), with an offline deterministic mock when no key is set.
"""

__version__ = "1.0.0"

from .config import Config  # noqa: E402
from .errors import FatherAgentError  # noqa: E402
from .factory import Factory, GenerationResult  # noqa: E402
from .spec import SubAgentSpec  # noqa: E402
from .validator import Validator  # noqa: E402

__all__ = ["Config", "Factory", "FatherAgentError", "GenerationResult", "SubAgentSpec",
           "Validator", "__version__"]
