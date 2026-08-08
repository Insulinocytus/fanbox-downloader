## MODIFIED Requirements

### Requirement: Docker 镜像分发

项目 SHALL 提供一个 Docker 镜像，其中包含下载程序、Python 依赖、CloakBrowser，以及在 Linux headless 模式下运行所需的系统依赖。容器 SHALL 作为常驻服务运行，而不是执行一次下载后退出。容器 SHALL 在启动初始化阶段根据 `PUID` 和 `PGID` 设置服务用户及可写目录的所有权，初始化完成后 SHALL 以该 UID/GID 执行 Python 下载程序，且默认不得以 root 执行应用程序。服务进程的主 UID 和主 GID，以及其在可写目录中新创建文件的所有权， SHALL 与配置的 `PUID`/`PGID` 一致。

#### Scenario: 启动服务镜像

- **WHEN** 操作者使用合法的环境变量启动镜像
- **THEN** 容器保持运行，并等待或执行计划中的下载任务

#### Scenario: 默认非 root 身份

- **WHEN** 操作者未设置 `PUID` 或 `PGID`
- **THEN** 容器使用默认 UID/GID `10001:10001` 完成初始化，并以该身份执行 Python 下载程序，且新创建的可写文件属于 `10001:10001`

#### Scenario: 使用自定义 UID/GID

- **WHEN** 操作者设置合法的 `PUID=3000` 和 `PGID=3000`
- **THEN** entrypoint 调整服务用户和组、完成权限初始化，并以 `3000:3000` 身份执行 Python 下载程序，且新创建的可写文件属于 `3000:3000`

#### Scenario: 使用不同的自定义 UID/GID

- **WHEN** 操作者设置合法且不冲突的不同 `PUID` 和 `PGID`
- **THEN** 服务进程的主 UID/GID 以及新创建可写文件的 UID/GID 分别与两个配置值一致

#### Scenario: 权限初始化失败

- **WHEN** entrypoint 无法修改服务用户/组或无法调整所需目录的所有权
- **THEN** 容器在启动下载程序前退出，并报告初始化错误

### Requirement: Headless 浏览器生命周期

服务 SHALL 为每一轮下载以 headless 模式启动一个不依赖持久化用户 profile 的 CloakBrowser context，并 SHALL 在任务成功、失败或容器正常停止时关闭浏览器。服务 SHALL 在每次启动浏览器时注入运行时 Cookie，不得要求上一次任务留下的浏览器 profile 状态才能完成登录。

#### Scenario: 任务成功后的清理

- **WHEN** 一轮下载完成
- **THEN** 服务在等待下一次计划之前关闭该轮任务使用的浏览器 context，并释放该轮的临时浏览器状态

#### Scenario: 任务失败后的清理

- **WHEN** 一轮下载因为错误结束
- **THEN** 服务关闭浏览器 context，并继续等待之后的计划任务；启动配置非法时除外

#### Scenario: 容器重建后启动浏览器

- **WHEN** 操作者重建容器且仅保留下载目录，没有之前的浏览器 profile
- **THEN** 服务使用运行时配置的 Cookie 启动新的 headless 浏览器并执行任务

### Requirement: 持久化运行数据

镜像 SHALL 提供稳定的容器路径，让操作者可以挂载下载目录。使用相同 volume 重建容器后，之前的下载文件 SHALL 仍然可用；服务 SHALL 不要求 `.cloak-profile` 目录或其 volume。启动初始化 SHALL 确保下载目录及运行所需浏览器缓存对目标 UID/GID 可写。

#### Scenario: 使用 volume 重建容器

- **WHEN** 操作者删除并重建容器，同时重新挂载下载目录
- **THEN** 新容器可以继续看到已有下载文件，并可以在没有旧浏览器 profile 的情况下启动浏览器

#### Scenario: 不挂载浏览器 profile

- **WHEN** 操作者按照新的部署方式只挂载下载目录
- **THEN** 容器可以正常启动、执行任务和关闭浏览器，不因缺少 `.cloak-profile` 而失败

#### Scenario: 使用宿主机 UID/GID 写入挂载目录

- **WHEN** 操作者使用 bind mount 并设置与宿主机账户匹配的 `PUID`/`PGID`
- **THEN** 新生成的下载文件由该 UID/GID 所有，并且宿主机账户可以直接访问
