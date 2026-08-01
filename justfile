default:
    just --list

check:
    bash tests/run.sh
    python3 -B -m unittest discover -s tests -p 'test_*.py'
    python3 -B -m unittest discover -s hooks -p 'test_*.py'

hook-check:
    python3 -B hooks/deploy.py check

hook-install:
    python3 -B hooks/deploy.py install
