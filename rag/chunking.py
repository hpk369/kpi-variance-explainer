"""Splits the knowledge/ Markdown files into chunks for Vector Search.

One chunk per heading section (so one glossary term = one chunk), at most MAX_CHARS characters (~400 tokens).
Each chunk's text starts with the document title and section, so a query like "M&A advisory revenue drivers"
matches the right one-pager section. Pure Python: used by rag/01_chunk_and_load and by the unit tests.

    from rag.chunking import load_folder
    chunks = load_folder("knowledge")      # list of dicts, one per chunk
Northlake Bank (fictional). Synthetic data only.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

MAX_CHARS = 1600            # ~400 tokens for English prose
FICTIONAL = "Northlake Bank (fictional), synthetic"
REQUIRED_META = ("doc_id", "doc_type", "title", "as_of")
COLUMNS = ["chunk_id", "doc_id", "doc_type", "title", "section", "section_path", "as_of", "owner",
           "chunk_index", "text", "n_chars", "source_file", "content_hash"]

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


def parse_front_matter(raw: str) -> tuple[dict, str]:
    """Simple `key: value` front matter between --- lines (no YAML library needed on the cluster)."""
    if not raw.startswith("---"):
        return {}, raw
    end = raw.find("\n---", 3)
    if end < 0:
        return {}, raw
    meta = {}
    for line in raw[3:end].strip().splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip('"').strip("'")
    return meta, raw[end + 4:].lstrip("\n")


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "section"


def _split_long(body: str, limit: int) -> list[str]:
    """Split on blank lines (then on lines) so no part is longer than limit."""
    if len(body) <= limit:
        return [body]
    parts, cur = [], ""
    for block in re.split(r"\n\s*\n", body):
        pieces = [block] if len(block) <= limit else block.splitlines()
        for piece in pieces:
            if cur and len(cur) + len(piece) + 2 > limit:
                parts.append(cur)
                cur = piece
            else:
                cur = f"{cur}\n\n{piece}" if cur else piece
    if cur:
        parts.append(cur)
    return parts


def chunk_document(raw: str, source_file: str = "", max_chars: int = MAX_CHARS) -> list[dict]:
    meta, body = parse_front_matter(raw)
    missing = [k for k in REQUIRED_META if not meta.get(k)]
    if missing:
        raise ValueError(f"{source_file}: front matter is missing {', '.join(missing)}")
    if FICTIONAL not in body:
        raise ValueError(f"{source_file}: must contain the line '{FICTIONAL}'")
    doc_id, title = meta["doc_id"], meta["title"]

    # Walk the headings; each section is the text under the deepest heading until the next heading.
    sections, path, buf = [], [], []

    def flush():
        text = "\n".join(buf).strip()
        if text:
            sections.append(([name for lvl, name in path if lvl > 1], text))   # H1 repeats the title: drop it
        buf.clear()

    for line in body.splitlines():
        m = _HEADING.match(line)
        if m:
            flush()
            level = len(m.group(1))
            path[:] = [p for p in path if p[0] < level] + [(level, m.group(2).strip())]
        else:
            buf.append(line)
    flush()

    chunks, seen = [], set()
    for inner, text in sections:
        section = inner[-1] if inner else "Overview"
        section_path = " > ".join(inner) if inner else "Overview"
        parts = _split_long(text, max_chars - len(title) - len(section_path) - 20)
        for i, part in enumerate(parts, 1):
            label = section if len(parts) == 1 else f"{section} (part {i})"
            cid = f"{doc_id}::{_slug(label)}"
            n = 2
            while cid in seen:
                cid = f"{doc_id}::{_slug(label)}-{n}"
                n += 1
            seen.add(cid)
            full = f"{title}\nSection: {section_path}\n\n{part}"
            chunks.append({
                "chunk_id": cid, "doc_id": doc_id, "doc_type": meta["doc_type"], "title": title,
                "section": label, "section_path": section_path, "as_of": meta["as_of"], "owner": meta.get("owner", ""),
                "chunk_index": len(chunks), "text": full, "n_chars": len(full), "source_file": source_file,
                "content_hash": hashlib.sha256(full.encode("utf-8")).hexdigest()[:16],
            })
    return chunks


def load_folder(folder: str | Path, max_chars: int = MAX_CHARS) -> list[dict]:
    folder = Path(folder)
    files = sorted(folder.glob("*.md"))
    if not files:
        raise FileNotFoundError(f"No .md files in {folder}")
    out, ids = [], set()
    for f in files:
        doc = chunk_document(f.read_text(encoding="utf-8"), f.name, max_chars)
        if doc[0]["doc_id"] in ids:
            raise ValueError(f"{f.name}: doc_id {doc[0]['doc_id']} is used twice")
        ids.add(doc[0]["doc_id"])
        out.extend(doc)
    return out


if __name__ == "__main__":   # python rag/chunking.py  -> chunk counts per document
    import sys
    from collections import Counter

    chunks = load_folder(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "knowledge")
    for doc, n in Counter(c["doc_id"] for c in chunks).items():
        print(f"{doc:30} {n:3} chunks")
    print(f"{len(chunks)} chunks, longest {max(c['n_chars'] for c in chunks)} characters")
