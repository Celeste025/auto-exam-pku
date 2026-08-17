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
5. 若现有参考条文明显不足以判断（缺关键条款、只有相近但可能不符的条文等），可请求补充检索：
   {"need_more":true,"brief":"缺少什么信息"}
   仅在确实不足时使用；能依据现有条文作答时不要使用 need_more。
正常作答：
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
    retry_top_k: int = 8
    rag_retry: bool = True
    keep_history: bool = False
    max_history_turns: int = 4
    temperature: float = 0.2
    exam: Any = field(default=None, repr=False)
    _client: Any = field(default=None, init=False, repr=False)
    _retriever: RegulationRetriever | None = field(default=None, init=False, repr=False)
    _prefix: list[dict[str, str]] = field(default_factory=list, init=False, repr=False)
    _history: list[dict[str, str]] = field(default_factory=list, init=False, repr=False)
    _ready: bool = field(default=False, init=False, repr=False)
    last_debug: dict[str, Any] | None = field(default=None, init=False, repr=False)

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
                profile = load_exam_profile(None)
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
            retry_top_k=int(os.getenv("DEEPSEEK_RAG_RETRY_TOP_K", "8") or "8"),
            rag_retry=os.getenv("DEEPSEEK_RAG_RETRY", "true").lower()
            in {"1", "true", "yes", "y"},
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
        self.last_debug = None

        query = self._retrieval_query(question) if self.mode == "rag" else ""
        context = ""
        rag_hits: list[dict[str, Any]] = []
        rag_meta: dict[str, Any] = {"pass": 1, "retried": False}

        if self.mode == "rag" and self._retriever is not None:
            hits = self._retriever.search(query, top_k=self.top_k)
            context, rag_hits = self._hits_to_context(hits)
        elif self.mode == "direct":
            print("[llm] direct：本题不附带检索上下文")

        allow_need_more = self.mode == "rag" and self._retriever is not None and self.rag_retry
        user_content = self._format_question(
            question, context, allow_need_more=allow_need_more
        )
        messages = list(self._prefix)
        if self.keep_history:
            messages.extend(self._history)
        messages.append({"role": "user", "content": user_content})

        content, cache_info = self._chat(client, messages)
        keys, brief, parsed = self._parse_keys(content, question)
        caches: list[Any] = [cache_info]

        # 缺信息 → 放宽 RAG 重试一次（更大 top_k + 关 early-stop + 多路 RRF）
        if (
            allow_need_more
            and self._wants_more(parsed)
            and self._retriever is not None
        ):
            print(f"[rag] 模型请求补充检索: {brief or (content or '')[:120]}")
            retry_k = max(self.retry_top_k, self.top_k + 2)
            hits2 = self._retriever.search_rrf(query, top_k=retry_k)
            context2, rag_hits2 = self._hits_to_context(hits2, label="retry")
            rag_meta = {
                "pass": 2,
                "retried": True,
                "first_brief": brief,
                "retry_top_k": retry_k,
            }
            # 合并命中供 debug（先重试、再首轮未覆盖的）
            seen = {h["chunk_id"] for h in rag_hits2}
            rag_hits = rag_hits2 + [h for h in rag_hits if h["chunk_id"] not in seen]

            user2 = self._format_question(
                question,
                context2,
                allow_need_more=False,
                retry_note=(
                    "已根据你的反馈做了更宽检索（多路融合、无 early-stop）。"
                    "请直接作答；即使仍不完全充分，也必须给出最佳答案，禁止再返回 need_more。"
                ),
            )
            messages2 = list(self._prefix)
            if self.keep_history:
                messages2.extend(self._history)
            messages2.append({"role": "user", "content": user2})
            content, cache_info2 = self._chat(client, messages2)
            caches.append(cache_info2)
            keys, brief, parsed = self._parse_keys(
                content, question, honor_need_more=False
            )
            user_content = user2

            # 仍无可用答案：同上下文再逼一次（不再扩 RAG）
            if self._wants_more(parsed) or (
                not keys and not self._has_option_answers(parsed, question)
            ):
                print("[rag] 补充后仍不足/无答案，强制要求作答（不再扩检索）")
                messages3 = list(messages2)
                messages3.append(
                    {"role": "assistant", "content": content or '{"need_more":true}'}
                )
                messages3.append(
                    {
                        "role": "user",
                        "content": (
                            "禁止 need_more。请仅依据已给出的参考条文给出最终 JSON 答案"
                            "（可标明依据不足，但仍须填写 keys 或 options）。"
                        ),
                    }
                )
                content, cache_info3 = self._chat(client, messages3)
                caches.append(cache_info3)
                keys, brief, parsed = self._parse_keys(
                    content, question, honor_need_more=False
                )
                rag_meta["forced_answer"] = True

        if not keys and content and not self._has_option_answers(parsed, question):
            print(f"[llm] 原始回复解析失败: {content[:240]}")

        if self.keep_history:
            self._history.append({"role": "user", "content": user_content})
            self._history.append({"role": "assistant", "content": content or "{}"})
            max_msgs = max(0, self.max_history_turns) * 2
            if max_msgs and len(self._history) > max_msgs:
                self._history = self._history[-max_msgs:]

        self.last_debug = {
            "mode": self.mode,
            "model": self.model,
            "rag_hits": rag_hits,
            "rag_meta": rag_meta,
            "llm_raw": content,
            "brief": brief,
            "parsed": parsed,
            "keys": keys,
            "cache": caches[-1] if caches else None,
            "caches": caches,
        }
        return keys

    def _chat(self, client: Any, messages: list[dict[str, str]]) -> tuple[str, dict[str, Any] | None]:
        resp = client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            max_tokens=1200,
            response_format={"type": "json_object"},
            extra_body={"thinking": {"type": "disabled"}},
        )
        cache_info = self._log_cache(resp)
        msg = resp.choices[0].message
        content = (msg.content or "").strip()
        if not content:
            content = (getattr(msg, "reasoning_content", None) or "").strip()
        if not content:
            print(f"[llm] 空响应 finish_reason={resp.choices[0].finish_reason}")
        return content, cache_info

    def _hits_to_context(
        self, hits: list[Any], *, label: str = ""
    ) -> tuple[str, list[dict[str, Any]]]:
        assert self._retriever is not None
        context = self._retriever.format_context(hits)
        rag_hits: list[dict[str, Any]] = []
        parts: list[str] = []
        total_chars = 0
        for h in hits:
            n = len(h.chunk.text)
            total_chars += n
            parts.append(
                f"{h.chunk.doc_title}:{h.chunk.article}(score={h.score:.1f},chars={n})"
            )
            rag_hits.append(
                {
                    "doc_title": h.chunk.doc_title,
                    "article": h.chunk.article,
                    "audience": h.chunk.audience,
                    "score": round(float(h.score), 3),
                    "chars": n,
                    "chunk_id": h.chunk.chunk_id,
                    "text": h.chunk.text,
                }
            )
        tag = f"[{label}] " if label else ""
        print(f"[rag] {tag}n={len(hits)} sum_chars={total_chars} hits=" + ", ".join(parts))
        return context, rag_hits

    @staticmethod
    def _wants_more(parsed: dict[str, Any] | None) -> bool:
        if not isinstance(parsed, dict):
            return False
        v = parsed.get("need_more")
        if v is True:
            return True
        if isinstance(v, str) and v.strip().lower() in {"1", "true", "yes", "y"}:
            return True
        if isinstance(v, (int, float)) and int(v) == 1:
            return True
        return False

    @staticmethod
    def _has_option_answers(parsed: dict[str, Any] | None, question: ExamQuestion) -> bool:
        if not isinstance(parsed, dict) or not question.options:
            return False
        options_obj = parsed.get("options")
        return isinstance(options_obj, dict) and bool(options_obj)

    @staticmethod
    def _retrieval_query(question: ExamQuestion) -> str:
        parts = [question.stem or ""]
        for opt in question.options:
            parts.append(f"{opt.key}.{opt.text}")
        return "\n".join(parts)

    def _format_question(
        self,
        question: ExamQuestion,
        context: str,
        *,
        allow_need_more: bool = False,
        retry_note: str = "",
    ) -> str:
        qtype = (question.question_type or "").lower()
        lines: list[str] = []
        if context:
            lines.append("【检索到的参考条文】")
            lines.append(context)
            lines.append("")
        if retry_note:
            lines.append(f"【补充说明】{retry_note}")
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
        if allow_need_more:
            lines.append(
                '若参考条文明显不足以作答，可改为输出：'
                '{"need_more":true,"brief":"缺少何种条款/信息"}'
            )
        elif retry_note:
            lines.append("禁止输出 need_more，必须给出 keys 或 options。")
        return "\n".join(lines)

    @staticmethod
    def _parse_keys(
        content: str,
        question: ExamQuestion,
        *,
        honor_need_more: bool = True,
    ) -> tuple[list[str], str | None, dict[str, Any] | None]:
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
            return [], None, None

        brief = data.get("brief")
        if brief:
            print(f"[llm] brief: {brief}")

        # 缺信息信号：首轮不当作解析失败
        if honor_need_more and DeepSeekSession._wants_more(data):
            return [], (str(brief) if brief else None), data

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
            return keys, (str(brief) if brief else None), data

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
        return keys, (str(brief) if brief else None), data

    @staticmethod
    def _log_cache(resp: Any) -> dict[str, Any] | None:
        usage = getattr(resp, "usage", None)
        if not usage:
            return None
        hit = getattr(usage, "prompt_cache_hit_tokens", None)
        miss = getattr(usage, "prompt_cache_miss_tokens", None)
        total = getattr(usage, "prompt_tokens", None)
        info = {
            "prompt_cache_hit_tokens": hit,
            "prompt_cache_miss_tokens": miss,
            "prompt_tokens": total,
        }
        if hit is not None or miss is not None:
            print(f"[llm] cache hit={hit} miss={miss} prompt_tokens={total}")
        return info
