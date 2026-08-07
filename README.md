# Fanbox 下载器

用于下载已订阅 Fanbox 作者的帖子封面、正文图片和附件。

## 使用方法

1. 解压发布包。
2. 打开 `config.json`。
3. 填写 Cookie、作者 ID 和下载目录。
4. 双击 `fanbox-downloader.exe`。

第一次运行会自动下载约 200MB 的 CloakBrowser 浏览器，请保持网络连接。
运行期间浏览器会停留在 Fanbox 首页，API 请求在后台执行，请不要关闭浏览器窗口。

## 配置

```json
{
  "cookie": "你的 FANBOXSESSID",
  "creators": [
    "作者ID"
  ],
  "download_directory": "downloads",
  "file_delay": 0,
  "post_delay": 5
}
```

### cookie

登录 Fanbox 后按 `F12`，在 Application（应用程序）→ Cookies 中找到 `FANBOXSESSID`，复制它的值。

### creators

作者 ID 可以从作者主页 URL 中找到：

```text
https://www.fanbox.cc/@作者ID
https://作者ID.fanbox.cc/
```

`@` 后面的部分，或者 `.fanbox.cc` 前面的部分，就是需要填写的作者 ID。多个作者在 JSON 数组中用逗号分隔。

### download_directory

支持相对路径：

```json
"download_directory": "downloads"
```

也支持 Windows 绝对路径，建议使用正斜杠：

```json
"download_directory": "D:/FANBOX"
```

### file_delay / post_delay

- `file_delay`：每个图片或附件下载后的等待秒数，默认 `0`。
- `post_delay`：每个帖子处理完成后的等待秒数，默认 `5`。

## 下载目录

```text
downloads/
└── 作者ID/
    └── 帖子ID-帖子标题/
        ├── 帖子ID_0.jpeg
        ├── 帖子ID_1.png
        └── ...
```

已存在的帖子文件夹会被直接跳过。如果某个帖子下载不完整，请删除对应帖子文件夹后重新运行。

## 从源码构建

需要 Python 环境的开发者可以运行：

```powershell
.\build.ps1
```

发布包会生成到：

```text
release/fanbox-downloader-windows-x64.zip
```
