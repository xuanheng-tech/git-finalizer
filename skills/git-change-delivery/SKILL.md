---
name: git-change-delivery
description: "用于仓库文件变更的上下文加载、测试与 evidence 范围选择、Snapshot Runner 审查，以及按授权边界使用 Git Finalizer commit、bootstrap、publication 和 remote verify；不管理 worktree lifecycle。"
---

# Git change delivery

## 何时使用

- 修改代码或其他仓库文件，需要选择测试/审查范围并交付。
- 审查当前 diff 或相对本地 base 的分支。
- 用户要求 commit/push，或向空 remote 首次发布、发布已有 local commits、恢复中断的发布。

只读调查、纯系统/服务/数据库操作不需要本 Skill；借用其审查规则时不调用 Finalizer。
触发本 Skill 不等于运行全部工具或全量测试。

## 流程

`worktree-lifecycle` 负责选择 checkout、lease、handoff、integration 与 release；本 Skill 从已
选定的仓库开始，不创建或移除 worktree。`toolchain_compatibility.json` 绑定各工具的 public
contract version。

1. 读取适用规则；需要确定性上下文时用 Context Loader，见
   [references/context-loader.md](references/context-loader.md)。
2. 在三个工具之外实施最小变更，运行从定向到必要程度的真实检查。工具输出不替代测试。
3. 需要审查证据时用 Snapshot Runner，默认 `--summary`，摘要不足再读 artifact，见
   [references/snapshot-runner.md](references/snapshot-runner.md)。简单且不发布的任务可跳过；
   发布前通常至少审查当前 diff。
4. 用 Git Finalizer 一次调用完成 commit/push/远端核验，见
   [references/git-finalizer.md](references/git-finalizer.md)。Finalizer 返回的证据交给上层
   lifecycle 继续 handoff/integration/release。

仓库任务开始、恢复或工具更新后运行 `tool-skill-sync doctor --repo <checkout> --summary`；
指纹变化时重读规则。源码候选、已安装 runtime 与已合入 main 是不同状态。更新本 Skill 时同步
payload pins，经 owner 的安装流程交付并以实际 doctor 收口。

## 授权

先应用用户的持续自动交付授权：覆盖本任务时，验收后直接 commit、push 并远端核验；“完成”
“修复”“继续推进”包含交付，不因本轮未重复写 commit/push 而停在草稿。用户要求只读、不提交、
不推送或仅本地 commit 时遵守更窄指令；仅授权 commit 不扩大为 push。暂停、resume、压缩、
重试和同一任务的受控 checkout 迁移不使授权失效。本 Skill 与工具权限都不授予 Git 权限。
merge/main integration、force push、history rewrite、tag/release、branch 删除和 production
激活按全局规则判断；已被覆盖时直接继续。

## Git Finalizer 模式

选择模式不得扩大用户的 commit/push 授权。先用 `command -v git-finalize` 与 `realpath -e`
解析安装入口，下文 `git-finalize` 指该绝对路径；所有模式都加 `--summary --repo <absolute-repo>`。

| 目标 | 接口 |
| --- | --- |
| 只验证，不写 Git | `--mode verify-only -- <files>` |
| 只建本地 commit | `--mode commit-only --message <msg> -- <files>` |
| unborn 仓库只建 root commit | `--initial-commit-only --message <msg> -- <files>` |
| commit + push + 远端核验（有 upstream） | 默认模式 `--message <msg> -- <files>` |
| 发布已有 local commits（有 upstream） | `--resume-publish <full-head-oid>` |
| clean feature branch 首次发布（无 upstream、远端无同名） | `--publish-existing-branch <oid> --remote <r> --remote-branch <b>` |
| 已有历史推向全空 repository | `--publish-existing-history <oid> --remote <r> --remote-branch <b>`；中断用 `--resume-existing-history-publish` |
| 同步两个远端的同名已发布分支 | `--sync-published-branch <oid> --source-remote <r> --remote <r> --remote-branch <b> --expected-target-oid <oid>` |
| Controller 已签发 publication lease 的 integration candidate | `--publish-integration-candidate <oid> --repo <candidate> --lease-id <id> --run-id <run>` |
| 退役已集成的 remote/local feature ref | `--retire-remote-branch …` / `--retire-local-branch …`（见 reference） |
| 规划/确保空 Gitea repository | `--repo-plan` / `--repo-ensure` |

- `--snapshot` 只用于 initial publish。没有可用远端但本地提交安全时先 `commit-only`，之后用
  `--resume-publish` 或 `--publish-existing-branch`，不重复制造 commit。
- 运行策略允许且任务已授权时直接执行；需要审批时走执行器原生审批通道，不制造预期失败、
  不关闭 signing/hooks、不切换权限配置。
- push 结果不明时先核验远端 ref（如 `git ls-remote`），未发生才重试；已发生则用 resume 接口
  恢复。不拆成原始 Git 写命令，不 force、不 amend。
- tag、Release、Artifact 和 deployment 属于独立 Release 流程。
- 内容扫描误报只用 reference 定义的 exact `--fixture-exceptions`。

## 结束报告

最终回复说明实际 Git 结果：未运行、本地 commit `<oid>`、已推送并远端核验 `<oid>`，或在某
阶段受阻（同时给出已创建的 commit 和恢复条件）。只有 Finalizer 证据显示远端核验通过才能
称为“已发布”。有意保留未发布改动时说明原因。

需要跨会话追踪未发布或受阻的仓库改动时，可登记本地 queue，见
[references/unpublished-queue.md](references/unpublished-queue.md)；queue 失败不改变实际结果，
也不触发任何 Git 操作。

## 职责分离

Context Loader 只读上下文；Snapshot Runner 只收集只读快照、不运行测试、不提交；Git
Finalizer 只做提交前验证、显式路径暂存、commit、fast-forward-safe push 和发布后核验。审阅、
核验和验收由执行 agent 完成；仓库或平台明确要求独立审核时等待该审核。

## 停止发布的情况

以下只阻止 Finalizer/发布，不阻止实施、测试或审查：

- 所选模式超出授权；测试失败或审查有未解决 blocker。
- 显式文件范围无法只覆盖本任务改动，或存在无法隔离的 staged/他人改动。
- 远端已分叉或模式前提（upstream、ahead/behind、remote 状态）不满足。
- 继续需要原始 Git 写命令、自动冲突解决、history rewrite、force push 或重复 commit。
- 工具输出无法确认结果：先核验，不猜测、不伪造成功。
