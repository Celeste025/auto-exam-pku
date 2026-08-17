"""可插拔答题策略。

新增策略：实现 AnswerStrategy，并在 STRATEGIES 注册或在 get_strategy 中分支即可。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import ExamQuestion


class AnswerStrategy(ABC):
    name: str = "base"

    @abstractmethod
    def answer(self, question: ExamQuestion) -> list[str]:
        """返回选项 key 列表，例如 ['B'] 或 ['A', 'C']；填空题可返回 ['文本']。"""


class ManualAnswerStrategy(AnswerStrategy):
    """占位：只打印，不选题。"""

    name = "manual"

    def answer(self, question: ExamQuestion) -> list[str]:
        print("[manual] 已读到题目，暂不自动作答：")
        print(question.pretty())
        return []


class NaiveAlwaysAStrategy(AnswerStrategy):
    """流程测试用：凡有选项一律选 A；多选也只选 A；填空填 'A'。"""

    name = "naive_a"

    def answer(self, question: ExamQuestion) -> list[str]:
        qtype = (question.question_type or "").lower()
        if question.options:
            keys = [o.key for o in question.options]
            if "A" in keys:
                chosen = ["A"]
            else:
                chosen = [keys[0]]
            print(f"[naive_a] {qtype or 'unknown'} -> {chosen}")
            return chosen
        if "fill" in qtype or "blank" in qtype:
            print("[naive_a] 填空 -> ['A']")
            return ["A"]
        print("[naive_a] 无选项，跳过")
        return []


class DeepSeekLLMStrategy(AnswerStrategy):
    """DeepSeek LLM（有 refs 走 RAG，无 refs 走 direct）。"""

    name = "llm"

    def __init__(self, session=None) -> None:
        from .deepseek_session import DeepSeekSession

        self.session = session or DeepSeekSession.from_env()

    @classmethod
    def from_env(cls, exam=None) -> DeepSeekLLMStrategy:
        from .deepseek_session import DeepSeekSession

        return cls(DeepSeekSession.from_env(exam=exam))

    def answer(self, question: ExamQuestion) -> list[str]:
        keys = self.session.ask_question(question)
        print(f"[llm] -> {keys}")
        if not keys:
            print("[llm] 未能解析出选项，跳过本题")
        return keys


STRATEGIES: dict[str, type[AnswerStrategy]] = {
    ManualAnswerStrategy.name: ManualAnswerStrategy,
    NaiveAlwaysAStrategy.name: NaiveAlwaysAStrategy,
}


def get_strategy(name: str, *, exam=None) -> AnswerStrategy:
    key = (name or "manual").strip().lower()
    if key == "llm":
        return DeepSeekLLMStrategy.from_env(exam=exam)
    cls = STRATEGIES.get(key)
    if not cls:
        known = ", ".join(sorted([*STRATEGIES, "llm"]))
        raise ValueError(f"未知策略 '{name}'。可选: {known}")
    return cls()
