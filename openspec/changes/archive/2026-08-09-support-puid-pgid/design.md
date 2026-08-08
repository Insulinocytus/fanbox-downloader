## Context

当前 `Dockerfile` 在构建时创建 UID 10001 的 `appuser`，并通过 `USER appuser` 直接启动 Python。该模型安全但无法自动适配宿主机 bind mount 的 UID/GID。参见 `proposal.md` 和 `specs/scheduled-container-downloader/spec.md`。

## Goals / Non-Goals

**Goals:**

- 兼容现有默认行为：未设置 `PUID`/`PGID` 时仍使用 `10001:10001`。
- 允许 Compose 或其他容器编排通过 `PUID`/`PGID` 指定宿主机账户身份。
- 只让 root 执行短暂的启动初始化，Python 下载程序始终以目标非 root 身份运行。
- 在 named volume 和 bind mount 两种场景下初始化 `/data`、浏览器 profile 和运行缓存的所有权。
- 保持现有数据路径、环境变量和调度逻辑不变。

**Non-Goals:**

- 不让用户通过 `PUID`/`PGID` 运行 root（UID/GID 0 不在支持范围内）。
- 不在 Python 应用中实现用户切换或权限修复。
- 不支持同时使用 Compose `user:` 覆盖身份并要求 entrypoint 自动 chown。
- 不改变宿主机目录的 ACL、SELinux 或 Docker daemon 的 rootless 配置。

## Decisions

- **采用 root entrypoint + 降权执行**：容器默认以 root 进入 entrypoint，用于校验数字 UID/GID、调整内部 `appuser`/组标识和目录所有权；完成后使用轻量级降权执行器 `exec` Python。这样应用进程的真实 `os.getuid()`/`os.getgid()` 就是目标值，并且不会以 root 长期运行。
- **保留内部服务用户**：继续使用 `appuser`，默认 UID/GID 为 10001；启动时修改该用户和组，而不是直接以任意未登记的 numeric user 运行。这样用户拥有 home 目录和稳定的 `/etc/passwd` 记录，浏览器运行时更可靠。
- **使用 `PUID`/`PGID` 而不是 `user:`**：这是 LinuxServer.io 风格，适合 NAS 和 bind mount。Compose 示例使用环境变量，不设置 `user:`；两者同时使用会让 entrypoint 失去 root 权限，无法完成初始化。
- **限制可修改范围**：启动时调整 `/data`、`/opt/cloakbrowser-cache` 和目标用户 home 目录；`/app` 只读运行所需文件不作为数据权限入口。递归 chown 用于修复已有 volume 或宿主机目录的旧所有权。
- **严格校验并失败关闭**：`PUID`/`PGID` 必须是非零正整数；用户或组 ID 与不可安全调整的现有账户冲突时直接失败，不静默复用其他账户。错误输出只包含变量名和初始化原因，不输出 Cookie。
- **使用现有基础镜像能力或最小降权工具**：优先使用镜像中可靠的系统降权能力；若基础镜像不提供正确处理信号和 `exec` 的工具，增加单一轻量依赖（例如 Debian 的 `gosu`），不引入完整 init 系统。
- **使 entrypoint 可测试**：把 UID/GID 解析、冲突检查、目录初始化和最终命令组装保持为可独立验证的 shell 分支；至少覆盖默认身份、自定义 `3000:3000`、非法值和权限失败路径。

## Risks / Trade-offs

- [每次启动递归 chown 大量下载文件会变慢] → 只处理约定的可写目录；默认 UID/GID 不变时可跳过不必要的用户修改，但首次启动或身份变化仍必须修复所有权。
- [容器短暂以 root 启动扩大初始化权限] → entrypoint 只执行白名单内的 user/group/chown 操作，随后使用 `exec` 降权；Python 不允许以 root 身份启动。
- [目标 UID/GID 与镜像已有账户冲突] → 预检并失败，不使用非唯一 UID/GID 或覆盖无关账户。
- [Compose 配置错误设置了 `user:`] → 文档明确说明互斥关系；entrypoint 检测非 root 启动时给出可操作的错误。
- [不同宿主机的 ACL/SELinux 策略阻止 chown] → 保留清晰失败日志；此类宿主机策略需由部署者调整，应用层不绕过安全策略。

## Migration Plan

1. 发布包含新 entrypoint 的镜像，未设置 `PUID`/`PGID` 的现有部署继续使用 `10001:10001`。
2. 使用 bind mount 的部署者在 Compose 中加入匹配宿主机账户的 `PUID`/`PGID`，删除 `user:` 覆盖，并重建容器。
3. 首次启动完成后验证 `id`、下载文件和 profile 文件的所有权；named volume 会在初始化阶段自动修正。
4. 如需回滚，恢复旧镜像；旧镜像继续以固定 10001 运行，已由新镜像生成的文件可能需要宿主机手动恢复为 10001 所有。
