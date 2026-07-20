# Codex Git Finalizer

Codex Git Finalizer 是面向本地开发工作流的发布收尾脚本：只暂存明确列出的文件，创建提交，并在确认远端可快进后执行 non-force push。

当前版本：`0.2.0`

## 使用方式

正常模式用于已有 upstream 的成熟仓库：

```bash
codex-git-finalize \
  --repo /home/user/projects/example \
  --message "fix: describe the change" \
  -- path/to/file
```

首次发布模式只接受 unborn 分支和完全空的远端，并创建首个非空 root commit：

```bash
codex-git-finalize \
  --initial-publish \
  --remote origin \
  --repo /home/user/projects/example \
  --message "feat: initialize project" \
  -- README.md
```

若首次发布已经创建 root commit、但 push 未确认完成，可用完整 HEAD OID 恢复同一次发布：

```bash
codex-git-finalize \
  --resume-initial-publish <full-head-oid> \
  --remote origin \
  --repo /home/user/projects/example
```

## 工作流边界

正常开发流程是先完成修改、测试和审查，再调用正常模式提交推送。首次发布仅适用于 unborn 本地仓库和完全空远端；恢复模式仅适用于干净、非空的单 root HEAD，不会再次创建提交。

文件范围必须在 `--` 后逐项显式给出；脚本不会使用 `git add .` 或 `git add -A`。测试夹具豁免只能通过 `--allow-test-fixture <exact-path>` 精确指定。

工具不会自动 pull、merge 或 rebase，不会 amend，不会 force push，也不会创建远端仓库。

运行完整集成测试：

```bash
bash tests/run.sh
```
