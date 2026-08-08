## 1. 浏览器生命周期

- [x] 1.1 将持久化 CloakBrowser 启动方式替换为非持久化 context API，显式创建页面，并保留运行时 Cookie 注入和浏览器清理逻辑。
- [x] 1.2 移除程序对 `PROFILE_DIR` 的依赖，确保每轮下载在没有 `/data/.cloak-profile` 的情况下也能启动。

## 2. 容器身份与存储

- [x] 2.1 修改 `docker-entrypoint.sh`，确保执行 `usermod`/`groupmod` 后，`appuser` 的 UID 和主 GID 会显式同步到 `PUID`/`PGID`。
- [x] 2.2 在权限修复前校验最终数字 UID/GID，将可写目录调整为配置的用户/组，并使用相同的主组启动 Python，同时保留冲突检查和 root 启动检查。
- [x] 2.3 删除 `.cloak-profile` 目录创建、权限初始化和 Docker volume 声明；保留持久化下载目录及必要的浏览器二进制缓存。

## 3. 测试与文档

- [x] 3.1 更新浏览器生命周期单元测试，覆盖非持久化 context 创建、显式页面创建、Cookie 注入和清理。
- [x] 3.2 增加容器级检查，覆盖默认 `10001:10001`、自定义 `3000:3000` 以及不同且不冲突的 PUID/PGID，并验证进程身份和新建文件的所有权。
- [x] 3.3 更新 README 的部署示例和迁移说明，改为只挂载下载目录，说明浏览器状态是临时的，并说明旧 profile 仅用于回滚。

## 4. 验证

- [ ] 4.1 构建 Docker 镜像并运行现有测试套件。
- [ ] 4.2 在没有 profile volume 的情况下连续运行两轮下载，验证每次都能使用运行时 Cookie 启动浏览器。
- [ ] 4.3 验证使用下载 volume 重建容器后，下载文件仍然保留、不会出现 Chromium profile 锁错误，并且新文件使用配置的 UID/GID。
