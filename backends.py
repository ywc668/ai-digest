"""Scoring backends — pluggable LLM clients for the progressive scorer.

Two implementations:
  OllamaBackend    — local model via Ollama HTTP API (free, no rate limits)
  AnthropicBackend — Claude API (paid, rate-limited)

Both expose:  async complete(prompt, max_tokens) -> CompletionResult
"""

import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


@dataclass
class CompletionResult:
    text: str
    input_tokens: int
    output_tokens: int
    duration_ms: int
    backend: str
    model: str


class OllamaBackend:
    """Local scoring via Ollama. Thinking disabled, JSON output enforced."""

    name = "ollama"

    def __init__(
        self,
        model: str = "qwen3.6:latest",
        base_url: str = "http://localhost:11434",
        max_concurrent: int = 4,
        timeout: float = 120.0,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout)
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def complete(self, prompt: str, max_tokens: int = 150) -> CompletionResult:
        payload = {
            "model": self.model,
            "stream": False,
            "format": "json",
            "think": False,
            "options": {"num_predict": max_tokens, "temperature": 0},
            "messages": [{"role": "user", "content": prompt}],
        }
        async with self._semaphore:
            start = time.monotonic()
            resp = await self._client.post(f"{self.base_url}/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
        return CompletionResult(
            text=data["message"]["content"].strip(),
            input_tokens=data.get("prompt_eval_count", 0),
            output_tokens=data.get("eval_count", 0),
            duration_ms=int((time.monotonic() - start) * 1000),
            backend=self.name,
            model=self.model,
        )

    async def check_available(self) -> bool:
        """Verify the Ollama server is up and the model exists."""
        try:
            resp = await self._client.get(f"{self.base_url}/api/tags")
            resp.raise_for_status()
            models = [m["name"] for m in resp.json().get("models", [])]
            if self.model not in models and self.model.split(":")[0] not in [
                m.split(":")[0] for m in models
            ]:
                logger.error(f"Model '{self.model}' not found in Ollama. Available: {models}")
                return False
            return True
        except Exception as e:
            logger.error(f"Ollama not reachable at {self.base_url}: {e}")
            return False

    async def close(self):
        await self._client.aclose()


class _RateLimiter:
    """Token bucket rate limiter to stay under API req/min limits."""

    def __init__(self, requests_per_minute: int = 40):
        self._interval = 60.0 / requests_per_minute
        self._lock = asyncio.Lock()
        self._last_request = 0.0

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            wait = self._last_request + self._interval - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()


class AnthropicBackend:
    """Claude API scoring with rate limiting (kept as cloud fallback)."""

    name = "anthropic"

    def __init__(
        self,
        model: str = "claude-sonnet-4-20250514",
        requests_per_minute: int = 40,
        max_concurrent: int = 2,
    ):
        import anthropic  # deferred so ollama-only setups don't need the key

        self.model = model
        self._client = anthropic.AsyncAnthropic()
        self._rate_limiter = _RateLimiter(requests_per_minute)
        self._semaphore = asyncio.Semaphore(max_concurrent)

    async def complete(self, prompt: str, max_tokens: int = 150) -> CompletionResult:
        async with self._semaphore:
            await self._rate_limiter.acquire()
            start = time.monotonic()
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        return CompletionResult(
            text=text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            duration_ms=int((time.monotonic() - start) * 1000),
            backend=self.name,
            model=self.model,
        )

    async def check_available(self) -> bool:
        import os
        if not os.environ.get("ANTHROPIC_API_KEY"):
            logger.error("ANTHROPIC_API_KEY not set")
            return False
        return True

    async def close(self):
        pass


def create_backend(scoring_config: dict):
    """Build the configured backend. Falls back to legacy config keys."""
    backend_name = scoring_config.get("backend", "anthropic")
    if backend_name == "ollama":
        cfg = scoring_config.get("ollama", {})
        return OllamaBackend(
            model=cfg.get("model", "qwen3.6:latest"),
            base_url=cfg.get("base_url", "http://localhost:11434"),
            max_concurrent=cfg.get("max_concurrent", 4),
        )
    cfg = scoring_config.get("anthropic", {})
    return AnthropicBackend(
        model=cfg.get("model", scoring_config.get("model", "claude-sonnet-4-20250514")),
        requests_per_minute=cfg.get(
            "requests_per_minute", scoring_config.get("requests_per_minute", 40)
        ),
        max_concurrent=cfg.get("max_concurrent", scoring_config.get("max_concurrent", 2)),
    )
