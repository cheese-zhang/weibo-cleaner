#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微博批量清理脚本

功能：
- 列出当前登录用户可访问的全部微博（默认仅原创，feature=0）。
- 按日期区间筛选（--since / --until）。
- 默认只进行 dry-run，预览将要删除的微博；加 --confirm 才真正删除。

用法示例：
    python weibo_cleanup.py --uid 7910373640 --cookie "XSRF-TOKEN=xxx; SUBP=yyy; ..."
    python weibo_cleanup.py --uid 7910373640 --cookie "..." --since 2025-01-01 --until 2025-04-30
    python weibo_cleanup.py --uid 7910373640 --cookie "..." --confirm
"""

import argparse
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from urllib.parse import unquote

import requests

WEIBO_LIST_URL = "https://weibo.com/ajax/statuses/mymblog"
WEIBO_DESTROY_URL = "https://weibo.com/ajax/statuses/destroy"

# 微博接口使用东八区
TZ_CN = timezone(timedelta(hours=8))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)


def parse_weibo_time(s: str) -> datetime:
    """解析微博 created_at，例如 'Sat Apr 12 19:24:15 +0800 2025'。"""
    return datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")


def parse_cookie_string(cookie_str: str) -> dict:
    """把浏览器里复制出来的 Cookie 字符串转成字典。"""
    cookies = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        cookies[key.strip()] = unquote(value.strip())
    if "XSRF-TOKEN" not in cookies:
        raise ValueError(
            "Cookie 中缺少 XSRF-TOKEN。请从已登录 weibo.com 的浏览器中复制完整 Cookie。"
        )
    return cookies


def build_session(cookies: dict, uid: str) -> requests.Session:
    """构造带公共请求头的 requests.Session。"""
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": UA,
            "Referer": f"https://weibo.com/u/{uid}",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "X-Requested-With": "XMLHttpRequest",
        }
    )
    session.cookies.update(cookies)
    return session


def fetch_posts(
    session: requests.Session,
    uid: str,
    feature: int = 0,
    max_pages: int = 500,
    page_delay: Optional[float] = None,
    on_progress: Optional[Callable[[dict], None]] = None,
):
    """分页拉取用户的微博列表。"""
    posts = []
    page = 1
    total = None

    print(f"开始拉取用户 {uid} 的微博列表（feature={feature}）...")
    seen_ids = set()
    while page <= max_pages:
        url = f"{WEIBO_LIST_URL}?uid={uid}&page={page}&feature={feature}"
        try:
            resp = session.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            raise RuntimeError(f"第 {page} 页请求失败：{e}")
        except ValueError as e:
            raise RuntimeError(f"第 {page} 页返回非 JSON：{e}")

        if data.get("ok") != 1:
            # ok 为 -100 一般表示未登录或 Cookie 过期
            code = data.get("ok")
            msg = data.get("msg") or data.get("message") or ""
            raise RuntimeError(f"接口返回异常（ok={code}）：{msg}。请检查 Cookie 是否有效。")

        data_data = data.get("data") or {}
        lst = data_data.get("list") or []
        if not lst:
            break

        new_posts = []
        for post in lst:
            post_id = post.get("idstr") or str(post.get("id") or "")
            if post_id and post_id in seen_ids:
                continue
            if post_id:
                seen_ids.add(post_id)
            new_posts.append(post)
        posts.extend(new_posts)
        if total is None:
            total = data_data.get("total")

        print(f"  第 {page:3d} 页：本页 {len(lst):2d} 条，累计 {len(posts):4d} 条" + (f" / 约 {total}" if total else ""))
        if on_progress:
            on_progress(
                {
                    "phase": "scan",
                    "page": page,
                    "pages": page,
                    "page_count": len(lst),
                    "new_count": len(new_posts),
                    "count": len(posts),
                    "total": total,
                    "max_pages": max_pages,
                    "hit_limit": page == max_pages,
                }
            )

        # 同一页被重复返回时继续请求只会制造死循环/重复数据。
        if page > 1 and not new_posts:
            break

        # 微博反爬不严格，但保险起见每页休息 1~2 秒
        time.sleep((page_delay if page_delay is not None else 1.0) + (page % 2) * 0.5)
        page += 1

    if on_progress:
        on_progress(
            {
                "phase": "scan_complete",
                "page": min(page, max_pages),
                "pages": min(page, max_pages),
                "count": len(posts),
                "total": total,
                "max_pages": max_pages,
                "hit_limit": page > max_pages,
            }
        )
    print(f"共拉取到 {len(posts)} 条微博。")
    return posts


def filter_posts_by_date(posts: list, since: Optional[str], until: Optional[str]) -> list:
    """按创建日期筛选微博。"""
    selected = posts[:]

    if since:
        since_dt = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=TZ_CN)
        selected = [p for p in selected if parse_weibo_time(p["created_at"]) >= since_dt]

    if until:
        # until 当天 23:59:59（东八区）
        until_dt = (
            datetime.strptime(until, "%Y-%m-%d").replace(tzinfo=TZ_CN)
            + timedelta(days=1)
            - timedelta(seconds=1)
        )
        selected = [p for p in selected if parse_weibo_time(p["created_at"]) <= until_dt]

    return selected


def delete_one(session: requests.Session, post: dict, xsrf_token: str) -> bool:
    """调用 /ajax/statuses/destroy 删除单条微博。"""
    idstr = post.get("idstr") or str(post.get("id"))
    if not idstr:
        raise ValueError(f"微博对象缺少 id/idstr：{post}")

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "X-Xsrf-Token": xsrf_token,
    }
    resp = session.post(WEIBO_DESTROY_URL, data={"id": idstr}, headers=headers, timeout=30)
    resp.raise_for_status()
    result = resp.json()

    if result.get("ok") != 1:
        msg = result.get("msg") or result.get("message") or str(result)
        raise RuntimeError(f"删除失败：{msg}")
    return True


def preview_posts(posts: list, limit: int = 20):
    """在终端列出将要操作的微博。"""
    print(f"\n===== 符合条件共 {len(posts)} 条 =====")
    for i, p in enumerate(posts[:limit], 1):
        dt = parse_weibo_time(p["created_at"]).strftime("%Y-%m-%d %H:%M")
        text = (p.get("text_raw") or p.get("text") or "").replace("\n", " ").replace("\r", " ")
        if len(text) > 60:
            text = text[:60] + "…"
        print(f"{i:3d}. [{dt}] {text}")
    if len(posts) > limit:
        print(f"... 还有 {len(posts) - limit} 条未显示 ...")


def main():
    # Windows 终端默认可能为 GBK，强制使用 UTF-8 输出，避免打印微博内容时崩溃
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="批量清理自己发的微博。默认 dry-run，不会真正删除。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
日期说明：
  --since YYYY-MM-DD   只删除该日期 00:00（东八区）及之后发布的微博
  --until YYYY-MM-DD   只删除该日期 23:59（东八区）及之前发布的微博
  两个参数可同时使用，实现时间段清理。

Cookie 获取：
  1. 在 Chrome 中打开 https://weibo.com/u/<你的UID> 并确保已登录。
  2. F12 → Network（网络）→ 刷新页面。
  3. 任意请求右键 → Copy → Copy as cURL(bash)，把 Cookie: 后的字符串粘过来。
  4. 或者直接到 Application（应用）→ Cookies → https://weibo.com 复制整行 Cookie。
        """,
    )
    parser.add_argument("--uid", required=True, help="你的微博 UID（个人主页URL里的数字）")
    cookie_group = parser.add_mutually_exclusive_group(required=True)
    cookie_group.add_argument("--cookie", help='登录 Cookie 字符串，必须包含 XSRF-TOKEN')
    cookie_group.add_argument("--cookie-file", help='存放 Cookie 字符串的文件路径（避免 Cookie 出现在命令行历史）')
    parser.add_argument("--since", metavar="YYYY-MM-DD", help="起始日期（包含）")
    parser.add_argument("--until", metavar="YYYY-MM-DD", help="结束日期（包含）")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="确认删除。不加此参数时只预览，不会删除任何微博。",
    )
    parser.add_argument(
        "--feature",
        type=int,
        default=0,
        help="微博类型筛选：0=全部（默认，实测为原创），1=原创，2=图片，3=视频，4=音乐",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.5,
        help="两次删除之间的间隔秒数（默认 1.5，过快可能触发风控）",
    )
    args = parser.parse_args()

    # 校验日期格式
    for d in (args.since, args.until):
        if d:
            try:
                datetime.strptime(d, "%Y-%m-%d")
            except ValueError:
                parser.error(f"日期格式错误：{d}，应为 YYYY-MM-DD")

    if args.cookie_file:
        with open(args.cookie_file, "r", encoding="utf-8") as f:
            cookie_str = f.read().strip()
    else:
        cookie_str = args.cookie

    cookies = parse_cookie_string(cookie_str)
    session = build_session(cookies, args.uid)

    try:
        posts = fetch_posts(session, args.uid, feature=args.feature)
    except RuntimeError as e:
        print(f"错误：{e}", file=sys.stderr)
        sys.exit(1)

    selected = filter_posts_by_date(posts, args.since, args.until)

    if not selected:
        print("\n没有符合筛选条件的微博，无需操作。")
        return

    preview_posts(selected)

    if not args.confirm:
        print(
            "\n这是预览模式（dry-run），没有删除任何微博。"
            "如果确认要删除以上微博，请重新运行并加上 --confirm。"
        )
        return

    # 真正删除
    print(f"\n开始删除，共 {len(selected)} 条，间隔 {args.delay}s ...")
    xsrf_token = cookies["XSRF-TOKEN"]
    success = 0
    failed = 0
    for i, post in enumerate(selected, 1):
        idstr = post.get("idstr") or str(post.get("id"))
        dt = parse_weibo_time(post["created_at"]).strftime("%Y-%m-%d %H:%M")
        text = (post.get("text_raw") or "").replace("\n", " ")[:30]
        try:
            delete_one(session, post, xsrf_token)
            success += 1
            print(f"[{i}/{len(selected)}] 已删除 {idstr} [{dt}] {text}")
        except Exception as e:
            failed += 1
            print(f"[{i}/{len(selected)}] 删除失败 {idstr} [{dt}]：{e}", file=sys.stderr)

        if i < len(selected):
            time.sleep(args.delay)

    print(f"\n完成：成功 {success} 条，失败 {failed} 条。")


if __name__ == "__main__":
    main()
