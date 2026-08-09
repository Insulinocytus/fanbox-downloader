import json
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


class IncrementalScanTests(unittest.TestCase):
    def run_with_pages(self, root, state, pages, process_result=("downloaded", 0)):
        with patch("fanbox_dl.OUT", root):
            fanbox_dl.save_state(state)
            with patch.object(fanbox_dl, "post_urls", return_value=list(pages)), \
                 patch.object(
                     fanbox_dl,
                     "api_get",
                     side_effect=lambda url: {"posts": pages[url]},
                 ) as api_get, patch.object(
                     fanbox_dl, "process_post", return_value=process_result
                 ) as process, patch.object(fanbox_dl, "close_browser"), \
                 patch.object(fanbox_dl.time, "sleep"), redirect_stdout(StringIO()):
                fanbox_dl.run_once(["creator"])
            loaded, _ = fanbox_dl.load_state()
        return api_get, process, loaded

    def test_fast_scan_stops_after_two_pages_that_were_already_known(self):
        state = {
            "version": 1,
            "creators": {
                "creator": {
                    "initialized": True,
                    "posts": {"1": "empty", "2": "empty"},
                }
            },
        }
        pages = {
            "page-1": [{"id": "1", "isPinned": True}],
            "page-2": [{"id": "2"}],
            "page-3": [{"id": "3"}],
        }

        with tempfile.TemporaryDirectory() as root:
            api_get, process, loaded = self.run_with_pages(root, state, pages)

        self.assertEqual([call.args[0] for call in api_get.call_args_list], ["page-1", "page-2"])
        process.assert_not_called()
        self.assertNotIn("3", loaded["creators"]["creator"]["posts"])

    def test_unknown_pages_do_not_count_toward_known_page_overlap(self):
        state = {
            "version": 1,
            "creators": {
                "creator": {
                    "initialized": True,
                    "posts": {"1": "empty", "2": "empty"},
                }
            },
        }
        pages = {
            "page-1": [{"id": "10"}],
            "page-2": [{"id": "11"}],
            "page-3": [{"id": "1"}],
            "page-4": [{"id": "2"}],
            "page-5": [{"id": "12"}],
        }

        with tempfile.TemporaryDirectory() as root:
            api_get, process, _ = self.run_with_pages(root, state, pages)

        self.assertEqual(
            [call.args[0] for call in api_get.call_args_list],
            ["page-1", "page-2", "page-3", "page-4"],
        )
        self.assertEqual([call.args[1]["id"] for call in process.call_args_list], ["10", "11"])

    def test_uninitialized_creator_scans_every_page_and_marks_initialized(self):
        pages = {
            "page-1": [{"id": "1"}],
            "page-2": [{"id": "2"}],
            "page-3": [{"id": "3"}],
        }

        with tempfile.TemporaryDirectory() as root, patch("fanbox_dl.OUT", root), \
             patch.object(fanbox_dl, "post_urls", return_value=list(pages)), \
             patch.object(fanbox_dl, "api_get", side_effect=lambda url: {"posts": pages[url]}) as api_get, \
             patch.object(fanbox_dl, "process_post", return_value=("empty", 0)), \
             patch.object(fanbox_dl, "close_browser"), patch.object(fanbox_dl.time, "sleep"), \
             redirect_stdout(StringIO()):
            fanbox_dl.run_once(["creator"])
            state, valid = fanbox_dl.load_state()

        self.assertTrue(valid)
        self.assertEqual(api_get.call_count, 3)
        self.assertTrue(state["creators"]["creator"]["initialized"])

    def test_failed_initialization_retries_full_scan_next_run(self):
        pages = {"page-1": [{"id": "1"}], "page-2": [{"id": "2"}]}

        with tempfile.TemporaryDirectory() as root, patch("fanbox_dl.OUT", root), \
             patch.object(fanbox_dl, "post_urls", return_value=list(pages)), \
             patch.object(fanbox_dl, "api_get", side_effect=lambda url: {"posts": pages[url]}) as api_get, \
             patch.object(
                 fanbox_dl,
                 "process_post",
                 side_effect=[(None, 0), ("empty", 0), ("empty", 0)],
             ), patch.object(fanbox_dl, "close_browser"), patch.object(fanbox_dl.time, "sleep"), \
             redirect_stdout(StringIO()):
            fanbox_dl.run_once(["creator"])
            first, _ = fanbox_dl.load_state()
            fanbox_dl.run_once(["creator"])
            second, _ = fanbox_dl.load_state()

        self.assertFalse(first["creators"]["creator"]["initialized"])
        self.assertTrue(second["creators"]["creator"]["initialized"])
        self.assertEqual(api_get.call_count, 4)

    def test_missing_download_directory_is_retried_before_page_scan(self):
        state = {
            "version": 1,
            "creators": {
                "creator": {
                    "initialized": True,
                    "posts": {"123": "downloaded"},
                }
            },
        }

        with tempfile.TemporaryDirectory() as root:
            _, process, _ = self.run_with_pages(root, state, {})

        process.assert_called_once_with("creator", {"id": "123"})

    def test_failed_missing_directory_repair_is_retried_next_run(self):
        state = {
            "version": 1,
            "creators": {
                "creator": {
                    "initialized": True,
                    "posts": {"1": "empty", "2": "empty", "123": "downloaded"},
                }
            },
        }
        pages = {
            "page-1": [{"id": "1"}],
            "page-2": [{"id": "2"}],
            "page-3": [{"id": "123"}],
        }

        with tempfile.TemporaryDirectory() as root, patch("fanbox_dl.OUT", root):
            fanbox_dl.save_state(state)

            def process_post(creator, post):
                if not os.path.isdir(os.path.join(root, creator, "123-title")):
                    os.makedirs(os.path.join(root, creator, "123-title"))
                    return None, 0
                return "downloaded", 0

            with patch.object(fanbox_dl, "post_urls", return_value=list(pages)), \
                 patch.object(
                     fanbox_dl, "api_get", side_effect=lambda url: {"posts": pages[url]}
                 ), patch.object(fanbox_dl, "process_post", side_effect=process_post) as process, \
                 patch.object(fanbox_dl, "close_browser"), \
                 patch.object(fanbox_dl.time, "sleep"), redirect_stdout(StringIO()):
                fanbox_dl.run_once(["creator"])
                fanbox_dl.run_once(["creator"])

            loaded, _ = fanbox_dl.load_state()

        self.assertEqual([call.args[1]["id"] for call in process.call_args_list], ["123", "123"])
        self.assertEqual(loaded["creators"]["creator"]["posts"]["123"], "downloaded")
        self.assertTrue(loaded["creators"]["creator"]["initialized"])


class PostProcessingTests(unittest.TestCase):
    def test_restricted_post_is_recorded_without_fetching_details(self):
        with patch.object(fanbox_dl, "post_files") as post_files:
            status, downloaded = fanbox_dl.process_post(
                "creator", {"id": "123", "isRestricted": True}
            )

        self.assertEqual((status, downloaded), ("restricted", 0))
        post_files.assert_not_called()

    def test_post_without_files_is_recorded_as_empty(self):
        with patch.object(fanbox_dl, "post_files", return_value=("title", [])), \
             redirect_stdout(StringIO()):
            status, downloaded = fanbox_dl.process_post("creator", {"id": "123"})

        self.assertEqual((status, downloaded), ("empty", 0))

    def test_post_is_downloaded_only_when_every_file_succeeds(self):
        files = [("https://example/1.jpg", ""), ("https://example/2.jpg", "")]
        with patch.object(fanbox_dl, "post_files", return_value=("title", files)), \
             patch.object(fanbox_dl, "dl", side_effect=[True, False]) as download, \
             patch.object(fanbox_dl.time, "sleep"), redirect_stdout(StringIO()):
            status, downloaded = fanbox_dl.process_post("creator", {"id": "123"})

        self.assertEqual((status, downloaded), ("downloaded", 1))
        self.assertEqual(download.call_count, 2)

    def test_partial_failure_stays_unfinished_and_retry_skips_existing_files(self):
        files = [("https://example/1.jpg", ""), ("https://example/2.jpg", "")]
        with patch.object(fanbox_dl, "post_files", return_value=("title", files)), \
             patch.object(
                 fanbox_dl,
                 "dl",
                 side_effect=[True, RuntimeError("failed"), False, True],
             ) as download, patch.object(fanbox_dl.time, "sleep"), \
             redirect_stdout(StringIO()):
            first = fanbox_dl.process_post("creator", {"id": "123"})
            second = fanbox_dl.process_post("creator", {"id": "123"})

        self.assertEqual(first, (None, 1))
        self.assertEqual(second, ("downloaded", 1))
        self.assertEqual(download.call_count, 4)


class PostStateTests(unittest.TestCase):
    def test_state_round_trip_preserves_each_creator(self):
        state = {
            "version": 1,
            "creators": {
                "creator-a": {
                    "initialized": True,
                    "posts": {"1": "downloaded", "2": "empty"},
                },
                "creator-b": {
                    "initialized": False,
                    "posts": {"3": "restricted"},
                },
            },
        }

        with tempfile.TemporaryDirectory() as root, patch("fanbox_dl.OUT", root):
            fanbox_dl.save_state(state)
            loaded, valid = fanbox_dl.load_state()

        self.assertTrue(valid)
        self.assertEqual(loaded, state)

    def test_invalid_state_warns_and_returns_empty_state(self):
        with tempfile.TemporaryDirectory() as root, patch("fanbox_dl.OUT", root):
            with open(os.path.join(root, ".fanbox-state.json"), "w", encoding="utf-8") as fp:
                fp.write("not-json")

            output = StringIO()
            with redirect_stdout(output):
                state, valid = fanbox_dl.load_state()

        self.assertFalse(valid)
        self.assertEqual(state, {"version": 1, "creators": {}})
        self.assertIn("状态文件", output.getvalue())

    def test_invalid_state_shape_warns_and_returns_empty_state(self):
        invalid = {
            "version": 1,
            "creators": {
                "creator": {"initialized": True, "posts": {"1": {}}}
            },
        }

        with tempfile.TemporaryDirectory() as root, patch("fanbox_dl.OUT", root):
            with open(os.path.join(root, ".fanbox-state.json"), "w", encoding="utf-8") as fp:
                json.dump(invalid, fp)

            with redirect_stdout(StringIO()):
                state, valid = fanbox_dl.load_state()

        self.assertFalse(valid)
        self.assertEqual(state, {"version": 1, "creators": {}})

    def test_migrates_existing_post_directories_into_new_state(self):
        with tempfile.TemporaryDirectory() as root, patch("fanbox_dl.OUT", root):
            os.makedirs(os.path.join(root, "creator", "123-old-title"))
            os.makedirs(os.path.join(root, "creator", "456-another-title"))
            os.makedirs(os.path.join(root, "creator", "not-a-post"))
            state = {"version": 1, "creators": {}}

            fanbox_dl.migrate_existing_posts(state, ["creator"])

        self.assertEqual(
            state["creators"]["creator"],
            {
                "initialized": False,
                "posts": {"123": "downloaded", "456": "downloaded"},
            },
        )

    def test_finds_downloaded_posts_whose_directory_was_deleted(self):
        state = {
            "version": 1,
            "creators": {
                "creator": {
                    "initialized": True,
                    "posts": {"123": "downloaded", "456": "downloaded", "789": "empty"},
                }
            },
        }

        with tempfile.TemporaryDirectory() as root, patch("fanbox_dl.OUT", root):
            os.makedirs(os.path.join(root, "creator", "456-renamed-title"))

            missing = fanbox_dl.missing_downloaded_posts(state, "creator")

        self.assertEqual(missing, ["123"])

    def test_save_state_replaces_a_temporary_file_atomically(self):
        state = {"version": 1, "creators": {}}
        calls = []

        with tempfile.TemporaryDirectory() as root, patch("fanbox_dl.OUT", root), \
             patch("fanbox_dl.os.replace", side_effect=lambda source, target: (
                 calls.append((source, target)), os.rename(source, target)
             )[-1]):
            fanbox_dl.save_state(state)

            with open(os.path.join(root, ".fanbox-state.json"), encoding="utf-8") as fp:
                saved = json.load(fp)

        self.assertEqual(saved, state)
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(calls[0][0], calls[0][1])
        self.assertTrue(calls[0][0].endswith(".tmp"))


if __name__ == "__main__":
    unittest.main()
