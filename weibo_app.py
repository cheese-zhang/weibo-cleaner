#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微博清理工具的本地 Web 入口。

Cookie 和任务状态只保存在当前进程内。服务只监听 127.0.0.1，关闭窗口后不会把登录态写入磁盘。
"""

import argparse
import ctypes
import hashlib
import json
import os
import re
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from weibo_cleanup import (
    TZ_CN,
    WEIBO_LIST_URL,
    build_session,
    delete_one,
    fetch_posts,
    filter_posts_by_date,
    parse_cookie_string,
    parse_weibo_time,
)

ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
UI_FILE = ROOT / "weibo_ui.html"
MAX_PAGES = 500
QR_LOGIN_URL = "https://passport.weibo.com/"


class TaskCancelled(Exception):
    pass


class AppState:
    def __init__(self):
        self.lock = threading.Lock()
        self.uid = ""
        self.cookie = ""
        self.login = {"status": "signed_out", "message": "等待登录"}
        self.task: Optional[dict] = None
        self.cancel_event: Optional[threading.Event] = None

    def session_ready(self) -> bool:
        return bool(self.uid and self.cookie)

    def set_login(self, status: str, message: str, uid: str = ""):
        with self.lock:
            self.login = {"status": status, "message": message}
            if uid:
                self.uid = uid

    def set_session(self, uid: str, cookie: str):
        with self.lock:
            self.uid = uid
            self.cookie = cookie
            self.login = {"status": "ready", "message": "已登录，Cookie 仅保存在本次运行内"}

    def set_uid(self, uid: str):
        with self.lock:
            self.uid = uid

    def public_login(self) -> dict:
        with self.lock:
            return {
                **self.login,
                "uid": self.uid,
                "cookie_present": bool(self.cookie),
            }

    def public_task(self) -> dict:
        with self.lock:
            if not self.task:
                return {"status": "idle"}
            task = dict(self.task)
            task["logs"] = list(task.get("logs", []))[-100:]
            return task


STATE = AppState()


def cookie_header(cookies: list[dict]) -> str:
    return "; ".join(
        f"{item['name']}={item['value']}"
        for item in cookies
        if "weibo.com" in item.get("domain", "")
    )


def is_sina_sso_complete(page_url: str) -> bool:
    return urlparse(page_url).hostname == "my.sina.com.cn"


def uid_from_profile_url(page_url: str) -> str:
    parsed = urlparse(page_url)
    if parsed.hostname not in {"weibo.com", "www.weibo.com"}:
        return ""
    match = re.match(r"^/u/(\d+)", parsed.path)
    return match.group(1) if match else ""


def verify_cleanup_session(cookie: str, uid: str) -> tuple[bool, str]:
    """用实际清理接口验证 Cookie 和 UID；失败原因不包含 Cookie 值。"""
    try:
        session = build_session(parse_cookie_string(cookie), uid)
        response = session.get(f"{WEIBO_LIST_URL}?uid={uid}&page=1&feature=0", timeout=15)
    except ValueError:
        return False, "缺少微博清理所需的 XSRF-TOKEN"
    except Exception as exc:
        return False, f"请求异常：{type(exc).__name__}"

    if response.status_code != HTTPStatus.OK:
        return False, f"HTTP {response.status_code}"
    try:
        payload = response.json()
    except ValueError:
        return False, "微博接口返回非 JSON"
    if payload.get("ok") != 1:
        return False, f"接口 ok={payload.get('ok', 'missing')}"
    return True, ""


def visible_window_handles() -> set[int]:
    """读取当前可见顶层窗口句柄，用于把新开的登录窗口置前。"""
    if os.name != "nt":
        return set()
    handles = set()
    user32 = ctypes.windll.user32
    enum_windows = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def collect(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            handles.add(int(hwnd))
        return True

    user32.EnumWindows(enum_windows(collect), 0)
    return handles


def focus_new_windows(before: set[int]) -> bool:
    """尽力将 Playwright 刚创建的可见窗口恢复并置前。"""
    if os.name != "nt":
        return False
    user32 = ctypes.windll.user32
    candidates = visible_window_handles() - before
    focused = False
    for hwnd in candidates:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        focused = bool(user32.SetForegroundWindow(hwnd)) or focused
    return focused


def start_qr_login():
    with STATE.lock:
        if STATE.login.get("status") == "waiting":
            return False
        STATE.login = {"status": "waiting", "message": "登录窗口已打开，请在窗口中扫码"}

    thread = threading.Thread(target=_qr_login_worker, name="weibo-qr-login", daemon=True)
    thread.start()
    return True


def _qr_login_worker():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        STATE.set_login("error", "扫码登录需要 Playwright。请运行：pip install -r requirements.txt")
        return

    try:
        with sync_playwright() as playwright:
            before_windows = visible_window_handles()
            try:
                browser = playwright.chromium.launch(
                    headless=False,
                    channel="msedge",
                    ignore_default_args=["--enable-automation"],
                    args=["--disable-blink-features=AutomationControlled", "--new-window", "--start-maximized"],
                )
            except Exception as exc:
                raise RuntimeError("未找到 Microsoft Edge。请安装或更新 Edge 后重试。") from exc
            try:
                STATE.set_login("opening", "浏览器已启动，正在打开微博登录页…")
                context = browser.new_context()
                context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
                page = context.new_page()
                page.bring_to_front()
                page.goto(QR_LOGIN_URL, wait_until="commit", timeout=60_000)
                page.bring_to_front()
                if focus_new_windows(before_windows):
                    STATE.set_login("waiting", "登录窗口已置前，请使用手机微博扫一扫")
                else:
                    STATE.set_login("waiting", "登录页已打开，请切换到 Edge/Chrome 窗口扫码")
                deadline = time.monotonic() + 300
                checked_cookie = ""
                sso_return_attempted = False
                while time.monotonic() < deadline:
                    if not sso_return_attempted and is_sina_sso_complete(page.url):
                        sso_return_attempted = True
                        STATE.set_login("waiting", "已完成新浪授权，正在连接微博账号…")
                        page.goto("https://weibo.com", wait_until="commit", timeout=30_000)
                    try:
                        cookies = context.cookies()
                    except Exception:
                        break
                    uid = next(
                        (uid_from_profile_url(candidate.url) for candidate in context.pages if uid_from_profile_url(candidate.url)),
                        "",
                    )
                    cookie = cookie_header(cookies)
                    fingerprint = hashlib.sha256(f"{uid}\n{cookie}".encode("utf-8")).hexdigest()
                    if uid and cookie and fingerprint != checked_cookie:
                        checked_cookie = fingerprint
                        verified, reason = verify_cleanup_session(cookie, uid)
                        if verified:
                            STATE.set_session(uid, cookie)
                            return
                        STATE.set_login("waiting", f"已检测到微博账号（UID {uid}），但清理接口验证失败：{reason}")
                    elif {"XSRF-TOKEN", "SUB"}.issubset({item["name"] for item in cookies}):
                        STATE.set_login("waiting", "检测到访客 Cookie，正在等待微博账号页面")
                    try:
                        page.wait_for_timeout(250)
                    except Exception:
                        break
                raise RuntimeError("扫码窗口已关闭或等待超时，未能读取登录状态；请重新扫码")
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as exc:
        STATE.set_login("error", f"无法打开扫码登录窗口：{str(exc)[:180]}")


def before_month_date(month: str) -> str:
    first = datetime.strptime(month, "%Y-%m").replace(tzinfo=TZ_CN, day=1)
    return (first - timedelta(days=1)).strftime("%Y-%m-%d")


def resolve_filters(payload: dict) -> tuple[Optional[str], Optional[str], str]:
    mode = payload.get("mode", "all")
    if mode == "all":
        return None, None, "全部微博"
    if mode == "before":
        month = str(payload.get("before", ""))
        if not re.fullmatch(r"\d{4}-\d{2}", month):
            raise ValueError("请选择要清理月份之前的月份")
        return None, before_month_date(month), f"{month} 之前"
    if mode == "range":
        since = str(payload.get("since", ""))
        until = str(payload.get("until", ""))
        if not since or not until:
            raise ValueError("请选择开始日期和结束日期")
        try:
            for value in (since, until):
                datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("日期格式不正确") from exc
        if since > until:
            raise ValueError("开始日期不能晚于结束日期")
        return since, until, f"{since} 至 {until}"
    raise ValueError("不认识的清理范围")


def start_cleanup(payload: dict) -> str:
    with STATE.lock:
        if not STATE.session_ready():
            raise ValueError("请先完成扫码登录")
        if STATE.task and STATE.task.get("status") in {"running", "cancelling"}:
            raise ValueError("已有清理任务正在运行")
        uid, cookie = STATE.uid, STATE.cookie

    since, until, scope_label = resolve_filters(payload)
    try:
        delay = max(0.8, min(float(payload.get("delay", 1.5)), 10.0))
    except (TypeError, ValueError):
        delay = 1.5
    confirm = bool(payload.get("confirm", False))
    task_id = str(int(time.time() * 1000))
    cancel_event = threading.Event()
    task = {
        "id": task_id,
        "status": "running",
        "phase": "starting",
        "scope": scope_label,
        "confirm": confirm,
        "page": 0,
        "pages": 0,
        "scanned": 0,
        "total": None,
        "selected": 0,
        "success": 0,
        "failed": 0,
        "hit_limit": False,
        "logs": ["正在连接微博并读取微博列表…"],
        "error": "",
    }
    with STATE.lock:
        STATE.task = task
        STATE.cancel_event = cancel_event
    threading.Thread(
        target=_cleanup_worker,
        args=(task_id, uid, cookie, since, until, delay, confirm, cancel_event),
        name="weibo-cleanup",
        daemon=True,
    ).start()
    return task_id


def _append_log(task_id: str, message: str):
    with STATE.lock:
        if STATE.task and STATE.task.get("id") == task_id:
            STATE.task.setdefault("logs", []).append(message)


def _update_task(task_id: str, **fields):
    with STATE.lock:
        if STATE.task and STATE.task.get("id") == task_id:
            STATE.task.update(fields)


def _check_cancel(cancel_event: threading.Event):
    if cancel_event.is_set():
        raise TaskCancelled()


def _cleanup_worker(task_id, uid, cookie, since, until, delay, confirm, cancel_event):
    try:
        cookies = parse_cookie_string(cookie)
        session = build_session(cookies, uid)

        def progress(info: dict):
            _check_cancel(cancel_event)
            if info.get("phase") == "scan":
                _update_task(
                    task_id,
                    phase="scanning",
                    page=info["page"],
                    pages=info["pages"],
                    scanned=info["count"],
                    total=info.get("total"),
                    hit_limit=info.get("hit_limit", False),
                    partial=bool(info.get("total") and info["count"] < info["total"]),
                )
                if info["page"] == 1 or info["page"] % 10 == 0:
                    _append_log(task_id, f"已读取第 {info['page']} 页，累计 {info['count']} 条")

        posts = fetch_posts(
            session,
            uid,
            max_pages=MAX_PAGES,
            page_delay=1.0,
            on_progress=progress,
        )
        _check_cancel(cancel_event)
        selected = filter_posts_by_date(posts, since, until)
        _update_task(
            task_id,
            phase="preview" if not confirm else "deleting",
            scanned=len(posts),
            selected=len(selected),
        )
        _append_log(task_id, f"扫描完成：共读取 {len(posts)} 条，符合范围 {len(selected)} 条")
        if not selected:
            _update_task(task_id, status="completed", phase="done")
            return
        if not confirm:
            _update_task(task_id, status="completed", phase="preview")
            return

        xsrf_token = cookies["XSRF-TOKEN"]
        for index, post in enumerate(selected, 1):
            _check_cancel(cancel_event)
            post_id = post.get("idstr") or str(post.get("id") or "?")
            try:
                delete_one(session, post, xsrf_token)
                _update_task(task_id, current=index)
                with STATE.lock:
                    if STATE.task:
                        STATE.task["success"] = STATE.task.get("success", 0) + 1
                if index == 1 or index % 10 == 0 or index == len(selected):
                    _append_log(task_id, f"已删除 {index}/{len(selected)} 条")
            except Exception as exc:
                with STATE.lock:
                    if STATE.task:
                        STATE.task["failed"] = STATE.task.get("failed", 0) + 1
                _append_log(task_id, f"第 {index} 条失败（{post_id}）：{str(exc)[:120]}")
            if index < len(selected):
                time.sleep(delay)
        _update_task(task_id, status="completed", phase="done")
    except TaskCancelled:
        _update_task(task_id, status="cancelled", phase="cancelled")
        _append_log(task_id, "任务已停止，未开始的微博不会继续处理")
    except Exception as exc:
        _update_task(task_id, status="error", phase="error", error=str(exc)[:500])
        _append_log(task_id, f"任务失败：{str(exc)[:180]}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def _send(self, body, status=HTTPStatus.OK, content_type="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send(UI_FILE.read_bytes(), content_type="text/html; charset=utf-8")
        elif path == "/api/login/status":
            self._send(STATE.public_login())
        elif path == "/api/task/status":
            self._send(STATE.public_task())
        else:
            self._send({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            payload = self._json()
            if path == "/api/login/start":
                started = start_qr_login()
                self._send({"ok": True, "started": started})
            elif path == "/api/login/manual":
                uid = str(payload.get("uid", "")).strip()
                cookie = str(payload.get("cookie", "")).strip()
                if not uid.isdigit():
                    raise ValueError("UID 应为微博个人主页中的数字")
                parse_cookie_string(cookie)
                STATE.set_session(uid, cookie)
                self._send({"ok": True})
            elif path == "/api/login/uid":
                uid = str(payload.get("uid", "")).strip()
                if not uid.isdigit():
                    raise ValueError("UID 应为微博个人主页中的数字")
                with STATE.lock:
                    if not STATE.cookie:
                        raise ValueError("登录态已失效，请重新扫码")
                STATE.set_uid(uid)
                self._send({"ok": True})
            elif path == "/api/task/start":
                self._send({"ok": True, "task_id": start_cleanup(payload)})
            elif path == "/api/task/cancel":
                with STATE.lock:
                    if STATE.cancel_event:
                        STATE.cancel_event.set()
                    if STATE.task and STATE.task.get("status") == "running":
                        STATE.task["status"] = "cancelling"
                self._send({"ok": True})
            else:
                self._send({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)


class LocalServer(ThreadingHTTPServer):
    """只允许一个本地服务实例，防止浏览器随机连到旧进程。"""

    allow_reuse_address = False
    allow_reuse_port = False


def main():
    parser = argparse.ArgumentParser(description="微博清理工具本地 Web 界面")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()
    try:
        server = LocalServer((args.host, args.port), Handler)
    except OSError as exc:
        print(f"无法启动：端口 {args.port} 已被其他程序占用。请先关闭旧的微博清理工具窗口。", file=sys.stderr)
        print(f"详细信息：{exc}", file=sys.stderr)
        sys.exit(1)
    url = f"http://{args.host}:{args.port}"
    print(f"微博清理工具已启动：{url}")
    print("Cookie 只保存在当前进程内；按 Ctrl+C 退出并清除登录态。")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
