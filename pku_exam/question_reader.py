from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, Tag

from .models import ExamQuestion, QuestionOption

# 常见题号样式：第1题 / 1/50 / 题目1
INDEX_PATTERNS = [
    re.compile(r"第\s*(\d+)\s*/\s*(\d+)\s*题"),
    re.compile(r"第\s*(\d+)\s*题"),
    re.compile(r"(?:题目|题号)[:：]?\s*(\d+)\s*/\s*(\d+)"),
    re.compile(r"(\d+)\s*/\s*(\d+)"),
]

TYPE_KEYWORDS = ("单选题", "多选题", "判断题", "填空题", "简答题", "单选", "多选", "判断")

OPTION_LINE = re.compile(
    r"^\s*([A-HＡ-Ｈ])[\.．、\)）:\s]\s*(.+?)\s*$",
    re.MULTILINE,
)

# 答题页可能出现的容器选择器（登录后根据真实 DOM 再精调）
QUESTION_ROOT_SELECTORS = [
    "[class*='question']",
    "[class*='exam']",
    "[class*='subject']",
    "[class*='topic']",
    "main",
    "#app",
    "body",
]


class QuestionReader:
    """从 HTML / URL / Playwright 页面读取当前题目。"""

    def __init__(self, base_url: str = "https://exam.pku.edu.cn") -> None:
        self.base_url = base_url.rstrip("/")

    # ---------- 公开入口 ----------

    def from_html(self, html: str, *, source: str = "html") -> ExamQuestion:
        soup = BeautifulSoup(html, "lxml")
        question = self._parse_from_soup(soup)
        question.source = source
        return question

    def from_file(self, path: str | Path) -> ExamQuestion:
        file_path = Path(path)
        html = file_path.read_text(encoding="utf-8")
        return self.from_html(html, source=str(file_path))

    def from_url(
        self,
        url: str,
        *,
        cookies: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 20.0,
    ) -> ExamQuestion:
        """用 Cookie 拉取页面。未登录会被 IAAA 重定向到登录页。"""
        default_headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if headers:
            default_headers.update(headers)

        with httpx.Client(
            headers=default_headers,
            cookies=cookies or {},
            follow_redirects=True,
            timeout=timeout,
        ) as client:
            resp = client.get(urljoin(self.base_url + "/", url))
            resp.raise_for_status()
            final_url = str(resp.url)
            html = resp.text

        if self._looks_like_login_page(html, final_url):
            raise PermissionError(
                "未能进入答题页：当前会话未登录或 Cookie 失效，"
                "页面被重定向到统一身份认证。请先登录后导出 Cookie，"
                "或改用 from_file / from_playwright_page。"
            )
        return self.from_html(html, source=final_url)

    def from_playwright_page(self, page: Any) -> ExamQuestion:
        """从已登录的 Playwright Page 读取当前题目（SPA 渲染后有效）。"""
        html = page.content()
        url = page.url
        if self._looks_like_login_page(html, url):
            raise PermissionError("Playwright 当前页面仍是登录页，请先完成 IAAA 登录。")
        question = self.from_html(html, source=url)
        # 再尝试用页面文本做一次增强（部分 Vue/React 文本节点更干净）
        try:
            body_text = page.inner_text("body")
            if body_text and (not question.stem or len(question.stem) < 8):
                enriched = self._parse_from_plain_text(body_text)
                if enriched.stem:
                    enriched.source = url
                    return enriched
        except Exception:
            pass
        return question

    def dump_debug(self, html: str, out_dir: str | Path = "debug") -> Path:
        """保存原始 HTML，便于对照真实 DOM 调整选择器。"""
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        path = out / "last_page.html"
        path.write_text(html, encoding="utf-8")
        return path

    # ---------- 解析逻辑 ----------

    def _looks_like_login_page(self, html: str, url: str) -> bool:
        url_l = url.lower()
        if "iaaa.pku.edu.cn" in url_l:
            return True
        markers = ("账号登录", "扫码登录", "Unified Authentication", "oauth.jsp")
        return any(m in html for m in markers)

    def _parse_from_soup(self, soup: BeautifulSoup) -> ExamQuestion:
        # 优先尝试从内嵌 JSON / __NEXT_DATA__ / window 状态里取题
        from_json = self._try_parse_embedded_json(soup)
        if from_json and from_json.stem:
            return from_json

        root = self._pick_question_root(soup)
        plain = root.get_text("\n", strip=True)
        question = self._parse_from_plain_text(plain)

        # 若纯文本选项不足，再从 radio/checkbox/label 结构补
        if len(question.options) < 2:
            options = self._extract_options_from_inputs(root)
            if options:
                question.options = options

        if not question.stem:
            question.stem = self._guess_stem(root, question.options)

        question.raw_text = plain
        question.meta["parser"] = "html_dom"
        return question

    def _pick_question_root(self, soup: BeautifulSoup) -> Tag:
        for selector in QUESTION_ROOT_SELECTORS:
            node = soup.select_one(selector)
            if node and len(node.get_text(strip=True)) > 20:
                return node
        return soup.body or soup

    def _parse_from_plain_text(self, text: str) -> ExamQuestion:
        index, total = self._extract_index(text)
        qtype = self._extract_type(text)
        options = self._extract_options_from_text(text)
        stem = self._extract_stem_from_text(text, options)
        return ExamQuestion(
            index=index,
            total=total,
            question_type=qtype,
            stem=stem,
            options=options,
            raw_text=text,
            meta={"parser": "plain_text"},
        )

    def _extract_index(self, text: str) -> tuple[int | None, int | None]:
        for pattern in INDEX_PATTERNS:
            m = pattern.search(text)
            if not m:
                continue
            groups = m.groups()
            if len(groups) == 2:
                return int(groups[0]), int(groups[1])
            return int(groups[0]), None
        return None, None

    def _extract_type(self, text: str) -> str | None:
        for kw in TYPE_KEYWORDS:
            if kw in text:
                if kw in ("单选", "多选", "判断") and f"{kw}题" in text:
                    return f"{kw}题"
                if kw.endswith("题"):
                    return kw
                return f"{kw}题"
        return None

    def _extract_options_from_text(self, text: str) -> list[QuestionOption]:
        options: list[QuestionOption] = []
        for m in OPTION_LINE.finditer(text):
            key = self._normalize_option_key(m.group(1))
            val = m.group(2).strip()
            if val:
                options.append(QuestionOption(key=key, text=val))
        # 去重保序
        seen: set[str] = set()
        uniq: list[QuestionOption] = []
        for opt in options:
            if opt.key in seen:
                continue
            seen.add(opt.key)
            uniq.append(opt)
        return uniq

    def _extract_options_from_inputs(self, root: Tag) -> list[QuestionOption]:
        options: list[QuestionOption] = []
        labels = root.select("label")
        for i, label in enumerate(labels):
            text = label.get_text(" ", strip=True)
            if not text:
                continue
            m = re.match(r"^([A-HＡ-Ｈ])[\.．、\)）:\s]*(.+)$", text)
            if m:
                key = self._normalize_option_key(m.group(1))
                options.append(QuestionOption(key=key, text=m.group(2).strip()))
            else:
                key = chr(ord("A") + i)
                options.append(QuestionOption(key=key, text=text))
        return options

    def _extract_stem_from_text(
        self, text: str, options: list[QuestionOption]
    ) -> str:
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not lines:
            return ""

        option_keys = {o.key for o in options}
        stem_lines: list[str] = []
        for ln in lines:
            # 跳过明显是导航/页眉的行
            if any(x in ln for x in ("倒计时", "剩余时间", "交卷", "上一题", "下一题")):
                continue
            m = OPTION_LINE.match(ln)
            if m and self._normalize_option_key(m.group(1)) in option_keys:
                break
            # 跳过纯题型/题号行，但题号后若带题干则保留后半段
            if re.fullmatch(r"第\s*\d+\s*(?:/\s*\d+\s*)?题", ln):
                continue
            if ln in TYPE_KEYWORDS or ln in {f"{k}" for k in TYPE_KEYWORDS}:
                continue
            stem_lines.append(ln)

        stem = "\n".join(stem_lines).strip()
        # 若选项文本也混进了 stem，裁掉从第一个选项开始的部分
        if options:
            first = f"{options[0].key}."
            pos = stem.find(first)
            if pos > 0:
                stem = stem[:pos].strip()
        return stem

    def _guess_stem(self, root: Tag, options: list[QuestionOption]) -> str:
        for selector in (
            "[class*='stem']",
            "[class*='title']",
            "[class*='content']",
            "h1",
            "h2",
            "h3",
            "p",
        ):
            node = root.select_one(selector)
            if not node:
                continue
            text = node.get_text(" ", strip=True)
            if len(text) >= 8:
                return text
        plain = root.get_text("\n", strip=True)
        return self._extract_stem_from_text(plain, options)

    def _try_parse_embedded_json(self, soup: BeautifulSoup) -> ExamQuestion | None:
        """尝试从 script 标签中的 JSON 提取题目（不同前端实现差异大）。"""
        scripts = soup.find_all("script")
        candidates: list[Any] = []
        for script in scripts:
            content = script.string or script.get_text() or ""
            content = content.strip()
            if not content:
                continue
            if content.startswith("{") or content.startswith("["):
                try:
                    candidates.append(json.loads(content))
                except json.JSONDecodeError:
                    pass
            # window.__INITIAL_STATE__ = {...}
            m = re.search(
                r"(?:window\.)?(?:__INITIAL_STATE__|__NUXT__|__NEXT_DATA__)\s*=\s*(\{.*?\})\s*;?\s*$",
                content,
                re.DOTALL,
            )
            if m:
                try:
                    candidates.append(json.loads(m.group(1)))
                except json.JSONDecodeError:
                    pass

        for data in candidates:
            found = self._walk_json_for_question(data)
            if found:
                return found
        return None

    def _walk_json_for_question(self, data: Any, depth: int = 0) -> ExamQuestion | None:
        if depth > 8:
            return None
        if isinstance(data, dict):
            keys = {k.lower() for k in data.keys()}
            # 启发式：同时有题干和选项字段
            stem_key = next(
                (
                    k
                    for k in data.keys()
                    if k.lower()
                    in {
                        "stem",
                        "title",
                        "content",
                        "question",
                        "questioncontent",
                        "questiontitle",
                        "subject",
                    }
                ),
                None,
            )
            opts_key = next(
                (
                    k
                    for k in data.keys()
                    if k.lower() in {"options", "choices", "answers", "items"}
                ),
                None,
            )
            if stem_key and opts_key:
                stem = str(data[stem_key]).strip()
                options = self._normalize_json_options(data[opts_key])
                if stem and options:
                    qtype = None
                    for tk in ("type", "questionType", "question_type", "typeName"):
                        if tk in data:
                            qtype = str(data[tk])
                            break
                    return ExamQuestion(
                        question_type=qtype,
                        stem=stem,
                        options=options,
                        meta={"parser": "embedded_json", "keys": list(keys)},
                    )
            for value in data.values():
                found = self._walk_json_for_question(value, depth + 1)
                if found:
                    return found
        elif isinstance(data, list):
            for item in data:
                found = self._walk_json_for_question(item, depth + 1)
                if found:
                    return found
        return None

    def _normalize_json_options(self, raw: Any) -> list[QuestionOption]:
        options: list[QuestionOption] = []
        if not isinstance(raw, list):
            return options
        for i, item in enumerate(raw):
            if isinstance(item, str):
                m = OPTION_LINE.match(item)
                if m:
                    options.append(
                        QuestionOption(
                            key=self._normalize_option_key(m.group(1)),
                            text=m.group(2).strip(),
                        )
                    )
                else:
                    options.append(QuestionOption(key=chr(ord("A") + i), text=item))
            elif isinstance(item, dict):
                key = (
                    item.get("key")
                    or item.get("label")
                    or item.get("option")
                    or item.get("code")
                    or chr(ord("A") + i)
                )
                text = (
                    item.get("text")
                    or item.get("content")
                    or item.get("value")
                    or item.get("title")
                    or ""
                )
                options.append(
                    QuestionOption(key=self._normalize_option_key(str(key)), text=str(text).strip())
                )
        return [o for o in options if o.text]

    @staticmethod
    def _normalize_option_key(key: str) -> str:
        table = str.maketrans("ＡＢＣＤＥＦＧＨ", "ABCDEFGH")
        key = key.translate(table).upper().strip()
        return key[:1] if key else "?"
