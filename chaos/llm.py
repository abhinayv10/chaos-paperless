"""Minimal LLM provider interface for the planner and analyst agents.

A provider turns (system prompt, user prompt) into one JSON object. That is
its whole job: it has no tools, cannot run commands and never touches Docker.
Everything it returns is validated by the caller before use.

Selection (CHAOS_LLM_PROVIDER): ollama | anthropic | openai | none | auto.
'auto' (default) uses Anthropic when credentials are present, else a
reachable local Ollama, else no provider. With no provider the agents fall
back to deterministic rules and label their output as such.
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional, Protocol

import httpx

OLLAMA_URL = os.environ.get("CHAOS_OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("CHAOS_OLLAMA_MODEL", "llama3.1")
ANTHROPIC_MODEL = os.environ.get("CHAOS_ANTHROPIC_MODEL", "claude-opus-5-5")
OPENAI_URL = os.environ.get("CHAOS_OPENAI_URL", "https://api.openai.com/v1")
OPENAI_MODEL = os.environ.get("CHAOS_OPENAI_MODEL", "")
LLM_TIMEOUT_S = float(os.environ.get("CHAOS_LLM_TIMEOUT_S", "180"))


class LLMError(Exception):
    """Provider unreachable, refused, or returned something that is not JSON."""


class Provider(Protocol):
    name: str
    model: str

    def complete_json(self, system: str, user: str) -> dict: ...


def extract_json(text: str) -> dict:
    """Parse a JSON object from model output, tolerating code fences and
    surrounding prose. Anything else is an error, never a guess."""
    if not isinstance(text, str) or not text.strip():
        raise LLMError("empty response")
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE)
    for candidate in (cleaned, cleaned[cleaned.find("{"): cleaned.rfind("}") + 1]):
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
        raise LLMError("response is JSON but not an object")
    raise LLMError("response is not valid JSON")


class OllamaProvider:
    name = "ollama"

    def __init__(self, model: str = OLLAMA_MODEL, url: str = OLLAMA_URL, timeout: float = LLM_TIMEOUT_S):
        self.model, self.url, self.timeout = model, url.rstrip("/"), timeout

    def complete_json(self, system: str, user: str) -> dict:
        try:
            r = httpx.post(f"{self.url}/api/chat", timeout=self.timeout, json={
                "model": self.model, "stream": False, "format": "json",
                "options": {"temperature": 0.2},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            })
            r.raise_for_status()
            return extract_json(r.json()["message"]["content"])
        except LLMError:
            raise
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"ollama: {type(e).__name__}: {e}") from e


class OpenAICompatibleProvider:
    """Any /chat/completions endpoint (OpenAI, LM Studio, vLLM, ...)."""

    name = "openai"

    def __init__(self, model: str = OPENAI_MODEL, url: str = OPENAI_URL, api_key: str | None = None,
                 timeout: float = LLM_TIMEOUT_S):
        self.model, self.url, self.timeout = model, url.rstrip("/"), timeout
        self.api_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")

    def complete_json(self, system: str, user: str) -> dict:
        if not self.model:
            raise LLMError("openai: set CHAOS_OPENAI_MODEL")
        try:
            r = httpx.post(f"{self.url}/chat/completions", timeout=self.timeout,
                           headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
                           json={"model": self.model, "response_format": {"type": "json_object"},
                                 "messages": [{"role": "system", "content": system},
                                              {"role": "user", "content": user}]})
            r.raise_for_status()
            return extract_json(r.json()["choices"][0]["message"]["content"])
        except LLMError:
            raise
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"openai: {type(e).__name__}: {e}") from e


class AnthropicProvider:
    """Claude via the official SDK (pip install anthropic). Credentials come
    from the environment (ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / profile)."""

    name = "anthropic"

    def __init__(self, model: str = ANTHROPIC_MODEL, timeout: float = LLM_TIMEOUT_S):
        self.model, self.timeout = model, timeout

    def complete_json(self, system: str, user: str) -> dict:
        try:
            import anthropic
        except ImportError as e:
            raise LLMError("anthropic: SDK not installed (pip install anthropic)") from e
        try:
            client = anthropic.Anthropic(timeout=self.timeout)
            # fallbacks="default": a safety-classifier decline is re-run on
            # Anthropic's recommended fallback model instead of failing the call
            response = client.beta.messages.create(
                model=self.model, max_tokens=16000, system=system,
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
                output_config={"effort": "medium"},
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.APIConnectionError as e:
            raise LLMError(f"anthropic: connection error: {e}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"anthropic: HTTP {e.status_code}: {e.message}") from e
        except Exception as e:  # noqa: BLE001 - e.g. missing credentials
            raise LLMError(f"anthropic: {type(e).__name__}: {e}") from e
        if response.stop_reason == "refusal":
            raise LLMError("anthropic: request declined (stop_reason=refusal)")
        if response.stop_reason == "max_tokens":
            raise LLMError("anthropic: response truncated (max_tokens)")
        text = next((b.text for b in response.content if b.type == "text"), "")
        return extract_json(text)


def _ollama_reachable(url: str = OLLAMA_URL) -> bool:
    try:
        return httpx.get(f"{url.rstrip('/')}/api/tags", timeout=1.5).status_code == 200
    except Exception:  # noqa: BLE001
        return False


def get_provider(choice: str | None = None) -> Optional[Provider]:
    """Resolve the configured provider. None means: no LLM, use rules."""
    choice = (choice or os.environ.get("CHAOS_LLM_PROVIDER", "auto")).strip().lower()
    if choice in ("none", "rules", "off"):
        return None
    if choice == "ollama":
        return OllamaProvider()
    if choice == "anthropic":
        return AnthropicProvider()
    if choice == "openai":
        return OpenAICompatibleProvider()
    if choice != "auto":
        raise LLMError(f"unknown CHAOS_LLM_PROVIDER '{choice}'")
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return AnthropicProvider()
    if _ollama_reachable():
        return OllamaProvider()
    if os.environ.get("OPENAI_API_KEY") and OPENAI_MODEL:
        return OpenAICompatibleProvider()
    return None


def describe(provider: Optional[Provider]) -> dict:
    if provider is None:
        return {"provider": None, "model": None,
                "note": "no LLM provider configured; agents use deterministic rules"}
    return {"provider": provider.name, "model": provider.model}
