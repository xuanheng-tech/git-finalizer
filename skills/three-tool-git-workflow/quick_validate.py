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
REQUIRED_REFERENCES = (
    "references/context-loader.md",
    "references/git-finalizer.md",
    "references/snapshot-runner.md",
)
CONTRACT_MARKERS = (
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
    "最终回复必须包含一行",
    "Publication decision: <publish_now|publication_blocked|intentionally_unpublished|not_applicable>",
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
    "默认模式才执行 push 和远端 post-verify",
    "不得重跑会创建提交的模式或重复制造 commit",
    "tag、Release、Artifact 和 deployment",
)


def contract_section(content: str) -> str:
    start = "## 任务结束发布决策（强制）"
    end = "## 职责分离"
    if start not in content or end not in content:
        return ""
    return content.split(start, 1)[1].split(end, 1)[0]


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
