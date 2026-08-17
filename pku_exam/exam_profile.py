"""多场次考试配置：自动扫描 exams/<id>/exam.json。

目录约定::

    exams/
      exam54/
        exam.json           # url / refs 等
        refs/*.pdf          # 参考资料（可空）
        rag/                # --build-index 生成
          clean.txt
          chunks.json

场次 id = 目录名。默认场次由 --exam / EXAM_ID 指定；
若都未指定且只发现一个场次，则自动选用该场次。

`refs` 可为路径、路径列表或空列表；文件夹会收集其下全部 .pdf。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXAMS_DIR = REPO_ROOT / "exams"


@dataclass(frozen=True)
class ExamProfile:
    """一场考试的解析后配置（路径均为绝对路径）。"""

    id: str
    name: str
    url: str
    root_dir: Path
    ref_paths: tuple[Path, ...]
    rag_dir: Path = field(default_factory=Path)
    description: str = ""

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
            "name": self.name,
            "url": self.url,
            "root_dir": str(self.root_dir),
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


def discover_exam_dirs(exams_dir: str | Path | None = None) -> dict[str, Path]:
    """扫描 exams/*/exam.json，返回 {id: exam.json 路径}。"""
    root = exams_root(exams_dir)
    if not root.is_dir():
        raise FileNotFoundError(f"找不到 exams 目录: {root}")
    found: dict[str, Path] = {}
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        exam_json = child / "exam.json"
        if exam_json.is_file():
            found[child.name] = exam_json
    return found


def list_exam_ids(exams_dir: str | Path | None = None) -> list[str]:
    return sorted(discover_exam_dirs(exams_dir).keys())


def resolve_exam_id(
    exam_id: str | None = None,
    *,
    exams_dir: str | Path | None = None,
) -> str:
    known = discover_exam_dirs(exams_dir)
    if not known:
        raise ValueError(
            f"在 {exams_root(exams_dir)} 下未发现任何 exams/<id>/exam.json。\n"
            "请新建场次目录并放入 exam.json。"
        )
    chosen = (exam_id or os.getenv("EXAM_ID", "")).strip()
    if not chosen:
        if len(known) == 1:
            return next(iter(known))
        known_s = ", ".join(known.keys())
        raise ValueError(
            f"未指定 exam id：请传 --exam 或设置 EXAM_ID。可选: {known_s}"
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
    eid = resolve_exam_id(exam_id, exams_dir=exams_dir)
    exam_json = discover_exam_dirs(exams_dir)[eid]
    data = json.loads(exam_json.read_text(encoding="utf-8"))
    return _profile_from_dict(data, root_dir=exam_json.parent, exam_json=exam_json)


def _resolve_path(base: Path, raw: str | Path) -> Path:
    p = Path(raw)
    if not p.is_absolute():
        p = (base / p).resolve()
    return p


def _expand_refs(root_dir: Path, refs_raw: Any) -> list[Path]:
    """解析 refs。空列表 / 显式空 = 无参考资料（走模型直答）。"""
    if refs_raw is None:
        default_dir = root_dir / "refs"
        if default_dir.is_dir():
            pdfs = sorted(default_dir.rglob("*.pdf"))
            return [p.resolve() for p in pdfs]
        return []
    if isinstance(refs_raw, (str, Path)):
        refs_list: list[Any] = [refs_raw]
    elif isinstance(refs_raw, list):
        refs_list = list(refs_raw)
    else:
        raise ValueError(f"exam.json 的 refs 类型无效: {type(refs_raw)}")

    if not refs_list:
        return []

    out: list[Path] = []
    for item in refs_list:
        if item is None or str(item).strip() == "":
            continue
        path = _resolve_path(root_dir, item)
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
            raise FileNotFoundError(f"参考路径不存在: {path}")
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
    root_dir: Path,
    exam_json: Path | None,
) -> ExamProfile:
    eid = str(data.get("id") or root_dir.name).strip()
    name = str(data.get("name") or eid).strip()
    url = str(data.get("url") or data.get("exam_url") or "").strip()
    if not url:
        raise ValueError(f"exam '{eid}' 缺少 url 字段（见 {exam_json or root_dir}）")

    rag_raw = data.get("rag_dir") or "rag"
    rag_dir = _resolve_path(root_dir, rag_raw)
    refs = tuple(_expand_refs(root_dir, data.get("refs")))

    return ExamProfile(
        id=eid,
        name=name,
        url=url,
        root_dir=root_dir.resolve(),
        ref_paths=tuple(refs),
        rag_dir=rag_dir,
        description=str(data.get("description") or ""),
    )
