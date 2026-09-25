# GF Integration Boundary

Git Finalizer（GF）核心只依赖 bash、Git 与 `/usr/bin/python3`。所有外部集成都是
**请求触发**的 adapter；本文件是适配器边界的权威清单，由
`tests/test_integration_boundaries.sh` 机器核对。每个 adapter 在源码中以唯一
`# GF-INTEGRATION-ADAPTER: <name> (single site...)` 标记锚定其外部布局知识，禁止在
其他位置新增同类布局字面量。

## 边界矩阵

| Integration | 触发命令 | 能力检测 | 版本兼容 | 不可用/不兼容时（fail-closed） | 允许的 adapter 内部知识 |
| --- | --- | --- | --- | --- | --- |
| Worktree Controller（retirement） | `--retire-local-branch` / `--retire-remote-branch` + `--retirement-plan-id` | `worktree-controller capabilities --json` 的 `decision.branch_retirement_version` 必须等于 plan `schema_version`；可执行文件缺席时**显式跳过**探测并落到文档化的 legacy 独立核验路径，探测不可读或不匹配一律 fail closed | plan schema v1；capability 版本必须显式匹配 | 无 state → `Controller repo.lock 缺失或不安全`；legacy namespace → `legacy codex-worktree namespace` 迁移 blocker；能力不可读/不匹配 → `capabilities probe failed` / `does not match plan schema`；plan 重放 → `already records a consumed receipt` | 状态根 `<GIT_COMMON_DIR>/worktree-controller/v1`、`repo.lock`、`branch-retirement-plans/<64hex>.json`、`branch-retirement-receipts/<id>-{local\|remote}.json`（receipt 仅存在性检查）、policy term `<canonical>/.agents/worktree-policy.toml`、`worktree-controller` 可执行文件名（唯一 `shutil.which` 位点）；legacy `codex-worktree` 只用于命名迁移 blocker，任何路径都不读遗留状态 |
| Worktree Controller（integration publication） | 仅 `--publish-integration-candidate` | 只读取 Controller 已签发的 lease 与其锁；不探测其余 authority 目录 | lease 字段按 GF 公开输入合同校验 | lease 缺失/漂移 → blocked，不写 remote | `publication-lease.json`、`repo.lock`、`repo.json`、`bindings/wt_<sha256(git-dir)>.json`、`records/<allocation_id>.json`、`integration-intents/*.json`（前缀枚举）、`prepared-candidate-receipts/<id>.json`。其中 `wt_` 键派生与 intent 语义是 Controller 内部事实，GF 只作镜像消费，已记为债务（见下节） |
| Worktree Controller（reviewed sensitive source） | 仅 `--reviewed-sensitive-source <file>` | 无状态读取；linkage 四字段作为不透明 identity 消费 | review-file schema 由 GF 自身合同定义 | 缺 linkage → `source review 要求完整 Controller linkage` | 无（不触任何 WC state 布局） |
| Snapshot Runner evidence | 仅 `--initial-publish --snapshot <64hex>` | state 根取 `XDG_STATE_HOME`（否则 `~/.local/state`），可整体重定向到 fixture | `meta.json`/`snapshot.json` 字段按对端**已发布**的证据合同标识校验 | artifact 缺失/损坏 → `snapshot evidence rejected: ...`，永不静默跳过 | `snapshot-runner/snapshots/<id>/` 目录形状、三个 artifact 名、已发布合同标识（`schema_version` 2、`producer_security_epoch` 4）与 GF 侧 fail-closed 字节上限 |
| Gitea repository bootstrap | 仅 `--repo-plan` / `--repo-ensure` | 无；目标 URL 每次显式传入，无默认主机 | Gitea REST `/api/v1`（credential-free 探测，凭据走 Git authority） | 缺 `--gitea-url`/`--owner`/`--repo-name`/`--visibility` → argparse 用法错误 exit 2；探测或权限失败 → `BLOCK_INVALID_CONFIG`/`BLOCK_PERMISSION`/`BLOCK_INVALID_OWNER`/`BLOCK_REMOTE_MISMATCH` + `status=blocked` + exit 2（其中 `BLOCK_REMOTE_MISMATCH` 可能在 repository 已创建之后产生，是否变更看 `bootstrap.executed`）；决策无法归类 → `status=failed` + exit 3 | 只读 GET 与显式 POST 的端点形状集中于此 companion；POST 只在决策为 `CREATE_ALLOWED` 且调用方显式给出 `--repo-ensure` 时发出（GF 侧不设交互确认门，显式命令本身即授权边界） |

## 遗留兼容性知识与版本边界

- Worktree Controller 的 authority state digest 重算只存在于 retirement plan verifier 内，并显式
  标注为**遗留兼容独立核验路径**。上游已在 `worktree-controller/tool_cli_contract.json` 的
  `state_digest` 段把该 recipe 发布为 contract v2 的一部分，并要求外部消费者“只从
  `branch-retirement-verify` 取得判定，不得复制 canonicalization、digest recipe 或 blocker 清单”。
  因此 GF 的镜像是**被点名的过渡性偏差（技术债务）**，不是中立的防御性设计。
- 收敛目标明确但**当前不可替换**：公开只读接口
  `worktree-controller branch-retirement-verify --repo <path> --plan-id <sha256> --operation {local|remote} --json`
  （read_only，result contract `branch-retirement/v2`，存在性由
  `decision.branch_retirement_verify_version` 宣告，四种判定 `VALID/PLAN_ABSENT/STATE_STALE/PLAN_MALFORMED`
  一律以 rc 0 返回，只有 `ControllerError` 返回 2）只存在于上游 `main` 一代。GF 本批次**不**切换，
  三条可复核的原因：
  1. 本环境实际可调用的 production controller 入口仍是上一代：`--version` 无该动词，
     `capabilities` 不宣告 `branch_retirement_verify_version`；切换会立即破坏真实 controller E2E；
     上游同日的在途实验已把 `branch_retirement_contract.version` 升为 3、`storage_layout.version`
     升为 2 并新增三个动词，而 `branch_retirement_verify_version` 仍为 1，说明该代语义当日即可移动
     而版本宣告不变。
  2. 它**不是** digest 镜像的直接替代品：`verify` 在拿锁之前做 TOCTOU 观察，且不复核 plan 的八个
     identity 字段与调用方 CLI 意图；GF 在 exclusive 锁内仍需那次复核。因此收敛形态是“锁前用
     `verify` 观察替代 `capabilities` 探测，锁内改用 `branch-retirement-plan --json` 返回的完整 plan
     做字段复核”，而不是“删除独立核验”——`verify` 的锁外观察无法证明锁内状态未变。
  3. 上游没有提供无 controller 场景的等价判定：`verify` 需要可执行文件，而 GF 的独立核验路径在
     controller 缺席时必须继续 fail closed（签发 plan 中 `branch_retirement_trust_mode` 恒为
     `legacy`）。
  切换的前置条件因此是：production controller 实际激活带该动词的一代、上游冻结该 blocker 集合、
  并且明确无 controller 场景的权威判定入口或正式放弃该场景；届时 GF 的收敛形态是“锁前
  `verify` 观察 + 锁内 plan 字段复核”，而不是“删除独立核验”。
- 因此 digest 镜像继续承担无 controller 环境的 fail-closed 核验，位点不变、不扩展；它仍是被上游
  点名的过渡性偏差（“不得复制 canonicalization、digest recipe 或 blocker 清单”），而不是中立的
  防御性设计。
- 版本边界由两处共同承担：`decision.branch_retirement_version` 必须等于 plan 的
  `schema_version`（探测失败或不匹配即 fail closed；可执行文件缺席时显式跳过并走独立核验），以及
  plan 文件名空间的 `worktree-controller/v1` 常量。legacy `codex-worktree` namespace 只用于产生
  精确迁移 blocker；namespace 迁移本身归 Worktree Controller 治理，GF 不提供迁移通道。
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
- 另有三个**条件键**只在相关场景出现，消费者必须按可选处理：`resume`（resume 接口的恢复上下文）、
  `fixture_exceptions`（fixture 例外记录）、`reviewed_sensitive_sources`（已消费的 sensitive-source
  review 身份）。测试从源码提取这三者并要求本文件逐个列出，新增条件键必须同时改文档。
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
  7 键判定对象（`final_phase,finalizer_version,mode,next_action,reason,status,summary_schema_version`，
  其中 `mode` 为 `integration_candidate_publish`），退出码 `{0,1,2}`，其 `next_action` 使用 companion
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
| WC retirement | `tests/test_retire_branch_real_controller.sh`（真实 controller 作 oracle，入口缺席时显式 skip）、`tests/test_retire_local_branch.sh`、`tests/test_retire_remote_branch.sh` | 篡改/drift/replay/lock 串行化 + capability probe 不可读或不匹配 | 不 import controller 代码、不复制其版本常量；contract 变化表现为能力/plan 校验失败而非测试崩溃 |
| WC integration publication | `tests/test_integration_publish.py` | lease 缺失/漂移/锁不合法 → blocked | 只消费 lease JSON 字段（其余 inventory 项为上表登记的债务） |
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

- `codex-worktree` 只允许出现在 retirement 的两个迁移 blocker 位点。
- `worktree-controller/v1` 状态根只允许出现在三个带 marker 的 adapter 文件。
- `repo.lock` 只允许出现在 retirement、integration publication 两个 lease adapter 与 bash 预检。
- `publication-lease` 只允许出现在 integration publication 与 retirement 两个 lease adapter。
- `branch-retirement-plans`/`branch-retirement-receipts` 文件名只允许出现在 retirement plan verifier。
- `.agents/worktree-policy.toml` policy term 只允许出现在 retirement plan verifier。
- `worktree-controller` 可执行文件探测（`which("worktree-controller")`）只有一个位点，即 retirement
  plan verifier 的能力探测；其他代码不得按名字调用 controller。
- `snapshot-runner/snapshots` 布局只允许出现在 snapshot adapter。
- `/api/v1` 只允许出现在 Gitea bootstrap adapter。
- Context Loader 无运行时耦合（GF 不调用它，也不引用其名称或状态）。
