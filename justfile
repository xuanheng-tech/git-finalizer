default:
    just --list

check:
    bash tests/run.sh
    python3 -B -m unittest discover -s tests -p 'test_*.py'
    python3 -B skills/git-change-delivery/quick_validate.py skills/git-change-delivery
    python3 -B -m unittest discover -s skills/git-change-delivery -p 'test_*.py'
    ./tool-skill-sync --source-repo git-finalizer="$PWD" check --source-only git-finalizer

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
