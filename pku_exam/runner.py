"""自动答题编排：读题 -> 策略作答 -> 点选项 -> 下一题。

默认不交卷；传入 auto_submit=True 时在答完最后一题后点击提交。
"""

from __future__ import annotations

from typing import Any

from .answerer import AnswerStrategy
from .debug_log import DebugRun
from .exam_actor import ExamActor
from .models import ExamQuestion


def run_auto_answer_loop(
    page: Any,
    strategy: AnswerStrategy,
    *,
    max_questions: int | None = None,
    step_delay_ms: int = 400,
    stop_on_error: bool = False,
    auto_submit: bool = False,
    debug: DebugRun | None = None,
) -> list[dict[str, Any]]:
    """自动答题循环。

    max_questions:
      - None / <=0 : 一直做到最后一题
      - >0         : 最多做这么多题
    auto_submit:
      - False（默认）: 答完不点「提交考试」
      - True       : 到达最后一题后自动提交（含确认弹窗）
    """
    actor = ExamActor(page, step_delay_ms=step_delay_ms)
    results: list[dict[str, Any]] = []
    unlimited = max_questions is None or max_questions <= 0
    limit = max_questions if not unlimited else 10_000_000
    i = 0
    reached_end = False

    while i < limit:
        i += 1
        question: ExamQuestion = actor.read_current_question()
        progress = actor.read_progress()
        print("-" * 60)
        cap = "∞" if unlimited else str(max_questions)
        print(f"[auto] ({i}/{cap}) 进度 {progress.get('text')} | 策略={strategy.name}")
        print(question.pretty())

        keys: list[str] = []
        try:
            keys = strategy.answer(question)
            if keys:
                actor.select_keys(keys, question_type=question.question_type)
            else:
                print("[auto] 策略未返回答案，跳过选题")
            row = {
                "seq": i,
                "index": question.index,
                "keys": keys,
                "ok": True,
                "stem": question.stem[:80],
                "type": question.question_type,
            }
            results.append(row)
            if debug is not None:
                debug.log_answer(
                    {
                        "seq": i,
                        "progress": progress,
                        "question": question.to_dict(),
                        "keys": keys,
                        "ok": True,
                        "llm": strategy.get_last_debug(),
                    }
                )
        except Exception as exc:
            print(f"[auto] 选题失败: {exc}")
            row = {
                "index": question.index,
                "keys": [],
                "ok": False,
                "error": str(exc),
                "stem": question.stem[:80],
            }
            results.append(row)
            if debug is not None:
                debug.log_answer(
                    {
                        "seq": i,
                        "progress": progress,
                        "question": question.to_dict(),
                        "keys": [],
                        "ok": False,
                        "error": str(exc),
                        "llm": strategy.get_last_debug(),
                    }
                )
            if "has been closed" in str(exc).lower() or "Target page" in str(exc):
                print("[auto] 浏览器已关闭，中止全卷")
                break
            if stop_on_error:
                break
            continue

        # 最后一题：下一题按钮禁用
        if actor.is_last_question():
            reached_end = True
            print("[auto] 已到最后一题（下一题不可用）")
            break

        moved = actor.click_next()
        if not moved:
            print("[auto] 无法进入下一题，停止")
            break

    if auto_submit:
        if not reached_end:
            print("[auto] 未答到最后一题，跳过自动提交（避免半卷交卷）")
        else:
            try:
                print("[auto] 正在自动提交试卷…")
                actor.submit_exam(confirm=True)
                print("[auto] 已触发提交")
            except Exception as exc:
                print(f"[auto] 自动提交失败: {exc}")
                results.append({"submit": False, "error": str(exc)})
            else:
                results.append({"submit": True})
    else:
        print("[auto] 不会自动交卷（如需交卷请加 --submit）")

    return results
