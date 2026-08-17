"""BM25 条款检索（结构切块 + 分数间隙自适应）。

自适应按 Top1/Top2 比值多档取条，永不只取 1 条。
重试路径支持多路查询 + RRF 融合（关闭 early-stop）。
前言块已按小节切分，参与检索（不再整块过滤）。
场次已用不同参考 PDF 区分本研，不再做 audience 筛选。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

from .chunker import RegulationChunk, build_or_load_chunks, build_or_load_chunks_for_exam

# (Top1/Top2 下限, 取条数)：比值越高越少取；阈值偏松，减少轻易缩到 2/3。
# 未命中任一档时取满 top_k；条数会被夹到 [2, top_k]。
DEFAULT_GAP_LEVELS: tuple[tuple[float, int], ...] = (
    (1.55, 2),
    (1.35, 3),
    (1.22, 4),
)

_STOPWORDS = frozenset(
    {
        "的",
        "了",
        "是",
        "在",
        "和",
        "与",
        "或",
        "及",
        "等",
        "下列",
        "根据",
        "关于",
        "正确",
        "错误",
        "属于",
        "以下",
        "哪项",
        "哪些",
        "什么",
        "如何",
        "可以",
        "应当",
        "应该",
        "是否",
        "一个",
        "一种",
        "学生",
        "学校",
        "本题",
        "选项",
    }
)


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


def _keyword_subquery(query: str, *, max_terms: int = 40) -> str:
    """题干关键词子查询（通用分词，不维护易混词表）。"""
    seen: set[str] = set()
    keep: list[str] = []
    for t in _tokenize(query):
        if len(t) < 2 or t in _STOPWORDS or t in seen:
            continue
        seen.add(t)
        keep.append(t)
        if len(keep) >= max_terms:
            break
    return " ".join(keep)


@dataclass
class RetrievedChunk:
    chunk: RegulationChunk
    score: float


class RegulationRetriever:
    def __init__(
        self,
        chunks: list[RegulationChunk],
        *,
        exclude_preamble: bool = False,
        preamble_penalty: float = 0.0,
    ) -> None:
        if exclude_preamble:
            self.chunks = [c for c in chunks if not _is_preamble(c)]
            skipped = len(chunks) - len(self.chunks)
            if skipped:
                print(f"[rag] 已过滤前言块: {skipped}，参与检索: {len(self.chunks)}")
        else:
            self.chunks = chunks
            n_pre = sum(1 for c in chunks if _is_preamble(c))
            if n_pre:
                print(f"[rag] 前言块参与检索: {n_pre}，总块数: {len(self.chunks)}")
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
        return cls(chunks, exclude_preamble=False)

    @classmethod
    def from_exam(cls, exam) -> RegulationRetriever:
        chunks = build_or_load_chunks_for_exam(exam, force_rebuild=False)
        if not chunks:
            print(f"[rag] exam={exam.id} 无条款可索引（refs 为空）")
            return cls([], exclude_preamble=False)
        print(f"[rag] exam={exam.id} 条款块数量(含前言): {len(chunks)}")
        print(f"[rag] refs={[str(p) for p in exam.ref_paths]}")
        print(f"[rag] chunks_cache={exam.chunks_path}")
        return cls(chunks, exclude_preamble=False)

    def _bm25_rank(self, query: str) -> list[RetrievedChunk]:
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
            if _is_preamble(ch) and self.preamble_penalty:
                bonus -= self.preamble_penalty
            if re.match(r"^第.+条$", ch.article):
                bonus += 0.8
            scores[i] = float(scores[i]) + bonus

        ranked: list[RetrievedChunk] = []
        for i, sc in enumerate(scores):
            if sc <= 0:
                continue
            ranked.append(RetrievedChunk(chunk=self.chunks[i], score=float(sc)))
        ranked.sort(key=lambda x: x.score, reverse=True)
        return ranked

    def search(
        self,
        query: str,
        *,
        top_k: int = 4,
        adaptive: bool = True,
        gap_levels: Sequence[tuple[float, int]] | None = None,
    ) -> list[RetrievedChunk]:
        """检索条款。

        adaptive（永不只取 1 条）按 Top1/Top2 从高到低匹配 gap_levels：
        默认 1.55→2 / 1.35→3 / 1.22→4，否则取满 top_k。阈值越大越难 early-stop。
        """
        ranked = self._bm25_rank(query)
        if not ranked:
            return []

        levels = list(gap_levels) if gap_levels is not None else list(DEFAULT_GAP_LEVELS)
        levels = sorted(
            ((float(g), max(2, min(int(k), top_k))) for g, k in levels),
            key=lambda x: x[0],
            reverse=True,
        )
        hits = ranked[:top_k]

        if adaptive and len(hits) >= 2:
            s0, s1 = hits[0].score, hits[1].score
            ratio = s0 / s1 if s1 > 1e-6 else 99.0
            take = top_k
            matched_gap: float | None = None
            for gap, k in levels:
                if ratio >= gap:
                    take = k
                    matched_gap = gap
                    break
            chosen = hits[:take]
            if matched_gap is not None:
                print(
                    f"[rag] 自适应: Top1/Top2={ratio:.2f} >= {matched_gap}，取 {len(chosen)} 条"
                )
            else:
                print(f"[rag] 自适应: Top1/Top2={ratio:.2f}，取 Top-{len(chosen)}")
            return chosen
        return hits[:top_k]

    def search_rrf(
        self,
        query: str,
        *,
        top_k: int = 8,
        rrf_k: int = 60,
        pool: int | None = None,
    ) -> list[RetrievedChunk]:
        """多路召回 + RRF 融合，不做 early-stop（用于缺信息重试）。

        路径：完整题干 BM25 + 关键词子查询 BM25。
        """
        sub = _keyword_subquery(query)
        queries = [query]
        if sub and sub.strip() != query.strip():
            queries.append(sub)
            print(f"[rag] RRF 关键词路: {sub[:120]}{'…' if len(sub) > 120 else ''}")
        else:
            print("[rag] RRF: 仅题干一路（关键词路与题干等价，跳过）")

        cand_n = pool if pool is not None else max(top_k * 4, 24)
        rrf_scores: dict[str, float] = {}
        best_bm25: dict[str, float] = {}
        chunk_by_id: dict[str, RegulationChunk] = {}

        for q in queries:
            ranked = self._bm25_rank(q)
            for rank, hit in enumerate(ranked[:cand_n]):
                cid = hit.chunk.chunk_id
                rrf_scores[cid] = rrf_scores.get(cid, 0.0) + 1.0 / (rrf_k + rank + 1)
                chunk_by_id[cid] = hit.chunk
                prev = best_bm25.get(cid, 0.0)
                if hit.score > prev:
                    best_bm25[cid] = hit.score

        merged = [
            RetrievedChunk(chunk=chunk_by_id[cid], score=score)
            for cid, score in rrf_scores.items()
        ]
        merged.sort(
            key=lambda h: (h.score, best_bm25.get(h.chunk.chunk_id, 0.0)),
            reverse=True,
        )
        chosen = merged[:top_k]
        print(f"[rag] RRF 融合: paths={len(queries)} → Top-{len(chosen)}（无 early-stop）")
        return chosen

    def format_context(self, hits: Iterable[RetrievedChunk]) -> str:
        blocks = []
        for i, h in enumerate(hits, 1):
            blocks.append(f"[{i}] (score={h.score:.2f}) {h.chunk.prompt_block()}")
        return "\n\n".join(blocks)
