from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class PublishedBranchSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="gf-published-sync-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.source = self.root / "source.git"
        self.target = self.root / "target.git"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "push.jsonl"
        self.env = {
            "PATH": str(self.bin) + ":" + os.defpath,
            "LANG": "C.UTF-8", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
            "TMPDIR": str(self.root),
        }
        self.git("init", "-q", "--initial-branch=master", str(self.repo), cwd=self.root)
        self.git("config", "user.name", "Synthetic Sync Author")
        self.git("config", "user.email", "sync@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        self.base = self.commit("f.txt", "base\n")
        self.tip = self.commit("f.txt", "published\n")
        for remote in (self.source, self.target):
            self.git("init", "-q", "--bare", "--initial-branch=master", str(remote), cwd=self.root)
            self.git("fetch", "-q", "--no-tags", str(self.repo), self.tip, cwd=remote)
        self.git("update-ref", "refs/heads/master", self.tip, cwd=self.source)
        self.git("update-ref", "refs/heads/master", self.base, cwd=self.target)
        self.git("remote", "add", "source", str(self.source))
        self.git("remote", "add", "mirror", str(self.target))
        self.git("update-ref", "refs/remotes/source/master", self.tip)
        self.git("update-ref", "refs/remotes/mirror/master", self.base)
        self.git("branch", "--set-upstream-to=source/master", "master")
        self.install_probe()

    def git(self, *arguments: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["/usr/bin/git", "-C", str(cwd or self.repo), *arguments],
            env=self.env, capture_output=True, text=True, check=True, timeout=20,
        )
        return result.stdout.strip()

    def commit(self, path: str, content: str) -> str:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.git("add", "-f", "--", path)
        self.git("commit", "-qm", "synthetic fixture")
        return self.git("rev-parse", "HEAD")

    def install_probe(self) -> None:
        script = self.bin / "git"
        script.write_text("#!/usr/bin/python3\n" + r'''import json, os, subprocess, sys
from pathlib import Path
args = sys.argv[1:]
mode = os.environ.get("SYNC_PROBE_MODE", "")
if "push" in args:
    with Path(os.environ["SYNC_PROBE_LOG"]).open("a") as stream:
        stream.write(json.dumps(args) + "\n")
    if mode == "push_refused":
        sys.exit(1)
    result = subprocess.run(["/usr/bin/git", *args])
    sys.exit(1 if mode == "uncertain_after_push" else result.returncode)
result = subprocess.run(["/usr/bin/git", *args])
if result.returncode == 0 and "fetch" in args:
    if mode == "source_drift":
        subprocess.run(["/usr/bin/git", "-C", os.environ["SYNC_SOURCE"], "update-ref",
                        "refs/heads/master", os.environ["SYNC_BASE"]], check=True)
    elif mode == "target_drift":
        subprocess.run(["/usr/bin/git", "-C", os.environ["SYNC_TARGET"], "update-ref",
                        "refs/heads/master", os.environ["SYNC_TIP"]], check=True)
    elif mode == "upstream_drift":
        subprocess.run(["/usr/bin/git", "-C", os.environ["SYNC_REPO"], "config",
                        "branch.master.remote", "mirror"], check=True)
sys.exit(result.returncode)
''', encoding="utf-8")
        script.chmod(0o700)
        self.env.update({
            "SYNC_PROBE_LOG": str(self.log), "SYNC_SOURCE": str(self.source),
            "SYNC_TARGET": str(self.target), "SYNC_REPO": str(self.repo),
            "SYNC_BASE": self.base, "SYNC_TIP": self.tip,
        })

    def run_sync(self, *extra: str, oid: str | None = None,
                 expected: str | None = None, source: str = "source",
                 target: str = "mirror", branch: str = "master") -> tuple[int, dict]:
        result = subprocess.run(
            [str(ROOT / "git-finalize"), "--summary", "--sync-published-branch", oid or self.tip,
             "--source-remote", source, "--remote", target, "--remote-branch", branch,
             "--expected-target-oid", expected or self.base, "--repo", str(self.repo), *extra],
            env=self.env, capture_output=True, text=True, timeout=90, check=False,
        )
        self.assertTrue(result.stdout.strip(), result.stderr)
        return result.returncode, json.loads(result.stdout)

    def target_oid(self) -> str:
        return self.git("rev-parse", "refs/heads/master", cwd=self.target)

    def assert_blocked(self, *extra: str, **kwargs) -> dict:
        status, result = self.run_sync(*extra, **kwargs)
        self.assertNotEqual(status, 0, result)
        self.assertFalse(result["push"]["executed"], result)
        self.assertFalse(self.log.exists())
        self.assertEqual(self.target_oid(), self.base)
        return result

    def prepare_reviewed_history(self) -> None:
        marker = "-" * 5 + "BEGIN PRIVATE KEY" + "-" * 5
        self.old_changelog = "# Changelog\n\n## Older\n\n- Reject `" + marker + "` literals.\n"
        self.old_reasons = (
            'BODY_REFUSAL_REASONS = {\n'
            '    "content contains credential": "content is refused by policy",\n'
            '}\n'
        )
        self.commit("CHANGELOG.md", self.old_changelog)
        self.base = self.commit("reasons.py", self.old_reasons)
        self.git("fetch", "-q", "--no-tags", str(self.repo), self.base, cwd=self.target)
        self.git("update-ref", "refs/heads/master", self.base, cwd=self.target)
        self.git("update-ref", "refs/remotes/mirror/master", self.base)
        self.commit("CHANGELOG.md", "## Newer\n\n- Ordinary update.\n\n" + self.old_changelog)
        self.tip = self.commit("reasons.py", self.old_reasons + "\nORDINARY_CHANGE = True\n")
        self.publish_review_source()

    def publish_review_source(self) -> None:
        self.git("fetch", "-q", "--no-tags", str(self.repo), self.tip, cwd=self.source)
        self.git("update-ref", "refs/heads/master", self.tip, cwd=self.source)
        self.git("update-ref", "refs/remotes/source/master", self.tip)
        self.env.update({"SYNC_BASE": self.base, "SYNC_TIP": self.tip})

    def exact_history_review(self) -> Path:
        entries = []
        for path, detector in (
            ("CHANGELOG.md", "private-key-header-v1"),
            ("reasons.py", "credential-assignment-v1"),
        ):
            entries.append({
                "path": path,
                "blob_oid": self.git("rev-parse", self.tip + ":" + path),
                "sha256": hashlib.sha256((self.repo / path).read_bytes()).hexdigest(),
                "detector": detector,
                "reason": "Synthetic inherited header or refusal prose reviewed against mirror baseline",
            })
        review = self.root / "review.json"
        review.write_text(json.dumps({
            "schema_version": 1,
            "repository": {
                "root_commit": self.git("rev-list", "--max-parents=0", self.tip),
                "remote_url_sha256": hashlib.sha256(str(self.target).encode()).hexdigest(),
            },
            "exceptions": entries,
        }), encoding="utf-8")
        return review

    def test_sync_reviews_inherited_literals_against_target_baseline(self) -> None:
        self.prepare_reviewed_history()
        before = self.git("for-each-ref", "--format=%(refname) %(objectname) %(upstream)", "refs/heads", "refs/tags")
        status, result = self.run_sync("--fixture-exceptions", str(self.exact_history_review()))
        self.assertEqual(status, 0, result)
        self.assertEqual(self.target_oid(), self.tip)
        self.assertTrue(result["mode_result"]["local_head_unchanged"])
        self.assertTrue(result["mode_result"]["local_upstream_unchanged"])
        self.assertEqual(self.git("for-each-ref", "--format=%(refname) %(objectname) %(upstream)", "refs/heads", "refs/tags"), before)
        self.assertEqual(len(result["fixture_exceptions"]), 2)

    def test_reviewed_history_dry_run_does_not_push(self) -> None:
        self.prepare_reviewed_history()
        status, result = self.run_sync("--dry-run", "--fixture-exceptions", str(self.exact_history_review()))
        self.assertEqual(status, 0, result)
        self.assertEqual(len(result["fixture_exceptions"]), 2)
        self.assertFalse(result["push"]["executed"])
        self.assertFalse(self.log.exists())
        self.assertEqual(self.target_oid(), self.base)

    def test_exact_review_does_not_allow_new_quoted_header(self) -> None:
        self.prepare_reviewed_history()
        self.tip = self.commit("CHANGELOG.md", self.old_changelog + self.old_changelog)
        self.publish_review_source()
        result = self.assert_blocked("--fixture-exceptions", str(self.exact_history_review()))
        self.assertIn("fixture exception", result["reason"])

    def test_exact_review_does_not_allow_changed_refusal_prose(self) -> None:
        self.prepare_reviewed_history()
        self.tip = self.commit("reasons.py", self.old_reasons.replace(
            "content is refused by policy", "content has changed refusal prose"))
        self.publish_review_source()
        result = self.assert_blocked("--fixture-exceptions", str(self.exact_history_review()))
        self.assertIn("fixture exception", result["reason"])

    def test_sync_published_master_preserves_checkout_upstream_and_tags(self) -> None:
        self.git("config", "push.followTags", "true")
        self.git("tag", "-a", "fixture-only", "-m", "synthetic", self.tip)
        self.git("switch", "-qc", "unrelated-local-branch")
        local_head = self.commit("local.txt", "keep local unpublished work\n")
        before = self.git("for-each-ref", "--format=%(refname) %(objectname) %(upstream)", "refs/heads", "refs/tags")
        status, result = self.run_sync()
        self.assertEqual(status, 0, result)
        self.assertEqual(result["mode"], "sync_published_branch")
        self.assertEqual(result["mode_result"]["final_remote_oid"], self.tip)
        self.assertEqual(self.target_oid(), self.tip)
        self.assertEqual(self.git("rev-parse", "HEAD"), local_head)
        self.assertEqual(self.git("for-each-ref", "--format=%(refname) %(objectname) %(upstream)", "refs/heads", "refs/tags"), before)
        self.assertEqual(self.git("for-each-ref", "refs/tags", cwd=self.target), "")
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertIn("--no-follow-tags", call)
        self.assertEqual(call[-2:], ["mirror", self.tip + ":refs/heads/master"])
        self.assertFalse(any(arg.startswith(("--force", "+")) for arg in call))
        self.assertFalse(result["commit"]["created"])

    def test_dry_run_validates_without_push(self) -> None:
        status, result = self.run_sync("--dry-run")
        self.assertEqual(status, 0, result)
        self.assertEqual(result["mode_result"]["commit_count"], 1)
        self.assertTrue(result["mode_result"]["local_head_unchanged"])
        self.assertTrue(result["mode_result"]["local_upstream_unchanged"])
        self.assertEqual(result["mode_result"]["post_verify"], "skipped_by_dry_run")
        self.assertFalse(result["push"]["executed"])
        self.assertFalse(self.log.exists())
        self.assertEqual(self.target_oid(), self.base)

    def test_already_aligned_recovers_without_another_push(self) -> None:
        self.git("update-ref", "refs/heads/master", self.tip, cwd=self.target)
        status, result = self.run_sync()
        self.assertEqual(status, 0, result)
        self.assertEqual(result["push"]["result"], "not_needed_already_aligned")
        self.assertEqual(result["mode_result"]["post_verify"], "passed")
        self.assertFalse(self.log.exists())

    def test_uncertain_success_is_observed_before_recovery(self) -> None:
        self.env["SYNC_PROBE_MODE"] = "uncertain_after_push"
        status, result = self.run_sync()
        self.assertEqual(status, 0, result)
        self.assertEqual(result["push"]["result"], "confirmed_after_uncertain")
        self.assertEqual(self.target_oid(), self.tip)

    def test_refused_push_never_claims_success(self) -> None:
        self.env["SYNC_PROBE_MODE"] = "push_refused"
        status, result = self.run_sync()
        self.assertNotEqual(status, 0)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.target_oid(), self.base)

    def test_unpublished_local_oid_cannot_be_synchronized(self) -> None:
        unpublished = self.commit("local.txt", "not published\n")
        self.assert_blocked(oid=unpublished)

    def test_wrong_source_oid_is_blocked(self) -> None:
        self.assert_blocked(oid=self.base)

    def test_wrong_target_oid_is_blocked(self) -> None:
        self.assert_blocked(expected=self.tip)

    def test_divergence_is_not_merged_or_forced(self) -> None:
        self.git("switch", "-qc", "divergence", self.base)
        divergent = self.commit("different.txt", "different branch\n")
        self.git("fetch", "-q", str(self.repo), divergent, cwd=self.target)
        self.git("update-ref", "refs/heads/master", divergent, cwd=self.target)
        status, result = self.run_sync(expected=divergent)
        self.assertNotEqual(status, 0, result)
        self.assertIn("not an ancestor", result["reason"])
        self.assertFalse(self.log.exists())
        self.assertEqual(self.target_oid(), divergent)

    def test_source_drift_before_push_is_blocked(self) -> None:
        self.env["SYNC_PROBE_MODE"] = "source_drift"
        self.assert_blocked()

    def test_target_drift_before_push_is_blocked(self) -> None:
        self.env["SYNC_PROBE_MODE"] = "target_drift"
        status, result = self.run_sync()
        self.assertNotEqual(status, 0, result)
        self.assertFalse(self.log.exists())
        self.assertEqual(self.target_oid(), self.tip)

    def test_upstream_drift_before_push_is_blocked(self) -> None:
        self.env["SYNC_PROBE_MODE"] = "upstream_drift"
        self.assert_blocked()

    def test_sensitive_history_is_scanned(self) -> None:
        sensitive = self.commit("f.txt", "sk-" + "A" * 48 + "\n")
        self.git("fetch", "-q", str(self.repo), sensitive, cwd=self.source)
        self.git("update-ref", "refs/heads/master", sensitive, cwd=self.source)
        self.assert_blocked(oid=sensitive)

    def test_sensitive_path_is_scanned(self) -> None:
        sensitive = self.commit("config/credentials.ini", "synthetic data\n")
        self.git("fetch", "-q", str(self.repo), sensitive, cwd=self.source)
        self.git("update-ref", "refs/heads/master", sensitive, cwd=self.source)
        self.assert_blocked(oid=sensitive)

    def test_non_regular_history_is_blocked(self) -> None:
        (self.repo / "link").symlink_to("f.txt")
        self.git("add", "--", "link")
        self.git("commit", "-qm", "synthetic link")
        tip = self.git("rev-parse", "HEAD")
        self.git("fetch", "-q", str(self.repo), tip, cwd=self.source)
        self.git("update-ref", "refs/heads/master", tip, cwd=self.source)
        self.assert_blocked(oid=tip)

    def test_dirty_or_untracked_checkout_is_blocked(self) -> None:
        (self.repo / "f.txt").write_text("dirty\n")
        self.assert_blocked()
        self.git("checkout", "--", "f.txt")
        (self.repo / "untracked.txt").write_text("untracked\n")
        self.assert_blocked()

    def test_staged_checkout_is_blocked(self) -> None:
        (self.repo / "f.txt").write_text("staged\n")
        self.git("add", "--", "f.txt")
        self.assert_blocked()

    def test_detached_checkout_is_blocked(self) -> None:
        self.git("checkout", "-q", "--detach")
        self.assert_blocked()

    def test_pending_operation_is_blocked(self) -> None:
        (self.repo / ".git" / "MERGE_HEAD").write_text(self.base + "\n")
        self.assert_blocked()

    def test_branch_must_exist_on_both_remotes(self) -> None:
        self.assert_blocked(branch="missing")
        self.assert_blocked(branch="refs/tags/fixture-only")
        self.assert_blocked(branch="master:other")

    def test_missing_target_does_not_create_a_branch(self) -> None:
        self.git("update-ref", "-d", "refs/heads/master", cwd=self.target)
        status, result = self.run_sync()
        self.assertNotEqual(status, 0, result)
        self.assertFalse(result["push"]["executed"])
        self.assertFalse(self.log.exists())

    def test_same_remote_or_endpoint_is_blocked(self) -> None:
        self.assert_blocked(source="mirror")
        self.git("remote", "add", "alias", str(self.source))
        self.assert_blocked(target="alias")

    def test_ambiguous_or_changed_push_endpoint_is_blocked(self) -> None:
        self.git("config", "--add", "remote.mirror.url", str(self.source))
        self.assert_blocked()
        self.git("config", "--unset-all", "remote.mirror.url")
        self.git("config", "remote.mirror.url", str(self.target))
        self.git("config", "remote.mirror.pushurl", str(self.source))
        self.assert_blocked()

    def test_short_oid_or_unsafe_remote_name_is_blocked(self) -> None:
        self.assert_blocked(oid=self.tip[:12])
        self.assert_blocked(source="--all")
        self.assert_blocked(target=str(self.target))

    def test_duplicate_flags_and_other_operations_are_blocked(self) -> None:
        for extra in [
            ("--source-remote", "source"), ("--expected-target-oid", self.base),
            ("--sync-published-branch", self.tip), ("--mode", "commit-only"),
            ("--resume-publish", self.tip), ("--message", "not a commit"),
            ("--allow-test-fixture", "tests/f.txt"), ("--", "f.txt"),
        ]:
            with self.subTest(extra=extra):
                self.assert_blocked(*extra)


if __name__ == "__main__":
    unittest.main()
