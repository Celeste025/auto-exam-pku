from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class QuestionOption:
    """单个选项。"""

    key: str
    text: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class ExamQuestion:
    """从答题页解析出的一道题。"""

    index: int | None = None
    total: int | None = None
    question_type: str | None = None
    stem: str = ""
    options: list[QuestionOption] = field(default_factory=list)
    raw_text: str = ""
    source: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    def pretty(self) -> str:
        lines: list[str] = []
        header_parts: list[str] = []
        if self.index is not None:
            if self.total is not None:
                header_parts.append(f"第 {self.index}/{self.total} 题")
            else:
                header_parts.append(f"第 {self.index} 题")
        if self.question_type:
            header_parts.append(f"[{self.question_type}]")
        if header_parts:
            lines.append(" ".join(header_parts))
        if self.stem:
            lines.append(self.stem)
        for opt in self.options:
            lines.append(f"  {opt.key}. {opt.text}")
        return "\n".join(lines).strip()
