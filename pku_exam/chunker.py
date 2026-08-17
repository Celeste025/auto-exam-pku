"""按校规结构切块：法规文件 + 第X条（非固定字数）。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from .pdf_text import extract_pdf_text

Audience = Literal["undergrad", "graduate", "general"]

ARTICLE_SPLIT = re.compile(r"(?=第[一二三四五六七八九十百千零〇两\d]+条\s)")
ARTICLE_HEAD = re.compile(r"^(第[一二三四五六七八九十百千零〇两\d]+条)")

# 研究生手册为主 + 共用规章；长标题优先匹配，避免短标题误伤
KNOWN_DOC_TITLES: list[str] = [
    "中华人民共和国学位法",
    "普通高等学校学生管理规定",
    "高等学校预防与处理学术不端行为办法",
    "学位论文作假行为处理办法",
    "北京大学章程",
    "北京大学学位授予工作实施办法",
    "北京大学研究生学籍管理办法",
    "北京大学研究生休学和休学复学实施细则",
    "北京大学研究生入学核查办法",
    "北京大学研究生转专业实施细则",
    "北京大学研究生课程学习与成绩管理办法",
    "北京大学研究生公共必修课学习和考试的规定",
    "北京大学博士研究生培养工作规定",
    "北京大学学术学位硕士研究生培养工作规定",
    "北京大学专业学位硕士研究生培养工作规定",
    "北京大学硕博连读研究生培养工作规定",
    "北京大学博士研究生学科综合考试实施细则",
    "北京大学博士研究生分流实施细则",
    "北京大学与国（境）外单位联合培养研究生实施细则",
    "北京大学研究生基本学术规范及管理办法",
    "北京大学预防与处理学术不端行为办法（试行）",
    "北京大学研究生涉密学位论文保密管理规定",
    "北京大学图书馆涉密学位论文的管理办法",
    "北京大学关于学位论文抽检结果的处理办法",
    "北京大学博士学位论文匿名评阅和导师在答辩中回避评议制度的实施原则",
    "北京大学研究生学业奖学金管理办法",
    "北京大学博士研究生岗位奖学金管理办法（试行）",
    "北京大学博士研究生校长奖学金管理办法",
    "北京大学博士研究生资助体系改革实施办法（试行）",
    "北京大学延长期博士生资助管理办法",
    "北京大学课程助教管理办法（试行）",
    "北京大学研究生学生证及校徽管理和使用的几项规定",
    "北京大学学生奖励评选办法实施细则",
    "北京大学学生奖励评选办法",
    "北京大学奖学金评审办法",
    "北京大学学生违纪处分办法",
    "北京大学学生申诉处理办法",
    "北京大学学生公寓管理办法",
    "北京大学学生社团管理办法",
    "北京大学保密工作规定",
    "北京大学学生就医指南",
]


@dataclass
class RegulationChunk:
    chunk_id: str
    doc_title: str
    audience: Audience
    article: str
    text: str

    def to_dict(self) -> dict:
        return asdict(self)

    def prompt_block(self) -> str:
        return f"【{self.doc_title} · {self.article} · {self.audience}】\n{self.text}"


def infer_audience(doc_title: str) -> Audience:
    t = doc_title
    if "研究生" in t and "本科" not in t:
        return "graduate"
    if "本科" in t or "学士" in t:
        return "undergrad"
    return "general"


def _title_line_pattern(title: str) -> re.Pattern[str]:
    """标题尽量单独成行（允许轻微空白），减少正文引用误匹配。"""
    escaped = re.escape(title)
    # 允许 PDF 抽取产生的空格
    loose = r"\s*".join(map(re.escape, title))
    return re.compile(rf"(?m)^[ \t]*{loose}[ \t]*$")


def _find_doc_spans(full_text: str) -> list[tuple[str, int, int]]:
    # 正文起点：优先学位法，其次学位授予办法 / 研究生学籍办法
    body_start = 0
    for pat in (
        r"(?m)^[ \t]*中华人民共和国学位法",
        r"(?m)^[ \t]*北京大学学位授予工作实施办法",
        r"(?m)^[ \t]*北京大学研究生学籍管理办法",
    ):
        m0 = re.search(pat, full_text)
        if m0:
            body_start = m0.start()
            break

    # 长标题优先，避免短标题抢匹配
    titles_by_len = sorted(KNOWN_DOC_TITLES, key=len, reverse=True)
    hits: list[tuple[str, int]] = []
    occupied: list[tuple[int, int]] = []

    def overlaps(a: int, b: int) -> bool:
        for x, y in occupied:
            if not (b <= x or a >= y):
                return True
        return False

    for title in titles_by_len:
        pat = _title_line_pattern(title)
        found = None
        for m in pat.finditer(full_text):
            if m.start() < body_start:
                continue
            # 跳过仍像目录的行（后面跟大量点号）
            line = full_text[m.start() : full_text.find("\n", m.start())]
            if re.search(r"\.{4,}|…{2,}|\s{2,}\d+\s*$", line):
                continue
            if overlaps(m.start(), m.end()):
                continue
            found = m
            break
        if not found:
            # 退路：正文区第一次出现（非行锚定）
            loose = r"\s*".join(map(re.escape, title))
            for m in re.finditer(loose, full_text):
                if m.start() < body_start:
                    continue
                if overlaps(m.start(), m.end()):
                    continue
                # 避免落在明显引用句中间：要求前方换行或页码
                prev = full_text[max(0, m.start() - 12) : m.start()]
                if "《" in prev:
                    continue
                found = m
                break
        if found:
            hits.append((title, found.start()))
            occupied.append((found.start(), found.end()))

    hits.sort(key=lambda x: x[1])
    result: list[tuple[str, int, int]] = []
    for i, (title, start) in enumerate(hits):
        end = hits[i + 1][1] if i + 1 < len(hits) else len(full_text)
        result.append((title, start, end))
    return result


def split_regulation_chunks(full_text: str) -> list[RegulationChunk]:
    docs = _find_doc_spans(full_text)
    chunks: list[RegulationChunk] = []
    if not docs:
        docs = [("校规汇编", 0, len(full_text))]

    for doc_title, start, end in docs:
        body = full_text[start:end].strip()
        audience = infer_audience(doc_title)
        parts = ARTICLE_SPLIT.split(body)
        article_parts: list[str] = []
        preamble_bits: list[str] = []
        for p in parts:
            p = p.strip()
            if not p:
                continue
            if ARTICLE_HEAD.match(p):
                article_parts.append(p)
            elif not article_parts:
                preamble_bits.append(p)
            else:
                article_parts[-1] = article_parts[-1] + "\n" + p

        for i, art in enumerate(article_parts):
            m = ARTICLE_HEAD.match(art)
            article = m.group(1) if m else f"段{i+1}"
            text = art.strip()
            if len(text) < 15:
                continue
            if len(text) > 6000:
                text = text[:6000]
            chunks.append(
                RegulationChunk(
                    chunk_id=f"{doc_title}#{article}",
                    doc_title=doc_title,
                    audience=audience,
                    article=article,
                    text=text,
                )
            )

        if not article_parts and preamble_bits:
            # 无「第X条」的文件仍保留前言块，但检索侧会降权/过滤
            text = "\n".join(preamble_bits)[:6000]
            chunks.append(
                RegulationChunk(
                    chunk_id=f"{doc_title}#前言",
                    doc_title=doc_title,
                    audience=audience,
                    article="前言",
                    text=text,
                )
            )
    return chunks


def build_or_load_chunks(
    pdf_path: str | Path,
    *,
    cache_path: str | Path | None = None,
    text_cache_path: str | Path | None = None,
    force_rebuild: bool = False,
) -> list[RegulationChunk]:
    pdf_path = Path(pdf_path)
    if cache_path is None:
        cache_path = Path("storage") / f"{pdf_path.stem}_chunks.json"
    else:
        cache_path = Path(cache_path)

    if (
        not force_rebuild
        and cache_path.exists()
        and pdf_path.exists()
        and cache_path.stat().st_mtime >= pdf_path.stat().st_mtime
    ):
        chunks = _load_chunks_file(cache_path)
        print(
            f"[chunker] 已有预处理缓存，跳过重建: {cache_path} "
            f"(chunks={len(chunks)})"
        )
        return chunks

    text = extract_pdf_text(
        pdf_path,
        cache_path=text_cache_path,
        clean=True,
        force=force_rebuild,
    )
    chunks = split_regulation_chunks(text)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps([c.to_dict() for c in chunks], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[chunker] {pdf_path.name}: chunks={len(chunks)} -> {cache_path}")
    return chunks


def _load_chunks_file(cache_path: Path) -> list[RegulationChunk]:
    raw = json.loads(cache_path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        items = raw.get("chunks") or []
    elif isinstance(raw, list):
        items = raw
    else:
        raise ValueError(f"无法解析 chunks 缓存: {cache_path}")
    return [RegulationChunk(**x) for x in items]


def index_is_fresh(exam) -> bool:
    """参考 PDF 的切块缓存是否仍有效（存在、来源一致、且不旧于任一 PDF）。"""
    pdfs = list(getattr(exam, "pdf_paths", None) or [])
    if not pdfs and getattr(exam, "has_refs", False):
        pdf = getattr(exam, "primary_pdf", None)
        pdfs = [pdf] if pdf is not None else []
    if not pdfs:
        return False
    cache = exam.chunks_path
    if not cache.exists() or not all(p.exists() for p in pdfs):
        return False
    newest_pdf = max(p.stat().st_mtime for p in pdfs)
    if cache.stat().st_mtime < newest_pdf:
        return False
    try:
        raw = json.loads(cache.read_text(encoding="utf-8"))
    except Exception:
        return False
    expected = [p.name for p in pdfs]
    if isinstance(raw, dict):
        sources = raw.get("sources")
        if sources is not None and list(sources) != expected:
            return False
        return True
    # 旧版纯 list：仅当恰好一本 PDF 时仍视为有效
    if isinstance(raw, list):
        return len(pdfs) == 1
    return False


def build_or_load_chunks_for_exam(
    exam,
    *,
    force_rebuild: bool = False,
) -> list[RegulationChunk]:
    """索引 exam.refs 中的全部 PDF，合并写入 chunks.json。无参考时返回空列表。"""
    pdfs = list(getattr(exam, "pdf_paths", None) or [])
    if not pdfs:
        pdf = getattr(exam, "primary_pdf", None)
        pdfs = [pdf] if pdf is not None else []
    if not pdfs:
        print(f"[chunker] exam={exam.id} 无参考 PDF，跳过切块")
        return []

    exam.ensure_rag_dir()
    cache_path = exam.chunks_path

    if not force_rebuild and index_is_fresh(exam):
        chunks = _load_chunks_file(cache_path)
        print(
            f"[chunker] 已有预处理缓存，跳过重建: {cache_path} "
            f"(pdfs={len(pdfs)}, chunks={len(chunks)})"
        )
        return chunks

    all_chunks: list[RegulationChunk] = []
    clean_parts: list[str] = []
    for pdf in pdfs:
        text_cache = exam.rag_dir / f"{pdf.stem}.clean.txt"
        text = extract_pdf_text(
            pdf,
            cache_path=text_cache,
            clean=True,
            force=force_rebuild,
        )
        clean_parts.append(f"===== {pdf.name} =====\n{text}")
        pieces = split_regulation_chunks(text)
        # 多 PDF 时用文件名区分 chunk_id，避免同名规章冲突
        if len(pdfs) > 1:
            for c in pieces:
                c.chunk_id = f"{pdf.stem}::{c.chunk_id}"
        all_chunks.extend(pieces)
        print(f"[chunker] {pdf.name}: +{len(pieces)} chunks")

    exam.clean_text_path.write_text("\n\n".join(clean_parts), encoding="utf-8")
    payload = {
        "sources": [p.name for p in pdfs],
        "chunks": [c.to_dict() for c in all_chunks],
    }
    cache_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"[chunker] exam={exam.id}: pdfs={len(pdfs)} chunks={len(all_chunks)} "
        f"-> {cache_path}"
    )
    return all_chunks
