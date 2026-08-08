## Why

当前镜像固定使用 UID/GID 10001，挂载宿主机目录时，宿主机用户可能无法直接读取下载文件或浏览器 profile。采用 LinuxServer.io 常见的 `PUID`/`PGID` 启动模式，可以在保留非 root 运行的同时适配不同宿主机用户和 NAS 权限模型。

## What Changes

- 新增 `PUID` 和 `PGID` 环境变量，默认值保持为 `10001`，确保现有部署行为兼容。
- 容器启动时由 root entrypoint 校验并应用目标 UID/GID。
- 启动 entrypoint 为数据目录、浏览器 profile 和运行所需缓存调整所有权。
- 初始化完成后降权，以目标 UID/GID 执行 Python 下载程序；Python 程序本身不以 root 运行。
- 非法 UID/GID、无法调整用户/组或目录权限时，容器在启动阶段明确失败。
- 文档说明使用 `PUID`/`PGID` 时不要再通过 Compose `user:` 覆盖容器用户。

## Capabilities

### New Capabilities

<!-- None. This change modifies the existing container runtime capability. -->

### Modified Capabilities

- `scheduled-container-downloader`: 修改 Docker 镜像运行身份和环境变量配置，支持通过 `PUID`/`PGID` 自动修正挂载目录权限并以目标身份运行服务。

## Impact

- 修改 `Dockerfile` 的启动用户和 entrypoint 设计，可能新增一个轻量级降权启动工具或等效实现。
- 新增容器启动脚本，并调整 `/data`、浏览器缓存及用户 home 目录的权限初始化流程。
- 修改环境变量文档和 Compose 部署示例。
- 不改变下载调度、Cookie 配置、数据目录路径或镜像发布流程。
