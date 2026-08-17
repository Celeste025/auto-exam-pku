"""北京大学在线考试：登录 / 抓题 / 多场次自动答题。

推荐入口::

    python -m pku_exam.cli --list-exams
    python -m pku_exam.cli --auto-answer --exam exam54 --strategy llm
"""

from .answerer import (
    AnswerStrategy,
    DeepSeekLLMStrategy,
    ManualAnswerStrategy,
    NaiveAlwaysAStrategy,
    STRATEGIES,
    get_strategy,
)
from .exam_profile import ExamProfile, load_exam_profile, list_exam_ids
from .models import ExamQuestion, QuestionOption
from .question_reader import QuestionReader

__all__ = [
    "AnswerStrategy",
    "DeepSeekLLMStrategy",
    "ExamProfile",
    "ExamQuestion",
    "ManualAnswerStrategy",
    "NaiveAlwaysAStrategy",
    "QuestionOption",
    "QuestionReader",
    "STRATEGIES",
    "get_strategy",
    "list_exam_ids",
    "load_exam_profile",
]
