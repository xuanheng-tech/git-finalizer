"""A public-protocol stub, independent of Controller's private storage."""

from __future__ import annotations

import argparse
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
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--candidate-oid", required=True)
    parser.add_argument("--lease-id", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(sys.argv[3:])
    identity = config["identity"]
    if (
        not args.json
        or args.candidate_oid != identity["candidate_oid"]
        or args.lease_id != identity["lease_id"]
        or args.run_id != identity["run_id"]
    ):
        return 2

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
        if not request:
            return 1
        auth_req = json.loads(request)
        if (
            set(auth_req) != {"schema_version", "contract", "action", "identity"}
            or auth_req["schema_version"] != 1
            or auth_req["contract"] != CONTRACT
            or auth_req["action"] != "authorize"
            or auth_req["identity"] != identity
        ):
            return 1
        if not emit("authorized", **recipe()):
            return 1
        request = sys.stdin.readline()
        if not request:
            return 1
        comp_req = json.loads(request)
        if (
            set(comp_req) != {"schema_version", "contract", "action", "identity", "result"}
            or comp_req["schema_version"] != 1
            or comp_req["contract"] != CONTRACT
            or comp_req["action"] != "complete"
            or comp_req["identity"] != identity
        ):
            return 1
        result = comp_req["result"]
        if (
            not isinstance(result, dict)
            or set(result) != {"record_id", "executor", "push_executed", "remote_oid"}
            or not isinstance(result["executor"], dict)
            or set(result["executor"]) != {"name", "version"}
        ):
            return 1
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
