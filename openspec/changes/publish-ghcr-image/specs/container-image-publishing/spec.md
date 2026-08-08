## Purpose

为项目提供可重复、可审计的 GitHub Actions 镜像发布流程，使仓库代码变更能够自动生成并发布到 GitHub Container Registry，供部署环境直接拉取。

## ADDED Requirements

### Requirement: 自动构建并发布 GHCR 镜像

系统 SHALL 提供 GitHub Actions 工作流，在 push 到 `main` 分支或任意 Git tag 时构建仓库根目录中的 Dockerfile，并将构建成功的镜像发布到 `ghcr.io/<owner>/<repository>`。

#### Scenario: main 分支发布 latest

- **WHEN** 一次包含 Docker 相关构建上下文的提交被 push 到 `main`
- **THEN** 工作流构建 Docker image 并将其推送为 GHCR 的 `latest` 标签

#### Scenario: Git tag 发布版本标签

- **WHEN** 任意 Git tag 被 push 到仓库
- **THEN** 工作流构建 Docker image 并将其推送为与该 Git tag 对应的镜像标签

#### Scenario: 构建失败时不发布

- **WHEN** Docker image 构建失败
- **THEN** 工作流失败且不得推送一个表示本次构建成功的镜像标签

### Requirement: 使用仓库身份安全写入 GHCR

工作流 SHALL 使用 GitHub Actions 提供的仓库令牌登录 GHCR，并 SHALL 仅授予完成 checkout、构建和写入 GHCR 所需的最小权限；不得在仓库文件或工作流日志中暴露额外的长期凭据。

#### Scenario: 使用 GITHUB_TOKEN 推送

- **WHEN** 工作流运行在具有 package 写入权限的仓库中
- **THEN** 工作流使用当前仓库的 `GITHUB_TOKEN` 完成 GHCR 认证并推送镜像

#### Scenario: 无写入权限时安全失败

- **WHEN** 仓库未授予工作流 GHCR package 写入权限
- **THEN** 推送步骤失败，且工作流不使用硬编码的个人访问令牌作为后备凭据

### Requirement: 保持构建结果可追踪并复用缓存

工作流 SHALL 为同一提交保留可追踪的 Docker 构建来源，并 SHALL 启用 GitHub Actions 缓存以减少后续构建时间；缓存不可用时仍 SHALL 能够完成正常构建。

#### Scenario: 使用缓存重复构建

- **WHEN** 后续工作流运行使用与之前相同或相近的 Docker 构建层
- **THEN** 工作流尝试从 GitHub Actions 缓存读取并写入构建缓存，同时仍生成并发布正确标签的镜像

#### Scenario: 缓存不可用

- **WHEN** Docker 构建缓存不存在、失效或无法读取
- **THEN** 工作流回退到完整构建，并在构建成功后继续发布镜像
