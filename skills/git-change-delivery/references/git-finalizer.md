# Git Finalizer

## 同步已发布分支

双远端同步单独使用 `--sync-published-branch <full-published-oid>`，并提供
`--source-remote <name> --remote <name> --remote-branch <existing-branch>
--expected-target-oid <full-oid> --repo <absolute-repo> --summary`。
两个 remote 必须既有、不同且各自只有相同 fetch/push endpoint；source 和 target 使用同名既有
branch。live source 必须等于 exact OID，target 必须等于 expected OID 且是 source 的祖先。
保留现有 commit-range 敏感内容与路径扫描，只做 non-force、`--no-follow-tags` 精确 branch push。

`--dry-run` 展示并验证交易但不 push；可 fetch objects，保持 branch refs、FETCH_HEAD、HEAD、index
和 upstream。目标已等于 source 的恢复只核验两端 OID，不重复写入。未知 push 结果先读取事实，
两端精确一致才允许成功；分叉、源变化、目标漂移或本地变化都阻断。pre-push expected-OID
检查不是 server-side CAS，远端普通 fast-forward gate 和 post-verify 继续生效。不修改 remote/
upstream，不创建新分支、不 merge/force、不复制 tags 或个人备份，也不替代 Controller 的新代码
main integration。这些检查不能授予业务授权；遵守用户指定的 remote 可见性和动作范围。

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

feature branch retirement 是独立操作，不属于 commit/publish mode。Controller 先证明 release、
占用清空和 direct ancestry；Finalizer 在明确授权下分别用 expected-OID CAS/lease 退役 local 或
remote ref，且永不删除 worktree。

Controller schema v2 integration candidate publication 也是独立接口。它不创建 commit，不从
canonical checkout 发布，也不由 caller 另行选择 remote/main；只消费 Controller 已签发的同一
publication lease，并把 verified receipt 交回 Controller 完成 lifecycle。

先应用当前用户的持续自动交付授权。其覆盖普通开发任务时，验收后直接 commit/push，不要求
逐次人工确认；缺少远端时先安全地本地 commit。只读或更窄的 no-commit/no-push 指令优先：
只验证用 `--mode verify-only`，仅本地提交用 `--mode commit-only`，已覆盖 commit/push 且达到
交付状态用默认模式或正式首次发布/恢复接口。先以 `command -v git-finalize` 与
`realpath -e` 解析实际安装路径，完整调用命令使用该绝对入口；以下
`/absolute/path/to/git-finalize` 为占位符，安装目录无需固定。调用前还必须满足：

- 实现已完成，相关测试和检查实际通过；
- Snapshot Runner 的相关审查已通过，阻断项已经解决；
- 当前授权范围、文件所有权和已有改动来源清楚，授权改动可独立隔离；
- status 和 diff 已核对，候选范围保持聚焦；
- `--` 后逐一列出每个允许验证或暂存的显式 repository-relative 文件路径。

### 统一原生调用合同

终端和所有执行器使用同一个绝对 CLI 入口、相同参数及当前 Linux 用户身份。将完整命令与
精确仓库工作目录提交给执行器，由其当前运行策略和 OS 权限机制控制执行。当前策略已允许
且任务授权覆盖时（如 Full Access + `approval_policy = "never"`），直接调用，不新增人工确认；
仅在当前运行策略要求时使用执行器支持的原生审批通道。不得伪造审批、改变
权限配置、使用 root，或将生命周期拆成原始 Git 写命令。

```bash
/absolute/path/to/git-finalize --summary --mode verify-only --repo /absolute/repo -- path/to/file
```

只有按 [明确发布授权](../SKILL.md#明确发布授权) 判定为当前有效且覆盖对应操作的授权，才可选择
commit/publication 模式。执行器若因沙箱拒绝操作，应通过它支持的原生权限申请流程处理同一
完整命令；没有合法执行能力时停止。
CLI 不读取任何执行器的私有 transcript、session、规则文件或内部权限枚举，也不代理宿主
提权。原生审批不能替代文件范围、敏感内容、Controller authority、远端和回滚检查。

动作已获授权时，Finalizer 可使用现有 Git credential authority，无需仅因凭据已配置而再次请求
业务授权；不得读取、输出、复制或修改凭据，交互认证仍由用户本人完成。credential authority
只提供执行能力，不授予或扩大 commit/publication 权限。

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

### 本机 Git remote 与失败分流

- 可先用 `git remote get-url --all <remote>` 和 `git remote get-url --push --all <remote>` 读取本地配置。若 endpoint host 是 `localhost`、`127.0.0.0/8` 或 `::1`，把它视为宿主机 loopback remote；不要修改 endpoint。
- 对 loopback remote，远端访问仍须遵守执行器原生权限边界。应由完整 Finalizer 调用执行获授权的远端检查、push 与 post-verify，不得以原始 Git 命令绕过门槛。
- 沙箱内出现 `connection refused`、systemd bus 不可见、`ss` 看不到宿主机监听端口或类似结果时，只能判定为沙箱证据，不得判定 SSH/Gitea 已停止。不要启动、停止或修改 `ssh.service`、`gitea.service`，也不要改 remote、SSH key、防火墙或监听地址。
- 只有同一条 Git 远端命令在沙箱外仍失败，才进入真实服务诊断；先进行只读的 endpoint、进程和日志核对，任何服务或系统变更仍需另行明确授权。

不得用 `git add .`、`git add -A`、原始 `git commit` 或原始 `git push` 绕过 Finalizer。不得自动 amend、force push、切换或创建分支、pull、merge、rebase、stash、解决冲突或重写历史；不得使用 `git reset --hard`、`git clean`、破坏性 checkout 或强制删除分支来丢弃改动。

## 已有历史分支的三种模式

三种模式共用适用于各自执行边界的路径规范化、显式范围、已有 staged scope、冲突、敏感内容、大文件和 whitespace 等本地提交前检查。verify-only 不执行会证明写入或 commit 能力的步骤；commit-only 与默认模式才执行实际暂存和 commit。

### Verify only

```bash
/absolute/path/to/git-finalize \
  --summary \
  --mode verify-only \
  --repo <absolute-repo> \
  -- <repo-relative-file>...
```

verify-only 要求已有 commit 的 attached local branch，但不要求 upstream。它不接受 `--message`，不执行 `git add`、commit、fetch、`ls-remote`、push 或任何其他远端命令，也不修改 HEAD、index、worktree、refs、tag 或 Git 配置。成功只证明适用的只读本地提交前验证通过且所报告状态前后不变；不证明 commit hooks、author/签名、index 实际写入能力、远端状态或 fast-forward 条件。

### Commit only

冲突由调用方在获授权的 source checkout 中处理。非空、已解决的双亲 merge 可显式使用
`--merge-parent <full-oid>`，且只接受 `--mode verify-only` 或 `--mode commit-only`；默认继续拒绝
未完成的 Git 操作。此参数要求普通文件 `MERGE_HEAD` 中只有一个精确 OID、index 无未解决项，
没有其它进行中的 Git 操作，并保留显式文件范围、敏感内容、原 HEAD、双亲与 index tree 核验。
verify-only 保持 HEAD/index/worktree/merge identity；commit-only 只产生本地 merge commit。
它不解决冲突，也不允许同时发布；之后使用原有首次发布或 resume 入口与 Controller 集成。

```bash
/absolute/path/to/git-finalize \
  --summary \
  --mode commit-only \
  --repo <absolute-repo> \
  --message <commit-message> \
  -- <repo-relative-file>...
```

commit-only 用于已授权创建本地 commit、但未授权或暂不适合 push 的任务。它要求已有 commit 的 attached local branch，不要求 upstream，不执行 fetch、`ls-remote`、push、远端验证或其他远端操作，也不修改 remote、tag 或 Git 配置。成功必须报告新 commit OID、因该模式跳过的 push/远端验证及最终工作区状态。

unborn 仓库仅获本地 commit 授权、remote target 尚未明确时，使用：

```bash
/absolute/path/to/git-finalize \
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
/absolute/path/to/git-finalize \
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
/absolute/path/to/git-finalize \
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
/absolute/path/to/git-finalize \
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
/absolute/path/to/git-finalize \
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
/absolute/path/to/git-finalize \
  --summary \
  --resume-publish <full-head-oid> \
  --repo <absolute-repo>
```

`<full-head-oid>` 必须是当前 HEAD 的确切完整小写 OID。该接口要求非 unborn、非 root 的
attached branch，index 和包含 untracked 文件在内的 worktree 完全 clean，且不存在未完成的
Git operation；不接受 `--message`、`--remote`、`--dry-run` 或其他 mode。只有携带
`--reviewed-sensitive-source` 时才接受 `--` 后的精确复核 scope，并要求完整 Controller linkage。

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
/absolute/path/to/git-finalize \
  --summary \
  --publish-integration-candidate <full-candidate-oid> \
  --repo <absolute-candidate-worktree> \
  --lease-id <controller-lease-uuid> \
  --run-id <controller-run-id>
```

该接口要求 Controller 的公开 capability `integration_publication_execution_version` 为 1。
`integration-publication-session/v1` 由 Controller 核验 clean attached candidate、allocation、
lease、intent、prepared receipt 与 validation evidence；这些私有字段和存储路径不由 Finalizer
读取或重建。公开 identity 绑定 repository/allocation、lease/run、remote/target、expected main、
candidate、validation evidence、record version 和 expiry，任一响应漂移都停止。

Controller 在同一公开 stdio session 中持有现有 exclusive repository lock，先 verify，收到
请求后重新 authorize，并在 Finalizer 推送后独立核验 completion。remote 已等于 candidate 时
只返回 `already_published_recovered`，不再 push；过期 lease 也可恢复已发生的精确结果。否则
生产方必须确认 lease 未过期且 live target 精确等于 expected main，才允许 non-force、`--no-follow-tags` 的
`candidate:refs/heads/<target>` push 并 live verify。它不 fetch、不切换分支、不 add/commit，不修改
canonical files/index/local branch，也不释放 lease 或 worktree。成功 receipt 的 `record_id` 必须
传给 Controller `lease-complete --run-id ...`；Controller 才拥有 receipt persistence、lease
release、registered disposable cleanup 和 guarded release。缺能力、断连、超时或 completion
拒绝都保持 blocked，不回退到私有文件读取。若进程中断，先以同一 lease/run/OID
重入本接口读取 remote fact；不得改走 normal mode、创建新 commit 或原始 `git push`。

## Initial publish

新项目的完整顺序是：创建空 remote → `git clone`（或 `git init` 加 `git remote add`）→ Context Loader → 实现与测试 → Snapshot Runner → Git Finalizer `--initial-publish` → 后续使用 normal 模式。

调用格式：

```bash
/absolute/path/to/git-finalize \
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
/absolute/path/to/git-finalize \
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

## Local and remote feature branch retirement

正常 lifecycle 先由 Controller 在 release 后生成计划。`CHERRY_EQUIVALENT`、`MERGE_ONLY`、
`UNIQUE`、`MIXED` 和历史未登记 refs 只报告，不生成可执行计划。local 与 remote 是两个独立、
可重试的 operation；Finalizer 使用公开协议分别完成 Controller 登记，不能假设原子完成。

```bash
/absolute/path/to/git-finalize \
  --summary \
  --retire-local-branch <branch-or-refs/heads/branch> \
  --remote <remote-name> --integrated-into <branch> \
  --expected-local-oid <full-feature-oid> \
  --expected-integrated-oid <full-integration-oid> \
  --retirement-plan-id <controller-plan-sha256> \
  --repo <absolute-repo> --dry-run
```

local retirement 必须绑定 eligible Controller plan。Controller 的公开 verify/authorize 接口在
repository lock 下核验 authority 并签发一次性 CAS 授权；Finalizer 复核 live integration OID、
ancestry、checkout、upstream 和 exact local OID，删除只用
`git update-ref -d <ref> <expected-oid>`，然后通过 complete 交回 Controller 核验、保存凭据。
Finalizer 独立确认目标缺失、其他 local refs、remote refs、HEAD/index/worktree/tags 均未漂移。

正式 handoff 使用 Worktree Controller 的公开 retirement 动词：`worktree-controller
branch-retirement-plan --repo <path> (--branch <b>|--allocation-id <id>) --remote <r>
--integrated-into <t> --json` 产出持久化 plan 与 `plan_id`（`--dry-run` 严格只读，仅用于
资格预检）。原有 plan-only CLI 输入保持不变，Finalizer 内部调用
`branch-retirement-verify`、`branch-retirement-authorize` 与 `branch-retirement-complete`，完成后
不需要调用方再登记。`branch-retirement-status` 与 `branch-retirement-executions --action list`
提供公开核对入口；能力版本以 installed `capabilities --json` 为准。
完成后目标仍不存在时，重复调用返回 `ALREADY_ABSENT_VERIFIED`，不签发新 CAS；已有 receipt
但 ref 再现时拒绝。中断恢复绑定 plan/operation 的同一 request：旧 executor 仍存活时阻断，
已结束且 ref 仍为 expected OID 时通过公开 abandon 重新授权；ref 已缺失时只完成原授权。
任何 complete 拒绝都报告失败，不因 Git ref 已删除而改报成功。未知执行结果先查询 live ref，
不盲目重放。旧消费者已删除且没有未完成授权的 ref，通过公开 `branch-retirement-record`
登记 `ATTEST_ABSENT` 观察，凭据保持 `CONTROLLER_OBSERVED` 来源。

`--integrated-into` 接受与 plan 目标同一身份的任一跳法（裸分支名、`<remote>/<branch>` 或
`refs/heads/...`）；Controller plan 原样保存操作者输入的字面值，登记与核对以解析后的 ref
为准。plan 的 authority digest 只覆盖 Controller authority 文件而不含 refs：plan 创建后任何
相关的 authority 写入（新 intent、lease、receipt 等）都会使 Controller 以 state-drift fail
closed，而 branch 自身移动则由 expected-OID CAS 与最后时刻 live 复核拦截；remote 上 integration
分支的新 push 同样导致拒绝。因此一次 plan 必须在安静的短窗口内创建并消费，过期就重新规划。
Finalizer 在 validation 前还会通过公开的 `capabilities --json` 证明 Controller 的
`branch_retirement_version`、`branch_retirement_verify_version`、`branch_retirement_execution_version`
均为 1；缺失、不可读、不匹配或 Controller 未安装时 fail closed，不回退到私有文件核验。
仓内仅有 legacy `codex-worktree` namespace 时保留精确迁移 blocker；namespace 激活属
Controller 治理职责，不由 Finalizer 代办。authority digest 始终作为 producer 的不透明结果，
Finalizer 不读取 authority JSON、不重算 recipe，也不在外部持锁后嵌套调用 Controller。

```bash
/absolute/path/to/git-finalize \
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

## Reviewed sensitive source（Python、CSS、JSON；1.8.1）

当任务明确授权审查某个敏感命名的源码文件时，normal、commit-only、verify-only、
publish-existing-branch 和 configured-upstream resume-publish 可显式使用
`--reviewed-sensitive-source <absolute-json-file>`。它只豁免 exact `.py`、`.css` 或 `.json` 文件的
credential/secret/token **路径标记**，包括设计系统中的 `tokens/tokens.css`、`tokens/registry.json`
或 `dist/tokens.json`；`.env` 和 private/SSH key 路径始终不接受，即使同一路径也包含 token 标记。
既有 secret 内容扫描仍在工作区、index 和提交后运行，不能组合任何 fixture 内容例外。
JSON 内容扫描也检查解码后的字符串与键，不能通过 JSON 转义隐藏凭据签名。
resume-publish 重新核对 HEAD 复核身份，且待发布历史中该路径的每个 blob 必须精确匹配
复核的 HEAD blob；旧内容不能因当前文件通过复核而获准。恢复时重传复核文件、完整 linkage
与 `--` 后的原始精确 scope，不重复创建 commit。其他 initial/history/integration 接口不消费该 review。

existing branch/history publication 及各 resume 接口接受 `--allow-large-binary <path>`。
例外只绑定该规范路径的普通 HEAD blob，要求大于 5 MiB、至多 25 MiB；历史中其它 blob
和未实际匹配发布范围的例外仍拒绝，内容扫描照常执行。删除对象不按当前 ignore 规则误判为
新文件，但更早的待发布新增对象仍逐个扫描。

先使用 Controller 取得合法 task/writer 和显式 source scope，完成定向业务与 secret 审查；
再由受信任调用者生成以下外部 review JSON。它不授予 writer、不修改 Controller，也不改变
用户的 commit/push 授权。此机制信任当前用户与 OS，不声称防御恶意同 UID 进程。

```json
{
  "schema_version": 1,
  "kind": "reviewed-sensitive-source",
  "repository": {"common_dir": "/absolute/canonical/.git", "root_commit": "<root OID>"},
  "allocation": {
    "repository_id": "<Controller repository ID>",
    "allocation_id": "<allocation ID>",
    "task_key": "<task key>",
    "authority_key": "<authority key>",
    "worktree_path": "/absolute/selected-worktree"
  },
  "scope_sha256": "<SHA256 of sorted explicit relative paths, joined by LF, ending in LF>",
  "reviewed_at": "<UTC ISO timestamp>",
  "expires_at": "<UTC ISO timestamp, at most seven days after review>",
  "reviews": [{
    "path": "scripts/credential_transfer.py",
    "sha256": "<exact content SHA256>",
    "purpose": "<specific authorized review purpose>",
    "evidence": {"path": "/absolute/external/review.md", "sha256": "<evidence SHA256>"}
  }]
}
```

Review 与 evidence 必须是 worktree/Git common directory 外部的 canonical 普通文件、当前 UID
所有、group/other 不可写、各不超过 64 KiB；不能引用 symlink。Review 不接受未知字段，最多
16 个 exact source 条目，每个不超过 1 MiB，必须为不含 NUL 的 UTF-8 文本。Python 必须通过 AST
解析；JSON 必须合法，且不能包含重复键或 NaN/Infinity；CSS 的语法与设计语义由项目检查验证。
Review JSON 与 evidence 只含
审查结论和非秘密 identity，不记录 secret 值。调用时必须同时传入完全匹配的
`--repository-id --allocation-id --task-key --authority-key` 和完整显式文件列表。

任一仓库、路径、content hash、allocation、scope、evidence hash 或有效期变化都会阻断，必须
重新审查；不存储永久 allowlist。`reviewed_sensitive_sources` 摘要 receipt 绑定 review 文件
SHA256、源码/evidence/purpose 摘要和 identity；只有完整验证成功才报告 `content_scan=passed`。
verify-only 仍不写 index/HEAD 或访问远端。Git filter/hook 改变已审查源码时停止；若 hook 已创建
commit，receipt 保留 exact OID，禁止误报成功或自动重提。

commit-only 之后首次发布同一 clean feature branch 时，向 `--publish-existing-branch`
传入同一 review 文件、四项 Controller linkage，以及 `--` 后的原始完整 review scope。
这份 scope 只绑定 review；Finalizer 仍扫描所有待发布 commit 和路径。HEAD 内容和每个待发布
历史 blob 都必须匹配 review，旧版本不能借当前 review 放行；push 前再次验证 evidence 与
有效期。此入口不创建新 commit，不接受 `--mode`、`--message` 或 `--dry-run`，不得组合
fixture 内容例外。其他 existing/history/resume/integration 模式仍不消费此 review。

多文件提交时，`scope_sha256` 和 CLI 文件列表绑定完整提交范围；`reviews` 只列其中被路径规则
拦截的文件。例如 `tokens/tokens.css`、`tokens/tokens.json`、`tokens/registry.json`、
`dist/tokens.css` 和 `dist/tokens.json` 需要精确 review，`dist/registry.json` 无此路径标记，
可以在完整 scope 中但不列入 `reviews`。不要把所有 CSS/JSON 或某个目录加入永久 allowlist。

## Release 流程边界

tag、Release、Artifact 和 deployment 是独立 Release 流程，必须按仓库既有文档、recipe 或自动化另行授权和执行；它们不属于 verify-only、commit-only 或默认模式，也不得从任何 Finalizer 成功结果推导其授权或完成状态。

## 必须停止的情况

- 所选模式超出当前有效的明确 commit/push 授权边界，或操作属于授权外的高风险范围。
- 相关测试失败、必要检查未运行、审查未通过或 Runner 有未解决阻断。
- 文件范围或所有权不清、已授权改动无法隔离、已有 staged 文件超出显式范围。
- normal 模式为 detached/unborn HEAD、upstream 缺失或不可解析、remote 配置含糊、本地落后或分叉。
- initial 模式发现本地已有 commit、预存 index、范围外 worktree 变更、可解析或冲突 upstream、remote 非空。
- 普通 resume 的 OID、HEAD、branch、cleanliness、configured upstream、ahead/behind 或 remote
  精确状态不满足要求；root resume 的 OID、唯一 root commit 或远端形状不满足要求。
- index 有冲突、范围检查或安全检查失败、工具失败，verify-only 发生任何状态变化，或需要远端的模式出现 push 结果不确定或 post-verify 不一致。
- 继续需要 amend、force push、重复创建 commit、自动冲突处理，或任何原始 Git 写命令绕过 Finalizer。

停止时报告工具是否已经创建 commit、确切 commit OID、已验证与未验证状态，以及工具给出的安全恢复入口。不得把失败或不确定结果描述为发布成功。

## Exact content review

固定 Controller retirement capability 的公开说明只在匹配 JSON schema 且该 literal 唯一时
作为非凭据处理，不按文件名、空格、JSON 后缀或整文件白名单放行。其它 raw/decoded 敏感赋值
继续检查；全转义敏感键仍检查。decoded known-token、private-key、SSH-key 签名在 working/blob
完整扫描中独立检查，不能由 credential-only fixture review 一并跳过。分类进程失败、空或未知
结果拒绝候选；普通 non-schema 内容继续 raw 检查。重复键、非有限常量或损坏的 JSON
容器仍逐个解码有效字符串 literal，不能通过容器错误关闭转义内容检查。

`--fixture-exceptions <absolute-json-file>` 是显式精确复核，原有签名匹配规则继续执行。支持 normal、
configured-upstream `--resume-publish` 以及 existing branch/history publication/resume；
verify-only、commit-only 只接受下述 backend assignment fixture，不支持 root publication，
也不能与 `--allow-test-fixture` 混用。
复核文件 schema 为 1，顶层仅含 `schema_version`、`repository`、`exceptions`。
`repository` 仅含 Git 唯一 `root_commit` 和唯一 push URL 的 `remote_url_sha256`。
每个 exception 仅含 `path`、`blob_oid`、原始字节 `sha256`、`detector`、非空 `reason`，最多 16 条；
receipt 返回绑定全部字段的 exception ID。例外只排除指定 detector，其他内容与路径守卫照常执行。
测试路径必须精确位于 `tests/`，或为单层 `backend/tests/test_[A-Za-z0-9_]+.py`。
后者只允许 `credential-assignment-v1`；known token、private/SSH key 签名继续拒绝。
local 模式仅接受后者，从现有本地 `origin` 唯一 push URL 绑定 repository identity，
不要求 upstream，不执行 fetch、ls-remote 或 push，也不配置 remote。
local review 目标必须为真实候选，raw blob 和只读 clean-filter blob 必须一致，已有 staged
变更的 index blob 也必须一致；verify-only 不写 index，receipt 不证明实际暂存或 hooks。
commit-only 在实际暂存后再次绑定 exact index blob 与 commit tree。legacy `--allow-test-fixture`
仍只接受原来的 `tests/`，范围不扩展。
唯一文档路径为 `CHANGELOG.md`，且只接受 `private-key-header-v1`：
marker 必须是 upstream 已有的行内反引号示例，从最早示例起的历史后缀必须逐字节保留；
新增 marker、闭合 key marker、其他文件或规则均拒绝。normal 必须与 upstream 对齐，先校验 working
raw blob，再复核 index 的相同 OID；filter 或并发变更导致 staged blob 漂移时停止 commit/push。
resume 则将每条复核绑定待发布历史中的 exact path/blob/detector，未使用或过期复核也会被拒绝。

Python prose 复核仅接受 `.py` 和 `credential-assignment-v1`，且必须存在发布过的 upstream
文件。源码不超过 1 MiB，必须能通过 UTF-8 AST 解析；所有 assignment 命中必须完整位于
模块级大写 `*_REASONS` 字典的普通多词字符串 key/value 对内，两者间仅允许空白和冒号。
完整原始 key/value 对只能来自 upstream 的同类声明，新增/改变字面量、其他位置的赋值、配置字段、解析失败
均拒绝。私钥、known-token、SSH 和路径守卫照常执行；首次发布没有可证明的 baseline 时拒绝。
这仍是逐 blob 的显式人工复核，不改变默认扫描，也不是任意 Python 源码豁免。
Python prose 复核仍不支持 verify-only、commit-only；local 只接受上述 backend assignment fixture。
