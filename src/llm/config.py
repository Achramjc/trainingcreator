"""Configuration for the optional, grounded LLM enhancement layer.

The layer is OFF by default.  Nothing in the deterministic pipeline changes
unless it is switched on explicitly, and even when it is on every generated
sentence has to survive :mod:`src.llm.grounding` before it reaches a learner.

Environment
-----------
``TRAINING_CREATOR_LLM``
    ``off`` (default) or ``anthropic``.  Anything else is treated as ``off``
    and recorded as a note on the config, because silently guessing at a
    backend name is the wrong failure mode for a regulated tool.
``TRAINING_CREATOR_LLM_MODEL``
    Model id, default ``claude-opus-5``.
``ANTHROPIC_API_KEY``
    Only its *presence* is ever read here, and only so the report can say
    "no credential was configured".  The value is never stored, logged, or
    serialised.  Its absence is not fatal: the Anthropic SDK also resolves an
    ``ant auth login`` profile, so we let the call itself decide.
``TRAINING_CREATOR_LLM_LIVE_TESTS``
    Read only by the test suite, to opt in to the one live smoke test.
"""

import os
from dataclasses import dataclass, field, replace
from typing import Dict, List, Mapping, Optional

#: Backends this build knows how to talk to.
BACKENDS = ("off", "anthropic")

DEFAULT_MODEL = "claude-opus-5"

#: Non-streaming default.  Do not lowball it: a truncated JSON response is a
#: wasted call, and the whole document already sits in the cached prefix.
DEFAULT_MAX_TOKENS = 16000

#: Client-level settings.  ``max_retries=2`` is the SDK default, restated so a
#: reader does not have to know that; 120s covers a thinking model's turn.
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_MAX_RETRIES = 2

ENV_ENABLE = "TRAINING_CREATOR_LLM"
ENV_MODEL = "TRAINING_CREATOR_LLM_MODEL"
ENV_API_KEY = "ANTHROPIC_API_KEY"
ENV_LIVE_TESTS = "TRAINING_CREATOR_LLM_LIVE_TESTS"


@dataclass(frozen=True)
class LLMConfig:
    """Immutable settings for one enhancement run."""

    backend: str = "off"
    model: str = DEFAULT_MODEL
    api_key_present: bool = False
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    max_retries: int = DEFAULT_MAX_RETRIES
    #: Fraction of a generated sentence's content words that must appear in the
    #: cited excerpt.  See :func:`src.llm.grounding.verify_claim`.
    min_overlap: float = 0.5
    #: A generated sentence longer than this is rejected unread: training prose
    #: that runs past it is not a summary sentence, it is a paragraph.
    max_sentence_chars: int = 300
    #: Lines of context either side of a cited span when building the excerpt.
    span_context_lines: int = 1
    notes: tuple = field(default_factory=tuple)

    @property
    def enabled(self) -> bool:
        return self.backend != "off"

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "LLMConfig":
        """Build a config from the process environment (or a mapping, for tests)."""
        source = os.environ if env is None else env
        raw = (source.get(ENV_ENABLE) or "off").strip().lower()
        notes: List[str] = []
        if raw not in BACKENDS:
            notes.append(
                "{0}={1!r} is not one of {2}; the LLM layer stays off.".format(
                    ENV_ENABLE, raw, ", ".join(BACKENDS)))
            raw = "off"

        model = (source.get(ENV_MODEL) or "").strip() or DEFAULT_MODEL
        api_key_present = bool((source.get(ENV_API_KEY) or "").strip())
        if raw == "anthropic" and not api_key_present:
            notes.append(
                "{0} is not set. The Anthropic SDK may still resolve an "
                "`ant auth login` profile; if it cannot, the call fails and the "
                "deterministic content is kept.".format(ENV_API_KEY))

        return cls(backend=raw, model=model, api_key_present=api_key_present,
                   notes=tuple(notes))

    def with_enabled(self, enabled: bool) -> "LLMConfig":
        """Return a copy with the layer forced on or off (the CLI ``--llm`` flag).

        Turning it on when the environment named no backend promotes it to
        ``anthropic`` - the only real backend in this build - rather than
        leaving the caller with a flag that silently does nothing.
        """
        if not enabled:
            return replace(self, backend="off")
        if self.backend == "off":
            return replace(self, backend="anthropic")
        return self

    def to_dict(self) -> Dict:
        """Report-safe description.  Never contains the API key."""
        return {
            "backend": self.backend,
            "enabled": self.enabled,
            "model": self.model,
            "api_key_present": self.api_key_present,
            "max_tokens": self.max_tokens,
            "timeout_seconds": self.timeout,
            "max_retries": self.max_retries,
            "min_content_word_overlap": self.min_overlap,
            "max_sentence_chars": self.max_sentence_chars,
            "notes": list(self.notes),
        }


def live_tests_enabled(env: Optional[Mapping[str, str]] = None) -> bool:
    """True only when BOTH the live-test opt-in and a key are set."""
    source = os.environ if env is None else env
    return (source.get(ENV_LIVE_TESTS, "").strip() == "1"
            and bool((source.get(ENV_API_KEY) or "").strip()))
