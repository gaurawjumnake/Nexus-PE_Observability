"""
LLM Client Abstraction
========================
All agents use this interface. Swap provider by changing the
NEXUS_LLM_PROVIDER environment variable — no agent code changes.

Supported providers:
    anthropic  →  claude-sonnet-4-5  (default)
    openai     →  gpt-4o

Usage:
    from app.core.llm_client import get_llm_client
    llm = get_llm_client()
    response = llm.complete(system="...", user="...")
"""

import os
import json
from abc import ABC, abstractmethod
from typing import Optional


class BaseLLMClient(ABC):
    @abstractmethod
    def complete(
        self,
        user: str,
        system: Optional[str] = None,
        max_tokens: int = 2048,
    ) -> str:
        """Send a prompt, return the text response."""


# ------------------------------------------------------------------
# Anthropic
# ------------------------------------------------------------------
class AnthropicClient(BaseLLMClient):
    def __init__(self, model: str = "claude-sonnet-4-5"):
        try:
            import anthropic
            self._client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
            self._model = model
        except ImportError:
            raise RuntimeError("pip install anthropic")

    def complete(self, user: str, system: Optional[str] = None, max_tokens: int = 2048) -> str:
        kwargs = {"model": self._model, "max_tokens": max_tokens,
                  "messages": [{"role": "user", "content": user}]}
        if system:
            kwargs["system"] = system
        msg = self._client.messages.create(**kwargs)
        return msg.content[0].text


# ------------------------------------------------------------------
# OpenAI
# ------------------------------------------------------------------
class OpenAIClient(BaseLLMClient):
    def __init__(self, model: str = "gpt-4o"):
        try:
            from openai import OpenAI
            self._client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
            self._model = model
        except ImportError:
            raise RuntimeError("pip install openai")

    def complete(self, user: str, system: Optional[str] = None, max_tokens: int = 2048) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        resp = self._client.chat.completions.create(
            model=self._model, messages=messages, max_tokens=max_tokens
        )
        return resp.choices[0].message.content #type:ignore


# ------------------------------------------------------------------
# Factory
# ------------------------------------------------------------------
def get_llm_client() -> BaseLLMClient:
    provider = os.getenv("NEXUS_LLM_PROVIDER", "anthropic").lower()
    if provider == "anthropic":
        return AnthropicClient()
    if provider == "openai":
        return OpenAIClient()
    raise ValueError(f"Unknown NEXUS_LLM_PROVIDER: {provider}")
