# GF Integration Boundary

Git Finalizer（GF）核心只依赖 GNU bash、Git、GNU utilities 与 `PATH` 中的 Python 3.11+。所有外部集成都是
**请求触发**的 adapter；本文件是适配器边界的权威清单，由
`tests/test_integration_boundaries.sh` 机器核对。每个 adapter 在其**每个**承载文件中以唯一
`# GF-INTEGRATION-ADAPTER: <name> (single site...)` 标记锚定外部布局知识；同一 adapter 可以
跨文件（retirement 同时标记 CLI 与 protocol companion），但任何文件内不得出现第二处该标记，
其他位置禁止新增同类布局字面量。

## 边界矩阵

| Integration | 触发命令 | 能力检测 | 版本兼容 | 不可用/不兼容时（fail-closed） | 允许的 adapter 内部知识 |
| --- | --- | --- | --- | --- | --- |
| Worktree Controller（retirement） | `--retire-local-branch` / `--retire-remote-branch` + `--retirement-plan-id` | `capabilities --json` 中 `branch_retirement_version`、`branch_retirement_verify_version`、`branch_retirement_execution_version` 均为 1 | plan v1、公开 `branch-retirement-execution/v1` 与 `retirement-execution-result/v1` | 缺执行器 → `Worktree Controller is required`；能力拒绝/不匹配 → `Controller capabilities refused or failed` / `required public retirement protocols`；receipt 与 ref 矛盾 → `already records a consumed receipt`；仅有旧 namespace → `legacy codex-worktree namespace` | 只消费公开 status/verify/authorize/complete 与 executions/record；digest 不透明，不读私有文件。CLI 仅检查旧/新 namespace 的目录存在性以产生迁移 blocker |
| Worktree Controller（integration publication） | 仅 `--publish-integration-candidate` | `capabilities --json` 中 `integration_publication_execution_version` 为 1 | 公开 `integration-publication-session/v1` JSON-line session | 缺执行器 → `Worktree Controller is required`；缺能力 → `required public integration publication protocol`；核验、授权或完成确认拒绝 → blocked；断连/超时先读远端事实再重入 | 只消费公开 identity、verify/authorize/complete 与 producer-observed completion；不读 lease、lock、binding、record、intent 或 prepared receipt 私有文件 |
| Worktree Controller（reviewed sensitive source） | 仅 `--reviewed-sensitive-source <file>` | 无状态读取；linkage 四字段作为不透明 identity 消费 | review-file schema 由 GF 自身合同定义 | 缺 linkage → `source review 要求完整 Controller linkage` | 无（不触任何 WC state 布局） |
| Workflow deployment health | 仅 `tool-skill-sync doctor` | 只读 installed Controller capabilities 与 frozen runtime contract | compatibility 中显式列出的 CLI、capability、storage 版本 | 无安装、字节或合同漂移 → `FAIL`；不激活 runtime | Controller 可执行文件名、active runtime 的 `site-packages/worktree_controller/tool_cli_contract.json`、installed instructions 与 Skills 的路径身份；只在 `tooling/workflow_health.py` 的独立 adapter 内 |
| Snapshot Runner evidence | 仅 `--initial-publish --snapshot <64hex>` | state 根取 `XDG_STATE_HOME`（否则 `~/.local/state`），可整体重定向到 fixture | `meta.json`/`snapshot.json` 字段按对端**已发布**的证据合同标识校验 | artifact 缺失/损坏 → `snapshot evidence rejected: ...`，永不静默跳过 | `snapshot-runner/snapshots/<id>/` 目录形状、三个 artifact 名、已发布合同标识（`schema_version` 2、`producer_security_epoch` 4）与 GF 侧 fail-closed 字节上限 |
| Gitea repository bootstrap | 仅 `--repo-plan` / `--repo-ensure` | 无；目标 URL 每次显式传入，无默认主机 | Gitea REST `/api/v1`（credential-free 探测，凭据走 Git authority） | 缺 `--gitea-url`/`--owner`/`--repo-name`/`--visibility` → argparse 用法错误 exit 2；探测或权限失败 → `BLOCK_INVALID_CONFIG`/`BLOCK_PERMISSION`/`BLOCK_INVALID_OWNER`/`BLOCK_REMOTE_MISMATCH` + `status=blocked` + exit 2（其中 `BLOCK_REMOTE_MISMATCH` 可能在 repository 已创建之后产生，是否变更看 `bootstrap.executed`）；决策无法归类 → `status=failed` + exit 3 | 只读 GET 与显式 POST 的端点形状集中于此 companion；POST 只在决策为 `CREATE_ALLOWED` 且调用方显式给出 `--repo-ensure` 时发出（GF 侧不设交互确认门，显式命令本身即授权边界） |

## 遗留兼容性知识与版本边界

- retirement 的判定与 authority digest 属于 Controller。Finalizer 不复制 canonicalization、digest
  recipe 或私有 authority 文件清单，也不在外部持有 exclusive lock 后调用 producer。
- integration publication 的私有状态核验同样属于 Controller。公开 session 由生产方持有现有
  repository lock，跨度为 verify → authorize → consumer push → producer remote observation。
  Finalizer 只校验公开 identity 与 action，执行 non-force branch push，并独立 live verify。
  缺少 protocol 不回退；断连释放锁后，remote 已等于同一 candidate 的重入只恢复结果。
  completion 的执行回执不代替 `lease-complete` 持久化的 lifecycle receipt；lease 和 checkout
  释放仍归上层 Controller。该协议信任本地调用方与 OS，不声称防止任意同 UID 干预。
- public verify 提供只读 verdict；authorize 在 producer lock 下重新核验并签发一次性授权，
  Finalizer 执行 expected-OID CAS，complete 由 producer 验证结果并保存 `AUTHORIZED_EXECUTION`
  凭据。任何 complete 拒绝都保留失败，不能仅凭 ref 缺失宣称完成。
- 请求 identity 由 plan/operation 稳定绑定。仍存活的 executor 阻断重入；已结束且 expected ref
  仍存在时通过公开 abandon 重新授权，已缺失时只完成原授权。已完成操作返回幂等结果；receipt
  与重新出现的 ref 矛盾时拒绝。无未完成授权的旧消费者通过公开 record 登记 `ATTEST_ABSENT`，
  provenance 为 `CONTROLLER_OBSERVED`，不伪装为授权 CAS。
- `mode_result.retirement_receipt_id` 只来自已核验的 Controller 正式凭据；首次完成、
  幂等重查和旧消费者的已缺失引用恢复均保留该编号，不复制生产方的编号算法。
- plan-only CLI 输入不变；缺少 Controller 或任一 required public retirement protocols 时 fail
  closed，不回退到私有文件核验。仅 legacy namespace 的仓库仍保留迁移 blocker，迁移归 producer。
- Snapshot Runner 与 Gitea 的知识只到“对端已发布合同 + 形状”一层：GF 固定 Snapshot Runner README
  宣布的证据合同标识（`schema_version` 2、`producer_security_epoch` 4），并 fail-closed 镜像其
  **未发布**的字节上限；Gitea 侧只固定 REST `/api/v1` 端点形状。GF 不固定任何 sibling 的**工具
  版本号**，因此 sibling 的普通版本漂移不得要求核心改动；对端提升已发布的证据合同标识时，表现为
  `snapshot evidence rejected` 的精确 blocker，而不是静默误判。

## 对代理稳定的 machine-readable 结果合同

- **bash 拥有的生命周期命令**（verify/commit/publish/resume/initial/retirement 系列）每次
  `--summary` 输出单一 JSON：顶层**固定**键集合为
  `allocation_id,authority_key,branch,commit,dry_run,exit_code,final_phase,finalizer_version,mode,mode_result,next_action,next_action_omitted_characters,push,reason,reason_omitted_characters,repository,repository_id,requested_path_count,role,status,summary_schema_version,task_key,upstream,warning_characters_omitted,warnings,warnings_omitted,worktree_path`；
  `summary_schema_version` 保持 1。该集合与模式无关（由 `tests/test_integration_boundaries.sh` 对
  verify-only、commit-only 与 blocked 路径核对）。
- 另有四个**条件键**只在相关场景出现，消费者必须按可选处理：`resume`（resume 接口的恢复上下文）、
  `fixture_exceptions`（fixture 例外记录）、`reviewed_sensitive_sources`（已消费的 sensitive-source
  review 身份）、`diagnostics`（实际入口/sidecar 路径与摘要，以及同一次扫描绑定的首个内容拒绝元数据）。
  `diagnostics.schema_version` 为 1；内容 finding 仅含规则、路径、扫描阶段、大小、SHA-256 和可用的
  blob OID，不含命中值或源码片段。消费者忽略未知可选字段，继续按原三元组判断；其它拒绝的
  `content_scan` 为 null。内容拒绝/风险自动带诊断，其它 bash core 场景通过 `--summary --diagnostics`
  按需展开；普通摘要保持原输出预算。测试从源码提取条件键并要求本文件逐个列出，新增条件键必须同时改文档。
- 序列化失败的兜底对象使用**不同**的键形状（`summary_status`/`operation_status`/
  `operation_exit_code`），它不是正常合同的一部分，只表示“无法产出合规摘要”，Agent 必须按失败处理。
- `status` 的取值封闭为 `success`、`blocked`、`failed`（测试从源码 `summary_status` 字面量提取并要求
  逐个出现）。`commit_retained` 不是 status，而是 `summary_mode_state`/`mode_result` 的模式状态词，
  用于表达“commit 已创建但发布被阻断”。
- 错误分类由三元组 `(final_phase, status, next_action)` 承载。`next_action` 是**封闭枚举**，测试要求
  本文件列出源码出现的全部字面量：`none`、`resolve_blocker_and_retry`、`inspect_failure`、
  `inspect_failure_with_local_commit_retained`、`inspect_local_root_commit`、`inspect_existing_branch_publish_failure`、
  `inspect_initial_branch_publish_failure`、`resume_initial_publish`、`resume_existing_history_publish`、
  `retry_same_resume`、`inspect_remote_state_then_rerun_same_retirement`、`rerun_without_summary_for_diagnostics`、
  `run_without_dry_run_after_review`、`publish_install_verify_then_run_without_dry_run`。
  governance/authority 阻断固定使用 `resolve_blocker_and_retry`；人工诊断文本只在 `reason`
  （截断时带 `reason_omitted_characters`），机器消费者不得解析自然语言前缀做分类。
- 退出码公开面：bash 主路径只用 `{0,1}`，且 `exit_code` 与进程退出码一致（测试对 blocked 路径核对
  rc 与 JSON 字段相等）。exec-forwarded companion **不**产出上述 envelope：
  `--repo-plan/--repo-ensure` 输出自己的 bootstrap receipt（含 `decision`、`bootstrap`、`errors`
  等键，无 `exit_code`/`final_phase`/`mode_result`），自身判定为 `0` 成功、`2` 用法错误
  （argparse）或 `BLOCK_*` 阻断、`3` 决策无法归类的失败；`1` 仍可能由 GF 的 pre-execution guard
  （伴生文件缺失/不安全）或未捕获的伴生 traceback 产生，因此**不能**把 rc 2 读成“远端未发生变化”
  ——`BLOCK_REMOTE_MISMATCH` 可能发生在 repository 已创建之后，是否变更由 receipt 的
  `bootstrap.executed` 表达；`--publish-integration-candidate` 输出自己的
  判定对象：blocked 时是 7 键（`final_phase,finalizer_version,mode,next_action,reason,status,summary_schema_version`）；
  success 时另有 repository/allocation、lease/run、target/expected main/candidate、`record_id`、
  `push`、`remote_verify` 和 `controller_execution`，后者标记公开 protocol 与尚需 lifecycle
  completion。`mode` 为 `integration_candidate_publish`，退出码 `{0,1,2}`，其 `next_action` 使用 companion
  自己的词表（例如 lease 缺失时的 `read_remote_fact_and_reenter_controller_publish_gate`），不属于上面
  列出的 bash `next_action` 枚举。Agent 不得假设 companion 输出与 `--summary` 同形。
  `tool_cli_contract.json.exit_codes` 与 bootstrap 的分类实现由 `tests/test_tool_contract.py`
  双向核对，且核对是“反谎话”的：合同文本必须声明 `BLOCK_*` 为 `status=blocked`、必须把远端变更
  事实指向 `bootstrap.executed`、必须承认 `1` 来自 pre-execution guard/未捕获异常，并禁止任何
  “never returns”或“refusal before any remote mutation”式排序保证；这些断言同时绑定伴生源码里的
  分类行与 `remote changed before origin update` 位点。该文件任何字节改动都会连带 `public_cli_contract_sha256` 与本仓库
  `tool_skill_manifest.json` 的重绑定，因此合同文本与 manifest 必须在同一提交内一起更新。

## 独立测试矩阵

| 集成 | 正向 | 负向（fail-closed） | sibling 版本漂移隔离 |
| --- | --- | --- | --- |
| 核心生命周期（无外部集成） | `tests/test_standalone_operations.sh`（最小 HOME/PATH，无 controller/sibling/Skill/Gitea） | 同套件断言 governance 请求仍被精确 blocker 拒绝且零变更 | 完全独立：不加载任何 sibling |
| WC retirement | `tests/test_retire_branch_real_controller.sh`（真实 controller 作 oracle，入口缺席时显式 skip）、`tests/test_retire_local_branch.sh`、`tests/test_retire_remote_branch.sh` | 篡改/drift/receipt 矛盾/lock 串行化 + capability 拒绝、completion 拒绝与恢复；`tests/test_retirement_protocol.py` 覆盖过期授权、活跃 executor 和公开协议中断 | 不 import controller 代码、不复制其版本常量；contract 变化表现为能力/plan 校验失败而非测试崩溃 |
| WC integration publication | `tests/test_integration_publish.py`（独立公开协议 stub，无私有 metadata）与 producer 的真实 consumer acceptance | 能力缺失、身份/阶段/schema 漂移、超时、推送失败、完成拒绝与中断恢复 → blocked 或精确恢复 | 只消费公开 versioned protocol，不 import producer 代码或读取存储布局 |
| WC reviewed sensitive source | `tests/test_reviewed_source.py` | linkage 不全 → `source review 要求完整 Controller linkage` | 四字段作不透明 identity |
| Snapshot Runner evidence | `tests/run.sh` 内 `test_initial_snapshot_*` 用例（`XDG_STATE_HOME` 重定向 fixture） | `tests/test_standalone_operations.sh` 证据缺失用例 + `tests/test_integration_boundaries.sh` 状态根重定位探针 | 只读已发布合同标识与 artifact 形状，不校验生产者工具版本 |
| Gitea repository bootstrap | `tests/test_repo_bootstrap.py`（stub HTTP） | `BLOCK_*` 决策 → exit 2、`UNKNOWN` → exit 3、缺参数 → argparse 2 | 无默认主机、无工具版本常量 |
| 边界与 machine 合同本身 | `tests/test_integration_boundaries.sh`（marker 单点、扩散 ledger、矩阵↔代码一致、`--summary` 键集合与词表、companion 输出不同形） | 同一套件即回归门 | 不依赖任何 sibling 存在 |

## 禁止扩散清单（测试强制）

强制范围是**生产代码面**：根 CLI `git-finalize`、根目录 `git-finalize-*.py` 伴生与
`tooling/*.py`，按文件系统枚举（含未纳入 git 的同类新文件）。测试 fixture、文档与本文件、
Skill 参考和 `.gitea/workflows` 不在该范围内：前者必须能构造外部状态才能验证合同，后者不含
运行时布局推导。`tooling/tool_skill_sync.py` 会按**名称**引用 sibling 工具（它是安装器），但
不引用任何对端 state 布局；下面的 ledger 因此按布局字面量与外部可执行文件探测位点约束它。

- `codex-worktree` 只允许出现在 CLI 的 namespace 迁移 blocker。
- `worktree-controller/v1` 状态根只允许出现在 CLI 的目录存在性检查。
- `repo.lock`、`publication-lease` 及 allocation/intent/prepared receipt 私有布局禁止出现在生产代码。
- `branch-retirement-plans`/`branch-retirement-receipts` 文件名和 `worktree-policy.toml` policy term
  禁止出现在生产代码；retirement 只消费公开接口。
- `worktree-controller` 可执行文件探测（`which("worktree-controller")`）只允许在 retirement、
  integration publication protocol companions 与 read-only deployment-health adapter；核心生命周期不得按名字调用 controller。
- active runtime frozen contract 路径只允许在 deployment-health adapter。
- retirement adapter 识别 public CLI schema 中 execution/v1 的精确 capability 描述；
  working/index/history 共用 assignment 判定，不按文件名豁免 JSON；其它敏感赋值和
  key/token 签名保持拒绝，重复键或非法 JSON 不进入 schema 描述处理。
- `snapshot-runner/snapshots` 布局只允许出现在 snapshot adapter。
- `/api/v1` 只允许出现在 Gitea bootstrap adapter。
- Context Loader 无运行时耦合（GF 不调用它，也不引用其名称或状态）。
