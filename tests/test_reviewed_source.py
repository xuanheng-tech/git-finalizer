from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ReviewedSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="finalizer-source-review-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.env = {
            "PATH": os.environ["PATH"],
            "HOME": str(self.base),
            "LC_ALL": "C",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
        }
        self.git("init", "-q", "--initial-branch=main")
        self.git("config", "user.name", "Synthetic Source Reviewer")
        self.git("config", "user.email", "review@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        (self.repo / "README.md").write_text("Synthetic repository\n")
        self.git("add", "README.md")
        self.git("commit", "-qm", "Synthetic baseline")
        self.path = "credential_transfer.py"
        self.source = self.repo / self.path
        self.source.write_text('def role():\n    return "production"\n')
        self.evidence = self.base / "evidence.md"
        self.evidence.write_text(
            "Reviewed synthetic production-only source. No credentials.\n"
        )
        self.evidence.chmod(0o600)
        self.file = self.base / "review.json"
        now = datetime.now(timezone.utc)
        self.review = {
            "schema_version": 1,
            "kind": "reviewed-sensitive-source",
            "repository": {
                "common_dir": str(self.repo / ".git"),
                "root_commit": self.git("rev-parse", "HEAD"),
            },
            "allocation": {
                "repository_id": "synthetic-repository",
                "allocation_id": "synthetic-allocation",
                "task_key": "source-review",
                "authority_key": "source-review",
                "worktree_path": str(self.repo),
            },
            "scope_sha256": sha((self.path + "\n").encode()),
            "reviewed_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=1)).isoformat(),
            "reviews": [
                {
                    "path": self.path,
                    "sha256": sha(self.source.read_bytes()),
                    "purpose": "Review exact synthetic credential-handling source",
                    "evidence": {
                        "path": str(self.evidence),
                        "sha256": sha(self.evidence.read_bytes()),
                    },
                }
            ],
        }
        self.write_review()

    def git(self, *arguments: str) -> str:
        return (
            subprocess.check_output(
                ["git", "-C", str(self.repo), *arguments],
                env=self.env,
                stderr=subprocess.PIPE,
            )
            .decode()
            .strip()
        )

    def write_review(self) -> None:
        self.file.write_text(json.dumps(self.review) + "\n")
        self.file.chmod(0o600)

    def replace_source(self, path: str, content: bytes) -> None:
        self.source.unlink()
        self.path = path
        self.source = self.repo / path
        self.source.parent.mkdir(parents=True, exist_ok=True)
        self.source.write_bytes(content)
        self.review["scope_sha256"] = sha((path + "\n").encode())
        self.review["reviews"][0].update(path=path, sha256=sha(content))
        self.write_review()

    def state(self) -> tuple[str, str, str]:
        return (
            self.git("rev-parse", "HEAD"),
            self.git("ls-files", "--stage"),
            self.git("status", "--porcelain=v1", "--untracked-files=all"),
        )

    def run_finalizer(
        self,
        mode: str | None = "verify-only",
        *,
        approved: bool = True,
        paths: tuple[str, ...] | None = None,
        extra: tuple[str, ...] = (),
    ) -> tuple[int, dict]:
        before = self.state()
        command = [
            str(ROOT / "git-finalize"),
            "--summary",
            "--repo",
            str(self.repo),
            "--repository-id",
            "synthetic-repository",
            "--allocation-id",
            "synthetic-allocation",
            "--task-key",
            "source-review",
            "--authority-key",
            "source-review",
        ]
        existing_branch = mode == "publish-existing-branch"
        if existing_branch:
            command += [
                "--publish-existing-branch",
                before[0],
                "--remote",
                "origin",
                "--remote-branch",
                "feature/reviewed-source",
            ]
        elif mode is not None:
            command += ["--mode", mode]
        if mode != "verify-only" and not existing_branch:
            command += ["--message", "Review synthetic source"]
        if approved:
            command += ["--reviewed-sensitive-source", str(self.file)]
        command += list(extra)
        if not existing_branch or approved or paths is not None:
            command += ["--", *(paths or (self.path,))]
        result = subprocess.run(
            command,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        data = json.loads(result.stdout)
        if mode is not None and not existing_branch:
            self.assertFalse(data["push"]["executed"])
        if mode == "verify-only" or existing_branch:
            self.assertEqual(before, self.state())
        return result.returncode, data

    def prepare_branch_publication(self) -> Path:
        remote = self.base / "remote.git"
        self.git("init", "-q", "--bare", "--initial-branch=main", str(remote))
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "-qu", "origin", "main")
        self.git("switch", "-qc", "feature/reviewed-source")
        return remote

    def commit_fixture(self) -> None:
        self.git("add", "--", self.path)
        self.git("commit", "-qm", "Synthetic publication fixture")

    def assert_publish_blocked(self, **kwargs) -> dict:
        status, data = self.run_finalizer("publish-existing-branch", **kwargs)
        self.assertNotEqual(status, 0, data)
        self.assertEqual(data["status"], "blocked")
        self.assertFalse(data["push"]["executed"])
        self.assertFalse(data["commit"]["created"])
        self.assertEqual(
            self.git("ls-remote", "origin", "refs/heads/feature/reviewed-source"),
            "",
        )
        return data

    def assert_blocked(self, **kwargs) -> dict:
        for mode in ("verify-only", "commit-only"):
            with self.subTest(mode=mode):
                before = self.state()
                status, data = self.run_finalizer(mode, **kwargs)
                self.assertNotEqual(status, 0, data)
                self.assertEqual(data["status"], "blocked")
                self.assertEqual(before, self.state())
        return data

    def test_exact_review_verifies_and_commits_with_receipt(self) -> None:
        for mode in ("verify-only", "commit-only"):
            status, data = self.run_finalizer(mode)
            self.assertEqual(status, 0, data)
            (receipt,) = data["reviewed_sensitive_sources"]
            self.assertEqual(receipt["sha256"], sha(self.source.read_bytes()))
            self.assertEqual(
                receipt["review_id"], "sha256:" + sha(self.file.read_bytes())
            )
            self.assertEqual(receipt["allocation"], self.review["allocation"])
            self.assertEqual(receipt["content_scan"], "passed")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_missing_review_blocks(self) -> None:
        self.assert_blocked(approved=False)

    def test_commit_review_publishes_same_exact_existing_branch(self) -> None:
        remote = self.prepare_branch_publication()
        receipts = []
        for mode in ("verify-only", "commit-only", "publish-existing-branch"):
            status, data = self.run_finalizer(mode)
            self.assertEqual(status, 0, data)
            receipts.append(data["reviewed_sensitive_sources"])
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual(receipts[1], receipts[2])
        self.assertTrue(data["push"]["executed"])
        self.assertFalse(data["commit"]["created"])
        self.assertEqual(self.git("rev-list", "--count", "HEAD"), "2")
        self.assertEqual(
            self.git("rev-parse", "HEAD"),
            self.git(
                "--git-dir=" + str(remote), "rev-parse", "feature/reviewed-source"
            ),
        )
        self.assertEqual(self.git("rev-parse", "HEAD"), self.git("rev-parse", "@{u}"))

    def test_design_token_batch_verifies_commits_and_publishes(self) -> None:
        self.source.unlink()
        css = b":root { --ds-color-primary: #1362d4; --ds-space-base: 8px; }\n"
        document = b'{"color":{"primary":{"$value":"#1362d4","$type":"color"}}}\n'
        registry = b'{"color.primary":{"status":"active","category":"color"}}\n'
        sources = {
            "tokens/tokens.css": css,
            "tokens/tokens.json": document,
            "tokens/registry.json": registry,
            "dist/tokens.css": css,
            "dist/tokens.json": document,
            "dist/registry.json": registry,
        }
        paths = tuple(sorted(sources))
        template = self.review["reviews"][0]
        self.review["reviews"] = []
        for path, content in sources.items():
            source = self.repo / path
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(content)
            if path != "dist/registry.json":
                self.review["reviews"].append(
                    {**template, "path": path, "sha256": sha(content)}
                )
        self.review["scope_sha256"] = sha(("\n".join(paths) + "\n").encode())
        self.write_review()
        remote = self.prepare_branch_publication()
        receipts = []
        for mode in ("verify-only", "commit-only", "publish-existing-branch"):
            status, data = self.run_finalizer(mode, paths=paths)
            self.assertEqual(status, 0, data)
            receipts.append(data["reviewed_sensitive_sources"])
            self.assertEqual(len(receipts[-1]), 5)
            for receipt in receipts[-1]:
                self.assertEqual(receipt["sha256"], sha(sources[receipt["path"]]))
                self.assertEqual(receipt["content_scan"], "passed")
        self.assertEqual(receipts[0], receipts[1])
        self.assertEqual(receipts[1], receipts[2])
        self.assertTrue(data["push"]["executed"])
        self.assertEqual(
            self.git("rev-parse", "HEAD"),
            self.git("--git-dir=" + str(remote), "rev-parse", "feature/reviewed-source"),
        )

    def test_design_json_normal_publication_keeps_remote_verification(self) -> None:
        self.replace_source("tokens/tokens.json", b'{"color":{"$value":"#1362d4"}}\n')
        remote = self.base / "remote.git"
        self.git("init", "-q", "--bare", "--initial-branch=main", str(remote))
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "-qu", "origin", "main")
        status, data = self.run_finalizer(None)
        self.assertEqual(status, 0, data)
        self.assertEqual(
            self.git("rev-parse", "HEAD"),
            self.git("--git-dir=" + str(remote), "rev-parse", "main"),
        )
        self.assertEqual(data["reviewed_sensitive_sources"][0]["content_scan"], "passed")

    def test_design_sources_require_review_and_keep_all_secret_detectors(self) -> None:
        examples = (
            "gh" + "p_" + "X" * 32,
            "-" * 5 + "BEGIN PRIVATE KEY" + "-" * 5,
            "ssh-" + "rsa " + "X" * 48,
            "pass" + 'word = "synthetic-blocked-value"',
        )
        for suffix in ("css", "json"):
            with self.subTest(suffix=suffix):
                safe = (
                    b":root { --ds-space-base: 8px; }\n"
                    if suffix == "css" else b'{"spacing":8}\n'
                )
                self.replace_source("dist/tokens." + suffix, safe)
                self.assert_blocked(approved=False)
                for index, signature in enumerate(examples):
                    if suffix == "css":
                        content = ("/* " + signature + " */\n").encode()
                    else:
                        value = (
                            {"pass" + "word": "synthetic-blocked-value"}
                            if index == 3 else {"note": signature}
                        )
                        content = json.dumps(value).encode()
                    self.source.write_bytes(content)
                    self.review["reviews"][0]["sha256"] = sha(content)
                    self.write_review()
                    data = self.assert_blocked()
                    self.assertNotIn(signature, json.dumps(data))
                    self.assertNotEqual(
                        data["reviewed_sensitive_sources"][0]["content_scan"], "passed"
                    )

    def test_design_json_escapes_cannot_hide_secret_content(self) -> None:
        examples = (
            ("github" + "_pat_" + "X" * 32, b"github_pat_", b"github\\u005fpat_"),
            ("-----BEGIN " + "PRIVATE KEY-----", b"PRIVATE", b"PRIV\\u0041TE"),
            ("ssh-" + "ed25519 " + "X" * 48, b"ssh-", b"ss\\u0068-"),
        )
        for signature, literal, escaped in examples:
            with self.subTest(signature=signature[:12]):
                content = json.dumps({"note": signature}).encode().replace(literal, escaped)
                self.replace_source("dist/tokens.json", content)
                self.assert_blocked()
        content = b'{"pass\\u0077ord":"synthetic-blocked-value"}'
        self.replace_source("dist/tokens.json", content)
        self.assert_blocked()

    def test_design_json_decoded_signatures_block_publication_history(self) -> None:
        content = json.dumps({"note": "ssh-" + "ed25519 " + "X" * 48}).encode()
        self.replace_source("dist/tokens.json", content)
        self.prepare_branch_publication()
        self.commit_fixture()
        self.assert_publish_blocked()

    def test_design_review_rejects_private_key_env_and_unsupported_paths(self) -> None:
        for path in (
            "tokens/private-key.css", "tokens/ssh_key.json",
            "tokens/.env.json", "dist/tokens.txt",
        ):
            with self.subTest(path=path):
                content = b"{}\n" if path.endswith(".json") else b":root { --color: red; }\n"
                self.replace_source(path, content)
                self.assert_blocked()

    def test_design_review_rejects_invalid_text_and_json(self) -> None:
        examples = (
            ("css", b"\xff"),
            ("css", b":root { --color: red; }\x00"),
            ("json", b"{"),
            ("json", b'{"color":1,"color":2}'),
            ("json", b'{"color":NaN}'),
        )
        for suffix, content in examples:
            with self.subTest(suffix=suffix, content=content):
                self.replace_source("dist/tokens." + suffix, content)
                self.assert_blocked()

    def test_design_publication_rejects_unreviewed_historical_css(self) -> None:
        content = b":root { --ds-color-primary: red; }\n"
        self.replace_source("dist/tokens.css", content)
        self.prepare_branch_publication()
        self.source.write_bytes(content + b"/* earlier unreviewed revision */\n")
        self.commit_fixture()
        self.source.write_bytes(content)
        self.commit_fixture()
        data = self.assert_publish_blocked()
        self.assertIn("history blob mismatch", json.dumps(data))

    def test_publish_missing_review_and_scope_block(self) -> None:
        self.prepare_branch_publication()
        self.commit_fixture()
        self.assert_publish_blocked(approved=False)
        self.assert_publish_blocked(paths=(self.path, "README.md"))
        self.assert_publish_blocked(extra=("--fixture-exceptions", str(self.file)))

    def test_publish_review_identity_and_validity_match_commit_validation(self) -> None:
        self.prepare_branch_publication()
        original = copy.deepcopy(self.review)
        changes = (
            ("repository", "common_dir", str(self.base / "other.git")),
            ("repository", "root_commit", "0" * 40),
            ("allocation", "repository_id", "other-repository"),
            ("allocation", "allocation_id", "other-allocation"),
            ("allocation", "task_key", "other-task"),
            ("allocation", "authority_key", "other-authority"),
            ("allocation", "worktree_path", str(self.base / "other")),
        )
        for section, key, value in changes:
            with self.subTest(section=section, key=key):
                self.review = copy.deepcopy(original)
                self.review[section][key] = value
                self.write_review()
                self.assert_blocked()
        self.review = copy.deepcopy(original)
        self.write_review()
        status, data = self.run_finalizer("commit-only")
        self.assertEqual(status, 0, data)
        for section, key, value in changes:
            with self.subTest(section=section, key=key, mode="publish"):
                self.review = copy.deepcopy(original)
                self.review[section][key] = value
                self.write_review()
                self.assert_publish_blocked()
        self.review = copy.deepcopy(original)
        now = datetime.now(timezone.utc)
        self.review["reviewed_at"] = (now - timedelta(days=2)).isoformat()
        self.review["expires_at"] = (now - timedelta(days=1)).isoformat()
        self.write_review()
        self.assert_publish_blocked()
        self.review = copy.deepcopy(original)
        self.review["reviews"][0]["purpose"] = ""
        self.write_review()
        self.assert_publish_blocked()

    def test_publish_missing_or_changed_evidence_blocks(self) -> None:
        self.prepare_branch_publication()
        self.commit_fixture()
        self.evidence.unlink()
        self.assert_publish_blocked()
        self.evidence.write_text("Changed evidence\n")
        self.assert_publish_blocked()

    def test_publish_same_path_changed_content_blocks(self) -> None:
        self.prepare_branch_publication()
        status, data = self.run_finalizer("commit-only")
        self.assertEqual(status, 0, data)
        self.source.write_text(self.source.read_text() + "# changed\n")
        self.commit_fixture()
        self.assert_publish_blocked()

    def test_publish_different_path_cannot_reuse_review(self) -> None:
        self.prepare_branch_publication()
        self.source.rename(self.repo / "credential_other.py")
        self.git("add", "credential_other.py")
        self.git("commit", "-qm", "Synthetic different path")
        self.assert_publish_blocked(paths=("credential_other.py",))

    def test_publish_unreviewed_history_blob_blocks(self) -> None:
        self.prepare_branch_publication()
        reviewed_content = self.source.read_bytes()
        self.source.write_bytes(reviewed_content + b"# unreviewed old version\n")
        self.commit_fixture()
        self.source.write_bytes(reviewed_content)
        self.commit_fixture()
        data = self.assert_publish_blocked()
        self.assertIn("history blob mismatch", json.dumps(data))

    def test_publish_scans_all_paths_beyond_review_scope(self) -> None:
        self.prepare_branch_publication()
        self.commit_fixture()
        (self.repo / "credential_other.py").write_text("# unreviewed\n")
        self.git("add", "credential_other.py")
        self.git("commit", "-qm", "Synthetic other path")
        self.assert_publish_blocked()

    def test_publish_secret_content_blocks_even_with_exact_review(self) -> None:
        self.prepare_branch_publication()
        examples = (
            "gh" + "p_" + "X" * 32,
            "-" * 5 + "BEGIN PRIVATE KEY" + "-" * 5,
            "ssh-" + "rsa " + "X" * 48,
            "password" + ' = "synthetic-blocked-value"',
        )
        for content in examples:
            with self.subTest(detector=examples.index(content)):
                self.source.write_text("# " + content + "\n")
                self.review["reviews"][0]["sha256"] = sha(self.source.read_bytes())
                self.write_review()
                self.commit_fixture()
                data = self.assert_publish_blocked()
                self.assertNotIn(content, json.dumps(data))
                self.assertNotEqual(
                    data["reviewed_sensitive_sources"][0]["content_scan"], "passed"
                )

    def test_normal_publish_keeps_review_and_remote_verification(self) -> None:
        remote = self.base / "remote.git"
        self.git("init", "-q", "--bare", "--initial-branch=main", str(remote))
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "-qu", "origin", "main")
        status, data = self.run_finalizer(None)
        self.assertEqual(status, 0, data)
        self.assertTrue(data["push"]["executed"])
        self.assertEqual(
            self.git("rev-parse", "HEAD"),
            self.git("--git-dir=" + str(remote), "rev-parse", "main"),
        )
        self.assertEqual(
            data["reviewed_sensitive_sources"][0]["content_scan"], "passed"
        )

    def test_same_path_modified_blocks(self) -> None:
        self.source.write_text(self.source.read_text() + "# changed\n")
        self.assert_blocked()

    def test_different_path_blocks(self) -> None:
        other = "credential_other.py"
        (self.repo / other).write_bytes(self.source.read_bytes())
        self.review["scope_sha256"] = sha((other + "\n").encode())
        self.write_review()
        self.assert_blocked(paths=(other,))

    def test_different_repository_blocks(self) -> None:
        self.review["repository"]["common_dir"] = str(self.base / "other.git")
        self.write_review()
        self.assert_blocked()

    def test_repository_root_mismatch_blocks(self) -> None:
        self.review["repository"]["root_commit"] = "0" * 40
        self.write_review()
        self.assert_blocked()

    def test_scope_and_allocation_mismatch_block(self) -> None:
        self.assert_blocked(paths=(self.path, "README.md"))
        self.review["allocation"]["allocation_id"] = "other-allocation"
        self.write_review()
        self.assert_blocked()

    def test_missing_and_changed_evidence_block(self) -> None:
        self.evidence.unlink()
        self.assert_blocked()
        self.evidence.write_text("Different review\n")
        self.assert_blocked()

    def test_missing_purpose_blocks(self) -> None:
        self.review["reviews"][0]["purpose"] = ""
        self.write_review()
        self.assert_blocked()

    def test_expired_and_overlong_review_block(self) -> None:
        now = datetime.now(timezone.utc)
        self.review["reviewed_at"] = (now - timedelta(days=2)).isoformat()
        self.review["expires_at"] = (now - timedelta(days=1)).isoformat()
        self.write_review()
        self.assert_blocked()
        self.review["expires_at"] = (now + timedelta(days=7)).isoformat()
        self.write_review()
        self.assert_blocked()

    def test_all_secret_detectors_remain_mandatory(self) -> None:
        examples = [
            "gh" + "p_" + "X" * 32,
            "-" * 5 + "BEGIN PRIVATE KEY" + "-" * 5,
            "ssh-" + "rsa " + "X" * 48,
            "password" + ' = "synthetic-blocked-value"',
        ]
        for content in examples:
            with self.subTest(detector=examples.index(content)):
                self.source.write_text("# " + content + "\n")
                self.review["reviews"][0]["sha256"] = sha(self.source.read_bytes())
                self.write_review()
                data = self.assert_blocked()
                self.assertNotIn(content, json.dumps(data))
                self.assertNotEqual(
                    data["reviewed_sensitive_sources"][0]["content_scan"], "passed"
                )

    def test_content_exceptions_cannot_combine(self) -> None:
        self.assert_blocked(extra=("--allow-test-fixture", self.path))

    def test_repository_authored_and_writable_review_block(self) -> None:
        self.file.chmod(0o666)
        self.assert_blocked()
        self.file = self.repo / "review.json"
        self.write_review()
        self.assert_blocked()

    def test_symlink_and_invalid_python_block(self) -> None:
        body = self.source.read_bytes()
        self.source.unlink()
        self.source.symlink_to(self.evidence)
        self.assert_blocked()
        self.source.unlink()
        self.source.write_bytes(body + b"invalid python !!!\n")
        self.review["reviews"][0]["sha256"] = sha(self.source.read_bytes())
        self.write_review()
        self.assert_blocked()

    def test_git_clean_filter_hash_change_blocks_before_commit(self) -> None:
        attributes = self.repo / ".git/info/attributes"
        attributes.write_text(self.path + " filter=synthetic\n")
        self.git("config", "filter.synthetic.clean", "sed s/production/changed/")
        before = self.git("rev-parse", "HEAD")
        status, data = self.run_finalizer("commit-only")
        self.assertNotEqual(status, 0, data)
        self.assertFalse(data["commit"]["created"])
        self.assertEqual(before, self.git("rev-parse", "HEAD"))

    def test_commit_hook_change_cannot_reuse_review(self) -> None:
        hook = self.repo / ".git/hooks/pre-commit"
        hook.write_text(
            "#!/bin/sh\nprintf '# hook changed\\n' >> "
            + self.path
            + "\ngit add -- "
            + self.path
            + "\n"
        )
        hook.chmod(0o700)
        status, data = self.run_finalizer("commit-only")
        self.assertNotEqual(status, 0, data)
        self.assertTrue(data["commit"]["created"])
        self.assertFalse(data["push"]["executed"])
        self.assertEqual(self.git("rev-parse", "HEAD"), data["commit"]["sha"])


if __name__ == "__main__":
    unittest.main()
