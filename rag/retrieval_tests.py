"""Gate 5 retrieval tests: six questions, each must find its expected document in the top 3.

Run:  python rag/retrieval_tests.py              # against the Vector Search index (needs .env)
      python rag/retrieval_tests.py --offline    # keyword (BM25) check on the local knowledge/ files, no Databricks
      python rag/retrieval_tests.py --show 5     # print the top 5 instead of the top 3

The expected section is shown for information; only the document decides PASS/FAIL.
Exit code 1 if any test fails. Northlake Bank (fictional). Synthetic data only.
"""
from __future__ import annotations

import argparse
import math
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TOP = 3
# (question, expected doc_id, expected section)
TESTS = [
    ("what is RAROC", "glossary", "RAROC"),
    ("hurdle rate", "glossary", "Hurdle rate"),
    ("how do we treat slipped deals", "glossary", "Slipped deal"),
    ("DQ break escalation", "sop_dq_breaks", "Escalation"),
    ("M&A advisory revenue drivers", "product_ma_advisory", "Revenue drivers"),
    ("weighted pipeline", "glossary", "Weighted pipeline"),
]


def search_index(query: str, k: int) -> list[dict]:
    from common import dbx

    return dbx.vs_query(query, k=k, columns=["chunk_id", "doc_id", "section"])


class Bm25:
    """Plain keyword search over the local chunks: a sanity check that runs without Databricks."""

    def __init__(self, chunks: list[dict]):
        self.chunks = chunks
        self.docs = [self._tokens(c["text"]) for c in chunks]
        self.avg = sum(map(len, self.docs)) / len(self.docs)
        df = Counter(t for d in self.docs for t in set(d))
        self.idf = {t: math.log(1 + (len(self.docs) - n + 0.5) / (n + 0.5)) for t, n in df.items()}

    @staticmethod
    def _tokens(s: str) -> list[str]:
        return re.findall(r"[a-z0-9&]+", s.lower())

    def search(self, query: str, k: int) -> list[dict]:
        q = self._tokens(query)
        scored = []
        for c, d in zip(self.chunks, self.docs):
            tf = Counter(d)
            s = sum(self.idf.get(t, 0) * tf[t] * 2.2 / (tf[t] + 1.2 * (0.25 + 0.75 * len(d) / self.avg)) for t in q)
            scored.append({"chunk_id": c["chunk_id"], "doc_id": c["doc_id"], "section": c["section"], "score": round(s, 3)})
        return sorted(scored, key=lambda r: -r["score"])[:k]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--offline", action="store_true", help="BM25 over local knowledge/ instead of Vector Search")
    ap.add_argument("--show", type=int, default=TOP, help="how many results to print per question")
    args = ap.parse_args()

    if args.offline:
        from rag.chunking import load_folder

        engine = Bm25(load_folder(ROOT / "knowledge"))
        search, where = engine.search, "local BM25 (offline)"
    else:
        from common import dbx

        search, where = search_index, f"Vector Search index {dbx.vs_index_name()}"
    print(f"Retrieval tests on {where}: expected document in the top {TOP}\n")

    passed = 0
    for i, (q, doc, section) in enumerate(TESTS, 1):
        try:
            hits = search(q, max(TOP, args.show))
        except Exception as ex:
            print(f"[{i}] FAIL  {q!r}\n      error: {str(ex)[:300]}\n")
            continue
        top_docs = [h.get("doc_id") for h in hits[:TOP]]
        ok = doc in top_docs
        passed += ok
        rank = top_docs.index(doc) + 1 if ok else None
        print(f"[{i}] {'PASS' if ok else 'FAIL'}  {q!r} → expected {doc} › {section}" + (f" (rank {rank})" if ok else ""))
        for r, h in enumerate(hits[: args.show], 1):
            mark = "✓" if h.get("doc_id") == doc and h.get("section") == section else " "
            print(f"      {r}. {mark} {h.get('doc_id')} › {h.get('section')}  score={h.get('score')}")
        print()
    print(f"{passed}/{len(TESTS)} passed")
    return 0 if passed == len(TESTS) else 1


if __name__ == "__main__":
    sys.exit(main())
