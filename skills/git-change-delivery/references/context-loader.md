# Context Loader

## 入口与调用

当前正式 command 是 `project-context`；本机 stable entry 是
`/home/hsd/.local/bin/project-context`。调用前可用 `type -a`/`readlink -f` 核对，不使用
历史仓库 wrapper 作为 installed authority：

```bash
/home/hsd/.local/bin/project-context --repo <absolute-repo>
/home/hsd/.local/bin/project-context --repo <absolute-repo> --focus <topic> --path <repo-relative-path>
/home/hsd/.local/bin/project-context --repo <absolute-repo> --format json --path <repo-relative-path>
```

`--repo` 必须是现有、非 bare Git worktree 中的规范绝对路径。Markdown 格式要求它就是
canonical worktree root；JSON 格式允许它位于 worktree 内，并把实际根目录单独输出。`--focus`
与 `--path` 各自最多提供一次，分别指定有界关注主题和仓库相对目标路径；目标不得逃逸仓库。
`--help` 和 `--version` 用于查看本机接口信息。

## 适用时机

- 在正式代码任务开始时，按需加载仓库的确定性本地上下文。
- 在编辑前确认分支、HEAD、upstream、工作树、适用的根级说明、项目入口和声明命令。
- 对已经充分理解且范围很小的局部任务，可按比例跳过；不得为了形式完整重复加载已确认且未变化的上下文。

Context Loader 不是审批工具。调用它不授予修改、commit、push 或其他外部写入权限。

## 输入、输出与边界

输入是明确的本地仓库范围。默认把 Markdown 写到 stdout；`--format json` 输出确定性 JSON。
两种格式都按固定顺序提供有界上下文，主要包括：

- Git 分支、HEAD、配置的 upstream、可用时的 ahead/behind，以及工作树变更；
- 根级 `AGENTS.md` 和 `README.md`；
- 受支持入口文件中的声明命令和选定项目入口；
- 最近提交与有界的两层目录树。

输出可能因单项或全局大小限制而截断或省略；应把截断标记视为证据边界。工具只读取本地
状态并渲染上下文，不访问网络，不写目标仓库，也不执行实现、测试、代码审查或任何 Git
mutation（包括 add、commit、fetch、push、ref/config 写入）。

## 失败处理

- 参数或仓库根无效时通常退出 2，并在 stderr 输出 `error: ...`。
- 收集失败或无法写出上下文时退出 1，并输出受控错误。
- 记录实际退出码和可见错误；不要声称上下文已加载，也不要伪造缺失字段。
- 可用明确、只读且范围受控的仓库检查补齐必要事实；若关键上下文仍不清楚，停止后续修改或发布。

Context Loader 的成功只证明上下文已收集并输出，不证明代码正确、测试通过、diff 已审查或仓库可以发布。
