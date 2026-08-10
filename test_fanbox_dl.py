import os
import shutil
import sqlite3
import tempfile
import unittest
from collections import deque
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

import creator_sync
import fanbox_dl
from creator_sync import (
    AuthenticationError,
    CloudflareBlocked,
    CreatorSync,
    FanboxTimeout,
    RetryableFanboxError,
    wait_with_progress,
)
from fanbox_dl import Fanbox, extract_post_files, next_scheduled_time, read_config, run_scheduler

creator_sync.API_DELAY = 0


class ScriptedFanbox:
    """用字典脚本化 Fanbox 单次领域操作的测试适配器。"""

    def __init__(self, *, pages=None, posts=None, details=None, content=None, failures=None):
        self.pages = pages or {}
        self.posts = posts or {}
        self.details = details or {}
        self.content = content or {}
        self.failures = {key: deque(value) for key, value in (failures or {}).items()}
        self.calls = []
        self.closed = False

    def _call(self, operation, key):
        self.calls.append((operation, key))
        scripted = self.failures.get((operation, key))
        if scripted:
            failure = scripted.popleft()
            if failure is not None:
                raise failure

    def author_pages(self, creator):
        self._call("author_pages", creator)
        return list(self.pages.get(creator, []))

    def page_posts(self, page_url):
        self._call("page_posts", page_url)
        return list(self.posts.get(page_url, []))

    def post_detail(self, post_id):
        self._call("post_detail", post_id)
        return self.details[post_id]

    def download_file(self, url, path):
        self._call("download_file", url)
        if os.path.exists(path):
            return False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fp:
            fp.write(self.content[url])
        return True

    def close(self):
        self.calls.append(("close", None))
        self.closed = True


def sync_config(root, creators=None):
    return {
        "creators": creators or ["creator"],
        "download_directory": root,
        "file_delay": 0,
        "post_delay": 0,
    }


def no_sleep(_seconds):
    pass


def retry_failures(message):
    return [RetryableFanboxError(message)] * creator_sync.MAX_ATTEMPTS


class ConfigurationTests(unittest.TestCase):
    def test_requires_cookie(self):
        with self.assertLogs("fanbox_dl", level="ERROR") as logs, \
             self.assertRaises(SystemExit):
            read_config({"FANBOX_CREATORS": "creator"})

        self.assertIn("FANBOX_COOKIE", "\n".join(logs.output))

    def test_reads_environment_defaults(self):
        config = read_config({
            "FANBOX_COOKIE": "test-cookie",
            "FANBOX_CREATORS": " creator-a, creator-b ,, ",
        })

        self.assertEqual(config["cookie"], "test-cookie")
        self.assertEqual(config["creators"], ["creator-a", "creator-b"])
        self.assertEqual(config["download_directory"], "/data/downloads")
        self.assertEqual(config["state_directory"], "/data/downloads")
        self.assertEqual(config["file_delay"], 0.0)
        self.assertEqual(config["post_delay"], 10.0)
        self.assertEqual(config["timezone"].key, "Asia/Shanghai")
        self.assertEqual(config["cron"], "0 */6 * * *")
        self.assertFalse(config["run_on_start"])

    def test_rejects_invalid_environment_values(self):
        cases = [
            ({"FANBOX_COOKIE": "", "FANBOX_CREATORS": "creator"}, "FANBOX_COOKIE"),
            ({"FANBOX_COOKIE": "cookie", "FANBOX_CREATORS": ""}, "FANBOX_CREATORS"),
            ({"FANBOX_COOKIE": "cookie", "FANBOX_CREATORS": "creator", "FANBOX_FILE_DELAY": "-1"}, "FANBOX_FILE_DELAY"),
            ({"FANBOX_COOKIE": "cookie", "FANBOX_CREATORS": "creator", "FANBOX_TIMEZONE": "invalid/zone"}, "FANBOX_TIMEZONE"),
            ({"FANBOX_COOKIE": "cookie", "FANBOX_CREATORS": "creator", "FANBOX_CRON": "not cron"}, "FANBOX_CRON"),
            ({"FANBOX_COOKIE": "cookie", "FANBOX_CREATORS": "creator", "FANBOX_RUN_ON_START": "maybe"}, "FANBOX_RUN_ON_START"),
        ]

        for environment, name in cases:
            with self.subTest(name=name), self.assertLogs("fanbox_dl", level="ERROR") as logs, \
                 self.assertRaises(SystemExit):
                read_config(environment)
            self.assertIn(name, "\n".join(logs.output))

    def test_reports_all_configuration_errors_without_exposing_cookie(self):
        secret = "super-secret-cookie"
        environment = {
            "FANBOX_COOKIE": secret,
            "FANBOX_CREATORS": "",
            "FANBOX_DOWNLOAD_DIRECTORY": "",
            "FANBOX_FILE_DELAY": "-1",
            "FANBOX_POST_DELAY": "not-a-number",
            "FANBOX_TIMEZONE": "invalid/zone",
            "FANBOX_CRON": "not cron",
            "FANBOX_RUN_ON_START": "maybe",
        }

        with self.assertLogs("fanbox_dl", level="ERROR") as logs, \
             self.assertRaises(SystemExit):
            read_config(environment)

        output = "\n".join(logs.output)
        for name in (
            "FANBOX_CREATORS",
            "FANBOX_DOWNLOAD_DIRECTORY",
            "FANBOX_FILE_DELAY",
            "FANBOX_POST_DELAY",
            "FANBOX_TIMEZONE",
            "FANBOX_CRON",
            "FANBOX_RUN_ON_START",
        ):
            self.assertIn(name, output)
        self.assertNotIn(secret, output)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        fanbox_dl._shutdown_requested = False

    def tearDown(self):
        fanbox_dl._shutdown_requested = False

    def test_next_cron_time_uses_configured_timezone(self):
        now = datetime(2024, 1, 1, 0, 5, tzinfo=ZoneInfo("Asia/Shanghai"))

        result = next_scheduled_time("0 * * * *", ZoneInfo("Asia/Shanghai"), now)

        self.assertEqual(result, datetime(2024, 1, 1, 1, 0, tzinfo=ZoneInfo("Asia/Shanghai")))

    def test_next_cron_time_after_long_run_skips_elapsed_occurrence(self):
        finished = datetime(2024, 1, 1, 1, 5, tzinfo=ZoneInfo("Asia/Shanghai"))

        result = next_scheduled_time("0 * * * *", ZoneInfo("Asia/Shanghai"), finished)

        self.assertEqual((result.hour, result.minute), (2, 0))

    def test_startup_run_true_calls_parameterless_sync_before_waiting(self):
        calls = []
        config = read_config({
            "FANBOX_COOKIE": "cookie",
            "FANBOX_CREATORS": "creator",
            "FANBOX_RUN_ON_START": "true",
        })

        class Sync:
            def run(self):
                calls.append("run")
                fanbox_dl._shutdown_requested = True

        run_scheduler(config, sync=Sync(), sleep_fn=lambda _: self.fail("should not wait"))

        self.assertEqual(calls, ["run"])

    def test_startup_run_false_waits_for_schedule(self):
        calls = []
        waits = []
        config = read_config({
            "FANBOX_COOKIE": "cookie",
            "FANBOX_CREATORS": "creator",
            "FANBOX_CRON": "0 * * * *",
            "FANBOX_RUN_ON_START": "false",
        })

        class Sync:
            def run(self):
                calls.append("run")
                fanbox_dl._shutdown_requested = True

        times = iter([
            datetime(2024, 1, 1, 0, 5, tzinfo=ZoneInfo("Asia/Shanghai")),
            datetime(2024, 1, 1, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
            datetime(2024, 1, 1, 1, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        ])
        with redirect_stdout(StringIO()):
            run_scheduler(
                config,
                sync=Sync(),
                now_fn=lambda: next(times),
                sleep_fn=waits.append,
            )

        self.assertEqual(calls, ["run"])
        self.assertEqual(len(waits), 1)

    def test_cloudflare_aborted_run_does_not_stop_scheduler(self):
        calls = []
        config = read_config({
            "FANBOX_COOKIE": "cookie",
            "FANBOX_CREATORS": "creator",
            "FANBOX_CRON": "0 * * * *",
            "FANBOX_RUN_ON_START": "true",
        })

        class Sync:
            def run(self):
                calls.append("run")
                if len(calls) == 2:
                    fanbox_dl._shutdown_requested = True
                return "cloudflare_aborted"

        times = iter([
            datetime(2024, 1, 1, 0, 5, tzinfo=ZoneInfo("Asia/Shanghai")),
            datetime(2024, 1, 1, 1, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        ])
        with redirect_stdout(StringIO()):
            run_scheduler(
                config,
                sync=Sync(),
                now_fn=lambda: next(times),
                sleep_fn=lambda _: None,
            )

        self.assertEqual(calls, ["run", "run"])


class BrowserLifecycleTests(unittest.TestCase):
    def setUp(self):
        fanbox_dl._browser_context = None
        fanbox_dl._browser_page = None

    def tearDown(self):
        fanbox_dl._browser_context = None
        fanbox_dl._browser_page = None

    def test_browser_fetch_uses_ephemeral_headless_context(self):
        class FakePage:
            def goto(self, *_args, **_kwargs):
                pass

            def evaluate(self, *_args, **_kwargs):
                return {"status": 200, "text": '{"body":{}}'}

        class FakeContext:
            pages = []

            def add_cookies(self, cookies):
                self.cookies = cookies

            def new_page(self):
                self.page = FakePage()
                return self.page

            def close(self):
                self.closed = True

        context = FakeContext()
        with patch.object(fanbox_dl, "launch_context", return_value=context) as launch, \
             patch.object(fanbox_dl, "_cookie_value", "cookie"), \
             redirect_stdout(StringIO()):
            result = fanbox_dl.browser_fetch("https://api.fanbox.cc/test")

        launch.assert_called_once_with(headless=True, humanize=True)
        self.assertEqual(context.cookies[0]["value"], "cookie")
        self.assertEqual(result["status"], 200)

        Fanbox().close()
        self.assertTrue(context.closed)
        self.assertIsNone(fanbox_dl._browser_context)
        self.assertIsNone(fanbox_dl._browser_page)


class FanboxAdapterTests(unittest.TestCase):
    def test_extracts_cover_html_images_and_files(self):
        post = {
            "id": "12387654",
            "coverImageUrl": "https://pixiv.pximg.net/cover.jpg",
            "body": {
                "html": '<a href="https://downloads.fanbox.cc/images/post/1/html.png">图</a>',
                "images": [{"originalUrl": "https://downloads.fanbox.cc/images/post/1/image.png"}],
                "imageMap": {"2": {"originalUrl": "https://downloads.fanbox.cc/images/post/1/map.png"}},
                "files": [{"url": "https://downloads.fanbox.cc/files/post/1/file.zip", "name": "file.zip"}],
                "fileMap": {"2": {"url": "https://downloads.fanbox.cc/files/post/1/map.psd", "name": "map.psd"}},
            },
        }

        resources = extract_post_files(post)

        self.assertEqual(
            [url for url, _ in resources],
            [
                "https://pixiv.pximg.net/cover.jpg",
                "https://downloads.fanbox.cc/images/post/1/html.png",
                "https://downloads.fanbox.cc/images/post/1/image.png",
                "https://downloads.fanbox.cc/images/post/1/map.png",
                "https://downloads.fanbox.cc/files/post/1/file.zip",
                "https://downloads.fanbox.cc/files/post/1/map.psd",
            ],
        )

    def test_metadata_operation_uses_browser_instead_of_curl(self):
        result = {"status": 200, "text": '{"body":{"posts":[{"id":"1"}]}}'}
        with patch.object(fanbox_dl, "browser_fetch", return_value=result), \
             patch.object(fanbox_dl, "sess", side_effect=AssertionError("curl must not handle metadata")):
            posts = Fanbox().page_posts("page-1")

        self.assertEqual(posts, [{"id": "1"}])

    def test_pagination_rejects_missing_or_invalid_lists(self):
        for body, operation in [
            ({}, lambda fanbox: fanbox.author_pages("creator")),
            ({"pageUrls": None}, lambda fanbox: fanbox.author_pages("creator")),
            ({}, lambda fanbox: fanbox.page_posts("page-1")),
            ({"posts": {}}, lambda fanbox: fanbox.page_posts("page-1")),
            ({"posts": [{}]}, lambda fanbox: fanbox.page_posts("page-1")),
            ({"pageUrls": [None]}, lambda fanbox: fanbox.author_pages("creator")),
        ]:
            with self.subTest(body=body), patch.object(
                fanbox_dl, "_api_get_once", return_value=body
            ), self.assertRaises(RetryableFanboxError):
                operation(Fanbox())

    def test_metadata_operation_classifies_failures(self):
        cases = [
            ({"status": 0, "text": "AbortError"}, FanboxTimeout),
            ({"status": 403, "text": "block_ip"}, CloudflareBlocked),
            ({"status": 500, "text": "failed"}, RuntimeError),
        ]
        for result, error in cases:
            with self.subTest(error=error.__name__), patch.object(
                fanbox_dl, "browser_fetch", return_value=result
            ), self.assertRaises(error):
                Fanbox().page_posts("page-1")

        with patch.object(fanbox_dl, "browser_fetch", side_effect=OSError("failed")), \
             self.assertRaises(RetryableFanboxError):
            Fanbox().page_posts("page-1")
        with patch.object(
            fanbox_dl, "browser_fetch", return_value={"status": 401, "text": "expired"}
        ), self.assertRaises(AuthenticationError):
            Fanbox().page_posts("page-1")

        with patch.object(
            fanbox_dl, "browser_fetch", return_value={"status": 404, "text": "missing"}
        ), self.assertRaises(RuntimeError) as error:
            Fanbox().page_posts("page-1")
        self.assertNotIsInstance(error.exception, RetryableFanboxError)

        with patch.object(
            fanbox_dl,
            "browser_fetch",
            return_value={"status": 200, "text": '{"body": {}}'},
        ), self.assertRaises(RetryableFanboxError):
            Fanbox().post_detail("1")

    def test_file_operation_uses_curl_and_writes_bytes(self):
        class Response:
            status_code = 200
            text = ""

            def raise_for_status(self):
                pass

            def iter_content(self, _size):
                return [b"file", b" bytes"]

        class Session:
            def get(self, *_args, **_kwargs):
                return Response()

        with tempfile.TemporaryDirectory() as root, patch.object(fanbox_dl, "sess", return_value=Session()):
            path = os.path.join(root, "post", "file.bin")
            created = Fanbox().download_file("https://example/file", path)
            with open(path, "rb") as fp:
                content = fp.read()

        self.assertTrue(created)
        self.assertEqual(content, b"file bytes")

    def test_file_operation_classifies_cloudflare_challenge(self):
        class Response:
            status_code = 403
            text = "Cloudflare challenge required"

        class Session:
            def get(self, *_args, **_kwargs):
                return Response()

        with tempfile.TemporaryDirectory() as root, \
             patch.object(fanbox_dl, "sess", return_value=Session()), \
             self.assertRaises(CloudflareBlocked):
            Fanbox().download_file(
                "https://example/file",
                os.path.join(root, "post", "file.bin"),
            )

    def test_sync_owns_metadata_retry(self):
        fanbox = ScriptedFanbox(
            pages={"creator": []},
            failures={("author_pages", "creator"): [FanboxTimeout("timeout")]},
        )
        with tempfile.TemporaryDirectory() as root, patch("creator_sync.time.sleep") as sleep, \
             self.assertLogs("creator_sync", level="WARNING") as logs:
            CreatorSync(sync_config(root), fanbox).run()

        self.assertEqual(fanbox.calls.count(("author_pages", "creator")), 2)
        sleep.assert_any_call(10)
        failure = "\n".join(logs.output)
        self.assertIn("operation=author_pages", failure)
        self.assertIn("author=creator", failure)
        self.assertIn("category=retryable", failure)
        self.assertIn("attempt=1/4", failure)
        self.assertIn("wait=10s", failure)
        self.assertIn("escalation=retry", failure)

    def test_exhausted_retry_uses_four_attempts_and_backoff_sequence(self):
        waits = []
        fanbox = ScriptedFanbox(
            failures={("author_pages", "creator"): retry_failures("failed")},
        )
        with tempfile.TemporaryDirectory() as root, redirect_stdout(StringIO()):
            CreatorSync(sync_config(root), fanbox, sleep_fn=waits.append).run()

        self.assertEqual(fanbox.calls.count(("author_pages", "creator")), 4)
        self.assertEqual(waits, [10, 30, 60])

    def test_cloudflare_metadata_uses_special_cooling_retry(self):
        waits = []
        fanbox = ScriptedFanbox(
            pages={"creator": []},
            failures={
                ("author_pages", "creator"): [
                    CloudflareBlocked("blocked"),
                    None,
                ]
            },
        )
        with tempfile.TemporaryDirectory() as root, redirect_stdout(StringIO()):
            CreatorSync(sync_config(root), fanbox, sleep_fn=waits.append).run()

        self.assertEqual(fanbox.calls.count(("author_pages", "creator")), 2)
        self.assertEqual(sum(waits), 30)

    def test_nonretryable_operation_is_attempted_once(self):
        fanbox = ScriptedFanbox(
            failures={("author_pages", "creator"): [RuntimeError("permanent")]},
        )
        with tempfile.TemporaryDirectory() as root, redirect_stdout(StringIO()):
            CreatorSync(sync_config(root), fanbox, sleep_fn=no_sleep).run()

        self.assertEqual(fanbox.calls.count(("author_pages", "creator")), 1)

    def test_cloudflare_wait_prints_progress_every_ten_seconds(self):
        waits = []
        with patch("creator_sync.time.sleep", side_effect=waits.append), \
             self.assertLogs("creator_sync", level="INFO") as logs:
            wait_with_progress(25)

        self.assertEqual(waits, [10, 10, 5])
        self.assertEqual(
            [line.rsplit("wait=", 1)[1] for line in logs.output],
            [
                "25s escalation=retry",
                "15s escalation=retry",
                "5s escalation=retry",
            ],
        )


def query_download_database(directory, query, parameters=()):
    path = os.path.join(directory, creator_sync.DATABASE_FILE)
    connection = sqlite3.connect(path)
    try:
        return list(connection.execute(query, parameters))
    finally:
        connection.close()


def read_download_state(directory):
    creators = dict(query_download_database(
        directory, "SELECT creator_id, baseline_complete FROM creators"
    ))
    post_rows = query_download_database(
        directory,
        "SELECT creator_id, post_id, status, title, directory_path FROM posts",
    )
    posts = {
        (creator, post_id): {
            "status": status,
            "title": title,
            "directory_path": directory_path,
        }
        for creator, post_id, status, title, directory_path in post_rows
    }
    return creators, posts


def read_manifest(directory, post_id):
    rows = query_download_database(
        directory,
        """
        SELECT position, local_name, resource_url, expected_size, phase
        FROM files WHERE creator_id = 'creator' AND post_id = ?
        ORDER BY position
        """,
        (post_id,),
    )
    return [
        {
            "position": position,
            "local_name": local_name,
            "resource_url": resource_url,
            "expected_size": expected_size,
            "phase": phase,
        }
        for position, local_name, resource_url, expected_size, phase in rows
    ]


class CreatorSyncTests(unittest.TestCase):
    def test_non_cloudflare_failures_reset_task_cloudflare_streak(self):
        waits = []
        fanbox = ScriptedFanbox(
            pages={"creator": []},
            failures={
                ("author_pages", "creator"): [
                    CloudflareBlocked("blocked-1"),
                    RetryableFanboxError("ordinary-1"),
                    CloudflareBlocked("blocked-2"),
                    RetryableFanboxError("ordinary-2"),
                    CloudflareBlocked("blocked-3"),
                    None,
                ]
            },
        )

        with tempfile.TemporaryDirectory() as root, \
             self.assertLogs("creator_sync", level="INFO"):
            result = CreatorSync(
                sync_config(root), fanbox, sleep_fn=waits.append
            ).run()

        self.assertEqual(result.outcome, "complete")
        self.assertEqual(result.cloudflare_blocks, 3)
        self.assertEqual(waits, [10, 10, 10, 10, 10, 10, 10, 30, 10, 10, 10])

    def test_third_consecutive_cloudflare_block_aborts_run_and_skips_authors(self):
        fanbox = ScriptedFanbox(
            pages={"first": [], "second": []},
            failures={
                ("author_pages", "first"): [
                    CloudflareBlocked("blocked-1"),
                    CloudflareBlocked("blocked-2"),
                    CloudflareBlocked("blocked-3"),
                ]
            },
        )

        with tempfile.TemporaryDirectory() as root, \
             self.assertLogs("creator_sync", level="INFO") as logs:
            result = CreatorSync(
                sync_config(root, creators=["first", "second"]),
                fanbox,
                sleep_fn=no_sleep,
            ).run()

        self.assertEqual(result.outcome, "cloudflare_aborted")
        self.assertEqual(result.creators_partial, 1)
        self.assertEqual(result.creators_skipped, 1)
        self.assertEqual(result.cloudflare_blocks, 3)
        self.assertEqual(fanbox.calls.count(("author_pages", "first")), 3)
        self.assertNotIn(("author_pages", "second"), fanbox.calls)
        summary = "\n".join(logs.output)
        self.assertIn("outcome=cloudflare_aborted", summary)
        self.assertIn("creators_skipped=1", summary)

    def test_each_run_starts_with_zero_cloudflare_streak(self):
        fanbox = ScriptedFanbox(
            pages={"creator": []},
            failures={
                ("author_pages", "creator"): [
                    CloudflareBlocked("blocked-1"),
                    CloudflareBlocked("blocked-2"),
                    CloudflareBlocked("blocked-3"),
                ]
            },
        )

        with tempfile.TemporaryDirectory() as root, \
             self.assertLogs("creator_sync", level="INFO"):
            sync = CreatorSync(sync_config(root), fanbox, sleep_fn=no_sleep)
            first = sync.run()
            second = sync.run()

        self.assertEqual(first.outcome, "cloudflare_aborted")
        self.assertEqual(second.outcome, "complete")
        self.assertEqual(second.cloudflare_blocks, 0)

    def test_authentication_error_terminates_without_retrying(self):
        fanbox = ScriptedFanbox(
            failures={
                ("author_pages", "creator"): [
                    creator_sync.AuthenticationError("登录失效")
                ]
            }
        )

        with tempfile.TemporaryDirectory() as root, \
             self.assertLogs("creator_sync", level="ERROR") as logs, \
             self.assertRaises(SystemExit) as exit_error:
            CreatorSync(sync_config(root), fanbox, sleep_fn=no_sleep).run()

        self.assertEqual(exit_error.exception.code, 1)
        self.assertEqual(fanbox.calls.count(("author_pages", "creator")), 1)
        self.assertTrue(fanbox.closed)
        output = "\n".join(logs.output)
        self.assertIn("category=authentication", output)
        self.assertIn("operation=author_pages", output)
        self.assertIn("author=creator", output)
        self.assertIn("escalation=terminate_process", output)

    def test_atomic_rename_error_terminates_process(self):
        url = "https://example/file.bin"
        fanbox = ScriptedFanbox(
            pages={"creator": ["page-1"]},
            posts={"page-1": [{"id": "1"}]},
            details={"1": ("title", [(url, "")])},
            content={url: b"content"},
        )

        with tempfile.TemporaryDirectory() as root, \
             patch("creator_sync.os.replace", side_effect=OSError("disk full")), \
             self.assertLogs("creator_sync", level="ERROR") as logs, \
             self.assertRaises(SystemExit) as exit_error:
            CreatorSync(sync_config(root), fanbox, sleep_fn=no_sleep).run()

        self.assertEqual(exit_error.exception.code, 1)
        self.assertTrue(fanbox.closed)
        output = "\n".join(logs.output)
        self.assertIn("category=local_persistence", output)
        self.assertIn("operation=atomic_rename", output)
        self.assertIn("author=creator", output)
        self.assertIn("post=1", output)
        self.assertIn("file=", output)
        self.assertIn("escalation=terminate_process", output)

    def test_complete_baseline_persists_downloaded_empty_and_restricted_posts(self):
        file_url = "https://example/cover.jpg"
        named_url = "https://example/archive"
        fanbox = ScriptedFanbox(
            pages={"creator": ["page-1"]},
            posts={"page-1": [
                {"id": "1"}, {"id": "2"}, {"id": "3", "isRestricted": True},
            ]},
            details={
                "1": ("标题/:测试", [(file_url, ""), (named_url, "a:b.zip")]),
                "2": ("空帖子", []),
            },
            content={file_url: b"cover", named_url: b"archive"},
        )

        with tempfile.TemporaryDirectory() as root, redirect_stdout(StringIO()):
            CreatorSync(sync_config(root), fanbox).run()
            creators, posts = read_download_state(root)
            manifest = read_manifest(root, "1")
            post_dir = os.path.join(root, "creator", "1-标题__测试")
            with open(os.path.join(post_dir, "1_0.jpg"), "rb") as fp:
                cover = fp.read()
            with open(os.path.join(post_dir, "1_1_a_b.zip"), "rb") as fp:
                archive = fp.read()

        self.assertEqual(creators, {"creator": 1})
        self.assertEqual((cover, archive), (b"cover", b"archive"))
        self.assertEqual(
            {post_id: row["status"] for (_, post_id), row in posts.items()},
            {"1": "downloaded", "2": "empty", "3": "restricted"},
        )
        self.assertNotIn(("post_detail", "3"), fanbox.calls)
        self.assertEqual(
            manifest,
            [
                {
                    "position": 0,
                    "local_name": "1_0.jpg",
                    "resource_url": file_url,
                    "expected_size": len(b"cover"),
                    "phase": "complete",
                },
                {
                    "position": 1,
                    "local_name": "1_1_a_b.zip",
                    "resource_url": named_url,
                    "expected_size": len(b"archive"),
                    "phase": "complete",
                },
            ],
        )
        self.assertTrue(fanbox.closed)

    def test_failed_baseline_page_is_persisted_and_full_scan_retries(self):
        fanbox = ScriptedFanbox(
            pages={"creator": ["page-1", "page-2"]},
            posts={"page-1": [{"id": "1"}], "page-2": [{"id": "2"}]},
            details={"1": ("one", []), "2": ("two", [])},
            failures={("page_posts", "page-2"): retry_failures("failed page")},
        )

        with tempfile.TemporaryDirectory() as root, redirect_stdout(StringIO()):
            sync = CreatorSync(sync_config(root), fanbox, sleep_fn=no_sleep)
            sync.run()
            first_creators, first_posts = read_download_state(root)
            first_calls = list(fanbox.calls)
            sync.run()
            second_creators, second_posts = read_download_state(root)

        self.assertEqual(first_creators, {"creator": 0})
        self.assertEqual(first_posts[("creator", "1")]["status"], "downloading")
        self.assertNotIn(("creator", "2"), first_posts)
        self.assertFalse(any(call[0] == "post_detail" for call in first_calls))
        self.assertEqual(second_creators, {"creator": 1})
        self.assertEqual(
            {post_id: row["status"] for (_, post_id), row in second_posts.items()},
            {"1": "empty", "2": "empty"},
        )
        self.assertEqual(
            [key for operation, key in fanbox.calls if operation == "page_posts"],
            ["page-1", "page-2", "page-2", "page-2", "page-2", "page-1", "page-2"],
        )

    def test_malformed_page_entry_skips_only_current_author(self):
        fanbox = ScriptedFanbox(
            pages={"creator": ["page-1"], "second": ["second-page"]},
            posts={"page-1": [{}], "second-page": []},
        )

        with tempfile.TemporaryDirectory() as root, redirect_stdout(StringIO()):
            config = sync_config(root, creators=["creator", "second"])
            CreatorSync(config, fanbox).run()
            creators, posts = read_download_state(root)

        self.assertEqual(creators, {"creator": 0, "second": 1})
        self.assertEqual(posts, {})
        self.assertIn(("author_pages", "second"), fanbox.calls)

    def test_post_detail_failure_does_not_revoke_successful_baseline(self):
        fanbox = ScriptedFanbox(
            pages={"creator": ["page-1"], "second": ["second-page"]},
            posts={"page-1": [{"id": "1"}]},
            details={"1": ("one", [])},
            failures={("post_detail", "1"): retry_failures("detail failed")},
        )

        with tempfile.TemporaryDirectory() as root, redirect_stdout(StringIO()):
            CreatorSync(
                sync_config(root, creators=["creator", "second"]),
                fanbox,
                sleep_fn=no_sleep,
            ).run()
            creators, posts = read_download_state(root)

        self.assertEqual(creators, {"creator": 1, "second": 1})
        self.assertEqual(posts[("creator", "1")]["status"], "downloading")
        self.assertIn(("author_pages", "second"), fanbox.calls)

    def test_incremental_scan_stops_after_two_fully_known_pages(self):
        initial = ScriptedFanbox(
            pages={"creator": ["old"]},
            posts={"old": [{"id": "1"}, {"id": "2"}]},
            details={"1": ("one", []), "2": ("two", [])},
        )
        incremental = ScriptedFanbox(
            pages={"creator": ["page-1", "page-2", "page-3"]},
            posts={
                "page-1": [{"id": "1"}],
                "page-2": [{"id": "2"}],
                "page-3": [{"id": "3"}],
            },
        )

        with tempfile.TemporaryDirectory() as root, redirect_stdout(StringIO()):
            CreatorSync(sync_config(root), initial).run()
            CreatorSync(sync_config(root), incremental).run()
            _, posts = read_download_state(root)

        self.assertEqual(
            [key for operation, key in incremental.calls if operation == "page_posts"],
            ["page-1", "page-2"],
        )
        self.assertNotIn(("creator", "3"), posts)

    def test_unknown_pages_reset_incremental_known_page_count(self):
        initial = ScriptedFanbox(
            pages={"creator": ["old"]},
            posts={"old": [{"id": "1"}, {"id": "2"}]},
            details={"1": ("one", []), "2": ("two", [])},
        )
        incremental = ScriptedFanbox(
            pages={"creator": ["page-1", "page-2", "page-3", "page-4", "page-5"]},
            posts={
                "page-1": [{"id": "10"}],
                "page-2": [{"id": "11"}],
                "page-3": [{"id": "1"}],
                "page-4": [{"id": "2"}],
                "page-5": [{"id": "12"}],
            },
            details={"10": ("ten", []), "11": ("eleven", [])},
        )

        with tempfile.TemporaryDirectory() as root, redirect_stdout(StringIO()):
            CreatorSync(sync_config(root), initial).run()
            CreatorSync(sync_config(root), incremental).run()

        self.assertEqual(
            [key for operation, key in incremental.calls if operation == "page_posts"],
            ["page-1", "page-2", "page-3", "page-4"],
        )

    def test_permanent_file_error_is_attempted_once(self):
        url = "https://example/missing.jpg"
        fanbox = ScriptedFanbox(
            pages={"creator": ["page-1"]},
            posts={"page-1": [{"id": "123"}]},
            details={"123": ("title", [(url, "")])},
            failures={("download_file", url): [RuntimeError("HTTP 404")]},
        )

        with tempfile.TemporaryDirectory() as root, \
             self.assertLogs("creator_sync", level="ERROR") as logs:
            CreatorSync(sync_config(root), fanbox, sleep_fn=no_sleep).run()
            _, posts = read_download_state(root)

        self.assertEqual(fanbox.calls.count(("download_file", url)), 1)
        self.assertEqual(posts[("creator", "123")]["status"], "downloading")
        failure = "\n".join(logs.output)
        self.assertIn("operation=download_file", failure)
        self.assertIn("author=creator", failure)
        self.assertIn("post=123", failure)
        self.assertIn("category=permanent_remote", failure)
        self.assertIn("escalation=skip_post", failure)
        self.assertNotIn(url, failure)

    def test_partial_download_retries_without_reopening_the_baseline(self):
        first_url = "https://example/1.jpg"
        second_url = "https://example/2.jpg"
        failed_url = "https://example/failed.jpg"
        fanbox = ScriptedFanbox(
            pages={"creator": ["page-1"]},
            posts={"page-1": [{"id": "124"}, {"id": "123"}]},
            details={
                "123": ("title", [(first_url, ""), (second_url, "")]),
                "124": ("failed", [(failed_url, "")]),
            },
            content={first_url: b"one", second_url: b"two", failed_url: b"later"},
            failures={("download_file", failed_url): retry_failures("failed")},
        )

        with tempfile.TemporaryDirectory() as root, redirect_stdout(StringIO()):
            sync = CreatorSync(sync_config(root), fanbox, sleep_fn=no_sleep)
            sync.run()
            first_state = read_download_state(root)
            sync.run()
            _, posts = read_download_state(root)
            post_dir = os.path.join(root, "creator", "123-title")
            with open(os.path.join(post_dir, "123_0.jpg"), "rb") as fp:
                first = fp.read()
            with open(os.path.join(post_dir, "123_1.jpg"), "rb") as fp:
                second = fp.read()

        self.assertEqual(first_state[0], {"creator": 1})
        self.assertEqual(first_state[1][("creator", "124")]["status"], "downloading")
        self.assertEqual(first_state[1][("creator", "123")]["status"], "downloaded")
        self.assertEqual((first, second), (b"one", b"two"))
        self.assertEqual(posts[("creator", "124")]["status"], "downloaded")
        self.assertEqual(posts[("creator", "123")]["status"], "downloaded")

    def test_missing_and_size_changed_files_repair_from_original_manifest(self):
        first_url = "https://example/original-1.jpg"
        second_url = "https://example/original-2.jpg"
        fanbox = ScriptedFanbox(
            pages={"creator": ["page-1"]},
            posts={"page-1": [{"id": "123"}]},
            details={"123": ("title", [(first_url, ""), (second_url, "")])},
            content={first_url: b"one", second_url: b"two"},
        )

        with tempfile.TemporaryDirectory() as root, redirect_stdout(StringIO()):
            sync = CreatorSync(sync_config(root), fanbox)
            sync.run()
            post_dir = os.path.join(root, "creator", "123-title")
            first_path = os.path.join(post_dir, "123_0.jpg")
            second_path = os.path.join(post_dir, "123_1.jpg")
            extra_path = os.path.join(post_dir, "notes.txt")

            os.remove(first_path)
            with open(extra_path, "wb") as fp:
                fp.write(b"user file")
            fanbox.details["123"] = (
                "edited",
                [("https://example/replacement.jpg", "")],
            )
            fanbox.calls.clear()
            sync.run()
            missing_repair_calls = list(fanbox.calls)

            with open(second_path, "wb") as fp:
                fp.write(b"truncated")
            fanbox.calls.clear()
            sync.run()
            changed_repair_calls = list(fanbox.calls)

            with open(first_path, "rb") as fp:
                first = fp.read()
            with open(second_path, "rb") as fp:
                second = fp.read()
            with open(extra_path, "rb") as fp:
                extra = fp.read()

        self.assertEqual(
            [call for call in missing_repair_calls if call[0] == "download_file"],
            [("download_file", first_url)],
        )
        self.assertEqual(
            [call for call in changed_repair_calls if call[0] == "download_file"],
            [("download_file", second_url)],
        )
        self.assertFalse(any(call[0] == "post_detail" for call in missing_repair_calls))
        self.assertFalse(any(call[0] == "post_detail" for call in changed_repair_calls))
        self.assertEqual((first, second, extra), (b"one", b"two", b"user file"))

    def test_repair_detail_exhaustion_skips_author_and_continues(self):
        url = "https://example/repair.jpg"
        fanbox = ScriptedFanbox(
            pages={"creator": ["page-1"], "second": ["second-page"]},
            posts={"page-1": [{"id": "123"}]},
            details={"123": ("title", [(url, "")])},
            content={url: b"bytes"},
        )

        with tempfile.TemporaryDirectory() as root, redirect_stdout(StringIO()):
            config = sync_config(root, creators=["creator", "second"])
            sync = CreatorSync(config, fanbox, sleep_fn=no_sleep)
            sync.run()

            shutil.rmtree(os.path.join(root, "creator", "123-title"))
            fanbox.failures[("post_detail", "123")] = deque(
                retry_failures("detail failed")
            )
            fanbox.calls.clear()
            sync.run()
            creators, posts = read_download_state(root)

        self.assertEqual(creators, {"creator": 1, "second": 1})
        self.assertEqual(posts[("creator", "123")]["status"], "downloading")
        self.assertIn(("author_pages", "second"), fanbox.calls)

    def test_interrupted_transfer_leaves_only_part_and_restarts_from_zero(self):
        url = "https://example/file.jpg"

        class InterruptingFanbox(ScriptedFanbox):
            attempts = 0

            def download_file(self, resource_url, path):
                self._call("download_file", resource_url)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                self.attempts += 1
                if self.attempts <= 4:
                    with open(path, "wb") as fp:
                        fp.write(b"partial")
                    raise RetryableFanboxError("interrupted")
                with open(path, "wb") as fp:
                    fp.write(self.content[resource_url])
                return True

        fanbox = InterruptingFanbox(
            pages={"creator": ["page-1"]},
            posts={"page-1": [{"id": "123"}]},
            details={"123": ("title", [(url, "")])},
            content={url: b"complete bytes"},
        )

        with tempfile.TemporaryDirectory() as root, redirect_stdout(StringIO()):
            sync = CreatorSync(sync_config(root), fanbox, sleep_fn=no_sleep)
            sync.run()
            post_dir = os.path.join(root, "creator", "123-title")
            final_path = os.path.join(post_dir, "123_0.jpg")
            part_path = final_path + ".part"
            first_manifest = read_manifest(root, "123")
            first_files = (os.path.exists(final_path), os.path.exists(part_path))

            sync.run()
            second_manifest = read_manifest(root, "123")
            with open(final_path, "rb") as fp:
                completed = fp.read()

        self.assertEqual(first_files, (False, True))
        self.assertEqual(first_manifest[0]["phase"], "downloading")
        self.assertEqual(second_manifest[0]["phase"], "complete")
        self.assertEqual(completed, b"complete bytes")

    def test_staged_part_is_promoted_without_redownloading(self):
        url = "https://example/file.jpg"
        fanbox = ScriptedFanbox(
            pages={"creator": ["page-1"]},
            posts={"page-1": [{"id": "123"}]},
            details={"123": ("title", [(url, "")])},
            content={url: b"file bytes"},
        )
        original_replace = os.replace
        interrupted = False

        def interrupt_first_promotion(source, target):
            nonlocal interrupted
            if source.endswith(".part") and not interrupted:
                interrupted = True
                raise OSError("rename interrupted")
            return original_replace(source, target)

        with tempfile.TemporaryDirectory() as root, redirect_stdout(StringIO()):
            sync = CreatorSync(sync_config(root), fanbox)
            with patch("creator_sync.os.replace", side_effect=interrupt_first_promotion), \
                 self.assertLogs("creator_sync", level="ERROR"), \
                 self.assertRaises(SystemExit) as exit_error:
                sync.run()
            staged = read_manifest(root, "123")
            calls_after_failure = fanbox.calls.count(("download_file", url))

            with patch("creator_sync.os.replace", side_effect=OSError("recovery rename failed")), \
                 self.assertLogs("creator_sync", level="ERROR") as recovery_logs, \
                 self.assertRaises(SystemExit):
                sync.run()

            sync.run()
            completed = read_manifest(root, "123")
            calls_after_recovery = fanbox.calls.count(("download_file", url))

        self.assertEqual(exit_error.exception.code, 1)
        self.assertEqual(staged[0]["phase"], "staged")
        self.assertEqual(completed[0]["phase"], "complete")
        self.assertEqual((calls_after_failure, calls_after_recovery), (1, 1))
        self.assertIn("operation=atomic_rename", "\n".join(recovery_logs.output))


class StateDatabaseTests(unittest.TestCase):
    def test_database_defaults_to_download_directory_and_uses_safe_pragmas(self):
        fanbox = ScriptedFanbox(pages={"creator": []})
        with tempfile.TemporaryDirectory() as root, redirect_stdout(StringIO()):
            CreatorSync(sync_config(root), fanbox, sleep_fn=no_sleep).run()
            path = os.path.join(root, creator_sync.DATABASE_FILE)
            database = creator_sync._DownloadState(path)
            try:
                pragmas = (
                    database.connection.execute("PRAGMA journal_mode").fetchone()[0],
                    database.connection.execute("PRAGMA synchronous").fetchone()[0],
                    database.connection.execute("PRAGMA foreign_keys").fetchone()[0],
                )
            finally:
                database.close()

        self.assertEqual(pragmas, ("delete", 2, 1))

    def test_state_directory_moves_only_the_database(self):
        fanbox = ScriptedFanbox(pages={"creator": []})
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as state:
            config = sync_config(root)
            config["state_directory"] = state
            with redirect_stdout(StringIO()):
                CreatorSync(config, fanbox).run()

            self.assertTrue(os.path.isfile(os.path.join(state, creator_sync.DATABASE_FILE)))
            self.assertFalse(os.path.exists(os.path.join(root, creator_sync.DATABASE_FILE)))

    def test_legacy_json_is_silently_ignored(self):
        fanbox = ScriptedFanbox(pages={"creator": []})
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, ".fanbox-state.json"), "w", encoding="utf-8") as fp:
                fp.write("not-json")
            output = StringIO()
            with redirect_stdout(output):
                CreatorSync(sync_config(root), fanbox).run()
            creators, _ = read_download_state(root)

        self.assertEqual(creators, {"creator": 1})
        self.assertNotIn("json", output.getvalue().lower())
        self.assertNotIn("状态文件", output.getvalue())

    def test_corrupt_database_is_preserved_and_terminates(self):
        fanbox = ScriptedFanbox()
        corrupt = b"this is not sqlite"
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, creator_sync.DATABASE_FILE)
            with open(path, "wb") as fp:
                fp.write(corrupt)
            with self.assertLogs("creator_sync", level="ERROR") as logs, \
                 self.assertRaises(SystemExit) as exit_error:
                CreatorSync(sync_config(root), fanbox).run()
            with open(path, "rb") as fp:
                after = fp.read()

        self.assertEqual(exit_error.exception.code, 1)
        self.assertEqual(after, corrupt)
        self.assertIn("operation=open_database", "\n".join(logs.output))
        self.assertIn("category=local_persistence", "\n".join(logs.output))
        self.assertTrue(fanbox.closed)

    def test_new_author_defaults_to_incomplete_when_listing_fails(self):
        fanbox = ScriptedFanbox(
            failures={("author_pages", "creator"): retry_failures("failed")}
        )
        with tempfile.TemporaryDirectory() as root, redirect_stdout(StringIO()):
            CreatorSync(sync_config(root), fanbox, sleep_fn=no_sleep).run()
            creators, _ = read_download_state(root)

        self.assertEqual(creators, {"creator": 0})


if __name__ == "__main__":
    unittest.main()
