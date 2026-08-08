# Fanbox 下载器

这是一个运行在 Docker 中的 Fanbox 下载服务，用于下载已订阅作者的帖子封面、正文图片和附件。

容器会常驻运行，并按照 cron 计划自动执行下载。每轮任务结束后浏览器会关闭，但浏览器 profile 和下载文件会通过 volume 保留。

## 构建镜像

```bash
docker build -t fanbox-downloader .
```

镜像会在构建阶段使用 `uv.lock` 安装依赖，并安装 CloakBrowser 和 Linux 浏览器依赖；容器首次启动时不需要再下载浏览器。

## 本地开发

安装 [uv](https://docs.astral.sh/uv/getting-started/installation/) 后，在项目根目录执行：

```bash
uv sync --locked
uv run python -m unittest discover -v
```

依赖声明位于 `pyproject.toml`，锁文件为 `uv.lock`。依赖变更后使用 `uv lock` 更新锁文件，再用 `uv sync --locked` 验证。

## 配置环境变量

`FANBOX_COOKIE` 和 `FANBOX_CREATORS` 是必填项。

| 环境变量 | 是否必填 | 默认值 | 说明 |
|---|---:|---|---|
| `FANBOX_COOKIE` | 是 | 无 | Fanbox 的 `FANBOXSESSID` Cookie |
| `FANBOX_CREATORS` | 是 | 无 | 作者 ID，多个作者用逗号分隔 |
| `FANBOX_DOWNLOAD_DIRECTORY` | 否 | `/data/downloads` | 容器内下载目录 |
| `FANBOX_FILE_DELAY` | 否 | `0` | 每个文件下载后的等待秒数 |
| `FANBOX_POST_DELAY` | 否 | `10` | 每个帖子处理后的等待秒数 |
| `FANBOX_TIMEZONE` | 否 | `Asia/Shanghai` | IANA 时区名称 |
| `FANBOX_CRON` | 否 | `0 */6 * * *` | 标准五段式 cron 表达式 |
| `FANBOX_RUN_ON_START` | 否 | `false` | 是否在容器启动后立即执行一次 |

示例环境变量文件 `.env`：

```dotenv
FANBOX_COOKIE=你的FANBOXSESSID
FANBOX_CREATORS=cowmopcat,another-creator
FANBOX_TIMEZONE=Asia/Shanghai
FANBOX_CRON=0 */6 * * *
FANBOX_RUN_ON_START=false
```

`.env` 只是 Docker 的环境变量注入方式，不会被应用读取，也不要提交到 Git。

### 获取 Cookie

登录 Fanbox 后按 `F12`，在浏览器开发者工具的 Application（应用程序）→ Cookies 中找到 `FANBOXSESSID`，复制它的值。

Cookie 是登录凭证，不要写入 Dockerfile、镜像、日志或公开仓库。Cookie 失效后，更新环境变量并重启容器。

### 作者 ID

作者 ID 可以从作者主页 URL 中找到：

```text
https://www.fanbox.cc/@作者ID
https://作者ID.fanbox.cc/
```

`@` 后面或 `.fanbox.cc` 前面的部分就是作者 ID。

## 启动容器

推荐使用 Docker named volume 保存数据：

```bash
docker volume create fanbox-downloads
docker volume create fanbox-profile

docker run -d \
  --name fanbox-downloader \
  --restart unless-stopped \
  --env-file .env \
  -v fanbox-downloads:/data/downloads \
  -v fanbox-profile:/data/.cloak-profile \
  fanbox-downloader
```

也可以使用宿主机目录：

```bash
mkdir -p downloads .cloak-profile

docker run -d \
  --name fanbox-downloader \
  --restart unless-stopped \
  --env-file .env \
  -v "$PWD/downloads:/data/downloads" \
  -v "$PWD/.cloak-profile:/data/.cloak-profile" \
  fanbox-downloader
```

镜像使用非 root 用户运行。使用宿主机目录时，请确保挂载目录对容器用户可写；named volume 通常不需要额外处理。

查看日志：

```bash
docker logs -f fanbox-downloader
```

## 调度规则

- `FANBOX_RUN_ON_START=true`：容器启动后立即执行一轮，然后等待 cron。
- `FANBOX_RUN_ON_START=false`：启动后等待下一次 cron。
- 同时只允许一轮下载任务运行。
- 如果任务执行时间超过下一个计划时间，错过的那一轮直接跳过，不排队、不补执行。
- 容器重启后不会补执行停机期间错过的任务。
- 任务结束或失败后，浏览器都会关闭；之后的计划仍然可以继续执行。

例如 `FANBOX_CRON=0 * * * *` 表示每小时整点执行。如果 00:00 的任务执行到 01:05，则 01:00 被跳过，下一次是 02:00。

## 数据目录

```text
/data/downloads/
└── 作者ID/
    └── 帖子ID-帖子标题/
        ├── 帖子ID_0.jpeg
        ├── 帖子ID_1.png
        └── ...

/data/.cloak-profile/
└── CloakBrowser 持久化 profile
```

已存在的帖子文件夹会被跳过。如果帖子下载不完整，请删除对应帖子文件夹后再执行。
