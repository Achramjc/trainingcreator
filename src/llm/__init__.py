"""Optional, grounded LLM enhancement layer (M1).

Off by default.  When it is on, it proposes better learning objectives, plain-
language section summaries and stronger distractors - and every proposal is
mechanically checked against a cited line span of the source document before it
is allowed anywhere near a learner.  Anything that fails the check is discarded
and the deterministic content is kept, with the reason recorded in an
``EnhancementReport`` that goes to the subject-matter expert.

Nothing here is approved content.  The SME approval step remains mandatory; see
``docs/LLM.md`` and GOAL.md Risk 2.

Importing this package has no side effects and does not require the
``anthropic`` SDK - the SDK is imported lazily inside ``AnthropicProvider``.
"""

from .config import (
    BACKENDS,
    DEFAULT_MODEL,
    ENV_API_KEY,
    ENV_ENABLE,
    ENV_LIVE_TESTS,
    ENV_MODEL,
    LLMConfig,
    live_tests_enabled,
)
from .enhance import (
    EnhancementItem,
    EnhancementReport,
    enhance_assessment,
    enhance_module,
    merge_reports,
    naive_pickers,
    worst_naive_score,
)
from .grounding import (
    Verdict,
    document_asserts,
    document_lines,
    resolve_span,
    span_excerpt,
    verify_claim,
    verify_distractor,
)
from .prompts import STABLE_SYSTEM_PROMPT, format_document, system_blocks
from .provider import (
    AnthropicProvider,
    FakeProvider,
    NullProvider,
    Provider,
    ProviderResult,
    build_provider,
)

__all__ = [
    "BACKENDS",
    "DEFAULT_MODEL",
    "ENV_API_KEY",
    "ENV_ENABLE",
    "ENV_LIVE_TESTS",
    "ENV_MODEL",
    "LLMConfig",
    "live_tests_enabled",
    "Provider",
    "ProviderResult",
    "AnthropicProvider",
    "FakeProvider",
    "NullProvider",
    "build_provider",
    "Verdict",
    "verify_claim",
    "verify_distractor",
    "document_asserts",
    "document_lines",
    "resolve_span",
    "span_excerpt",
    "STABLE_SYSTEM_PROMPT",
    "format_document",
    "system_blocks",
    "EnhancementItem",
    "EnhancementReport",
    "enhance_module",
    "enhance_assessment",
    "merge_reports",
    "naive_pickers",
    "worst_naive_score",
]
