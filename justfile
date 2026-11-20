default:
    just --list

check:
    python3 -B scripts/check.py

# Mandatory maintainer gate before creating a release tag.
release-check:
    just lint
    python3 -B scripts/check.py --require-controller --require-snapshot-runner

toolchain-check source_root="..":
    ./tool-skill-sync --source-root "{{source_root}}" --source-repo git-finalizer="$PWD" check --source-only context-loader
    ./tool-skill-sync --source-root "{{source_root}}" --source-repo git-finalizer="$PWD" check --source-only snapshot-runner
    ./tool-skill-sync --source-root "{{source_root}}" --source-repo git-finalizer="$PWD" check --source-only git-finalizer

lint:
    bash scripts/lint.sh
    ruff check src tests scripts

shellcheck: lint

skill-check:
    ./tool-skill-sync --source-repo git-finalizer="$PWD" skill-check

skill-install:
    ./tool-skill-sync --source-repo git-finalizer="$PWD" skill-install

# Explicit local deployment gate; CI source-only checks remain independent.
workflow-check repo="." source_root="..":
    ./tool-skill-sync --source-root "{{source_root}}" --source-repo git-finalizer="$PWD" doctor --repo "{{repo}}" --summary
