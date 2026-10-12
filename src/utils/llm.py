"""Thin wrapper around the Anthropic API for JSON-only requests.

The API key is read from the ANTHROPIC_API_KEY environment variable, never from code or config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

API_KEY_ENV = "ANTHROPIC_API_KEY"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

API_KEY_HELP = (
    f"{API_KEY_ENV} is not set, so the punctuation check (Anthropic API) was skipped.\n"
    "  Windows (PowerShell, this window only): $env:ANTHROPIC_API_KEY = \"sk-ant-...\"\n"
    "  Windows (permanent, then open a new window): setx ANTHROPIC_API_KEY \"sk-ant-...\"\n"
    "  macOS (this window only): export ANTHROPIC_API_KEY=\"sk-ant-...\"\n"
    "  macOS (permanent): add that export line to ~/.zshrc"
)


class LLMError(RuntimeError):
    """A request that produced no usable answer (refusal, truncated output, API failure)."""


class MissingApiKeyError(LLMError):
    pass


def has_api_key() -> bool:
    return bool(os.environ.get(API_KEY_ENV, "").strip())


@dataclass
class Usage:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0

    def add(self, usage) -> None:
        self.requests += 1
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_write_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0

    def cost(self, prices: dict) -> float:
        """Estimated US dollars, from per-million-token prices in config."""
        return (self.input_tokens * prices.get("input", 0)
                + self.output_tokens * prices.get("output", 0)
                + self.cache_write_tokens * prices.get("cache_write", 0)
                + self.cache_read_tokens * prices.get("cache_read", 0)) / 1_000_000


@dataclass
class ClaudeClient:
    model: str
    effort: str = "low"
    max_tokens: int = 16000
    fallbacks: str | None = "default"
    timeout_sec: float = 120.0
    max_retries: int = 3
    client: object | None = None          # an anthropic.Anthropic; created on first use
    usage: Usage = field(default_factory=Usage)

    @classmethod
    def from_settings(cls, settings: dict) -> "ClaudeClient":
        return cls(
            model=settings["model"],
            effort=settings.get("effort", "low"),
            max_tokens=settings.get("max_tokens", 16000),
            fallbacks=settings.get("fallbacks", "default"),
            timeout_sec=settings.get("request_timeout_sec", 120.0),
            max_retries=settings.get("max_retries", 3),
        )

    def _api(self):
        if self.client is None:
            if not has_api_key():
                raise MissingApiKeyError(API_KEY_HELP)
            import anthropic   # imported here so the other checks never need the package loaded
            # The SDK reads ANTHROPIC_API_KEY itself and retries 429 / 5xx / network errors.
            self.client = anthropic.Anthropic(timeout=self.timeout_sec, max_retries=self.max_retries)
        return self.client

    def complete_json(self, system: str, user: str, schema: dict) -> str:
        """Send one request whose reply must match `schema`. Returns the reply text (JSON)."""
        api = self._api()
        request = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            # The system prompt is identical for every request in a run: cache it.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
            "output_config": {"effort": self.effort,
                              "format": {"type": "json_schema", "schema": schema}},
        }
        if self.fallbacks:
            # If a safety classifier declines, the API reruns the request on a fallback model.
            response = api.beta.messages.create(betas=[FALLBACK_BETA], fallbacks=self.fallbacks, **request)
        else:
            response = api.messages.create(**request)

        self.usage.add(response.usage)
        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise LLMError(f"the model declined the request (category: {category})")
        if response.stop_reason == "max_tokens":
            raise LLMError("the reply was cut off at max_tokens")
        text = "".join(block.text for block in response.content if block.type == "text")
        if not text.strip():
            raise LLMError("the reply had no text")
        return text
