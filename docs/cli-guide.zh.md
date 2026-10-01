# Git Finalizer 命令行完整说明（中文）

本文件是 `git-finalize` 各模式的完整操作说明，从 README 拆出以便 README 保持面向新读者的快速上手结构。合同语义与 README、`docs/agent-contract.md` 和 `tool_cli_contract.json` 一致；如三者冲突，以已发布版本的 `tool_cli_contract.json` 与本仓库测试为准。

## Gitea repository bootstrap

Repository bootstrap 是独立于 commit/publication 的显式操作。`--repo-plan` 只读检查 target、
owner capability、repository existence、visibility 和本地 remote；`--repo-ensure` 只创建或验证
一个空 repository，并可在指定 remote 完全缺失时通过 `--add-origin` 添加精确 URL：

```bash
git-finalize \
  --repo-plan \
  --repo /home/user/projects/example \
  --gitea-url http://gitea.example.test \
  --gitea-profile local \
  --owner example-owner \
  --repo-name example \
  --visibility private \
  --summary
```

`--repo-ensure` 不 commit、不 push，也不会在 publication 404/失败后被隐式调用。它不覆盖、
rename、transfer 或 delete 既有 repository，不配置 collaborator/team、webhook、deploy key、
branch protection 或 mirror，也不初始化 README、LICENSE 或 `.gitignore`。API credential 只从
Git credential authority 解析；token/password 不能通过普通 CLI 参数传入，也不会进入 remote
URL、日志或 receipt。bootstrap outcome 与 `publication.executed=false` 分开记录。

## 使用方式

正常模式用于已有 upstream 的成熟仓库：

```bash
git-finalize \
  --repo /home/user/projects/example \
  --message "fix: describe the change" \
  -- path/to/file
```

只创建本地 commit、明确跳过 push 和全部远端验证时，使用 `commit-only`：

```bash
git-finalize \
  --mode commit-only \
  --repo /home/user/projects/example \
  --message "fix: describe the change" \
  -- path/to/file
```

该模式保留 normal 模式的显式路径、staged scope、敏感内容、大文件、冲突、attached branch
和非-unborn HEAD 检查，但不要求 upstream，不执行 fetch、ls-remote、push 或 post-push
verification，也不修改 remote、tag 或 Git 配置。成功输出会报告 mode、新 commit hash、因模式
跳过的 push/远端验证以及最终工作区状态。

尚无任何 commit 且远端目标未获明确授权时，使用显式的本地 root commit 模式：

```bash
git-finalize \
  --initial-commit-only \
  --repo /home/user/projects/example \
  --message "feat: initialize project locally" \
  -- README.md
```

该模式只接受 attached unborn branch、空 index 和完整显式文件范围；它创建并核验一个非空
root commit，但不要求 remote，也不执行 fetch、`ls-remote`、push 或远端验证。它不能与
`--initial-publish`、`--snapshot` 或任何 remote 参数组合。

只读验证当前显式文件范围时，使用 `verify-only`；该模式不接受或要求 `--message`：

```bash
git-finalize \
  --mode verify-only \
  --repo /home/user/projects/example \
  -- path/to/file
```

verify-only 要求已有 commit 的 attached branch，但不要求 upstream。它复用路径规范化、已有
staged scope、冲突、敏感路径与内容、大文件及例外范围检查，并以只读方式确认 HEAD 到当前
worktree 的候选 diff 非空、ignored 文件不会进入候选范围且 whitespace 检查通过。
提交前还在独立临时对象库中核对 Git clean-filter/encoding 转换后的内容与二进制大小，
退出即清理临时对象，不写仓库对象库或 index。它不运行
`git add`、commit hooks、`git commit` 或任何远端命令，也不执行 config、tag 或 ref 写操作；
因此不验证 commit message、author/签名、hook 结果、index 实际写入能力、远端状态或
fast-forward 条件。
`--dry-run` 与 verify-only 不兼容：verify-only 本身即为严格只读验证，失败使用非零退出码。

首次发布模式只接受 unborn 分支和完全空的远端，并创建首个非空 root commit：

```bash
git-finalize \
  --initial-publish \
  --remote origin \
  --repo /home/user/projects/example \
  --message "feat: initialize project" \
  --snapshot <snapshot-runner-id> \
  -- README.md
```

已有正常历史的本地功能分支首次发布到尚不存在的同名远端分支时，使用独立模式：

```bash
git-finalize \
  --initial-branch-publish \
  --remote origin \
  --remote-branch feat/example \
  --repo /home/user/projects/example \
  --message "feat: publish example branch" \
  -- path/to/file
```

该模式要求当前 symbolic local branch 与 `--remote-branch` 完全一致，拒绝
`main`、`master`、`trunk` 和 `production`，并在暂存前确认显式远端目标不存在。当前 upstream
可以缺失或错误指向其他分支；它不参与 push 目标选择，成功的显式
`HEAD:refs/heads/<remote-branch>` push 会通过 `--set-upstream` 将其替换为正确目标。commit 前后
均复查目标仍不存在；post-verify 要求远端目标与 HEAD 一致、upstream 精确对齐、ahead/behind
为 `0/0`、index 为空、显式路径干净，并确认所有远端非目标 refs（包括 main 与 tags）和本地
tags 均未变化。

`--snapshot` 是 initial publish 的可选严格证据绑定。提供后，Finalizer 要求 Snapshot Runner
schema 2 / security epoch 4 的 `diff-audit` artifact 为完整 unborn initial 证据：
`truncated=false`、`evidence_gaps=[]`、`complete=true`、敏感扫描完整，并且 `--` 后每个显式
路径恰好由正文或 `generated_manifest` 证据覆盖。Finalizer 在 `git add` 前核对 worktree，
暂存后核对 index，创建 root commit 后再核对 HEAD tree；任一 size、SHA-256、executable、
路径集合或 snapshot artifact 漂移都会停止。未提供 `--snapshot` 时，既有 normal、initial 和
resume 工作流语义保持不变。

`git-finalize-snapshot-verify.py` 是主入口同目录下的标准库-only companion，由主入口
固定定位和调用，不是独立发布命令；安装 Finalizer 时必须与主脚本一起部署。

若首次发布已经创建 root commit、但 push 未确认完成，可用完整 HEAD OID 恢复同一次发布：

```bash
git-finalize \
  --resume-initial-publish <full-head-oid> \
  --remote origin \
  --repo /home/user/projects/example
```

若普通 attached branch 已有 configured upstream，工作区与 index 均 clean，且一个或多个本地
commit 尚未发布，可复用完整 HEAD OID 发布 `upstream..HEAD`，而不会再创建 commit：

```bash
git-finalize \
  --resume-publish <full-head-oid> \
  --repo /home/user/projects/example
```

`--resume-publish` 严格从当前 branch 的唯一 configured upstream 推导 push 目标，不接受
`--remote`、message 或其他 mode。只有使用 `--reviewed-sensitive-source` 时，才接受 `--` 后
的精确复核 scope，并要求完整 Controller linkage；HEAD 复核身份和每个历史 blob 都会
重新核对。它要求 ahead 至少为 1、behind 为 0，并对待发布
range 中每一个 commit（包括最终 tree 已删除的中间内容）执行 whitespace、敏感路径与内容、
ignored 文件、大二进制及异常对象检查；随后仅执行显式 branch refspec 的 non-force、
`--no-follow-tags` push。post-verify 要求远端目标等于原 HEAD、ahead/behind 为 `0/0`、工作区与
index 仍 clean，并确认 HEAD、本地 branch、tags 和 Git 配置均未改变。旧的
`--resume-initial-publish` 继续只处理单 root commit 和 initial-publish 的空远端恢复状态。

Worktree Controller schema v2 已为 validated integration candidate 签发 publication lease 时，
使用独立的精确发布入口；remote、target ref、expected main OID、candidate OID、holder/run 与
validation evidence 全部从同一 lease 读取，不由 caller 重复声明：

```bash
git-finalize \
  --publish-integration-candidate <full-candidate-oid> \
  --repo /absolute/integration-candidate \
  --lease-id <controller-lease-uuid> \
  --run-id <controller-run-id> \
  --summary
```

该入口要求 candidate clean、attached，并绑定 pre-0.6 `VALIDATED` intent 或 Controller 0.6+
`PUBLISHING` V2 intent；V2 还必须精确匹配 publication identity 与完整 prepared receipt。
Controller allocation 必须处于 `INTEGRATING`。它在 Controller 现有 repository lock 下串行执行
live expected-main 检查、
non-force `candidate:refs/heads/<target>` push 和 remote verify；不修改 canonical files、index 或
local checked-out branch。若中断后 remote 已精确等于 candidate，则返回
`already_published_recovered` 而不重复 push；过期、被替换或 identity 漂移的 lease fail closed。
Finalizer receipt 交回 Controller 后，由 Controller 持久化 publication receipt、释放 lease 并
执行 guarded cleanup/release。

若已有 clean attached feature branch 和既有 committed HEAD，但尚未配置 upstream，且同名远端
branch 不存在，使用显式首次发布入口；该入口不创建 commit：

```bash
git-finalize \
  --publish-existing-branch <full-head-oid> \
  --remote origin \
  --remote-branch feat/example \
  --repo /home/user/projects/example
```

该模式要求本地 branch 与 `--remote-branch` 完全一致，拒绝受保护分支、dirty/staged 状态、
detached HEAD、任何既有 upstream 及已存在的远端目标。Finalizer fetch 后对 captured remote
heads 尚不可达的 commits 执行既有 commit-range 安全检查，再以 non-force、`--no-follow-tags`、
`--set-upstream` 的精确 branch refspec 首次发布。post-verify 要求 HEAD 和 commit history 不变、
远端 OID 与 HEAD 相同、upstream 精确、ahead/behind 为 `0/0` 且 index/worktree clean。

本地 repository 已有一个或多个 commit、而目标是经 `--repo-plan` / `--repo-ensure` 建立并
验证的空 Gitea repository 时，使用独立的 existing-history first-publication 入口：

```bash
git-finalize \
  --publish-existing-history <full-head-oid> \
  --remote origin \
  --remote-branch main \
  --repo /home/user/projects/example
```

该入口不创建 commit，允许受保护的初始 branch，但要求 attached、clean、本地 branch 与显式
target 同名，并在 fetch 前后及 push gate 前验证整个 remote 没有任何用户 Git refs；已有 main、
其他 branch、tag 或其他 ref 都会 fail closed，并应转回普通 existing-history publication 规则。
push 仅使用 non-force、`--no-follow-tags`、`--set-upstream` 的精确 branch refspec，成功后验证
HEAD、history、index、worktree 不变，remote 只有目标 branch，且 local/upstream/remote 为 `0/0`。

若 push 中断或结果不确定，保留工具报告的完整 OID，且只在 remote 仍为空或仅有完全相同的
target OID 时使用显式恢复入口；不要重跑 first-publication 或创建替代 commit：

```bash
git-finalize \
  --resume-existing-history-publish <full-head-oid> \
  --remote origin \
  --remote-branch main \
  --repo /home/user/projects/example
```

已完成并由 Worktree Controller 正式 release 的 feature branch 使用独立 retirement
operation。Controller 生成 OID-bound plan；Finalizer 分别执行 local/remote ref mutation，不删除
worktree，也不复用 commit/publish mode：

```bash
git-finalize \
  --retire-local-branch feat/example \
  --remote origin --integrated-into origin/main \
  --expected-local-oid <full-feature-oid> \
  --expected-integrated-oid <full-main-oid> \
  --retirement-plan-id <controller-plan-sha256> \
  --repo /home/user/projects/example \
  --dry-run
```

```bash
git-finalize \
  --retire-remote-branch feat/example \
  --remote origin \
  --integrated-into origin/main \
  --expected-remote-oid <full-feature-oid> \
  --repo /home/user/projects/example \
  --dry-run
```

每次 retirement 都重新 fetch 并查询 live remote，只接受 `refs/heads/*`，拒绝受保护分支和
remote default branch，要求 clean attached worktree、feature OID 精确匹配、feature commit
是 live integration target 的 ancestor，且没有 checkout、local-only/remote-only 或其他
local upstream 依赖。需要 CI 时增加 `--ci-required --ci-status SUCCESS --ci-commit-oid <oid>`
以及 `--ci-verification-source tool_authenticated|human_authenticated_ui`；CI OID 必须等于 live
integration target OID。

plan-bound 操作使用 Controller 的公开 verify/authorize/complete 协议：producer 在 repository
lock 下核验 authority 并签发一次性授权，Finalizer 不读取私有 authority 文件或重算 digest。
任何 Controller 状态漂移都会在 Git ref mutation 前 fail closed。local 删除
只使用 `git update-ref -d <ref> <expected-oid>` 的 compare-and-delete，且验证目标缺失、其他 local
refs、live remote、HEAD/index/worktree/tags 未变。local 与 remote summary/receipt 相互独立，
允许任一侧完成后幂等续办，不声明原子性。cherry-equivalent、merge-only、UNIQUE/MIXED 以及历史
未登记 refs 第一版仅由 Controller 分类报告，Finalizer 不自动删除。

`--integrated-into` 接受 branch、完整 `refs/heads/*`，以及与 `--remote` 同名的
`<remote>/<branch>` remote-tracking 写法；后者在 live remote 查询前规范化为
`refs/heads/<branch>`。若远端 branch 名本身以 remote 名开头，使用完整 `refs/heads/*` 消除歧义。

实际删除只使用绑定 exact expected OID 的
`--force-with-lease=<ref>:<expected> <remote> :<ref>` compare-and-delete；它不是 unconditional
force push。删除后工具实时确认 remote ref 缺失，执行 `fetch --prune`，并验证 remote-tracking
ref 缺失、integration OID 未变、HEAD/index/worktree/tags 未变。`--summary` 的
remote `mode_result.result` 为 `REMOTE_BRANCH_RETIRED_VERIFIED`、`ALREADY_ABSENT_VERIFIED`、
`RETIREMENT_PREFLIGHT_PASSED`、`RETIREMENT_BLOCKED` 或 `REMOTE_DELETE_UNVERIFIED`，同时构成
deterministic retirement receipt。local 对应 `LOCAL_BRANCH_RETIRED_VERIFIED` 和
`LOCAL_DELETE_UNVERIFIED`；Finalizer 不另建持久 audit store，通过公开 complete 由 Controller
分别登记两类结果。Git 删除成功但 complete 被拒绝时仍报告失败。

正式 handoff 全部走 Worktree Controller 公开合同：`worktree-controller
branch-retirement-plan --repo <path> (--branch <b>|--allocation-id <id>) --remote <r>
--integrated-into <t> --json` 在 Controller authority 下持久化 plan 并给出 `plan_id`；
原有 plan-only CLI 参数不变，Finalizer 自动完成授权及登记，调用方不需要再执行 record。
`branch-retirement-status --plan-id <id>` 和 `branch-retirement-executions --action list` 是公开
核对入口。Controller 必须明确公布三个 retirement capability 的版本 1；缺配或未安装时阻断。
plan 绑定创建时的 authority 快照，branch 自身移动由 expected-OID CAS 兜底，因此应在安静的
短窗口内创建并消费。完成且 ref 仍缺失时重复调用返回 `ALREADY_ABSENT_VERIFIED`；receipt
已存在而 ref 再现时拒绝。

中断恢复使用同一 plan/operation request。旧 executor 仍存活时不重入；已结束且 ref 仍为
expected OID 时通过公开 abandon 重新授权，ref 已缺失时只完成原授权，不重复删除。未知
remote 结果先查询 live ref。旧消费者已删除且没有未完成授权的目标，通过公开 record 登记
`ATTEST_ABSENT` 观察，Controller 凭据保留 `CONTROLLER_OBSERVED` 来源。

### 有界结果摘要

normal、commit-only、verify-only、initial、initial-branch、publish-existing-branch、retirement
和 resume 均可显式加入
`--summary`，以单行确定性 JSON 代替原有阶段输出：

```bash
git-finalize \
  --summary \
  --repo /home/user/projects/example \
  --message "fix: describe the change" \
  -- path/to/file
```

未指定时，stdout、stderr 和退出码沿用原合同。摘要直接来自本次执行状态，不会为构造结果
重跑 Git 命令；成功、阻断和失败仍分别保留真实 `status`、`final_phase` 与 `exit_code`。
公共字段包括仓库、branch/upstream、请求路径数、commit、push、post-verify、warning、失败原因
和 `next_action`。只有实际执行使用显式 branch refspec 和 `--no-follow-tags` 的 push 时，
`push.branch_refspec_only` 与 `follow_tags_requested` 才分别为 `true` 和 `false`；未执行 push
时两者为 `null`。这些字段不枚举远端已有 tag。

initial 的 `mode_result` 另含 initial publish、Snapshot artifact 校验和远端最终结论；
initial-branch 另含首次分支发布状态、远端最终结论和 post-verify；publish-existing-branch
另含原 HEAD 复用、commit-range 安全检查、push 目标及 HEAD/index/worktree 不变证明；resume 另含实际 interface 与
publish kind、恢复起点、原提交复用、发布前 ahead/behind、commit 数量与 range、push 目标、最终
HEAD/remote/worktree、已完成阶段和是否仍需恢复；commit-only 另含跳过的远端验证、post-verify
和最终工作区 `clean|dirty` 状态，其 `push.result` 为 `skipped_by_commit_only`。只有确有恢复入口时才输出顶层
`resume` 引用。verify-only 的 `mode_result` 另含本地验证结果、commit/push/远端验证跳过原因，
以及验证前后 HEAD、index 和 worktree 不变的结论；其 `push.result` 为
`skipped_by_verify_only`。warning 最多
5 条、每条最多 256 字符；失败原因最多 512 字符，省略数量通过对应 `*_omitted*` 字段显式
报告。摘要不包含 diff、文件正文、commit message、凭据或完整命令日志。失败仍使用非零退出码；
若序列化本身失败，fallback 会同时保留 Git 操作状态和原退出码。

### Exact synthetic fixture exceptions for existing history

`--fixture-exceptions /absolute/reviewed-fixtures.json` is accepted only by
`--publish-existing-branch`, `--publish-existing-history` and
`--resume-existing-history-publish`. The first scans commits not reachable from the
captured remote heads; the latter two scan the entire proposed history, including
when the remote already contains the expected commit. All three use the same exact
binding and content checks. This does not enable the path-only `--allow-test-fixture`
option or grant an exception to ordinary `--resume-publish`.

The JSON contract is `{"schema_version":1,"repository":{"root_commit":"<full root OID>",
"remote_url_sha256":"<SHA256 of exact push URL, without newline>"},"exceptions":[
{"path":"tests/example.py","blob_oid":"<full blob OID>","sha256":"<content SHA256>",
"detector":"private-key-header-v1","reason":"Reviewed synthetic test data"}]}`.
Repository identity binds the single reachable root commit and selected push endpoint;
each reviewed historical blob requires its own entry. The file must be a canonical
absolute regular file, at most 64 KiB, with 1–16 entries. Paths must be exact canonical
`tests/` paths, without glob syntax. Supported rule IDs are `private-key-header-v1`,
`known-token-v1` and `credential-assignment-v1`, corresponding to the existing private-key
header, known token prefix and quoted credential assignment detectors. Each matched
rule needs its own reviewed entry. Other rules (including SSH keys) and all path, type,
size and Git gates remain active.
Every historical blob at an explicitly excepted path must match a listed approval,
even if a later mutation removes the detector signature.
No fixture is approved automatically: the caller must review its origin and complete
content before explicitly supplying this file.

Unknown fields/rules/schema, duplicate entries or JSON keys, repository/hash mismatch,
and unused exceptions fail closed. Changed content or another path requires a new
explicitly reviewed identity. Output and `--summary` record `exception_id` (SHA256 of
the canonical approval), repository, matched path/blob/SHA256, detector, reason and
`applied=true`. No content or credentials are included. Resume requires the same
explicit exception file; a previous successful scan never disables later scans.

## Execution boundary

Terminal and agent executors invoke the same `git-finalize` CLI as the current non-root user.
The executor retains native approval and OS sandbox enforcement. No private session files,
transcript parser, provider hook, or host execution bridge is required. A sandbox refusal must
use the executor's native approval path, never a permission-mode override or an alternate proxy.
The CLI retains explicit scope, sensitive-source review, worktree, commit and publication checks.


## 工作流边界

正常开发流程是先完成修改、测试和审查，再调用正常模式提交推送。首次发布仅适用于 unborn
本地仓库和完全空远端；需要同时创建 commit 的已有历史功能分支首次发布使用 initial-branch
模式，已有 clean committed HEAD 且无 upstream 时使用 publish-existing-branch；经显式 Gitea
bootstrap 的 verified-empty repository 接收既有历史时使用 publish-existing-history。恢复模式仅
适用于干净状态且不会再次创建提交：root 恢复入口处理 initial-publish 的单 root commit，
existing-history first-publication 使用自己的精确恢复入口，通用恢复入口处理 configured upstream
之上的一个或多个既有 commit。commit-only 仅在已有 commit 的
attached branch 上创建本地提交，不接触远端；verify-only 在相同本地 branch 边界内只验证显式
候选范围，不修改 HEAD、index、worktree、refs、tags 或配置，也不接触远端。

空远端经 `git clone` 创建的 unborn 分支可能已有精确匹配但尚不可解析的 tracking target；
initial 与 root resume 接受该状态并由成功 push 自然完成 upstream。正常模式和通用 resume 仍要求
upstream commit 已可解析。Upstream target 配置与 resolved upstream commit 是不同事实。

文件范围必须在 `--` 后逐项显式给出；脚本不会使用 `git add .` 或 `git add -A`。测试夹具豁免只能通过 `--allow-test-fixture <exact-path>` 精确指定。

若显式路径已经作为删除暂存，而工作树中的同名本地副本已被忽略，工具会保留该 staged deletion，不会重新添加或提交本地副本。

默认拒绝超过 5 MiB 的明显二进制文件。确需保留原始图片等文件时，可重复使用 `--allow-large-binary <exact-path>`，但该路径必须同时出现在 `--` 后的显式文件范围内，必须是普通文件，工作树与 staged blob 都必须大于 5 MiB 且不超过 25 MiB。该参数只放行精确文件的大小检查，不放行敏感路径、credential / secret 内容检查、staged scope、远端状态或快进检查；超过 25 MiB 仍会拒绝。

existing branch/history publication 与 resume 模式也接受这一参数；它们不创建 commit，
例外绑定规范路径的普通 HEAD blob，仍要求大于 5 MiB、至多 25 MiB。待发布历史中其它
blob 不获得例外，未匹配范围的例外也拒绝。清理已发布且现在 ignored 的删除路径不会误报，
更早的待发布新增 blob 继续扫描。

例如：

```bash
git-finalize \
  --repo /home/user/projects/example \
  --message "docs: archive original photo" \
  --allow-large-binary Attachments/photo.jpg \
  -- Attachments/photo.jpg
```

工具不会自动 pull、merge 或 rebase，不会 amend 或 force push。远端 repository 仅能通过上述
显式 `--repo-plan` / `--repo-ensure` 路径创建，绝不会由 commit/push 失败隐式触发。

运行完整检查：

```bash
just check
```

仓库内 Python 入口均使用进程级 `-B`，避免工具执行或 Hook 检查向源码树写入
`__pycache__`。语法与行为验证使用 `just check`；不要以 `python -m py_compile` 作为本仓库的
常规检查，因为 `py_compile` 会显式生成 `.pyc`，即使解释器同时传入 `-B`。

## Standalone 边界

Git Finalizer 核心（verify、commit、publish、resume、initial 系列）只要求 bash、Git 与
`PATH` 中的 `python3`（Python 3.11+），可在无 Worktree Controller、无 Context Loader/Snapshot Runner、无共享
Skill 安装、无私有 Gitea 配置的独立仓库中运行（`tests/test_standalone_operations.sh` 以最小
HOME/PATH 环境证明）。Controller 治理（retirement、reviewed-sensitive-source linkage、
integration publication）与可选集成（`--snapshot` 证据、`--repo-plan/--repo-ensure` 的显式
Gitea 目标）只在被明确请求时生效；缺失对应 authority 或依赖时按精确 blocker fail closed，
永不隐式降级或回退到非权威状态。共享 `git-change-delivery` Skill 是面向 Agent 工作流的可选
使用文档与 helper 集，不是 Git Finalizer 的运行依赖，也不是任何授权来源。

每个可选集成的外部布局知识都收敛到唯一带 `GF-INTEGRATION-ADAPTER` 标记的 adapter，边界矩阵、
fail-closed blocker 与 Agent-facing machine-readable 结果合同见
[`integration-boundaries.md`](integration-boundaries.md)，由
`tests/test_integration_boundaries.sh` 机器核对。
