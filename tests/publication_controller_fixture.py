"""A public-protocol stub, independent of Controller's private storage."""

from __future__ import annotations

import fcntl
import json
from pathlib import Path
import subprocess
import sys
import time

CONTRACT = "integration-publication-session/v1"


def main() -> int:
    config_path = Path(sys.argv[1])
    config = json.loads(config_path.read_text())
    command = sys.argv[2]
    if command == "capabilities":
        print(json.dumps({"decision": {"integration_publication_execution_version": config.get("capability", 1)}}))
        return 0
    if command != "publication-execution-session":
        return 2
    identity = config["identity"]

    def observed() -> str:
        result = subprocess.run(
            ["git", "-C", config["repository"], "ls-remote", "--exit-code", "--refs", "origin", "refs/heads/main"],
            check=True, capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )
        return result.stdout.split("\t", 1)[0]

    def emit(phase: str, **fields: object) -> bool:
        delay = config.get("delays", {}).get(phase, 0)
        time.sleep(delay)
        if config.get("refusal") == phase:
            phase = "blocked"
            fields = {"code": "FIXTURE_REFUSAL", "reason": "public protocol refused execution"}
        document = {"schema_version": 1, "contract": CONTRACT, "phase": phase, **fields}
        document.update(config.get("overrides", {}).get(phase, {}))
        print(config.get("raw", {}).get(phase, json.dumps(document)), flush=True)
        return phase != "blocked"

    def recipe() -> dict[str, object]:
        remote_oid = observed()
        action = "RECOVER_ALREADY_PUBLISHED" if remote_oid == identity["candidate_oid"] else "PUSH_CANDIDATE"
        return {"identity": identity, "action": action, "remote_oid": remote_oid}

    with config_path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not emit("verified", **recipe()):
            return 1
        request = sys.stdin.readline()
        if not request or json.loads(request)["action"] != "authorize":
            return 1
        if not emit("authorized", **recipe()):
            return 1
        request = sys.stdin.readline()
        if not request:
            return 1
        result = json.loads(request)["result"]
        if observed() != identity["candidate_oid"]:
            return 1
        if not emit(
            "completed", identity=identity, record_id=result["record_id"],
            remote_verify={"status": "verified", "oid": observed()}, lifecycle_completion_required=True,
        ):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
