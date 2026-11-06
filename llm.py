"""Groq chat-model setup and availability reporting."""
from __future__ import annotations

from typing import Dict

import config


class LLMUnavailable(RuntimeError):
    """The configured Groq model cannot be used."""


def get_chat_model(model: str = ""):
    """Build the LangChain chat model used by the agent."""
    api_key = config.GROQ_API_KEY
    if not api_key:
        raise LLMUnavailable(
            "Groq is not configured. Set GROQ_API_KEY in data/.env and restart the server."
        )

    try:
        from langchain_groq import ChatGroq
    except ImportError as exc:
        raise LLMUnavailable(
            "The Groq integration is missing. Install project dependencies with "
            "'python -m pip install -r requirements.txt'."
        ) from exc

    return ChatGroq(
        model=(model or config.GROQ_MODEL).strip(),
        api_key=api_key,
        temperature=config.LLM_TEMPERATURE,
        max_retries=2,
        timeout=30,
    )


def groq_status() -> Dict[str, object]:
    """Report local configuration without making a billable API request."""
    if not config.GROQ_API_KEY:
        return {
            "ok": False,
            "model": None,
            "message": "Groq is not configured. Set GROQ_API_KEY in data/.env.",
        }
    return {
        "ok": True,
        "model": config.GROQ_MODEL,
        "message": f"Groq is configured for model {config.GROQ_MODEL}.",
    }
