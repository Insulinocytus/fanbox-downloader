## Context

当前项目是单脚本 Python 应用，依赖声明位于根目录 `requirements.txt`，Dockerfile 使用 `pip install -r requirements.txt`。项目不需要构建可发布的 Python wheel，运行入口仍是 `fanbox_dl.py`；Docker 还需要在构建阶段执行 CloakBrowser 浏览器安装。

## Goals / Non-Goals

**Goals:**

- 让 `pyproject.toml` 成为唯一的直接依赖声明来源。
- 用提交到仓库的 `uv.lock` 固定直接依赖及传递依赖版本。
- 让本地开发、测试和 Docker 构建使用相同的 uv 工作流。
- 保留当前 Python 版本、Docker 非 root 运行、浏览器预安装和容器入口行为。

**Non-Goals:**

- 不把脚本改造成可发布的 Python 包或命令行分发包。
- 不新增任务运行时依赖、测试框架或 CI 平台。
- 不改变 Fanbox 下载、cron 调度、环境变量或 volume 行为。

## Decisions

### 使用 PEP 621 `pyproject.toml` 和 `uv.lock`

在 `[project]` 中声明项目名称、Python 版本约束和现有运行时依赖；使用 `uv lock` 生成并提交锁文件。相比继续维护 `requirements.txt` 或手写多个 requirements 文件，这能让依赖元数据、版本解析和传递依赖来源统一。

直接依赖从现有 `requirements.txt` 迁移，并补充跨平台 `zoneinfo` 所需的 `tzdata`；它不是业务依赖，但 Windows 等没有系统时区数据库的平台需要它。由于项目是脚本应用，使用 uv 的非 package 模式，避免为本项目生成 wheel 或额外的构建配置。

### 使用虚拟环境作为本地和 Docker 安装目标

本地通过 `uv sync --locked` 创建 `.venv`，测试通过 `uv run` 执行。Docker 在 `/app/.venv` 中执行 `uv sync --locked --no-dev`，并将该虚拟环境加入 PATH；这样运行时不会依赖系统 Python 的全局 site-packages。

相比 `uv pip install`，`uv sync --locked` 会同时校验 lockfile，能在依赖文件与锁文件不同步时直接失败。

### Docker 使用固定版本的 uv 引导镜像

Dockerfile 从官方 uv 镜像复制固定版本的 `uv`/`uvx` 二进制，避免在构建阶段额外使用 pip 安装包管理器。依赖层只复制 `pyproject.toml` 和 `uv.lock`，先执行锁定安装，再复制应用代码，以保留 Docker 构建缓存。

CloakBrowser 仍在依赖安装后、切换到非 root 用户前执行安装，并继续使用现有的 `CLOAKBROWSER_CACHE_DIR`。应用启动命令改为使用 `/app/.venv/bin/python`（或 PATH 中的虚拟环境 Python）。

### 删除 `requirements.txt`

迁移完成后删除旧依赖文件，避免两个来源产生漂移。README 只记录 uv 的安装、同步、测试和 Docker 构建命令；`uv.lock` 必须纳入版本控制，`.venv/` 加入 `.gitignore`。

## Risks / Trade-offs

- [开发环境需要安装 uv] → README 提供官方安装方式，并以 `uv sync` 作为唯一初始化步骤。
- [锁文件可能包含平台标记或不同 Python 版本的解析结果] → 在 `pyproject.toml` 明确 Python 版本范围，并在 Docker 与本地验证 `uv sync --locked`。
- [Docker 镜像构建依赖外部 uv 镜像] → 固定 uv 版本；若镜像不可用，可回滚到上一个 Git commit，不影响已构建镜像运行。
- [虚拟环境增加少量镜像层内容] → 只安装 production 依赖并复用 Docker 依赖层缓存；不把开发工具加入生产环境。

## Migration Plan

1. 新增 `pyproject.toml`，将 `requirements.txt` 的依赖迁移到 `[project.dependencies]`。
2. 使用目标 Python 版本运行 `uv lock`，检查并提交 `uv.lock`。
3. 更新 `.gitignore`、Dockerfile 和 README，删除 `requirements.txt`。
4. 执行 `uv sync --locked`、测试和 Docker image 构建验证。
5. 若迁移验证失败，回滚该变更 commit；由于运行时配置和数据目录不变，不需要数据迁移。

## Open Questions

无。
