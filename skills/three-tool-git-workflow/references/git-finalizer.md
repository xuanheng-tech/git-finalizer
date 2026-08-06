# Git Finalizer

## 职责、入口与模式门槛

Git Finalizer 只负责适用的本地提交前验证、显式路径暂存、commit，以及仅默认模式下的 fast-forward-safe push 和远端 post-verify；它不实施代码、不运行测试、不审查 diff，也不解决冲突。initial、initial-branch 和 resume 仍是独立的首次发布或恢复接口。

选择模式不得扩大用户原有的 commit/push 授权：只验证使用 `--mode verify-only`，只授权本地 commit 使用 `--mode commit-only`，只有明确授权 commit 和 push 且任务达到交付状态才使用默认模式。调用绝对入口 `/home/hsd/bin/codex-git-finalize` 前，还必须满足：

- 实现已完成，相关测试和检查实际通过；
- Snapshot Runner 的相关审查已通过，阻断项已经解决；
- 当前轮范围、文件所有权和已有改动来源清楚，授权改动可独立隔离；
- status 和 diff 已核对，候选范围保持聚焦；
- `--` 后逐一列出每个允许验证或暂存的显式 repository-relative 文件路径。

### 普通 Codex 原生调用契约（强制）

对 verify-only、commit-only、normal、initial 和 resume 的所有非 `--dry-run` 调用：

1. 使用当前会话的原生 shell 执行工具，把从 `/home/hsd/bin/codex-git-finalize` 到最后一个显式文件参数的完整命令放在同一个 `cmd` 中。
2. 同一次工具调用必须设置 `sandbox_permissions: "require_escalated"`、仓库 `workdir` 和清晰且与所选模式一致的 `justification`；verify-only 或 commit-only 的理由不得请求或暗示远端权限。若工具支持持久审批前缀，可使用 `prefix_rule: ["/home/hsd/bin/codex-git-finalize"]`；该前缀不改变 sandbox，不能替代 `require_escalated`。
3. 不得使用 PATH 简写、`bash -lc`、`sh -c`、其他 wrapper 或多个工具调用拼接 Finalizer 生命周期。
4. 不得先在沙箱内执行一次再根据失败申请 escalation；Finalizer 的首次真实调用就必须在沙箱外。

原生工具参数示例：

```json
{
  "cmd": "/home/hsd/bin/codex-git-finalize --summary --repo /absolute/repo --message 'message' -- path/to/file",
  "workdir": "/absolute/repo",
  "sandbox_permissions": "require_escalated",
  "justification": "允许 Git Finalizer 对已审查的显式文件执行 commit、fetch、push 和远端核验吗？",
  "prefix_rule": ["/home/hsd/bin/codex-git-finalize"]
}
```

### `--summary` 结果消费

verify-only、commit-only、normal、initial、initial-branch 和 resume 默认显式加入 `--summary`；即使短场景的摘要略长，也不退回非结构化默认输出。摘要是机器消费接口，但不能替代进程退出码、Finalizer 安全判定或所选模式要求的实际 Git 状态。

优先读取实际 JSON 字段：`status`、`final_phase`、`mode`、`repository`、`branch`、`upstream`、`requested_path_count`、`commit`、`push`、`mode_result`、`warnings`、`reason`、`next_action`，以及存在时的 `resume`。所有模式都要求退出码为零、`status=success`、模式与请求一致、没有影响决策的 warning 或恢复引用，且摘要与实际状态一致；另外按模式核对：

- verify-only：本地验证通过，`commit.created=false`，push、远端验证和 post-verify 均明确因该模式跳过，HEAD、index 和 worktree 前后不变；不得把成功扩写为已验证 commit hooks、签名、index 写入能力或远端状态。
- commit-only：`commit.created=true` 且有新 commit OID，`push.executed=false`、push/远端验证/post-verify 均明确因该模式跳过，最终工作区状态已报告。
- 默认、initial、initial-branch 和 resume：按各模式合同核对实际 push 与远端 post-verify；需要 push 的成功结果必须包含 `push.executed=true`、`push.result=succeeded`、显式 branch refspec、`push.branch_refspec_only=true` 和 `push.follow_tags_requested=false`。

出现非零退出码，`status` 为 `blocked`、`failed` 或 `partial`，结果不满足所选模式合同，warning 影响范围、安全或发布，存在 `resume`，摘要与实际状态不一致，或用户要求详细证据时必须展开。展开时保留 `final_phase`、`reason`、恢复引用和 `next_action`，并按现有停止或恢复流程处理；不得把模式约定的跳过描述为已执行，也不得把失败、不确定结果或未执行操作描述为成功。

如果当前工具没有单命令 `require_escalated` 能力，或 approval policy 拒绝提出该申请，停止发布并报告；不得切换到 `codex-admin`、全局 `danger-full-access` 或通用网络权限。

### 本机 Git remote 与失败分流

- 可先用 `git remote get-url --all <remote>` 和 `git remote get-url --push --all <remote>` 读取本地配置。若 endpoint host 是 `localhost`、`127.0.0.0/8` 或 `::1`，把它视为宿主机 loopback remote；不要修改 endpoint。
- 对 loopback remote，`git ls-remote`、`git fetch`、`git push` 等实际接触远端的命令必须在第一次调用时使用单命令 `require_escalated`。只读验收可直接对完整 `git ls-remote origin` 调用申请 escalation。
- 沙箱内出现 `connection refused`、systemd bus 不可见、`ss` 看不到宿主机监听端口或类似结果时，只能判定为沙箱证据，不得判定 SSH/Gitea 已停止。不要启动、停止或修改 `ssh.service`、`gitea.service`，也不要改 remote、SSH key、防火墙或监听地址。
- 只有同一条 Git 远端命令在沙箱外仍失败，才进入真实服务诊断；先进行只读的 endpoint、进程和日志核对，任何服务或系统变更仍需另行明确授权。

不得用 `git add .`、`git add -A`、原始 `git commit` 或原始 `git push` 绕过 Finalizer。不得自动 amend、force push、切换或创建分支、pull、merge、rebase、stash、解决冲突或重写历史；不得使用 `git reset --hard`、`git clean`、破坏性 checkout 或强制删除分支来丢弃改动。

## 已有历史分支的三种模式

三种模式共用适用于各自执行边界的路径规范化、显式范围、已有 staged scope、冲突、敏感内容、大文件和 whitespace 等本地提交前检查。verify-only 不执行会证明写入或 commit 能力的步骤；commit-only 与默认模式才执行实际暂存和 commit。

### Verify only

```bash
/home/hsd/bin/codex-git-finalize \
  --summary \
  --mode verify-only \
  --repo <absolute-repo> \
  -- <repo-relative-file>...
```

verify-only 要求已有 commit 的 attached local branch，但不要求 upstream。它不接受 `--message`，不执行 `git add`、commit、fetch、`ls-remote`、push 或任何其他远端命令，也不修改 HEAD、index、worktree、refs、tag 或 Git 配置。成功只证明适用的只读本地提交前验证通过且所报告状态前后不变；不证明 commit hooks、author/签名、index 实际写入能力、远端状态或 fast-forward 条件。

### Commit only

```bash
/home/hsd/bin/codex-git-finalize \
  --summary \
  --mode commit-only \
  --repo <absolute-repo> \
  --message <commit-message> \
  -- <repo-relative-file>...
```

commit-only 用于已授权创建本地 commit、但未授权或暂不适合 push 的任务。它要求已有 commit 的 attached local branch，不要求 upstream，不执行 fetch、`ls-remote`、push、远端验证或其他远端操作，也不修改 remote、tag 或 Git 配置。成功必须报告新 commit OID、因该模式跳过的 push/远端验证及最终工作区状态。

## Normal publish

调用格式：

```bash
/home/hsd/bin/codex-git-finalize \
  --summary \
  --repo <absolute-repo> \
  --message <commit-message> \
  -- <repo-relative-file>...
```

normal 模式要求当前 HEAD 是已有 commit 的 attached local branch，且存在唯一、精确、可解析的远端 branch upstream。有效 push remote 必须与 upstream remote 一致，remote 的 fetch/push endpoint 必须唯一且相同。

Finalizer 在 commit 前 fetch 并核对远端 branch、upstream OID 和 ahead/behind；本地落后或已经分叉时停止。通过后，它只对显式路径执行 literal-pathspec add，拒绝显式范围外的 staged 文件、空 staged diff、未解决 index 冲突、`git diff --cached --check` 失败以及内建敏感路径、内容或大二进制保护命中的文件。创建 commit 后再次 fetch 和检查 fast-forward 条件，再 push 到已验证的 upstream branch，最后核对 HEAD、upstream 和远端 branch OID 一致且 ahead/behind 为 `0/0`。

如果 commit 已创建但后续 fetch、push 或验证失败，保留并报告该 commit；不得自动 pull、merge、rebase、reset，不得重跑会创建另一 commit 的发布流程，也不得仅因 push 失败而 recommit。

当前已经存在目标本地 commit、只需继续推送时，不得运行默认或 commit-only 模式重复制造 commit。必须使用前一次 Finalizer 结果给出的既有 resume/恢复入口；initial root commit 使用下述 `--resume-initial-publish`，其他情形只执行工具明确给出的安全恢复动作，没有明确入口时停止并报告。

## Initial publish

新项目的完整顺序是：创建空 remote → `git clone`（或 `git init` 加 `git remote add`）→ Context Loader → 实现与测试 → Snapshot Runner → Git Finalizer `--initial-publish` → 后续使用 normal 模式。

调用格式：

```bash
/home/hsd/bin/codex-git-finalize \
  --summary \
  --initial-publish \
  --remote <remote-name> \
  --repo <absolute-repo> \
  --message <commit-message> \
  -- <repo-relative-file>...
```

initial 模式仅接受 symbolic attached 的 unborn local branch、本地没有任何可解析 commit、指定 remote 已配置且完全为空的情形。index 不得已有条目，worktree 中的全部变更必须都在显式文件范围内。remote 的 fetch/push endpoint 必须唯一且相同。

通过检查后，Finalizer 创建第一个非空 root commit，复查 remote 仍为空，执行带 upstream 建立语义的 initial push，并验证 remote 只有目标 branch，且本地 HEAD、upstream 和远端 OID 相同、ahead/behind 为 `0/0`、worktree 与 index 干净。不得预先创建空 root commit，也不得手工执行 `git push --set-upstream`。

如果 root commit 已创建但 push 失败或结果不确定，保留并记录工具报告的完整 root OID；不得重跑 `--initial-publish`，不得创建第二个 commit，必须转入精确 resume 流程。

## Configured-but-unresolved upstream

已配置但尚不能解析 commit 的 upstream 是有效状态，典型例子是 clone 空 remote 后配置已指向 `origin/main`，但对应 remote-tracking commit 尚不存在。

必须区分：

- upstream 是本地 branch 配置中的 tracking target；
- upstream commit 是否可解析，以及 ahead/behind 是否可计算，是另一件事；
- 这些信息来自本地 Git config/refs，不代表实时 remote 状态。

initial 和 resume 可以接受与所选 remote/branch 精确匹配的 configured-but-unresolved upstream；冲突、含糊或指向其他目标的配置必须停止。normal 模式则要求 upstream 已精确解析。initial 仍必须实时确认 remote 完全为空。

## Resume initial publish

仅当 `--initial-publish` 已经创建 root commit，但 initial push 失败或结果不确定时使用：

```bash
/home/hsd/bin/codex-git-finalize \
  --summary \
  --resume-initial-publish <full-root-oid> \
  --remote <remote-name> \
  --repo <absolute-repo>
```

`<full-root-oid>` 必须是该工具报告的确切完整小写 OID，并与当前 HEAD 完全一致。resume 不接受 `--message`、文件列表、`--dry-run` 或新的 commit 内容。

resume 要求当前 HEAD 仍是唯一、非空的 root commit，symbolic branch 未改变，index 和包含 untracked 文件在内的 worktree 完全干净，且不存在未完成的 merge、rebase、cherry-pick、revert、bisect 或 sequencer 操作。所选 remote 必须已配置且 endpoint 唯一一致；upstream 只能缺失、精确未解析，或精确解析到该 OID。

远端只允许两种安全状态：完全为空，或仅有目标 branch 且它正好指向该 root OID。存在额外 ref、目标 branch 指向其他 OID 或结果含糊时停止。Finalizer 根据远端和 upstream 状态执行必要的 set-upstream push，不创建 commit，最后验证 HEAD、唯一 root commit、远端 branch、upstream、`0/0`、干净 index/worktree 全部一致。可重试提示只适用于工具明确标为可安全重试的同一 resume 命令。

## `--dry-run`

normal、commit-only 和 initial 模式可增加 `--dry-run`，用于报告显式范围和静态风险。它不执行 `git add`、commit、fetch 或 push；也不实时证明远端 branch、ahead/behind 或空 remote 状态。verify-only 本身是严格只读验证且不接受 `--dry-run`；resume 不支持 dry-run。dry-run 不替代测试、Runner 审查或真实发布后的验证。

## Release 流程边界

tag、Release、Artifact 和 deployment 是独立 Release 流程，必须按仓库既有文档、recipe 或自动化另行授权和执行；它们不属于 verify-only、commit-only 或默认模式，也不得从任何 Finalizer 成功结果推导其授权或完成状态。

## 必须停止的情况

- 所选模式超出用户明确授权的 commit/push 边界，或操作属于尚未确认的高风险范围。
- 相关测试失败、必要检查未运行、审查未通过或 Runner 有未解决阻断。
- 文件范围或所有权不清、当前轮改动无法隔离、已有 staged 文件超出显式范围。
- normal 模式为 detached/unborn HEAD、upstream 缺失或不可解析、remote 配置含糊、本地落后或分叉。
- initial 模式发现本地已有 commit、预存 index、范围外 worktree 变更、可解析或冲突 upstream、remote 非空。
- resume 的 OID、HEAD、root commit、branch、cleanliness、upstream 或 remote 精确状态不满足要求。
- index 有冲突、范围检查或安全检查失败、工具失败，verify-only 发生任何状态变化，或需要远端的模式出现 push 结果不确定或 post-verify 不一致。
- 继续需要 amend、force push、重复创建 commit、自动冲突处理，或任何原始 Git 写命令绕过 Finalizer。

停止时报告工具是否已经创建 commit、确切 commit OID、已验证与未验证状态，以及工具给出的安全恢复入口。不得把失败或不确定结果描述为发布成功。
