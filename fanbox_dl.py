#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fanbox 下载器：通过环境变量配置，并按 cron 常驻运行。"""
import atexit
import json
import os
import re
import signal
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cloakbrowser import launch_context
from croniter import croniter
from curl_cffi import requests

from creator_sync import (
    CloudflareBlocked,
    CreatorSync,
    FanboxTimeout,
    RetryableFanboxError,
)

DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_CRON = "0 */6 * * *"

_shutdown_requested = False

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.fanbox.cc",
    "Referer": "https://www.fanbox.cc/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
}
IMPERSONATE = "chrome"  # 浏览器指纹,fanbox 的 Cloudflare 只放行真浏览器
BROWSER_FETCH_SCRIPT = """
async ({url}) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 30000);
    try {
        const response = await fetch(url, {
            credentials: "include",
            headers: {"Accept": "application/json, text/plain, */*"},
            signal: controller.signal
        });
        return {status: response.status, text: await response.text()};
    } catch (error) {
        return {status: 0, text: String(error)};
    } finally {
        clearTimeout(timer);
    }
}
"""

_sess = None
_browser_context = None
_browser_page = None
_cookie_value = None

def read_config(env=None):
    """Read and validate the complete runtime configuration from the environment."""
    env = os.environ if env is None else env

    cookie = env.get("FANBOX_COOKIE", "").strip()
    if not cookie:
        sys.exit("FANBOX_COOKIE 必须设置且不能为空")

    creators = [creator.strip() for creator in env.get("FANBOX_CREATORS", "").split(",")
                if creator.strip()]
    if not creators:
        sys.exit("FANBOX_CREATORS 必须设置至少一个作者 ID")

    download_directory = env.get("FANBOX_DOWNLOAD_DIRECTORY", "/data/downloads").strip()
    if not download_directory:
        sys.exit("FANBOX_DOWNLOAD_DIRECTORY 不能为空")
    if not os.path.isabs(download_directory) and not download_directory.startswith("/"):
        download_directory = os.path.abspath(download_directory)

    state_directory = env.get("FANBOX_STATE_DIRECTORY", "").strip()
    if not state_directory:
        state_directory = download_directory
    elif not os.path.isabs(state_directory) and not state_directory.startswith("/"):
        state_directory = os.path.abspath(state_directory)

    def read_delay(name, default):
        raw = env.get(name, str(default))
        try:
            value = float(raw)
        except (TypeError, ValueError):
            sys.exit(f"{name} 必须是非负数字")
        if value < 0:
            sys.exit(f"{name} 必须是非负数字")
        return value

    timezone_name = env.get("FANBOX_TIMEZONE", DEFAULT_TIMEZONE).strip()
    try:
        timezone = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError):
        sys.exit(f"FANBOX_TIMEZONE 无效: {timezone_name}")

    cron_expression = env.get("FANBOX_CRON", DEFAULT_CRON).strip()
    if not cron_expression or not croniter.is_valid(cron_expression):
        sys.exit("FANBOX_CRON 必须是有效的五段式 cron 表达式")

    run_on_start = env.get("FANBOX_RUN_ON_START", "false").strip().lower()
    if run_on_start not in {"true", "false"}:
        sys.exit("FANBOX_RUN_ON_START 必须是 true 或 false")

    return {
        "cookie": cookie,
        "creators": creators,
        "download_directory": download_directory,
        "state_directory": state_directory,
        "file_delay": read_delay("FANBOX_FILE_DELAY", 0),
        "post_delay": read_delay("FANBOX_POST_DELAY", 10),
        "timezone": timezone,
        "cron": cron_expression,
        "run_on_start": run_on_start == "true",
    }

def configure(env=None):
    global _cookie_value
    config = read_config(env)
    _cookie_value = config["cookie"]
    return config


def sess():
    global _sess, _cookie_value
    if _sess is None:
        if _cookie_value is None:
            configure()
        _sess = requests.Session(impersonate=IMPERSONATE)
        _sess.cookies.set("FANBOXSESSID", _cookie_value, domain=".fanbox.cc")
    return _sess

def browser_fetch(url):
    global _browser_context, _browser_page
    if _browser_page is None:
        if _cookie_value is None:
            configure()
        print("正在启动 CloakBrowser...", flush=True)
        _browser_context = launch_context(
            headless=True,
            humanize=True,
        )
        if _cookie_value:
            _browser_context.add_cookies([{
                "name": "FANBOXSESSID",
                "value": _cookie_value,
                "domain": ".fanbox.cc",
                "path": "/",
                "secure": True,
                "httpOnly": True,
            }])
        _browser_page = (_browser_context.pages[0] if _browser_context.pages
                         else _browser_context.new_page())
        _browser_page.goto(
            "https://www.fanbox.cc/",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        print("CloakBrowser 已登录 Fanbox，开始后台 API 请求。", flush=True)
    return _browser_page.evaluate(BROWSER_FETCH_SCRIPT, {"url": url})

def close_browser():
    global _browser_context, _browser_page
    if _browser_context is not None:
        try:
            _browser_context.close()
        except Exception:
            pass
        _browser_context = _browser_page = None

atexit.register(close_browser)

def extract_post_files(post):
    """按 gallery-dl 的 1-6 顺序提取帖子文件 URL。"""
    content = post.get("body") or {}
    files = []  # (url, 建议文件名)

    # 1. 帖子封面
    cover = post.get("coverImageUrl")
    if cover:
        cover = re.sub(r"/c/[0-9a-z_]+/", "/", cover)
        files.append((cover, ""))

    # 2. 正文 HTML 中的 Fanbox 图片/附件链接
    if content.get("html"):
        for href in re.findall(r'href="([^"]+)"', content["html"]):
            if "fanbox.pixiv.net/images/entry" in href or "downloads.fanbox.cc" in href:
                files.append((href, ""))
        for src in re.findall(r'data-src-original="([^"]+)"', content["html"]):
            files.append((src, ""))

    # 3-4. 正文图片:当前格式 images,旧格式 imageMap
    for group in ("images", "imageMap"):
        values = content.get(group) or ()
        if isinstance(values, dict):
            values = values.values()
        for image in values:
            files.append((image.get("originalUrl") or image.get("url"), ""))

    # 5-6. 正文附件:当前格式 files,旧格式 fileMap
    for group in ("files", "fileMap"):
        values = content.get(group) or ()
        if isinstance(values, dict):
            values = values.values()
        for file in values:
            files.append((file.get("url"), file.get("name", "")))

    # 去重,去掉空值
    seen, out = set(), []
    for url, name in files:
        if url and url not in seen:
            seen.add(url)
            out.append((url, name))
    return out

def _api_get_once(url):
    try:
        result = browser_fetch(url)
    except Exception as exc:
        raise RetryableFanboxError(str(exc)) from exc

    status = result.get("status", 0)
    response_text = result.get("text", "")
    if status == 0:
        raise FanboxTimeout(f"浏览器请求失败: {response_text}")
    if status == 403 and (
            "block_ip" in response_text or
            "challenge" in response_text.lower()):
        raise CloudflareBlocked("Fanbox 元数据请求被 Cloudflare 拦截")
    if status in (401, 403):
        sys.exit("登录失效(cookie 无效/过期)。请更新 FANBOX_COOKIE")
    if status != 200:
        raise RuntimeError(f"Fanbox API 返回 HTTP {status}: {response_text[:200]}")
    try:
        return json.loads(response_text)["body"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise RuntimeError("Fanbox API 返回内容不是有效 JSON") from exc


class Fanbox:
    """每次调用只执行一个 Fanbox 领域操作的生产适配器。"""

    @staticmethod
    def _required_list(body, field):
        if not isinstance(body, dict) or not isinstance(body.get(field), list):
            raise RetryableFanboxError(f"Fanbox API 响应缺少有效的 {field} 列表")
        return body[field]

    def author_pages(self, creator):
        body = _api_get_once(
            "https://api.fanbox.cc/post.paginateCreator?creatorId=" + creator
        )
        pages = self._required_list(body, "pageUrls")
        if any(not isinstance(page, str) or not page for page in pages):
            raise RetryableFanboxError("Fanbox API 响应包含无效的分页 URL")
        return pages

    def page_posts(self, page_url):
        posts = self._required_list(_api_get_once(page_url), "posts")
        if any(
            not isinstance(post, dict) or not post.get("id")
            for post in posts
        ):
            raise RetryableFanboxError("Fanbox API 响应包含无效的帖子条目")
        return posts

    def post_detail(self, post_id):
        post = _api_get_once(f"https://api.fanbox.cc/post.info?postId={post_id}")["post"]
        return post.get("title") or post_id, extract_post_files(post)

    def download_file(self, url, path):
        if os.path.exists(path):
            return False
        response = sess().get(url, impersonate=IMPERSONATE, stream=True, timeout=60)
        if response.status_code == 403 and "block_ip" in response.text:
            raise CloudflareBlocked("下载文件时被 Cloudflare 拦截")
        response.raise_for_status()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fp:
            for chunk in response.iter_content(1 << 16):
                fp.write(chunk)
        return True

    def close(self):
        close_browser()

def next_scheduled_time(cron_expression, timezone, now=None):
    now = now or datetime.now(timezone)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone)
    else:
        now = now.astimezone(timezone)
    return croniter(cron_expression, now).get_next(datetime)


def run_scheduler(config, sync=None, now_fn=None, sleep_fn=time.sleep):
    global _shutdown_requested
    if sync is None:
        sync = CreatorSync(config, Fanbox())
    now_fn = now_fn or (lambda: datetime.now(config["timezone"]))

    if config["run_on_start"]:
        sync.run()

    while not _shutdown_requested:
        next_run = next_scheduled_time(config["cron"], config["timezone"], now_fn())
        print(f"下一次执行时间: {next_run.isoformat()}", flush=True)
        while not _shutdown_requested:
            remaining = (next_run - now_fn()).total_seconds()
            if remaining <= 0:
                break
            sleep_fn(min(remaining, 60))
        if not _shutdown_requested:
            sync.run()


def handle_shutdown(signum, _frame):
    global _shutdown_requested
    _shutdown_requested = True
    print(f"收到信号 {signum}，正在停止调度。", flush=True)
    close_browser()


def main():
    config = configure()
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)
    try:
        run_scheduler(config)
    finally:
        close_browser()


if __name__ == "__main__":
    main()
