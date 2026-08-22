# Phase closure SOP

此 SOP 由三工具共同 workflow owner 维护；项目内如已有更具体的 authoritative SOP，以更具体
规则为准，不复制本文件。

## Main Integration Gate

`feature remote verified` 不等于 `main integrated verified`。阶段报告必须分别记录：

- Feature Release：feature remote ref 与预期 OID 的实时核验。
- Main Integration：main 的实时 OID、集成方式，以及 feature commits 对 main 的 ancestry 证明。

无 ancestry 的 squash/tree/semantic equivalence 不得作为 v1 remote retirement 依据。

## Retirement / Tool Skill Sync Gate

发布后、local cleanup 前依次确认：remote CI green 且绑定 main OID；main contains commits；remote
branch retirement verified；随后才由现有人工流程完成 local worktree 与 local branch cleanup。
Git Finalizer 不执行后二者。工具项目还必须记录 binary/entry 与 canonical Skill compatibility；
非工具项目记 `N/A`。Worktree Controller 当前为 `planned / unavailable`，不得据此声称自动清理。

使用 [`phase-closure-report-template.md`](phase-closure-report-template.md) 保留最小证据。
