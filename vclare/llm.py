"""Minimal OpenAI-compatible chat client.

Only the Python standard library is used so the framework can be imported and
tested without extra dependencies. Install a provider SDK or point
``base_url`` at any OpenAI-compatible endpoint to run the LLM stages.

The prompt used for the two arbitration questions is deliberately absent from
the open-source release. ``LLMClient`` is therefore used only for specification
mining, targeted editing, code generation and testbench generation.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional


DEFAULT_BASE_URLS = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com",
}

DEFAULT_API_KEY_ENV = {
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}


class LLMClient:
    """Small chat-completions client for OpenAI-compatible providers."""

    def __init__(
        self,
        model: str,
        provider: str = "openai",
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout: float = 120.0,
    ) -> None:
        self.model = model
        self.provider = provider.lower()
        self.base_url = (
            base_url
            or os.getenv("VCLARE_BASE_URL")
            or DEFAULT_BASE_URLS.get(self.provider)
        )
        if not self.base_url:
            raise ValueError("base_url is required for provider {!r}".format(provider))

        env_var = DEFAULT_API_KEY_ENV.get(self.provider, "OPENAI_API_KEY")
        self.api_key = api_key or os.getenv(env_var) or os.getenv("OPENAI_API_KEY")
        self.timeout = timeout

    def available(self) -> bool:
        return bool(self.api_key)

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        **extra: Any,
    ) -> str:
        if not self.api_key:
            raise RuntimeError(
                "No API key configured. Set OPENAI_API_KEY / DEEPSEEK_API_KEY "
                "or pass api_key explicitly."
            )

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        payload.update(extra)

        request = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer {}".format(self.api_key),
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                "LLM request failed with HTTP {}: {}".format(error.code, detail)
            ) from error
        except urllib.error.URLError as error:
            raise RuntimeError("LLM request failed: {}".format(error)) from error

        try:
            return body["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, AttributeError) as error:
            raise RuntimeError("Unexpected LLM response: {}".format(body)) from error


def system_user(system: str, user: str) -> List[Dict[str, str]]:
    """Build a two-message conversation."""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
