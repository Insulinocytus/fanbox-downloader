import json
import os
import tempfile
import unittest

import fanbox_dl
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from fanbox_dl import (
    extract_post_files,
    post_directory,
    post_downloaded,
    read_config,
    wait_with_progress,
)


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
                os.path.join(
                    os.path.dirname(__file__),
                    "downloads",
                    "creator",
                    "12387654-标题__测试",
                )
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

    def test_read_config_resolves_relative_download_directory(self):
        with tempfile.TemporaryDirectory() as root:
            config_path = os.path.join(root, "config.json")
            with open(config_path, "w", encoding="utf-8") as fp:
                json.dump(
                    {
                        "cookie": "test-cookie",
                        "creators": ["creator-a", "creator-b"],
                        "download_directory": "downloads/fanbox",
                        "file_delay": 2.5,
                        "post_delay": 12,
                    },
                    fp,
                )

            config = read_config(config_path, root)

            self.assertEqual(config["cookie"], "test-cookie")
            self.assertEqual(config["creators"], ["creator-a", "creator-b"])
            self.assertEqual(
                config["download_directory"],
                os.path.join(root, "downloads", "fanbox"),
            )
            self.assertEqual(config["file_delay"], 2.5)
            self.assertEqual(config["post_delay"], 12.0)

    def test_read_config_keeps_absolute_download_directory(self):
        with tempfile.TemporaryDirectory() as root:
            absolute = os.path.join(root, "absolute-downloads")
            config_path = os.path.join(root, "config.json")
            with open(config_path, "w", encoding="utf-8") as fp:
                json.dump(
                    {
                        "cookie": "test-cookie",
                        "creators": ["creator-a"],
                        "download_directory": absolute,
                    },
                    fp,
                )

            config = read_config(config_path, root)

            self.assertEqual(config["download_directory"], absolute)
            self.assertEqual(config["file_delay"], 2.0)
            self.assertEqual(config["post_delay"], 10.0)

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
        ) as fetch, patch.object(fanbox_dl.time, "sleep") as sleep:
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
