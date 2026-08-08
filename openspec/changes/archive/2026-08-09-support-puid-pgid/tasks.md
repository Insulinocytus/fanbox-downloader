## 1. 容器启动身份

- [x] 1.1 新增 root entrypoint，读取 `PUID`/`PGID`，默认使用 `10001:10001`，并校验为非零正整数。
- [x] 1.2 在 entrypoint 中处理服务用户/组 UID/GID 冲突，并在无法安全调整时以明确错误退出。
- [x] 1.3 为 `/data`、`/opt/cloakbrowser-cache` 和服务用户 home 目录初始化所有权，覆盖 named volume 与 bind mount 场景。
- [x] 1.4 使用可靠的降权执行方式 `exec` Python 程序，确保应用进程以目标 UID/GID 运行而非 root。

## 2. Docker 镜像

- [x] 2.1 调整 `Dockerfile`，让 entrypoint 以 root 启动，并补充所需的最小降权工具或等效系统能力。
- [x] 2.2 保持现有数据卷、默认 UID/GID、Python 入口和下载运行时行为兼容。

## 3. 文档与部署示例

- [x] 3.1 更新环境变量文档，说明 `PUID`/`PGID` 默认值、用途、非法值和权限初始化行为。
- [x] 3.2 添加 Docker Compose 示例，展示 `PUID=3000`、`PGID=3000` 和 bind mount，并明确不要同时设置 `user:`。

## 4. 验证

- [x] 4.1 添加或运行最小启动检查，覆盖默认 `10001:10001`、自定义 `3000:3000`、非法 UID/GID、UID/GID 冲突和权限失败路径。
- [x] 4.2 构建镜像并验证容器内 Python、下载文件和 profile 文件分别以目标 UID/GID 运行和创建。
- [x] 4.3 验证现有调度测试与默认配置行为不受影响。
