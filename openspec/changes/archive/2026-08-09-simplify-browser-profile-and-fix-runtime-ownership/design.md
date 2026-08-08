## Context（背景）

详见 `proposal.md` 的“原因”部分。当前程序使用 `launch_persistent_context(PROFILE_DIR, ...)` 启动浏览器，注入 `FANBOXSESSID`，并在每轮任务结束后关闭浏览器。当前 entrypoint 分别修改 `appgroup` 和 `appuser`，但修改组的数字 GID 后，没有显式恢复 `appuser` 的主组关系。

## Goals / Non-Goals（目标与非目标）

**目标：**

- 使用不共享磁盘 profile、不会产生 Chromium 单实例锁的浏览器 context。
- 保留运行时 Cookie 注入和 headless CloakBrowser 行为。
- 对默认 UID/GID、相同的自定义 UID/GID 以及不同的自定义 UID/GID，确保最终服务身份和可写文件所有权都严格匹配 `PUID:PGID`。
- 保留下载文件的持久化和应用程序的非 root 运行方式。

**非目标：**

- 不重新设计 cron 调度器或下载断点行为。
- 不完全删除启动时的权限修复；bind mount 和已有 volume 仍然需要它。
- 不增加可能误删其他 Chromium 进程锁文件的 profile 清理逻辑。
- 不保留不同下载轮次之间的浏览器 localStorage、缓存或 Cloudflare 放行状态。

## Decisions（设计决策）

### 使用 `launch_context` 替代 `launch_persistent_context`

改用 CloakBrowser 的非持久化 context API。它不会复用 `/data/.cloak-profile`；程序需要显式创建页面、注入 `FANBOXSESSID`，并在本轮任务结束时关闭 context。相比维护锁文件清理机制，这种方式更简单，也更不容易受到旧 profile 状态影响。

现有持久化 profile 仅作为回滚旧版本时的部署选项保留，不再作为新版本的运行要求。`/opt/cloakbrowser-cache` 中的浏览器二进制缓存仍然属于镜像和运行依赖，与浏览器用户状态无关。

### 只使用 Cookie 作为登录输入

当前程序已经在每次创建浏览器 context 时设置 `FANBOXSESSID`。将它确定为唯一的运行时登录来源。新的 context 必须能够在不读取旧 profile 状态的情况下访问 Fanbox 并调用 API。

### 显式设置服务用户的主组

完成 UID/GID 修改后，显式将 `appuser` 的主组设置为 `appgroup`，然后再校验最终的数字 UID 和主 GID，最后执行 `chown` 和 `gosu`。`groupmod --gid` 会修改组的数字 ID，但不一定会同步用户在 `/etc/passwd` 中记录的主 GID；`usermod --uid` 只会修改 UID。因此不能只根据这些命令成功就推断身份关系正确，必须在修改完成后检查最终结果。

使用命名的 `appuser:appgroup` 修复可写目录的所有权，并使用相同的用户/组启动应用程序。这样，已有文件和 Python 新创建的文件都会收敛到配置的数字 UID/GID。

### 保留 UID/GID 冲突检查

保留当前对无关用户或组的 UID/GID 冲突检查，以及 entrypoint 必须以 root 启动的要求。本次变更只修复服务用户自身的 UID/GID 关系，不扩大允许接管的容器身份范围。

## Risks / Trade-offs（风险与取舍）

- **Cloudflare 或网站状态可能无法跨轮次保留** → 依赖运行时注入的 Cookie 和现有重试机制，并在部署测试中连续验证两轮。如果确实需要持久化挑战状态，再重新评估 profile 方案，不在本次变更中悄悄恢复共享状态。
- **现有 profile 数据不再使用** → 迁移时保留旧 profile 目录，并在部署文档中说明如何为旧版本回滚重新挂载它。
- **大型下载目录或浏览器缓存仍可能使递归 `chown` 较慢** → 本次变更移除 profile 目录这一部分开销，但不进行更大范围的启动性能重构。
- **UID/GID 修改错误可能导致容器无法启动** → 增加容器级检查，验证 `id -u`、`id -g` 和新建可写文件的 `stat` 结果；无法满足不变量时沿用现有错误路径终止初始化。

## Migration Plan（迁移计划）

1. 停止旧容器，并确认没有 Chromium 进程正在使用旧 profile volume。
2. 使用只挂载下载目录的新镜像部署；在连续两轮下载成功前，将旧 profile volume 保留为备份。
3. 将 `PUID` 和 `PGID` 设置为目标宿主机用户/组的 ID，并使用 `id` 和 `stat` 检查容器进程及新建下载文件的所有权。
4. 如果非持久化浏览器无法通过目标网站检查，则回滚到旧镜像并重新挂载保存的 profile volume。
5. 验证成功后，按照运维保留策略移除不再使用的 profile 挂载和旧 profile 数据。
