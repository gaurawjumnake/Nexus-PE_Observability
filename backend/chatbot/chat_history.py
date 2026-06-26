"""
Chat history store + query rewriter.

- In-memory per-session store (survives the process lifetime).
- LLM rewrite is only triggered when the question contains follow-up signals
  (pronouns / deictic references like "this", "it", "the score").
  All other queries are passed through unchanged — zero extra LLM cost.
"""

import re
from collections import deque
from dataclasses import dataclass
from typing import Optional

# Signals that the question refers to something from a prior turn.
_FOLLOWUP_RE = re.compile(
    r"\b(this|it|that|its|these|those|the same|above|previous|mentioned|"
    r"the score|the kpi|the metric|the value|the result)\b",
    re.IGNORECASE,
)


@dataclass
class ChatMessage:
    role: str    # "user" or "assistant"
    content: str


class ChatHistoryStore:
    """Module-level singleton: session_id → deque[ChatMessage]."""

    _store: dict[str, deque] = {}

    @classmethod
    def get(cls, session_id: str, top_c: int = 5) -> list[ChatMessage]:
        """Return the last *top_c* user/assistant exchange pairs (2×top_c messages)."""
        msgs = cls._store.get(session_id)
        if not msgs:
            return []
        return list(msgs)[-(top_c * 2):]

    @classmethod
    def add(cls, session_id: str, role: str, content: str, max_size: int = 50) -> None:
        if session_id not in cls._store:
            cls._store[session_id] = deque(maxlen=max_size)
        cls._store[session_id].append(ChatMessage(role=role, content=content))

    @classmethod
    def clear(cls, session_id: str) -> bool:
        """Delete all history for *session_id*. Returns True if the session existed."""
        if session_id in cls._store:
            del cls._store[session_id]
            return True
        return False


def needs_rewrite(question: str) -> bool:
    """Return True only if the question contains follow-up signals."""
    return bool(_FOLLOWUP_RE.search(question))


def rewrite_with_history(question: str, history: list[ChatMessage], model: str) -> str:
    """One-shot LLM call: produce a self-contained version of *question*."""
    if not history:
        return question

    from backend.utilites.llm_models import get_llm_client

    history_text = "\n".join(f"{m.role.upper()}: {m.content}" for m in history)
    prompt = (
        "Rewrite the follow-up question to be fully self-contained using the conversation history.\n"
        "Return ONLY the rewritten question — no explanation, no prefix.\n"
        "If it is already self-contained, return it unchanged.\n\n"
        f"History:\n{history_text}\n\n"
        f"Question: {question}\n\n"
        "Rewritten question:"
    )

    try:
        client = get_llm_client()
        rewritten = (client.complete(prompt) or "").strip()
        return rewritten if rewritten else question
    except Exception:
        return question
