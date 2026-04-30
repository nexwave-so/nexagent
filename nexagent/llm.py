"""OpenRouter LLM client for Nexagent cold path."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from .config import Config

logger = logging.getLogger(__name__)

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class LLMClient:
    """Async OpenRouter client. All calls are best-effort — failures are logged, never raised."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.config.openrouter_api_key)

    async def start(self) -> None:
        if not self.enabled:
            logger.info("LLM cold path disabled (no OPENROUTER_API_KEY)")
            return
        self._client = httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {self.config.openrouter_api_key}",
                "HTTP-Referer": "https://nexwave.so",
                "X-Title": "Nexagent",
                "Content-Type": "application/json",
            },
            timeout=120.0,
        )
        logger.info(
            "LLM client ready (fast=%s, reason=%s)",
            self.config.llm_model_fast,
            self.config.llm_model_reasoning,
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    async def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        model: str | None = None,
        max_tokens: int = 1000,
        temperature: float = 0.3,
    ) -> str | None:
        """Send a chat completion request. Returns the response text or None on failure."""
        if not self._client:
            return None

        use_model = model or self.config.llm_model_fast
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        body: dict[str, Any] = {
            "model": use_model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        try:
            resp = await self._client.post(_OPENROUTER_URL, json=body)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            # Strip markdown fences if present
            if content.startswith("```"):
                content = content.split("\n", 1)[1] if "\n" in content else content[3:]
                if content.endswith("```"):
                    content = content[:-3]
            return content.strip()
        except Exception as e:
            logger.warning("LLM call failed (model=%s): %s", use_model, e)
            return None

    async def complete_json(
        self,
        prompt: str,
        *,
        system: str = "",
        model: str | None = None,
        max_tokens: int = 1000,
        temperature: float = 0.1,
    ) -> dict | None:
        """Send a completion and parse the response as JSON."""
        raw = await self.complete(
            prompt,
            system=system + "\n\nRespond ONLY with valid JSON. No markdown, no backticks, no preamble.",
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        if not raw:
            return None
        try:
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3].strip()
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.warning("LLM returned invalid JSON: %s — raw: %.200s", e, raw)
            return None
