# Context Loader（已冻结 / 遗留可选参考）

## 状态说明

`context-loader`（`project-context`）现已进入**维护冻结（maintenance freeze）**状态，**不再属于**常规仓库交付或任务启动前置步骤。

- 常规开发与审查中，请直接使用低开销的定点只读命令（`rg`、`git status --short`、`git diff` 与目标文件读取），无需为了形式完整调用 `project-context`。
- 本文保留仅为历史/遗留兼容参考，便于在明确需要离线复核旧流程或既有脚本引用时核对边界。

## 遗留边界（仅手工调用时适用）

- 若明确需要手工调用，命令为 `project-context --repo <absolute-repo>`（只读解析 `command -v project-context` 与 `realpath -e`）。
- 它只读取本地 Git 状态、根级说明和有界目录树并输出到 stdout，不访问网络，也不执行测试、审查或任何 Git 写入。
- 调用它不授予任何修改、commit、push 或发布权限，也不替代实际测试或 Snapshot Runner 审查。
