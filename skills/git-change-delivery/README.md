# Versioned Git change delivery Skill

唯一 active canonical Skill source 位于：

```text
/home/hsd/projects/git-finalizer/skills/git-change-delivery/
```

live 目标位于：

```text
/home/hsd/.agents/skills/git-change-delivery/
```

旧名称 source/live 的 `three-tool-git-workflow/SKILL.md` 只是 deprecated compatibility shim；
它不携带 references 或 helper。`README.md`、reference、shim 或 installed copy 都不是第二个
authority。只读检查 canonical source/live、shim、SHA-256、权限和未知 live 文件：

```bash
just skill-check
```

显式安装并在安装后重复核验：

```bash
just skill-install
```

安装管理 canonical 的 `SKILL.md`、`quick_validate.py`、`unpublished_queue.py` 和 `references/`
下四个合同文件，并验证旧名称仅安装 shim `SKILL.md`；发现未知 live 文件时会停止，不会静默
删除。`deploy.py`、两个 `test_*.py` 和本 README 仅属于版本化恢复/测试资产，不安装到 live
目录。

四态发布决策保持 `explicit-only`，不会启用默认自动 commit/push。该仓库只拥有这一 change
delivery 集成资产，不拥有 Context Loader 或 Snapshot Runner 的实现。Completed-but-Unpublished
Queue 只持久化用户私有 control-plane metadata，不执行 Git finalization 或 worktree 清理。

跨三工具的 binary/entry identity、Skill payload、CLI contract 和兼容性由仓库根目录的
`codex-skill-sync` 验证。旧的 `deploy.py` 保留为已安装 Skill 的兼容检查/显式恢复入口；新发布
流程以完整 ToolReleaseBundle 的 pair-level `CURRENT` 原子切换为 authority，不得只更新 Skill。
