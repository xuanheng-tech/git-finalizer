# Tool / Skill lifecycle contract v2

## Owner and authority

`git-finalizer` 是最窄的共同 owner：它拥有唯一 active canonical `git-change-delivery` Skill
及其安装 helper，而 Context Loader 与 Snapshot Runner 仍只拥有各自实现。旧名称
`three-tool-git-workflow` 只保留一个 deprecated compatibility shim，不是第二个 authority。
这里的 release infrastructure 也不是 worktree lifecycle 编排工具。

每个工具仓库只保存自己的 `tool_cli_contract.json` 与 `tool_skill_manifest.json`。三个 manifest
都绑定同一个 canonical Skill payload；installed Skill 是同步副本，不能人工编辑。

canonical manifest 的 `tool_commit` 固定为 `@release`，因为被跟踪文件无法无循环地包含“含有
自身的 commit OID”。构建 bundle 时，Skill Sync 将它物化为 exact full HEAD OID；只有显式
`--allow-dirty-source` 的隔离验收 bundle 使用 `worktree:<HEAD>` 并标记
`source_state=dirty_test_only`，不得作为正式 release。

`canonical_skill_sha256` 是 canonical Skill 以下有序 payload 行的 SHA-256：

```text
<file-sha256><two spaces><relative-path>\n
```

范围为 `SKILL.md`、`quick_validate.py`、`unpublished_queue.py` 与四个 active references；
compatibility shim 的独立 SHA-256 由 ToolSkillManifest v2 绑定，并随 bundle/production pair 一起
验证和安装。

## Contract 升格规则

- 单个工具的 public CLI contract 演进（其 `tool_cli_contract.json` 的 `contract_version` 变化或
  surface 增删）：在同一提交内更新该工具自身 contract 文件、`toolchain_compatibility.json` 顶层
  `<tool>_contract_version` 与 `tools.<tool>.public_cli_contract_version`、以及拥有该 contract 的
  manifest 的 `public_cli_contract_sha256` 字节 pin；这不改变 `toolchain_contract_version`。
- canonical Skill payload 的任何编辑：重算 payload tree hash，并在同一提交内重绑 root manifest 与
  两个 mirror manifest 的 `canonical_skill_sha256`；三个 manifest 必须继续绑定同一 payload。
- `toolchain_contract_version` 只在跨工具绑定语义变化时升格——即 manifest schema、workflow Skill
  contract 语义或 Worktree Controller binding 规则本身变化。per-tool contract 版本推进不属于升格。
  升格必须在同一提交内原子完成：`tool_skill_sync.check_tool` 的受支持版本集合与 controller
  binding 分支、`toolchain_compatibility.json`、全部 manifest 的
  `compatible_toolchain_contract_version`、以及相关测试。当前受支持集合为 1–6；v4 起要求
  Worktree Controller contract 2，v5 起固定为 3，该数字是 adapter binding，独立于 WC CLI contract；v6 的 runtime requirements 另行校验
  实际 active Controller 的 CLI、capability 与 storage 合同。

## Commands

```bash
./tool-skill-sync --source-root /absolute/projects --source-repo git-finalizer=/absolute/worktree check --source-only git-finalizer
./tool-skill-sync status
./tool-skill-sync check <context-loader|snapshot-runner|git-finalizer>
./tool-skill-sync --install-root <isolated-root> install <tool> --allow-dirty-source
./tool-skill-sync --install-root <isolated-root> rollback <tool>
./tool-skill-sync install <tool> --activate-production
./tool-skill-sync rollback <tool> --activate-production
./tool-skill-sync upgrade <tool> [--dry-run]
```

`status` 只读取 canonical source、PATH 中 stable entries 与 active Skill，输出 `none`、
`binary_only`、`skill_only` 或 `incompatible`。`check` 对 schema、tool/version、entrypoints、Skill
和 CLI hashes、install target 及 toolchain contract version fail closed，不静默修复；它同时校验
canonical source contract 与已安装生产状态，`--source-only` 仅在无安装的构建环境跳过后者。

`upgrade` 是 `python_console_scripts` 工具的唯一生产升级入口，用于消除手工 `uv tool install`
造成的 CLI 中断与符号链接修复。它先读取现有 uv receipt，保留 `UV_TOOL_BIN_DIR`、index 列表和
`no-build`，再以 `uv tool install --force` 安装 manifest 声明的版本——`--reinstall` 会先卸载再
校验 entrypoint 冲突，可能让生产短暂没有可用 CLI，因此不使用。安装后校验全部 entrypoint、版本、
receipt 的 bin 目录/index/no-build、符号链接目标，以及 site-packages 对已发布 wheel `RECORD` 的
逐文件 SHA-256。任一校验失败即重新安装上一版本并 fail closed。

## ToolReleaseBundle and activation

每个 versioned bundle 包含：

```text
SKILL.md
references/ and Skill helpers
compatibility/three-tool-git-workflow/SKILL.md
executable/ or executable_identity.json
tool_cli_contract.json
tool_skill_manifest.json
release_manifest.json
```

Python package 工具绑定并验证现有 console-script identity，不复制 venv；Git Finalizer bundle
绑定正式脚本、所有 sidecar、`tool-skill-sync` 和 `tool-temp-dir` 实现。
`release_manifest.json` 记录 exact tool identity、binary/entry SHA、Skill SHA、CLI contract SHA 和
materialized manifest SHA，且不含 timestamp 或 developer-specific path。

`install` 在 managed root 内 stage → hash/schema verify → smoke → rename immutable bundle，随后先
保留旧 pair 为 `PREVIOUS`，再以 `os.replace` 原子切换 pair-level `CURRENT` symlink。任何 stage、
verify 或 smoke 失败都不会替换 `CURRENT`。`rollback` 先完整验证 `PREVIOUS`，再把 binary/identity
与 Skill 作为同一个 bundle 回切；禁止单独回退 Skill。

默认 `install` 仍只切换 managed bundle，便于隔离验收。显式 `--activate-production` 在 bundle
完整验证与 smoke 后，事务化安装 active Skill；对 Python package 工具只验证已安装 console
scripts 的版本并保留其稳定入口，对 Git Finalizer 同步安装 executable、snapshot companion、
Skill Sync。入口改名时必须已有验证过的旧 bundle；先验证新入口，再移除内容与旧 bundle 完全匹配的退役文件。安装前保存新旧 target 集合的并集，任一写入、验证或 `KeyboardInterrupt` 都恢复
旧 pair；成功后才更新 managed `CURRENT` 与 production `PREVIOUS`。`rollback
--activate-production` 同步恢复完整 pair，禁止只回退 Skill。

production Skill 写入受 trusted-state gate 约束，与 `deploy.py install` 共用同一判定语义
（payload tree digest 公式与 accepted registry 由测试锁定一致）：任一激活写入前，installed
payload tree 必须等于本次 bundle、等于 `RELEASED_CANONICAL_SKILL_SHA256` 中已发布 canonical
谱系、等于 CURRENT pointer bundle（受管回切/重装），或整个 Skill 尚不存在；否则以
`unknown content drift` fail closed，逐文件输出 live digest，backup 也不会创建。payload
不完整（部分 managed 文件缺失）同样拒绝。`rollback --activate-production` 在恢复前对
CURRENT pair 做逐 target 验证，同样拒绝已漂移的 production 状态。人工漂移必须先捕获分类，
再显式处置，任何生产入口都不静默覆盖。

## Unified release gate

三个 owner 的发布按以下 gate 执行；每步失败停止：

```text
CLI contract check
→ ToolSkillManifest / toolchain compatibility check
→ project tests, lint, types and shell checks as applicable
→ code + Skill + contracts committed
→ remote publish and exact OID verification
→ versioned ToolReleaseBundle
→ Skill Sync managed install / explicit production activation
→ installed smoke
→ release receipt
```

单仓库 CI 运行本仓库的 contract tests；需要三个 checkout 的 bundle/compatibility gate 在共同
release workspace 运行。现有生产发布脚本不被隐式改写。

## Installed workflow and instruction check

`tool-skill-sync doctor --repo <actual-checkout> --summary` is a read-only deployment gate.
Exit 0 means the selected three-tool source manifests, actual binaries/sidecars, shared Skill,
reviewed external Controller requirements and instruction discovery budget pass. Exit 1 reports
component failures; it never installs, activates, commits, changes instructions or mutates a checkout.
Use the full JSON only to investigate a named component. `--previous-fingerprint <sha256>` reports
whether observed content changed; it cannot attest what a running model has loaded.

The three managed tools keep their independent software/CLI versions. Toolchain contract v6 adds
`runtime_requirements.worktree-controller` with separate accepted CLI contracts, storage layouts
and required capability versions. The older `worktree_controller_contract_version` is the
integration-adapter contract, not the WC CLI contract. Doctor observes the actual active pinned
runtime and frozen contract, validates installed RECORD content, and reports its source checkout
as a candidate separately. It does not activate a source HEAD, infer compatibility from software
version alone, or treat equal CLI numbers as sufficient.

Source-file tools verify all deployed executable/sidecar bytes, including Skill Sync. Python tools
verify installed wheel RECORD content in addition to entrypoint versions. WC remains owned by its
official stage/rollout/activate flow. A simultaneous WC development line can change its source
without changing the active runtime; only dependent incompatible operations are blocked.

Doctor resolves global override precedence, repository root-to-cwd instruction scope, real paths,
shared personal Skills and optional Claude bridge. It reports instruction byte limits and uncommitted
instruction changes in registered checkouts (bounded to 64). Such pending work is not integrated
main. A differing checkout AGENTS is a review signal, not permission to overwrite branch-specific
rules. No content is copied into other checkouts. The global task-start/resume rule invokes this
gate and rereads changed files; no watcher, daemon, hook replacement or background upgrade is added.

The v6 source gate validates the independent Controller requirements before any install. The doctor
also binds the installed lifecycle Skill to the active Controller commit and refuses a mixed
observation if the active pointer changes during its probe. Source checkout edits never select a new
runtime. A linked checkout fingerprint includes canonical governance references, so a canonical update
requires a reread even while the branch's own instructions remain unchanged.

## Exact released package metadata

A concurrently edited Python tool checkout need not be reset/stashed or copied to install a known
release. `--source-ref <tool>=<full-commit-oid>` reads only `pyproject.toml` and
`tool_cli_contract.json` directly from that local commit. The tool must be a Python console-script
package; Git Finalizer's source-file payload still requires a clean committed source checkout.
The pinned metadata must match the selected manifest's version, CLI hash and entrypoints.
No Git checkout, ref, index or working file changes. Bundle provenance records the selected OID;
the owner Skill source must still be clean. Arbitrary dirty sources remain test-only.

Example after the exact released OID has been reviewed:

`tool-skill-sync --source-ref snapshot-runner=<full-commit-oid> upgrade snapshot-runner --dry-run`

Use the same pin for actual upgrade and managed install/activation. Upgrade now validates the source
contract before invoking the package installer. Finish with installed smoke and doctor. Do not treat
a package install as instruction integration, or advance a manifest solely to silence a failing gate.
