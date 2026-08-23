---
name: three-tool-git-workflow
description: "用于实现、修复、重构或其他代码与仓库文件修改，按范围加载仓库上下文、选择必要测试与 Snapshot Runner 审查，并按用户授权边界选择 Git Finalizer 的只验证、本地提交或默认提交推送模式；也用于审查当前 diff 或分支、空远端 initial publish、失败或不确定结果的恢复，以及 Git Finalizer 或本地 Git 远端因 localhost、127.0.0.1、::1 沙箱边界失败时的正确 escalation 与诊断分流。不用于与仓库无关的普通问答，也不用于不涉及仓库文件、Git 状态或仓库级证据的纯系统、服务、数据库操作及只读信息查询。"
---

# Three-tool Git workflow

## 用途与触发

在以下任一条件成立时使用本 Skill：

- 实现、修复、重构或以其他方式修改代码或仓库文件。
- 需要加载仓库上下文，或决定必要测试与 Snapshot Runner 审查的合理范围。
- 需要审查当前 diff 或相对明确本地 base 的分支。
- 用户明确要求使用 Three-tool Git workflow，或明确授权 commit/push。
- 用户要求向完全空的 remote 首次发布，或发布已经存在的普通 local commits，或用已创建
  root commit 的完整 OID 恢复失败或不确定的 initial publish。

触发本 Skill 不等于必须运行全部三工具或全量测试。不得从“完成实现”“修复问题”或历史任务推导 commit/push 授权。

## 不适用场景

- 与仓库无关的普通问答。
- 不涉及仓库文件或 Git 状态的纯系统、服务或数据库操作。
- 不需要仓库级证据的纯只读信息查询。

只读仓库调查可使用本 Skill 的范围判断、上下文或审查规则，但不得因此机械运行测试、Snapshot Runner 或 Git Finalizer。

## 总体流程

Worktree Controller 是独立第 4 个生命周期工具；全局 `worktree-lifecycle` Skill 负责四工具的
端到端编排，本 Skill 不复制其预算、authority、handoff 或 release 算法。流程边界是：

1. 写任务先由 Worktree Controller 获取 lifecycle 状态，并按 policy plan/reuse/acquire；普通
   feature 不在 protected canonical 中实施。
2. 读取适用规则；在正式代码任务开始时按需使用 Context Loader 获取确定性的本地仓库上下文。调用和失败处理见 [references/context-loader.md](references/context-loader.md)。
3. 在三工具之外实施最小范围变更，并运行从定向到必要完整度的实际测试与检查。记录真实结果；任何工具输出都不能替代测试。
4. 按风险选择 Snapshot Runner 命令，默认显式使用 `--summary` 作首轮判断；仅在摘要异常、证据不足或任务需要具体内容时读取正式 artifact。命令选择、摘要充分性、展开条件和产物语义见 [references/snapshot-runner.md](references/snapshot-runner.md)。
5. 需要复用 Finalizer 的本地提交前验证、执行已授权 Git 写入或显式退役远端 feature branch 时，按下表及 reference 选择独立接口；所有模式默认显式启用 `--summary`，结果消费、展开和停止语义见 [references/git-finalizer.md](references/git-finalizer.md)。
6. Git Finalizer 只返回 commit/publication evidence；随后由 Worktree Controller 记录 handoff，
   并在 repository control plane 完成 integration/publication 后独立判断 release。Finalizer 不
   删除 local worktree 或 branch。

`toolchain_compatibility.json` 是总体 contract version 绑定。任一工具的 public contract 发生
incompatible change 而未同步更新该文件时，兼容检查必须失败。

简单、局部且不发布的任务可跳过 Runner；不要机械运行四个 Runner 命令。需要发布时，只选择足以证明发布门槛的最小相关 Runner 集合，通常至少审查当前 diff；不得因为任务简单而跳过 Git Finalizer 所要求的 Snapshot Runner 审查。

## Git Finalizer 模式选择

选择 Finalizer 模式不得扩大用户原有的 commit/push 授权：

| 用户目标与授权 | 实际接口 | 结果边界 |
| --- | --- | --- |
| 只验证，不授权 commit/push | `--mode verify-only` | 只读本地验证；不创建 commit，不访问远端 |
| 已授权本地 commit，未授权或暂不适合 push | `--mode commit-only` | 创建一个本地 commit；不访问远端 |
| unborn 仓库仅授权本地 root commit，远端未授权或目标有歧义 | `--initial-commit-only` | 创建一个本地 root commit；不配置或访问远端 |
| 已明确授权 commit 和 push，且任务已达到交付状态 | 默认模式（不传 `--mode`） | commit、push、远端 post-verify |
| 已存在普通 local commits，已明确授权继续 push | `--resume-publish <full-head-oid> --repo <absolute-repo>` | 不创建 commit；发布 configured upstream 并远端 post-verify |
| 已存在 clean feature branch、无 upstream、远端同名 branch 不存在，已明确授权首次发布 | `--publish-existing-branch <full-head-oid> --remote <name> --remote-branch <branch> --repo <absolute-repo>` | 不创建 commit；non-force 首次发布、设置 upstream 并远端 post-verify |
| 已有 local history，目标是经 repo-plan/repo-ensure 验证的全空 Gitea repository | `--publish-existing-history <full-head-oid> --remote <name> --remote-branch <branch> --repo <absolute-repo>` | 不创建 commit；全 remote refs 空检查、normal first push、远端 post-verify |
| existing-history first push 中断或结果不确定，且 remote 为空或仅有 expected target | `--resume-existing-history-publish <full-head-oid> --remote <name> --remote-branch <branch> --repo <absolute-repo>` | 不创建 commit；精确恢复并验证 upstream/remote `0/0` |
| 已完成 ancestry-provable integration，明确授权退役 remote feature ref | `--retire-remote-branch <branch> --remote <name> --integrated-into <branch> --expected-remote-oid <full-oid> --repo <absolute-repo>` | 不创建 commit；expected-OID compare-and-delete、远端 post-verify |
| 明确要求规划或确保一个空 Gitea repository | `--repo-plan` / `--repo-ensure` | 与 publication 分离；仅显式 ensure 可创建 repository，绝不 commit/push |

```bash
/home/hsd/bin/codex-git-finalize --summary --mode verify-only --repo <absolute-repo> -- <repo-relative-file>...
/home/hsd/bin/codex-git-finalize --summary --mode commit-only --repo <absolute-repo> --message <commit-message> -- <repo-relative-file>...
/home/hsd/bin/codex-git-finalize --summary --initial-commit-only --repo <absolute-repo> --message <commit-message> -- <repo-relative-file>...
/home/hsd/bin/codex-git-finalize --summary --repo <absolute-repo> --message <commit-message> -- <repo-relative-file>...
/home/hsd/bin/codex-git-finalize --summary --resume-publish <full-head-oid> --repo <absolute-repo>
/home/hsd/bin/codex-git-finalize --summary --publish-existing-branch <full-head-oid> --remote <name> --remote-branch <branch> --repo <absolute-repo>
/home/hsd/bin/codex-git-finalize --summary --publish-existing-history <full-head-oid> --remote <name> --remote-branch <branch> --repo <absolute-repo>
/home/hsd/bin/codex-git-finalize --summary --resume-existing-history-publish <full-head-oid> --remote <name> --remote-branch <branch> --repo <absolute-repo>
/home/hsd/bin/codex-git-finalize --summary --retire-remote-branch <branch> --remote <name> --integrated-into <branch> --expected-remote-oid <full-oid> --repo <absolute-repo> --dry-run
```

- 三种模式共用适用于各自执行边界的本地提交前检查；`verify-only` 不实际运行 commit hooks、签名或索引写入，因此不得声称验证了这些能力。
- `commit-only` 不要求 upstream，不执行 fetch、push、远端验证或其他远端操作。
- `initial-commit-only` 只接受 attached unborn branch 和空 index，创建一个本地 root commit，
  不配置或访问 remote；它不接受 `--snapshot` 或其他 publish mode。
- `verify-only` 不修改 HEAD、index、worktree、refs 或 Git 配置，也不要求 upstream。
- 三种提交生命周期模式中只有默认模式执行 commit 后的 push 和远端 post-verify；显式
  resume 接口也可发布既有 commit，但不创建 commit。
- 普通 attached branch 的既有 commits 使用 `--resume-publish`；它要求 clean index/worktree、
  configured upstream、`ahead >= 1`、`behind = 0`，只做 non-force、`--no-follow-tags` 的精确
  branch push 和远端 post-verify。root commit 继续使用 `--resume-initial-publish`。
- clean attached feature branch 尚无 upstream 且 explicit 同名 remote branch 不存在时，使用
  `--publish-existing-branch`；它绑定完整 HEAD OID，不创建 commit，拒绝受保护分支和任何既有
  upstream，并在 non-force 首次 push 后验证 upstream、remote OID 与 `0/0`。
- local repository 已有 history 且 Gitea target 已显式 repo-plan/repo-ensure 时，使用
  `--publish-existing-history`；它允许初始 protected branch，但要求整个 remote 无用户 Git refs。
  任一 branch/tag/其他 ref 都阻断；中断后只用 `--resume-existing-history-publish` 接受 empty 或
  expected-target-only 的精确远端形状。
- remote retirement 是与 publish mode 分离的显式 remote mutation。它只接受非受保护、非
  default 的 `refs/heads/*`，每次重新 fetch，要求 exact expected OID、ancestry、local lifecycle
  与 CI gate 通过，使用 lease-bound compare-and-delete，随后验证 remote 缺失和本地状态不变；
  不删除 local branch/worktree，不做 force push 或 semantic-equivalence retirement。
- 不得重跑会创建提交的模式或重复制造 commit。
- tag、Release、Artifact 和 deployment 继续属于独立 Release 流程，不并入任何 Git Finalizer 模式。

## 任务结束发布决策（强制）

Publication decision 只回答：当前任务结束前，是否应执行被明确授权的 Git Finalizer 操作。它不描述 Git 最终结果，也不得被当作“是否已经远端发布”的状态。任何任务在最终回复前都必须形成且只形成一个发布决定。涉及 Git 仓库修改时不得仅报告“未 commit/push”后结束；没有仓库修改的任务也必须按下列规则明确判定是否适用。按顺序选择首个符合的状态，后续状态不再适用：

1. `not_applicable`：本轮是只读调查、没有仓库文件变化、不是 Git 仓库操作、只运行服务或查询状态，或所有临时变化均已安全清理。
2. `publication_blocked`：任务本应进入已授权 finalization，但存在任务未完成，测试、验收或 Snapshot 未通过，cwd/repo 不一致，所选模式需要的远端分叉或不可访问，Hook/bridge 阻断，工作树无法安全分离，staged/index 异常，敏感内容、异常删除或未解释的范围外改动，或其他明确安全阻断。技术、范围或完成度阻断优先于“有意不发布”，不得用后者掩盖失败或异常。
3. `intentionally_unpublished`：仓库修改不存在上述阻断，但用户明确要求不 commit/push、仅保留本地修改或未提交草稿，或者当前 explicit-only 合同下没有获得明确发布授权。此状态表示有意保留未发布结果，不是遗漏。
4. `publish_now`：已获得明确且覆盖所选 Git 写模式的授权，实现完成，范围匹配的测试和验收通过，Snapshot Runner 无阻断，目标改动可与其他工作树改动安全分离，cwd/repo 及该模式要求的 upstream/远端状态可用，且不存在敏感内容、异常删除或未解释的范围外改动。当前应立即实际调用 Git Finalizer。`publish_now` 只是执行决策，不是最终结果；实际终态必须单独报告 Finalization outcome。

### 明确发布授权

保持 explicit-only。用户明确要求 commit、push、发布或使用 Git Finalizer，或当前任务合同明确规定验收后的具体 Git 动作，才构成相应授权；授权范围以用户明确要求的动作、仓库和文件为限。只验证选择 `verify-only`，仅要求 commit 选择 `commit-only` 且不得自行扩大为 push；只有明确授权 commit 和 push 才选择默认模式。已有普通 local commits 且只授权继续 push 时可选择 `--resume-publish`；无 upstream 的首次分支发布可选择 `--publish-existing-branch`，两者授权均不包含创建新 commit。“完成这个任务”“全权处理”“修复这个问题”以及一般实现、测试或验收要求都不构成 commit/push 授权，不得从模糊意图、历史任务或完成状态推导权限。

### Finalization scope

当 `Publication decision: publish_now` 时，必须在执行前按明确授权确定一个 scope：

```text
Finalization scope: <commit_only|commit_and_push>
```

- 用户只明确授权 commit：`commit_only`。
- 用户明确授权 commit + push、发布，或任务提示词明确规定验收后 commit + push：`commit_and_push`。
- 已有普通 local commits 且只授权继续 push 的 `--resume-publish` 也属于 `commit_and_push`，但不授权创建新 commit。
- 已有 clean local feature branch 首次发布的 `--publish-existing-branch` 也属于 `commit_and_push`，但不授权创建新 commit。
- 不得把 commit 授权自动扩大为 push 授权；只有 Git Finalizer 实际支持的模式才能执行。
- 非 `publish_now` 时使用 `not_applicable`；若是 `publication_blocked` 且阻断前已经明确授权 scope，可保留 `commit_only` 或 `commit_and_push`，不得猜测。

### Finalization outcome

所有任务最终回复都必须报告实际 Git 终态。允许状态仅为：

```text
not_run
local_commit_created
remote_pushed
remote_verified
blocked
```

- `not_run`：没有实际执行会创建 commit 或接触远端的 Finalizer 操作。只读任务、没有授权的修改任务和用户明确禁止发布的任务使用此状态；`verify-only` 不构成 Git finalization。
- `local_commit_created`：Git Finalizer 已成功创建本地 commit，但没有执行或没有授权执行 push。这是 `commit-only` 的正常成功终态，不得称为“已发布”。
- `remote_pushed`：push 确已成功，但当前执行没有完成实时远端 OID 核验。只有工具证据明确支持这一实际状态时才使用；不得为了凑状态主动跳过核验，也不得升级成 `remote_verified` 或声称完整发布成功。
- `remote_verified`：push 成功，且 Git Finalizer 已完成实时远端核验，满足 `HEAD = upstream = remote OID`。这是完整远端发布成功终态。
- `blocked`：Finalizer preflight 或执行被阻断。必须报告阻断阶段；如果阻断前已经创建本地 commit，正文还必须报告：

```text
Local commit: <oid>
Remote publication: blocked
```

不得只靠一个 outcome 丢失已经创建本地 commit 的事实。

### 合法组合

| Publication decision | Finalization scope | Finalization outcome |
| --- | --- | --- |
| `not_applicable` | `not_applicable` | `not_run` |
| `intentionally_unpublished` | `not_applicable` | `not_run` |
| `publish_now` | `commit_only` | `local_commit_created` |
| `publish_now` | `commit_and_push` | `remote_pushed` 或 `remote_verified` |
| `publication_blocked` | `not_applicable`、`commit_only` 或 `commit_and_push` | `blocked` |

`commit_only + remote_pushed|remote_verified` 非法；`publish_now + not_run` 不能作为正常完成终态；`remote_verified` 只能与 `commit_and_push` 配对。

`intentionally_unpublished + local_commit_created` 默认非法。只有在复合历史报告必须明确保留此前阶段已创建的 commit 时，才可使用 `commit_only + local_commit_created`，并在正文增加：

```text
Prior-stage local commit: <oid>
```

该兼容说明不构成当前任务的新 commit/push 授权，不得扩展为一般新任务合同。

历史 commit-only fixture 如果确有明确 commit 授权且 Finalizer 已创建本地 commit，无论旧报告曾写 `publish_now` 还是 `intentionally_unpublished`，按新合同都归一为：

```text
Publication decision: publish_now
Finalization scope: commit_only
Finalization outcome: local_commit_created
```

### 最终报告合同

最终回复必须各包含且只包含一行：

```text
Publication decision: <publish_now|publication_blocked|intentionally_unpublished|not_applicable>
Finalization scope: <commit_only|commit_and_push|not_applicable>
Finalization outcome: <not_run|local_commit_created|remote_pushed|remote_verified|blocked>
```

典型组合：

```text
Publication decision: publish_now
Finalization scope: commit_only
Finalization outcome: local_commit_created
```

```text
Publication decision: publish_now
Finalization scope: commit_and_push
Finalization outcome: remote_verified
```

```text
Publication decision: intentionally_unpublished
Finalization scope: not_applicable
Finalization outcome: not_run
```

```text
Publication decision: publication_blocked
Finalization scope: commit_and_push
Finalization outcome: blocked
```

并只补充与实际状态对应的必要信息：

- `publish_now`：所选模式或恢复接口、commit 结果，以及所选接口实际发生的 push 和远端核验结果；不得从 decision 推导 outcome。
- `publication_blocked`：阻断阶段和原因、是否已创建 commit（如有则给出 OID）、当前工作树摘要和恢复条件；scope 使用阻断前已知授权范围。
- `intentionally_unpublished`：未发布原因、仍未提交的目标改动摘要，以及是否适合后续单独发布；不得把范围不明、测试失败或远端异常描述为有意未发布。
- `not_applicable`：不适用原因。

保持内部完整检查、外部摘要报告；默认不输出长路径清单，只有异常、删除、范围外改动或无法用摘要解释风险时才展开。

### Completed-but-Unpublished Queue

对预计会修改的 Git repo，任务开始时可调用本地 queue `summary`；无 active record 时它不输出，
有记录时只显示该 repo 的 pending/blocked 数量与最早日期，不得把完整 backlog 自动注入 context。
helper、schema、权限、CLI 和 bounded review 合同见
[Completed-but-Unpublished Queue reference](references/unpublished-queue.md)。

任务结束且存在持久 repo 修改时，在最终回复前按实际三行 finalization 事实处理 queue：

- `not_applicable` 或没有既有 record 的 `publish_now + remote_verified`：不创建 active record；
- `intentionally_unpublished`：幂等 upsert 为 `pending`；
- `publication_blocked`：幂等 upsert 为 `blocked`；
- Finalizer 以 `publish_now` 开始但实际 outcome 为 `blocked`：可按该执行事实 upsert `blocked`，但
  最终三行仍必须使用主合同的 `publication_blocked + blocked` 合法组合；
- `remote_pushed` 未完成实时核验：upsert `blocked`；
- `commit_only + local_commit_created`：upsert `pending`；
- 已有 record 获得 `remote_verified`，或 bounded review 以明确 remote OID 证明 exact upstream
  equivalence：只关闭 metadata 为 `published`，不得修改原 worktree。

Queue 写入失败不得改写真实 Publication decision/scope/outcome，也不得触发自动 Snapshot、
commit、push、stash、reset、restore 或 clean；最终回复应简短报告 record ID，或明确
`record_failed` 及原因。Git Finalizer 只提供 publication evidence，不拥有 queue。

## 职责分离

- Context Loader 只加载确定性的本地上下文；不实施、不测试、不审查、不发布。
- 实现与测试由常规开发命令完成；不由三个生命周期工具代替。
- Snapshot Runner 只收集并发布供人工检查的只读快照；不修改目标仓库 Git 状态、不运行测试、不自动调用模型、不 commit/push。
- Git Finalizer 只做适用的本地提交前验证、显式路径暂存、commit，以及由默认或显式发布/恢复接口授权的 fast-forward-safe push 和发布后验证；不加载上下文、不实施、不测试、不审查。

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

- 所选模式超出用户明确授权的 commit/push 边界。
- 测试失败、必要测试未运行或审查未通过。
- 当前轮范围、文件所有权或已有改动来源不清，显式文件范围不能覆盖且仅覆盖当前轮改动，或存在无法安全隔离的 staged/用户改动。
- Runner 有未解决 blocker，或其产物和证据缺口不足以支持发布结论。
- 对需要远端的模式，远端已分叉，或分支、upstream、remote、ahead/behind、initial/resume 状态不满足 [Git Finalizer reference](references/git-finalizer.md) 的模式前提。
- 高风险操作尚未取得针对确切动作、目标和范围的确认。
- 继续需要原始 Git 写命令、自动冲突解决、历史重写、force push、amend 或重复创建 commit。
- 工具失败、输出不完整或结果无法确认；不得绕过、猜测或伪造成功。
