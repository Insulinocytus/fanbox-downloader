## Why（为什么要改）

当前项目通过 PyInstaller 打包成 Windows 可执行文件，需要手动启动，无法方便地长期部署在服务器上。改成 Docker 常驻服务后，可以通过环境变量配置，并按照指定时区和 cron 计划自动执行下载任务。

## What Changes（改动内容）

- **BREAKING**：移除 PyInstaller 和 Windows ZIP 发布方式，改为构建 Docker image。
- **BREAKING**：移除 `config.json`，所有运行配置改用环境变量。
- `FANBOX_COOKIE` 和 `FANBOX_CREATORS` 都必须设置；缺少任意一个时，容器在启动阶段直接报错退出。
- 在容器中以 headless 模式运行 CloakBrowser。
- 容器启动后保持运行，按照带时区的 cron 表达式执行下载。
- 增加一个环境变量，用来决定容器启动时是否立即执行一次下载。
- 同一时间最多执行一个下载任务；如果上一次任务还没结束，则跳过期间错过的计划时间，不排队补执行。
- 每次下载任务结束后关闭浏览器，同时通过 Docker volume 保留 `.cloak-profile` 和下载文件。
- 更新 README，说明 Docker 构建、环境变量、volume 和运行方式。

## Capabilities（能力）

### New Capabilities（新增能力）

- `scheduled-container-downloader`：Docker 部署、环境变量配置、持久化数据，以及按照 cron 定时且不重叠地执行下载。

### Modified Capabilities（修改的已有能力）

无。

## Impact（影响范围）

- 影响 `fanbox_dl.py` 的配置读取、程序入口、浏览器生命周期和定时执行逻辑。
- 影响测试和 README 文档。
- 删除 PyInstaller spec、构建脚本和 Windows 发布流程。
- 新增 Docker 构建文件、依赖文件和定时调度依赖。
- 部署者需要把原来 `config.json` 中的值改成环境变量，并挂载下载目录和 `.cloak-profile`。
