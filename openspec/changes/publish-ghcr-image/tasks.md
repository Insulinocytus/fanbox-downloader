## 1. GitHub Actions 工作流

- [x] 1.1 新增 `.github/workflows/publish-image.yml`，配置 push 到 `main` 和任意 Git tag 的触发条件。
- [x] 1.2 配置 workflow 的最小权限（`contents: read`、`packages: write`）以及 Docker 构建所需的 checkout 和 Buildx 初始化步骤。

## 2. GHCR 构建与发布

- [x] 2.1 使用 `GITHUB_TOKEN` 登录 `ghcr.io`，不引入长期个人访问令牌。
- [x] 2.2 配置镜像名称为 `ghcr.io/<owner>/<repository>`，并生成 `main` 的 `latest` 标签和 Git tag 对应的版本标签。
- [x] 2.3 使用仓库根目录的 `Dockerfile` 构建镜像，写入 OCI source/revision 元数据，并启用 GitHub Actions BuildKit 缓存。
- [x] 2.4 确保仅在构建成功后推送镜像，并保留构建失败时的失败状态。

## 3. 验证发布流程

- [x] 3.1 检查 workflow YAML、触发条件、权限、镜像标签和缓存配置符合规格。
- [x] 3.2 在 `main` 提交和版本 tag 发布后验证 Actions 成功，并从 GHCR 验证 `latest` 与版本标签可拉取。
