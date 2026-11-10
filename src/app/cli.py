"""Command line entry point: python -m app.cli <command>."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.adapters.eml import EmlAdapter
from app.config import get_settings
from app.observability import tracing
from app.store import Store


def _store() -> Store:
    store = Store(get_settings().db_path)
    tracing.set_sink(store)  # every span from here on is persisted
    return store


def _emails(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in paths or [str(get_settings().data_dir / "emails")]:
        path = Path(p)
        out.extend(sorted(path.glob("*.eml")) if path.is_dir() else [path])
    return out


def cmd_parse(args: argparse.Namespace) -> int:
    adapter = EmlAdapter()
    for path in _emails(args.paths):
        inq = adapter.parse(path)
        print(json.dumps(inq.model_dump(mode="json", exclude={"body"}), indent=2))
        print(inq.body[:400], "\n" + "-" * 60)
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    from app.agents.extractor import extract_fields
    from app.llm.client import GeminiLLM

    store = _store()
    llm = GeminiLLM()
    adapter = EmlAdapter()

    async def run() -> None:
        for path in _emails(args.paths):
            inq = adapter.parse(path)
            store.add_inquiry(inq)
            with tracing.trace(inq.id, name="extract", input={"file": path.name}):
                fields = await extract_fields(inq, llm)
            print(f"{path.name:36s} {inq.id}  {fields.model_dump_json()}")

    asyncio.run(run())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli", description="Golden Hour Assistant")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("parse", help="parse .eml files into Inquiry objects (no LLM)")
    p.add_argument("paths", nargs="*")
    p.set_defaults(func=cmd_parse)

    p = sub.add_parser("extract", help="run field extraction on .eml files (calls Gemini)")
    p.add_argument("paths", nargs="*")
    p.set_defaults(func=cmd_extract)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
