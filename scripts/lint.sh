#!/usr/bin/env bash

# Resolve a usable shellcheck and lint every bash surface, or fail with a clear message.
# Availability is proven by executing a candidate, because a host can ship the binary without
# execute permission for the CI user, and a fallback that fetches a package can fail transiently,
# so each candidate is probed twice before the next one is tried.

set -Eeuo pipefail

if [[ ! -f git-finalize || ! -d tests ]]; then
    printf 'error: run scripts/lint.sh from the repository root\n' >&2
    exit 2
fi

targets=(git-finalize tool-skill-sync scripts/*.sh tests/*.sh)
options=(--exclude=SC2016 --)

probe() {
    local attempt=0
    while ((attempt < 2)); do
        if "$@" --version >/dev/null 2>&1; then
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    return 1
}

if [[ -n ${GF_SHELLCHECK:-} ]] && probe "$GF_SHELLCHECK"; then
    exec "$GF_SHELLCHECK" "${options[@]}" "${targets[@]}"
fi

if command -v shellcheck >/dev/null 2>&1 && probe shellcheck; then
    exec shellcheck "${options[@]}" "${targets[@]}"
fi

if command -v uvx >/dev/null 2>&1 && probe uvx --from shellcheck-py shellcheck; then
    exec uvx --from shellcheck-py shellcheck "${options[@]}" "${targets[@]}"
fi

if command -v uv >/dev/null 2>&1; then
    tool_bin=$(uv tool dir 2>/dev/null || true)
    if [[ -n $tool_bin && -x "$tool_bin/bin/shellcheck" ]] &&
        probe "$tool_bin/bin/shellcheck"; then
        exec "$tool_bin/bin/shellcheck" "${options[@]}" "${targets[@]}"
    fi
    if probe uv tool run --from shellcheck-py shellcheck; then
        exec uv tool run --from shellcheck-py shellcheck "${options[@]}" "${targets[@]}"
    fi
fi

printf 'error: no usable shellcheck; install the shellcheck package, or uv (which provides\n' >&2
printf '       shellcheck-py), or export GF_SHELLCHECK=/absolute/path/to/shellcheck\n' >&2
exit 2
