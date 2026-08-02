# Codex Git Finalizer

Codex Git Finalizer 是面向本地开发工作流的发布收尾脚本：只暂存明确列出的文件，创建提交，并在确认远端可快进后执行 non-force push。

当前版本：`0.4.0`

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
  --snapshot <snapshot-runner-id> \
  -- README.md
```

`--snapshot` 是 initial publish 的可选严格证据绑定。提供后，Finalizer 要求 Snapshot Runner
schema 2 / security epoch 4 的 `diff-audit` artifact 为完整 unborn initial 证据：
`truncated=false`、`evidence_gaps=[]`、`complete=true`、敏感扫描完整，并且 `--` 后每个显式
路径恰好由正文或 `generated_manifest` 证据覆盖。Finalizer 在 `git add` 前核对 worktree，
暂存后核对 index，创建 root commit 后再核对 HEAD tree；任一 size、SHA-256、executable、
路径集合或 snapshot artifact 漂移都会停止。未提供 `--snapshot` 时，既有 normal、initial 和
resume 工作流语义保持不变。

`codex-git-finalize-snapshot-verify.py` 是主入口同目录下的标准库-only companion，由主入口
固定定位和调用，不是独立发布命令；安装 Finalizer 时必须与主脚本一起部署。

若首次发布已经创建 root commit、但 push 未确认完成，可用完整 HEAD OID 恢复同一次发布：

```bash
codex-git-finalize \
  --resume-initial-publish <full-head-oid> \
  --remote origin \
  --repo /home/user/projects/example
```

### 有界结果摘要

normal、initial 和 resume 均可显式加入 `--summary`，以单行确定性 JSON 代替原有阶段输出：

```bash
codex-git-finalize \
  --summary \
  --repo /home/user/projects/example \
  --message "fix: describe the change" \
  -- path/to/file
```

未指定时，stdout、stderr 和退出码沿用原合同。摘要直接来自本次执行状态，不会为构造结果
重跑 Git 命令；成功、阻断和失败仍分别保留真实 `status`、`final_phase` 与 `exit_code`。
公共字段包括仓库、branch/upstream、请求路径数、commit、push、post-verify、warning、失败原因
和 `next_action`。`push.branch_refspec_only=true` 与 `follow_tags_requested=false` 描述本次调用
范围，不枚举远端已有 tag。

initial 的 `mode_result` 另含 initial publish、Snapshot artifact 校验和远端最终结论；resume
另含恢复起点、原提交复用、已完成阶段和是否仍需恢复。只有确有恢复入口时才输出顶层
`resume` 引用。warning 最多 5 条、每条最多 256 字符；失败原因最多 512 字符，省略数量通过
对应 `*_omitted*` 字段显式报告。摘要不包含 diff、文件正文、commit message、凭据或完整命令
日志。失败仍使用非零退出码；若序列化本身失败，fallback 会同时保留 Git 操作状态和原退出码。

PreToolUse bridge 仅将 `--summary` 作为官方无值参数原样转发；其调用授权、路径校验、停止
条件和输出上限不变。

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

仓库内 Python 入口均使用进程级 `-B`，避免工具执行或 Hook 检查向源码树写入
`__pycache__`。语法与行为验证使用 `just check`；不要以 `python -m py_compile` 作为本仓库的
常规检查，因为 `py_compile` 会显式生成 `.pyc`，即使解释器同时传入 `-B`。

## 版本维护

主脚本的 `VERSION`、README 当前版本、对应的 `CHANGELOG.md` 条目和必要测试必须在同一发布
准备批次中完成。`CHANGELOG.md` 是版本变化的权威记录，Gitea Release 说明从对应章节生成。
合入 `main` 不等于发布；正式发布仍需独立创建并推送 tag，Git Finalizer 本身不会创建或推送
tag。
