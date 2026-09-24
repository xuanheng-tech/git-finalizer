# Snapshot Runner

## 共同边界

唯一公共入口 `/home/hsd/bin/snapshot-runner` 必须以绝对路径直接调用（五个子命令：四个证据收集命令加一个 `read`），并通过原生权限机制使用 `require_escalated`；不得使用 PATH 简写、`bash -lc`、`sh -c` 或其他 wrapper。它们收集目标仓库的只读证据，不对目标仓库执行 Git 写入。

Runner 会在目标仓库之外创建本地状态和内容寻址产物。默认位置为 `~/.local/state/snapshot-runner/snapshots/<snapshot-id>/`；若设置了绝对 `XDG_STATE_HOME`，则使用其下的 `snapshot-runner/snapshots/`。状态目录必须已存在、由当前用户拥有且权限为 `0700`。

当前正式合同为 Snapshot Runner 2.3.1（public CLI contract 3）。四个收集子命令默认显式添加 `--summary`，无需每个任务重复检查版本；`read` 不支持 `--summary`。若实际命令不支持这些参数，报告安装或版本漂移并退回原有默认输出，不得伪造摘要结果。

`--summary` 成功时输出单行确定性 JSON，提供 `command`、`status`、`next_action`、`snapshot_id`/`artifact`、`repository`、`head`、`scope`、命令相关 `result` 计数、`warnings`/`warnings_omitted`、`truncated` 和 `evidence_gap`。摘要由同一正式 artifact 派生，只用于首轮决策；`snapshot.json` 仍是权威的有界快照证据。完整产物目录还包含：

- `preview.txt` 是人工预览摘要，不是完整审查证据。
- `snapshot.json` 是完整的有界快照证据；仍可能记录截断、拒绝项或 evidence gaps。
- `meta.json` 描述快照和摘要的元数据与摘要值。

Runner 处于 prepare-only 模式，不自动调用模型。自动脱敏只能覆盖配置的模式，不能证明任意 secret 不存在；任何手工上传前必须先审查 `preview.txt`，并按需检查 `snapshot.json`。

## 摘要优先与按需展开

仅当命令成功、`status` 和 `next_action` 符合继续条件、`truncated=false`、`evidence_gap=false`、没有影响当前决策的 warning，且任务不需要检查 diff、正文、日志或 file-context 具体内容时，摘要可作为该阶段的首轮充分证据；不得仅因 artifact 存在就机械打开。

命令失败或退出码异常，`evidence_gap=true`、`truncated=true`，`status` 或 `next_action` 表明阻断、部分完成或需要审查，warning 影响范围、提交、发布或安全判断，`warnings_omitted` 可能改变决策，任务需要具体代码、diff、删除、测试失败或 file-context，用户要求详细证据，或既有流程要求人工审查具体内容时，不得只依赖摘要。使用摘要中的 `artifact` 引用，通过 `read` 或直接读取当前判断所需的章节或文件；artifact 本身仍有截断或缺口时执行现有聚焦取证流程，打开 artifact 不等于自动消除 evidence gap。

## 命令选择

### `snapshot-runner repo-status`

```bash
/home/hsd/bin/snapshot-runner repo-status --repo <absolute-repo> --summary
```

用于在改动很多、来源不明或状态复杂时建立仓库状态快照。摘要通常足以快速确认仓库、工作树和 upstream 状态；无异常时无需打开 artifact。小而明确的局部任务不必机械调用。

### `snapshot-runner diff-audit`

```bash
/home/hsd/bin/snapshot-runner diff-audit --repo <absolute-repo> --summary
```

用于审查当前 worktree 的 staged、unstaged 和 untracked 变更。摘要可先判断范围、计数和 gap；涉及代码语义、精确 diff、删除或 file-context 时仍须读取正式证据。对 unborn HEAD，使用空树基线。发布前通常用它证明当前 diff 与授权范围一致；它不替代实际测试，也不自行给出代码正确性结论。

### `snapshot-runner branch-review`

```bash
/home/hsd/bin/snapshot-runner branch-review --repo <absolute-repo> <local-base-branch-or-tag> --summary
```

用于相对一个明确的本地 base branch 或 tag 审查当前已提交分支。摘要可先筛查 commit、diff 和 delete 计数；需要审查实际分支内容时不得省略 artifact。base 必须唯一解析为现有本地 branch 或 tag；Runner 以唯一 merge base 到目标 HEAD 的范围收集 commits、diff 和相关上下文。unborn 目标分支不适用。不要用猜测的默认分支、远端名称或未验证 ref 代替显式 base。

### `snapshot-runner test-triage`

```bash
/home/hsd/bin/snapshot-runner test-triage --repo <absolute-repo> <repo-relative-log-path> --summary
```

仅在测试已经实际运行且需要分类失败日志时使用。摘要只提供日志规模和证据状态，不代表测试通过或失败；诊断时必须读取必要日志证据。日志必须是仓库内安全的相对路径、普通 UTF-8 文本且不被判定为敏感路径；超过当前 2 MiB 上限时保留 head/tail 并记录中间省略。该命令不运行测试，也不能把日志分类转换成“测试通过”。

### `snapshot-runner read`

```bash
/home/hsd/bin/snapshot-runner read --repo <absolute-repo> --field <field> [--path <repo-relative-path>]
```

按需读取既有 content-addressed snapshot 的字段或有界文件内容，是把摘要展开为具体证据的正式通道。它要求快照已存在且 `--path` 不逃逸仓库；`read` 不收集新快照、不接受 `--summary`，其输出同样受截断与 evidence gap 语义约束。仅在摘要或 `snapshot.json` 仍不足以支持当前判断时使用。

## 按比例使用

- 不要为了满足形式依次运行四个收集命令。
- 状态来源清晰、改动小且不发布时，可完全跳过 Runner。
- 当前工作树审查选择 `diff-audit`；已提交分支相对明确 base 的审查选择 `branch-review`；复杂或来源不明状态先选择 `repo-status`；只有真实失败日志需要分类时选择 `test-triage`。
- 计划调用 Git Finalizer 时，必须完成足以覆盖发布范围的最小相关 Snapshot Runner 审查并解决阻断；通常至少需要当前 diff 审查。

## 失败与产物处理

- Runner 因明确不支持的文件类型（如 YAML）拒绝内容时，视为工具能力边界，不视为代码 finding。
- 变更仅含此类文件时，可用人工聚焦 diff、格式或语法解析、项目测试和 `git diff --check` 作为替代审查证据。
- 混合变更仍须对支持的文件使用 Runner，并单独审查不支持部分。
- 仅真实 finding、范围混杂或证据不足阻止发布；不得用原始 Git 绕过其他停止条件。
- Runner 工作流失败时通常退出 2，并以 `workflow_failed: <code>: <message>` 报告。
- 记录实际退出码和可见错误；不得改用 wrapper 绕过校验，不得伪造 snapshot ID、产物路径或审查结果。
- 只有命令实际打印成功的 snapshot ID 和路径后，才能报告产物已创建。
- 需要展开时还必须检查 artifact 的 `incomplete`、conversion safety、拒绝项和 evidence gaps。并非每个缺口都自动成为发布阻断，但任何影响当前发布结论的缺口必须先解决。
- 除上述已由替代证据覆盖的不支持文件类型外，Runner 失败、产物不完整到无法支持结论或审查发现阻断时，停止 Git Finalizer。
