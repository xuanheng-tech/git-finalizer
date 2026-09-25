# GF Integration Boundaries

Git Finalizer（GF）核心只依赖 bash、Git 与 `/usr/bin/python3`。所有外部集成都是
**请求触发**的 adapter；本文件是适配器边界的权威清单，由
`tests/test_integration_boundaries.sh` 机器核对。每个 adapter 在源码中以唯一
`# GF-INTEGRATION-ADAPTER: <name> (single site...)` 标记锚定其外部布局知识，禁止在
其他位置新增同类布局字面量。

## 边界矩阵

| Integration | 触发命令 | 能力检测 | 版本兼容 | 不可用/不兼容时（fail-closed） | 允许的 adapter 内部知识 |
| --- | --- | --- | --- | --- | --- |
| Worktree Controller（retirement） | `--retire-local-branch` / `--retire-remote-branch` + `--retirement-plan-id` | `worktree-controller capabilities --json` 的 `decision.branch_retirement_version` 必须等于 plan `schema_version`（二进制缺席时落到文档化的 legacy 独立核验路径） | plan schema v1；capability 版本必须显式匹配 | 无 state → `Controller repo.lock 缺失或不安全`；legacy namespace → `legacy codex-worktree namespace` 迁移 blocker；能力不可读/不匹配 → `capabilities probe failed` / `does not match plan schema`；plan 重放 → `already records a consumed receipt` | 状态根 `<GIT_COMMON_DIR>/worktree-controller/v1`、`repo.lock`、`branch-retirement-plans/<64hex>.json`、`branch-retirement-receipts/<id>-{local\|remote}.json`（receipt 仅存在性检查）；legacy `codex-worktree` 仅用于命名迁移 blocker |
| Worktree Controller（integration publication） | 仅 `--publish-integration-candidate` | 只读取 Controller 已签发的 `publication-lease.json` 与其锁 | lease 字段按 GF 公开输入合同校验 | lease 缺失/漂移 → blocked，不写 remote | `publication-lease.json`、`repo.lock`（不读其余目录清单） |
| Worktree Controller（reviewed sensitive source） | 仅 `--reviewed-sensitive-source <file>` | 无状态读取；linkage 四字段作为不透明 identity 消费 | review-file schema 由 GF 自身合同定义 | 缺 linkage → `source review 要求完整 Controller linkage` | 无（不触任何 WC state 布局） |
| Snapshot Runner evidence | 仅 `--initial-publish --snapshot <64hex>` | state 根取 `XDG_STATE_HOME`（否则 `~/.local/state`），可整体重定向到 fixture | `meta.json`/`snapshot.json` 字段按 GF 侧证据合同校验 | artifact 缺失/损坏 → `snapshot evidence rejected: ...`，永不静默跳过 | `snapshot-runner/snapshots/<id>/` 目录形状与三个 artifact 名 |
| Gitea repository bootstrap | 仅 `--repo-plan` / `--repo-ensure` | 无；目标 URL 每次显式传入，无默认主机 | Gitea REST `/api/v1`（credential-free 探测，凭据走 Git authority） | 缺 `--gitea-url` → argparse 拒绝；探测/权限失败 → `BLOCK_INVALID_CONFIG`/`BLOCK_PERMISSION`/`BLOCK_INVALID_OWNER`/`BLOCK_REMOTE_MISMATCH` 决策 + `status=blocked`；确认条件不满足 → `exit 3`；只有 `CREATE_ALLOWED` 才允许显式 POST | 只读 GET 与显式 POST 的端点形状集中于此 companion |

## 对代理稳定的 machine-readable 结果合同

- 每次 `--summary` 输出单一 JSON：顶层键集合固定为
  `allocation_id,authority_key,branch,commit,dry_run,exit_code,final_phase,finalizer_version,mode,mode_result,next_action,next_action_omitted_characters,push,reason,reason_omitted_characters,repository,repository_id,requested_path_count,role,status,summary_schema_version,task_key,upstream,warning_characters_omitted,warnings,warnings_omitted,worktree_path`；
  `summary_schema_version` 保持 1。
- 顶层键集合与模式无关（由 `tests/test_integration_boundaries.sh` 对 verify-only、
  commit-only 与 blocked 路径逐一核对）；嵌套对象按模式裁剪子字段，例如 `commit-only` 的
  `push.result=skipped_by_commit_only`、`verify-only` 的 `mode_result.local_validation`。
  机器消费者只可依赖本文件声明的子字段，不得假设跨模式同形。
- `status ∈ {success, blocked, failed, commit_retained}`；`exit_code` 与进程退出码一致
  （0=success，1=其余）。错误分类由三元组 `(final_phase, status, next_action)` 承载：
  governance/authority 问题固定 `next_action=resolve_blocker_and_retry`，人工诊断文本在
  `reason`（截断时带 `reason_omitted_characters`），机器消费者不得解析自然语言 reason 之外的
  前缀做分类。`mode_result.result` 的枚举即上表各命令的公开结果码。
- 退出码公开面：bash 主路径 `{0,1}`；exec-forwarded 伴生 `{0,2,3}`（`--repo-plan/--repo-ensure`）
  与 `{0,1,2}`（`--publish-integration-candidate`），与 `tool_cli_contract.json.exit_codes`
  一致。

## 遗留兼容性知识与版本边界

- Worktree Controller 的 authority state digest 重算只存在于 retirement plan verifier 内，
  并显式标注为**遗留兼容独立核验路径**（镜像冻结的 v1 布局）。该 recipe 不属于任何已发布
  合同，因此新版本 controller 的治理语义不得在此累积；新增治理判定必须走公开
  `capabilities` / plan / receipt 合同。
- 版本边界由两处共同承担：`decision.branch_retirement_version` 必须等于 plan 的
  `schema_version`（能力探测失败或不匹配即 fail closed），以及 plan 文件名空间的
  `worktree-controller/v1` 常量。legacy `codex-worktree` namespace 只用于产生精确迁移
  blocker，任何路径都不会读取非权威遗留状态；namespace 迁移本身归 Worktree Controller 治理。
- Snapshot Runner 与 Gitea 的兼容知识只到“形状”一层：artifact 目录/文件名与 REST 端点形状，
  不含对端版本常量、内部字段推导或生产者实现假设。GF 不 pin sibling 工具版本，sibling 升级
  不得要求核心改动。

## 独立测试矩阵

| 集成 | 正向 | 负向（fail-closed） | sibling 版本漂移隔离 |
| --- | --- | --- | --- |
| 核心生命周期（无外部集成） | `tests/test_standalone_operations.sh`（最小 HOME/PATH，无 controller/sibling/Skill/Gitea） | 同套件断言 governance 请求仍被精确 blocker 拒绝且零变更 | 完全独立：不加载任何 sibling |
| WC retirement | `tests/test_retire_branch_real_controller.sh`（真实 controller 作 oracle，入口缺席时显式 skip）、`tests/test_retire_local_branch.sh`、`tests/test_retire_remote_branch.sh` | 篡改/drift/replay/lock 串行化 + capability probe 不可读或不匹配 | 不 import controller 代码、不复制其版本常量；contract 变化表现为能力/plan 校验失败而非测试崩溃 |
| WC integration publication | `tests/test_integration_publish.py` | lease 缺失/漂移/锁不合法 → blocked | 只消费 lease JSON 字段 |
| WC reviewed sensitive source | `tests/test_reviewed_source.py` | linkage 不全 → `source review 要求完整 Controller linkage` | 四字段作不透明 identity |
| Snapshot Runner evidence | `tests/run.sh` 内 `test_initial_snapshot_*` 用例（`XDG_STATE_HOME` 重定向 fixture） | `tests/test_standalone_operations.sh` 证据缺失用例 + `tests/test_integration_boundaries.sh` 状态根重定位探针 | 只读三个 artifact 形状，不校验生产者版本 |
| Gitea repository bootstrap | `tests/test_repo_bootstrap.py`（stub HTTP） | `BLOCK_*` 决策、缺 URL、未确认 → exit 3 | 无默认主机、无版本常量 |
| 边界与 machine 合同本身 | `tests/test_integration_boundaries.sh`（marker 单点、扩散 ledger、矩阵↔代码一致、`--summary` 键集合与错误三元组） | 同一套件即回归门 | 不依赖任何 sibling 存在 |

## 禁止扩散清单（测试强制）

- `codex-worktree` 只允许出现在 retirement 的两个迁移 blocker 位点。
- `worktree-controller/v1` 布局字面量只允许出现在三个带 marker 的 adapter 文件。
- `publication-lease` 只允许出现在 integration publication 与 retirement 两个 lease adapter。
- `branch-retirement-plans`/`branch-retirement-receipts` 文件名只允许出现在 retirement plan verifier。
- `snapshot-runner/snapshots` 布局只允许出现在 snapshot adapter。
- `/api/v1` 只允许出现在 Gitea bootstrap adapter（及其测试 fixture）。
- Context Loader 无运行时耦合（GF 不调用它）。
