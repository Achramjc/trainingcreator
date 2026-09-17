"""Providers for the enhancement layer.

A provider does exactly one thing: turn (system blocks, user prompt, JSON
schema) into either parsed JSON or a *reason it could not*.  It never raises
into the pipeline - a failed call is a `ProviderResult` with `data=None` and an
`error`, and the caller keeps the deterministic content.  That is the whole
contract; everything downstream (grounding, reporting) treats "no enhancement"
as a perfectly normal outcome.

Three implementations:

``NullProvider``   the layer is off.  Returns "no enhancement", always.
``FakeProvider``   scripted results, for tests.  Never touches the network.
``AnthropicProvider``  the real one.  ``import anthropic`` happens *inside* the
                   call so that the package, the CLI and the entire test suite
                   work with the SDK absent.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

try:                                        # pragma: no cover - typing only
    from typing import Protocol, runtime_checkable
except ImportError:                         # pragma: no cover - py<3.8
    Protocol = object                       # type: ignore

    def runtime_checkable(cls):             # type: ignore
        return cls

from .config import LLMConfig


@dataclass
class ProviderResult:
    """What one model call produced.

    ``data``     parsed JSON matching the requested schema, or None.
    ``refused``  the model declined the request (``stop_reason == "refusal"``).
                 Not an error: it means "no enhancement", recorded as such.
    ``error``    human-readable reason there is no data.  Shown to the SME.
    ``usage``    token accounting for the report (input, cache read, output).
    """

    data: Optional[Dict[str, Any]] = None
    refused: bool = False
    error: Optional[str] = None
    usage: Dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.data is not None

    def to_dict(self) -> Dict:
        return {
            "ok": self.ok,
            "refused": self.refused,
            "error": self.error,
            "usage": dict(self.usage),
        }


@runtime_checkable
class Provider(Protocol):
    """Structural type every provider satisfies."""

    name: str

    def complete_json(self, system_blocks: Sequence[Dict[str, Any]],
                      user_prompt: str,
                      schema: Dict[str, Any]) -> ProviderResult:
        ...                                 # pragma: no cover - protocol


class NullProvider:
    """The layer is off.  Present so callers never branch on ``None``."""

    name = "null"

    def complete_json(self, system_blocks, user_prompt, schema) -> ProviderResult:
        return ProviderResult(data=None, error="LLM enhancement is disabled.")


class FakeProvider:
    """Scripted provider for tests.  Makes no network call, ever.

    ``scripted`` is consumed in order, one entry per ``complete_json`` call:

    * ``dict``            -> returned as the parsed data of a successful call
    * ``ProviderResult``  -> returned verbatim (use for refusals / errors)
    * ``Exception``       -> raised, to prove the caller survives a crash

    Running past the end of the script yields an explicit error result rather
    than an IndexError, so a test that under-scripts fails loudly on the
    assertion it cares about instead of on plumbing.
    """

    name = "fake"

    def __init__(self, scripted: Optional[Sequence[Any]] = None,
                 usage: Optional[Dict[str, int]] = None):
        self.scripted: List[Any] = list(scripted or [])
        self.calls: List[Dict[str, Any]] = []
        self._usage = dict(usage or {"input_tokens": 1000,
                                     "cache_read_input_tokens": 900,
                                     "output_tokens": 100})

    def complete_json(self, system_blocks, user_prompt, schema) -> ProviderResult:
        self.calls.append({
            "system_blocks": list(system_blocks),
            "user_prompt": user_prompt,
            "schema": schema,
        })
        if not self.scripted:
            return ProviderResult(data=None,
                                  error="FakeProvider script exhausted.")
        item = self.scripted.pop(0)
        if isinstance(item, ProviderResult):
            return item
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, dict):
            return ProviderResult(data=item, usage=dict(self._usage))
        return ProviderResult(data=None,
                              error="FakeProvider: unusable script entry "
                                    "{0!r}".format(type(item).__name__))


class AnthropicProvider:
    """Calls the Claude API for structured JSON, and never lets it escape.

    Caching: the caller passes the stable system prompt and the line-numbered
    SOP as two ``system`` blocks, each marked ``cache_control``.  The three
    enhancement calls for one document share that prefix, so calls two and
    three read it from cache.  The client is built once per provider instance
    so all three calls go through the same connection pool.
    """

    name = "anthropic"

    def __init__(self, config: Optional[LLMConfig] = None):
        self.config = config or LLMConfig(backend="anthropic")
        self._client = None

    # -- SDK plumbing -------------------------------------------------------
    def _sdk(self):
        """Import the SDK lazily.  Absent SDK == no enhancement, not a crash."""
        import anthropic  # noqa: F401  (lazy on purpose - see module docstring)
        return anthropic

    def _get_client(self, anthropic):
        if self._client is None:
            self._client = anthropic.Anthropic(
                max_retries=self.config.max_retries,
                timeout=self.config.timeout,
            )
        return self._client

    # -- public API ---------------------------------------------------------
    def complete_json(self, system_blocks, user_prompt, schema) -> ProviderResult:
        try:
            anthropic = self._sdk()
        except ImportError as exc:
            return ProviderResult(
                data=None,
                error="The `anthropic` SDK is not installed ({0}). Install "
                      "requirements-llm.txt to enable the LLM layer.".format(exc))

        try:
            client = self._get_client(anthropic)
            response = client.messages.create(
                model=self.config.model,
                max_tokens=self.config.max_tokens,
                system=list(system_blocks),
                messages=[{"role": "user", "content": user_prompt}],
                output_config={"format": {"type": "json_schema",
                                          "schema": schema}},
            )
        # Most specific first.  A blanket `except APIStatusError` would hide the
        # difference between "your key is wrong" and "retry in a minute", and
        # the SME reading the report needs that difference.
        except anthropic.AuthenticationError as exc:
            return ProviderResult(
                data=None,
                error="Authentication failed ({0}). No credential was accepted; "
                      "the deterministic content is unchanged.".format(
                          _status(exc)))
        except anthropic.RateLimitError as exc:
            return ProviderResult(
                data=None,
                error="Rate limited ({0}) after {1} retries.".format(
                    _status(exc), self.config.max_retries))
        except anthropic.APIStatusError as exc:
            status = getattr(exc, "status_code", 0) or 0
            kind = "Server error" if status >= 500 else "API error"
            return ProviderResult(
                data=None,
                error="{0} ({1}): {2}".format(kind, status, _message(exc)))
        except anthropic.APIConnectionError as exc:
            return ProviderResult(
                data=None,
                error="Could not reach the API: {0}".format(_message(exc)))
        except Exception as exc:            # pragma: no cover - belt and braces
            return ProviderResult(
                data=None,
                error="Unexpected {0}: {1}".format(type(exc).__name__, exc))

        usage = _usage_dict(getattr(response, "usage", None))

        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            return ProviderResult(
                data=None, refused=True, usage=usage,
                error="The model declined to answer{0}. Treated as "
                      "'no enhancement'.".format(
                          " (category: {0})".format(category) if category else ""))

        try:
            text = next(block.text for block in response.content
                        if getattr(block, "type", None) == "text")
        except StopIteration:
            return ProviderResult(
                data=None, usage=usage,
                error="The response carried no text block (stop_reason={0!r}).".format(
                    getattr(response, "stop_reason", None)))

        try:
            data = json.loads(text)
        except ValueError as exc:
            return ProviderResult(
                data=None, usage=usage,
                error="Response was not valid JSON: {0}".format(exc))

        if not isinstance(data, dict):
            return ProviderResult(
                data=None, usage=usage,
                error="Response JSON was {0}, expected an object.".format(
                    type(data).__name__))

        return ProviderResult(data=data, usage=usage)


def _status(exc) -> str:
    return str(getattr(exc, "status_code", "") or "no status")


def _message(exc) -> str:
    return str(getattr(exc, "message", None) or exc)


def _usage_dict(usage) -> Dict[str, int]:
    """Pull the three numbers the report cares about off a usage object."""
    if usage is None:
        return {}
    out: Dict[str, int] = {}
    for key in ("input_tokens", "cache_creation_input_tokens",
                "cache_read_input_tokens", "output_tokens"):
        value = getattr(usage, key, None)
        if isinstance(value, int):
            out[key] = value
    return out


def build_provider(config: LLMConfig) -> Provider:
    """Factory used by the CLI and the web app.  Monkeypatch this in tests."""
    if not config.enabled:
        return NullProvider()
    if config.backend == "anthropic":
        return AnthropicProvider(config)
    return NullProvider()
