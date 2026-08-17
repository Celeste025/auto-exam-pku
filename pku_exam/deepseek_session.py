"""DeepSeek 答题会话：默认 RAG（结构条款检索）+ 多选逐项判定。

也保留 full 模式（整本 PDF 前缀 + Context Cache）以备对照。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from .models import ExamQuestion
from .retriever import RegulationRetriever

SYSTEM_PROMPT_RAG = """你是北京大学校规校纪考试助手。
你必须严格依据「检索到的参考条文」作答，不要编造条文中不存在的内容。
作答要求：
1. 单选题 / 判断题：只选一个选项字母。
2. 多选题：必须对每个选项逐项判断对错，再汇总所有正确项（若全部正确可以全选）。
3. 填空题：尽量使用条文原词；多空用英文分号 ; 分隔。
4. 只输出 JSON。
单选/判断/填空：{"keys":["A"],"brief":"依据：文件名+条款"}
多选：{"options":{"A":{"ok":true,"why":"..."},"B":{"ok":false,"why":"..."}},"brief":"总结"}
"""

SYSTEM_PROMPT_DIRECT = """你是北京大学在线考试答题助手。
当前场次未提供校规章参考材料，请依据你掌握的知识谨慎作答。
作答要求：
1. 单选题 / 判断题：只选一个选项字母。
2. 多选题：必须对每个选项逐项判断对错，再汇总所有正确项（若全部正确可以全选）。
3. 填空题：给出最可能的填空内容；多空用英文分号 ; 分隔。
4. 只输出 JSON。
单选/判断/填空：{"keys":["A"],"brief":"简要理由"}
多选：{"options":{"A":{"ok":true,"why":"..."},"B":{"ok":false,"why":"..."}},"brief":"总结"}
"""

SYSTEM_PROMPT_FULL = """你是北京大学校规校纪考试助手。
你必须严格依据用户提供的参考材料作答，不要编造材料中不存在的条款。
作答要求：
1. 单选题 / 判断题：只选一个选项字母。
2. 多选题：对每个选项逐项判断，汇总所有正确项（允许全选）。
3. 填空题：给出填空内容；多空用英文分号 ; 分隔。
4. 只输出 JSON。
单选/判断/填空：{"keys":["A"],"brief":"..."}
多选：{"options":{"A":{"ok":true,"why":"..."},"B":{"ok":false,"why":"..."}},"brief":"..."}
"""


@dataclass
class DeepSeekSession:
    api_key: str
    model: str = "deepseek-v4-flash"
    base_url: str = "https://api.deepseek.com"
    pdf_path: str = ""
    mode: str = "rag"  # rag | full | direct
    top_k: int = 4
    keep_history: bool = False
    max_history_turns: int = 4
    temperature: float = 0.2
    exam: Any = field(default=None, repr=False)
    _client: Any = field(default=None, init=False, repr=False)
    _retriever: RegulationRetriever | None = field(default=None, init=False, repr=False)
    _prefix: list[dict[str, str]] = field(default_factory=list, init=False, repr=False)
    _history: list[dict[str, str]] = field(default_factory=list, init=False, repr=False)
    _ready: bool = field(default=False, init=False, repr=False)

    @classmethod
    def from_env(cls, exam: Any | None = None) -> DeepSeekSession:
        from dotenv import load_dotenv

        from .exam_profile import load_exam_profile

        load_dotenv(".env")
        api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            raise ValueError(
                "缺少 DEEPSEEK_API_KEY。请在 .env 中填写后重试。\n可参考 .env.example"
            )

        profile = exam
        if profile is None:
            try:
                profile = load_exam_profile(os.getenv("EXAM_ID") or None)
            except (FileNotFoundError, ValueError):
                profile = None

        pdf_path = os.getenv("EXAM_PDF_PATH", "").strip()
        mode = os.getenv("DEEPSEEK_MODE", "rag").strip().lower() or "rag"
        if profile is not None:
            if profile.has_refs and profile.primary_pdf is not None:
                pdf_path = str(profile.primary_pdf)
            else:
                # 无参考资料：强制直答，不走 RAG/full
                pdf_path = ""
                mode = "direct"
                print(f"[llm] exam={profile.id} 无 refs，使用 direct 模式（不调用 RAG）")

        return cls(
            api_key=api_key,
            model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash").strip(),
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip(),
            pdf_path=pdf_path,
            mode=mode,
            top_k=int(os.getenv("DEEPSEEK_RAG_TOP_K", "4") or "4"),
            keep_history=os.getenv("DEEPSEEK_KEEP_HISTORY", "false").lower()
            in {"1", "true", "yes", "y"},
            exam=profile,
        )

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise SystemExit("请先安装: pip install openai") from exc
        self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        return self._client

    def ensure_ready(self) -> None:
        if self._ready:
            return

        # 无参考或显式 direct
        no_refs = self.exam is not None and not self.exam.has_refs
        if self.mode == "direct" or no_refs or (self.mode != "full" and not self.pdf_path):
            print("[llm] direct 模式：不加载参考资料 / 不构建 RAG 索引")
            self._retriever = None
            self._prefix = [{"role": "system", "content": SYSTEM_PROMPT_DIRECT}]
            self.mode = "direct"
            self._ready = True
            return

        if self.mode == "full":
            from .pdf_text import extract_pdf_text

            if not self.pdf_path:
                raise ValueError("full 模式需要参考 PDF，但当前 exam 未配置 refs")
            cache = self.exam.clean_text_path if self.exam is not None else None
            print(f"[llm] full 模式：抽取整本 PDF {self.pdf_path}")
            pdf_text = extract_pdf_text(self.pdf_path, cache_path=cache, clean=True)
            print(f"[llm] 字符数: {len(pdf_text)}")
            self._prefix = [
                {"role": "system", "content": SYSTEM_PROMPT_FULL},
                {
                    "role": "user",
                    "content": "以下是校规校纪参考材料全文：\n\n" + pdf_text,
                },
                {
                    "role": "assistant",
                    "content": "已掌握参考材料。请发送题目。",
                },
            ]
        else:
            if self.exam is not None:
                from .chunker import index_is_fresh

                if self.exam.has_refs and not index_is_fresh(self.exam):
                    eid = self.exam.id
                    raise RuntimeError(
                        f"exam={eid} 参考 PDF 尚未预处理（或缓存已过期）。\n"
                        f"请先运行：\n"
                        f"  python -m pku_exam.cli --exam {eid} --build-index\n"
                        f"强制重建：\n"
                        f"  python -m pku_exam.cli --exam {eid} --build-index --force-rebuild"
                    )
                print(f"[llm] rag 模式：exam={self.exam.id} -> {self.exam.chunks_path}")
                self._retriever = RegulationRetriever.from_exam(self.exam)
            else:
                print(f"[llm] rag 模式：构建条款索引 {self.pdf_path}")
                self._retriever = RegulationRetriever.from_pdf(self.pdf_path)
            self._prefix = [{"role": "system", "content": SYSTEM_PROMPT_RAG}]
        self._ready = True

    def ask_question(self, question: ExamQuestion) -> list[str]:
        self.ensure_ready()
        client = self._ensure_client()

        context = ""
        if self.mode == "rag" and self._retriever is not None:
            query = self._retrieval_query(question)
            hits = self._retriever.search(query, top_k=self.top_k)
            context = self._retriever.format_context(hits)
            parts = []
            total_chars = 0
            for h in hits:
                n = len(h.chunk.text)
                total_chars += n
                parts.append(
                    f"{h.chunk.doc_title}:{h.chunk.article}(score={h.score:.1f},chars={n})"
                )
            print(
                f"[rag] n={len(hits)} sum_chars={total_chars} hits="
                + ", ".join(parts)
            )
        elif self.mode == "direct":
            print("[llm] direct：本题不附带检索上下文")

        user_content = self._format_question(question, context)
        messages = list(self._prefix)
        if self.keep_history:
            messages.extend(self._history)
        messages.append({"role": "user", "content": user_content})

        resp = client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            max_tokens=1200,
            response_format={"type": "json_object"},
            extra_body={"thinking": {"type": "disabled"}},
        )
        self._log_cache(resp)
        msg = resp.choices[0].message
        content = (msg.content or "").strip()
        if not content:
            content = (getattr(msg, "reasoning_content", None) or "").strip()
        if not content:
            print(f"[llm] 空响应 finish_reason={resp.choices[0].finish_reason}")

        keys = self._parse_keys(content, question)
        if not keys and content:
            print(f"[llm] 原始回复解析失败: {content[:240]}")

        if self.keep_history:
            self._history.append({"role": "user", "content": user_content})
            self._history.append({"role": "assistant", "content": content or "{}"})
            max_msgs = max(0, self.max_history_turns) * 2
            if max_msgs and len(self._history) > max_msgs:
                self._history = self._history[-max_msgs:]
        return keys

    @staticmethod
    def _retrieval_query(question: ExamQuestion) -> str:
        parts = [question.stem or ""]
        for opt in question.options:
            parts.append(f"{opt.key}.{opt.text}")
        return "\n".join(parts)

    def _format_question(self, question: ExamQuestion, context: str) -> str:
        qtype = (question.question_type or "").lower()
        lines: list[str] = []
        if context:
            lines.append("【检索到的参考条文】")
            lines.append(context)
            lines.append("")
        lines.append(f"题型: {question.question_type or 'unknown'}")
        lines.append(f"题干: {question.stem}")
        if question.options:
            lines.append("选项:")
            for opt in question.options:
                lines.append(f"{opt.key}. {opt.text}")

        if "multiple" in qtype or "多选" in qtype:
            lines.append(
                "请对每个选项逐项判断 ok=true/false，输出 JSON："
                '{"options":{"A":{"ok":true,"why":"文件+条款"},"B":{"ok":false,"why":"..."}},'
                '"brief":"总结"}'
            )
        else:
            lines.append(
                '请作答，只输出 JSON：{"keys":[...],"brief":"简要理由"}'
            )
        return "\n".join(lines)

    @staticmethod
    def _parse_keys(content: str, question: ExamQuestion) -> list[str]:
        data: Any = None
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            m = re.search(r"\{[\s\S]*\}", content)
            if m:
                try:
                    data = json.loads(m.group(0))
                except json.JSONDecodeError:
                    data = None
        if not isinstance(data, dict):
            return []

        brief = data.get("brief")
        if brief:
            print(f"[llm] brief: {brief}")

        keys: list[str] = []
        qtype = (question.question_type or "").lower()
        valid = {o.key.upper() for o in question.options}

        # 多选逐项
        options_obj = data.get("options")
        if isinstance(options_obj, dict) and question.options:
            for k, v in options_obj.items():
                key = str(k).strip().upper()[:1]
                ok = False
                if isinstance(v, dict):
                    ok = bool(v.get("ok") is True or v.get("correct") is True)
                elif isinstance(v, bool):
                    ok = v
                if ok and (not valid or key in valid) and key not in keys:
                    keys.append(key)
            keys = sorted(keys)
            return keys

        raw = data.get("keys") or data.get("answer") or data.get("answers")
        if isinstance(raw, str):
            keys = [raw]
        elif isinstance(raw, list):
            keys = [str(x).strip() for x in raw if str(x).strip()]

        if question.options and valid:
            norm: list[str] = []
            for k in keys:
                k2 = k.strip().upper()
                m = re.match(r"^([A-H])\b", k2)
                if m:
                    k2 = m.group(1)
                if k2 in valid and k2 not in norm:
                    norm.append(k2)
            keys = norm
            if "single" in qtype or "judgment" in qtype or "单选" in qtype or "判断" in qtype:
                keys = keys[:1]
        return keys

    @staticmethod
    def _log_cache(resp: Any) -> None:
        usage = getattr(resp, "usage", None)
        if not usage:
            return
        hit = getattr(usage, "prompt_cache_hit_tokens", None)
        miss = getattr(usage, "prompt_cache_miss_tokens", None)
        total = getattr(usage, "prompt_tokens", None)
        if hit is not None or miss is not None:
            print(f"[llm] cache hit={hit} miss={miss} prompt_tokens={total}")
