"""多场次考试配置：自动扫描 exams/<id>.json。

目录约定::

    exams/
      exam54.json           # 仅需 url / refs
      exam57.json
      refs/                 # 共享参考 PDF（用户放入）
        2026eg.pdf
      exam54/               # 自动生成：RAG 缓存（勿手改）
        clean.txt
        chunks.json

场次 id = JSON 文件名（不含扩展名）。默认场次由 --exam 指定；
若都未指定且只发现一个场次，则自动选用该场次。

`refs` 为 exams/refs/ 下的文件名（或相对该目录的路径）；空列表表示无参考（direct）。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXAMS_DIR = REPO_ROOT / "exams"
RESERVED_DIR_NAMES = frozenset({"refs"})


@dataclass(frozen=True)
class ExamProfile:
    """一场考试的解析后配置（路径均为绝对路径）。"""

    id: str
    url: str
    exams_dir: Path
    config_path: Path
    ref_paths: tuple[Path, ...]
    rag_dir: Path = field(default_factory=Path)

    @property
    def refs_dir(self) -> Path:
        return self.exams_dir / "refs"

    @property
    def clean_text_path(self) -> Path:
        return self.rag_dir / "clean.txt"

    @property
    def chunks_path(self) -> Path:
        return self.rag_dir / "chunks.json"

    @property
    def primary_pdf(self) -> Path | None:
        """主参考 PDF；无参考资料时返回 None。"""
        pdfs = [p for p in self.ref_paths if p.suffix.lower() == ".pdf"]
        return pdfs[0] if pdfs else None

    @property
    def has_refs(self) -> bool:
        """是否存在可用于 RAG 的 PDF（非 PDF 文件不算）。"""
        return self.primary_pdf is not None

    def ensure_rag_dir(self) -> None:
        self.rag_dir.mkdir(parents=True, exist_ok=True)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "config": str(self.config_path),
            "refs": [str(p) for p in self.ref_paths],
            "has_refs": self.has_refs,
            "rag_dir": str(self.rag_dir),
            "clean_text": str(self.clean_text_path),
            "chunks": str(self.chunks_path),
        }


def exams_root(exams_dir: str | Path | None = None) -> Path:
    raw = exams_dir or os.getenv("EXAMS_DIR", str(DEFAULT_EXAMS_DIR))
    path = Path(raw)
    if not path.is_absolute():
        path = (REPO_ROOT / path).resolve()
    return path


def discover_exam_configs(exams_dir: str | Path | None = None) -> dict[str, Path]:
    """扫描 exams/*.json，返回 {id: 配置文件路径}。"""
    root = exams_root(exams_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"找不到 exams 目录: {root}")
    found: dict[str, Path] = {}
    for child in sorted(root.glob("*.json")):
        if not child.is_file():
            continue
        eid = child.stem
        if eid in RESERVED_DIR_NAMES:
            continue
        found[eid] = child
    return found


# 兼容旧名
discover_exam_dirs = discover_exam_configs


def list_exam_ids(exams_dir: str | Path | None = None) -> list[str]:
    return sorted(discover_exam_configs(exams_dir).keys())


def resolve_exam_id(
    exam_id: str | None = None,
    *,
    exams_dir: str | Path | None = None,
) -> str:
    known = discover_exam_configs(exams_dir)
    if not known:
        raise ValueError(
            f"在 {exams_root(exams_dir)} 下未发现任何 exams/<id>.json。\n"
            "请新建 exams/<id>.json，并把参考 PDF 放到 exams/refs/。"
        )
    chosen = (exam_id or "").strip()
    if not chosen:
        if len(known) == 1:
            return next(iter(known))
        known_s = ", ".join(known.keys())
        raise ValueError(
            f"未指定 exam id：请传 --exam。可选: {known_s}"
        )
    if chosen not in known:
        known_s = ", ".join(known.keys())
        raise ValueError(f"未知 exam '{chosen}'。可选: {known_s}")
    return chosen


def load_exam_profile(
    exam_id: str | None = None,
    *,
    exams_dir: str | Path | None = None,
) -> ExamProfile:
    root = exams_root(exams_dir)
    eid = resolve_exam_id(exam_id, exams_dir=exams_dir)
    config_path = discover_exam_configs(exams_dir)[eid]
    data = json.loads(config_path.read_text(encoding="utf-8-sig"))
    return _profile_from_dict(data, exam_id=eid, exams_dir=root, config_path=config_path)


def _resolve_path(base: Path, raw: str | Path) -> Path:
    p = Path(raw)
    if not p.is_absolute():
        p = (base / p).resolve()
    return p


def _expand_refs(refs_dir: Path, refs_raw: Any) -> list[Path]:
    """解析 refs：相对路径均相对于 exams/refs/。"""
    if refs_raw is None:
        # 未写 refs：不自动扫全目录，避免误绑；显式 [] 或省略都视为无参考
        return []
    if isinstance(refs_raw, (str, Path)):
        refs_list: list[Any] = [refs_raw]
    elif isinstance(refs_raw, list):
        refs_list = list(refs_raw)
    else:
        raise ValueError(f"exam 配置的 refs 类型无效: {type(refs_raw)}")

    if not refs_list:
        return []

    refs_dir.mkdir(parents=True, exist_ok=True)

    out: list[Path] = []
    for item in refs_list:
        if item is None or str(item).strip() == "":
            continue
        raw = str(item).strip().replace("\\", "/")
        # 兼容旧写法 "refs/xxx.pdf"
        if raw.lower().startswith("refs/"):
            raw = raw[5:]
        path = _resolve_path(refs_dir, raw)
        if path.is_dir():
            pdfs = sorted(path.rglob("*.pdf"))
            if not pdfs:
                print(f"[exam] 参考目录为空，跳过: {path}")
                continue
            out.extend(pdfs)
        elif path.is_file():
            if path.suffix.lower() != ".pdf":
                print(f"[exam] 跳过非 PDF 参考文件: {path}")
                continue
            out.append(path)
        else:
            raise FileNotFoundError(
                f"参考文件不存在: {path}\n请将 PDF 放到 {refs_dir} 并在 json 的 refs 中写文件名。"
            )
    seen: set[Path] = set()
    uniq: list[Path] = []
    for p in out:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            uniq.append(rp)
    return uniq


def _profile_from_dict(
    data: dict[str, Any],
    *,
    exam_id: str,
    exams_dir: Path,
    config_path: Path | None,
) -> ExamProfile:
    eid = exam_id.strip()
    url = str(data.get("url") or data.get("exam_url") or "").strip()
    if not url:
        raise ValueError(f"exam '{eid}' 缺少 url 字段（见 {config_path or eid}）")

    exams_dir = exams_dir.resolve()
    rag_dir = (exams_dir / eid).resolve()
    refs = tuple(_expand_refs(exams_dir / "refs", data.get("refs")))

    return ExamProfile(
        id=eid,
        url=url,
        exams_dir=exams_dir,
        config_path=(config_path or (exams_dir / f"{eid}.json")).resolve(),
        ref_paths=tuple(refs),
        rag_dir=rag_dir,
    )
