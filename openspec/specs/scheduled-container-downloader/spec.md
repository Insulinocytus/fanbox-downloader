## Purpose

定义一个可以部署到 Docker 的 Fanbox 下载服务：通过环境变量配置，按照指定时区的 cron 计划运行，下载文件和浏览器 profile 持久化保存，并且不允许任务重叠执行。

## Requirements

### Requirement: Docker 镜像分发
项目 SHALL 提供一个 Docker image，其中包含下载程序、Python 依赖、CloakBrowser，以及在 Linux headless 模式下运行所需的系统依赖。容器 SHALL 作为常驻服务运行，而不是执行一次下载后退出。

#### Scenario: 启动服务镜像
- **WHEN** 操作者使用合法的环境变量启动 image
- **THEN** 容器保持运行，并等待或执行计划中的下载任务

### Requirement: 只使用环境变量配置
服务 SHALL 只从环境变量读取运行配置，且 SHALL 不再要求 `config.json`。服务 SHALL 支持以下环境变量：`FANBOX_COOKIE`、用逗号分隔的 `FANBOX_CREATORS`、`FANBOX_DOWNLOAD_DIRECTORY`、`FANBOX_FILE_DELAY`、`FANBOX_POST_DELAY`、`FANBOX_TIMEZONE`、`FANBOX_CRON` 和 `FANBOX_RUN_ON_START`。

`FANBOX_COOKIE` 和 `FANBOX_CREATORS` SHALL 必填。Cookie SHALL 是非空字符串；`FANBOX_CREATORS` 解析后至少要有一个非空作者 ID。其他默认值 SHALL 如下：`FANBOX_DOWNLOAD_DIRECTORY` 为 `/data/downloads`，`FANBOX_FILE_DELAY` 为 `0` 秒，`FANBOX_POST_DELAY` 为 `10` 秒，`FANBOX_TIMEZONE` 为 `Asia/Shanghai`，`FANBOX_CRON` 为 `0 */6 * * *`，`FANBOX_RUN_ON_START` 为 `false`。

#### Scenario: 使用可选配置的默认值
- **WHEN** 启动服务时设置非空的 `FANBOX_COOKIE` 和 `FANBOX_CREATORS`，并省略其他可选环境变量
- **THEN** 服务使用文档定义的默认下载目录、延迟、时区、cron 和启动行为

#### Scenario: 解析多个作者
- **WHEN** `FANBOX_CREATORS` 包含用逗号分隔的作者 ID，且项目两边可能有空格
- **THEN** 服务去除空格、忽略空项目，并依次处理剩余的作者 ID

#### Scenario: 拒绝非法配置
- **WHEN** Cookie 缺失或为空，延迟是负数或无法解析为数字，时区或 cron 无效，启动开关不是支持的布尔值，或者没有有效作者 ID
- **THEN** 服务在开始调度前退出，并指出出错的环境变量

### Requirement: 按时区执行 cron
服务 SHALL 把 `FANBOX_CRON` 解释为标准五段式 cron 表达式，并使用 `FANBOX_TIMEZONE` 指定的时区计算执行时间。

#### Scenario: 执行计划任务
- **WHEN** 到达配置时区中的下一次 cron 时间，且当前没有正在执行的下载任务
- **THEN** 服务启动一轮下载

#### Scenario: 不补执行错过的任务
- **WHEN** 服务停止期间错过了一个或多个 cron 时间点
- **THEN** 服务恢复后不排队、不补执行这些错过的任务

### Requirement: 可选的启动时执行
当 `FANBOX_RUN_ON_START` 为 true 时，服务 SHALL 在进入正常 cron 调度前立即启动一轮下载；当它为 false 时，服务 SHALL 只等待下一次 cron 时间。

#### Scenario: 开启启动时执行
- **WHEN** 容器以 `FANBOX_RUN_ON_START=true` 启动
- **THEN** 服务不等待下一次 cron，而是立即开始一轮下载

#### Scenario: 关闭启动时执行
- **WHEN** 容器以 `FANBOX_RUN_ON_START=false` 启动
- **THEN** 服务在下一次 cron 时间之前不开始下载

### Requirement: 不允许任务重叠
服务 SHALL 同时最多执行一轮下载。上一次任务仍在执行时到达的 cron 时间 SHALL 直接跳过，不能排队等待。

#### Scenario: 计划时间落在长任务期间
- **WHEN** 任务从 00:00 开始，并在 01:00 的 cron 时间点仍未结束
- **THEN** 01:00 这一轮被跳过，当前下载继续执行

#### Scenario: 后续时间仍然有效
- **WHEN** 当前任务在某个 cron 时间点之后结束，之后又到达更晚的 cron 时间点
- **THEN** 服务在这个更晚的时间点启动新一轮下载

### Requirement: Headless 浏览器生命周期
服务 SHALL 为每一轮下载以 headless 模式启动 CloakBrowser，并 SHALL 在任务成功、失败或容器正常停止时关闭浏览器。

#### Scenario: 任务成功后的清理
- **WHEN** 一轮下载完成
- **THEN** 服务在等待下一次计划之前关闭该轮任务使用的浏览器 context

#### Scenario: 任务失败后的清理
- **WHEN** 一轮下载因为错误结束
- **THEN** 服务关闭浏览器 context，并继续等待之后的计划任务；启动配置非法时除外

### Requirement: 持久化运行数据
image SHALL 提供稳定的容器路径，让操作者可以挂载下载目录和 CloakBrowser profile。使用相同 volume 重建容器后，之前的下载文件和 `.cloak-profile` 状态 SHALL 仍然可用。

#### Scenario: 使用 volume 重建容器
- **WHEN** 操作者删除并重建容器，同时重新挂载下载目录和浏览器 profile 目录
- **THEN** 新容器可以继续看到已有下载文件和 `.cloak-profile` 状态
