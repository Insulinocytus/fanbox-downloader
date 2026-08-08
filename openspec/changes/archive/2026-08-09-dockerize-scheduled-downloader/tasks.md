## 1. 配置改造

- [x] 1.1 增加环境变量解析测试：覆盖必填 Cookie、默认值、多个作者、合法/非法布尔值、非法延迟、非法时区、非法 cron 和空作者列表。
- [x] 1.2 删除 `read_config()` 和 frozen executable 路径逻辑，改为只从环境变量读取并校验配置；Cookie 缺失或为空时启动失败，日志中不能输出 Cookie。
- [x] 1.3 删除 Windows 控制台编码设置，使用固定的容器下载目录和 `.cloak-profile` 路径。

## 2. 常驻调度与下载

- [x] 2.1 增加调度测试：验证 `FANBOX_RUN_ON_START=true/false`、带时区的下一次 cron 时间，以及长任务结束后会跳过已经错过的时间点。
- [x] 2.2 把现有下载流程整理成可以单独调用的“一轮下载”操作，同时保持作者遍历、重试、已下载跳过和文件下载行为不变。
- [x] 2.3 使用 `croniter` 增加同步调度循环，并输出下一次计划执行时间。
- [x] 2.4 将 CloakBrowser 改为 `headless=True`，并保证任务成功、普通异常、Cloudflare 错误、SIGTERM 和 SIGINT 时都会关闭浏览器并重置相关状态。

## 3. Docker 打包

- [x] 3.1 增加依赖文件，包含现有 Python 依赖和 `croniter`，并固定适合 Docker 构建的版本范围。
- [x] 3.2 编写非 root 的 Linux Dockerfile：安装时区和浏览器系统库，安装 Python 依赖，构建时预装 CloakBrowser，创建可写的 `/data/downloads` 和 `/data/.cloak-profile`，并把调度程序作为容器入口。
- [x] 3.3 增加 `.dockerignore`，排除 Cookie/config 文件、下载目录、浏览器 profile、Python 缓存、PyInstaller 输出和 release 压缩包。
- [x] 3.4 删除 `build.ps1`、PyInstaller spec、配置示例和已经过时的 Windows 打包说明。

## 4. 文档和验证

- [x] 4.1 重写 README：加入 Docker 构建和运行命令、所有环境变量及默认值、cron/时区示例、volume 挂载、非 root 权限要求、启动时执行规则、跳过重叠任务规则和 Cookie 更换提醒。
- [x] 4.2 运行 Python 测试，确认环境变量解析、调度、现有文件提取/下载逻辑和浏览器清理测试通过。
- [ ] 4.3 构建 Docker image，并验证非法配置会启动失败；合法配置下容器会保持运行并等待下一次 cron。
- [ ] 4.4 使用相同的 volume 重建测试容器，确认下载文件和 profile 数据能够保留，且不会被打进 image。
