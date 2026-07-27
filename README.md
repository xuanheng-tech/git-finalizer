# Codex Git Finalizer

Codex Git Finalizer 是面向本地开发工作流的发布收尾脚本：只暂存明确列出的文件，创建提交，并在确认远端可快进后执行 non-force push。

当前版本：`0.2.4`

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

## Codex PreToolUse bridge

Git Finalizer Hook 的版本化源、测试、只读漂移检查和显式恢复入口见
[`hooks/README.md`](hooks/README.md)。

## 工作流边界

正常开发流程是先完成修改、测试和审查，再调用正常模式提交推送。首次发布仅适用于 unborn 本地仓库和完全空远端；恢复模式仅适用于干净、非空的单 root HEAD，不会再次创建提交。

空远端经 `git clone` 创建的 unborn 分支可能已有精确匹配但尚不可解析的 tracking target；initial 与 resume 模式接受该状态并由成功 push 自然完成 upstream。正常模式仍要求 upstream commit 已可解析。Upstream target 配置与 resolved upstream commit 是不同事实。

文件范围必须在 `--` 后逐项显式给出；脚本不会使用 `git add .` 或 `git add -A`。测试夹具豁免只能通过 `--allow-test-fixture <exact-path>` 精确指定。

若显式路径已经作为删除暂存，而工作树中的同名本地副本已被忽略，工具会保留该 staged deletion，不会重新添加或提交本地副本。

默认拒绝超过 5 MiB 的明显二进制文件。确需保留原始图片等文件时，可重复使用 `--allow-large-binary <exact-path>`，但该路径必须同时出现在 `--` 后的显式文件范围内，必须是普通文件，工作树与 staged blob 都必须大于 5 MiB 且不超过 25 MiB。该参数只放行精确文件的大小检查，不放行敏感路径、credential / secret 内容检查、staged scope、远端状态或快进检查；超过 25 MiB 仍会拒绝。

例如：

```bash
codex-git-finalize \
  --repo /home/user/projects/example \
  --message "docs: archive original photo" \
  --allow-large-binary Attachments/photo.jpg \
  -- Attachments/photo.jpg
```

工具不会自动 pull、merge 或 rebase，不会 amend，不会 force push，也不会创建远端仓库。

运行完整检查：

```bash
just check
```
