# Versioned Three-tool Git workflow Skill

唯一 active canonical Skill source 位于：

```text
/home/hsd/projects/git-finalizer/skills/three-tool-git-workflow/
```

live 目标位于：

```text
/home/hsd/.agents/skills/three-tool-git-workflow/
```

`README.md`、reference 或 installed copy 都不是第二个 authority。只读检查 source/live 文件
清单、SHA-256、权限和未知 live 文件：

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

跨三工具的 binary/entry identity、Skill payload、CLI contract 和兼容性由仓库根目录的
`codex-skill-sync` 验证。旧的 `deploy.py` 保留为已安装 Skill 的兼容检查/显式恢复入口；新发布
流程以完整 ToolReleaseBundle 的 pair-level `CURRENT` 原子切换为 authority，不得只更新 Skill。
