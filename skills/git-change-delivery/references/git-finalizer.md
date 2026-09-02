# Git Finalizer

## 职责、入口与模式门槛

Git Finalizer 只负责适用的本地提交前验证、显式路径暂存、commit，以及由默认或显式发布/恢复接口授权的 fast-forward-safe push 和远端 post-verify；它不实施代码、不运行测试、不审查 diff，也不解决冲突。initial、initial-branch 和 resume 仍是独立的首次发布或恢复接口。

`--repo-plan` / `--repo-ensure` 是与 commit/publication 分离的 Gitea repository bootstrap
接口。plan 只读；ensure 只创建或验证一个显式 owner/name/visibility 的空 repository，并可在
指定 remote 完全缺失时添加精确 origin。credential 仅由 Git credential authority 提供，CLI、
remote URL、日志和 receipt 均不得携带 token。bootstrap 成功不表示 publication 已运行。

已有一个或多个 local commits 首次发布到上述 bootstrap 后的 verified-empty Gitea repository
时，使用 `--publish-existing-history`。它与 unborn `--initial-publish`、普通 feature
`--publish-existing-branch` 和 configured-upstream `--resume-publish` 都是独立接口；中断恢复只
使用 `--resume-existing-history-publish`。

远端 feature branch retirement 是独立操作，不属于 commit/publish mode。它只在明确授权下用
expected-OID lease 退役已 ancestry-integrated 的远端 branch，不删除 local worktree/branch。

Controller schema v2 integration candidate publication 也是独立接口。它不创建 commit，不从
canonical checkout 发布，也不由 caller 另行选择 remote/main；只消费 Controller 已签发的同一
publication lease，并把 verified receipt 交回 Controller 完成 lifecycle。

选择模式不得扩大用户原有的 commit/push 授权：只验证使用 `--mode verify-only`，只授权本地 commit 使用 `--mode commit-only`，只有明确授权 commit 和 push 且任务达到交付状态才使用默认模式。调用绝对入口 `/home/hsd/bin/codex-git-finalize` 前，还必须满足：

- 实现已完成，相关测试和检查实际通过；
- Snapshot Runner 的相关审查已通过，阻断项已经解决；
- 当前轮范围、文件所有权和已有改动来源清楚，授权改动可独立隔离；
- status 和 diff 已核对，候选范围保持聚焦；
- `--` 后逐一列出每个允许验证或暂存的显式 repository-relative 文件路径。

### 普通 Codex 原生调用契约（强制）

对 verify-only、commit-only、normal、initial 和 resume 的所有非 `--dry-run` 调用：

1. 使用当前会话的原生 shell 执行工具，把从 `/home/hsd/bin/codex-git-finalize` 到最后一个显式文件参数的完整命令放在同一个 `cmd` 中。
2. `permission_mode=default` 的旧式路径在同一次工具调用中设置 `sandbox_permissions: "require_escalated"`、仓库 `workdir` 和与所选模式一致的 `justification`。若当前运行时已经以 `approval_policy=never` 的当前 Linux 用户 direct execution 产生 `permission_mode=bypassPermissions`，则只传完整命令与精确 `workdir`，不添加该工具合同拒绝的本地 escalation 字段。bridge 在 direct 路径拒绝 root、transcript escalation marker、其他 permission mode、wrapper 和未知参数；不得由 caller 自行切换 permission profile。
3. 不得使用 PATH 简写、`bash -lc`、`sh -c`、其他 wrapper 或多个工具调用拼接 Finalizer 生命周期。
4. 不得先在另一个权限边界执行一次再重试；Finalizer 的首次真实调用就必须走适用于当前会话的同一正式 bridge boundary。

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

Codex 已经以 current-user direct mode 运行时，对应调用只保留相同完整命令和精确工作目录：

```json
{
  "cmd": "/home/hsd/bin/codex-git-finalize --summary --repo /absolute/repo --message 'message' -- path/to/file",
  "workdir": "/absolute/repo"
}
```

是否属于该路径由运行时产生的 `permission_mode=bypassPermissions` 和 bridge 验证决定，caller
不得通过参数或配置自行声明。

### `--summary` 结果消费

verify-only、commit-only、normal、initial、initial-branch 和 resume 默认显式加入 `--summary`；即使短场景的摘要略长，也不退回非结构化默认输出。摘要是机器消费接口，但不能替代进程退出码、Finalizer 安全判定或所选模式要求的实际 Git 状态。

优先读取实际 JSON 字段：`status`、`final_phase`、`mode`、`repository`、可空的
`repository_id` / `allocation_id` / `task_key` / `authority_key` / `worktree_path` / `role`、
`branch`、`upstream`、`requested_path_count`、`commit`、`push`、`mode_result`、`warnings`、
`reason`、`next_action`，以及存在时的 `resume`。Controller linkage 缺失时允许为 null，不得
伪造。所有模式都要求退出码为零、`status=success`、模式与请求一致、没有影响决策的 warning
或恢复引用，且摘要与实际状态一致；另外按模式核对：

- verify-only：本地验证通过，`commit.created=false`，push、远端验证和 post-verify 均明确因该模式跳过，HEAD、index 和 worktree 前后不变；不得把成功扩写为已验证 commit hooks、签名、index 写入能力或远端状态。
- commit-only：`commit.created=true` 且有新 commit OID，`push.executed=false`、push/远端验证/post-verify 均明确因该模式跳过，最终工作区状态已报告。
- 默认、initial、initial-branch 和 resume：按各模式合同核对实际 push 与远端 post-verify；需要 push 的成功结果必须包含 `push.executed=true`、`push.result=succeeded`、显式 branch refspec、`push.branch_refspec_only=true` 和 `push.follow_tags_requested=false`。
- integration candidate publish：要求 `mode=integration_candidate_publish`、`status=success`、
  `remote_verify.status=verified` 且 remote OID 等于 candidate；首次执行为
  `push.result=published`，中断恢复可为 `already_published_recovered` 且
  `push.executed=false`。两者都必须返回同一 lease/run/candidate identity 和 `record_id`。

出现非零退出码，`status` 为 `blocked`、`failed` 或 `partial`，结果不满足所选模式合同，warning 影响范围、安全或发布，存在 `resume`，摘要与实际状态不一致，或用户要求详细证据时必须展开。展开时保留 `final_phase`、`reason`、恢复引用和 `next_action`，并按现有停止或恢复流程处理；不得把模式约定的跳过描述为已执行，也不得把失败、不确定结果或未执行操作描述为成功。

如果当前工具没有单命令 `require_escalated` 能力且运行时也未提供合法的 current-user direct `bypassPermissions`，停止发布并报告；不得切换到 `codex-admin`、sudo、root、伪造 permission mode 或通用网络权限。

### 本机 Git remote 与失败分流

- 可先用 `git remote get-url --all <remote>` 和 `git remote get-url --push --all <remote>` 读取本地配置。若 endpoint host 是 `localhost`、`127.0.0.0/8` 或 `::1`，把它视为宿主机 loopback remote；不要修改 endpoint。
- 对 loopback remote，旧式 `default` 路径的独立 `git ls-remote`、`git fetch`、`git push` 等命令必须在第一次调用时使用单命令 `require_escalated`；current-user direct 路径不得用原始 Git 远端命令绕过 Finalizer，应由完整 Finalizer 调用执行获授权的远端检查、push 与 post-verify。
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

unborn 仓库仅获本地 commit 授权、remote target 尚未明确时，使用：

```bash
/home/hsd/bin/codex-git-finalize \
  --summary \
  --initial-commit-only \
  --repo <absolute-repo> \
  --message <commit-message> \
  -- <repo-relative-file>...
```

`initial-commit-only` 要求 attached unborn branch、空 index、非空显式范围，并在 commit 后验证
唯一 root commit、路径范围和 clean 状态。它不接受 remote、snapshot 或其他 publish mode，
不执行 fetch、`ls-remote`、push 或远端验证。

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

当前已经存在目标本地 commit、只需继续推送时，不得运行默认或 commit-only 模式重复制造 commit。普通 attached branch 使用下述 `--resume-publish`；initial root commit 使用 `--resume-initial-publish`。

若普通 attached feature branch 尚无 configured upstream，且显式同名 remote branch 不存在，
不得伪造 upstream 或创建空 commit；使用 `--publish-existing-branch`：

```bash
/home/hsd/bin/codex-git-finalize \
  --summary \
  --publish-existing-branch <full-head-oid> \
  --remote <remote-name> \
  --remote-branch <same-local-branch-name> \
  --repo <absolute-repo>
```

该接口要求 clean index/worktree、attached 非受保护 branch、完整 HEAD OID、无任何 configured
upstream，并要求显式 remote target 不存在。它不 add、不 commit、不改写 HEAD；fetch 后检查
captured remote heads 尚不可达的 commit objects，仅以 non-force、`--no-follow-tags`、
`--set-upstream` 的精确 branch refspec 发布。成功必须验证 remote OID、upstream、`0/0`、
clean 状态以及 HEAD/branch/history 不变。若远端目标已存在、push 结果或 post-verify 含糊，
fail closed，不自动重试、覆盖或切换到 force。

## Existing-history first publication to verified-empty Gitea

完成显式 `--repo-plan` → `--repo-ensure` 后，本地已有 history 且目标 repository 仍完全空时：

```bash
/home/hsd/bin/codex-git-finalize \
  --summary \
  --publish-existing-history <full-head-oid> \
  --remote <remote-name> \
  --remote-branch <same-local-branch-name> \
  --repo <absolute-repo>
```

该接口要求 attached、clean、至少一个 existing commit，完整 HEAD OID，显式 remote/branch 与
本地 branch 一致，且 upstream 缺失或精确未解析。它在 fetch 前后和最终 push gate 前读取整个
remote refs：任一 existing branch、tag 或其他用户 ref 都会阻断，并转回普通 existing-history
publication 规则；不得 force。它不 add、不 commit，只执行 non-force、`--no-follow-tags`、
`--set-upstream` 的精确 branch refspec。成功必须证明 local HEAD/history/index/worktree 不变，
remote 只有目标 branch，且 HEAD、upstream、remote OID 相同、ahead/behind 为 `0/0`。

若 push 中断或结果不确定，使用工具报告的同一完整 OID：

```bash
/home/hsd/bin/codex-git-finalize \
  --summary \
  --resume-existing-history-publish <full-head-oid> \
  --remote <remote-name> \
  --remote-branch <same-local-branch-name> \
  --repo <absolute-repo>
```

resume 只接受 remote 仍完全空，或只有目标 ref 且其 OID 精确等于 expected HEAD；任何额外 ref、
目标 OID 不同、upstream 冲突或本地漂移都 fail closed。恢复不创建 commit，也不改变 branch/tag。

## Resume publish existing commits

普通 attached branch 已有一个或多个连续 local ahead commits，且用户已明确授权继续 push 时使用：

```bash
/home/hsd/bin/codex-git-finalize \
  --summary \
  --resume-publish <full-head-oid> \
  --repo <absolute-repo>
```

`<full-head-oid>` 必须是当前 HEAD 的确切完整小写 OID。该接口要求非 unborn、非 root 的
attached branch，index 和包含 untracked 文件在内的 worktree 完全 clean，且不存在未完成的
Git operation；不接受 `--message`、文件列表、`--remote`、`--dry-run` 或其他 mode。

当前 branch 必须有唯一 configured upstream；push 目标严格由该 upstream 推导。Finalizer
fetch 后要求 `ahead >= 1`、`behind = 0`，以 `upstream..HEAD` 为发布范围，并对范围内每个
commit 和 changed object 执行适用的文件范围、敏感路径/内容、大文件、ignored/异常文件和
whitespace 检查。它不执行 add 或 commit，不修改 tag 或 Git 配置，只允许 non-force、
`--no-follow-tags` 的精确 branch refspec push。

成功后 remote target ref 必须等于 HEAD，ahead/behind 必须为 `0/0`，index/worktree 保持
clean，HEAD、local branch、tag 和 Git 配置保持不变。push 或 post-verify 失败时保留原 commits，
只按摘要给出的同一完整 OID 恢复入口重试；不得创建替代 commit 或改写历史。

## Controller-leased integration candidate publication

上层 Controller 已完成 frozen intent claim、exact candidate validation 和 lease-acquire 后使用：

```bash
/home/hsd/bin/codex-git-finalize \
  --summary \
  --publish-integration-candidate <full-candidate-oid> \
  --repo <absolute-candidate-worktree> \
  --lease-id <controller-lease-uuid> \
  --run-id <controller-run-id>
```

该接口要求 candidate 是 clean attached integration checkout，并由唯一 pre-0.6 `VALIDATED`
intent 或 Controller 0.6+ lease-bound `PUBLISHING` V2 intent、`INTEGRATING` allocation、exact
validation evidence 和 schema v2 lease 共同绑定。V2 还必须匹配 prepared base/commit/tree/scope、
tests/Snapshot evidence 与 immutable prepared-candidate receipt。repository ID、allocation、holder、
run、remote、target ref、expected main、candidate 和 expiry 任一不匹配都停止。

Finalizer 使用 Controller 现有 `repo.lock` 的 exclusive flock，先读取 live target；remote 已等于
candidate 时只返回 `already_published_recovered`，不再 push。否则 lease 必须仍未过期且 live
target 精确等于 expected main，随后只执行 non-force、`--no-follow-tags` 的
`candidate:refs/heads/<target>` push 并 live verify。它不 fetch、不切换分支、不 add/commit，不修改
canonical files/index/local branch，也不释放 lease 或 worktree。成功 receipt 的 `record_id` 必须
传给 Controller `lease-complete --run-id ...`；Controller 才拥有 receipt persistence、lease
release、registered disposable cleanup 和 guarded release。若进程中断，先以同一 lease/run/OID
重入本接口读取 remote fact；不得改走 normal mode、创建新 commit 或原始 `git push`。

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

`<full-root-oid>` 必须是该工具报告的确切完整小写 OID，并与当前 HEAD 完全一致。root resume 不接受 `--message`、文件列表、`--dry-run` 或新的 commit 内容。

resume 要求当前 HEAD 仍是唯一、非空的 root commit，symbolic branch 未改变，index 和包含 untracked 文件在内的 worktree 完全干净，且不存在未完成的 merge、rebase、cherry-pick、revert、bisect 或 sequencer 操作。所选 remote 必须已配置且 endpoint 唯一一致；upstream 只能缺失、精确未解析，或精确解析到该 OID。

远端只允许两种安全状态：完全为空，或仅有目标 branch 且它正好指向该 root OID。存在额外 ref、目标 branch 指向其他 OID 或结果含糊时停止。Finalizer 根据远端和 upstream 状态执行必要的 set-upstream push，不创建 commit，最后验证 HEAD、唯一 root commit、远端 branch、upstream、`0/0`、干净 index/worktree 全部一致。可重试提示只适用于工具明确标为可安全重试的同一 resume 命令。

## `--dry-run`

normal、commit-only 和 initial 模式可增加 `--dry-run`，用于报告显式范围和静态风险。它不执行 `git add`、commit、fetch 或 push；也不实时证明远端 branch、ahead/behind 或空 remote 状态。verify-only 本身是严格只读验证且不接受 `--dry-run`；resume 不支持 dry-run。

retirement 的 `--dry-run` 不同：它会执行 live `fetch`/`ls-remote` 并完成全部 preflight，但不
push delete；成功结果必须是 `RETIREMENT_PREFLIGHT_PASSED`。两类 dry-run 都不能替代真实操作
后的 post-verify。

## Remote feature branch retirement

```bash
/home/hsd/bin/codex-git-finalize \
  --summary \
  --retire-remote-branch <branch-or-refs/heads/branch> \
  --remote <remote-name> \
  --integrated-into <branch-or-refs/heads/branch> \
  --expected-remote-oid <full-feature-oid> \
  --repo <absolute-repo> \
  --dry-run
```

删除前必须去掉 `--dry-run` 并取得对确切 remote/ref 的明确授权。工具每次重新 fetch 和查询 live
refs，只接受 `refs/heads/*`；拒绝 symbolic ref、tag、remote default branch 以及复用既有
protected-branch authority 判定的 `main`、`master`、`trunk`、`production`。preflight 要求
attached 且完全 clean、feature live OID 等于 expected OID、integration target 存在、feature
commit 是其 ancestor，并且目标 branch 没有 attached worktree、local-only/remote-only 差异或
其他 local branch 的 required upstream 依赖。squash、tree/semantic equivalence 在 v1 一律
`RETIREMENT_BLOCKED`。

repository policy 要求 CI 时，必须同时传
`--ci-required --ci-status SUCCESS --ci-commit-oid <live-integration-oid>` 以及
`--ci-verification-source tool_authenticated|human_authenticated_ui`；可选
`--ci-verified-at <timestamp>` 只记录外部证据时间，不替代 OID/status gate。私有 CI 不可认证时
停止，不尝试绕过认证。

实际 delete 只允许 Git 的
`--force-with-lease=refs/heads/<branch>:<expected-oid> <remote> :refs/heads/<branch>`；这是绑定精确
OID 的 compare-and-delete，不是 unconditional force push。并发更新造成 lease mismatch 时
fail closed。随后必须 live 验证目标 ref 缺失、integration OID 未变，执行 `fetch --prune`，并
确认 remote-tracking ref、HEAD/index/worktree/tags 均未变化。摘要中的 `mode_result` 是确定性
receipt，result 只取 `REMOTE_BRANCH_RETIRED_VERIFIED`、`ALREADY_ABSENT_VERIFIED`、
`RETIREMENT_PREFLIGHT_PASSED`、`RETIREMENT_BLOCKED` 或 `REMOTE_DELETE_UNVERIFIED`；工具不另建
持久 audit store。

## Release 流程边界

tag、Release、Artifact 和 deployment 是独立 Release 流程，必须按仓库既有文档、recipe 或自动化另行授权和执行；它们不属于 verify-only、commit-only 或默认模式，也不得从任何 Finalizer 成功结果推导其授权或完成状态。

## 必须停止的情况

- 所选模式超出用户明确授权的 commit/push 边界，或操作属于尚未确认的高风险范围。
- 相关测试失败、必要检查未运行、审查未通过或 Runner 有未解决阻断。
- 文件范围或所有权不清、当前轮改动无法隔离、已有 staged 文件超出显式范围。
- normal 模式为 detached/unborn HEAD、upstream 缺失或不可解析、remote 配置含糊、本地落后或分叉。
- initial 模式发现本地已有 commit、预存 index、范围外 worktree 变更、可解析或冲突 upstream、remote 非空。
- 普通 resume 的 OID、HEAD、branch、cleanliness、configured upstream、ahead/behind 或 remote
  精确状态不满足要求；root resume 的 OID、唯一 root commit 或远端形状不满足要求。
- index 有冲突、范围检查或安全检查失败、工具失败，verify-only 发生任何状态变化，或需要远端的模式出现 push 结果不确定或 post-verify 不一致。
- 继续需要 amend、force push、重复创建 commit、自动冲突处理，或任何原始 Git 写命令绕过 Finalizer。

停止时报告工具是否已经创建 commit、确切 commit OID、已验证与未验证状态，以及工具给出的安全恢复入口。不得把失败或不确定结果描述为发布成功。
