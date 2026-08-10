# Fanbox 下载器

这是一个运行在 Docker 中的 Fanbox 下载服务，用于下载已订阅作者的帖子封面、正文图片和附件。

容器会常驻运行，并按照 cron 计划自动执行下载。每轮任务结束后浏览器都会关闭；下载文件通过 volume 保留，浏览器 profile 不持久化。

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
| `FANBOX_STATE_DIRECTORY` | 否 | 下载目录 | SQLite 下载记录目录；下载目录位于 NFS/SMB 时应配置为本地持久化目录 |
| `FANBOX_FILE_DELAY` | 否 | `0` | 每个文件下载后的等待秒数 |
| `FANBOX_POST_DELAY` | 否 | `10` | 每个帖子处理后的等待秒数 |
| `FANBOX_TIMEZONE` | 否 | `Asia/Shanghai` | IANA 时区名称 |
| `FANBOX_CRON` | 否 | `0 */6 * * *` | 标准五段式 cron 表达式 |
| `FANBOX_RUN_ON_START` | 否 | `false` | 是否在容器启动后立即执行一次 |
| `PUID` | 否 | `10001` | 容器内服务用户的 UID；启动时用于修正可写目录权限 |
| `PGID` | 否 | `10001` | 容器内服务用户的 GID；启动时用于修正可写目录权限 |

`PUID` 和 `PGID` 必须是非零正整数；值与容器中其他用户或组冲突时，容器会在启动阶段失败。启用自动权限修正时不要设置 Compose 的 `user:`，否则 entrypoint 无法以 root 完成初始化。

示例环境变量文件 `.env`：

```dotenv
FANBOX_COOKIE=你的FANBOXSESSID
FANBOX_CREATORS=your-creator-id,another-creator
FANBOX_TIMEZONE=Asia/Shanghai
FANBOX_CRON=0 */6 * * *
FANBOX_RUN_ON_START=false
PUID=10001
PGID=10001
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

docker run -d \
  --name fanbox-downloader \
  --restart unless-stopped \
  --env-file .env \
  -v fanbox-downloads:/data/downloads \
  fanbox-downloader
```

也可以使用宿主机目录：

```bash
mkdir -p downloads

docker run -d \
  --name fanbox-downloader \
  --restart unless-stopped \
  --env-file .env \
  -v "$PWD/downloads:/data/downloads" \
  fanbox-downloader
```

容器启动时会短暂以 root 初始化用户和目录权限，随后以 `PUID:PGID` 运行 Python 程序；Python 程序不会以 root 运行。使用宿主机目录时，将 `PUID`/`PGID` 设置为宿主机用户和组的 ID，entrypoint 会自动修正挂载目录权限。使用该模式时不要在 Compose 中设置 `user:`。

使用 Docker Compose（bind mount + UID/GID 3000）：

```yaml
services:
  fanbox-downloader:
    image: ghcr.io/insulinocytus/fanbox-downloader:latest
    container_name: fanbox-downloader
    restart: unless-stopped
    environment:
      FANBOX_COOKIE: "你的FANBOXSESSID"
      FANBOX_CREATORS: "your-creator-id,another-creator"
      PUID: "3000"
      PGID: "3000"
      FANBOX_TIMEZONE: "Asia/Shanghai"
      FANBOX_CRON: "0 */6 * * *"
      FANBOX_RUN_ON_START: "false"
    volumes:
      - ./data/downloads:/data/downloads
```

使用 `PUID`/`PGID` 时不要同时设置 `user:`。启动前创建宿主机目录：

```bash
mkdir -p data/downloads
docker compose up -d
```

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
- 每轮任务结束后浏览器都会关闭。连续三次 Cloudflare 拦截只中止本轮任务，调度进程会继续等待下一次 cron；认证、SQLite、磁盘或本地写入错误则记录日志并以非零状态终止进程。

例如 `FANBOX_CRON=0 * * * *` 表示每小时整点执行。如果 00:00 的任务执行到 01:05，则 01:00 被跳过，下一次是 02:00。

## 数据目录

下载记录默认与下载产物一起持久化：

```text
/data/downloads/
├── .fanbox-state.sqlite3
└── 作者ID/
    └── 帖子ID-帖子标题/
        ├── 帖子ID_0.jpeg
        ├── 帖子ID_1.png
        └── ...
```

`.fanbox-state.sqlite3` 是作者基线和帖子下载记录的唯一状态来源。容器重建后只要重新挂载同一下载目录，就会继续使用该状态；旧 `.fanbox-state.json` 会被静默忽略，不迁移也不参与判断。数据库无法打开或完整性检查失败时，程序会保留原文件并终止，不会自动删除或覆盖。

数据库同时保存每个帖子的逐文件下载清单、原资源地址和首次完整传输的文件大小。传输先写入 `.part`，记录完整大小后再原子替换正式文件；中断遗留的 `.part` 会在下次运行从零下载。每轮任务会按原下载快照修复缺失或大小变化的文件，清单外的用户文件始终保留并忽略。

SQLite 使用回滚日志而不是 WAL。同一下载目录和状态数据库只支持一个下载器实例，程序不执行多实例检测。如果下载目录位于 NFS、SMB 等远程文件系统，请把 `FANBOX_STATE_DIRECTORY` 指向单独挂载的本地持久化目录，并确保该目录同样在容器重建后保留。

首次运行或 SQLite 下载记录缺失时，服务会扫描全部分页，并逐页保存发现的帖子。只有所有历史分页成功后才完成作者基线；分页中途失败时会保留已发现帖子，并在下轮重新执行完整基线扫描。帖子详情或文件下载失败不会撤销已经成功完成的基线。基线完成后，每轮从最新分页开始，只在连续两个分页全部是已记录帖子后停止；任一未知帖子都会重置连续计数。

服务不会按固定时间自动执行完整扫描。需要执行状态重建时，可以在程序停止后自行移走 `.fanbox-state.sqlite3` 再运行；项目不提供内置备份或迁移功能。

帖子只有在全部文件处理成功后才会被记录为完成。下载中断或部分文件失败时，后续运行会重试该帖子，并跳过已经存在的文件。删除整个帖子文件夹后，服务会重新下载该帖子。

浏览器每轮使用临时 profile，不需要挂载或保存 `.cloak-profile`。升级前如需保留旧版本回滚能力，可以暂时保留旧 profile volume；新版本不会读取它。
