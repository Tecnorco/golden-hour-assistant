"""Live router smoke test. Run with: pytest -m live"""

import asyncio

import pytest

from app.adapters.eml import EmlAdapter
from app.agents.router import route
from app.config import get_settings
from conftest import EMAILS

pytestmark = pytest.mark.live


@pytest.mark.parametrize("name,label", [
    ("f03_faq_what_to_wear.eml", "question"),
    ("r02_cancel_wedding.eml", "reschedule"),
    ("s01_spam.eml", "spam"),
    ("v01_vague_price.eml", "quote_request"),
])
def test_live_routes(name, label):
    if get_settings().api_key is None:
        pytest.skip("GEMINI_API_KEY not set")
    from app.llm.client import GeminiLLM

    d = asyncio.run(route(EmlAdapter().parse(EMAILS / name), GeminiLLM()))
    assert d.label == label and d.confidence >= 0.7
