---
name: three-tool-git-workflow
description: "用于实现、修复、重构或其他代码与仓库文件修改，按范围加载仓库上下文、选择必要测试与 Snapshot Runner 审查，并在用户明确授权且全部发布门槛满足后通过 Git Finalizer commit/push；也用于审查当前 diff 或分支、空远端 initial publish、失败或不确定结果的恢复，以及 Git Finalizer 或本地 Git 远端因 localhost、127.0.0.1、::1 沙箱边界失败时的正确 escalation 与诊断分流。不用于与仓库无关的普通问答，也不用于不涉及仓库文件、Git 状态或仓库级证据的纯系统、服务、数据库操作及只读信息查询。"
---

# Three-tool Git workflow

## 用途与触发

在以下任一条件成立时使用本 Skill：

- 实现、修复、重构或以其他方式修改代码或仓库文件。
- 需要加载仓库上下文，或决定必要测试与 Snapshot Runner 审查的合理范围。
- 需要审查当前 diff 或相对明确本地 base 的分支。
- 用户明确要求使用 Three-tool Git workflow，或明确授权 commit/push。
- 用户要求向完全空的 remote 首次发布，或用已创建 root commit 的完整 OID 恢复失败或不确定的 initial publish。

触发本 Skill 不等于必须运行全部三工具或全量测试。不得从“完成实现”“修复问题”或历史任务推导 commit/push 授权。

## 不适用场景

- 与仓库无关的普通问答。
- 不涉及仓库文件或 Git 状态的纯系统、服务或数据库操作。
- 不需要仓库级证据的纯只读信息查询。

只读仓库调查可使用本 Skill 的范围判断、上下文或审查规则，但不得因此机械运行测试、Snapshot Runner 或 Git Finalizer。

## 总体流程

1. 读取适用规则；在正式代码任务开始时按需使用 Context Loader 获取确定性的本地仓库上下文。调用和失败处理见 [references/context-loader.md](references/context-loader.md)。
2. 在三工具之外实施最小范围变更，并运行从定向到必要完整度的实际测试与检查。记录真实结果；任何工具输出都不能替代测试。
3. 按风险选择 Snapshot Runner 命令，默认显式使用 `--summary` 作首轮判断；仅在摘要异常、证据不足或任务需要具体内容时读取正式 artifact。命令选择、摘要充分性、展开条件和产物语义见 [references/snapshot-runner.md](references/snapshot-runner.md)。
4. 仅在用户已明确授权发布、实现完成、相关测试通过、审查通过、Runner 阻断已解决且当前轮文件范围可精确隔离时，使用 Git Finalizer；normal、initial、resume 默认显式启用 `--summary`，结果消费、展开和停止语义见 [references/git-finalizer.md](references/git-finalizer.md)。

简单、局部且不发布的任务可跳过 Runner；不要机械运行四个 Runner 命令。需要发布时，只选择足以证明发布门槛的最小相关 Runner 集合，通常至少审查当前 diff；不得因为任务简单而跳过 Git Finalizer 所要求的 Snapshot Runner 审查。

## 任务结束发布决策（强制）

任何任务在最终回复前都必须形成且只形成一个发布决定。涉及 Git 仓库修改时不得仅报告“未 commit/push”后结束；没有仓库修改的任务也必须按下列规则明确判定是否适用。按顺序选择首个符合的状态，后续状态不再适用：

1. `not_applicable`：本轮是只读调查、没有仓库文件变化、不是 Git 仓库操作、只运行服务或查询状态，或所有临时变化均已安全清理。
2. `publication_blocked`：存在任务未完成，测试、验收或 Snapshot 未通过，cwd/repo 不一致，远端分叉或不可访问，Hook/bridge 阻断，工作树无法安全分离，staged/index 异常，敏感内容、异常删除或未解释的范围外改动，或其他发布停止条件。技术、范围或完成度阻断优先于“有意不发布”，不得用后者掩盖失败或异常。
3. `intentionally_unpublished`：仓库修改不存在上述阻断，但用户明确要求不 commit/push、仅保留本地修改或未提交草稿，或者当前 explicit-only 合同下没有获得明确发布授权。此状态表示有意保留未发布结果，不是遗漏。
4. `publish_now`：已获得明确且覆盖实际动作的发布授权，实现完成，范围匹配的测试和验收通过，Snapshot Runner 无阻断，目标改动可与其他工作树改动安全分离，cwd、repo、upstream 和远端状态可用，且不存在敏感内容、异常删除或未解释的范围外改动。进入该状态后必须在最终回复前实际调用 Git Finalizer；只有 Finalizer 和发布后核验成功时最终状态才是 `publish_now`，否则改为 `publication_blocked`。

### 明确发布授权

保持 explicit-only。用户明确要求 commit、push、发布或使用 Git Finalizer，或当前任务合同明确规定验收后提交推送，才构成发布授权；授权范围以用户明确要求的动作、仓库和文件为限，仅要求 commit 不得自行扩大为 push。“完成这个任务”“全权处理”“修复这个问题”以及一般实现、测试或验收要求都不构成发布授权，不得从模糊意图、历史任务或完成状态推导权限。

### 最终报告合同

最终回复必须包含一行：

```text
Publication decision: <publish_now|publication_blocked|intentionally_unpublished|not_applicable>
```

并只补充与所选状态对应的必要信息：

- `publish_now`：commit、push 和远端核验的实际结果。
- `publication_blocked`：阻断阶段和原因、是否已创建 commit（如有则给出 OID）、当前工作树摘要和恢复条件。
- `intentionally_unpublished`：未发布原因、仍未提交的目标改动摘要，以及是否适合后续单独发布；不得把范围不明、测试失败或远端异常描述为有意未发布。
- `not_applicable`：不适用原因。

保持内部完整检查、外部摘要报告；默认不输出长路径清单，只有异常、删除、范围外改动或无法用摘要解释风险时才展开。

## 职责分离

- Context Loader 只加载确定性的本地上下文；不实施、不测试、不审查、不发布。
- 实现与测试由常规开发命令完成；不由三个生命周期工具代替。
- Snapshot Runner 只收集并发布供人工检查的只读快照；不修改目标仓库 Git 状态、不运行测试、不自动调用模型、不 commit/push。
- Git Finalizer 只做显式路径暂存、commit、fast-forward-safe push 和发布后验证；不加载上下文、不实施、不测试、不审查。

### Git Finalizer 命令级 escalation（强制）

- 普通 Codex 调用 `/home/hsd/bin/codex-git-finalize` 的任何非 `--dry-run` 模式时，第一次调用就必须把完整 Finalizer 命令作为一次原生 shell 工具调用提交，并显式设置 `sandbox_permissions: "require_escalated"` 和面向用户的 `justification`。可选的 `prefix_rule` 只能限定同类审批范围，不能替代 `sandbox_permissions`。
- 不得先在沙箱内试运行 Finalizer，也不得把它拆成沙箱内的 `git add`、`commit`、`fetch` 或 `push`。完整 Finalizer 进程必须在同一次获批的沙箱外调用中完成。
- 若 remote host 是 `localhost`、`127.0.0.0/8` 或 `::1`，任何实际接触远端的只读探测（如 `git ls-remote`）也必须直接申请单命令 escalation；读取 `git remote get-url` 等纯本地配置不需要 escalation。具体调用格式见 [Git Finalizer reference](references/git-finalizer.md)。
- 沙箱内的 `connection refused`、无法访问 systemd bus、看不到宿主机监听端口等结果只说明沙箱边界，不能证明宿主机 SSH/Gitea 服务故障。不得据此启动、停止或修改服务，不得修改 remote、密钥、防火墙或监听地址。只有同一 Git 远端命令在沙箱外仍失败，才可进入真实服务诊断，并先保持只读。
- 若当前 shell 工具不支持单命令 escalation，或当前 approval policy 不允许提出该申请，停止并报告能力缺口；不得改用 `codex-admin`、全局 `danger-full-access` 或通用网络放行绕过。

Context Loader 与 Snapshot Runner 仍保持各自的只读及原有权限边界。

不得让任一工具替代另一工具的结论，也不得把同一职责同时归给多个工具。

## 发布停止条件

以下条件只阻止 Git Finalizer 和发布，不阻止本 Skill 用于上下文加载、实施、测试范围判断或只读审查。出现任一条件时停止发布，报告已验证事实和最小安全下一步：

- 用户未明确授权 commit/push。
- 测试失败、必要测试未运行或审查未通过。
- 当前轮范围、文件所有权或已有改动来源不清，显式文件范围不能覆盖且仅覆盖当前轮改动，或存在无法安全隔离的 staged/用户改动。
- Runner 有未解决 blocker，或其产物和证据缺口不足以支持发布结论。
- 远端已分叉，或分支、upstream、remote、ahead/behind、initial/resume 状态不满足 [Git Finalizer reference](references/git-finalizer.md) 的模式前提。
- 高风险操作尚未取得针对确切动作、目标和范围的确认。
- 继续需要原始 Git 写命令、自动冲突解决、历史重写、force push、amend 或重复创建 commit。
- 工具失败、输出不完整或结果无法确认；不得绕过、猜测或伪造成功。
