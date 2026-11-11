"""Step: structured extraction of quote fields from an inquiry (one LLM call)."""

from __future__ import annotations

from app.agents.common import render_untrusted
from app.llm.client import LLM
from app.llm.prompts import load_prompt
from app.models import ExtractedFields, Inquiry
from app.observability.tracing import span


async def extract_fields(inq: Inquiry, llm: LLM) -> ExtractedFields:
    prompt = load_prompt("extractor")
    with span("extract_fields", input={"inquiry_id": inq.id, "prompt_version": prompt.version}) as s:
        resp = await llm.generate(
            prompt_name="extractor",
            system=prompt.text,
            turns=render_untrusted(inq),
            schema=ExtractedFields,
            temperature=0.0,
        )
        fields: ExtractedFields = resp.parsed
        # Dedupe while keeping order; extra_photos and the set count must agree.
        fields.add_ons = list(dict.fromkeys(fields.add_ons))
        if fields.extra_photo_sets > 0 and "extra_photos" not in fields.add_ons:
            fields.add_ons.append("extra_photos")
        if "extra_photos" in fields.add_ons and fields.extra_photo_sets == 0:
            fields.extra_photo_sets = 1
        s.output = fields.model_dump(mode="json")
        return fields
