#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fanbox 下载器(curl_cffi 版,可绕过 Cloudflare 拦截)。

用法:
  1. 第一次:   pip install curl_cffi
  2. 打开 fanbox_cookie.txt(空文件),把 FANBOXSESSID 的值粘到第一行(方法:
     浏览器登录 www.fanbox.cc 后按 F12 → Application → Cookies →
     https://www.fanbox.cc → 复制 FANBOXSESSID 那一行的值)
  3. 在 creators.txt 里填作者名,一行一个(例如: cowmopcat)
  4. 运行:     python fanbox_dl.py
"""
import os
import re
import sys
import time
import urllib.parse
from curl_cffi import requests

# Windows 控制台中文:先切 UTF-8 再让 Python 输出 UTF-8
os.system("chcp 65001 >nul")
sys.stdout.reconfigure(encoding="utf-8")

BASE = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(BASE, "fanbox_cookie.txt")
CREATORS_FILE = os.path.join(BASE, "creators.txt")
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
RETRY = 5      # 被 Cloudflare 临时拦截时的重试次数
DELAY = 1.0    # 每请求之间间隔(秒),别让 fanbox 觉得是机器人

_sess = None
_cookie_ok = False

def sess():
    global _sess, _cookie_ok
    if _sess is None:
        _sess = requests.Session(impersonate=IMPERSONATE)
        if os.path.exists(COOKIE_FILE):
            ck = open(COOKIE_FILE, encoding="utf-8").read().strip()
            if ck:
                _sess.cookies.set("FANBOXSESSID", ck, domain=".fanbox.cc")
                _cookie_ok = True
        if not _cookie_ok:
            print("注意:没读到 cookie(fanbox_cookie.txt 是空的或值不合法)。"
                  "付费帖子只能下到封面,正文下不到。把 FANBOXSESSID 粘到该文件"
                  "第一行后再跑。")
    return _sess

def api_get(url, retry=RETRY):
    for attempt in range(1, retry + 1):
        try:
            r = sess().get(url, headers=HEADERS, timeout=30)
        except requests.RequestsError as e:
            if attempt < retry:
                wait = 2 ** attempt
                print(f"  网络错误,{wait}s 后重试({attempt}/{retry})", flush=True)
                time.sleep(wait)
                continue
            raise
        if r.status_code == 403 and "block_ip" in r.text:
            if attempt < retry:
                wait = 2 ** attempt
                print(f"  Cloudflare 临时拦截,{wait}s 后重试({attempt}/{retry})",
                      flush=True)
                time.sleep(wait)
                continue
            sys.exit("被 Cloudflare 拦截,重试 {RETRY} 次仍失败。等一会或换个 IP/网络再试。")
        if r.status_code == 401 or (r.status_code == 403 and "body" not in r.text):
            sys.exit("登录失效(cookie 无效/过期)。请重新复制 FANBOXSESSID 到 fanbox_cookie.txt")
        r.raise_for_status()
        return r.json()["body"]
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
    r.raise_for_status()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fp:
        for chunk in r.iter_content(1 << 16):
            fp.write(chunk)
    return True

def sanitize(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip(" .")

def main():
    if not os.path.exists(CREATORS_FILE):
        sys.exit(f"没找到 {CREATORS_FILE},请先创建(一行一个作者名)")
    creators = [l.strip() for l in open(CREATORS_FILE, encoding="utf-8")
                if l.strip() and not l.strip().startswith("#")]
    if not creators:
        sys.exit(f"{CREATORS_FILE} 是空的,先填作者名")

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
                except Exception as e:
                    print(f"失败: {e}")
                    continue
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
                    except Exception as e:
                        print(f"\n  下载 {url} 失败: {e}")
                print(f"完成({n} 个文件) -> {post_dir}")
        print(f"  >> 本作者新增 {total_new} 个文件 -> {os.path.join(OUT, creator)}")

    print("\n全部完成。")

if __name__ == "__main__":
    main()
