"""AI provider switch. AI_PROVIDER=gemini for real runs, mock for offline tests."""
from ..config import PROVIDER


def get_provider():
    if PROVIDER == "mock":
        from .mock import MockProvider
        return MockProvider()
    from .gemini import GeminiProvider
    return GeminiProvider()
