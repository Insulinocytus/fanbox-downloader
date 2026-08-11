#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fanbox 下载器：通过环境变量配置，并按 cron 常驻运行。"""
import atexit
import json
import logging
import os
import re
import signal
import time
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cloakbrowser import launch_context
from croniter import croniter
from curl_cffi import requests

from creator_sync import (
    AuthenticationError,
    CloudflareBlocked,
    CreatorSync,
    FanboxTimeout,
    PostDetail,
    RetryableFanboxError,
)

DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_CRON = "0 */6 * * *"

_shutdown_requested = False
logger = logging.getLogger(__name__)

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
        return {
            status: response.status,
            text: await response.text(),
            headers: Object.fromEntries(response.headers.entries())
        };
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
    errors = []

    cookie = env.get("FANBOX_COOKIE", "").strip()
    if not cookie:
        errors.append("FANBOX_COOKIE 必须设置且不能为空")

    creators = [creator.strip() for creator in env.get("FANBOX_CREATORS", "").split(",")
                if creator.strip()]
    if not creators:
        errors.append("FANBOX_CREATORS 必须设置至少一个作者 ID")

    download_directory = env.get("FANBOX_DOWNLOAD_DIRECTORY", "/data/downloads").strip()
    if not download_directory:
        errors.append("FANBOX_DOWNLOAD_DIRECTORY 不能为空")
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
            errors.append(f"{name} 必须是非负数字")
            return default
        if value < 0:
            errors.append(f"{name} 必须是非负数字")
            return default
        return value

    file_delay = read_delay("FANBOX_FILE_DELAY", 0)
    post_delay = read_delay("FANBOX_POST_DELAY", 10)

    timezone_name = env.get("FANBOX_TIMEZONE", DEFAULT_TIMEZONE).strip()
    try:
        timezone = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError):
        errors.append("FANBOX_TIMEZONE 无效")
        timezone = ZoneInfo(DEFAULT_TIMEZONE)

    cron_expression = env.get("FANBOX_CRON", DEFAULT_CRON).strip()
    if not cron_expression or not croniter.is_valid(cron_expression):
        errors.append("FANBOX_CRON 必须是有效的五段式 cron 表达式")

    run_on_start = env.get("FANBOX_RUN_ON_START", "false").strip().lower()
    if run_on_start not in {"true", "false"}:
        errors.append("FANBOX_RUN_ON_START 必须是 true 或 false")

    if errors:
        logger.error("配置无效:\n%s", "\n".join(f"- {error}" for error in errors))
        raise SystemExit(1)

    return {
        "cookie": cookie,
        "creators": creators,
        "download_directory": download_directory,
        "state_directory": state_directory,
        "file_delay": file_delay,
        "post_delay": post_delay,
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
        logger.info("正在启动 CloakBrowser")
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
        logger.info("CloakBrowser 已登录 Fanbox，开始后台 API 请求")
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


def _response_retry_after(headers):
    headers = headers or {}
    return headers.get("retry-after") or headers.get("Retry-After")


def _raise_file_access_error(response, operation):
    status = response.status_code
    if status == 403:
        response_text = str(getattr(response, "text", "") or "")
        if "block_ip" in response_text or "challenge" in response_text.lower():
            raise CloudflareBlocked(f"{operation}时被 Cloudflare 拦截")
    if status in (401, 403):
        raise AuthenticationError("登录失效或账号未授权，请更新 FANBOX_COOKIE")
    if status == 429:
        retry_after = _response_retry_after(getattr(response, "headers", None))
        raise RetryableFanboxError(
            f"{operation}被限流", retry_after=retry_after
        )
    if status == 408 or status >= 500:
        raise RetryableFanboxError(f"{operation} HTTP {status}")


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
        raise AuthenticationError("登录失效或账号未授权，请更新 FANBOX_COOKIE")
    if status == 429:
        retry_after = _response_retry_after(result.get("headers"))
        raise RetryableFanboxError(
            f"Fanbox API HTTP 429: {response_text[:200]}",
            retry_after=retry_after,
        )
    if status == 408 or status >= 500:
        raise RetryableFanboxError(
            f"Fanbox API HTTP {status}: {response_text[:200]}"
        )
    if status != 200:
        raise RuntimeError(
            f"Fanbox API HTTP {status}: {response_text[:200]}"
        )
    try:
        return json.loads(response_text)["body"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise RetryableFanboxError("Fanbox API response is not valid JSON") from exc


class Fanbox:
    """每次调用只执行一个 Fanbox 领域操作的生产适配器。"""

    @staticmethod
    def _required_list(body, field):
        if not isinstance(body, dict) or not isinstance(body.get(field), list):
            raise RetryableFanboxError(f"Fanbox API 响应缺少有效的 {field} 列表")
        return body[field]

    @staticmethod
    def _is_restricted(post):
        value = post.get("isRestricted", False)
        if not isinstance(value, bool):
            raise RetryableFanboxError(
                "Fanbox API 响应包含无效的 isRestricted 标志"
            )
        return value

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
        for post in posts:
            if not isinstance(post, dict) or not post.get("id"):
                raise RetryableFanboxError("Fanbox API 响应包含无效的帖子条目")
            self._is_restricted(post)
        return posts

    def post_detail(self, post_id):
        try:
            post = _api_get_once(
                f"https://api.fanbox.cc/post.info?postId={post_id}"
            )["post"]
            is_restricted = self._is_restricted(post)
            return PostDetail(
                post.get("title") or post_id,
                [] if is_restricted else extract_post_files(post),
                is_restricted,
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise RetryableFanboxError(
                "Fanbox API post response is malformed"
            ) from exc

    def download_file(self, url, path):
        if os.path.exists(path):
            return False
        try:
            response = sess().get(url, impersonate=IMPERSONATE, stream=True, timeout=60)
        except Exception as exc:
            raise RetryableFanboxError(str(exc)) from exc
        _raise_file_access_error(response, "下载文件")
        response.raise_for_status()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fp:
            for chunk in response.iter_content(1 << 16):
                fp.write(chunk)
        return True

    def file_size(self, url):
        try:
            response = sess().head(
                url,
                impersonate=IMPERSONATE,
                headers={"Accept-Encoding": "identity"},
                allow_redirects=True,
                timeout=60,
            )
        except Exception as exc:
            raise RetryableFanboxError(str(exc)) from exc
        _raise_file_access_error(response, "查询文件大小")
        if response.status_code != 200:
            return None
        headers = getattr(response, "headers", None) or {}
        value = headers.get("content-length") or headers.get("Content-Length")
        value = str(value).strip() if value is not None else ""
        if not value.isascii() or not value.isdecimal():
            return None
        try:
            size = int(value)
        except ValueError:
            return None
        return size if size <= (1 << 63) - 1 else None

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
        logger.info("下一次执行时间: %s", next_run.isoformat())
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
    logger.info("收到信号 %s，正在停止调度", signum)
    close_browser()
    raise SystemExit(0)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = configure()
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)
    try:
        run_scheduler(config)
    finally:
        close_browser()


if __name__ == "__main__":
    main()
