import os
import unittest

from fanbox_dl import extract_post_files, post_directory


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


if __name__ == "__main__":
    unittest.main()
