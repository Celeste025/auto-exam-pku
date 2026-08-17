"""从 PDF 抽取纯文本，并对研究生手册类 PDF 做去噪简化。"""

from __future__ import annotations

import re
from pathlib import Path


_NOISE_EXACT = re.compile(
    r"^(?:"
    r"POSTGRADUATE\s*MANUAL(\s+OF\s+PEKING\s+UNIVERSITY)?|"
    r"OF\s+PEKING\s+UNIVERSITY|"
    r"\d{1,3}|"
    r"0{1,3}\d{0,3}|"
    r"目\s*录|"
    r"目录|"
    r"学\s*籍\s*与\s*教\s*务\s*管\s*理|"
    r"培\s*养\s*与\s*学\s*位\s*授\s*予|"
    r"奖\s*助\s*工\s*作|"
    r"其他相关管理规定|"
    r"附录.*"
    r")$",
    re.I,
)


def clean_handbook_text(text: str) -> str:
    """去掉页眉页脚、空页残渣、目录装饰，并尽量从正式规章正文起截取。"""
    lines_out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _NOISE_EXACT.match(line):
            continue
        if "POSTGRADUATE MANUAL" in line.upper():
            continue
        # 目录点线页码行
        if re.search(r"\.{4,}|\u2026{2,}", line) and re.search(r"\d+\s*$", line):
            continue
        # 过短且无汉字的行（多为版式碎片）
        if len(line) <= 2 and not re.search(r"[\u4e00-\u9fff]", line):
            continue
        lines_out.append(line)

    joined = "\n".join(lines_out)

    # 从「学位授予工作实施办法」正文标题起保留（跳过讲话/简介）
    m = re.search(r"(?m)^北京大学学位授予工作实施办法\s*$", joined)
    if m and m.start() > 500:
        joined = joined[m.start() :]
    return joined.strip()


def extract_pdf_text(
    pdf_path: str | Path,
    *,
    cache_path: str | Path | None = None,
    clean: bool = True,
    force: bool = False,
) -> str:
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"找不到参考 PDF: {pdf_path}")

    if cache_path is None:
        suffix = "_clean.txt" if clean else ".txt"
        cache_path = pdf_path.parent / "storage" / f"{pdf_path.stem}{suffix}"
    else:
        cache_path = Path(cache_path)

    if (
        not force
        and cache_path.exists()
        and cache_path.stat().st_mtime >= pdf_path.stat().st_mtime
    ):
        text = cache_path.read_text(encoding="utf-8")
        if len(text.strip()) > 100:
            return text

    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise SystemExit("请先安装: pip install pypdf") from exc

    reader = PdfReader(str(pdf_path))
    parts: list[str] = []
    skipped = 0
    for page in reader.pages:
        t = (page.extract_text() or "").strip()
        # 跳过几乎无字的页面（多为图片页）
        if len(re.sub(r"\s+", "", t)) < 30:
            skipped += 1
            continue
        parts.append(t)
    text = "\n".join(parts).strip()
    if clean:
        text = clean_handbook_text(text)
    if len(text) < 100:
        raise RuntimeError(f"PDF 文本提取结果过短，请检查文件: {pdf_path}")

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(text, encoding="utf-8")
    print(
        f"[pdf] {pdf_path.name}: pages={len(reader.pages)} skipped_imageish={skipped} "
        f"chars={len(text)} -> {cache_path}"
    )
    return text
