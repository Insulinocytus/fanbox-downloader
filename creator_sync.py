"""作者同步：把发现、续传和下载修复隐藏在一次 run() 调用后。"""
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass

DATABASE_FILE = ".fanbox-state.sqlite3"
SCHEMA_VERSION = "1"
POST_STATES = {"downloading", "downloaded", "empty", "restricted"}
RETRY = 5
API_DELAY = 1.0
CLOUDFLARE_WAITS = (30, 60, 120, 300)


class RetryableFanboxError(RuntimeError):
    """一次可重试的 Fanbox 元数据操作失败。"""


class FanboxTimeout(RetryableFanboxError):
    """一次 Fanbox 元数据操作超时。"""


class CloudflareBlocked(RuntimeError):
    """Fanbox 明确报告 Cloudflare 拦截。"""


class StateDatabaseError(RuntimeError):
    """下载记录数据库无法安全使用。"""


@dataclass(frozen=True)
class _PostResult:
    status: str | None
    downloaded_file_count: int
    title: str
    directory_path: str | None


@dataclass(frozen=True)
class _PostKey:
    creator_id: str
    post_id: str


@dataclass(frozen=True)
class _ManifestFile:
    position: int
    local_name: str
    resource_url: str
    expected_size: int | None
    phase: str


def wait_with_progress(seconds):
    remaining = int(seconds)
    while remaining > 0:
        print(f"  Cloudflare 冷却中,剩余约 {remaining} 秒", flush=True)
        wait = min(10, remaining)
        time.sleep(wait)
        remaining -= wait


class _DownloadState:
    """同步模块私有的 SQLite 下载记录；不是可替换仓储接口。"""

    def __init__(self, path):
        self.connection = None
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            self.connection = sqlite3.connect(path)
            self._verify_and_initialize()
        except (OSError, sqlite3.Error, StateDatabaseError) as exc:
            if self.connection is not None:
                self.connection.close()
            raise StateDatabaseError(f"无法打开或验证 {path}: {exc}") from exc

    def _verify_and_initialize(self):
        integrity = self.connection.execute("PRAGMA integrity_check").fetchone()
        if integrity != ("ok",):
            detail = integrity[0] if integrity else "无检查结果"
            raise StateDatabaseError(f"完整性检查失败: {detail}")

        tables = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
            if not row[0].startswith("sqlite_")
        }
        if tables and "metadata" not in tables:
            raise StateDatabaseError("数据库不是受支持的 Fanbox 下载记录")

        if "metadata" in tables:
            try:
                version = self.connection.execute(
                    "SELECT value FROM metadata WHERE key = 'schema_version'"
                ).fetchone()
            except sqlite3.Error as exc:
                raise StateDatabaseError("数据库元数据无法读取") from exc
            if version != (SCHEMA_VERSION,):
                raise StateDatabaseError("数据库版本不受支持")

        journal_mode = self.connection.execute("PRAGMA journal_mode = DELETE").fetchone()
        if not journal_mode or journal_mode[0].lower() != "delete":
            raise StateDatabaseError("无法启用 SQLite 回滚日志")
        self.connection.execute("PRAGMA synchronous = FULL")
        self.connection.execute("PRAGMA foreign_keys = ON")
        if self.connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
            raise StateDatabaseError("无法启用 SQLite 外键约束")

        with self.connection:
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS creators (
                    creator_id TEXT PRIMARY KEY,
                    baseline_complete INTEGER NOT NULL DEFAULT 0
                        CHECK (baseline_complete IN (0, 1))
                );
                CREATE TABLE IF NOT EXISTS posts (
                    creator_id TEXT NOT NULL,
                    post_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'downloading'
                        CHECK (status IN ('downloading', 'downloaded', 'empty', 'restricted')),
                    title TEXT,
                    directory_path TEXT,
                    PRIMARY KEY (creator_id, post_id),
                    FOREIGN KEY (creator_id) REFERENCES creators(creator_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS files (
                    creator_id TEXT NOT NULL,
                    post_id TEXT NOT NULL,
                    position INTEGER NOT NULL CHECK (position >= 0),
                    local_name TEXT NOT NULL,
                    resource_url TEXT NOT NULL,
                    expected_size INTEGER CHECK (expected_size >= 0),
                    phase TEXT NOT NULL DEFAULT 'downloading'
                        CHECK (phase IN ('downloading', 'staged', 'complete')),
                    PRIMARY KEY (creator_id, post_id, position),
                    UNIQUE (creator_id, post_id, local_name),
                    FOREIGN KEY (creator_id, post_id)
                        REFERENCES posts(creator_id, post_id) ON DELETE CASCADE
                );
                INSERT OR IGNORE INTO metadata(key, value)
                VALUES ('schema_version', '1');
            """)

    def close(self):
        self.connection.close()

    def ensure_creator(self, creator):
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO creators(creator_id) VALUES (?)", (creator,)
            )

    def baseline_complete(self, creator):
        row = self.connection.execute(
            "SELECT baseline_complete FROM creators WHERE creator_id = ?", (creator,)
        ).fetchone()
        return bool(row[0])

    def complete_baseline(self, creator):
        with self.connection:
            self.connection.execute(
                "UPDATE creators SET baseline_complete = 1 WHERE creator_id = ?",
                (creator,),
            )

    def record_page(self, creator, posts):
        """在一个事务中保存整页，并返回该页在事务前是否完全已知。"""
        post_ids = {str(post["id"]) for post in posts}
        if post_ids:
            placeholders = ",".join("?" for _ in post_ids)
            known = {
                row[0]
                for row in self.connection.execute(
                    f"SELECT post_id FROM posts WHERE creator_id = ? "
                    f"AND post_id IN ({placeholders})",
                    (creator, *post_ids),
                )
            }
        else:
            known = set()

        with self.connection:
            for post in posts:
                post_id = str(post["id"])
                status = "restricted" if post.get("isRestricted") else "downloading"
                title = str(post.get("title")) if post.get("title") else None
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO posts(
                        creator_id, post_id, status, title
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (creator, post_id, status, title),
                )
                if status == "restricted":
                    self.connection.execute(
                        """
                        UPDATE posts
                        SET status = 'restricted', title = COALESCE(title, ?)
                        WHERE creator_id = ? AND post_id = ?
                          AND status = 'downloading'
                          AND NOT EXISTS (
                              SELECT 1 FROM files
                              WHERE files.creator_id = posts.creator_id
                                AND files.post_id = posts.post_id
                          )
                        """,
                        (title, creator, post_id),
                    )
        return post_ids <= known

    def pending_posts(self, creator):
        return [
            {"id": row[0]}
            for row in self.connection.execute(
                """
                SELECT post_id FROM posts
                WHERE creator_id = ? AND status = 'downloading'
                ORDER BY CAST(post_id AS INTEGER) DESC, post_id DESC
                """,
                (creator,),
            )
        ]

    def post_snapshot(self, key):
        return self.connection.execute(
            """
            SELECT title, directory_path FROM posts
            WHERE creator_id = ? AND post_id = ?
            """,
            (key.creator_id, key.post_id),
        ).fetchone()

    def manifest_files(self, key):
        return [
            _ManifestFile(*row)
            for row in self.connection.execute(
                """
                SELECT position, local_name, resource_url, expected_size, phase
                FROM files
                WHERE creator_id = ? AND post_id = ?
                ORDER BY position
                """,
                (key.creator_id, key.post_id),
            )
        ]

    def create_manifest(self, key, title, directory_path, files):
        """在任何文件传输前，以单个事务创建下载清单。"""
        with self.connection:
            self.connection.execute(
                """
                UPDATE posts
                SET status = 'downloading', title = ?, directory_path = ?
                WHERE creator_id = ? AND post_id = ?
                """,
                (title, directory_path, key.creator_id, key.post_id),
            )
            self.connection.execute(
                "DELETE FROM files WHERE creator_id = ? AND post_id = ?",
                (key.creator_id, key.post_id),
            )
            self.connection.executemany(
                """
                INSERT INTO files(
                    creator_id, post_id, position, local_name, resource_url, phase
                ) VALUES (?, ?, ?, ?, ?, 'downloading')
                """,
                [
                    (key.creator_id, key.post_id, position, local_name, resource_url)
                    for position, local_name, resource_url in files
                ],
            )

    def stage_file(self, key, position, actual_size):
        with self.connection:
            self.connection.execute(
                """
                UPDATE files
                SET expected_size = COALESCE(expected_size, ?), phase = 'staged'
                WHERE creator_id = ? AND post_id = ? AND position = ?
                """,
                (actual_size, key.creator_id, key.post_id, position),
            )

    def complete_file(self, key, position):
        with self.connection:
            self.connection.execute(
                """
                UPDATE files SET phase = 'complete'
                WHERE creator_id = ? AND post_id = ? AND position = ?
                """,
                (key.creator_id, key.post_id, position),
            )

    def restart_files(self, key, positions):
        positions = tuple(positions)
        if not positions:
            return
        placeholders = ",".join("?" for _ in positions)
        with self.connection:
            self.connection.execute(
                "UPDATE posts SET status = 'downloading' "
                "WHERE creator_id = ? AND post_id = ?",
                (key.creator_id, key.post_id),
            )
            self.connection.execute(
                f"""
                UPDATE files SET phase = 'downloading'
                WHERE creator_id = ? AND post_id = ?
                  AND position IN ({placeholders})
                """,
                (key.creator_id, key.post_id, *positions),
            )

    def all_files_complete(self, key):
        return self.connection.execute(
            """
            SELECT COUNT(*) > 0
               AND SUM(CASE WHEN phase = 'complete' THEN 0 ELSE 1 END) = 0
            FROM files WHERE creator_id = ? AND post_id = ?
            """,
            (key.creator_id, key.post_id),
        ).fetchone() == (1,)

    def downloaded_posts(self, creator):
        return [
            {"id": row[0], "directory_path": row[1]}
            for row in self.connection.execute(
                """
                SELECT post_id, directory_path FROM posts
                WHERE creator_id = ? AND status = 'downloaded'
                ORDER BY CAST(post_id AS INTEGER) DESC, post_id DESC
                """,
                (creator,),
            )
        ]

    def reset_post(self, key):
        with self.connection:
            self.connection.execute(
                "DELETE FROM files WHERE creator_id = ? AND post_id = ?",
                (key.creator_id, key.post_id),
            )
            self.connection.execute(
                """
                UPDATE posts
                SET status = 'downloading', title = NULL, directory_path = NULL
                WHERE creator_id = ? AND post_id = ?
                """,
                (key.creator_id, key.post_id),
            )

    def save_post_result(self, key, result):
        if result.status is not None and result.status not in POST_STATES:
            raise ValueError(f"未知帖子状态: {result.status}")
        with self.connection:
            if result.status in {"empty", "restricted"}:
                self.connection.execute(
                    "DELETE FROM files WHERE creator_id = ? AND post_id = ?",
                    (key.creator_id, key.post_id),
                )
            self.connection.execute(
                """
                UPDATE posts
                SET status = COALESCE(?, status), title = ?, directory_path = ?
                WHERE creator_id = ? AND post_id = ?
                """,
                (
                    result.status,
                    result.title,
                    result.directory_path,
                    key.creator_id,
                    key.post_id,
                ),
            )


def _sanitize(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name).strip(" .")


def _post_directory(root, creator, post_id, title):
    title = _sanitize(str(title)) or post_id
    return os.path.join(root, creator, f"{post_id}-{title}")


def _manifest_entries(post_id, files):
    entries = []
    for position, (resource_url, name) in enumerate(files):
        extension = (
            os.path.splitext(name)[1]
            if name
            else os.path.splitext(resource_url.split("?")[0])[1] or ".bin"
        )
        local_name = f"{post_id}_{position}{extension}"
        if name:
            local_name = f"{post_id}_{position}_{_sanitize(name)}"
        entries.append((position, local_name, resource_url))
    return entries


def _file_has_size(path, expected_size):
    return (
        expected_size is not None
        and os.path.isfile(path)
        and os.path.getsize(path) == expected_size
    )


class CreatorSync:
    """同步配置中的全部作者；公开接口只有无参数 run()。"""

    def __init__(self, config, fanbox):
        self.config = config
        self.fanbox = fanbox
        self.root = config["download_directory"]
        state_directory = config.get("state_directory") or self.root
        self.database_path = os.path.join(state_directory, DATABASE_FILE)
        self.file_delay = config["file_delay"]
        self.post_delay = config["post_delay"]
        self.api_delay = API_DELAY

    def run(self):
        database = None
        try:
            try:
                database = _DownloadState(self.database_path)
            except StateDatabaseError as exc:
                print(f"错误: 下载记录数据库不可用: {exc}", file=sys.stderr, flush=True)
                raise SystemExit(1) from exc

            try:
                for creator in self.config["creators"]:
                    if not self._run_creator(database, creator):
                        return
            except sqlite3.Error as exc:
                print(
                    f"错误: 下载记录数据库操作失败: {self.database_path}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                raise SystemExit(1) from exc
            print("\n全部完成。")
        finally:
            if database is not None:
                database.close()
            self.fanbox.close()

    def _run_creator(self, database, creator):
        print(f"\n===== 作者: {creator} =====")
        database.ensure_creator(creator)
        baseline_complete = database.baseline_complete(creator)
        total_new = 0

        try:
            urls = self._metadata(lambda: self.fanbox.author_pages(creator))
        except CloudflareBlocked as exc:
            print(f"  {exc}")
            return False
        except Exception as exc:
            print(f"  获取作者帖子列表失败,本轮跳过作者: {exc}")
            return True

        known_pages = 0
        for page_number, url in enumerate(urls, 1):
            try:
                posts = self._metadata(lambda url=url: self.fanbox.page_posts(url))
                page_was_known = database.record_page(creator, posts)
            except sqlite3.Error:
                raise
            except CloudflareBlocked as exc:
                print(f"  {exc}")
                return False
            except Exception as exc:
                print(
                    f"  第 {page_number}/{len(urls)} 页失败,本轮跳过作者: {exc}"
                )
                return True
            if baseline_complete:
                known_pages = known_pages + 1 if page_was_known else 0
                if known_pages >= 2:
                    break

        if not baseline_complete:
            database.complete_baseline(creator)

        for post in database.pending_posts(creator):
            post_id = str(post["id"])
            print(f"  帖子 {post_id} ...", end=" ", flush=True)
            try:
                result = self._attempt_recorded_post(
                    database, creator, post, "失败"
                )
            except CloudflareBlocked as exc:
                print(f"\n  {exc}")
                return False
            if result is not None:
                total_new += result.downloaded_file_count

        repair_ok, repaired = self._repair_downloads(database, creator)
        total_new += repaired
        if not repair_ok:
            return False

        print(f"  >> 本作者新增 {total_new} 个文件 -> {os.path.join(self.root, creator)}")
        return True

    def _process_recorded_post(self, database, creator, post):
        key = _PostKey(creator, str(post["id"]))
        result = self._process_post(database, key, post)
        database.save_post_result(key, result)
        return result

    def _attempt_recorded_post(self, database, creator, post, failure_message):
        try:
            return self._process_recorded_post(database, creator, post)
        except sqlite3.Error:
            raise
        except CloudflareBlocked:
            raise
        except Exception as exc:
            print(f"{failure_message}: {exc}")
            return None
        finally:
            time.sleep(self.post_delay)

    def _repair_downloads(self, database, creator):
        downloaded_file_count = 0
        for post in database.downloaded_posts(creator):
            key = _PostKey(creator, str(post["id"]))
            post_id = key.post_id
            directory_path = post["directory_path"]
            files = database.manifest_files(key)

            if not directory_path or not os.path.isdir(directory_path):
                print(f"  帖子 {post_id} 目录已删除,执行快照重置...", flush=True)
                database.reset_post(key)
            else:
                broken = [
                    file.position
                    for file in files
                    if file.phase != "complete"
                    or not _file_has_size(
                        os.path.join(directory_path, file.local_name),
                        file.expected_size,
                    )
                ]
                if files and not broken:
                    continue
                if files:
                    print(
                        f"  帖子 {post_id} 有 {len(broken)} 个文件需要下载修复...",
                        flush=True,
                    )
                    database.restart_files(key, broken)
                else:
                    print(f"  帖子 {post_id} 缺少下载清单,执行快照重置...", flush=True)
                    database.reset_post(key)

            try:
                result = self._attempt_recorded_post(
                    database,
                    creator,
                    {"id": post_id},
                    f"  帖子 {post_id} 下载修复失败",
                )
            except CloudflareBlocked as exc:
                print(f"  {exc}")
                return False, downloaded_file_count
            if result is not None:
                downloaded_file_count += result.downloaded_file_count
        return True, downloaded_file_count

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
                print(
                    f"  Cloudflare 临时拦截,{wait}s 后重试"
                    f"(下一次 {attempt + 1}/{RETRY})",
                    flush=True,
                )
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

    def _process_post(self, database, key, post):
        post_id = str(post["id"])
        manifest = database.manifest_files(key)
        if manifest:
            title, directory_path = database.post_snapshot(key)
            if not title or not directory_path:
                raise RuntimeError(f"帖子 {post_id} 的下载清单缺少快照路径")
            return self._download_manifest(
                database, key, title, directory_path
            )

        if post.get("isRestricted"):
            title = str(post.get("title") or post_id)
            return _PostResult("restricted", 0, title, None)

        title, files = self._metadata(lambda: self.fanbox.post_detail(post_id))
        if not files:
            print("无文件,跳过")
            return _PostResult("empty", 0, title, None)

        directory_path = _post_directory(self.root, key.creator_id, post_id, title)
        database.create_manifest(
            key,
            title,
            directory_path,
            _manifest_entries(post_id, files),
        )
        return self._download_manifest(
            database, key, title, directory_path
        )

    def _download_manifest(
        self, database, key, title, directory_path
    ):
        os.makedirs(directory_path, exist_ok=True)
        downloaded_file_count = 0
        failed = False

        for file in database.manifest_files(key):
            final_path = os.path.join(directory_path, file.local_name)
            part_path = final_path + ".part"
            try:
                if self._recover_manifest_file(
                    database, key, file, final_path, part_path
                ):
                    continue

                if os.path.exists(part_path):
                    os.remove(part_path)
                created = self.fanbox.download_file(file.resource_url, part_path)
                if not created or not os.path.isfile(part_path):
                    raise RuntimeError("文件传输未产生临时文件")

                actual_size = os.path.getsize(part_path)
                if (
                    file.expected_size is not None
                    and actual_size != file.expected_size
                ):
                    raise RuntimeError(
                        f"下载大小 {actual_size} 与快照大小 "
                        f"{file.expected_size} 不一致"
                    )

                database.stage_file(
                    key, file.position, actual_size
                )
                os.replace(part_path, final_path)
                database.complete_file(key, file.position)
                downloaded_file_count += 1
            except sqlite3.Error:
                raise
            except CloudflareBlocked:
                raise
            except Exception as exc:
                failed = True
                print(f"\n  下载 {file.resource_url} 失败: {exc}")
            finally:
                time.sleep(self.file_delay)

        if failed or not database.all_files_complete(key):
            print(f"未完成 -> {directory_path}")
            return _PostResult(
                None, downloaded_file_count, title, directory_path
            )

        print(
            f"完成({len(database.manifest_files(key))} 个文件)"
            f" -> {directory_path}"
        )
        return _PostResult(
            "downloaded", downloaded_file_count, title, directory_path
        )

    @staticmethod
    def _recover_manifest_file(
        database, key, file, final_path, part_path
    ):
        final_is_complete = _file_has_size(final_path, file.expected_size)

        if file.phase in {"staged", "complete"} and final_is_complete:
            if file.phase == "staged":
                database.complete_file(key, file.position)
            if os.path.exists(part_path):
                os.remove(part_path)
            return True

        if file.phase == "staged" and _file_has_size(
            part_path, file.expected_size
        ):
            os.replace(part_path, final_path)
            database.complete_file(key, file.position)
            return True

        if file.phase != "downloading":
            database.restart_files(key, [file.position])
        return False
