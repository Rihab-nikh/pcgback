"""Shared OpenAI async client (module-level when the SDK is installed).

The accounting API can boot without the optional AI SDK for deterministic
reporting/tests. Extraction fails explicitly if invoked without the SDK.
"""
from app.core.config import OPENAI_API_KEY, OPENAI_BASE_URL


class _MissingOpenAIClient:
    def __getattr__(self, name):
        raise RuntimeError(
            "OpenAI extraction requires the 'openai' package. "
            "Install backend dependencies with: pip install -r requirements.txt"
        )


try:
    from openai import AsyncOpenAI
except ImportError:  # deterministic accounting/reporting can run without AI
    openai_client = _MissingOpenAIClient()
else:
    openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY, base_url=OPENAI_BASE_URL)
