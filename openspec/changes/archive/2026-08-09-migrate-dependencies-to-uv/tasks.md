## 1. 项目依赖定义

- [x] 1.1 新增 `pyproject.toml`，声明项目元数据、与 Docker 一致的 Python 版本范围、现有运行时依赖及跨平台时区依赖 `tzdata`，并配置为脚本项目而非可打包项目。
- [x] 1.2 使用目标 Python 环境运行 `uv lock`，检查 `uv.lock` 包含直接依赖及传递依赖，并将锁文件纳入版本控制。
- [x] 1.3 删除根目录 `requirements.txt`，在 `.gitignore` 中忽略 `.venv/` 和 uv 的本地缓存目录。

## 2. 构建和开发工作流

- [x] 2.1 修改 Dockerfile，从固定版本的官方 uv 镜像复制 uv，使用 `uv sync --locked --no-dev` 安装依赖，并保留 CloakBrowser 预安装、非 root 用户、数据 volume 和现有入口。
- [x] 2.2 更新 README，说明 uv 安装、`uv sync`、`uv run` 测试命令和基于锁文件的 Docker 构建流程，移除 requirements.txt 相关说明。

## 3. 验证和清理

- [x] 3.1 执行 `uv lock --check`、`uv sync --locked` 和 `uv run python -m unittest discover -v`，确认锁文件、环境同步和现有测试通过。
- [x] 3.2 搜索项目配置与文档，确认不再引用 `requirements.txt`、裸 `pip install` 或未锁定的依赖安装方式。
- [ ] 3.3 构建 Docker image，验证容器使用锁定依赖启动，且现有配置校验、下载目录和浏览器 profile 行为不变。
