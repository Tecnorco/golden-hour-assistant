"""Live smoke test against Gemini. Run with: pytest -m live"""

import asyncio
from datetime import date

import pytest

from app.adapters.eml import EmlAdapter
from app.agents.extractor import extract_fields
from app.config import get_settings
from conftest import EMAILS

pytestmark = pytest.mark.live


@pytest.fixture
def llm():
    from app.llm.client import GeminiLLM

    if get_settings().api_key is None:
        pytest.skip("GEMINI_API_KEY not set")
    return GeminiLLM()


def test_live_extracts_wedding_quote(llm):
    inq = EmlAdapter().parse(EMAILS / "q05_wedding_full_day.eml")
    f = asyncio.run(extract_fields(inq, llm))
    assert f.service_type == "wedding_full_day"
    assert f.event_date == date(2027, 6, 12)
    assert f.location == "Lakeview Springs"
    assert set(f.add_ons) == {"second_photographer", "printed_album", "engagement_session"}
