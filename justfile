default:
    just --list

check:
    bash tests/run.sh
    python3 -B -m unittest discover -s tests -p 'test_*.py'
    python3 -B -m unittest discover -s hooks -p 'test_*.py'
    python3 -B skills/three-tool-git-workflow/quick_validate.py skills/three-tool-git-workflow
    python3 -B -m unittest discover -s skills/three-tool-git-workflow -p 'test_*.py'

hook-check:
    python3 -B hooks/deploy.py check

hook-install:
    python3 -B hooks/deploy.py install

skill-check:
    python3 -B skills/three-tool-git-workflow/deploy.py check

skill-install:
    python3 -B skills/three-tool-git-workflow/deploy.py install
