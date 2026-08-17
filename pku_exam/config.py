from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .exam_profile import ExamProfile, load_exam_profile


@dataclass(frozen=True)
class Settings:
    exam_url: str
    storage_state_path: Path
    headless: bool
    exam_id: str = ""
    exam: ExamProfile | None = None


def load_settings(
    env_file: str | Path | None = ".env",
    *,
    exam_id: str | None = None,
) -> Settings:
    """从环境变量 / .env + exams 配置加载。登录靠 --manual-login 会话，不读账号密码。"""
    if env_file:
        load_dotenv(env_file)

    storage_state_path = Path(
        os.getenv("STORAGE_STATE_PATH", "storage/pku_exam_state.json")
    )
    headless = os.getenv("HEADLESS", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }

    url_override = os.getenv("EXAM_URL", "").strip()
    resolved_id = (exam_id or "").strip()

    profile: ExamProfile | None = None
    exam_url = url_override
    try:
        profile = load_exam_profile(resolved_id or None)
        resolved_id = profile.id
        if not exam_url:
            exam_url = profile.url
    except FileNotFoundError as exc:
        if not exam_url:
            raise ValueError(
                f"{exc}\n也可临时设置 EXAM_URL 使用旧模式。"
            ) from exc
        print(f"[warn] 未加载 exam 目录配置，使用 EXAM_URL={exam_url}")
    except ValueError:
        if not exam_url:
            raise

    if not exam_url:
        raise ValueError("缺少考试 URL：请配置 exams/<id>.json 或 EXAM_URL")

    return Settings(
        exam_url=exam_url,
        storage_state_path=storage_state_path,
        headless=headless,
        exam_id=resolved_id or (profile.id if profile else ""),
        exam=profile,
    )
