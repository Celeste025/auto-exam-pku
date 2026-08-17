"""BM25 条款检索（结构切块 + 前言过滤 + 分数间隙自适应）。

场次已用不同参考 PDF 区分本研，不再做 audience 筛选。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from .chunker import RegulationChunk, build_or_load_chunks, build_or_load_chunks_for_exam


def _tokenize(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    try:
        import jieba

        return [t.strip() for t in jieba.lcut(text) if t.strip() and not t.isspace()]
    except ImportError:
        toks = re.findall(r"[\u4e00-\u9fff]{1,}|[A-Za-z0-9]+", text)
        grams: list[str] = []
        for t in toks:
            if len(t) == 1:
                grams.append(t)
            else:
                grams.extend(t[i : i + 2] for i in range(len(t) - 1))
                grams.append(t)
        return grams


def _is_preamble(chunk: RegulationChunk) -> bool:
    return chunk.article in {"前言", "前言/目录"} or chunk.article.startswith("前言")


@dataclass
class RetrievedChunk:
    chunk: RegulationChunk
    score: float


class RegulationRetriever:
    def __init__(
        self,
        chunks: list[RegulationChunk],
        *,
        exclude_preamble: bool = True,
        preamble_penalty: float = 12.0,
    ) -> None:
        if exclude_preamble:
            self.chunks = [c for c in chunks if not _is_preamble(c)]
            skipped = len(chunks) - len(self.chunks)
            if skipped:
                print(f"[rag] 已过滤前言块: {skipped}，参与检索: {len(self.chunks)}")
        else:
            self.chunks = chunks
        self.preamble_penalty = preamble_penalty
        self._corpus_tokens = [
            _tokenize(c.doc_title + " " + c.article + " " + c.text) for c in self.chunks
        ]
        try:
            from rank_bm25 import BM25Okapi
        except ImportError as exc:
            raise SystemExit("请先安装: pip install rank_bm25 jieba") from exc
        if not self._corpus_tokens:
            self._bm25 = None
        else:
            self._bm25 = BM25Okapi(self._corpus_tokens)

    @classmethod
    def from_pdf(cls, pdf_path: str) -> RegulationRetriever:
        chunks = build_or_load_chunks(pdf_path, force_rebuild=False)
        print(f"[rag] 条款块数量(含前言): {len(chunks)}")
        return cls(chunks, exclude_preamble=True)

    @classmethod
    def from_exam(cls, exam) -> RegulationRetriever:
        chunks = build_or_load_chunks_for_exam(exam, force_rebuild=False)
        if not chunks:
            print(f"[rag] exam={exam.id} 无条款可索引（refs 为空）")
            return cls([], exclude_preamble=False)
        print(f"[rag] exam={exam.id} 条款块数量(含前言): {len(chunks)}")
        print(f"[rag] refs={[str(p) for p in exam.ref_paths]}")
        print(f"[rag] chunks_cache={exam.chunks_path}")
        return cls(chunks, exclude_preamble=True)

    def search(
        self,
        query: str,
        *,
        top_k: int = 4,
        adaptive: bool = True,
        gap_take1: float = 1.35,
        gap_take2: float = 1.15,
    ) -> list[RetrievedChunk]:
        tokens = _tokenize(query)
        if not tokens or self._bm25 is None or not self.chunks:
            return []
        scores = list(self._bm25.get_scores(tokens))

        boost_terms = [
            w
            for w in (
                "请假",
                "休学",
                "退学",
                "旷课",
                "张贴",
                "横幅",
                "刑事",
                "免予处罚",
                "代考",
                "开除学籍",
                "记过",
                "留校察看",
                "重修",
                "缓考",
                "通报批评",
                "书面警示",
            )
            if w in query
        ]
        for i, ch in enumerate(self.chunks):
            bonus = 0.0
            for w in boost_terms:
                if w in ch.text:
                    bonus += 1.5
            if _is_preamble(ch):
                bonus -= self.preamble_penalty
            if re.match(r"^第.+条$", ch.article):
                bonus += 0.8
            scores[i] = float(scores[i]) + bonus

        ranked: list[RetrievedChunk] = []
        for i, sc in enumerate(scores):
            if sc <= 0:
                continue
            ch = self.chunks[i]
            if _is_preamble(ch):
                continue
            ranked.append(RetrievedChunk(chunk=ch, score=float(sc)))
        ranked.sort(key=lambda x: x.score, reverse=True)
        hits = ranked[: max(top_k, 3)]

        if adaptive and len(hits) >= 2:
            s0, s1 = hits[0].score, hits[1].score
            ratio = s0 / s1 if s1 > 1e-6 else 99.0
            if ratio >= gap_take1:
                chosen = hits[:1]
                print(f"[rag] 自适应: Top1/Top2={ratio:.2f} >= {gap_take1}，只取 1 条")
            elif ratio >= gap_take2:
                chosen = hits[:2]
                print(f"[rag] 自适应: Top1/Top2={ratio:.2f} >= {gap_take2}，取 2 条")
            else:
                chosen = hits[:top_k]
                print(f"[rag] 自适应: Top1/Top2={ratio:.2f}，取 Top-{len(chosen)}")
            return chosen
        return hits[:top_k]

    def format_context(self, hits: Iterable[RetrievedChunk]) -> str:
        blocks = []
        for i, h in enumerate(hits, 1):
            blocks.append(f"[{i}] (score={h.score:.2f}) {h.chunk.prompt_block()}")
        return "\n\n".join(blocks)
