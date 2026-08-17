"""答题调试日志：开启后写入 debug/<exam>_<时间戳>/。"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEBUG_ROOT = REPO_ROOT / "debug"


def debug_enabled(*, cli_flag: bool | None = None) -> bool:
    """CLI --debug 或环境变量 DEBUG / EXAM_DEBUG。"""
    if cli_flag is True:
        return True
    raw = (os.getenv("DEBUG") or os.getenv("EXAM_DEBUG") or "").strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


class _TeeStream:
    def __init__(self, primary: TextIO, secondary: TextIO) -> None:
        self.primary = primary
        self.secondary = secondary

    def write(self, data: str) -> int:
        self.primary.write(data)
        self.secondary.write(data)
        self.secondary.flush()
        return len(data)

    def flush(self) -> None:
        self.primary.flush()
        self.secondary.flush()

    def isatty(self) -> bool:
        return bool(getattr(self.primary, "isatty", lambda: False)())

    def fileno(self) -> int:
        return self.primary.fileno()


class DebugRun:
    """一次自动答题的调试目录。

    产出::
        debug/<exam>_<ts>/
          run.json
          answers.jsonl
          console.log
    """

    def __init__(
        self,
        *,
        exam_id: str = "exam",
        strategy: str = "",
        model: str = "",
        root: Path | None = None,
    ) -> None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_id = (exam_id or "exam").replace("/", "_").replace("\\", "_")
        self.dir = (root or DEFAULT_DEBUG_ROOT) / f"{safe_id}_{ts}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.dir / "answers.jsonl"
        self.run_path = self.dir / "run.json"
        self.console_path = self.dir / "console.log"
        self.meta: dict[str, Any] = {
            "exam_id": exam_id,
            "strategy": strategy,
            "model": model,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "dir": str(self.dir),
        }
        self._records: list[dict[str, Any]] = []
        self._console_fp: TextIO | None = None
        self._orig_stdout = sys.stdout
        self._orig_stderr = sys.stderr
        self._teeing = False

    def start(self) -> None:
        self._console_fp = self.console_path.open("w", encoding="utf-8")
        sys.stdout = _TeeStream(self._orig_stdout, self._console_fp)  # type: ignore[assignment]
        sys.stderr = _TeeStream(self._orig_stderr, self._console_fp)  # type: ignore[assignment]
        self._teeing = True
        print(f"[debug] 日志目录: {self.dir}")
        self._write_run()

    def log_answer(self, record: dict[str, Any]) -> None:
        self._records.append(record)
        with self.jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def finish(self, *, summary: dict[str, Any] | None = None) -> Path:
        self.meta["finished_at"] = datetime.now().isoformat(timespec="seconds")
        self.meta["question_count"] = sum(
            1 for r in self._records if "question" in r or "index" in r
        )
        if summary:
            self.meta["summary"] = summary
        self._write_run()
        self.stop()
        print(f"[debug] 已保存: {self.dir}")
        return self.dir

    def stop(self) -> None:
        if not self._teeing:
            return
        sys.stdout = self._orig_stdout
        sys.stderr = self._orig_stderr
        self._teeing = False
        if self._console_fp is not None:
            self._console_fp.close()
            self._console_fp = None

    def _write_run(self) -> None:
        payload = {**self.meta, "answers_file": self.jsonl_path.name}
        self.run_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
