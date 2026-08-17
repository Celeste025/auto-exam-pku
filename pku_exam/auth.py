from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Settings


class ExamAuthError(RuntimeError):
    """登录或会话复用失败。"""


def _ensure_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise SystemExit(
            "未安装 playwright。请执行：\n"
            "  pip install -r requirements.txt\n"
            "  playwright install chromium"
        ) from exc
    return sync_playwright


def _is_login_url(url: str) -> bool:
    return "iaaa.pku.edu.cn" in url.lower() or "oauth.jsp" in url.lower()


def login_and_open_exam(
    settings: Settings,
    *,
    reuse_storage: bool = True,
    timeout_ms: int = 60_000,
) -> tuple[Any, Any, Any]:
    """打开浏览器，复用 storage_state，进入答题页。

    返回 (playwright, browser, page)。调用方负责 browser.close() / playwright.stop()，
    或使用 login_session 上下文管理器。
    """
    sync_playwright = _ensure_playwright()
    settings.storage_state_path.parent.mkdir(parents=True, exist_ok=True)

    pw = sync_playwright().start()
    browser = pw.chromium.launch(headless=settings.headless)

    storage = settings.storage_state_path
    context_kwargs: dict[str, Any] = {}
    if reuse_storage and storage.exists():
        context_kwargs["storage_state"] = str(storage)

    context = browser.new_context(**context_kwargs)
    page = context.new_page()
    page.set_default_timeout(timeout_ms)

    # 先打开考试站原点以操作 localStorage，清掉脏锁再进试卷
    page.goto("https://exam.pku.edu.cn/", wait_until="domcontentloaded")
    if not _is_login_url(page.url):
        cleared = clear_exam_tab_lock(page)
        if cleared:
            print(f"已清除 {cleared} 个残留作答锁 (pku-exam-active:*)")

    page.goto(settings.exam_url, wait_until="domcontentloaded")

    if _is_login_url(page.url):
        raise ExamAuthError(
            "会话已失效或未登录。请先执行: python -m pku_exam.cli --manual-login"
            + (f" --exam {settings.exam_id}" if settings.exam_id else "")
        )

    # 确保在目标考试页
    if settings.exam_url.rstrip("/") not in page.url.rstrip("/"):
        page.goto(settings.exam_url, wait_until="domcontentloaded")
        if _is_login_url(page.url):
            raise ExamAuthError("会话无效，访问考试页再次要求登录。请重新 --manual-login。")

    return pw, browser, page


class LoginSession:
    """上下文管理器：自动关闭浏览器。"""

    def __init__(self, settings: Settings, *, reuse_storage: bool = True) -> None:
        self.settings = settings
        self.reuse_storage = reuse_storage
        self._pw = None
        self.browser = None
        self.page = None

    def __enter__(self):
        self._pw, self.browser, self.page = login_and_open_exam(
            self.settings, reuse_storage=self.reuse_storage
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.browser:
            self.browser.close()
        if self._pw:
            self._pw.stop()

    def save_storage(self, path: Path | None = None) -> Path:
        assert self.page is not None
        target = path or self.settings.storage_state_path
        target.parent.mkdir(parents=True, exist_ok=True)
        self.page.context.storage_state(path=str(target))
        return target


def _is_exam_site(url: str) -> bool:
    return "exam.pku.edu.cn" in url.lower() and not _is_login_url(url)


def clear_exam_tab_lock(page: Any) -> int:
    """清除前端「单标签作答锁」(localStorage: pku-exam-active:*)。

    上次 Playwright 登录后立刻关窗时，锁会写进 storage_state；
    下次带着旧 owner 进来，页面会误报「已在另一个标签页打开」。
    """
    return page.evaluate(
        """() => {
            const keys = [];
            for (let i = 0; i < localStorage.length; i++) {
                const k = localStorage.key(i);
                if (k && k.startsWith('pku-exam-active:')) keys.push(k);
            }
            keys.forEach(k => localStorage.removeItem(k));
            return keys.length;
        }"""
    )


def strip_exam_tab_lock_from_storage_file(path: Path) -> int:
    """从已保存的 storage_state.json 里删掉作答锁，避免脏锁被复用。"""
    import json

    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    removed = 0
    for origin in data.get("origins") or []:
        ls = origin.get("localStorage") or []
        kept = []
        for item in ls:
            name = item.get("name") or ""
            if name.startswith("pku-exam-active:"):
                removed += 1
                continue
            kept.append(item)
        origin["localStorage"] = kept
    if removed:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return removed


def manual_login_and_save(settings: Settings, timeout_ms: int = 300_000) -> Path:
    """更安全的推荐方式：人工在可视浏览器里登录，程序只保存会话。

    密码不经过脚本填写，适合有验证码 / 扫码 / 二次验证的场景。
    会自动轮询：一旦检测到进入 exam.pku.edu.cn 即保存，无需抢着按回车。
    """
    sync_playwright = _ensure_playwright()
    settings.storage_state_path.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(settings.exam_url, wait_until="domcontentloaded")

        print("=" * 60)
        print("请在【刚刚弹出的 Chromium 窗口】里完成 IAAA 登录。")
        print("可用账号密码或 App 扫码；若有二次验证一并完成。")
        print("登录成功并看到考试系统页面后，本程序会自动保存会话。")
        print(f"当前 URL: {page.url}")
        print("=" * 60)

        deadline = timeout_ms
        step = 2000
        elapsed = 0
        last_url = ""
        while elapsed < deadline:
            page.wait_for_timeout(step)
            elapsed += step
            url = page.url
            if url != last_url:
                print(f"[{elapsed // 1000:>3}s] URL -> {url}")
                last_url = url
            if _is_exam_site(url):
                # 尽量回到目标考试页再存会话
                if settings.exam_url.rstrip("/") not in url.rstrip("/"):
                    page.goto(settings.exam_url, wait_until="domcontentloaded")
                    page.wait_for_timeout(1000)
                    if _is_login_url(page.url):
                        raise ExamAuthError(
                            "已登录考试站，但打开目标试卷又跳回登录页。"
                            f" 当前 URL: {page.url}"
                        )
                path = settings.storage_state_path
                context.storage_state(path=str(path))
                print("=" * 60)
                print("登录成功，会话已保存（浏览器关闭后会清理作答锁文件）。")
                print(f"会话文件: {path}")
                print(f"最终 URL: {page.url}")
                print("按回车关闭浏览器…")
                print("=" * 60)
                try:
                    input()
                except EOFError:
                    page.wait_for_timeout(2000)
                browser.close()
                # 关窗后再从文件剔除锁，避免页面仍打开时清锁导致前端锁死
                n = strip_exam_tab_lock_from_storage_file(path)
                if n:
                    print(f"已从会话文件清除 {n} 个作答锁，便于下次 --scrape / --auto-answer")
                return path

        browser.close()
        raise ExamAuthError(
            "超时仍未进入 exam.pku.edu.cn。\n"
            f"最后停留在: {last_url or page.url}\n"
            "常见原因：在错误的窗口登录、未完成验证码/扫码、或过早关闭了浏览器。\n"
            "请重新执行: python -m pku_exam.cli --manual-login"
        )
