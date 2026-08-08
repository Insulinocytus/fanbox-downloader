import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from unittest.mock import patch
from zoneinfo import ZoneInfo

import fanbox_dl

from fanbox_dl import (
    extract_post_files,
    next_scheduled_time,
    post_directory,
    post_downloaded,
    read_config,
    run_scheduler,
    wait_with_progress,
)


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

        self.assertEqual(result.hour, 2)
        self.assertEqual(result.minute, 0)

    def test_startup_run_true_runs_before_waiting(self):
        calls = []
        config = read_config({
            "FANBOX_COOKIE": "cookie",
            "FANBOX_CREATORS": "creator",
            "FANBOX_RUN_ON_START": "true",
        })

        def run_once(creators):
            calls.append(creators)
            fanbox_dl._shutdown_requested = True

        run_scheduler(config, run_once_fn=run_once, sleep_fn=lambda _: self.fail("should not wait"))

        self.assertEqual(calls, [["creator"]])

    def test_startup_run_false_waits_for_schedule(self):
        calls = []
        waits = []
        config = read_config({
            "FANBOX_COOKIE": "cookie",
            "FANBOX_CREATORS": "creator",
            "FANBOX_CRON": "0 * * * *",
            "FANBOX_RUN_ON_START": "false",
        })

        def run_once(creators):
            calls.append(creators)
            fanbox_dl._shutdown_requested = True

        times = iter([
            datetime(2024, 1, 1, 0, 5, tzinfo=ZoneInfo("Asia/Shanghai")),
            datetime(2024, 1, 1, 0, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
            datetime(2024, 1, 1, 1, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        ])
        with redirect_stdout(StringIO()):
            run_scheduler(
                config,
                run_once_fn=run_once,
                now_fn=lambda: next(times),
                sleep_fn=waits.append,
            )

        self.assertEqual(calls, [["creator"]])
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

        launch.assert_called_once_with(
            headless=True,
            humanize=True,
        )
        self.assertEqual(context.cookies[0]["value"], "cookie")
        self.assertEqual(result["status"], 200)

        fanbox_dl.close_browser()
        self.assertTrue(context.closed)
        self.assertIsNone(fanbox_dl._browser_context)
        self.assertIsNone(fanbox_dl._browser_page)

    def test_run_once_closes_browser_after_failure(self):
        with patch.object(fanbox_dl, "post_urls", side_effect=RuntimeError("failed")), \
             patch.object(fanbox_dl, "close_browser") as close, \
             redirect_stdout(StringIO()):
            fanbox_dl.run_once(["creator"])

        close.assert_called_once_with()


class FanboxExtractionTests(unittest.TestCase):
    def test_extracts_cover_html_images_and_files(self):
        post = {
            "id": "12387654",
            "title": "测试帖子",
            "coverImageUrl": "https://pixiv.pximg.net/cover.jpg",
            "body": {
                "html": (
                    '<a href="https://downloads.fanbox.cc/images/post/12387654/html.png">'
                    "正文图片</a>"
                ),
                "images": [
                    {"originalUrl": "https://downloads.fanbox.cc/images/post/12387654/image.png"}
                ],
                "imageMap": {
                    "image-2": {
                        "originalUrl": "https://downloads.fanbox.cc/images/post/12387654/image-map.png"
                    }
                },
                "files": [
                    {
                        "url": "https://downloads.fanbox.cc/files/post/12387654/file.zip",
                        "name": "file.zip",
                    }
                ],
                "fileMap": {
                    "file-2": {
                        "url": "https://downloads.fanbox.cc/files/post/12387654/file-map.psd",
                        "name": "file-map.psd",
                    }
                },
            },
        }

        resources = extract_post_files(post)

        self.assertEqual(
            [url for url, _ in resources],
            [
                "https://pixiv.pximg.net/cover.jpg",
                "https://downloads.fanbox.cc/images/post/12387654/html.png",
                "https://downloads.fanbox.cc/images/post/12387654/image.png",
                "https://downloads.fanbox.cc/images/post/12387654/image-map.png",
                "https://downloads.fanbox.cc/files/post/12387654/file.zip",
                "https://downloads.fanbox.cc/files/post/12387654/file-map.psd",
            ],
        )

    def test_post_directory_uses_id_and_sanitized_title(self):
        path = post_directory("creator", "12387654", '标题/:测试')

        self.assertEqual(
            os.path.normpath(path),
            os.path.normpath(
                os.path.join("/data/downloads", "creator", "12387654-标题__测试")
            ),
        )

    def test_existing_post_directory_is_detected(self):
        with tempfile.TemporaryDirectory() as root, patch("fanbox_dl.OUT", root):
            path = post_directory("creator", "12387654", "测试帖子")
            os.makedirs(path)

            self.assertTrue(
                post_downloaded(
                    "creator",
                    {"id": "12387654", "title": "测试帖子"},
                )
            )

    def test_api_get_uses_browser_fetch_instead_of_curl_session(self):
        result = {
            "status": 200,
            "text": '{"body":{"ok":true}}',
        }
        with patch.object(fanbox_dl, "browser_fetch", return_value=result), \
             patch.object(
                 fanbox_dl,
                 "sess",
                 side_effect=AssertionError("curl session must not handle API"),
             ):
            self.assertEqual(fanbox_dl.api_get("https://api.fanbox.cc/test"), {"ok": True})

    def test_api_get_retries_browser_fetch_timeout(self):
        with patch.object(
            fanbox_dl,
            "browser_fetch",
            side_effect=[
                {"status": 0, "text": "AbortError"},
                {"status": 200, "text": '{"body":{"ok":true}}'},
            ],
        ) as fetch, patch.object(fanbox_dl.time, "sleep") as sleep, \
             redirect_stdout(StringIO()):
            self.assertEqual(fanbox_dl.api_get("https://api.fanbox.cc/test"), {"ok": True})

        self.assertEqual(fetch.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_cloudflare_wait_prints_progress_every_ten_seconds(self):
        output = StringIO()
        waits = []
        with patch("fanbox_dl.time.sleep", side_effect=waits.append):
            with redirect_stdout(output):
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


if __name__ == "__main__":
    unittest.main()
