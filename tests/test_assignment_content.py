from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
DESCRIPTION = (
    "a 256-bit Controller-generated capability returned only by authorize and "
    "compared constant-time at complete or abandon; it is excluded from the "
    "identity so it cannot change the authorization address"
)
OPAQUE_VALUE = "A" * 32  # Artificial values, never caller credentials.


def schema(version: str = "1.13.3") -> dict:
    return {
        "schema_version": 1,
        "tool_name": "worktree-controller",
        "tool_version": version,
        "contract_version": 39,
        "branch_retirement_execution_version": 1,
        "branch_retirement_contract": {
            "contract": "branch-retirement/v3",
            "execution_authorization": {
                "contract": "branch-retirement-execution/v1",
                "capability": "branch_retirement_execution_version",
                "token": DESCRIPTION,
            },
        },
    }


class AssignmentContentTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="gf-assignment-content-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.env = {
            "PATH": os.defpath,
            "HOME": str(self.base),
            "LC_ALL": "C",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "TMPDIR": str(self.base),
        }
        self.git("init", "-q", "--initial-branch=main")
        self.git("config", "user.name", "Synthetic Author")
        self.git("config", "user.email", "synthetic@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        (self.repo / "README.md").write_text("Synthetic baseline\n")
        self.git("add", "--", "README.md")
        self.git("commit", "-qm", "Synthetic baseline")
        # Deliberately unrelated filename: the declaration, not a path, matters.
        self.path = "metadata.json"
        self.source = self.repo / self.path

    def git(self, *arguments: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(self.repo), *arguments],
            env=self.env, stderr=subprocess.PIPE, text=True,
        ).strip()

    def state(self) -> tuple[str, str, str]:
        return (
            self.git("rev-parse", "HEAD"),
            self.git("ls-files", "--stage"),
            self.git("status", "--porcelain=v1", "--untracked-files=all"),
        )

    def write(self, document: dict | bytes, **kwargs) -> None:
        content = document if isinstance(document, bytes) else json.dumps(document, **kwargs).encode()
        self.source.write_bytes(content)

    def finalize(self, mode: str = "verify-only") -> tuple[int, dict]:
        before = self.state()
        command = [str(ROOT / "git-finalize"), "--summary", "--repo", str(self.repo)]
        if mode == "initial-branch":
            command += ["--initial-branch-publish", "--remote", "origin", "--remote-branch", "feature/schema"]
        else:
            command += ["--mode", mode]
        if mode != "verify-only":
            command += ["--message", "Synthetic schema change"]
        result = subprocess.run(
            [*command, "--", self.path], env=self.env,
            capture_output=True, text=True, timeout=30,
        )
        self.assertTrue(result.stdout, result.stderr)
        summary = json.loads(result.stdout)
        if mode == "verify-only":
            self.assertEqual(self.state(), before)
        return result.returncode, summary

    def assert_blocked(self, document: dict | bytes, *, mode: str = "verify-only") -> dict:
        self.write(document)
        before = self.state()
        status, summary = self.finalize(mode)
        self.assertEqual(status, 1, summary)
        self.assertEqual(summary["status"], "blocked")
        self.assertFalse(summary["commit"]["created"])
        self.assertFalse(summary["push"]["executed"])
        self.assertEqual(self.git("rev-parse", "HEAD"), before[0])
        self.assertTrue("内容" in summary["reason"] or "硬编码" in summary["reason"], summary["reason"])
        return summary

    def prepare_publication(self) -> None:
        remote = self.base / "remote.git"
        self.git("init", "-q", "--bare", "--initial-branch=main", str(remote))
        self.git("remote", "add", "origin", str(remote))
        self.git("push", "-qu", "origin", "main")
        self.git("switch", "-qc", "feature/schema")

    def commit_fixture(self, message: str) -> None:
        self.git("add", "--", self.path)
        self.git("commit", "-qm", message)

    def test_exact_declaration_accepts_version_and_format_changes(self) -> None:
        for version, formatting in (
            ("1.13.3", {"indent": 2}),
            ("1.13.5", {"separators": (",", ":")}),
        ):
            with self.subTest(version=version):
                self.write(schema(version), **formatting)
                status, summary = self.finalize()
                self.assertEqual(status, 0, summary)
                self.assertEqual(summary["status"], "success")

    def test_escaped_exact_declaration_is_still_semantic(self) -> None:
        raw = json.dumps(schema()).encode().replace(b"256-bit", b"256\\u002dbit")
        self.write(raw)
        status, summary = self.finalize()
        self.assertEqual(status, 0, summary)

    def test_token_values_are_not_classified_by_spaces_or_prose(self) -> None:
        values = (
            OPAQUE_VALUE, "Bearer " + OPAQUE_VALUE,
            "a long password with spaces", DESCRIPTION + " " + OPAQUE_VALUE,
            DESCRIPTION.replace("256-bit", "512-bit"),
        )
        for value in values:
            with self.subTest(value=value[:12]):
                document = schema()
                document["branch_retirement_contract"]["execution_authorization"]["token"] = value
                self.assertIn("硬编码", self.assert_blocked(document)["reason"])

    def test_declaration_requires_explicit_matching_schema(self) -> None:
        cases = []
        document = schema()
        document["tool_name"] = "unrelated-tool"
        cases.append(document)
        document = schema()
        document["schema_version"] = True
        cases.append(document)
        document = schema()
        document["branch_retirement_contract"]["contract"] = "branch-retirement/v2"
        cases.append(document)
        document = schema()
        document["branch_retirement_contract"]["execution_authorization"]["capability"] = "unrelated"
        cases.append(document)
        cases.append({"token": DESCRIPTION})
        for document in cases:
            with self.subTest(document=document.get("tool_name")):
                self.assert_blocked(document)

    def test_other_sensitive_assignments_remain_blocked(self) -> None:
        for key in ("token", "password", "passwd", "secret", "credential", "credentials", "api_key", "client_secret"):
            with self.subTest(key=key):
                document = schema()
                document["extra"] = {key: OPAQUE_VALUE}
                self.assertIn("硬编码", self.assert_blocked(document)["reason"])

    def test_description_elsewhere_does_not_get_an_exception(self) -> None:
        document = schema()
        document["extra"] = {"token": DESCRIPTION}
        self.assert_blocked(document)
        document = schema()
        authorization = document["branch_retirement_contract"]["execution_authorization"]
        authorization["password"] = authorization.pop("token")
        self.assert_blocked(document)

    def test_masking_preserves_raw_assignment_length(self) -> None:
        # Raw escapes exceed the detector threshold; decoded value does not.
        raw = json.dumps(schema()).encode()
        extra = b', "password": "1234' + b'\\u0061' * 3 + b'"}'
        self.assert_blocked(raw[:-1] + extra)

    def test_masking_preserves_raw_assignment_with_escaped_apostrophe(self) -> None:
        raw = json.dumps(schema()).encode()
        extra = b', "password": "abc' + b'\\u0027' + b'SYNTHETIC_VALUE_0123456789"}'
        self.assert_blocked(raw[:-1] + extra)

    def test_escaped_sensitive_field_name_does_not_hide_an_assignment(self) -> None:
        document = schema()
        document["extra"] = {"password": OPAQUE_VALUE}
        raw = json.dumps(document).encode().replace(b'"password"', b'"pass\\u0077ord"')
        self.assert_blocked(raw)

    def test_escaped_key_and_quoted_value_cannot_hide_an_assignment(self) -> None:
        base = json.dumps(schema()).encode()
        for escaped_quote in (b'\\u0027', b'\\u0022'):
            with self.subTest(quote=escaped_quote):
                extra = (
                    b', "pass\\u0077ord": "abc' + escaped_quote
                    + b'SYNTHETIC_OPAQUE_LONG_0123456789"}'
                )
                self.assert_blocked(base[:-1] + extra)

    def test_signatures_override_the_declaration(self) -> None:
        for value in (
            "github" + "_pat_" + OPAQUE_VALUE,
            "-----BEGIN " + "PRIVATE KEY-----",
            " ssh-" + "ed25519 " + "A" * 48,
        ):
            with self.subTest(value=value[:12]):
                document = schema()
                document["description"] = value
                self.assert_blocked(document)

    def test_escaped_signatures_cannot_hide_in_schema_strings(self) -> None:
        document = schema()
        document["description"] = "github" + "_pat_" + OPAQUE_VALUE
        raw = json.dumps(document).encode().replace(b"github_pat_", b"github\\u005fpat_")
        self.assert_blocked(raw)

    def test_fully_escaped_public_and_sensitive_keys_remain_blocked(self) -> None:
        document = schema()
        document["extra"] = {"password": OPAQUE_VALUE}
        raw = json.dumps(document).encode().replace(b'"token"', b'"to\\u006ben"')
        raw = raw.replace(b'"password"', b'"pass\\u0077ord"')
        self.assert_blocked(raw)
        document = schema()
        document["branch_retirement_contract"]["execution_authorization"]["token"] = OPAQUE_VALUE
        raw = json.dumps(document).encode().replace(b'"token"', b'"to\\u006ben"')
        self.assert_blocked(raw)

    def test_escaped_assignments_and_signatures_are_blocked_in_staged_filter(self) -> None:
        target = self.base / "filtered.json"
        script = self.base / "filter.py"
        script.write_text(
            "import pathlib, sys\n"
            "sys.stdin.buffer.read()\n"
            "sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes())\n"
        )
        (self.repo / ".gitattributes").write_text("metadata.json filter=synthetic\n")
        self.git("add", "--", ".gitattributes")
        self.git("commit", "-qm", "Synthetic filter configuration")
        self.git("config", "filter.synthetic.clean", shlex.join([sys.executable, str(script), str(target)]))
        self.git("config", "filter.synthetic.required", "true")
        variants = []
        document = schema()
        document["extra"] = {"password": OPAQUE_VALUE}
        variants.append(json.dumps(document).encode().replace(b'"token"', b'"to\\u006ben"')
                        .replace(b'"password"', b'"pass\\u0077ord"'))
        for value, literal, escaped in (
            ("github" + "_pat_" + OPAQUE_VALUE, b"github_pat_", b"github\\u005fpat_"),
            ("-----BEGIN " + "PRIVATE KEY-----", b"PRIVATE", b"PRIV\\u0041TE"),
            (" ssh-" + "ed25519 " + "A" * 48, b"ssh-", b"ss\\u0068-"),
        ):
            document = schema()
            document["description"] = value
            variants.append(json.dumps(document).encode().replace(literal, escaped))
        for raw in variants:
            with self.subTest(kind=variants.index(raw)):
                target.write_bytes(raw)
                self.write(schema())
                head = self.git("rev-parse", "HEAD")
                status, summary = self.finalize("commit-only")
                self.assertEqual(status, 1, summary)
                self.assertEqual(summary["status"], "failed", summary)
                self.assertFalse(summary["commit"]["created"])
                self.assertFalse(summary["push"]["executed"])
                self.assertEqual(self.git("rev-parse", "HEAD"), head)
                self.assertIn("staged", summary["reason"])
                self.git("reset", "-q", "HEAD", "--", self.path)

    def test_invalid_duplicate_or_non_utf8_json_remains_strict(self) -> None:
        raw = json.dumps(schema()).encode()
        for content in (
            raw[:-1],
            raw.replace(b'"tool_name":', b'"tool_name": "other", "tool_name":', 1),
            raw[:-1] + b', "extra": NaN}',
            raw + b'\xff',
        ):
            with self.subTest(content=content[-10:]):
                self.assert_blocked(content)

    def test_classifier_process_failure_keeps_the_strict_result(self) -> None:
        binary = self.base / "bin"
        binary.mkdir()
        wrapper = binary / "python3"
        for exit_code in (0, 1, 2, 137):
            with self.subTest(exit_code=exit_code):
                wrapper.write_text(
                    '#!/bin/sh\n'
                    'if [ "$3" = file ] || [ "$3" = blob ]; then\n'
                    f'  exit {exit_code}\n'
                    'fi\n'
                    f'exec {shlex.quote(sys.executable)} "$@"\n'
                )
                wrapper.chmod(0o755)
                self.env["PATH"] = str(binary) + os.pathsep + os.defpath
                self.assert_blocked(schema())

    def test_invalid_json_cannot_hide_escaped_signatures(self) -> None:
        signatures = (
            (b"ghp_" + b"A" * 24, b"ghp\\u005f" + b"A" * 24),
            (b"-----BEGIN PRIVATE KEY-----", b"-----BEGIN\\u0020PRIVATE KEY-----"),
            (b"ssh-ed25519 " + b"A" * 48, b"ssh-ed25519\\u0020" + b"A" * 48),
        )
        for _plain, escaped in signatures:
            safe_container = b'{"description":"' + escaped + b'", "extra":0}'
            for raw in (
                safe_container.replace(b'"extra":0', b'"extra":0,"extra":1'),
                safe_container.replace(b'"extra":0', b'"extra":NaN'),
                safe_container[:-1],
                safe_container + b'\xff',
            ):
                with self.subTest(signature=escaped[:16], ending=raw[-20:]):
                    self.assert_blocked(raw)

    def test_duplicate_key_does_not_discard_an_escaped_sensitive_assignment(self) -> None:
        self.assert_blocked(
            b'{"p\\u0061ssword":"SYNTHETIC_VALUE_012345", "extra":0,"extra":1}'
        )

    def test_invalid_escaped_json_in_unpublished_history_remains_blocked(self) -> None:
        self.prepare_publication()
        self.git("push", "-qu", "origin", "feature/schema")
        self.write(b'{"description":"ghp\\u005f' + b'A' * 24 + b'", "extra":NaN}')
        self.commit_fixture("Synthetic historical escaped signature")
        self.write({"description": "safe public metadata"})
        self.commit_fixture("Synthetic safe replacement")
        head = self.git("rev-parse", "HEAD")
        before = self.state()
        result = subprocess.run(
            [str(ROOT / "git-finalize"), "--summary", "--repo", str(self.repo), "--resume-publish", head],
            env=self.env, capture_output=True, text=True, timeout=30,
        )
        data = json.loads(result.stdout)
        self.assertEqual(result.returncode, 1, data)
        self.assertFalse(data["push"]["executed"])
        self.assertIn("内容", data["reason"])
        self.assertEqual(self.state(), before)

    def test_initial_branch_publish_accepts_schema_blobs_in_history(self) -> None:
        self.prepare_publication()
        self.write(schema())
        self.commit_fixture("Synthetic public schema")
        self.write(schema("1.13.5"))
        status, summary = self.finalize("initial-branch")
        self.assertEqual(status, 0, summary)
        self.assertTrue(summary["commit"]["created"])
        self.assertTrue(summary["push"]["executed"])
        self.assertEqual(
            self.git("ls-remote", "origin", "refs/heads/feature/schema").split()[0],
            self.git("rev-parse", "HEAD"),
        )
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_initial_branch_publish_refuses_secret_in_older_blob(self) -> None:
        self.prepare_publication()
        unsafe = schema()
        unsafe["branch_retirement_contract"]["execution_authorization"]["token"] = OPAQUE_VALUE
        self.write(unsafe)
        self.commit_fixture("Synthetic unsafe historical blob")
        self.write(schema())
        self.commit_fixture("Synthetic safe schema")
        self.assert_blocked(schema("1.13.5"), mode="initial-branch")
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/feature/schema"), "")

    def test_clean_filter_cannot_replace_descriptor_with_staged_secret(self) -> None:
        script = self.base / "filter.py"
        script.write_text(
            "import json, sys\n"
            "data = json.load(sys.stdin)\n"
            "data['branch_retirement_contract']['execution_authorization']['token'] = 'A' * 32\n"
            "print(json.dumps(data))\n"
        )
        (self.repo / ".gitattributes").write_text("metadata.json filter=synthetic\n")
        self.git("add", "--", ".gitattributes")
        self.git("commit", "-qm", "Synthetic filter configuration")
        self.git("config", "filter.synthetic.clean", shlex.join([sys.executable, str(script)]))
        self.git("config", "filter.synthetic.required", "true")
        self.write(schema())
        before = self.state()
        status, summary = self.finalize("commit-only")
        self.assertEqual(status, 1, summary)
        self.assertEqual(summary["status"], "failed", summary)
        self.assertFalse(summary["commit"]["created"])
        self.assertFalse(summary["push"]["executed"])
        self.assertEqual(self.git("rev-parse", "HEAD"), before[0])
        self.assertIn("staged", summary["reason"])
        self.assertIn("硬编码", summary["reason"])


if __name__ == "__main__":
    unittest.main()
