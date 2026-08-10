# 问题追踪器：GitHub

本仓库的问题与规格保存在 GitHub Issues 中。所有操作使用 `gh` CLI。

## 约定

- **创建问题**：`gh issue create --title "..." --body "..."`。多行正文使用 heredoc。
- **读取问题**：`gh issue view <编号> --comments`，同时获取标签；需要过滤评论时使用 `jq`。
- **列出问题**：`gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'`，并按需添加 `--label` 和 `--state`。
- **评论**：`gh issue comment <编号> --body "..."`
- **添加或移除标签**：`gh issue edit <编号> --add-label "..."` / `--remove-label "..."`
- **关闭问题**：`gh issue close <编号> --comment "..."`

仓库从 `git remote -v` 推断；在仓库克隆目录中运行时，`gh` 会自动识别。

## 将 Pull Request 作为分类入口

**PRs as a request surface: no.**

（如果本仓库将外部 PR 视为功能请求，可改为 `yes`；`/triage` 会读取此标志。）

设为 `yes` 后，PR 使用与问题相同的标签与状态：

- **读取 PR**：`gh pr view <编号> --comments`，使用 `gh pr diff <编号>` 查看差异。
- **列出待分类的外部 PR**：运行 `gh pr list --state open --json number,title,body,labels,author,authorAssociation,comments`，仅保留 `authorAssociation` 为 `CONTRIBUTOR`、`FIRST_TIME_CONTRIBUTOR` 或 `NONE` 的项目。
- **评论、标记、关闭**：使用 `gh pr comment`、`gh pr edit --add-label` / `--remove-label`、`gh pr close`。

GitHub 的 Issue 与 PR 共用编号空间，因此 `#42` 可能是两者之一。先运行 `gh pr view 42`，失败后再运行 `gh issue view 42`。

## 当技能要求“发布到问题追踪器”

创建 GitHub Issue。

## 当技能要求“获取相关工单”

运行 `gh issue view <编号> --comments`。

## Wayfinding 操作

供 `/wayfinder` 使用。**地图**是一个 Issue，**子任务**也是 Issue。

- **地图**：使用标签 `wayfinder:map` 的单个 Issue，正文保存 Notes、Decisions-so-far 与 Fog。创建命令：`gh issue create --label wayfinder:map`。
- **子任务**：作为 GitHub 子 Issue 关联到地图。若未启用子 Issue，则在地图正文中使用任务列表，并在子任务正文顶部写入 `Part of #<地图编号>`。标签为 `wayfinder:<类型>`，类型包括 `research`、`prototype`、`grilling`、`task`。认领后分配给执行开发者。
- **阻塞关系**：优先使用 GitHub 原生 Issue dependencies。添加命令：`gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`。其中 `<blocker-db-id>` 是阻塞 Issue 的数字数据库 ID，可通过 `gh api repos/<owner>/<repo>/issues/<n> --jq .id` 获取，不是 `#编号` 或 `node_id`。若依赖功能不可用，则在子任务正文顶部写入 `Blocked by: #<n>, #<n>`。
- **前沿查询**：列出地图下所有未关闭子任务，排除仍有开放阻塞项或已有负责人者；按地图中的顺序选择第一个。
- **认领**：`gh issue edit <n> --add-assignee @me`，这是会话中的第一次写操作。
- **解决**：运行 `gh issue comment <n> --body "<答案>"`，然后 `gh issue close <n>`，最后把上下文链接追加到地图的 Decisions-so-far。
