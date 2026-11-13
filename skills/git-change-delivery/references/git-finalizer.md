# Git Finalizer

Git Finalizer 负责显式路径暂存、commit、fast-forward-safe push 和远端核验。它不实施代码、
不跑测试、不审查 diff、不解决冲突，也不管 tag/Release。安全检查（范围、敏感内容、大文件、
whitespace、ahead/behind、push 后核验）由工具自己执行；调用方只需选对入口、传对参数、
读懂结果。完整参数以 `git-finalize --help` 为准。

## 选择入口

所有入口都加 `--summary`。`--` 后逐一列出 repository-relative 文件，不用目录或通配。

| 场景 | 入口 |
| --- | --- |
| 只验证，不写 | `--mode verify-only --repo R -- files` |
| 本地 commit，不 push | `--mode commit-only --repo R --message M -- files` |
| 已有可解析 upstream，commit 并 push | `--repo R --message M -- files`（默认模式） |
| 已 commit、本分支无 upstream、远端同名分支不存在 | `--publish-existing-branch <HEAD OID> --remote X --remote-branch <同名> --repo R` |
| 已 commit、有 upstream、上次 push 失败或不确定 | `--resume-publish <HEAD OID> --repo R` |
| Controller 已签发 publication lease 的 integration candidate | `--publish-integration-candidate <OID> --repo <candidate> --lease-id L --run-id U`，成功后把 `record_id` 交给 `lease-complete` |
| 把一个 remote 上已发布的分支同步到另一个 remote | `--sync-published-branch <OID> --source-remote A --remote B --remote-branch <分支> --expected-target-oid <B 当前 OID> --repo R` |
| unborn 仓库首次发布到空 remote | `--initial-publish --remote X --repo R --message M -- files`；中断用 `--resume-initial-publish <root OID> --remote X --repo R` |
| unborn 仓库只做本地 root commit | `--initial-commit-only --repo R --message M -- files` |
| 已有本地历史，首次发布到完全空的 remote | `--publish-existing-history <OID> --remote X --remote-branch B --repo R`；中断用 `--resume-existing-history-publish` 同参数 |
| 已解决的双亲 merge | `--merge-parent <OID>`，只配 verify-only 或 commit-only |
| Gitea 空仓库创建 | `--repo-plan`（只读）→ `--repo-ensure` |

`--dry-run` 适用于 normal、commit-only、initial，只报告范围和静态风险，不 fetch/push。
retirement 的 `--dry-run` 会做完整 live preflight，成功为 `RETIREMENT_PREFLIGHT_PASSED`。

已有目标 commit 只差 push 时，不要再跑默认或 commit-only 模式，否则会重复制造 commit；
用对应的 resume 或 publish-existing 入口，并传工具报告的同一完整 OID。

## 读取结果

成功条件：退出码 0、`status=success`、`mode` 与请求一致。需要 push 的模式还要求
`push.executed=true`、`push.result=succeeded`（integration candidate 为 `published`，
中断后重入为 `already_published_recovered`）。commit-only 要求 `commit.created=true` 并给出 OID。

`status` 为 `blocked`/`failed`/`partial` 时，读 `reason`、`next_action` 和 `resume`：

- `resolve_blocker_and_retry`：修正 `reason` 指出的问题后，用同一入口重试。
- commit 已创建但 push 失败：保留该 commit，用 resume 入口和报告的 OID 继续，不要 recommit。
- push 结果不确定：先用 `git ls-remote` 读远端事实，再决定 resume 还是已完成。

## 测试 fixture 中的敏感内容

扫描只报告相对基线新增的匹配：待发布 commit 以其第一父提交为基线，工作区和暂存区以 `HEAD`
为基线。修改一个已含已发布合成样本的文件、但不改动样本本身时，不需要任何例外参数。
只有新增、改动或复制合成样本时才需要：

- commit-only / verify-only / 默认模式：`--allow-test-fixture tests/<file>`（只限 `tests/` 下）。
- publish-existing-branch、sync-published-branch、resume 类入口：`--fixture-exceptions <绝对路径 JSON>`，
  按 detector 逐条复核，不能和 `--allow-test-fixture` 混用：

```json
{
  "schema_version": 1,
  "repository": {
    "root_commit": "<唯一 root commit>",
    "remote_url_sha256": "<目标 remote 唯一 push URL 字符串的 sha256>"
  },
  "exceptions": [{
    "path": "tests/test_x.py",
    "blob_oid": "<HEAD 中该文件的 blob OID>",
    "sha256": "<该 blob 原始字节 sha256>",
    "detector": "private-key-header-v1",
    "reason": "<为什么是合成 fixture>"
  }]
}
```

detector 取值：`private-key-header-v1`、`known-token-v1`、`credential-assignment-v1`，按拒绝信息
逐个添加（同一文件可有多条），最多 16 条。`remote_url_sha256` 绑定目标 remote，换 remote
（如 Gitea 与 GitHub）要分别生成。`CHANGELOG.md` 只接受 `private-key-header-v1`，且 marker 必须是
upstream 已有的行内反引号示例。

## 敏感命名的源码文件

`tokens.css`、`registry.json`、`credential_*.py` 这类路径被路径规则拦截、但内容确属正常源码时，
用 `--reviewed-sensitive-source <绝对路径 JSON>`，同时传 `--repository-id --allocation-id
--task-key --authority-key` 和完整文件列表。支持 normal、commit-only、verify-only、
publish-existing-branch 和 resume-publish；`.env` 与 key 文件路径不接受。复核文件：

```json
{
  "schema_version": 1,
  "kind": "reviewed-sensitive-source",
  "repository": {"common_dir": "/abs/canonical/.git", "root_commit": "<root OID>"},
  "allocation": {
    "repository_id": "<Controller repository ID>",
    "allocation_id": "<allocation ID>",
    "task_key": "<task key>",
    "authority_key": "<authority key>",
    "worktree_path": "/abs/worktree"
  },
  "scope_sha256": "<排序后的完整相对路径以 LF 连接并以 LF 结尾的 sha256>",
  "reviewed_at": "<UTC ISO>",
  "expires_at": "<UTC ISO，至多 review 后 7 天>",
  "reviews": [{
    "path": "tokens/tokens.css",
    "sha256": "<文件内容 sha256>",
    "purpose": "<复核目的>",
    "evidence": {"path": "/abs/external/review.md", "sha256": "<evidence sha256>"}
  }]
}
```

复核文件与 evidence 放在 worktree 和 `.git` 之外，当前用户所有、group/other 不可写、各不超过
64 KiB。`reviews` 只列被路径规则拦截的文件；`scope_sha256` 覆盖整个提交范围。内容扫描照常执行。

`--allow-large-binary <path>` 放行单个 5–25 MiB 的二进制文件。

## 分支退役

任务分支合入后，由 Controller 生成计划，再执行计划里给出的命令：

```bash
worktree-controller branch-retirement-plan --repo R --branch B --remote X --integrated-into main --json
```

结果 `BRANCH_RETIREMENT_READY` 时，`decision.finalizer.remote` 和 `decision.finalizer.local` 就是
完整的 `git-finalize --retire-remote-branch ...` / `--retire-local-branch ...` 命令，直接执行；
Finalizer 内部完成 Controller 的 verify/authorize/complete。成功结果为
`REMOTE_BRANCH_RETIRED_VERIFIED` / `LOCAL_BRANCH_RETIRED_VERIFIED`，重复执行为
`ALREADY_ABSENT_VERIFIED`。`UNIQUE`、`MIXED`、`CHERRY_EQUIVALENT`、`MERGE_ONLY` 只报告，
分支保留。计划会因随后的 Controller 写入或分支移动失效，失效就重新生成。仓库策略要求 CI 时，
加 `--ci-required --ci-status SUCCESS --ci-commit-oid <OID> --ci-verification-source tool_authenticated`。

## 本机 loopback remote

remote host 是 `localhost`、`127.0.0.0/8` 或 `::1` 时，它是宿主机上的 Gitea/SSH。沙箱里出现
`connection refused` 等只说明沙箱看不到宿主网络，不说明服务停了；不要启停服务或改 remote，
在沙箱外重跑同一 Finalizer 命令。

## 停止

- 继续需要 amend、force push、history rewrite，或用原始 `git add/commit/push` 绕过 Finalizer。
- 本地落后或分叉、remote 配置含糊、push 结果在读取远端事实后仍无法确定。
- 测试或审查未通过，或改动范围无法与他人改动隔离。

停止时报告是否已创建 commit、确切 OID、已验证与未验证的状态和工具给出的恢复入口。
