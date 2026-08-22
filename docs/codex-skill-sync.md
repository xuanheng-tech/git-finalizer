# Tool / Skill lifecycle contract v1

## Owner and authority

`git-finalizer` 是最窄的共同 owner：它已经拥有唯一 active canonical
`three-tool-git-workflow` Skill 及其安装 helper，而 Context Loader 与 Snapshot Runner 仍只拥有
各自实现。这里新增的是 release infrastructure，不是第 4 个编排工具。

每个工具仓库只保存自己的 `tool_cli_contract.json` 与 `tool_skill_manifest.json`。三个 manifest
都绑定同一个 canonical Skill payload；installed Skill 是同步副本，不能人工编辑。

canonical manifest 的 `tool_commit` 固定为 `@release`，因为被跟踪文件无法无循环地包含“含有
自身的 commit OID”。构建 bundle 时，Skill Sync 将它物化为 exact full HEAD OID；只有显式
`--allow-dirty-source` 的隔离验收 bundle 使用 `worktree:<HEAD>` 并标记
`source_state=dirty_test_only`，不得作为正式 release。

`canonical_skill_sha256` 是以下有序 payload 行的 SHA-256：

```text
<file-sha256><two spaces><relative-path>\n
```

范围为 `SKILL.md`、`quick_validate.py`、`unpublished_queue.py` 与四个 active references。

## Commands

```bash
./codex-skill-sync status
./codex-skill-sync check <context-loader|snapshot-runner|git-finalizer>
./codex-skill-sync --install-root <isolated-root> install <tool> --allow-dirty-source
./codex-skill-sync --install-root <isolated-root> rollback <tool>
./codex-skill-sync install <tool> --activate-production
./codex-skill-sync rollback <tool> --activate-production
```

`status` 只读取 canonical source、PATH 中 stable entries 与 active Skill，输出 `none`、
`binary_only`、`skill_only` 或 `incompatible`。`check` 对 schema、tool/version、entrypoints、Skill
和 CLI hashes、install target 及 toolchain contract version fail closed，不静默修复。

## ToolReleaseBundle and activation

每个 versioned bundle 包含：

```text
SKILL.md
references/ and Skill helpers
executable/ or executable_identity.json
tool_cli_contract.json
tool_skill_manifest.json
release_manifest.json
```

Python package 工具绑定并验证现有 console-script identity，不复制 venv；Git Finalizer bundle
绑定正式脚本、snapshot companion、`codex-skill-sync` 实现与 PreToolUse bridge source。
`release_manifest.json` 记录 exact tool identity、binary/entry SHA、Skill SHA、CLI contract SHA 和
materialized manifest SHA，且不含 timestamp 或 developer-specific path。

`install` 在 managed root 内 stage → hash/schema verify → smoke → rename immutable bundle，随后先
保留旧 pair 为 `PREVIOUS`，再以 `os.replace` 原子切换 pair-level `CURRENT` symlink。任何 stage、
verify 或 smoke 失败都不会替换 `CURRENT`。`rollback` 先完整验证 `PREVIOUS`，再把 binary/identity
与 Skill 作为同一个 bundle 回切；禁止单独回退 Skill。

默认 `install` 仍只切换 managed bundle，便于隔离验收。显式 `--activate-production` 在 bundle
完整验证与 smoke 后，事务化安装 active Skill；对 Python package 工具只验证已安装 console
scripts 的版本并保留其稳定入口，对 Git Finalizer 同步安装 executable、snapshot companion、
Skill Sync 和 bridge。安装前保存整组 active pair，任一写入、验证或 `KeyboardInterrupt` 都恢复
旧 pair；成功后才更新 managed `CURRENT` 与 production `PREVIOUS`。`rollback
--activate-production` 同步恢复完整 pair，禁止只回退 Skill。

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
