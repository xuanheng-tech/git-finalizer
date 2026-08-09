# Versioned Three-tool Git workflow Skill

权威源位于：

```text
/home/hsd/projects/git-finalizer/skills/three-tool-git-workflow/
```

live 目标位于：

```text
/home/hsd/.agents/skills/three-tool-git-workflow/
```

只读检查 source/live 文件清单、SHA-256、权限和未知 live 文件：

```bash
just skill-check
```

显式安装并在安装后重复核验：

```bash
just skill-install
```

安装只管理 `SKILL.md`、`quick_validate.py`、`unpublished_queue.py` 和 `references/` 下四个合同
文件；发现未知 live 文件时会停止，不会静默删除。`deploy.py`、两个 `test_*.py` 和本 README
仅属于版本化恢复/测试资产，不安装到 live 目录。

四态发布决策保持 `explicit-only`，不会启用默认自动 commit/push。该仓库只拥有这一三工具
工作流集成资产，不拥有 Context Loader 或 Snapshot Runner 的实现。Completed-but-Unpublished
Queue 只持久化用户私有 control-plane metadata，不执行 Git finalization 或 worktree 清理。
