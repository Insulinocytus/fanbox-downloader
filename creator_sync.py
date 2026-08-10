"""作者同步：把发现、续传和下载修复隐藏在一次 run() 调用后。"""
import json
import os
import re
import time

STATE_FILE = ".fanbox-state.json"
STATE_VERSION = 1
POST_STATES = {"downloaded", "empty", "restricted"}
RETRY = 5
API_DELAY = 1.0
CLOUDFLARE_WAITS = (30, 60, 120, 300)


class RetryableFanboxError(RuntimeError):
    """一次可重试的 Fanbox 元数据操作失败。"""


class FanboxTimeout(RetryableFanboxError):
    """一次 Fanbox 元数据操作超时。"""


class CloudflareBlocked(RuntimeError):
    """Fanbox 明确报告 Cloudflare 拦截。"""


def wait_with_progress(seconds):
    remaining = int(seconds)
    while remaining > 0:
        print(f"  Cloudflare 冷却中,剩余约 {remaining} 秒", flush=True)
        wait = min(10, remaining)
        time.sleep(wait)
        remaining -= wait


def _empty_state():
    return {"version": STATE_VERSION, "creators": {}}


def _valid_state(state):
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


def _load_state(root):
    try:
        with open(os.path.join(root, STATE_FILE), encoding="utf-8") as fp:
            state = json.load(fp)
        if not _valid_state(state):
            raise ValueError("unsupported state format")
        return state, True
    except FileNotFoundError:
        return _empty_state(), False
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"警告: 状态文件无法读取,将重建: {exc}", flush=True)
        return _empty_state(), False


def _save_state(root, state):
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, STATE_FILE)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as fp:
        json.dump(state, fp, ensure_ascii=False, sort_keys=True)
    os.replace(temporary, path)


def _creator_state(state, creator):
    return state["creators"].setdefault(
        creator, {"initialized": False, "posts": {}}
    )


def _sanitize(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip(" .")


def _post_directory(root, creator, post_id, title):
    title = _sanitize(str(title)) or post_id
    return os.path.join(root, creator, f"{post_id}-{title}")


def _existing_post_ids(root, creator):
    try:
        return {
            match.group(1)
            for entry in os.scandir(os.path.join(root, creator))
            if entry.is_dir() and (match := re.match(r"^(\d+)-", entry.name))
        }
    except FileNotFoundError:
        return set()


def _missing_downloaded_posts(root, state, creator):
    existing = _existing_post_ids(root, creator)
    posts = _creator_state(state, creator)["posts"]
    return sorted(pid for pid, status in posts.items()
                  if status == "downloaded" and pid not in existing)


class CreatorSync:
    """同步配置中的全部作者；公开接口只有无参数 run()。"""

    def __init__(self, config, fanbox):
        self.config = config
        self.fanbox = fanbox
        self.root = config["download_directory"]
        self.file_delay = config["file_delay"]
        self.post_delay = config["post_delay"]
        self.api_delay = API_DELAY

    def run(self):
        state, valid = _load_state(self.root)
        if not valid:
            _save_state(self.root, state)

        try:
            for creator in self.config["creators"]:
                if not self._run_creator(state, creator):
                    return
            print("\n全部完成。")
        finally:
            self.fanbox.close()

    def _run_creator(self, state, creator):
        print(f"\n===== 作者: {creator} =====")
        author = _creator_state(state, creator)
        initializing = not author["initialized"]
        scan_ok = True
        total_new = 0

        for post_id in _missing_downloaded_posts(self.root, state, creator):
            print(f"  帖子 {post_id} 目录已删除,重新下载...", flush=True)
            was_initialized = author["initialized"]
            del author["posts"][post_id]
            author["initialized"] = False
            _save_state(self.root, state)
            try:
                status, downloaded = self._process_post(creator, {"id": post_id})
                total_new += downloaded
                if status:
                    author["posts"][post_id] = status
                    author["initialized"] = was_initialized
                    _save_state(self.root, state)
                else:
                    scan_ok = False
            except CloudflareBlocked as exc:
                print(f"  {exc}")
                return False
            except Exception as exc:
                scan_ok = False
                print(f"  帖子 {post_id} 修复失败: {exc}")
            finally:
                time.sleep(self.post_delay)

        try:
            urls = self._metadata(lambda: self.fanbox.author_pages(creator))
        except Exception as exc:
            print(f"  获取作者帖子列表失败: {exc}")
            return True

        known_pages = 0
        for page_number, url in enumerate(urls, 1):
            try:
                posts = self._metadata(lambda url=url: self.fanbox.page_posts(url))
            except CloudflareBlocked as exc:
                print(f"  {exc}")
                return False
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
                    _save_state(self.root, state)

                print(f"  帖子 {post_id} ...", end=" ", flush=True)
                try:
                    status, downloaded = self._process_post(creator, post)
                    total_new += downloaded
                    if status:
                        author["posts"][post_id] = status
                        _save_state(self.root, state)
                    else:
                        scan_ok = False
                except CloudflareBlocked as exc:
                    print(f"\n  {exc}")
                    return False
                except Exception as exc:
                    scan_ok = False
                    print(f"失败: {exc}")
                finally:
                    time.sleep(self.post_delay)

            if not initializing:
                known_pages = known_pages + 1 if page_was_known else 0
                if known_pages >= 2:
                    break

        if initializing and scan_ok:
            author["initialized"] = True
            _save_state(self.root, state)

        print(f"  >> 本作者新增 {total_new} 个文件 -> {os.path.join(self.root, creator)}")
        return True

    def _metadata(self, operation):
        for attempt in range(1, RETRY + 1):
            if attempt > 1:
                print(f"  冷却结束,正在发起第 {attempt}/{RETRY} 次请求...", flush=True)
            try:
                result = operation()
            except CloudflareBlocked as exc:
                if attempt >= RETRY:
                    raise CloudflareBlocked(
                        f"被 Cloudflare 拦截,重试 {RETRY - 1} 次仍失败。"
                        "已安全停止,稍后重新运行即可从断点继续。"
                    ) from exc
                wait = CLOUDFLARE_WAITS[min(attempt - 1, len(CLOUDFLARE_WAITS) - 1)]
                print(f"  Cloudflare 临时拦截,{wait}s 后重试(下一次 {attempt + 1}/{RETRY})",
                      flush=True)
                wait_with_progress(wait)
            except RetryableFanboxError as exc:
                if attempt >= RETRY:
                    raise
                wait = 2 ** attempt
                if isinstance(exc, FanboxTimeout):
                    message = "浏览器请求超时"
                else:
                    message = f"浏览器请求错误({exc})"
                print(f"  {message},{wait}s 后重试({attempt}/{RETRY})", flush=True)
                time.sleep(wait)
            else:
                time.sleep(self.api_delay)
                return result
        raise RuntimeError("unreachable")

    def _process_post(self, creator, post):
        post_id = str(post["id"])
        if post.get("isRestricted"):
            return "restricted", 0

        title, files = self._metadata(lambda: self.fanbox.post_detail(post_id))
        if not files:
            print("无文件,跳过")
            return "empty", 0

        post_dir = _post_directory(self.root, creator, post_id, title)
        downloaded = 0
        failed = False
        for index, (url, name) in enumerate(files):
            ext = (os.path.splitext(name)[1] if name
                   else os.path.splitext(url.split("?")[0])[1] or ".bin")
            filename = f"{post_id}_{index}{ext}"
            if name:
                filename = f"{post_id}_{index}_{_sanitize(name)}"
            try:
                if self.fanbox.download_file(url, os.path.join(post_dir, filename)):
                    downloaded += 1
            except CloudflareBlocked:
                raise
            except Exception as exc:
                failed = True
                print(f"\n  下载 {url} 失败: {exc}")
            time.sleep(self.file_delay)

        if failed:
            print(f"未完成 -> {post_dir}")
            return None, downloaded
        print(f"完成({len(files)} 个文件) -> {post_dir}")
        return "downloaded", downloaded
