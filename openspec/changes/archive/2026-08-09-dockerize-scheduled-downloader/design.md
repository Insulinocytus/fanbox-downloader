## Context（背景）

当前程序是一个执行一次就退出的 Python 进程：`main()` 完成一轮下载后结束。配置从程序旁边的 `config.json` 读取，浏览器以非 headless 模式启动，最后通过 PyInstaller 生成 Windows 发布包。

CloakBrowser 会通过 Playwright 启动自己的 Chromium 浏览器，因此 Docker image 除了 Python 依赖外，还需要 Linux 浏览器运行库。下载文件和浏览器 profile 不能只放在容器可写层中，必须通过 volume 持久化。

## Goals / Non-Goals（目标与非目标）

**Goals（目标）：**

- 使用一个 Python 进程作为容器主进程。
- 保留现有“执行一轮下载”的逻辑，只在外层增加常驻调度器。
- 在等待定时任务或启动浏览器之前，先检查所有环境变量。
- 构建一个可重复构建的 Linux Docker image，并在构建阶段准备好 CloakBrowser 浏览器。
- 每轮任务结束后关闭浏览器；容器正常停止时也要清理浏览器资源。

**Non-Goals（非目标）：**

- 不做分布式调度、多 worker、任务队列或执行历史记录。
- 不同时支持 `config.json` 和环境变量。
- 不做镜像仓库发布或 CI 自动推送。
- 不支持运行期间动态修改配置；修改环境变量后需要重启容器。

## Decisions（设计决定）

### 1. 使用单进程、同步执行

把当前程序拆成两部分：

1. 执行一轮完整下载的函数；
2. 长期运行的调度循环。

调度循环在同一个线程中同步调用下载函数。因此不会启动第二个下载线程，也不会出现两个下载任务并发执行。

一轮任务结束后，根据“任务结束时的当前时间”计算下一次 cron 时间。这样，任务执行期间已经错过的时间点会自然被跳过。

例如：任务从 00:00 执行到 01:05，cron 是每小时一次，那么 01:00 会被跳过，下一次执行时间是 02:00。

**没有采用 APScheduler 的原因：**

虽然 APScheduler 提供并发控制和错过任务处理，但本项目只有一个下载任务，不需要线程池、任务合并、补偿窗口等额外机制。单进程同步循环更容易维护。

### 2. 使用 `croniter` 解析 cron

使用 Python 标准库处理环境变量、时区、等待和信号；只额外使用 `croniter` 解析标准五段式 cron 表达式，并计算下一次执行时间。

使用 `zoneinfo` 处理 `FANBOX_TIMEZONE`。所有调度时间都使用带时区的时间对象。

每次循环的流程是：

```text
读取当前时间
    ↓
计算配置时区下的下一次 cron 时间
    ↓
等待到该时间
    ↓
执行一轮下载
    ↓
根据任务结束时间重新计算下一次时间
```

容器重启后也只等待未来的时间点，不补执行停机期间错过的任务。

**没有使用容器内系统 cron 的原因：**

系统 cron 会引入第二个进程、crontab 生成、环境变量传递、锁和额外的退出处理。单个 Python 进程更符合 Docker 的运行方式。

### 3. 启动时一次性读取并校验环境变量

启动时读取一次环境变量，生成不可变的运行配置：

| 环境变量 | 默认值或要求 |
|---|---|
| `FANBOX_COOKIE` | 必填，不能为空 |
| `FANBOX_CREATORS` | 必填；多个作者用逗号分隔 |
| `FANBOX_DOWNLOAD_DIRECTORY` | `/data/downloads` |
| `FANBOX_FILE_DELAY` | `0` 秒 |
| `FANBOX_POST_DELAY` | `10` 秒 |
| `FANBOX_TIMEZONE` | `Asia/Shanghai` |
| `FANBOX_CRON` | `0 */6 * * *` |
| `FANBOX_RUN_ON_START` | `false` |

Cookie 和作者 ID 都不能为空。作者 ID 按逗号拆分，去除两端空格，忽略空项目。延迟必须是非负数字；启动开关只接受明确的 true/false 值；时区和 cron 必须能够解析。

校验失败时，在启动阶段退出，并指出出错的环境变量，但绝不能把 Cookie 内容写入日志。Cookie 缺失或为空时，必须在启动阶段失败，而不是启动后执行受限下载。

**没有保留 `config.json` 兼容读取的原因：**

同时支持两套配置会产生优先级和部署误解。Docker 部署直接使用环境变量即可，不保留旧配置来源。

### 4. 使用固定的容器数据路径

默认使用以下路径：

- `/data/downloads`：下载文件；可通过 `FANBOX_DOWNLOAD_DIRECTORY` 修改。
- `/data/.cloak-profile`：CloakBrowser 的持久化 profile，不额外增加 profile 路径环境变量。

部署时可以分别挂载这两个目录，也可以直接挂载共同的 `/data` 目录。

浏览器程序本身在构建 Docker image 时安装，不放入 `.cloak-profile` volume，也不依赖容器第一次启动时临时下载。

### 5. 构建 headless、非 root 的 Linux image

Dockerfile 使用精简的 Python Linux 基础镜像，安装必要的时区和浏览器系统库，安装 Python 依赖，在构建阶段执行 CloakBrowser 安装，然后复制程序并以非 root 用户运行。

镜像需要创建并授权以下目录：

- `/data/downloads`
- `/data/.cloak-profile`

`.dockerignore` 必须排除本地 Cookie、配置文件、下载目录、浏览器 profile、缓存、PyInstaller 输出和 release 压缩包，确保秘密和本地数据不会进入镜像构建上下文。

**没有使用桌面版或 VNC image 的原因：**

浏览器固定使用 `headless=True`，不需要桌面环境；完整桌面镜像只会增加体积和维护成本。

### 6. 每轮任务结束后清理浏览器

每轮下载都要使用 `try/finally`，无论成功、普通异常、Cloudflare 错误还是正常停止，都调用 `close_browser()`。

关闭后清空浏览器相关全局变量，使下一轮任务重新创建浏览器 context，但仍使用同一个持久化 profile。

容器收到 SIGTERM 或 SIGINT 时停止继续调度，并在退出前关闭正在使用的浏览器。

## Risks / Trade-offs（风险与取舍）

- **CloakBrowser 或浏览器二进制不支持目标 Linux 架构** → 在 Docker 构建阶段预装浏览器，让构建时尽早失败；同时文档说明支持的架构。
- **宿主机挂载目录没有写权限** → 文档说明 volume 权限要求；启动或下载失败时明确指出目录路径。
- **headless 模式触发不同的 Cloudflare 行为** → 保留现有重试机制和持久化 profile；失败后可以重启容器，从已有目录继续。
- **精简镜像缺少时区数据** → 安装 `tzdata`，并在启动时校验 `FANBOX_TIMEZONE`。
- **Cookie 被写入镜像层或日志** → Cookie 只在容器运行时注入，不打进镜像，也不打印 Cookie 值。
- **当前本地 `config.json` 中的 Cookie 可能已经泄露** → 部署前更换 Cookie；本次 change 只负责移除新的发布流程中的配置文件，不负责清理 Git 历史。

## Migration Plan（迁移步骤）

1. 先更换当前已经暴露的 FANBOX Cookie。
2. 构建 Docker image，并把原 `config.json` 中的值转换成环境变量。
3. 挂载 `/data/downloads` 和 `/data/.cloak-profile`。
4. 第一次部署时可设置 `FANBOX_RUN_ON_START=true`，用于立即检查配置和下载流程。
5. 确认任务完成、浏览器关闭，并确认重建容器后下载文件和 profile 仍然存在。
6. 停止使用 Windows 可执行文件和 `config.json`。

回滚方式是停止 Docker 容器，继续使用旧的 Windows 发布包或旧源码版本。下载目录中的普通文件不需要迁移。
