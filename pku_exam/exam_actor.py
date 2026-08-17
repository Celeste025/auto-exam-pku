"""在考试页上执行：选题 / 下一题 / 读当前题；可选交卷。"""

from __future__ import annotations

import re
import time
from typing import Any

from .models import ExamQuestion, QuestionOption


class ExamActor:
    """Playwright 页面操作器。"""

    def __init__(self, page: Any, *, step_delay_ms: int = 400) -> None:
        self.page = page
        self.step_delay_ms = step_delay_ms

    def _sleep(self, ms: int | None = None) -> None:
        self.page.wait_for_timeout(ms if ms is not None else self.step_delay_ms)

    def read_current_question(self) -> ExamQuestion:
        """从当前渲染的题目卡片读取一题。"""
        self.page.wait_for_selector(".question-card, .option-item, .q-content", timeout=30_000)

        meta = self.page.evaluate(
            """() => {
                const card = document.querySelector('.question-card') || document.querySelector('main');
                if (!card) return null;
                const type = (card.querySelector('.q-type-badge')?.innerText || '').trim();
                const numText = (card.querySelector('.q-num')?.innerText || '').trim();
                const progress = (document.querySelector('.q-progress')?.innerText || '').trim();
                const stemEl = card.querySelector('.q-content, .fill-question-text, p.q-content');
                const stem = (stemEl?.innerText || '').trim();
                const options = [...card.querySelectorAll('label.option-item')].map(lab => {
                    const key = (lab.querySelector('.opt-marker')?.innerText || '').trim();
                    const text = (lab.querySelector('.opt-content')?.innerText || '').trim();
                    return { key, text };
                });
                return { type, numText, progress, stem, options };
            }"""
        )
        if not meta:
            raise RuntimeError("未找到题目卡片 DOM")

        index = None
        total = None
        m = re.search(r"(\d+)\s*/\s*(\d+)", meta.get("progress") or "")
        if m:
            index, total = int(m.group(1)), int(m.group(2))
        else:
            m2 = re.search(r"(\d+)", meta.get("numText") or "")
            if m2:
                index = int(m2.group(1))

        qtype_raw = meta.get("type") or ""
        type_map = {
            "单选题": "single",
            "多选题": "multiple",
            "判断题": "judgment",
            "填空题": "fill_blank",
        }
        qtype = type_map.get(qtype_raw, qtype_raw)

        options = [
            QuestionOption(key=o["key"], text=o["text"])
            for o in (meta.get("options") or [])
            if o.get("key")
        ]
        return ExamQuestion(
            index=index,
            total=total,
            question_type=qtype,
            stem=meta.get("stem") or "",
            options=options,
            raw_text="",
            source=self.page.url,
            meta={"parser": "dom_current"},
        )

    def select_keys(self, keys: list[str], *, question_type: str | None = None) -> None:
        """勾选选项。单选/判断点 radio；多选可多点；填空写入文本。"""
        if not keys:
            return
        qtype = (question_type or "").lower()

        if "fill" in qtype or "blank" in qtype:
            blanks = self.page.locator("input.inline-blank-input")
            if blanks.count() > 0:
                n = blanks.count()
                for i in range(n):
                    # 多空：按 keys 顺序填；不足则复用最后一个
                    text = keys[i] if i < len(keys) else keys[-1]
                    blanks.nth(i).fill(text)
                    self._sleep(100)
                return
            ta = self.page.locator("textarea")
            if ta.count() > 0:
                ta.first.fill(keys[0])
                self._sleep()
                return
            raise RuntimeError("填空题未找到输入框")

        # 多选：先清空已选（再点目标）；每次点 first，避免 nth 索引漂移
        if "multiple" in qtype or "多选" in qtype:
            while True:
                selected = self.page.locator("label.option-item.selected")
                if selected.count() == 0:
                    break
                selected.first.click()
                self._sleep(80)

        for key in keys:
            key = key.strip().upper()
            loc = self.page.locator("label.option-item").filter(
                has=self.page.locator(".opt-marker", has_text=re.compile(f"^{key}$"))
            )
            if loc.count() == 0:
                # 退化：整段文本以 key 开头
                loc = self.page.locator("label.option-item", has_text=re.compile(rf"^{key}\b"))
            if loc.count() == 0:
                raise RuntimeError(f"找不到选项 {key}")
            loc.first.click()
            self._sleep()

    def click_next(self) -> bool:
        """点击下一题。若按钮禁用（已是最后一题）返回 False。"""
        btn = self.page.locator("button.btn-next")
        if btn.count() == 0:
            raise RuntimeError("未找到「下一题」按钮")
        if btn.is_disabled():
            return False
        before = self.read_progress()
        btn.click()
        self._sleep(500)
        # 等题号变化
        try:
            self.page.wait_for_function(
                """([prev]) => {
                    const t = (document.querySelector('.q-progress')?.innerText || '').trim();
                    return t && t !== prev;
                }""",
                arg=[before.get("text") or ""],
                timeout=5_000,
            )
        except Exception:
            pass
        return True

    def click_prev(self) -> bool:
        btn = self.page.locator("button.btn-prev")
        if btn.count() == 0 or btn.is_disabled():
            return False
        btn.click()
        self._sleep(500)
        return True

    def read_progress(self) -> dict[str, Any]:
        text = ""
        try:
            text = self.page.locator(".q-progress").inner_text(timeout=2_000).strip()
        except Exception:
            pass
        index = total = None
        m = re.search(r"(\d+)\s*/\s*(\d+)", text)
        if m:
            index, total = int(m.group(1)), int(m.group(2))
        return {"text": text, "index": index, "total": total}

    def is_last_question(self) -> bool:
        btn = self.page.locator("button.btn-next")
        return btn.count() > 0 and btn.is_disabled()

    def submit_exam(self, *, confirm: bool = True) -> None:
        """点击「提交考试」，并尽量确认弹窗。"""
        btn = self.page.locator("button.btn-submit-side")
        if btn.count() == 0:
            btn = self.page.get_by_role("button", name=re.compile(r"提交考试|提交试卷|交卷"))
        if btn.count() == 0:
            raise RuntimeError("未找到「提交考试」按钮")
        btn.first.click()
        self._sleep(400)

        if not confirm:
            return

        # Element Plus / Ant Design / 通用确认框
        confirm_selectors = [
            ".el-message-box__btns button.el-button--primary",
            ".el-dialog__footer button.el-button--primary",
            ".ant-modal-confirm-btns .ant-btn-primary",
            ".ant-modal-footer .ant-btn-primary",
            "button:has-text('确定')",
            "button:has-text('确认')",
            "button:has-text('确认提交')",
            "button:has-text('确认交卷')",
        ]
        for sel in confirm_selectors:
            loc = self.page.locator(sel)
            try:
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    self._sleep(500)
                    print(f"[auto] 已点击确认弹窗: {sel}")
                    return
            except Exception:
                continue
        # 再试一次按角色
        for name in ("确定", "确认", "确认提交", "确认交卷"):
            loc = self.page.get_by_role("button", name=name)
            try:
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    self._sleep(500)
                    print(f"[auto] 已点击确认弹窗按钮: {name}")
                    return
            except Exception:
                continue
        print("[auto] 已点「提交考试」；未检测到确认弹窗（可能已直接提交或需人工确认）")
