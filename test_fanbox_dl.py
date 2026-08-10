import json
import os
import tempfile
import unittest
from collections import deque
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

import creator_sync
import fanbox_dl
from creator_sync import (
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


class ConfigurationTests(unittest.TestCase):
    def test_requires_cookie(self):
        with self.assertRaises(SystemExit) as error:
            read_config({"FANBOX_CREATORS": "creator"})

        self.assertIn("FANBOX_COOKIE", str(error.exception))

    def test_reads_environment_defaults(self):
        config = read_config({
            "FANBOX_COOKIE": "test-cookie",
            "FANBOX_CREATORS": " creator-a, creator-b ,, ",
        })

        self.assertEqual(config["cookie"], "test-cookie")
        self.assertEqual(config["creators"], ["creator-a", "creator-b"])
        self.assertEqual(config["download_directory"], "/data/downloads")
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
            with self.subTest(name=name), self.assertRaises(SystemExit) as error:
                read_config(environment)
            self.assertIn(name, str(error.exception))


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
        ), self.assertRaises(SystemExit):
            Fanbox().page_posts("page-1")

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

    def test_sync_owns_metadata_retry(self):
        fanbox = ScriptedFanbox(
            pages={"creator": []},
            failures={("author_pages", "creator"): [FanboxTimeout("timeout")]},
        )
        with tempfile.TemporaryDirectory() as root, patch("creator_sync.time.sleep") as sleep, \
             redirect_stdout(StringIO()):
            CreatorSync(sync_config(root), fanbox).run()

        self.assertEqual(fanbox.calls.count(("author_pages", "creator")), 2)
        sleep.assert_any_call(2)

    def test_cloudflare_wait_prints_progress_every_ten_seconds(self):
        output = StringIO()
        waits = []
        with patch("creator_sync.time.sleep", side_effect=waits.append), redirect_stdout(output):
            wait_with_progress(25)

        self.assertEqual(waits, [10, 10, 5])
        self.assertEqual(
            output.getvalue().splitlines(),
            [
                "  Cloudflare 冷却中,剩余约 25 秒",
                "  Cloudflare 冷却中,剩余约 15 秒",
                "  Cloudflare 冷却中,剩余约 5 秒",
            ],
        )


class CreatorSyncTests(unittest.TestCase):
    def test_complete_author_run_uses_one_interface(self):
        file_url = "https://example/cover.jpg"
        named_url = "https://example/archive"
        fanbox = ScriptedFanbox(
            pages={"creator": ["page-1"]},
            posts={
                "page-1": [
                    {"id": "1"},
                    {"id": "2"},
                    {"id": "3", "isRestricted": True},
                ]
            },
            details={
                "1": ("标题/:测试", [(file_url, ""), (named_url, "a:b.zip")]),
                "2": ("空帖子", []),
            },
            content={file_url: b"cover", named_url: b"archive"},
        )

        with tempfile.TemporaryDirectory() as root, redirect_stdout(StringIO()):
            CreatorSync(sync_config(root), fanbox).run()
            state, valid = creator_sync._load_state(root)
            post_dir = os.path.join(root, "creator", "1-标题__测试")
            with open(os.path.join(post_dir, "1_0.jpg"), "rb") as fp:
                cover = fp.read()
            with open(os.path.join(post_dir, "1_1_a_b.zip"), "rb") as fp:
                archive = fp.read()

        self.assertTrue(valid)
        self.assertEqual(cover, b"cover")
        self.assertEqual(archive, b"archive")
        self.assertEqual(
            state["creators"]["creator"],
            {
                "initialized": True,
                "posts": {"1": "downloaded", "2": "empty", "3": "restricted"},
            },
        )
        self.assertIn(("post_detail", "1"), fanbox.calls)
        self.assertIn(("post_detail", "2"), fanbox.calls)
        self.assertNotIn(("post_detail", "3"), fanbox.calls)
        self.assertEqual(
            [call for call in fanbox.calls if call[0] == "download_file"],
            [("download_file", file_url), ("download_file", named_url)],
        )
        self.assertTrue(fanbox.closed)

    def test_fast_scan_stops_after_two_known_pages(self):
        state = {
            "version": 1,
            "creators": {"creator": {"initialized": True, "posts": {"1": "empty", "2": "empty"}}},
        }
        fanbox = ScriptedFanbox(
            pages={"creator": ["page-1", "page-2", "page-3"]},
            posts={
                "page-1": [{"id": "1"}],
                "page-2": [{"id": "2"}],
                "page-3": [{"id": "3"}],
            },
        )

        with tempfile.TemporaryDirectory() as root, redirect_stdout(StringIO()):
            creator_sync._save_state(root, state)
            CreatorSync(sync_config(root), fanbox).run()
            loaded, _ = creator_sync._load_state(root)

        self.assertEqual(
            [key for operation, key in fanbox.calls if operation == "page_posts"],
            ["page-1", "page-2"],
        )
        self.assertNotIn("3", loaded["creators"]["creator"]["posts"])

    def test_unknown_pages_reset_known_page_overlap(self):
        state = {
            "version": 1,
            "creators": {"creator": {"initialized": True, "posts": {"1": "empty", "2": "empty"}}},
        }
        fanbox = ScriptedFanbox(
            pages={"creator": ["page-1", "page-2", "page-3", "page-4", "page-5"]},
            posts={
                "page-1": [{"id": "10"}],
                "page-2": [{"id": "11"}],
                "page-3": [{"id": "1"}],
                "page-4": [{"id": "2"}],
                "page-5": [{"id": "12"}],
            },
            details={"10": ("10", []), "11": ("11", [])},
        )

        with tempfile.TemporaryDirectory() as root, redirect_stdout(StringIO()):
            creator_sync._save_state(root, state)
            CreatorSync(sync_config(root), fanbox).run()

        self.assertEqual(
            [key for operation, key in fanbox.calls if operation == "page_posts"],
            ["page-1", "page-2", "page-3", "page-4"],
        )
        self.assertEqual(
            [key for operation, key in fanbox.calls if operation == "post_detail"],
            ["10", "11"],
        )

    def test_failed_baseline_retries_full_scan(self):
        url = "https://example/file.jpg"
        fanbox = ScriptedFanbox(
            pages={"creator": ["page-1", "page-2"]},
            posts={"page-1": [{"id": "1"}], "page-2": [{"id": "2"}]},
            details={"1": ("one", [(url, "")]), "2": ("two", [])},
            content={url: b"file"},
            failures={("download_file", url): [RuntimeError("failed")]},
        )

        with tempfile.TemporaryDirectory() as root, redirect_stdout(StringIO()):
            sync = CreatorSync(sync_config(root), fanbox)
            sync.run()
            first, _ = creator_sync._load_state(root)
            sync.run()
            second, _ = creator_sync._load_state(root)

        self.assertFalse(first["creators"]["creator"]["initialized"])
        self.assertTrue(second["creators"]["creator"]["initialized"])
        self.assertEqual(
            [key for operation, key in fanbox.calls if operation == "page_posts"],
            ["page-1", "page-2", "page-1", "page-2"],
        )

    def test_missing_download_directory_is_repaired_before_scan(self):
        state = {
            "version": 1,
            "creators": {"creator": {"initialized": True, "posts": {"123": "downloaded"}}},
        }
        fanbox = ScriptedFanbox(
            pages={"creator": []},
            details={"123": ("title", [])},
        )

        with tempfile.TemporaryDirectory() as root, redirect_stdout(StringIO()):
            creator_sync._save_state(root, state)
            CreatorSync(sync_config(root), fanbox).run()

        self.assertLess(
            fanbox.calls.index(("post_detail", "123")),
            fanbox.calls.index(("author_pages", "creator")),
        )

    def test_failed_directory_repair_is_retried_next_run(self):
        url = "https://example/file.jpg"
        state = {
            "version": 1,
            "creators": {
                "creator": {
                    "initialized": True,
                    "posts": {"1": "empty", "2": "empty", "123": "downloaded"},
                }
            },
        }
        fanbox = ScriptedFanbox(
            pages={"creator": ["page-1", "page-2", "page-3"]},
            posts={
                "page-1": [{"id": "1"}],
                "page-2": [{"id": "2"}],
                "page-3": [{"id": "123"}],
            },
            details={"123": ("title", [(url, "")])},
            content={url: b"file"},
            failures={("download_file", url): [RuntimeError("failed")]},
        )

        with tempfile.TemporaryDirectory() as root, redirect_stdout(StringIO()):
            creator_sync._save_state(root, state)
            sync = CreatorSync(sync_config(root), fanbox)
            sync.run()
            sync.run()
            loaded, _ = creator_sync._load_state(root)

        self.assertEqual(fanbox.calls.count(("download_file", url)), 2)
        self.assertEqual(loaded["creators"]["creator"]["posts"]["123"], "downloaded")
        self.assertTrue(loaded["creators"]["creator"]["initialized"])

    def test_partial_download_retries_and_keeps_completed_file(self):
        first_url = "https://example/1.jpg"
        second_url = "https://example/2.jpg"
        fanbox = ScriptedFanbox(
            pages={"creator": ["page-1"]},
            posts={"page-1": [{"id": "123"}]},
            details={"123": ("title", [(first_url, ""), (second_url, "")])},
            content={first_url: b"one", second_url: b"two"},
            failures={("download_file", second_url): [RuntimeError("failed")]},
        )

        with tempfile.TemporaryDirectory() as root, redirect_stdout(StringIO()):
            sync = CreatorSync(sync_config(root), fanbox)
            sync.run()
            sync.run()
            state, _ = creator_sync._load_state(root)
            post_dir = os.path.join(root, "creator", "123-title")
            with open(os.path.join(post_dir, "123_0.jpg"), "rb") as fp:
                first = fp.read()
            with open(os.path.join(post_dir, "123_1.jpg"), "rb") as fp:
                second = fp.read()

        self.assertEqual((first, second), (b"one", b"two"))
        self.assertEqual(fanbox.calls.count(("download_file", first_url)), 2)
        self.assertEqual(fanbox.calls.count(("download_file", second_url)), 2)
        self.assertEqual(state["creators"]["creator"]["posts"]["123"], "downloaded")


class StateTests(unittest.TestCase):
    def test_state_round_trip_preserves_each_creator(self):
        state = {
            "version": 1,
            "creators": {
                "creator-a": {"initialized": True, "posts": {"1": "downloaded", "2": "empty"}},
                "creator-b": {"initialized": False, "posts": {"3": "restricted"}},
            },
        }

        with tempfile.TemporaryDirectory() as root:
            creator_sync._save_state(root, state)
            loaded, valid = creator_sync._load_state(root)

        self.assertTrue(valid)
        self.assertEqual(loaded, state)

    def test_invalid_state_is_rebuilt_by_sync(self):
        fanbox = ScriptedFanbox(pages={"creator": []})
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, ".fanbox-state.json"), "w", encoding="utf-8") as fp:
                fp.write("not-json")
            output = StringIO()
            with redirect_stdout(output):
                CreatorSync(sync_config(root), fanbox).run()
            state, valid = creator_sync._load_state(root)

        self.assertTrue(valid)
        self.assertTrue(state["creators"]["creator"]["initialized"])
        self.assertIn("状态文件", output.getvalue())

    def test_existing_directory_does_not_create_downloaded_record(self):
        fanbox = ScriptedFanbox(
            pages={"creator": ["page-1"]},
            posts={"page-1": [{"id": "123"}]},
            details={"123": ("new-title", [])},
        )

        with tempfile.TemporaryDirectory() as root, redirect_stdout(StringIO()):
            os.makedirs(os.path.join(root, "creator", "123-old-title"))
            CreatorSync(sync_config(root), fanbox).run()
            state, _ = creator_sync._load_state(root)

        self.assertEqual(state["creators"]["creator"]["posts"]["123"], "empty")

    def test_save_state_replaces_a_temporary_file_atomically(self):
        state = {"version": 1, "creators": {}}
        calls = []

        with tempfile.TemporaryDirectory() as root, patch(
            "creator_sync.os.replace",
            side_effect=lambda source, target: (
                calls.append((source, target)), os.rename(source, target)
            )[-1],
        ):
            creator_sync._save_state(root, state)
            with open(os.path.join(root, ".fanbox-state.json"), encoding="utf-8") as fp:
                saved = json.load(fp)

        self.assertEqual(saved, state)
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(calls[0][0], calls[0][1])
        self.assertTrue(calls[0][0].endswith(".tmp"))


if __name__ == "__main__":
    unittest.main()
