#!/usr/bin/env python3
"""Validate the structure of the versioned Git change delivery Skill."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Sequence


SKILL_NAME = "git-change-delivery"
REQUIRED_REFERENCES = (
    "references/context-loader.md",
    "references/git-finalizer.md",
    "references/snapshot-runner.md",
    "references/unpublished-queue.md",
)

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
