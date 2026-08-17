from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .exam_profile import ExamProfile, load_exam_profile


@dataclass(frozen=True)
class Settings:
    username: str
    password: str
    exam_url: str
    storage_state_path: Path
    headless: bool
    exam_id: str = ""
    exam: ExamProfile | None = None


def load_settings(
    env_file: str | Path | None = ".env",
    *,
    require_credentials: bool = True,
    exam_id: str | None = None,
) -> Settings:
    """从环境变量 / .env + exams 配置加载。密码不会写进代码。"""
    if env_file:
        load_dotenv(env_file)

    username = os.getenv("PKU_USERNAME", "").strip()
    password = os.getenv("PKU_PASSWORD", "").strip()
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
    resolved_id = (exam_id or os.getenv("EXAM_ID", "")).strip()

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
        raise ValueError("缺少考试 URL：请配置 exams/<id>/exam.json 或 EXAM_URL")

    if require_credentials and (not username or not password):
        raise ValueError(
            "缺少 PKU_USERNAME / PKU_PASSWORD。\n"
            "请先复制 .env.example 为 .env，再填入测试账号（勿把 .env 提交到 git）。\n"
            "若已用 --manual-login 保存会话，可执行 --scrape（无需密码）。"
        )

    return Settings(
        username=username,
        password=password,
        exam_url=exam_url,
        storage_state_path=storage_state_path,
        headless=headless,
        exam_id=resolved_id or (profile.id if profile else ""),
        exam=profile,
    )
