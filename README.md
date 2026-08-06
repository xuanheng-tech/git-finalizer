# Codex Git Finalizer

Codex Git Finalizer 是面向本地开发工作流的发布收尾脚本：只暂存明确列出的文件，创建提交，并在确认远端可快进后执行 non-force push。

当前版本：`0.6.0`

## 使用方式

正常模式用于已有 upstream 的成熟仓库：

```bash
codex-git-finalize \
  --repo /home/user/projects/example \
  --message "fix: describe the change" \
  -- path/to/file
```

只创建本地 commit、明确跳过 push 和全部远端验证时，使用 `commit-only`：

```bash
codex-git-finalize \
  --mode commit-only \
  --repo /home/user/projects/example \
  --message "fix: describe the change" \
  -- path/to/file
```

该模式保留 normal 模式的显式路径、staged scope、敏感内容、大文件、冲突、attached branch
和非-unborn HEAD 检查，但不要求 upstream，不执行 fetch、ls-remote、push 或 post-push
verification，也不修改 remote、tag 或 Git 配置。成功输出会报告 mode、新 commit hash、因模式
跳过的 push/远端验证以及最终工作区状态。

只读验证当前显式文件范围时，使用 `verify-only`；该模式不接受或要求 `--message`：

```bash
codex-git-finalize \
  --mode verify-only \
  --repo /home/user/projects/example \
  -- path/to/file
```

verify-only 要求已有 commit 的 attached branch，但不要求 upstream。它复用路径规范化、已有
staged scope、冲突、敏感路径与内容、大文件及例外范围检查，并以只读方式确认 HEAD 到当前
worktree 的候选 diff 非空、ignored 文件不会进入候选范围且 whitespace 检查通过。它不运行
`git add`、commit hooks、`git commit` 或任何远端命令，也不执行 config、tag 或 ref 写操作；
因此不验证 commit message、author/签名、hook 结果、index 实际写入能力、远端状态或
fast-forward 条件。
`--dry-run` 与 verify-only 不兼容：verify-only 本身即为严格只读验证，失败使用非零退出码。

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

已有正常历史的本地功能分支首次发布到尚不存在的同名远端分支时，使用独立模式：

```bash
codex-git-finalize \
  --initial-branch-publish \
  --remote origin \
  --remote-branch feat/example \
  --repo /home/user/projects/example \
  --message "feat: publish example branch" \
  -- path/to/file
```

该模式要求当前 symbolic local branch 与 `--remote-branch` 完全一致，拒绝
`main`、`master`、`trunk` 和 `production`，并在暂存前确认显式远端目标不存在。当前 upstream
可以缺失或错误指向其他分支；它不参与 push 目标选择，成功的显式
`HEAD:refs/heads/<remote-branch>` push 会通过 `--set-upstream` 将其替换为正确目标。commit 前后
均复查目标仍不存在；post-verify 要求远端目标与 HEAD 一致、upstream 精确对齐、ahead/behind
为 `0/0`、index 为空、显式路径干净，并确认所有远端非目标 refs（包括 main 与 tags）和本地
tags 均未变化。

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

若普通 attached branch 已有 configured upstream，工作区与 index 均 clean，且一个或多个本地
commit 尚未发布，可复用完整 HEAD OID 发布 `upstream..HEAD`，而不会再创建 commit：

```bash
codex-git-finalize \
  --resume-publish <full-head-oid> \
  --repo /home/user/projects/example
```

`--resume-publish` 严格从当前 branch 的唯一 configured upstream 推导 push 目标，不接受
`--remote`、message、文件列表或其他 mode。它要求 ahead 至少为 1、behind 为 0，并对待发布
range 中每一个 commit（包括最终 tree 已删除的中间内容）执行 whitespace、敏感路径与内容、
ignored 文件、大二进制及异常对象检查；随后仅执行显式 branch refspec 的 non-force、
`--no-follow-tags` push。post-verify 要求远端目标等于原 HEAD、ahead/behind 为 `0/0`、工作区与
index 仍 clean，并确认 HEAD、本地 branch、tags 和 Git 配置均未改变。旧的
`--resume-initial-publish` 继续只处理单 root commit 和 initial-publish 的空远端恢复状态。

### 有界结果摘要

normal、commit-only、verify-only、initial、initial-branch 和 resume 均可显式加入
`--summary`，以单行确定性 JSON 代替原有阶段输出：

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
和 `next_action`。只有实际执行使用显式 branch refspec 和 `--no-follow-tags` 的 push 时，
`push.branch_refspec_only` 与 `follow_tags_requested` 才分别为 `true` 和 `false`；未执行 push
时两者为 `null`。这些字段不枚举远端已有 tag。

initial 的 `mode_result` 另含 initial publish、Snapshot artifact 校验和远端最终结论；
initial-branch 另含首次分支发布状态、远端最终结论和 post-verify；resume 另含实际 interface 与
publish kind、恢复起点、原提交复用、发布前 ahead/behind、commit 数量与 range、push 目标、最终
HEAD/remote/worktree、已完成阶段和是否仍需恢复；commit-only 另含跳过的远端验证、post-verify
和最终工作区 `clean|dirty` 状态，其 `push.result` 为 `skipped_by_commit_only`。只有确有恢复入口时才输出顶层
`resume` 引用。verify-only 的 `mode_result` 另含本地验证结果、commit/push/远端验证跳过原因，
以及验证前后 HEAD、index 和 worktree 不变的结论；其 `push.result` 为
`skipped_by_verify_only`。warning 最多
5 条、每条最多 256 字符；失败原因最多 512 字符，省略数量通过对应 `*_omitted*` 字段显式
报告。摘要不包含 diff、文件正文、commit message、凭据或完整命令日志。失败仍使用非零退出码；
若序列化本身失败，fallback 会同时保留 Git 操作状态和原退出码。

PreToolUse bridge 将 `--summary`、严格的 `--mode commit-only|verify-only`、通用
`--resume-publish` 及兼容的 root resume 入口原样转发；其调用授权、路径校验、停止条件和输出
上限不变。

## Codex PreToolUse bridge

Git Finalizer Hook 的版本化源、测试、只读漂移检查和显式恢复入口见
[`hooks/README.md`](hooks/README.md)。

## 工作流边界

正常开发流程是先完成修改、测试和审查，再调用正常模式提交推送。首次发布仅适用于 unborn
本地仓库和完全空远端；已有历史功能分支首次发布必须使用 initial-branch 模式；恢复模式仅适用于
干净状态且不会再次创建提交：root 恢复入口处理 initial-publish 的单 root commit，通用恢复入口
处理 configured upstream 之上的一个或多个既有 commit。commit-only 仅在已有 commit 的
attached branch 上创建本地提交，不接触远端；verify-only 在相同本地 branch 边界内只验证显式
候选范围，不修改 HEAD、index、worktree、refs、tags 或配置，也不接触远端。

空远端经 `git clone` 创建的 unborn 分支可能已有精确匹配但尚不可解析的 tracking target；
initial 与 root resume 接受该状态并由成功 push 自然完成 upstream。正常模式和通用 resume 仍要求
upstream commit 已可解析。Upstream target 配置与 resolved upstream commit 是不同事实。

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
