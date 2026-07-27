# Codex Git Finalizer PreToolUse bridge

本目录是 Git Finalizer PreToolUse bridge 的版本化权威源。bridge 校验并执行的是
Git Finalizer CLI 契约，因此 allowlist、对应测试和部署入口与 Finalizer 源码放在同一仓库。
它不管理其他 Codex Hook，也不改变 Git Finalizer 的参数或安全语义。
bridge 只放行固定绝对路径的 normal 与 `--initial-publish --remote <name>` 调用；
initial 模式必须提供 remote，normal 模式禁止 remote，其他未知参数继续拒绝。

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
      "command": "/usr/bin/python3 /home/hsd/.codex/hooks/codex_git_finalize_bridge.py",
      "timeout": 330,
      "statusMessage": "Validating Git Finalizer escalation"
    }
  ]
}
```

该对象应位于 `hooks.PreToolUse` 数组中。恢复配置时只人工核对或加入这一条，不要用本片段
覆盖完整 `hooks.json`。
