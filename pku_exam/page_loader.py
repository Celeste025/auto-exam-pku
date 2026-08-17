from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import ExamQuestion, QuestionOption
from .question_reader import QuestionReader

# 答题页渲染完成后常见的可见文案
CONTENT_HINTS = ("题", "单选", "多选", "判断", "下一题", "上一题", "交卷")


class ExamPageLoader:
    """等待 Vue 考试页渲染，并顺带拦截可能的题目 API。"""

    def __init__(self, reader: QuestionReader | None = None) -> None:
        self.reader = reader or QuestionReader()
        self.api_payloads: list[dict[str, Any]] = []

    def attach_network_sniffer(self, page: Any) -> None:
        def _on_response(response: Any) -> None:
            try:
                url = response.url
                ctype = (response.headers or {}).get("content-type", "")
                if "json" not in ctype.lower() and not url.lower().endswith(".json"):
                    # 仍尝试读取疑似业务 API
                    if "/api/" not in url.lower() and "exam" not in url.lower():
                        return
                if response.status != 200:
                    return
                data = response.json()
            except Exception:
                return
            self.api_payloads.append({"url": url, "data": data})

        page.on("response", _on_response)

    def wait_for_question_ui(self, page: Any, timeout_ms: int = 60_000) -> None:
        """等到 #app 出现实质内容，或超时。"""
        page.wait_for_selector("#app", timeout=timeout_ms)
        # SPA 常见：先空壳再请求数据
        try:
            page.wait_for_load_state("networkidle", timeout=min(timeout_ms, 20_000))
        except Exception:
            pass

        page.wait_for_function(
            """() => {
                const app = document.querySelector('#app');
                if (!app) return false;
                const text = (app.innerText || '').trim();
                if (text.length < 10) return false;
                const hints = ['题', '单选', '多选', '判断', '下一题', '上一题', '交卷', '选项'];
                return hints.some(h => text.includes(h)) || text.length > 40;
            }""",
            timeout=timeout_ms,
        )

    def load_question(self, page: Any, *, dump_dir: Path = Path("debug")) -> ExamQuestion:
        dump_dir = Path(dump_dir)
        dump_dir.mkdir(parents=True, exist_ok=True)

        self.attach_network_sniffer(page)
        # 若刚 goto，给前端一点时间；若已在页面上也无害
        try:
            self.wait_for_question_ui(page)
        except Exception as exc:
            html = page.content()
            self.reader.dump_debug(html, dump_dir)
            self._dump_api(dump_dir)
            raise TimeoutError(
                "考试页未渲染出题目（可能仍在加载、有弹窗，或考试已结束）。"
                f" 当前 URL: {page.url}\n原始错误: {exc}"
            ) from exc

        html = page.content()
        # 成功路径不落盘；失败时已在上方写出 debug 快照

        body_text = ""
        try:
            body_text = page.inner_text("body")
        except Exception:
            pass
        if "无法进入考试" in body_text or "另一个标签页" in body_text or "另一个" in body_text and "打开" in body_text:
            self.reader.dump_debug(html, dump_dir)
            self._dump_api(dump_dir)
            raise RuntimeError(
                "考试系统拒绝进入：该考试已在另一个浏览器标签页打开。\n"
                "请先关闭你平时浏览器里正在作答的标签页，再运行：\n"
                "  python -m pku_exam.cli --scrape\n"
                "（考试站通常只允许同一场考试开一个作答窗口。）"
            )

        # 优先从拦截到的 JSON 解析
        from_api = self._question_from_api_payloads()
        if from_api and from_api.stem:
            from_api.source = page.url
            return from_api

        return self.reader.from_playwright_page(page)

    def _dump_api(self, dump_dir: Path) -> None:
        path = dump_dir / "api_payloads.json"
        path.write_text(
            json.dumps(self.api_payloads, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _question_from_api_payloads(self) -> ExamQuestion | None:
        for item in self.api_payloads:
            found = self.reader._walk_json_for_question(item.get("data"))  # noqa: SLF001
            if found and found.stem:
                found.meta["api_url"] = item.get("url")
                found.meta["parser"] = "network_api"
                return found
        # 宽松：从任意 JSON 字符串字段里找题干+选项
        for item in self.api_payloads:
            text_blob = json.dumps(item.get("data"), ensure_ascii=False)
            if "选项" in text_blob or re.search(r'"[A-D]"\s*:', text_blob):
                q = self.reader._parse_from_plain_text(  # noqa: SLF001
                    self._flatten_json_text(item.get("data"))
                )
                if q.stem:
                    q.meta["api_url"] = item.get("url")
                    q.meta["parser"] = "network_api_text"
                    return q
        return None

    @staticmethod
    def _flatten_json_text(data: Any) -> str:
        parts: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)
            elif isinstance(node, str) and node.strip():
                parts.append(node.strip())

        walk(data)
        return "\n".join(parts)
