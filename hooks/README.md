# Codex Git Finalizer PreToolUse bridge

本目录是 Git Finalizer PreToolUse bridge 的版本化权威源。bridge 校验并执行的是
Git Finalizer CLI 契约，因此 allowlist、对应测试和部署入口与 Finalizer 源码放在同一仓库。
它不管理其他 Codex Hook，也不改变 Git Finalizer 的参数或安全语义。
bridge 只放行固定绝对路径的 normal、`--mode commit-only`、`--mode verify-only`、
`--initial-publish --remote <name>` 与
`--initial-branch-publish --remote <name> --remote-branch <name>` 调用，以及不带文件列表的
`--publish-existing-branch <full-head-oid> --remote <name> --remote-branch <name> --repo <path>`、
`--resume-publish <full-head-oid> --repo <path>` 和兼容的
`--resume-initial-publish <full-head-oid> --remote <name> --repo <path>`，以及显式
`--retire-remote-branch <branch> --remote <name> --integrated-into <branch>` 与
`--expected-remote-oid <full-oid> --repo <path>`。retirement 可携带完整 CI evidence、`--dry-run`
和 `--summary`，但不接受文件范围、message、snapshot 或其他 mode。initial 模式可额外携带
一个严格的 `--snapshot <64-lowercase-hex>`。带显式文件范围的模式可原样转发可重复的
`--allow-test-fixture <path>` 和 `--allow-large-binary <path>`；bridge 只验证参数 arity，路径语义
仍由 Finalizer 的正式规则判定。通用 resume 从 configured upstream 推导目标并禁止
remote；root resume 要求 remote。两者均禁止 message、dry-run、snapshot、remote-branch、其他
mode 和 `--` 文件范围；publish-existing-branch 同样禁止 message、dry-run、snapshot、其他
mode 和文件范围，但要求 explicit remote 与 remote-branch。其他未知参数继续拒绝。initial-branch、commit-only 与 verify-only 模式
禁止 snapshot；normal、commit-only 与 verify-only 模式禁止 remote 和 remote-branch；
verify-only 还禁止 message 和 dry-run。
existing-history 首次发布及对应 resume 可显式携带一次
`--fixture-exceptions <absolute-json-file>`；bridge 只转发该精确文件参数，仓库、路径、
blob/hash、detector 与 reason 的严格匹配由 Finalizer 执行。这两个接口继续拒绝原有
path-only `--allow-test-fixture`；其他模式不接受新的例外文件。
`tool_cli_contract.json` 的 `host_bridge.exposed_options` 是 bridge-exposed public options 的
机器可读 authority；版本化测试要求 bridge 的 arity/repeatability schema 与其精确一致，未知
mutation options 继续 fail closed。ToolReleaseBundle 校验也拒绝 CLI contract 与 bundled bridge
不匹配的组合。
bridge 将严格解析并规范化的绝对 `--repo` 作为 Finalizer 子进程 cwd；PreToolUse 的会话 cwd
不是目标仓库权限来源。bridge 同时支持两条明确执行边界：旧式
`permission_mode=default` 仍要求 transcript 中的原命令、`workdir=<repo>` 与
`sandbox_permissions=require_escalated` 精确匹配；Codex 0.149.1 在
`approval_policy=never` 的当前用户 direct execution 中产生的
`permission_mode=bypassPermissions` 只允许非 root 进程，且 transcript 不得伪造 sandbox
escalation。`acceptEdits`、`plan`、`dontAsk`、未知值和未知结构继续 fail closed。两条路径均继续
执行同一固定入口、参数、显式路径、allow rule 与 Finalizer 自身安全门；任何冲突均拒绝。
固定入口前可声明且只能成对声明
`NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost`。bridge 将这两个固定值
仅传给本次 Finalizer 子进程，并核对 transcript 中包含前缀的完整原命令；不会修改父进程
或全局代理。其他环境变量、单独声明、重复声明、通配符和非 loopback 目标均拒绝，
`env`/shell wrapper 仍拒绝。Finalizer 的其他校验和发布门槛不变。
命令仅把 Finalizer 名称或路径作为保守只读检查器的数据，或以固定入口精确查询 `--help`、
`--version` 时，bridge 不作 allow/deny 决定，而是交回原生 sandbox、approval、execpolicy 和
其他 Hook。shell wrapper、解释器、`find -exec`、可执行预处理器、带其他参数的元数据查询，
以及复合命令中的实际 Finalizer 调用仍按固定绝对路径直接调用合同拒绝。
精确调用 active 或 versioned `unpublished_queue.py` lifecycle helper 时，文件 scope 中出现
`codex-git-finalize` 仅作为路径数据，bridge 不作 allow/deny 决定并交回正常 sandbox/execpolicy；
该例外不适用于 Python `-c`、其他脚本、shell wrapper 或复合 Finalizer 执行。
固定 Controller 入口 `/home/hsd/.local/bin/codex-worktree` 的 `acquire`、`adopt`、
`review` 中，Finalizer 文件名仅作为 `--scope` / `--add-scope` 的参数时也交回原生控制。
这不授权 Controller mutation；其正式 lifecycle / writer / scope 检查仍须通过。
delegated execution、wrapper、其他参数位置和复合 Finalizer 执行继续拒绝。

## 管理范围

部署入口只管理以下两个目标：

```text
~/.codex/hooks/codex_git_finalize_bridge.py
~/.codex/hooks/test_codex_git_finalize_bridge.py
```

只读核验：

```bash
just hook-check
```

显式安装或恢复：

```bash
just hook-install
```

安装器只替换内容发生漂移或缺失的受管文件。每个文件先写入目标目录中的临时文件，
再原子替换并核验 SHA-256；已有普通文件保留原权限。缺失文件默认使用 bridge `0600`、
测试 `0644`。安装不会自动运行，也不会修改 `~/.codex/hooks.json`、其他 Hook、规则文件或
任何后台服务。

运行版本化测试：

```bash
python3 -B -m unittest discover -s hooks -p 'test_*.py' -v
```

Codex 每次 Hook 调用都会启动新的 Python 进程；安装后的下一次调用直接使用新文件，
无需 reload。

## `hooks.json` 最小配置片段

本机调查未发现 `~/.codex/hooks.json` 的版本化模板或生成来源。完整文件可能包含其他
私有 Hook，因此本仓库只记录 bridge 所需的最小 `PreToolUse` 条目，且安装器不会写入它：

```json
{
  "matcher": "^Bash$",
  "hooks": [
    {
      "type": "command",
      "command": "/usr/bin/python3 -B /home/hsd/.codex/hooks/codex_git_finalize_bridge.py",
      "timeout": 330,
      "statusMessage": "Validating Git Finalizer escalation"
    }
  ]
}
```

该对象应位于 `hooks.PreToolUse` 数组中。恢复配置时只人工核对或加入这一条，不要用本片段
覆盖完整 `hooks.json`。
