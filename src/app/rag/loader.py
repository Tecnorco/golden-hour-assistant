"""Load policy documents (markdown) from data/docs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# The brand-voice guide is a style guide for drafting, not a customer-facing
# policy, so it is fed to agents directly and never retrieved or cited.
EXCLUDED = {"brand-voice"}


@dataclass(frozen=True)
class Document:
    source: str  # file stem, e.g. "deposits-and-payment"
    title: str  # first H1
    text: str


def load_documents(docs_dir: Path) -> list[Document]:
    docs: list[Document] = []
    for path in sorted(docs_dir.glob("*.md")):
        if path.stem in EXCLUDED:
            continue
        text = path.read_text(encoding="utf-8")
        title = next((ln[2:].strip() for ln in text.splitlines() if ln.startswith("# ")), path.stem)
        docs.append(Document(source=path.stem, title=title, text=text))
    return docs
