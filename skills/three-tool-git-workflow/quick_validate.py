#!/usr/bin/env python3
"""Validate the versioned Three-tool Git workflow Skill and its decision contract."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Sequence


SKILL_NAME = "three-tool-git-workflow"
REQUIRED_STATES = (
    "not_applicable",
    "publication_blocked",
    "intentionally_unpublished",
    "publish_now",
)
REQUIRED_SCOPES = (
    "commit_only",
    "commit_and_push",
    "not_applicable",
)
REQUIRED_OUTCOMES = (
    "not_run",
    "local_commit_created",
    "remote_pushed",
    "remote_verified",
    "blocked",
)
REQUIRED_REFERENCES = (
    "references/context-loader.md",
    "references/git-finalizer.md",
    "references/snapshot-runner.md",
)
CONTRACT_MARKERS = (
    "Publication decision 只回答",
    "它不描述 Git 最终结果",
    "按顺序选择首个符合的状态，后续状态不再适用",
    "保持 explicit-only",
    "用户明确要求 commit、push、发布或使用 Git Finalizer",
    "仅要求 commit 选择 `commit-only` 且不得自行扩大为 push",
    "“完成这个任务”",
    "“全权处理”",
    "“修复这个问题”",
    "测试、验收或 Snapshot 未通过",
    "cwd/repo 不一致",
    "工作树无法安全分离",
    "没有获得明确发布授权",
    "实际调用 Git Finalizer",
    "`publish_now` 只是执行决策，不是最终结果",
    "### Finalization scope",
    "### Finalization outcome",
    "### 合法组合",
    "最终回复必须各包含且只包含一行",
    "Publication decision: <publish_now|publication_blocked|intentionally_unpublished|not_applicable>",
    "Finalization scope: <commit_only|commit_and_push|not_applicable>",
    "Finalization outcome: <not_run|local_commit_created|remote_pushed|remote_verified|blocked>",
    "`local_commit_created`",
    "不得称为“已发布”",
    "`remote_pushed`",
    "`remote_verified`",
    "HEAD = upstream = remote OID",
    "`blocked`",
    "Local commit: <oid>",
    "Remote publication: blocked",
    "Prior-stage local commit: <oid>",
    "历史 commit-only fixture",
    "无论旧报告曾写 `publish_now` 还是 `intentionally_unpublished`",
    "`commit_only + remote_pushed|remote_verified` 非法",
    "`publish_now + not_run` 不能作为正常完成终态",
    "`remote_verified` 只能与 `commit_and_push` 配对",
)
FINALIZER_MODE_MARKERS = (
    "## Git Finalizer 模式选择",
    "选择 Finalizer 模式不得扩大用户原有的 commit/push 授权",
    "`--mode verify-only`",
    "`--mode commit-only`",
    "默认模式（不传 `--mode`）",
    "三种模式共用适用于各自执行边界的本地提交前检查",
    "`commit-only` 不要求 upstream",
    "`verify-only` 不修改 HEAD、index、worktree、refs 或 Git 配置",
    "三种提交生命周期模式中只有默认模式执行 commit 后的 push 和远端 post-verify",
    "`--resume-publish <full-head-oid> --repo <absolute-repo>`",
    "configured upstream",
    "`ahead >= 1`",
    "`behind = 0`",
    "`--no-follow-tags`",
    "root commit 继续使用 `--resume-initial-publish`",
    "不得重跑会创建提交的模式或重复制造 commit",
    "tag、Release、Artifact 和 deployment",
)

VALID_FINALIZATION_COMBINATIONS = frozenset(
    {
        ("not_applicable", "not_applicable", "not_run"),
        ("intentionally_unpublished", "not_applicable", "not_run"),
        ("publish_now", "commit_only", "local_commit_created"),
        ("publish_now", "commit_and_push", "remote_pushed"),
        ("publish_now", "commit_and_push", "remote_verified"),
        ("publication_blocked", "not_applicable", "blocked"),
        ("publication_blocked", "commit_only", "blocked"),
        ("publication_blocked", "commit_and_push", "blocked"),
    }
)
PRIOR_STAGE_COMMIT_COMBINATION = (
    "intentionally_unpublished",
    "commit_only",
    "local_commit_created",
)
PRIOR_STAGE_COMMIT_PATTERN = re.compile(
    r"^Prior-stage local commit: [0-9a-f]{7,64}$", re.MULTILINE
)
REMOTE_SUCCESS_CLAIM_PATTERN = re.compile(
    r"^(?:Push|Remote publication|Remote verification): "
    r"(?:success|succeeded|verified)$",
    re.MULTILINE,
)


def contract_section(content: str) -> str:
    start = "## 任务结束发布决策（强制）"
    end = "## 职责分离"
    if start not in content or end not in content:
        return ""
    return content.split(start, 1)[1].split(end, 1)[0]


def finalization_report_errors(report: str) -> list[str]:
    """Validate the three canonical finalization report lines and their combination."""

    errors: list[str] = []
    specifications = (
        ("Publication decision", REQUIRED_STATES),
        ("Finalization scope", REQUIRED_SCOPES),
        ("Finalization outcome", REQUIRED_OUTCOMES),
    )
    values: dict[str, str] = {}
    for label, allowed in specifications:
        matches = re.findall(
            rf"^{re.escape(label)}: ([^\r\n]+)$",
            report,
            re.MULTILINE,
        )
        if len(matches) != 1:
            errors.append(f"report must contain exactly one {label} line")
            continue
        value = matches[0].strip()
        values[label] = value
        if value not in allowed:
            errors.append(
                f"invalid {label}: {value}; expected one of {', '.join(allowed)}"
            )

    if len(values) != len(specifications) or errors:
        return errors

    decision = values["Publication decision"]
    scope = values["Finalization scope"]
    outcome = values["Finalization outcome"]
    combination = (decision, scope, outcome)
    prior_stage_exception = (
        combination == PRIOR_STAGE_COMMIT_COMBINATION
        and PRIOR_STAGE_COMMIT_PATTERN.search(report) is not None
    )
    if combination not in VALID_FINALIZATION_COMBINATIONS and not prior_stage_exception:
        errors.append(
            "invalid finalization combination: "
            f"decision={decision}, scope={scope}, outcome={outcome}"
        )

    if scope == "commit_only" and REMOTE_SUCCESS_CLAIM_PATTERN.search(report):
        errors.append("commit_only report cannot claim remote publication success")
    if outcome == "remote_verified" and scope != "commit_and_push":
        errors.append("remote_verified requires commit_and_push")
    if decision == "publish_now" and outcome == "not_run":
        errors.append("publish_now cannot finish with not_run")
    if outcome == "blocked" and re.search(r"^Local commit: ", report, re.MULTILINE):
        if "Remote publication: blocked" not in report:
            errors.append(
                "a blocked report with a local commit must state "
                "Remote publication: blocked"
            )
    return errors


def validation_errors(skill_path: Path) -> list[str]:
    errors: list[str] = []
    skill_path = Path(skill_path)
    skill_md = skill_path / "SKILL.md"
    if skill_path.name != SKILL_NAME:
        errors.append(f"skill directory must be named {SKILL_NAME}")
    if skill_md.is_symlink() or not skill_md.is_file():
        return [f"SKILL.md is not a regular file: {skill_md}"]

    content = skill_md.read_text(encoding="utf-8")
    frontmatter = re.match(r"\A---\n(.*?)\n---\n", content, re.DOTALL)
    if frontmatter is None:
        errors.append("invalid YAML frontmatter delimiters")
    else:
        lines = [line for line in frontmatter.group(1).splitlines() if line.strip()]
        fields = [line.split(":", 1)[0].strip() for line in lines if ":" in line]
        if fields != ["name", "description"]:
            errors.append("frontmatter must contain only name and description")
        name = next(
            (line.split(":", 1)[1].strip() for line in lines if line.startswith("name:")),
            "",
        )
        description = next(
            (
                line.split(":", 1)[1].strip()
                for line in lines
                if line.startswith("description:")
            ),
            "",
        )
        if name != SKILL_NAME:
            errors.append(f"frontmatter name must be {SKILL_NAME}")
        if not description or len(description) > 1024:
            errors.append("frontmatter description must contain 1-1024 characters")
        if "<" in description or ">" in description:
            errors.append("frontmatter description cannot contain angle brackets")

    if len(content.splitlines()) >= 500:
        errors.append("SKILL.md must remain under 500 lines")
    if any(line.endswith((" ", "\t")) for line in content.splitlines()):
        errors.append("SKILL.md contains trailing whitespace")

    section = contract_section(content)
    definitions = tuple(re.findall(r"^\d+\. `([^`]+)`：", section, re.MULTILINE))
    if definitions != REQUIRED_STATES:
        errors.append(
            "publication states must be ordered exactly as " + ", ".join(REQUIRED_STATES)
        )
    for marker in CONTRACT_MARKERS:
        if marker not in section:
            errors.append(f"publication contract is missing: {marker}")
    for marker in FINALIZER_MODE_MARKERS:
        if marker not in content:
            errors.append(f"Finalizer mode contract is missing: {marker}")

    for relative in REQUIRED_REFERENCES:
        reference = skill_path / relative
        if reference.is_symlink() or not reference.is_file():
            errors.append(f"required reference is missing or not regular: {relative}")
        if f"]({relative})" not in content:
            errors.append(f"SKILL.md does not link required reference: {relative}")
    return errors


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print("Usage: python3 quick_validate.py <skill-directory>", file=sys.stderr)
        return 2
    errors = validation_errors(Path(arguments[0]))
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print("Skill is valid!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
