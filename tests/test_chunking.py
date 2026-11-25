"""rag/chunking.py and the knowledge/ corpus, without Databricks."""
from pathlib import Path

import pytest
import yaml

from rag.chunking import FICTIONAL, MAX_CHARS, chunk_document, load_folder

ROOT = Path(__file__).resolve().parents[1]
CHUNKS = load_folder(ROOT / "knowledge")
BY_ID = {c["chunk_id"]: c for c in CHUNKS}


def test_corpus_has_eleven_labelled_documents():
    docs = {c["doc_id"]: c["doc_type"] for c in CHUNKS}
    assert len(docs) == 11
    assert sorted(set(docs.values())) == ["commentary", "glossary", "product", "sop"]
    assert sum(t == "product" for t in docs.values()) == 6 and sum(t == "commentary" for t in docs.values()) == 3
    for f in (ROOT / "knowledge").glob("*.md"):
        assert FICTIONAL in f.read_text(encoding="utf-8"), f.name


def test_chunks_are_unique_and_small():
    assert len(BY_ID) == len(CHUNKS)
    assert max(c["n_chars"] for c in CHUNKS) <= MAX_CHARS
    assert all(c["text"].startswith(c["title"]) for c in CHUNKS)


def test_one_glossary_term_per_chunk():
    for term in ["raroc", "hurdle-rate", "weighted-pipeline", "win-rate", "slipped-deal", "coverage-capacity",
                 "revenue-vs-plan", "products-per-client", "reconciliation-break"]:
        assert f"glossary::{term}" in BY_ID, term
    raroc = BY_ID["glossary::raroc"]["text"]
    assert "Formula:" in raroc and "Owner:" in raroc and "Hurdle rate" not in raroc.split("\n")[1]


def test_glossary_numbers_match_constants():
    c = yaml.safe_load((ROOT / "config" / "constants.yaml").read_text())
    hurdle = f"{c['raroc']['hurdle_rate']:.0%}"
    tax = f"{c['raroc']['tax_rate']:.0%}"
    assert hurdle in BY_ID["glossary::hurdle-rate"]["text"]
    assert tax in BY_ID["glossary::raroc"]["text"]
    assert "12%" not in " ".join(c["text"] for c in CHUNKS if c["doc_id"] == "glossary")


def test_long_section_is_split_and_bad_docs_rejected():
    long = "---\ndoc_id: x\ndoc_type: sop\ntitle: X\nas_of: 2026-01-01\n---\n# X\n" + FICTIONAL + "\n## Big\n" + \
           "\n\n".join(["word " * 60] * 20)
    parts = [c for c in chunk_document(long) if c["section"].startswith("Big")]
    assert len(parts) > 1 and all(c["n_chars"] <= MAX_CHARS for c in parts)
    with pytest.raises(ValueError):
        chunk_document("---\ndoc_id: x\ndoc_type: sop\ntitle: X\nas_of: 2026-01-01\n---\n# X\nno label")
    with pytest.raises(ValueError):
        chunk_document("# no front matter\n" + FICTIONAL)
