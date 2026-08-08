## Why（原因）

定时下载容器不需要保留浏览器 profile：每次启动浏览器时都会注入 `FANBOXSESSID`，下载文件也已经通过独立 volume 持久化。持久化 `.cloak-profile` 反而会引入 Chromium 锁文件、异常退出后的 profile 锁定问题，以及启动时额外需要初始化权限的目录。与此同时，当前 PUID/PGID 初始化流程可能导致服务用户的数字主组 ID 与 `appgroup` 不一致，使容器创建的文件出现 UID 已更新但 GID 仍为镜像原始值的情况。

## What Changes（变更内容）

- **BREAKING** 不再要求部署时持久化或挂载 `.cloak-profile`。
- 每轮下载使用临时、非持久化的 CloakBrowser context，同时继续在运行时注入 `FANBOXSESSID`。
- 删除 `.cloak-profile` volume 以及相关的持久化要求和部署文档；继续持久化 `/data/downloads`。
- 修正启动时的用户和组初始化，确保服务用户 UID、主 GID、Python 进程身份以及可写目录所有权都与配置的 `PUID`/`PGID` 一致。
- 保留非法或冲突 UID/GID 的校验，并继续以非 root 用户运行 Python 程序。

## Capabilities（能力）

### New Capabilities（新增能力）

### Modified Capabilities（修改的能力）

- `scheduled-container-downloader`：浏览器状态改为临时使用，不再持久化；配置的 PUID/PGID 必须统一控制服务进程和可写文件的所有权。

## Impact（影响）

- `fanbox_dl.py`：浏览器生命周期和 CloakBrowser API 调用方式。
- `Dockerfile`：volume 声明和可写目录初始化。
- `docker-entrypoint.sh`：用户/组初始化、权限修复和进程启动身份。
- `README.md`：部署示例、环境变量说明和数据目录文档。
- `test_fanbox_dl.py`：浏览器启动契约和生命周期测试。
- `openspec/specs/scheduled-container-downloader/spec.md`：临时浏览器状态和 UID/GID 所有权约束的增量要求。
