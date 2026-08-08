## Why

当前项目使用 `requirements.txt` 管理依赖，缺少统一的项目元数据和可复现锁定结果；Docker 构建、开发环境和测试环境也需要分别处理依赖。使用 uv 的 `pyproject.toml` 与 `uv.lock` 可以统一依赖声明、锁定版本并简化安装流程。

## What Changes

- 新增 `pyproject.toml`，将运行时依赖、Python 版本要求和项目元数据集中管理。
- 新增并提交 `uv.lock`，确保开发、测试和 Docker 使用一致的依赖版本。
- 删除 `requirements.txt`，不再维护第二套依赖清单。
- 修改 Dockerfile，使用 uv 按锁文件安装依赖，并保留非 root、CloakBrowser 预安装和现有容器运行方式。
- 更新 README 中的依赖安装、测试和 Docker 构建说明，统一使用 `uv` 命令。
- 保持下载器运行时行为、环境变量配置和容器入口不变。

## Capabilities

### New Capabilities

无。本变更只调整依赖管理和构建工具，不新增用户可观察的系统能力。

### Modified Capabilities

无。本变更不改变现有功能要求，仅替换依赖声明和安装方式。

## Impact

- 影响项目根目录的依赖文件、Docker 构建流程、README 和开发命令。
- 影响本地开发者安装依赖和运行测试的方式。
- 影响 Docker image 构建阶段，但不改变运行时环境变量、volume、调度或下载逻辑。
- 不新增运行时 API，不改变现有下载数据格式。
