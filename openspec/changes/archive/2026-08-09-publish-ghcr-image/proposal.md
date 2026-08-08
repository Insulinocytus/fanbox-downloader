## Why

项目已有可运行的 Docker 镜像，但目前没有自动化发布流程，使用者需要手动构建和推送镜像。通过 GitHub Actions 发布到 GitHub Container Registry（GHCR），可以让 `main` 分支和版本 tag 的代码自动产生可拉取的镜像。

## What Changes

- 新增 GitHub Actions workflow，在 push 到 `main` 或创建 Git tag 时构建 Docker image。
- 将镜像推送到 `ghcr.io/<owner>/<repository>`。
- 为 `main` 构建发布 `latest` 标签，为版本 tag 发布对应的版本标签。
- 使用 GitHub Actions 内置的 `GITHUB_TOKEN` 完成 GHCR 登录和写入权限配置。
- 保留 Docker 构建缓存，以减少重复构建耗时。

## Capabilities

### New Capabilities

- `container-image-publishing`: 定义 GitHub Actions 自动构建并发布 Docker image 到 GHCR 的触发、标签、权限和失败行为。

### Modified Capabilities

<!-- No existing runtime requirement changes. -->

## Impact

- 新增 `.github/workflows/` 下的 GitHub Actions 配置。
- 依赖仓库启用 GitHub Actions，并允许 workflow 使用 `packages: write` 权限。
- 影响 GHCR 中的镜像名称和标签约定，不修改应用运行时或 Dockerfile 行为。
