import json
import os
import tempfile
import unittest
from unittest.mock import patch

from fanbox_dl import (
    extract_post_files,
    post_directory,
    post_downloaded,
    read_config,
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


if __name__ == "__main__":
    unittest.main()
