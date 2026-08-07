#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fanbox 下载器(curl_cffi 版,可绕过 Cloudflare 拦截)。

用法:
  1. 第一次:   pip install curl_cffi
  2. 在 config.json 中填写 cookie、作者列表和下载目录
  3. 运行:     python fanbox_dl.py
"""
import atexit
import json
import os
import re
import sys
import time
import urllib.parse
from cloakbrowser import launch_persistent_context
from curl_cffi import requests

# Windows 控制台中文:先切 UTF-8 再让 Python 输出 UTF-8
os.system("chcp 65001 >nul")
sys.stdout.reconfigure(encoding="utf-8")

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE, "config.json")
PROFILE_DIR = os.path.join(BASE, ".cloak-profile")
OUT = os.path.join(BASE, "downloads")

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
FILE_DELAY = 2.0
POST_DELAY = 10.0
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
_cookie_ok = False

class CloudflareBlocked(RuntimeError):
    pass

def read_config(path=None, base=None):
    path = path or CONFIG_FILE
    base = base or BASE
    if not os.path.exists(path):
        sys.exit(f"没找到 {path},请创建 config.json")

    try:
        with open(path, encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, json.JSONDecodeError) as exc:
        sys.exit(f"无法读取配置文件 {path}: {exc}")

    if not isinstance(data, dict):
        sys.exit("config.json 必须是 JSON 对象")

    cookie = data.get("cookie", "")
    if not isinstance(cookie, str):
        sys.exit("config.json 的 cookie 必须是字符串")

    creators = data.get("creators", [])
    if not isinstance(creators, list):
        sys.exit("config.json 的 creators 必须是数组")
    creators = [creator.strip() for creator in creators
                if isinstance(creator, str) and creator.strip()]

    download_directory = data.get("download_directory", "downloads")
    if not isinstance(download_directory, str) or not download_directory.strip():
        download_directory = "downloads"
    if not os.path.isabs(download_directory):
        download_directory = os.path.join(base, download_directory)

    def read_delay(name, default):
        value = data.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            sys.exit(f"config.json 的 {name} 必须是非负数字")
        return float(value)

    return {
        "cookie": cookie.strip(),
        "creators": creators,
        "download_directory": os.path.abspath(download_directory),
        "file_delay": read_delay("file_delay", 2.0),
        "post_delay": read_delay("post_delay", 10.0),
    }

def configure():
    global _cookie_value, OUT, FILE_DELAY, POST_DELAY
    config = read_config()
    _cookie_value = config["cookie"]
    OUT = config["download_directory"]
    FILE_DELAY = config["file_delay"]
    POST_DELAY = config["post_delay"]
    return config["creators"]

def sess():
    global _sess, _cookie_value, _cookie_ok
    if _sess is None:
        if _cookie_value is None:
            configure()
        _sess = requests.Session(impersonate=IMPERSONATE)
        if _cookie_value:
            _sess.cookies.set("FANBOXSESSID", _cookie_value, domain=".fanbox.cc")
            _cookie_ok = True
        if not _cookie_ok:
            print("注意:config.json 的 cookie 为空或值不合法。"
                  "付费帖子只能下到封面,正文下不到。")
    return _sess

def browser_fetch(url):
    global _browser_context, _browser_page
    if _browser_page is None:
        if _cookie_value is None:
            configure()
        print("正在启动 CloakBrowser...", flush=True)
        _browser_context = launch_persistent_context(
            PROFILE_DIR,
            headless=False,
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
            sys.exit("登录失效(cookie 无效/过期)。请更新 config.json 的 cookie")
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

def main():
    creators = configure()
    if not creators:
        sys.exit("config.json 的 creators 是空的,请先填作者 ID")

    for creator in creators:
        print(f"\n===== 作者: {creator} =====")
        try:
            urls = post_urls(creator)
        except Exception as e:
            print(f"  获取作者帖子列表失败: {e}")
            continue

        total_new = 0
        for ui, u in enumerate(urls, 1):
            try:
                posts = api_get(u).get("posts", [])
                time.sleep(DELAY)
            except CloudflareBlocked as e:
                print(f"  {e}")
                return
            except Exception as e:
                print(f"  第 {ui}/{len(urls)} 页失败: {e}")
                continue
            for p in posts:
                pid = p["id"]
                if post_downloaded(creator, p):
                    print(f"  帖子 {pid} 已存在,跳过")
                    continue
                print(f"  帖子 {pid} ...", end=" ", flush=True)
                try:
                    title, files = post_files(pid)
                    if not files:
                        print("无文件,跳过")
                        continue
                    post_dir = post_directory(creator, pid, title)
                    n = 0
                    for url, name in files:
                        ext = (os.path.splitext(name)[1] if name
                               else os.path.splitext(url.split("?")[0])[1] or ".bin")
                        fname = f"{pid}_{n}{ext}"
                        if name:
                            fname = f"{pid}_{n}_{sanitize(name)}"
                        path = os.path.join(post_dir, fname)
                        try:
                            if dl(url, path):
                                total_new += 1
                            n += 1
                        except CloudflareBlocked:
                            raise
                        except Exception as e:
                            print(f"\n  下载 {url} 失败: {e}")
                        time.sleep(FILE_DELAY)
                    print(f"完成({n} 个文件) -> {post_dir}")
                except CloudflareBlocked as e:
                    print(f"\n  {e}")
                    return
                except Exception as e:
                    print(f"失败: {e}")
                finally:
                    time.sleep(POST_DELAY)
        print(f"  >> 本作者新增 {total_new} 个文件 -> {os.path.join(OUT, creator)}")

    print("\n全部完成。")

if __name__ == "__main__":
    main()
