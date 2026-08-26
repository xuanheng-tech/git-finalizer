# Completed-but-Unpublished Queue v1

## 入口与边界

版本化 helper 与 live 入口分别为：

```bash
python3 -B <skill-source>/unpublished_queue.py ...
python3 -B /home/hsd/.agents/skills/git-change-delivery/unpublished_queue.py ...
```

它只管理本地 control-plane metadata，不执行 Snapshot、commit、push、fetch、stash、reset、
restore、clean 或 worktree/index 写入。Git Finalizer 只提供 publication evidence，不拥有 queue。

默认状态目录：

```text
${XDG_STATE_HOME:-~/.local/state}/codex-exec/completed-unpublished/v1/
```

目录必须为当前用户所有的普通目录且权限为 `0700`；`key`、`queue.lock`、`queue.jsonl` 和
migration dry-run 输出必须为普通文件且权限为 `0600`。`queue.jsonl` 每个 `record_id` 只有一条
current-state record；写入在 advisory lock 内原子替换。单条（含换行）最大 `8192` bytes。

## 身份、schema 与隐私

`repo_ref`、`task_ref`、`thread_ref`、`record_id` 和 `path_ref` 使用 queue 私有 32-byte key
计算 HMAC-SHA256 pseudonym。raw remote URL、task/thread ID 不落盘。task ID 未显式传入时，
helper 只从 `CODEX_THREAD_ID` 对应的一个 root rollout 中读取结构化 lifecycle envelope，要求恰有
一个 unmatched `task_started.turn_id`；它不读取或保存 message 正文，歧义时 fail closed。

record schema 固定包含：

```text
schema_version
record_id
repo_ref
task_ref
thread_ref
workstream
publication_decision
finalization_scope
finalization_outcome
queue_state
base_head
path_scope
created_at
updated_at
last_reviewed_at
closure_evidence
local_commit_oid
```

不得保存 prompt、assistant reply、diff/patch、源码正文、reasoning、credential 或完整 remote URL。

`path_scope` 最多保存 16 个排序后的相对路径代表项。路径不超过 16 个时为 `exact`，保存
file/symlink/deletion、mode 和内容 SHA-256（不保存内容）；更多路径时为 `bounded`，只保存
`path_count`、16 个 path reference、`paths_omitted`、path-set digest 和 scope fingerprint。
`bounded` record 不得自动判定等价发布。

## Task-end upsert

典型调用：

```bash
python3 -B /home/hsd/.agents/skills/git-change-delivery/unpublished_queue.py \
  upsert \
  --repo /absolute/repo \
  --workstream "short stable label" \
  --publication-decision intentionally_unpublished \
  --finalization-scope not_applicable \
  --finalization-outcome not_run \
  --path relative/file
```

测试、Workspace 或其他能直接提供 structured identity 的 caller 可显式传 `--task-id` 和
`--thread-id`。同一 repo/task/workstream 的 `record_id` 稳定；相同 scope 重试为 no-op，scope
扩大更新原 record。scope fingerprint 参与幂等判断，但不进入稳定 `record_id`，因此不会因扩大
scope 静默新增 active record。

映射：

- `intentionally_unpublished / not_applicable / not_run` → `pending`；
- `publication_blocked / <known scope> / blocked` → `blocked`；
- Finalizer 以 `publish_now` 开始但 outcome 为 `blocked` → queue `blocked`。这只是 ingestion
  compatibility；最终三行报告仍必须按主合同归一为 `publication_blocked / <known scope> / blocked`；
- `publish_now / commit_and_push / remote_pushed` → `blocked`；
- `publish_now / commit_only / local_commit_created` → `pending`；
- `not_applicable` 和没有既有 record 的 `publish_now / commit_and_push / remote_verified` 不创建
  active record；remote-verified task 若显式命中同一 task record，可将其关闭为 `published`。

## Repo-entry summary

```bash
python3 -B /home/hsd/.agents/skills/git-change-delivery/unpublished_queue.py \
  summary --repo /absolute/repo
```

只读取该 repo 的 `pending|blocked` 数量和最早日期。没有 active backlog 时 stdout 为空；有记录时
输出不超过 300 字符，例如：

```text
Unpublished backlog: 2 pending, 1 blocked. Oldest: 2026-08-09. Run queue review for details.
```

不得把完整 record 或 path list 默认注入 context。

## Bounded review 与关闭

```bash
python3 -B /home/hsd/.agents/skills/git-change-delivery/unpublished_queue.py \
  review --repo /absolute/repo [--record cuq_...] \
  [--remote-verified-oid <full-oid>]
```

`review` 只读取指定 repo/record 的 HEAD、configured upstream 和 exact scope tree/blob；不运行
网络命令，不修改 HEAD、refs、index、staged/worktree 状态。结果只使用：

```text
still_pending
still_blocked
already_published_equivalent
superseded_candidate
needs_human_review
```

`already_published_equivalent` 仅当：

1. record 为 exact scope；
2. 每个 path 的 state/mode/content SHA-256 与 configured upstream tree 完全相同；
3. caller 提供的 full `--remote-verified-oid` 等于本地 configured upstream OID。

此时只把 record 关闭为 `published`，`closure_evidence.kind` 为
`already_published_equivalent`；原 mixed worktree 即使 behind 或仍有等价 untracked 文件也完全
不变。没有显式 remote verification evidence 时，即使 tracking tree 相同也只返回
`needs_human_review`。

`review` 还提供派生的 `effective_state` 与 overlap 投影。`superseded_candidate` 投影为
`stale`，依据 fingerprint/commit/remote facts，而不是记录年龄；其他未关闭记录继续保持
`pending|blocked`。同一 repository 的 active exact scopes 通过 HMAC path refs 显示 confirmed
overlap；bounded scope 无法证明完整 path-set 不相交时显示 `potential_bounded`。输出包含对方
record/workstream owner，但不泄漏 raw path，也不自动合并或重新归属 scope。隐私 pseudonym
无法反查 repository 的未知 record 继续由 `validate` 保留并计数，不静默删除；人工确认前不把它
伪装成 published。

显式人工关闭：

```bash
python3 -B /home/hsd/.agents/skills/git-change-delivery/unpublished_queue.py \
  close --record cuq_... --state <published|superseded|dismissed> \
  --evidence-kind <fixed-kind> [--oid <full-oid>]
```

`published` 要求 `remote_verified|already_published_equivalent` 及 full OID；`superseded` 和
`dismissed` 只接受 helper 内固定 evidence kind。terminal record 不进入 active summary，也不能被
普通 upsert 重新打开。

## Validate 与 migration dry-run

```bash
python3 -B /home/hsd/.agents/skills/git-change-delivery/unpublished_queue.py validate
python3 -B /home/hsd/.agents/skills/git-change-delivery/unpublished_queue.py \
  migration-dry-run --source <frozen-candidate.json> [--output <state-dir/file.json>]
```

`validate` 对权限、JSONL、唯一/排序 ID、schema、state/finalization 组合、path bound、closure 和
8-KiB 上限 fail closed。

`migration-dry-run` 只接受显式的 frozen candidate JSON，不读取 Codex 历史，也不写
`queue.jsonl`。输出去除 raw repo/task/thread identity，固定排序且不含运行时间；相同 queue key 与
source bytes 得到字节级相同结果。`low|unknown` confidence 固定为
`excluded_low_confidence`；缺 task/thread/machine-readable scope 的 high/medium candidate 固定为
`human_confirmation_required`，不会正式导入。
