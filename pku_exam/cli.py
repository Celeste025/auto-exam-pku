"""登录 / 抓题 / 自动答题 CLI（支持多场次 --exam）。

示例：
  python -m pku_exam.cli --list-exams
  python -m pku_exam.cli --manual-login --exam exam54
  python -m pku_exam.cli --auto-answer --exam exam54 --strategy llm
  python -m pku_exam.cli --auto-answer --exam exam54 --strategy llm --submit
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .answerer import ManualAnswerStrategy, get_strategy
from .auth import ExamAuthError, LoginSession, manual_login_and_save
from .config import Settings, load_settings
from .exam_profile import list_exam_ids, load_exam_profile
from .page_loader import ExamPageLoader
from .question_reader import QuestionReader
from .runner import run_auto_answer_loop


def _parse_cookie_header(raw: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookies[name.strip()] = value.strip()
    return cookies


def _print_question(question) -> None:
    print("=" * 60)
    print(question.pretty() or "(未解析到有效题干)")
    print("=" * 60)
    print(json.dumps(question.to_dict(), ensure_ascii=False, indent=2))


def _load_session_settings(*, exam_id: str | None = None) -> Settings:
    load_dotenv(".env")
    return load_settings(exam_id=exam_id)


def _scrape_with_session(*, exam_id: str | None = None) -> int:
    try:
        settings = _load_session_settings(exam_id=exam_id)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not settings.storage_state_path.exists():
        print(
            f"未找到会话文件 {settings.storage_state_path}。"
            "请先：python -m pku_exam.cli --manual-login",
            file=sys.stderr,
        )
        return 1

    if settings.exam:
        print(f"[exam] {settings.exam.id}")
        print(f"[exam] url={settings.exam_url}")

    loader = ExamPageLoader()
    try:
        with LoginSession(settings, reuse_storage=True) as session:
            page = session.page
            print(f"已进入: {page.url}")
            print("等待 Vue 页面渲染题目 / 拦截 API…")
            question = loader.load_question(page)
            session.save_storage()
            print(f"会话已写入 -> {settings.storage_state_path}")
    except ExamAuthError as exc:
        print(f"认证失败: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"抓取失败: {exc}", file=sys.stderr)
        return 1

    _print_question(question)
    ManualAnswerStrategy().answer(question)
    return 0


def _run_auto_answer(
    *,
    strategy_name: str,
    max_questions: int | None,
    auto_submit: bool = False,
    exam_id: str | None = None,
) -> int:
    load_dotenv(".env")
    try:
        settings = _load_session_settings(exam_id=exam_id)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not settings.storage_state_path.exists():
        print("请先 --manual-login", file=sys.stderr)
        return 1

    settings = Settings(
        exam_url=settings.exam_url,
        storage_state_path=settings.storage_state_path,
        headless=False,
        exam_id=settings.exam_id,
        exam=settings.exam,
    )

    try:
        strategy = get_strategy(strategy_name, exam=settings.exam)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # RAG 场次：未预处理则提示操作方式并中止（避免静默边答边建索引）
    if strategy_name.strip().lower() == "llm" and settings.exam and settings.exam.has_refs:
        from .chunker import index_is_fresh

        if not index_is_fresh(settings.exam):
            eid = settings.exam.id
            print(
                "[提示] 本场次参考 PDF 尚未预处理，或 PDF 已更新导致缓存过期。\n"
                "请先执行：\n"
                f"  python -m pku_exam.cli --exam {eid} --build-index\n"
                "强制重建：\n"
                f"  python -m pku_exam.cli --exam {eid} --build-index --force-rebuild\n"
                "完成后再运行 --auto-answer。",
                file=sys.stderr,
            )
            return 1

    limit_desc = "全部题目" if max_questions is None or max_questions <= 0 else str(max_questions)
    submit_note = (
        "答完后将自动点击「提交考试」。"
        if auto_submit
        else "默认不交卷；需要交卷请加 --submit。"
    )
    exam_label = settings.exam_id or "(legacy)"
    print(
        f"自动答题：exam={exam_label}, strategy={strategy.name}, "
        f"max_questions={limit_desc}, auto_submit={auto_submit}\n"
        f"url={settings.exam_url}\n"
        f"{submit_note} 请关闭其他作答标签页。"
    )
    if settings.exam:
        print(f"refs={list(settings.exam.ref_paths)}")
        print(f"rag={settings.exam.rag_dir}")

    try:
        with LoginSession(settings, reuse_storage=True) as session:
            page = session.page
            print(f"已进入: {page.url}")
            page.wait_for_selector(".question-card, .load-error", timeout=60_000)
            if page.locator(".load-error").count() > 0:
                err = page.locator(".load-error").inner_text()
                raise RuntimeError(f"无法进入考试：{err}")

            results = run_auto_answer_loop(
                page,
                strategy,
                max_questions=max_questions,
                auto_submit=auto_submit,
            )
            print("=" * 60)
            print(json.dumps(results, ensure_ascii=False, indent=2))
            submitted = any(isinstance(r, dict) and r.get("submit") is True for r in results)
            n_q = sum(1 for r in results if isinstance(r, dict) and "index" in r)
            print(
                f"自动答题结束：共 {n_q} 题"
                + ("（已提交）。" if submitted else "（未交卷）。")
            )
            if sys.stdin.isatty():
                print("按回车关闭浏览器…")
                try:
                    input()
                except EOFError:
                    pass
            else:
                page.wait_for_timeout(1500)
            session.save_storage()
            session.browser.close()
            session.browser = None
            from .auth import strip_exam_tab_lock_from_storage_file

            n = strip_exam_tab_lock_from_storage_file(settings.storage_state_path)
            if n:
                print(f"已清理会话文件中的 {n} 个作答锁")
    except ExamAuthError as exc:
        print(f"认证失败: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"自动答题失败: {exc}", file=sys.stderr)
        return 1
    return 0


def _run_manual_login(*, exam_id: str | None = None) -> int:
    load_dotenv(".env")
    try:
        settings = load_settings(exam_id=exam_id)
    except ValueError as exc:
        # 无 exams 配置时仍允许手工登录
        exam_url = os.getenv("EXAM_URL", "https://exam.pku.edu.cn/examinee/exam/54/").strip()
        storage = Path(os.getenv("STORAGE_STATE_PATH", "storage/pku_exam_state.json"))
        settings = Settings(
            exam_url=exam_url,
            storage_state_path=storage,
            headless=False,
            exam_id=exam_id or "",
            exam=None,
        )
        print(f"[warn] exam 配置加载失败，使用 EXAM_URL={exam_url}: {exc}")

    settings = Settings(
        exam_url=settings.exam_url,
        storage_state_path=settings.storage_state_path,
        headless=False,
        exam_id=settings.exam_id,
        exam=settings.exam,
    )
    if settings.exam:
        print(f"[exam] {settings.exam.id}")
    try:
        path = manual_login_and_save(settings)
    except ExamAuthError as exc:
        print(f"失败: {exc}", file=sys.stderr)
        return 1
    print(f"登录会话已保存: {path}")
    print("之后可执行: python -m pku_exam.cli --scrape --exam", settings.exam_id or "exam54")
    print("或: python -m pku_exam.cli --auto-answer --exam", settings.exam_id or "exam54", "--strategy llm")
    return 0


def _list_exams() -> int:
    try:
        ids = list_exam_ids()
    except Exception as exc:
        print(f"无法列出 exams: {exc}", file=sys.stderr)
        return 1
    if not ids:
        print("(exams/ 下暂无场次)")
        return 0
    for eid in ids:
        try:
            p = load_exam_profile(eid)
            print(f"- {p.id}")
            print(f"  url: {p.url}")
            refs_disp = []
            for x in p.ref_paths:
                try:
                    refs_disp.append(str(x.relative_to(p.refs_dir)))
                except ValueError:
                    refs_disp.append(str(x))
            print(f"  refs: {refs_disp}")
            print(f"  rag: {p.rag_dir}")
        except Exception as exc:
            print(f"- {eid}: (加载失败: {exc})")
    return 0


def _build_index(*, exam_id: str | None, force: bool = False) -> int:
    """显式预处理参考 PDF → exams/<id>/{clean.txt,chunks.json}。

    默认：已有且不旧于 PDF 的缓存则跳过；--force-rebuild 才强制重建。
    """
    from .chunker import build_or_load_chunks_for_exam, index_is_fresh

    try:
        profile = load_exam_profile(exam_id)
    except Exception as exc:
        print(f"加载 exam 失败: {exc}", file=sys.stderr)
        return 1

    print(f"[index] exam={profile.id}")
    if not profile.has_refs:
        print("[index] 该场次 refs 为空，无需预处理（答题将走 direct 模式）")
        return 0

    print(f"[index] refs={list(profile.ref_paths)}")
    if not force and index_is_fresh(profile):
        print(f"[index] 缓存已是最新，跳过: {profile.chunks_path}")
        print("        若需强制重建请加 --force-rebuild")
        return 0

    chunks = build_or_load_chunks_for_exam(profile, force_rebuild=force)
    print(f"[index] 完成: chunks={len(chunks)}")
    print(f"  clean -> {profile.clean_text_path}")
    print(f"  chunks -> {profile.chunks_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="北大考试系统：登录 / 抓题 / 自动答题")
    parser.add_argument(
        "--exam",
        default=None,
        help="场次 id（扫描 exams/<id>.json）",
    )
    parser.add_argument("--list-exams", action="store_true", help="列出可用场次")
    parser.add_argument(
        "--build-index",
        action="store_true",
        help="预处理本场次 refs PDF → exams/<id>/（已有有效缓存则跳过；默认不执行）",
    )
    parser.add_argument(
        "--force-rebuild",
        action="store_true",
        help="与 --build-index 联用：忽略已有缓存，强制重新抽取与切块",
    )
    parser.add_argument("--manual-login", action="store_true", help="人工登录并保存会话")
    parser.add_argument("--scrape", action="store_true", help="复用会话抓当前题")
    parser.add_argument(
        "--auto-answer",
        action="store_true",
        help="自动选题并点下一题（默认不交卷）",
    )
    parser.add_argument(
        "--strategy",
        default="llm",
        help="答题策略：llm / naive_a / manual（默认 llm）",
    )
    parser.add_argument(
        "--max-questions",
        type=int,
        default=0,
        help="最多做几题；默认 0 表示做到最后一题为止。例如 --max-questions 3",
    )
    parser.add_argument(
        "--submit",
        action="store_true",
        help="答完后自动点击「提交考试」并确认弹窗（默认不交卷）",
    )
    parser.add_argument("--html", type=Path)
    parser.add_argument("--url", type=str)
    parser.add_argument("--cookie", type=str, default="")
    args = parser.parse_args(argv)

    exam_id = args.exam

    if args.list_exams:
        return _list_exams()
    if args.build_index:
        return _build_index(exam_id=exam_id, force=bool(args.force_rebuild))
    if args.force_rebuild and not args.build_index:
        print("提示: --force-rebuild 需与 --build-index 一起使用", file=sys.stderr)
        return 2
    if args.manual_login:
        return _run_manual_login(exam_id=exam_id)
    if args.scrape:
        return _scrape_with_session(exam_id=exam_id)
    if args.auto_answer:
        mq = None if args.max_questions <= 0 else args.max_questions
        env_submit = os.getenv("AUTO_SUBMIT", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "y",
        }
        return _run_auto_answer(
            strategy_name=args.strategy,
            max_questions=mq,
            auto_submit=bool(args.submit or env_submit),
            exam_id=exam_id,
        )

    if args.html:
        if not args.html.exists():
            print(f"文件不存在: {args.html}", file=sys.stderr)
            return 1
        question = QuestionReader().from_file(args.html)
        _print_question(question)
        return 0

    if args.url:
        cookies = _parse_cookie_header(args.cookie) if args.cookie else None
        try:
            question = QuestionReader().from_url(args.url, cookies=cookies)
        except PermissionError as exc:
            print(f"失败: {exc}", file=sys.stderr)
            return 1
        _print_question(question)
        return 0

    parser.print_help()
    print(
        "\n推荐：\n"
        "  python -m pku_exam.cli --list-exams\n"
        "  python -m pku_exam.cli --exam exam54 --build-index\n"
        "  python -m pku_exam.cli --manual-login --exam exam54\n"
        "  python -m pku_exam.cli --auto-answer --exam exam54 --strategy llm\n",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
