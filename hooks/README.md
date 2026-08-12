# Codex Git Finalizer PreToolUse bridge

本目录是 Git Finalizer PreToolUse bridge 的版本化权威源。bridge 校验并执行的是
Git Finalizer CLI 契约，因此 allowlist、对应测试和部署入口与 Finalizer 源码放在同一仓库。
它不管理其他 Codex Hook，也不改变 Git Finalizer 的参数或安全语义。
bridge 只放行固定绝对路径的 normal、`--mode commit-only`、`--mode verify-only`、
`--initial-publish --remote <name>` 与
`--initial-branch-publish --remote <name> --remote-branch <name>` 调用，以及不带文件列表的
`--resume-publish <full-head-oid> --repo <path>` 和兼容的
`--resume-initial-publish <full-head-oid> --remote <name> --repo <path>`。initial 模式可额外携带
一个严格的 `--snapshot <64-lowercase-hex>`。通用 resume 从 configured upstream 推导目标并禁止
remote；root resume 要求 remote。两者均禁止 message、dry-run、snapshot、remote-branch、其他
mode 和 `--` 文件范围；其他未知参数继续拒绝。initial-branch、commit-only 与 verify-only 模式
禁止 snapshot；normal、commit-only 与 verify-only 模式禁止 remote 和 remote-branch；
verify-only 还禁止 message 和 dry-run。
bridge 将严格解析并规范化的绝对 `--repo` 作为 Finalizer 子进程 cwd；PreToolUse 的会话 cwd
不是目标仓库权限来源。若本次调用已出现在 transcript，bridge 还会要求其中的原命令、
`workdir=<repo>` 和 `sandbox_permissions=require_escalated` 精确匹配；任何冲突均拒绝。
命令仅把 Finalizer 名称或路径作为保守只读检查器的数据，或以固定入口精确查询 `--help`、
`--version` 时，bridge 不作 allow/deny 决定，而是交回原生 sandbox、approval、execpolicy 和
其他 Hook。shell wrapper、解释器、`find -exec`、可执行预处理器、带其他参数的元数据查询，
以及复合命令中的实际 Finalizer 调用仍按固定绝对路径直接调用合同拒绝。

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
