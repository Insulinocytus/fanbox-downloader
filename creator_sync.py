"""作者同步：把发现、续传和下载修复隐藏在一次 run() 调用后。"""
import os
import re
import sqlite3
import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from dataclasses import dataclass
from enum import StrEnum

DATABASE_FILE = ".fanbox-state.sqlite3"
SCHEMA_VERSION = "1"
POST_STATES = {"downloading", "downloaded", "empty", "restricted"}
MAX_ATTEMPTS = 4
RETRY_WAITS = (10, 30, 60)
API_DELAY = 1.0
CLOUDFLARE_WAITS = (30, 60, 120, 300)
MAX_CONSECUTIVE_CLOUDFLARE = 3
logger = logging.getLogger(__name__)


class RetryableFanboxError(RuntimeError):
    """A Fanbox operation failed in a way that may succeed on retry."""

    def __init__(self, message, *, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after

class FanboxTimeout(RetryableFanboxError):
    """一次 Fanbox 元数据操作超时。"""


class CloudflareBlocked(RuntimeError):
    """Fanbox 明确报告 Cloudflare 拦截。"""


class AuthenticationError(RuntimeError):
    """Fanbox Cookie 失效或账号未授权。"""


class LocalPersistenceError(RuntimeError):
    """下载产物无法安全写入本地存储。"""

    def __init__(self, operation, cause, *, creator_id=None, post_id=None, file_path=None):
        self.operation = operation
        self.cause = cause
        self.creator_id = creator_id
        self.post_id = post_id
        self.file_path = file_path
        super().__init__(str(cause))


class RetryScope(StrEnum):
    AUTHOR = "author"
    FILE = "file"


class StateDatabaseError(RuntimeError):
    """下载记录数据库无法安全使用。"""


class RetryExhausted(RuntimeError):
    """A remote operation failed on all configured attempts."""

    def __init__(self, scope: RetryScope, operation, attempts, cause):
        self.scope = scope
        self.operation = operation
        self.attempts = attempts
        self.cause = cause
        super().__init__(
            f"{operation} failed after {attempts} attempts: {cause}"
        )

    @property
    def skips_author(self):
        return self.scope is not RetryScope.FILE


@dataclass(frozen=True)
class SyncResult:
    outcome: str
    creators_completed: int
    creators_partial: int
    creators_skipped: int
    files_downloaded: int
    cloudflare_blocks: int


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


def wait_with_progress(seconds, sleep_fn=None):
    sleep_fn = sleep_fn or time.sleep
    remaining = int(seconds)
    while remaining > 0:
        logger.info(
            "operation=cloudflare_cooldown category=cloudflare wait=%ss "
            "escalation=retry",
            remaining,
        )
        wait = min(10, remaining)
        sleep_fn(wait)
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

    def __init__(self, config, fanbox, sleep_fn=None):
        self.config = config
        self.fanbox = fanbox
        self.root = config["download_directory"]
        state_directory = config.get("state_directory") or self.root
        self.database_path = os.path.join(state_directory, DATABASE_FILE)
        self.file_delay = config["file_delay"]
        self.post_delay = config["post_delay"]
        self.api_delay = API_DELAY
        self.sleep_fn = sleep_fn or time.sleep

    def run(self):
        database = None
        self._cloudflare_streak = 0
        self._cloudflare_blocks = 0
        creators_completed = 0
        creators_partial = 0
        files_downloaded = 0
        try:
            try:
                database = _DownloadState(self.database_path)
            except StateDatabaseError as exc:
                logger.error(
                    "operation=open_database file=%s category=local_persistence "
                    "attempt=1/1 wait=none escalation=terminate_process error=%s",
                    self.database_path,
                    exc,
                )
                raise SystemExit(1) from exc

            try:
                creators = self.config["creators"]
                for index, creator in enumerate(creators):
                    outcome, downloaded = self._run_creator(database, creator)
                    files_downloaded += downloaded
                    if outcome == "cloudflare_aborted":
                        result = SyncResult(
                            "cloudflare_aborted",
                            creators_completed,
                            creators_partial + 1,
                            len(creators) - index - 1,
                            files_downloaded,
                            self._cloudflare_blocks,
                        )
                        self._log_summary(result)
                        return result
                    if outcome == "partial":
                        creators_partial += 1
                    else:
                        creators_completed += 1
            except AuthenticationError as exc:
                logger.error(
                    "operation=%s author=%s post=%s file=%s category=authentication "
                    "attempt=1/1 wait=none escalation=terminate_process error=%s",
                    getattr(exc, "operation", "fanbox_request"),
                    getattr(exc, "creator_id", None),
                    getattr(exc, "post_id", None),
                    getattr(exc, "file_path", None),
                    exc,
                )
                raise SystemExit(1) from exc
            except LocalPersistenceError as exc:
                logger.error(
                    "operation=%s author=%s post=%s file=%s category=local_persistence "
                    "attempt=1/1 wait=none escalation=terminate_process error=%s",
                    exc.operation,
                    exc.creator_id,
                    exc.post_id,
                    exc.file_path,
                    exc,
                )
                raise SystemExit(1) from exc
            except sqlite3.Error as exc:
                logger.error(
                    "operation=database_sync file=%s category=local_persistence "
                    "attempt=1/1 wait=none escalation=terminate_process error=%s",
                    self.database_path,
                    exc,
                )
                raise SystemExit(1) from exc
            except OSError as exc:
                logger.error(
                    "operation=sync category=local_persistence attempt=1/1 "
                    "wait=none escalation=terminate_process error=%s",
                    exc,
                )
                raise SystemExit(1) from exc
            result = SyncResult(
                "partial" if creators_partial else "complete",
                creators_completed,
                creators_partial,
                0,
                files_downloaded,
                self._cloudflare_blocks,
            )
            self._log_summary(result)
            return result
        finally:
            if database is not None:
                database.close()
            self.fanbox.close()

    @staticmethod
    def _log_summary(result):
        logger.info(
            "sync_summary outcome=%s creators_completed=%s creators_partial=%s "
            "creators_skipped=%s files_downloaded=%s cloudflare_blocks=%s",
            result.outcome,
            result.creators_completed,
            result.creators_partial,
            result.creators_skipped,
            result.files_downloaded,
            result.cloudflare_blocks,
        )

    def _run_creator(self, database, creator):
        logger.info("operation=sync_author author=%s", creator)
        database.ensure_creator(creator)
        baseline_complete = database.baseline_complete(creator)
        total_new = 0
        partial = False

        try:
            urls = self._metadata(
                lambda: self.fanbox.author_pages(creator),
                operation_name="author_pages",
                creator_id=creator,
            )
        except (AuthenticationError, LocalPersistenceError, OSError):
            raise
        except CloudflareBlocked as exc:
            return "cloudflare_aborted", total_new
        except RetryExhausted as exc:
            return "partial", total_new
        except Exception as exc:
            logger.error(
                "operation=author_pages author=%s category=permanent_remote "
                "attempt=1/1 wait=none escalation=skip_author error=%s",
                creator,
                exc,
            )
            return "partial", total_new

        known_pages = 0
        for page_number, url in enumerate(urls, 1):
            try:
                posts = self._metadata(
                    lambda url=url: self.fanbox.page_posts(url),
                    operation_name="page_posts",
                    creator_id=creator,
                )
                page_was_known = database.record_page(creator, posts)
            except sqlite3.Error:
                raise
            except (AuthenticationError, LocalPersistenceError, OSError):
                raise
            except CloudflareBlocked as exc:
                return "cloudflare_aborted", total_new
            except RetryExhausted as exc:
                return "partial", total_new
            except Exception as exc:
                logger.error(
                    "operation=page_posts author=%s page=%s/%s "
                    "category=permanent_remote attempt=1/1 wait=none "
                    "escalation=skip_author error=%s",
                    creator,
                    page_number,
                    len(urls),
                    exc,
                )
                return "partial", total_new
            if baseline_complete:
                known_pages = known_pages + 1 if page_was_known else 0
                if known_pages >= 2:
                    break

        if not baseline_complete:
            database.complete_baseline(creator)

        for post in database.pending_posts(creator):
            post_id = str(post["id"])
            logger.info("operation=process_post author=%s post=%s", creator, post_id)
            try:
                result = self._attempt_recorded_post(
                    database, creator, post, "失败"
                )
            except CloudflareBlocked as exc:
                return "cloudflare_aborted", total_new
            except RetryExhausted as exc:
                if exc.skips_author:
                    return "partial", total_new
                partial = True
                continue
            if result is not None:
                total_new += result.downloaded_file_count
                partial = partial or result.status is None
            else:
                partial = True

        try:
            repair_outcome, repaired = self._repair_downloads(database, creator)
        except RetryExhausted as exc:
            return "partial", total_new
        total_new += repaired
        if repair_outcome == "cloudflare_aborted":
            return "cloudflare_aborted", total_new
        partial = partial or repair_outcome == "partial"

        logger.info(
            "operation=sync_author author=%s files_downloaded=%s outcome=%s directory=%s",
            creator,
            total_new,
            "partial" if partial else "complete",
            os.path.join(self.root, creator),
        )
        return ("partial" if partial else "complete"), total_new

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
        except (AuthenticationError, LocalPersistenceError, OSError):
            raise
        except CloudflareBlocked:
            raise
        except RetryExhausted:
            raise
        except Exception as exc:
            logger.error(
                "operation=process_post author=%s post=%s category=permanent_remote "
                "attempt=1/1 wait=none escalation=skip_post error=%s detail=%s",
                creator,
                post.get("id"),
                exc,
                failure_message,
            )
            return None
        finally:
            self.sleep_fn(self.post_delay)

    def _repair_downloads(self, database, creator):
        downloaded_file_count = 0
        partial = False
        for post in database.downloaded_posts(creator):
            key = _PostKey(creator, str(post["id"]))
            post_id = key.post_id
            directory_path = post["directory_path"]
            files = database.manifest_files(key)

            if not directory_path or not os.path.isdir(directory_path):
                logger.info(
                    "operation=reset_snapshot author=%s post=%s reason=missing_directory",
                    creator,
                    post_id,
                )
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
                    logger.info(
                        "operation=repair_download author=%s post=%s files=%s",
                        creator,
                        post_id,
                        len(broken),
                    )
                    database.restart_files(key, broken)
                else:
                    logger.info(
                        "operation=reset_snapshot author=%s post=%s reason=missing_manifest",
                        creator,
                        post_id,
                    )
                    database.reset_post(key)

            try:
                result = self._attempt_recorded_post(
                    database,
                    creator,
                    {"id": post_id},
                    f"  帖子 {post_id} 下载修复失败",
                )
            except CloudflareBlocked as exc:
                return "cloudflare_aborted", downloaded_file_count
            except RetryExhausted as exc:
                if exc.skips_author:
                    raise
                partial = True
                continue
            if result is not None:
                downloaded_file_count += result.downloaded_file_count
                partial = partial or result.status is None
            else:
                partial = True
        return ("partial" if partial else "complete"), downloaded_file_count

    @staticmethod
    def _retry_after(value):
        if value is None:
            return None
        try:
            return max(0, float(value))
        except (TypeError, ValueError):
            try:
                date = parsedate_to_datetime(str(value))
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                return max(0, (date - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None

    def _retry(
        self,
        operation,
        *,
        scope: RetryScope,
        operation_name,
        creator_id=None,
        post_id=None,
        file_path=None,
    ):
        """Run one remote operation with the shared ordinary-failure policy."""
        last_error = None
        attempt = 1
        while attempt <= MAX_ATTEMPTS:
            try:
                result = operation()
            except CloudflareBlocked as exc:
                self._cloudflare_streak += 1
                self._cloudflare_blocks += 1
                if self._cloudflare_streak >= MAX_CONSECUTIVE_CLOUDFLARE:
                    logger.error(
                        "operation=%s author=%s post=%s file=%s category=cloudflare "
                        "attempt=%s/%s wait=none escalation=abort_run error=%s",
                        operation_name,
                        creator_id,
                        post_id,
                        file_path,
                        self._cloudflare_streak,
                        MAX_CONSECUTIVE_CLOUDFLARE,
                        exc,
                    )
                    raise
                wait = CLOUDFLARE_WAITS[self._cloudflare_streak - 1]
                logger.warning(
                    "operation=%s author=%s post=%s file=%s category=cloudflare "
                    "attempt=%s/%s wait=%ss escalation=retry error=%s",
                    operation_name,
                    creator_id,
                    post_id,
                    file_path,
                    self._cloudflare_streak,
                    MAX_CONSECUTIVE_CLOUDFLARE,
                    wait,
                    exc,
                )
                wait_with_progress(wait, self.sleep_fn)
            except AuthenticationError as exc:
                self._cloudflare_streak = 0
                exc.operation = operation_name
                exc.creator_id = creator_id
                exc.post_id = post_id
                exc.file_path = file_path
                raise
            except (sqlite3.Error, StateDatabaseError):
                self._cloudflare_streak = 0
                raise
            except RetryableFanboxError as exc:
                self._cloudflare_streak = 0
                last_error = exc
                if attempt >= MAX_ATTEMPTS:
                    logger.error(
                        "operation=%s author=%s post=%s file=%s category=retryable "
                        "attempt=%s/%s wait=none escalation=skip_%s error=%s",
                        operation_name,
                        creator_id,
                        post_id,
                        file_path,
                        attempt,
                        MAX_ATTEMPTS,
                        scope.value,
                        exc,
                    )
                    raise RetryExhausted(
                        scope, operation_name, MAX_ATTEMPTS, exc
                    ) from exc
                retry_after = self._retry_after(
                    getattr(exc, "retry_after", None)
                )
                wait = retry_after if retry_after is not None else RETRY_WAITS[attempt - 1]
                logger.warning(
                    "operation=%s author=%s post=%s file=%s category=retryable "
                    "attempt=%s/%s wait=%ss escalation=retry error=%s",
                    operation_name,
                    creator_id,
                    post_id,
                    file_path,
                    attempt,
                    MAX_ATTEMPTS,
                    f"{wait:g}",
                    exc,
                )
                self.sleep_fn(wait)
                attempt += 1
            except Exception:
                self._cloudflare_streak = 0
                raise
            else:
                self._cloudflare_streak = 0
                if self.api_delay:
                    self.sleep_fn(self.api_delay)
                return result
        raise RetryExhausted(scope, operation_name, MAX_ATTEMPTS, last_error)

    def _metadata(
        self,
        operation,
        *,
        operation_name,
        creator_id,
        post_id=None,
    ):
        return self._retry(
            operation,
            scope=RetryScope.AUTHOR,
            operation_name=operation_name,
            creator_id=creator_id,
            post_id=post_id,
        )

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

        title, files = self._metadata(
            lambda: self.fanbox.post_detail(post_id),
            operation_name="post_detail",
            creator_id=key.creator_id,
            post_id=post_id,
        )
        if not files:
            logger.info(
                "operation=process_post author=%s post=%s outcome=empty",
                key.creator_id,
                post_id,
            )
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

    def _download_file_attempt(self, file, part_path):
        if os.path.exists(part_path):
            os.remove(part_path)
        created = self.fanbox.download_file(file.resource_url, part_path)
        if not created or not os.path.isfile(part_path):
            raise RetryableFanboxError("download did not produce a file")
        actual_size = os.path.getsize(part_path)
        if file.expected_size is not None and actual_size != file.expected_size:
            raise RetryableFanboxError(
                f"download size {actual_size} does not match expected "
                f"{file.expected_size}"
            )
        return actual_size

    def _download_manifest(
        self, database, key, title, directory_path
    ):
        try:
            os.makedirs(directory_path, exist_ok=True)
        except OSError as exc:
            raise LocalPersistenceError(
                "create_directory",
                exc,
                creator_id=key.creator_id,
                post_id=key.post_id,
                file_path=directory_path,
            ) from exc
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

                actual_size = self._retry(
                    lambda: self._download_file_attempt(file, part_path),
                    scope=RetryScope.FILE,
                    operation_name="download_file",
                    creator_id=key.creator_id,
                    post_id=key.post_id,
                    file_path=final_path,
                )

                database.stage_file(
                    key, file.position, actual_size
                )
                try:
                    os.replace(part_path, final_path)
                except OSError as exc:
                    raise LocalPersistenceError(
                        "atomic_rename",
                        exc,
                        creator_id=key.creator_id,
                        post_id=key.post_id,
                        file_path=final_path,
                    ) from exc
                database.complete_file(key, file.position)
                downloaded_file_count += 1
            except sqlite3.Error:
                raise
            except (AuthenticationError, LocalPersistenceError):
                raise
            except OSError as exc:
                raise LocalPersistenceError(
                    "write_file",
                    exc,
                    creator_id=key.creator_id,
                    post_id=key.post_id,
                    file_path=final_path,
                ) from exc
            except CloudflareBlocked:
                raise
            except RetryExhausted:
                raise
            except Exception as exc:
                failed = True
                logger.error(
                    "operation=download_file author=%s post=%s file=%s "
                    "category=permanent_remote attempt=1/1 wait=none "
                    "escalation=skip_post error=%s",
                    key.creator_id,
                    key.post_id,
                    final_path,
                    exc,
                )
            finally:
                self.sleep_fn(self.file_delay)

        if failed or not database.all_files_complete(key):
            logger.warning(
                "operation=process_post author=%s post=%s outcome=partial directory=%s",
                key.creator_id,
                key.post_id,
                directory_path,
            )
            return _PostResult(
                None, downloaded_file_count, title, directory_path
            )

        logger.info(
            "operation=process_post author=%s post=%s outcome=complete files=%s directory=%s",
            key.creator_id,
            key.post_id,
            len(database.manifest_files(key)),
            directory_path,
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
            try:
                os.replace(part_path, final_path)
            except OSError as exc:
                raise LocalPersistenceError(
                    "atomic_rename",
                    exc,
                    creator_id=key.creator_id,
                    post_id=key.post_id,
                    file_path=final_path,
                ) from exc
            database.complete_file(key, file.position)
            return True

        if file.phase != "downloading":
            database.restart_files(key, [file.position])
        return False
