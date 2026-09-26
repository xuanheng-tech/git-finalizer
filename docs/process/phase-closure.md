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
branch retirement verified；随后才按现有人工流程处理 local worktree 与 local branch cleanup。
自 1.1.0 起，local branch retirement 存在显式接口，但只接受 eligible Controller plan，Finalizer
不自主清理、不删除 worktree。Worktree Controller 为 `available_external`（contract v5 绑定
contract 3），其 plan/lease 生产与 lifecycle 更新仍属上层职责，不得据此声称自动清理。工具项目还
必须记录 binary/entry 与 canonical Skill compatibility（含 installed live 与 source canonical 的
tree hash 对照）；非工具项目记 `N/A`。正式 release 只由 pushed tag 触发的 `release.yml` 产生，且一个版本只在一个主机首发（GitHub 为公开分发入口，
Gitea 为受治理交付与历史记录；规则见 [`docs/release-governance.md`](../release-governance.md)）；
合入 main 不构成发布，也不产生任何主机的 Release 或 artifact。

使用 [`phase-closure-report-template.md`](phase-closure-report-template.md) 保留最小证据。
