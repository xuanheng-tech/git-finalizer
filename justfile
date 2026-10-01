default:
    just --list

check:
    python3 -B scripts/check.py

# Mandatory maintainer gate before creating a release tag.
release-check:
    just lint
    python3 -B scripts/check.py --require-controller

toolchain-check source_root="..":
    ./tool-skill-sync --source-root "{{source_root}}" --source-repo git-finalizer="$PWD" check --source-only context-loader
    ./tool-skill-sync --source-root "{{source_root}}" --source-repo git-finalizer="$PWD" check --source-only snapshot-runner
    ./tool-skill-sync --source-root "{{source_root}}" --source-repo git-finalizer="$PWD" check --source-only git-finalizer

lint:
    bash scripts/lint.sh

shellcheck: lint

skill-check:
    python3 -B skills/git-change-delivery/deploy.py check

skill-install:
    python3 -B skills/git-change-delivery/deploy.py install

# Explicit local deployment gate; CI source-only checks remain independent.
workflow-check repo="." source_root="..":
    ./tool-skill-sync --source-root "{{source_root}}" --source-repo git-finalizer="$PWD" doctor --repo "{{repo}}" --summary
