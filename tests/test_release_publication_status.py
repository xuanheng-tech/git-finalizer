"""Publication observation is recoverable and never treats unknown remote state as absence."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


class PublicationStatusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.release = runpy.run_path(str(ROOT / "scripts/release.py"))
        cls.storage = tempfile.TemporaryDirectory(prefix="gf-publication-observation-")
        cls.addClassCleanup(cls.storage.cleanup)
        match = re.search(r'^readonly VERSION="([0-9.]+)"$',
                          (ROOT / "git-finalize").read_text(), re.MULTILINE)
        cls.tag = f"v{match.group(1)}"
        cls.artifact = cls.release["build"](cls.tag, dist=Path(cls.storage.name) / "dist")
        cls.digest = hashlib.sha256(cls.artifact.read_bytes()).hexdigest()

    def setUp(self) -> None:
        self.commit = "1" * 40
        self.tag_object = "2" * 40
        self.endpoint = "https://github.com/example/git-finalizer.git"
        self.push_endpoint = None
        self.repository = {"full_name": "example/git-finalizer", "archived": False,
                           "permissions": {"push": True}}
        self.release_data = {
            "tag_name": self.tag, "target_commitish": self.commit, "id": 12,
            "draft": False, "prerelease": False,
            "assets": [{"name": name, "size": path.stat().st_size, "state": "uploaded"}
                       for name, path in [(self.artifact.name, self.artifact),
                                          ("SHA256SUMS.txt", self.artifact.parent / "SHA256SUMS.txt")]],
        }
        self.repository_status = 200
        self.release_status = 200
        self.pages = [[]]
        self.commands = []
        self.tag_reads = 0
        self.drift_tag = False
        self.corrupt_checksum = False
        self.download_failure = False

    def transport(self, command, **kwargs):
        self.commands.append(command)
        stdout, stderr, code = "", "", 0
        if command[:3] == ["git", "cat-file", "-t"]:
            stdout = "commit\n"
        elif command[:3] == ["git", "rev-parse", "--verify"]:
            stdout = self.commit
        elif command[:3] == ["git", "remote", "get-url"]:
            stdout = (self.push_endpoint or self.endpoint) if "--push" in command else self.endpoint
        elif command[:2] == ["git", "ls-remote"]:
            self.tag_reads += 1
            if self.tag_object:
                oid = "3" * 40 if self.drift_tag and self.tag_reads > 1 else self.tag_object
                stdout = f"{oid}\trefs/tags/{self.tag}\n{self.commit}\trefs/tags/{self.tag}^{{}}\n"
        elif command[:2] == ["gh", "api"]:
            if "--paginate" in command:
                stdout = "\n".join(json.dumps(page) for page in self.pages)
                self.assertNotIn("--slurp", command)
            else:
                repository_lookup = command[-1] == "repos/example/git-finalizer"
                status = self.repository_status if repository_lookup else self.release_status
                body = self.repository if repository_lookup else self.release_data
                stdout = f"HTTP/2.0 {status}\r\nContent-Type: application/json\r\n\r\n{json.dumps(body)}"
                code = 0 if status == 200 else 1
                stderr = "private transport diagnostic" if code else ""
        elif command[:3] == ["gh", "release", "download"]:
            if self.download_failure:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            destination = Path(command[command.index("--dir") + 1])
            for path in (self.artifact, self.artifact.parent / "SHA256SUMS.txt"):
                shutil.copyfile(path, destination / path.name)
            if self.corrupt_checksum:
                (destination / "SHA256SUMS.txt").write_text(f"{'0' * 64}  {self.artifact.name}\n")
        else:
            self.fail(f"unexpected command in read-only observation: {command}")
        return subprocess.CompletedProcess(command, code, stdout, stderr)

    def observe(self, expected_sha256=None):
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin:/bin"}, clear=True), \
                mock.patch("subprocess.run", side_effect=self.transport):
            return self.release["publication_status"](self.tag, "github", self.commit, expected_sha256)

    def test_new_publication_is_reported_only_after_complete_draft_listing(self) -> None:
        self.tag_object = None
        self.release_status = 404
        self.pages = [[{"tag_name": "v0.0.0"}], []]
        report = self.observe()
        self.assertEqual((report["status"], report["state"]), ("ok", "unpublished"))
        self.assertTrue(any("--paginate" in command for command in self.commands))
        self.assertFalse(any(command[:3] == ["gh", "release", "download"] for command in self.commands))

    def test_tagged_interruption_preserves_tag_and_recommends_resume(self) -> None:
        self.release_status = 404
        report = self.observe()
        self.assertEqual(report["state"], "tagged_unpublished")
        self.assertIn("without_recreating_tag", report["next_action"])
        self.assertEqual(self.tag_reads, 1)

    def test_draft_in_later_page_is_detected_even_without_a_tag(self) -> None:
        self.release_status = 404
        self.release_data["draft"] = True
        self.pages = [[{"tag_name": "v0.0.0"}], [self.release_data]]
        for tag_object in (self.tag_object, None):
            with self.subTest(tag_present=bool(tag_object)):
                self.tag_object = tag_object
                report = self.observe()
                self.assertEqual(report["state"], "draft_incomplete")
                self.assertEqual(report["release_id"], 12)

    def test_read_access_alone_cannot_prove_release_absence(self) -> None:
        self.release_status = 404
        self.repository["permissions"]["push"] = False
        report = self.observe()
        self.assertEqual(report["status"], "blocked")
        self.assertIn("draft visibility", report["reason"])
        self.assertFalse(any("--paginate" in command for command in self.commands))

    def test_authentication_or_transport_failure_is_not_unpublished(self) -> None:
        for status in (401, 403, 404, 500):
            with self.subTest(status=status):
                self.repository_status = status
                report = self.observe()
                self.assertEqual(report["status"], "blocked")
                self.assertNotEqual(report["state"], "unpublished")
                self.assertNotIn("private transport diagnostic", json.dumps(report))

    def test_existing_release_requires_source_digest_before_completed_classification(self) -> None:
        report = self.observe()
        self.assertEqual(report["state"], "published_assets_verified")
        self.assertEqual(report["artifact_sha256"], self.digest)
        report = self.observe(self.digest)
        self.assertEqual((report["status"], report["state"]), ("ok", "published_verified"))
        self.assertEqual(report["next_action"], "none")
        self.assertEqual(report["artifact_size"], self.artifact.stat().st_size)

    def test_source_digest_mismatch_and_tag_drift_block_completion(self) -> None:
        report = self.observe("0" * 64)
        self.assertEqual((report["status"], report["state"]), ("blocked", "conflict"))
        self.tag_reads = 0
        self.drift_tag = True
        report = self.observe(self.digest)
        self.assertEqual((report["status"], report["state"]), ("blocked", "conflict"))
        self.assertIn("drifted", report["reason"])

    def test_corrupt_download_is_not_republished(self) -> None:
        self.corrupt_checksum = True
        report = self.observe(self.digest)
        self.assertEqual((report["status"], report["state"]), ("blocked", "published_incomplete"))
        self.assertIn("checksum", report["reason"])

    def test_download_timeout_preserves_unknown_outcome(self) -> None:
        self.download_failure = True
        report = self.observe(self.digest)
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(report["state"], "published_incomplete")
        self.assertNotIn("download", report["reason"])

    def test_ssh_endpoint_is_supported_and_ambiguous_endpoint_is_blocked(self) -> None:
        self.endpoint = "git@github.com:example/git-finalizer.git"
        self.assertEqual(self.observe(self.digest)["state"], "published_verified")
        self.push_endpoint = "https://github.com/another/repository.git"
        self.assertEqual(self.observe()["status"], "blocked")

    def test_duplicate_listing_cannot_be_reduced_to_one_release(self) -> None:
        self.release_status = 404
        self.pages = [[self.release_data], [self.release_data]]
        report = self.observe()
        self.assertEqual(report["status"], "blocked")
        self.assertIn("multiple releases", report["reason"])

    def test_published_release_without_tag_is_not_completed(self) -> None:
        self.tag_object = None
        report = self.observe(self.digest)
        self.assertEqual((report["status"], report["state"]), ("blocked", "conflict"))


if __name__ == "__main__":
    unittest.main()
