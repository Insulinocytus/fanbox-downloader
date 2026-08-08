## Context

当前仓库已有根目录 `Dockerfile`，使用 `python:3.13-slim-bookworm`、锁定的 `uv.lock` 和非 root 运行用户构建可部署镜像，但没有 `.github/workflows/` 发布流程。参见 `proposal.md` 和 `specs/container-image-publishing/spec.md`。

## Goals / Non-Goals

**Goals:**

- 用单个 GitHub Actions workflow 覆盖 `main` 和 Git tag 发布。
- 将镜像统一发布到当前仓库对应的 GHCR 地址。
- 使用 `GITHUB_TOKEN`、最小权限和可复用 BuildKit 缓存。
- 让镜像标签直接反映分支或版本 tag 的来源。

**Non-Goals:**

- 不修改 Dockerfile、应用运行时配置或容器数据目录。
- 不发布到 Docker Hub 或其他镜像仓库。
- 不在 pull request 中推送镜像；如需验证，可另行增加仅构建不推送的检查。

## Decisions

- **使用单个发布 workflow**：以 `push` 的 `main` 分支和 `tags: ['*']` 为触发条件，避免为相同构建逻辑维护多个文件。
- **使用 Docker 官方 Buildx 相关 actions**：采用 Docker metadata action 生成 `ghcr.io/${{ github.repository }}` 的标签，采用 Buildx action 构建，采用 login action 认证 GHCR。它们提供标准的 GitHub Actions 集成，少于手写 tag、登录和缓存逻辑。
- **标签约定**：`main` 使用 `latest`；Git tag 使用不带额外重写的 tag 名作为版本标签。这样部署者可以按稳定的 `latest` 或明确版本拉取。
- **凭据与权限**：workflow 顶层声明 `contents: read` 和 `packages: write`，登录用户名使用触发仓库所有者，密码使用 `${{ secrets.GITHUB_TOKEN }}`。不新增仓库 secret。
- **缓存策略**：使用 BuildKit 的 GitHub Actions cache backend（`cache-from`/`cache-to`），缓存只优化速度，不作为发布成功的前置条件。
- **来源追踪**：将 GitHub Actions 生成的 OCI source/revision labels 交给 metadata/build action 写入镜像元数据，以便从镜像回溯到仓库和提交。

## Risks / Trade-offs

- [GHCR 权限设置或仓库策略阻止 `GITHUB_TOKEN` 写入] → workflow 明确声明 `packages: write`；若组织策略仍拒绝，运行会失败并要求仓库管理员调整设置。
- [Git tag 包含 Docker 标签不接受的字符] → 让 metadata action 负责规范化标签；不为未经请求的标签兼容规则增加自定义脚本。
- [缓存服务暂时不可用] → 构建仍使用 Dockerfile 从头执行，缓存仅作为加速手段。
- [上游 action 版本变化] → 固定使用明确的 major 版本，并在后续维护时按 GitHub Actions 依赖更新策略升级。

## Migration Plan

1. 合并 workflow 后，在仓库 Actions 设置中确认允许 workflow 写入 packages，并在 `main` 上执行一次发布。
2. 从 GHCR 验证 `latest` 镜像可拉取，再推送版本 tag 验证版本标签。
3. 回滚时删除或禁用该 workflow；已发布的 GHCR 镜像保留，除非另行清理 package。
