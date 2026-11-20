"""Regression and frontier-feature verification tests for git-finalizer 2.0.0."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from git_finalizer.delivery import (
    content_scan,
    policy,
    repo_bootstrap,
    scanners,
    snapshot_verify,
    trailers,
)
from git_finalizer.errors import ErrorCode, FinalizerError

FINALIZER = ROOT / "git-finalize"
SKILL_SYNC = ROOT / "tool-skill-sync"


def _clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY"):
        env.pop(key, None)
    env.update(
        {
            "LC_ALL": "C",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Overhaul Tester",
            "GIT_AUTHOR_EMAIL": "tester@example.com",
            "GIT_COMMITTER_NAME": "Overhaul Tester",
            "GIT_COMMITTER_EMAIL": "tester@example.com",
        }
    )
    if extra:
        env.update(extra)
    return env


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=env or _clean_env(),
        check=True,
    )
    return res.stdout.strip()


def _init_repo(base: Path, *, branch: str = "main", with_base_commit: bool = True) -> Path:
    repo = base / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    env = _clean_env()
    subprocess.run(
        ["git", "init", "--quiet", f"--initial-branch={branch}", str(repo)],
        env=env,
        check=True,
    )
    if with_base_commit:
        (repo / "base.txt").write_text("base\n", encoding="utf-8")
        _git(repo, "add", "--", "base.txt", env=env)
        _git(repo, "commit", "-m", "initial commit", env=env)
    return repo


class Overhaul200Tests(unittest.TestCase):
    """Verify P0/P1/P2 fixes and frontier 2.0.0 capabilities."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run_finalizer(
        self, *args: str, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(FINALIZER), *args],
            capture_output=True,
            text=True,
            env=_clean_env(extra_env),
            check=False,
        )

    def test_state_aware_cli_parsing_prevents_flag_value_hijack_and_fake_success(self) -> None:
        repo = _init_repo(self.root / "parse")
        (repo / "note.txt").write_text("hello\n", encoding="utf-8")

        res1 = self._run_finalizer(
            "--mode",
            "commit-only",
            "--repo",
            str(repo),
            "--message",
            "--repo-plan",
            "--",
            "note.txt",
        )
        self.assertEqual(res1.returncode, 0, msg=res1.stderr)
        self.assertEqual(_git(repo, "log", "-1", "--format=%s"), "--repo-plan")

        (repo / "note.txt").write_text("hello2\n", encoding="utf-8")
        res2 = self._run_finalizer(
            "--mode",
            "commit-only",
            "--repo",
            str(repo),
            "--message",
            "--summary",
            "--",
            "note.txt",
        )
        self.assertEqual(res2.returncode, 0, msg=res2.stderr)
        self.assertNotIn('"summary_schema_version"', res2.stdout)
        self.assertEqual(_git(repo, "log", "-1", "--format=%s"), "--summary")

        for flag in ("--version", "--help"):
            with self.subTest(flag=flag):
                bad = self._run_finalizer("--summary", flag)
                self.assertNotEqual(bad.returncode, 0)
                payload = json.loads(bad.stdout)
                self.assertNotEqual(payload["status"], "success")
                self.assertEqual(payload["error_code"], ErrorCode.CLI_INVALID_ARGS)

    def test_atomic_staging_leaves_index_clean_when_validation_fails(self) -> None:
        repo = _init_repo(self.root / "atomic")
        (repo / "pre.txt").write_text("staged-already\n", encoding="utf-8")
        _git(repo, "add", "--", "pre.txt")
        index_before = _git(repo, "ls-files", "--stage")

        bad_payload = "ghp_" + ("A1b2C3d4E5" * 3) + "\n"
        (repo / "leak.txt").write_text(bad_payload, encoding="utf-8")

        res = self._run_finalizer(
            "--mode",
            "commit-only",
            "--summary",
            "--repo",
            str(repo),
            "--message",
            "should fail before touching index",
            "--",
            "leak.txt",
        )
        self.assertNotEqual(res.returncode, 0)
        index_after = _git(repo, "ls-files", "--stage")
        self.assertEqual(index_before, index_after)

    def test_execution_lock_fd_is_not_inherited_by_subprocesses(self) -> None:
        wrapper_dir = self.root / "lockbin"
        wrapper_dir.mkdir(mode=0o755)
        wrapper_dir.chmod(0o755)
        wrapper = wrapper_dir / "git-finalize"
        wrapper.symlink_to(FINALIZER)
        lockfile = wrapper_dir / ".git-finalizer-execution.lock"
        lockfile.write_text("", encoding="utf-8")
        lockfile.chmod(0o600)

        probe = wrapper_dir / "sarif-fd-probe.py"
        probe.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import json, os, sys",
                    "from pathlib import Path",
                    "report_path = Path(sys.argv[2])",
                    "lock_name = '.git-finalizer-execution.lock'",
                    "fd_dir = Path('/proc/self/fd')",
                    "if fd_dir.is_dir():",
                    "    for item in fd_dir.iterdir():",
                    "        try:",
                    "            target = os.readlink(item)",
                    "        except OSError:",
                    "            continue",
                    "        if lock_name in target:",
                    "            raise SystemExit(f'inherited lock fd: {item.name} -> {target}')",
                    "report_path.write_text(json.dumps({'version': '2.1.0', 'runs': [{'results': []}]}), encoding='utf-8')",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        probe.chmod(0o700)

        repo = _init_repo(self.root / "lockrepo")
        (repo / "ok.txt").write_text("clean\n", encoding="utf-8")
        res = subprocess.run(
            [
                str(wrapper),
                "--mode",
                "commit-only",
                "--summary",
                "--repo",
                str(repo),
                "--scanner",
                "sarif-cmd",
                "--sarif-cmd",
                str(probe),
                "--message",
                "clean with cloexec lock",
                "--",
                "ok.txt",
            ],
            capture_output=True,
            text=True,
            env=_clean_env(),
            check=False,
        )
        self.assertEqual(res.returncode, 0, msg=res.stderr or res.stdout)

    def test_content_scan_precision_and_sr_epoch6_patterns(self) -> None:
        self.assertEqual(content_scan.scan(b"issue: SK-2026-ABCDEFGH1234567890"), "CLEAR")
        self.assertEqual(
            content_scan.scan(("header=eyJ" + "a1B2c3D4e5" + ".eyJ" + "f6G7h8I9j0" + "." + "k1L2m3N4o5").encode()),
            "KNOWN",
        )
        self.assertEqual(
            content_scan.scan(("bot=" + "xoxb-" + "1234567890-" + "AbCdEfGhIjKl").encode()),
            "KNOWN",
        )
        self.assertEqual(
            content_scan.scan(("stripe=" + "sk_live_" + "51" + ("AbCdEf1234" * 3)).encode()),
            "KNOWN",
        )
        self.assertEqual(
            content_scan.scan(("app_token=" + "unquoted_secret_val_123456").encode()),
            "ASSIGNMENT",
        )

    def test_ai_provenance_trailers_and_agent_trace_note(self) -> None:
        built = trailers.build_provenance_trailers(
            trailer_agent="copilot-agent",
            snapshot_id="a" * 40,
            trace_ref="urn:agent-trace:run-9",
        )
        self.assertEqual(
            built,
            [
                ("Assisted-by", "copilot-agent"),
                ("Snapshot-Evidence-Id", "a" * 40),
                ("Agent-Trace-Ref", "urn:agent-trace:run-9"),
            ],
        )

        repo = _init_repo(self.root / "trace")
        (repo / "feature.py").write_text("print('hi')\n", encoding="utf-8")
        res = self._run_finalizer(
            "--mode",
            "commit-only",
            "--summary",
            "--repo",
            str(repo),
            "--message",
            "feat: provenance commit",
            "--trailer-agent",
            "claude-sonnet",
            "--trace-ref",
            "urn:trace:run-42",
            "--agent-trace",
            "--",
            "feature.py",
        )
        self.assertEqual(res.returncode, 0, msg=res.stderr or res.stdout)
        summary = json.loads(res.stdout)
        self.assertEqual(summary["status"], "success")
        body = _git(repo, "log", "-1", "--format=%B")
        self.assertIn("Assisted-by: claude-sonnet", body)
        self.assertIn("Agent-Trace-Ref: urn:trace:run-42", body)

        note_raw = _git(repo, "notes", "--ref", "refs/notes/agent-trace", "show", "HEAD")
        note = json.loads(note_raw)
        self.assertEqual(note["schema"], "https://agent-trace.dev/v0/record.schema.json")
        self.assertEqual(note["metadata"]["agent"], "claude-sonnet")
        self.assertEqual(note["metadata"]["trace_ref"], "urn:trace:run-42")
        self.assertEqual(note["files"][0]["path"], "feature.py")
        self.assertIn("sha256", note["files"][0])

    def test_declarative_delivery_policy_and_opa_hook(self) -> None:
        repo = _init_repo(self.root / "policy", branch="release/v1")
        agents_dir = repo / ".agents"
        agents_dir.mkdir()
        (agents_dir / "delivery-policy.toml").write_text(
            "\n".join(
                [
                    "schema_version = 1",
                    'extra_protected_branches = ["release/*"]',
                    'forbid_paths = ["*.pem"]',
                    'require_trailers = ["Assisted-by"]',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        _git(repo, "add", "--", ".agents/delivery-policy.toml")
        _git(repo, "commit", "-m", "add delivery policy")

        (repo / "work.txt").write_text("ok\n", encoding="utf-8")
        res_branch = self._run_finalizer(
            "--mode",
            "commit-only",
            "--summary",
            "--repo",
            str(repo),
            "--message",
            "blocked by release/* pattern",
            "--trailer-agent",
            "agent",
            "--",
            "work.txt",
        )
        self.assertNotEqual(res_branch.returncode, 0)
        self.assertEqual(json.loads(res_branch.stdout)["error_code"], ErrorCode.BRANCH_PROTECTED)

        # Switch to allowed branch, test forbid_paths and require_trailers.
        _git(repo, "checkout", "-b", "feat/work")
        (repo / "cert.pem").write_text("nonsecret cert placeholder\n", encoding="utf-8")
        res_forbid = self._run_finalizer(
            "--mode",
            "commit-only",
            "--summary",
            "--repo",
            str(repo),
            "--message",
            "blocked path",
            "--trailer-agent",
            "agent",
            "--",
            "cert.pem",
        )
        self.assertNotEqual(res_forbid.returncode, 0)
        self.assertEqual(json.loads(res_forbid.stdout)["error_code"], ErrorCode.POLICY_DENIED)

        res_missing_trailer = self._run_finalizer(
            "--mode",
            "commit-only",
            "--summary",
            "--repo",
            str(repo),
            "--message",
            "missing required trailer",
            "--",
            "work.txt",
        )
        self.assertNotEqual(res_missing_trailer.returncode, 0)
        self.assertEqual(json.loads(res_missing_trailer.stdout)["error_code"], ErrorCode.POLICY_DENIED)

        # Invalid schema_version fails closed.
        bad_repo = _init_repo(self.root / "bad-policy")
        (bad_repo / ".agents").mkdir()
        (bad_repo / ".agents" / "delivery-policy.toml").write_text("schema_version = 99\n", encoding="utf-8")
        with self.assertRaises(FinalizerError) as ctx:
            policy.load_delivery_policy(bad_repo)
        self.assertEqual(ctx.exception.code, ErrorCode.POLICY_DENIED)

        # OPA policy hook: allow then deny.
        opa_bin = self.root / "opa_bin"
        opa_bin.mkdir()
        opa_exe = opa_bin / "opa"
        opa_exe.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import json, os, sys",
                    "mode = os.environ.get('OPA_MODE', 'allow')",
                    "val = [] if mode == 'allow' else ['deny: policy gate failed']",
                    "print(json.dumps({'result': [{'expressions': [{'value': val}]}]}))",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        opa_exe.chmod(0o700)
        rego = self.root / "delivery.rego"
        rego.write_text("package git_finalizer.delivery\ndeny := []\n", encoding="utf-8")

        res_opa_ok = self._run_finalizer(
            "--mode",
            "commit-only",
            "--summary",
            "--repo",
            str(repo),
            "--message",
            "allowed by opa",
            "--trailer-agent",
            "agent",
            "--opa-policy",
            str(rego),
            "--",
            "work.txt",
            extra_env={"PATH": f"{opa_bin}:{os.environ.get('PATH', '')}", "OPA_MODE": "allow"},
        )
        self.assertEqual(res_opa_ok.returncode, 0, msg=res_opa_ok.stdout)

        (repo / "work.txt").write_text("ok2\n", encoding="utf-8")
        res_opa_deny = self._run_finalizer(
            "--mode",
            "commit-only",
            "--summary",
            "--repo",
            str(repo),
            "--message",
            "denied by opa",
            "--trailer-agent",
            "agent",
            "--opa-policy",
            str(rego),
            "--",
            "work.txt",
            extra_env={"PATH": f"{opa_bin}:{os.environ.get('PATH', '')}", "OPA_MODE": "deny"},
        )
        self.assertNotEqual(res_opa_deny.returncode, 0)
        self.assertEqual(json.loads(res_opa_deny.stdout)["error_code"], ErrorCode.POLICY_DENIED)

    def test_sarif_scanner_orchestration_and_fail_closed(self) -> None:
        script_dir = self.root / "scanners"
        script_dir.mkdir()
        sarif_exe = script_dir / "fake-sarif.py"
        sarif_exe.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import json, os, sys",
                    "from pathlib import Path",
                    "mode = os.environ.get('SARIF_MODE', 'clean')",
                    "if mode == 'clean':",
                    "    doc = {'version': '2.1.0', 'runs': [{'results': [",
                    "        {'ruleId': 'suppressed', 'level': 'error', 'suppressions': [{'kind': 'inSource'}]},",
                    "        {'ruleId': 'info', 'level': 'note', 'kind': 'informational'},",
                    "    ]}]}",
                    "elif mode == 'block':",
                    "    doc = {'version': '2.1.0', 'runs': [{'results': [{'ruleId': 'hardcoded-secret', 'level': 'error', 'locations': [{'physicalLocation': {'artifactLocation': {'uri': 'app.py'}, 'region': {'startLine': 3}}}]}]}]}",
                    "else:",
                    "    doc = {'version': '1.0.0', 'runs': []}",
                    "Path(sys.argv[2]).write_text(json.dumps(doc), encoding='utf-8')",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        sarif_exe.chmod(0o700)

        repo = _init_repo(self.root / "sarif")
        (repo / "app.py").write_text("x = 1\n", encoding="utf-8")
        res_clean = self._run_finalizer(
            "--mode",
            "commit-only",
            "--summary",
            "--repo",
            str(repo),
            "--scanner",
            "sarif-cmd",
            "--sarif-cmd",
            str(sarif_exe),
            "--message",
            "clean sarif",
            "--",
            "app.py",
            extra_env={"SARIF_MODE": "clean"},
        )
        self.assertEqual(res_clean.returncode, 0, msg=res_clean.stdout)

        (repo / "app.py").write_text("x = 2\n", encoding="utf-8")
        index_before = _git(repo, "ls-files", "--stage")
        res_block = self._run_finalizer(
            "--mode",
            "commit-only",
            "--summary",
            "--repo",
            str(repo),
            "--scanner",
            "sarif-cmd",
            "--sarif-cmd",
            str(sarif_exe),
            "--message",
            "blocked sarif",
            "--",
            "app.py",
            extra_env={"SARIF_MODE": "block"},
        )
        self.assertNotEqual(res_block.returncode, 0)
        self.assertEqual(json.loads(res_block.stdout)["error_code"], ErrorCode.EXTERNAL_SCANNER_BLOCKED)
        self.assertEqual(index_before, _git(repo, "ls-files", "--stage"))

        with self.assertRaises(FinalizerError):
            scanners.parse_sarif_findings(b'{"version": "1.0.0", "runs": []}', "sarif-cmd")

    def test_snapshot_verify_sr260_batch_read_content_and_size_guard(self) -> None:
        repo = _init_repo(self.root / "snap", with_base_commit=False)
        content_text = "alpha = 1\n"
        file_bytes = content_text.encode("utf-8")
        (repo / "a.py").write_bytes(file_bytes)

        file_sha = hashlib.sha256(file_bytes).hexdigest()
        ws = hashlib.sha256()
        ws.update(b"a.py\0" + str(len(file_bytes)).encode("ascii") + b"\0" + file_sha.encode("ascii") + b"\0" + b"0\n")
        snap_id = "c" * 64

        bin_dir = self.root / "sr260"
        bin_dir.mkdir()
        sr_log = self.root / "sr_calls.log"
        sr_bin = bin_dir / "snapshot-runner"
        sr_bin.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env python3",
                    "import json, os, sys",
                    "from pathlib import Path",
                    "args = sys.argv[1:]",
                    "with open(os.environ['SR_LOG'], 'a', encoding='utf-8') as f:",
                    "    f.write(' '.join(args) + '\\n')",
                    "snap_id = args[1]",
                    "fields_order = ['status_short', 'staged_diff', 'unstaged_diff', 'file_context', 'baseline_kind', 'baseline_oid', 'initial_publication']",
                    "data_dict = {",
                    "  'status_short': '?? a.py\\n',",
                    "  'staged_diff': '',",
                    "  'unstaged_diff': '',",
                    "  'file_context': [{'path': 'a.py', 'content': os.environ['CTX_CONTENT']}],",
                    "  'baseline_kind': 'empty_tree',",
                    "  'baseline_oid': '4b825dc642cb6eb9a060e54bf8d69288fbee4904',",
                    "  'initial_publication': {",
                    "    'schema_version': 1,",
                    "    'mode': 'unborn',",
                    "    'complete': True,",
                    "    'sensitive_scan': 'complete',",
                    "    'changed_file_count': 1,",
                    "    'covered_file_count': 1,",
                    "    'content_file_count': 1,",
                    "    'generated_file_count': 0,",
                    "    'workspace_sha256': os.environ['WS_SHA'],",
                    "    'files': [{'path': 'a.py', 'bytes': int(os.environ['F_BYTES']), 'sha256': os.environ['F_SHA'], 'executable': False, 'coverage': 'content'}],",
                    "    'generated_trees': [],",
                    "  },",
                    "}",
                    "if '--fields' in args:",
                    "    sel = {'kind': 'fields', 'values': args[args.index('--fields') + 1].split(',')}",
                    "    ev = {'fields': data_dict}",
                    "elif '--field' in args:",
                    "    fname = args[args.index('--field') + 1]",
                    "    sel = {'kind': 'field', 'value': fname}",
                    "    ev = {'field': fname, 'value': data_dict[fname]}",
                    "else:",
                    "    sel = {'kind': 'index'}",
                    "    extras = [x for x in os.environ.get('SR_INDEX_EXTRAS', '').split(',') if x]",
                    "    ev = {'fields': [{'field': f} for f in [*fields_order, *extras]]}",
                    "doc = {",
                    "  'evidence_schema_version': 1,",
                    "  'command': 'diff-audit',",
                    "  'snapshot_id': snap_id,",
                    "  'repository': Path(os.environ['TARGET_REPO']).name,",
                    "  'status': 'complete',",
                    "  'truncated': False,",
                    "  'evidence_gap': False,",
                    "  'found': True,",
                    "  'runner_version': os.environ['SR_VER'],",
                    "  'selector': sel,",
                    "  'evidence': ev,",
                    "}",
                    "print(json.dumps(doc))",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        sr_bin.chmod(0o700)

        env_ok = {
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
            "SR_LOG": str(sr_log),
            "SR_VER": "2.6.0",
            "SR_CONTRACT": "4",
            "SR_INDEX_EXTRAS": "current_branch,head",
            "TARGET_REPO": str(repo),
            "CTX_CONTENT": content_text,
            "WS_SHA": ws.hexdigest(),
            "F_BYTES": str(len(file_bytes)),
            "F_SHA": file_sha,
        }
        with mock.patch.dict(os.environ, env_ok):
            snapshot_verify.verify_snapshot_evidence(snap_id, repo, "worktree", ["a.py"])
        calls = sr_log.read_text(encoding="utf-8").splitlines()
        self.assertTrue(any("--fields" in line for line in calls))

        # Unexpected extra index field fails closed even on 2.6.0.
        with (
            mock.patch.dict(os.environ, {**env_ok, "SR_INDEX_EXTRAS": "current_branch,head,rogue"}),
            self.assertRaises(FinalizerError) as rogue_ctx,
        ):
            snapshot_verify.verify_snapshot_evidence(snap_id, repo, "worktree", ["a.py"])
        self.assertEqual(rogue_ctx.exception.code, ErrorCode.SNAPSHOT_EVIDENCE_REJECTED)

        # Tampered file_context.content fails closed.
        sr_log.unlink()
        with mock.patch.dict(os.environ, {**env_ok, "CTX_CONTENT": "tampered\n"}):
            with self.assertRaises(FinalizerError) as ctx:
                snapshot_verify.verify_snapshot_evidence(snap_id, repo, "worktree", ["a.py"])
            self.assertEqual(ctx.exception.code, ErrorCode.SNAPSHOT_EVIDENCE_REJECTED)

        # Legacy SR 2.5.13 contract_version 3 continues working cleanly.
        sr_log.unlink()
        with mock.patch.dict(
            os.environ,
            {**env_ok, "SR_VER": "2.5.13", "SR_CONTRACT": "3", "SR_INDEX_EXTRAS": ""},
        ):
            snapshot_verify.verify_snapshot_evidence(snap_id, repo, "worktree", ["a.py"])
        legacy_calls = sr_log.read_text(encoding="utf-8").splitlines()
        self.assertFalse(any("--fields" in line for line in legacy_calls))

        # Oversized git object checks `cat-file -s` BEFORE materializing `cat-file blob`.
        seen_cmds: list[tuple[str, ...]] = []

        def fake_git(_repo: Path, *git_args: str, git_env: dict[str, str] | None = None):
            seen_cmds.append(git_args)
            if "ls-files" in git_args:
                return subprocess.CompletedProcess(git_args, 0, b"100644 " + b"b" * 40 + b" 0\ta.py\0", b"")
            if "cat-file" in git_args and "-s" in git_args:
                return subprocess.CompletedProcess(
                    git_args, 0, str(snapshot_verify.MAX_SCAN_BYTES + 1).encode("ascii") + b"\n", b""
                )
            raise AssertionError(f"unexpected git call: {git_args}")

        with (
            mock.patch.object(snapshot_verify, "_git_command", side_effect=fake_git),
            self.assertRaises(FinalizerError),
        ):
            snapshot_verify._read_git_version(repo, "index", "a.py", git_env=None)
        self.assertTrue(any(c[:2] == ("cat-file", "-s") for c in seen_cmds))
        self.assertFalse(any(c[:2] == ("cat-file", "blob") for c in seen_cmds))

    def test_repo_bootstrap_rejects_non_loopback_http_and_allows_loopback(self) -> None:
        with self.assertRaises(repo_bootstrap.BootstrapFailure):
            repo_bootstrap._validate_gitea_url("http://gitea.example.com:3000", allow_insecure_http=False)
        norm, host, port = repo_bootstrap._validate_gitea_url(
            "http://127.0.0.1:3000/", allow_insecure_http=False
        )
        self.assertEqual((norm, host, port), ("http://127.0.0.1:3000", "127.0.0.1", 3000))

    def test_multi_client_skill_install_and_check_without_shared_root_leak(self) -> None:
        agents_root = self.root / "agents"
        bin_dir = self.root / "bin"
        hooks_dir = self.root / "hooks"
        bin_dir.mkdir()
        hooks_dir.mkdir()
        codex_home = self.root / ".codex"
        claude_home = self.root / ".claude"
        gemini_home = self.root / ".gemini"
        cursor_home = self.root / ".cursor"
        qoder_home = self.root / ".qoder"
        for d in (codex_home, claude_home, gemini_home, cursor_home, qoder_home):
            d.mkdir()

        common_args = [
            str(SKILL_SYNC),
            "--source-repo",
            f"git-finalizer={ROOT}",
            "--agents-root",
            str(agents_root),
            "--bin-dir",
            str(bin_dir),
            "--hooks-dir",
            str(hooks_dir),
        ]
        client_flags = [
            "--codex-home",
            str(codex_home),
            "--claude-home",
            str(claude_home),
            "--gemini-home",
            str(gemini_home),
            "--cursor-home",
            str(cursor_home),
            "--qoder-home",
            str(qoder_home),
        ]

        res_inst = subprocess.run(
            [*common_args, "skill-install", *client_flags],
            capture_output=True,
            text=True,
            env=_clean_env(),
            check=False,
        )
        self.assertEqual(res_inst.returncode, 0, msg=res_inst.stderr or res_inst.stdout)
        install_doc = json.loads(res_inst.stdout)
        self.assertEqual(install_doc["status"], "INSTALLED")
        self.assertFalse((agents_root / "skills" / "synced").exists())

        for cdir in (claude_home, cursor_home, qoder_home):
            link = cdir / "skills" / "git-change-delivery"
            self.assertTrue(link.is_symlink(), msg=f"missing per-skill symlink in {cdir}")
            self.assertEqual(link.resolve(), (agents_root / "skills" / "git-change-delivery").resolve())
        self.assertFalse((codex_home / "skills" / "git-change-delivery").exists())

        gemini_cfg = json.loads((gemini_home / "config" / "skills.json").read_text(encoding="utf-8"))
        self.assertIn(str(agents_root / "skills"), gemini_cfg["paths"])

        res_chk = subprocess.run(
            [*common_args, "skill-check", *client_flags],
            capture_output=True,
            text=True,
            env=_clean_env(),
            check=False,
        )
        self.assertEqual(res_chk.returncode, 0, msg=res_chk.stderr or res_chk.stdout)
        chk_doc = json.loads(res_chk.stdout)
        self.assertEqual(chk_doc["status"], "PASS")
        for cname in ("codex", "claude", "gemini", "cursor", "qoder"):
            self.assertEqual(chk_doc["clients"][cname]["status"], "PASS")

    def test_wheel_installed_tool_skill_sync_doctor_and_source_files_upgrade(self) -> None:
        from git_finalizer.toolchain import tool_skill_sync as sync

        # 1. Offline uv-tool wheel install from checkout + doctor outside repository root.
        uv_tools = self.root / "uv-tools"
        uv_bin = self.root / "uv-bin"
        uv_bin.mkdir()
        install_env = {
            **_clean_env(),
            "UV_TOOL_DIR": str(uv_tools),
            "UV_TOOL_BIN_DIR": str(uv_bin),
            "TOOL_SKILL_SYNC_SOURCE_ROOT": str(ROOT.parent),
        }
        wheel_dir = self.root / "wheelhouse"
        wheel_dir.mkdir()
        build_res = subprocess.run(
            ["uv", "build", "--wheel", "--offline", "--out-dir", str(wheel_dir), str(ROOT)],
            capture_output=True,
            text=True,
            env=install_env,
            check=False,
        )
        self.assertEqual(build_res.returncode, 0, msg=build_res.stderr or build_res.stdout)
        wheels = list(wheel_dir.glob("*.whl"))
        self.assertEqual(len(wheels), 1, wheels)

        inst_res = subprocess.run(
            ["uv", "tool", "install", "--force", "--offline", "--no-build", str(wheels[0])],
            capture_output=True,
            text=True,
            env=install_env,
            check=False,
        )
        self.assertEqual(inst_res.returncode, 0, msg=inst_res.stderr or inst_res.stdout)

        # 1b. Also test without TOOL_SKILL_SYNC_SOURCE_ROOT or --source-repo from an external cwd.
        plain_venv = self.root / "plain-venv"
        subprocess.run([sys.executable, "-m", "venv", str(plain_venv)], check=True)
        subprocess.run(
            ["uv", "pip", "install", "--offline", "--no-index", "--python", str(plain_venv / "bin/python"), str(wheels[0])],
            check=True,
            capture_output=True,
            text=True,
        )
        no_env_doc = subprocess.run(
            [str(plain_venv / "bin/tool-skill-sync"), "doctor", "--repo", str(ROOT), "--summary"],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            env=_clean_env(),
            check=False,
        )
        self.assertIn(no_env_doc.returncode, (0, 1), msg=no_env_doc.stderr or no_env_doc.stdout)
        self.assertNotIn("venv", no_env_doc.stderr)
        self.assertEqual(json.loads(no_env_doc.stdout)["operation"], "doctor")

        doc_res = subprocess.run(
            [
                str(uv_bin / "tool-skill-sync"),
                "--source-repo",
                f"git-finalizer={ROOT}",
                "doctor",
                "--repo",
                str(ROOT),
                "--summary",
            ],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            env=install_env,
            check=False,
        )
        self.assertIn(doc_res.returncode, (0, 1), msg=doc_res.stderr or doc_res.stdout)
        self.assertNotIn("venv", doc_res.stderr)
        doc_json = json.loads(doc_res.stdout)
        self.assertEqual(doc_json["operation"], "doctor")

        # 2. End-to-end 1.x source_files -> 2.0.0 python_console_scripts upgrade + bundle activation.
        sources_root = self.root / "sources"
        sources_root.mkdir()
        gf_repo = sources_root / "git-finalizer"
        gf_repo.mkdir()
        shutil.copytree(ROOT / "src", gf_repo / "src")
        shutil.copytree(ROOT / "skills", gf_repo / "skills")
        shutil.copy2(ROOT / "pyproject.toml", gf_repo / "pyproject.toml")
        shutil.copy2(ROOT / "README.md", gf_repo / "README.md")
        shutil.copy2(ROOT / "LICENSE", gf_repo / "LICENSE")
        shutil.copy2(ROOT / "toolchain_compatibility.json", gf_repo / "toolchain_compatibility.json")
        shutil.copy2(ROOT / "tool_cli_contract.json", gf_repo / "tool_cli_contract.json")

        for rel, ver in (
            ("git-finalize", "1.9.9"),
            ("tool-skill-sync", "1.9.9"),
            ("tool-temp-dir", "1.9.9"),
        ):
            p = gf_repo / rel
            p.write_text(f"#!/usr/bin/env sh\nVERSION='{ver}'\necho '{rel} {ver}'\n", encoding="utf-8")
            p.chmod(0o755)
        (gf_repo / "legacy_sidecar.py").write_text("# legacy helper\n", encoding="utf-8")
        (gf_repo / "tooling").mkdir()
        (gf_repo / "tooling" / "tool_skill_sync.py").write_text('VERSION = "1.9.9"\n', encoding="utf-8")

        legacy_contract = json.loads((ROOT / "tool_cli_contract.json").read_text(encoding="utf-8"))
        legacy_contract["tool_version"] = "1.9.9"
        (gf_repo / "tool_cli_contract.json").write_bytes(sync.canonical_json(legacy_contract))
        legacy_manifest = {
            "schema_version": 2,
            "tool_name": "git-finalizer",
            "tool_version": "1.9.9",
            "tool_commit": "@release",
            "compatible_toolchain_contract_version": 6,
            "skill_contract_version": 2,
            "canonical_skill": {"owner": "git-finalizer", "path": "skills/git-change-delivery/SKILL.md"},
            "canonical_skill_sha256": sync.tree_sha256(gf_repo / "skills/git-change-delivery", sync.SKILL_PAYLOAD),
            "compatibility_skill": {
                "name": sync.COMPATIBILITY_SKILL_NAME,
                "path": f"skills/{sync.COMPATIBILITY_SKILL_NAME}/SKILL.md",
                "sha256": sync.sha256_file(gf_repo / f"skills/{sync.COMPATIBILITY_SKILL_NAME}/SKILL.md"),
            },
            "public_cli_contract_sha256": sync.sha256_file(gf_repo / "tool_cli_contract.json"),
            "executable": {
                "kind": "source_files",
                "entrypoints": ["git-finalize", "tool-skill-sync", "tool-temp-dir"],
                "paths": [
                    "git-finalize",
                    "tool-skill-sync",
                    "tool-temp-dir",
                    "legacy_sidecar.py",
                    "tooling/tool_skill_sync.py",
                ],
                "production_targets": {
                    "git-finalize": "bin/git-finalize",
                    "tool-skill-sync": "bin/tool-skill-sync",
                    "tool-temp-dir": "bin/tool-temp-dir",
                    "legacy_sidecar.py": "bin/legacy_sidecar.py",
                    "tooling/tool_skill_sync.py": "bin/tool-skill-sync-support/tool_skill_sync.py",
                },
            },
            "install_targets": {
                "skill_directory": "git-change-delivery",
                "stable_entries": ["git-finalize", "tool-skill-sync", "tool-temp-dir"],
            },
        }
        (gf_repo / "tool_skill_manifest.json").write_bytes(sync.canonical_json(legacy_manifest))
        for file_path in (gf_repo / "skills").rglob("*"):
            if file_path.is_file():
                file_path.chmod(file_path.stat().st_mode & ~0o022)
        git_env = _clean_env()
        subprocess.run(["git", "-C", str(gf_repo), "init", "-q", "-b", "main"], env=git_env, check=True)
        subprocess.run(["git", "-C", str(gf_repo), "config", "user.name", "Test"], env=git_env, check=True)
        subprocess.run(["git", "-C", str(gf_repo), "config", "user.email", "t@example.invalid"], env=git_env, check=True)
        subprocess.run(["git", "-C", str(gf_repo), "add", "-A"], env=git_env, check=True)
        subprocess.run(["git", "-C", str(gf_repo), "commit", "-qm", "legacy"], env=git_env, check=True)

        install_root = self.root / "sync-state"
        agents_root = self.root / "agents"
        agents_root.mkdir()
        bin_dir = self.root / "prod-bin"
        bin_dir.mkdir()
        hooks_dir = self.root / "hooks"
        hooks_dir.mkdir()
        for rel in ("git-finalize", "tool-skill-sync", "tool-temp-dir", "legacy_sidecar.py"):
            shutil.copy2(gf_repo / rel, bin_dir / rel)
        support_dir = bin_dir / "tool-skill-sync-support"
        support_dir.mkdir()
        shutil.copy2(gf_repo / "tooling" / "tool_skill_sync.py", support_dir / "tool_skill_sync.py")
        sync.install(
            sync.Sources(sources_root),
            "git-finalizer",
            install_root,
            allow_dirty_source=False,
            activate_production=True,
            agents_root=agents_root,
            bin_dir=bin_dir,
            hooks_dir=hooks_dir,
        )
        self.assertTrue((bin_dir / "legacy_sidecar.py").is_file())

        # Transition fake checkout to 2.0.0 python_console_scripts.
        for name in ("git-finalize", "tool-skill-sync", "tool-temp-dir", "legacy_sidecar.py"):
            shutil.copy2(ROOT / name, gf_repo / name) if (ROOT / name).exists() else (gf_repo / name).unlink()
        shutil.rmtree(gf_repo / "tooling")
        shutil.copy2(ROOT / "tool_cli_contract.json", gf_repo / "tool_cli_contract.json")
        shutil.copy2(ROOT / "tool_skill_manifest.json", gf_repo / "tool_skill_manifest.json")
        subprocess.run(["git", "-C", str(gf_repo), "add", "-A"], env=git_env, check=True)
        subprocess.run(["git", "-C", str(gf_repo), "commit", "-qm", "v2"], env=git_env, check=True)

        sources_v2 = sync.Sources(sources_root)
        up_env = {**_clean_env(), "UV_TOOL_DIR": str(self.root / "uv-tools-v2")}
        with mock.patch.dict(os.environ, up_env):
            up = sync.upgrade(sources_v2, "git-finalizer", bin_dir=bin_dir)
        self.assertEqual(up["status"], "UPGRADED")
        self.assertEqual(up["previous_version"], "1.9.9")
        self.assertEqual(up["target_version"], "2.0.0")

        # Activate production bundle: obsolete legacy_sidecar.py retires while new console scripts remain intact.
        with mock.patch.dict(os.environ, up_env):
            act = sync.install(
                sources_v2,
                "git-finalizer",
                install_root,
                allow_dirty_source=False,
                activate_production=True,
                agents_root=agents_root,
                bin_dir=bin_dir,
                hooks_dir=hooks_dir,
            )
        self.assertEqual(act["status"], "INSTALLED")
        self.assertFalse((bin_dir / "legacy_sidecar.py").exists())
        self.assertFalse((support_dir / "tool_skill_sync.py").exists())
        for name in ("git-finalize", "tool-skill-sync", "tool-temp-dir"):
            self.assertTrue((bin_dir / name).is_symlink(), f"active console script deleted: {name}")
            ver = subprocess.run([str(bin_dir / name), "--version"], capture_output=True, text=True, check=False)
            self.assertEqual(ver.returncode, 0, msg=ver.stderr)
            self.assertIn("2.0.0", ver.stdout)


if __name__ == "__main__":
    unittest.main()
