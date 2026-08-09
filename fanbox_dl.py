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

OUT = "/data/downloads"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_CRON = "0 */6 * * *"
STATE_FILE = ".fanbox-state.json"
STATE_VERSION = 1
POST_STATES = {"downloaded", "empty", "restricted"}

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
RETRY = 5      # 初次请求 + 4 次 Cloudflare 重试
DELAY = 1.0    # API 请求之间间隔(秒)
FILE_DELAY = 0.0
POST_DELAY = 5.0
CLOUDFLARE_WAITS = (30, 60, 120, 300)
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

class CloudflareBlocked(RuntimeError):
    pass

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
        "file_delay": read_delay("FANBOX_FILE_DELAY", 0),
        "post_delay": read_delay("FANBOX_POST_DELAY", 10),
        "timezone": timezone,
        "cron": cron_expression,
        "run_on_start": run_on_start == "true",
    }

def configure(env=None):
    global _cookie_value, OUT, FILE_DELAY, POST_DELAY
    config = read_config(env)
    _cookie_value = config["cookie"]
    OUT = config["download_directory"]
    FILE_DELAY = config["file_delay"]
    POST_DELAY = config["post_delay"]
    return config


def empty_state():
    return {"version": STATE_VERSION, "creators": {}}


def valid_state(state):
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        return False
    creators = state.get("creators")
    if not isinstance(creators, dict):
        return False
    for creator in creators.values():
        if (not isinstance(creator, dict) or
                not isinstance(creator.get("initialized"), bool) or
                not isinstance(creator.get("posts"), dict) or
                any(not isinstance(pid, str) or not isinstance(status, str) or
                    status not in POST_STATES
                    for pid, status in creator["posts"].items())):
            return False
    return True


def load_state():
    try:
        with open(os.path.join(OUT, STATE_FILE), encoding="utf-8") as fp:
            state = json.load(fp)
        if not valid_state(state):
            raise ValueError("unsupported state format")
        return state, True
    except FileNotFoundError:
        return empty_state(), False
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"警告: 状态文件无法读取,将重建: {exc}", flush=True)
        return empty_state(), False


def save_state(state):
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, STATE_FILE)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as fp:
        json.dump(state, fp, ensure_ascii=False, sort_keys=True)
    os.replace(temporary, path)


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

def wait_with_progress(seconds):
    remaining = int(seconds)
    while remaining > 0:
        print(f"  Cloudflare 冷却中,剩余约 {remaining} 秒", flush=True)
        wait = min(10, remaining)
        time.sleep(wait)
        remaining -= wait

def api_get(url, retry=RETRY):
    for attempt in range(1, retry + 1):
        if attempt > 1:
            print(f"  冷却结束,正在发起第 {attempt}/{retry} 次请求...", flush=True)
        try:
            result = browser_fetch(url)
        except Exception as exc:
            if attempt < retry:
                wait = 2 ** attempt
                print(f"  浏览器请求错误({exc}),{wait}s 后重试({attempt}/{retry})",
                      flush=True)
                time.sleep(wait)
                continue
            raise

        status = result.get("status", 0)
        response_text = result.get("text", "")
        if status == 0:
            if attempt < retry:
                wait = 2 ** attempt
                print(f"  浏览器请求超时,{wait}s 后重试({attempt}/{retry})",
                      flush=True)
                time.sleep(wait)
                continue
            raise RuntimeError(f"浏览器请求失败: {response_text}")
        if status == 403 and (
                "block_ip" in response_text or
                "challenge" in response_text.lower()):
            if attempt < retry:
                wait = CLOUDFLARE_WAITS[min(attempt - 1, len(CLOUDFLARE_WAITS) - 1)]
                print(f"  Cloudflare 临时拦截,{wait}s 后重试(下一次 {attempt + 1}/{retry})",
                      flush=True)
                wait_with_progress(wait)
                continue
            raise CloudflareBlocked(
                f"被 Cloudflare 拦截,重试 {retry - 1} 次仍失败。"
                "已安全停止,稍后重新运行即可从断点继续。"
            )
        if status in (401, 403):
            sys.exit("登录失效(cookie 无效/过期)。请更新 FANBOX_COOKIE")
        if status != 200:
            raise RuntimeError(f"Fanbox API 返回 HTTP {status}: {response_text[:200]}")
        try:
            return json.loads(response_text)["body"]
        except (json.JSONDecodeError, KeyError) as exc:
            raise RuntimeError("Fanbox API 返回内容不是有效 JSON") from exc
    raise RuntimeError("unreachable")

def post_urls(creator):
    """作者的全部帖子页 URL"""
    body = api_get("https://api.fanbox.cc/post.paginateCreator?creatorId=" + creator)
    time.sleep(DELAY)
    return body.get("pageUrls", [])

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

def post_files(post_id):
    """返回(帖子标题, 帖子文件 URL 列表)。"""
    body = api_get(f"https://api.fanbox.cc/post.info?postId={post_id}")
    time.sleep(DELAY)
    post = body["post"]
    return post.get("title") or post_id, extract_post_files(post)

def post_directory(creator, post_id, title):
    title = sanitize(str(title)) or post_id
    return os.path.join(OUT, creator, f"{post_id}-{title}")


def creator_state(state, creator):
    return state["creators"].setdefault(
        creator, {"initialized": False, "posts": {}}
    )


def existing_post_ids(creator):
    try:
        return {
            match.group(1)
            for entry in os.scandir(os.path.join(OUT, creator))
            if entry.is_dir() and (match := re.match(r"^(\d+)-", entry.name))
        }
    except FileNotFoundError:
        return set()


def migrate_existing_posts(state, creators):
    for creator in creators:
        posts = creator_state(state, creator)["posts"]
        for post_id in existing_post_ids(creator):
            posts[post_id] = "downloaded"


def missing_downloaded_posts(state, creator):
    existing = existing_post_ids(creator)
    posts = creator_state(state, creator)["posts"]
    return sorted(pid for pid, status in posts.items()
                  if status == "downloaded" and pid not in existing)


def post_downloaded(creator, post):
    title = post.get("title") or post["id"]
    return os.path.isdir(post_directory(creator, post["id"], title))

def dl(url, path):
    if os.path.exists(path):
        return False
    r = sess().get(url, impersonate=IMPERSONATE, stream=True, timeout=60)
    if r.status_code == 403 and "block_ip" in r.text:
        raise CloudflareBlocked("下载文件时被 Cloudflare 拦截")
    r.raise_for_status()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fp:
        for chunk in r.iter_content(1 << 16):
            fp.write(chunk)
    return True

def sanitize(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip(" .")

def next_scheduled_time(cron_expression, timezone, now=None):
    now = now or datetime.now(timezone)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone)
    else:
        now = now.astimezone(timezone)
    return croniter(cron_expression, now).get_next(datetime)


def process_post(creator, post):
    post_id = str(post["id"])
    if post.get("isRestricted"):
        return "restricted", 0

    title, files = post_files(post_id)
    if not files:
        print("无文件,跳过")
        return "empty", 0

    post_dir = post_directory(creator, post_id, title)
    downloaded = 0
    failed = False
    for index, (url, name) in enumerate(files):
        ext = (os.path.splitext(name)[1] if name
               else os.path.splitext(url.split("?")[0])[1] or ".bin")
        fname = f"{post_id}_{index}{ext}"
        if name:
            fname = f"{post_id}_{index}_{sanitize(name)}"
        try:
            if dl(url, os.path.join(post_dir, fname)):
                downloaded += 1
        except CloudflareBlocked:
            raise
        except Exception as exc:
            failed = True
            print(f"\n  下载 {url} 失败: {exc}")
        time.sleep(FILE_DELAY)

    if failed:
        print(f"未完成 -> {post_dir}")
        return None, downloaded
    print(f"完成({len(files)} 个文件) -> {post_dir}")
    return "downloaded", downloaded


def run_once(creators):
    state, valid = load_state()
    if not valid:
        migrate_existing_posts(state, creators)
        save_state(state)

    try:
        for creator in creators:
            print(f"\n===== 作者: {creator} =====")
            author = creator_state(state, creator)
            initializing = not author["initialized"]
            scan_ok = True
            total_new = 0

            for post_id in missing_downloaded_posts(state, creator):
                print(f"  帖子 {post_id} 目录已删除,重新下载...", flush=True)
                was_initialized = author["initialized"]
                del author["posts"][post_id]
                author["initialized"] = False
                save_state(state)
                try:
                    status, downloaded = process_post(creator, {"id": post_id})
                    total_new += downloaded
                    if status:
                        author["posts"][post_id] = status
                        author["initialized"] = was_initialized
                        save_state(state)
                    else:
                        scan_ok = False
                except CloudflareBlocked as exc:
                    print(f"  {exc}")
                    return
                except Exception as exc:
                    scan_ok = False
                    print(f"  帖子 {post_id} 修复失败: {exc}")
                finally:
                    time.sleep(POST_DELAY)

            try:
                urls = post_urls(creator)
            except Exception as exc:
                print(f"  获取作者帖子列表失败: {exc}")
                continue

            known_pages = 0
            for page_number, url in enumerate(urls, 1):
                try:
                    posts = api_get(url).get("posts", [])
                    time.sleep(DELAY)
                except CloudflareBlocked as exc:
                    print(f"  {exc}")
                    return
                except Exception as exc:
                    scan_ok = False
                    known_pages = 0
                    print(f"  第 {page_number}/{len(urls)} 页失败: {exc}")
                    continue

                page_was_known = True
                for post in posts:
                    post_id = str(post["id"])
                    current = author["posts"].get(post_id)
                    needs_processing = current is None or (
                        current == "restricted" and not post.get("isRestricted")
                    )
                    if not needs_processing:
                        print(f"  帖子 {post_id} 已记录,跳过")
                        continue

                    page_was_known = False
                    if current == "restricted":
                        del author["posts"][post_id]
                        save_state(state)

                    print(f"  帖子 {post_id} ...", end=" ", flush=True)
                    try:
                        status, downloaded = process_post(creator, post)
                        total_new += downloaded
                        if status:
                            author["posts"][post_id] = status
                            save_state(state)
                        else:
                            scan_ok = False
                    except CloudflareBlocked as exc:
                        print(f"\n  {exc}")
                        return
                    except Exception as exc:
                        scan_ok = False
                        print(f"失败: {exc}")
                    finally:
                        time.sleep(POST_DELAY)

                if not initializing:
                    known_pages = known_pages + 1 if page_was_known else 0
                    if known_pages >= 2:
                        break

            if initializing and scan_ok:
                author["initialized"] = True
                save_state(state)

            print(f"  >> 本作者新增 {total_new} 个文件 -> {os.path.join(OUT, creator)}")

        print("\n全部完成。")
    finally:
        close_browser()


def run_scheduler(config, run_once_fn=run_once, now_fn=None, sleep_fn=time.sleep):
    global _shutdown_requested
    now_fn = now_fn or (lambda: datetime.now(config["timezone"]))

    if config["run_on_start"]:
        run_once_fn(config["creators"])

    while not _shutdown_requested:
        next_run = next_scheduled_time(config["cron"], config["timezone"], now_fn())
        print(f"下一次执行时间: {next_run.isoformat()}", flush=True)
        while not _shutdown_requested:
            remaining = (next_run - now_fn()).total_seconds()
            if remaining <= 0:
                break
            sleep_fn(min(remaining, 60))
        if not _shutdown_requested:
            run_once_fn(config["creators"])


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
