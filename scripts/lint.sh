#!/usr/bin/env bash

# Resolve a usable shellcheck and lint every bash surface, or fail with a clear message.
# A host may ship a shellcheck binary the CI user cannot execute, so usability is proven by
# running it rather than by its presence on PATH.

set -Eeuo pipefail

if [[ ! -f git-finalize || ! -d tests ]]; then
    printf 'error: run scripts/lint.sh from the repository root\n' >&2
    exit 2
fi

targets=(git-finalize tool-skill-sync scripts/*.sh tests/*.sh)
options=(--exclude=SC2016 --)

if command -v shellcheck >/dev/null 2>&1 && shellcheck --version >/dev/null 2>&1; then
    exec shellcheck "${options[@]}" "${targets[@]}"
fi

if command -v uvx >/dev/null 2>&1 &&
    uvx --from shellcheck-py shellcheck --version >/dev/null 2>&1; then
    exec uvx --from shellcheck-py shellcheck "${options[@]}" "${targets[@]}"
fi

printf 'error: no usable shellcheck; install the shellcheck package or uv (which provides uvx)\n' >&2
exit 2
