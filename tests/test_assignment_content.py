from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from git_finalizer.delivery import content_scan as SCANNER

OPAQUE_VALUE = "A" * 32  # Artificial values, never caller credentials.
DIRECTORY = "/run/credentials/worker-docker.service"
DIRECTORY_KEY = "SERVICE_CONTAINER_CREDENTIALS"


def environment_item(value: str = DIRECTORY, key: str = DIRECTORY_KEY) -> bytes:
    return f"environment = {{{json.dumps(key)}: {json.dumps(value)}}}\n".encode()


def additional_directory_forms(value: str = DIRECTORY) -> tuple[bytes, bytes]:
    quoted = json.dumps(value)
    return (f"CREDENTIALS = {quoted}\n".encode(),
            f'config = {{"container_credentials": {quoted}}}\n'.encode())


def metadata_document(version: str = "2.0.0") -> dict:
    return {
        "schema_version": 1,
        "tool_name": "worktree-controller",
        "tool_version": version,
        "contract_version": 62,
        "description": "synthetic public controller metadata",
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

    def application_test_path(self) -> None:
        self.path = "apps/worker_service/tests/test_worker_breakglass.py"
        self.source = self.repo / self.path
        self.source.parent.mkdir(parents=True)

    def test_application_directory_reference_passes_default_preflight(self) -> None:
        self.application_test_path()
        for raw in (
            environment_item(),
            environment_item().replace(b'"', b"'"),
            environment_item("/run/credentials/worker@blue.service"),
            'environment = {"备注": "目录", '.encode() + environment_item().split(b"{", 1)[1],
        ):
            with self.subTest(raw=raw):
                self.write(raw)
                status, summary = self.finalize()
                self.assertEqual(status, 0, summary)
                self.assertEqual(summary["mode_result"]["local_validation"], "passed", summary)

    def test_directory_classification_does_not_depend_on_a_test_path(self) -> None:
        self.write(environment_item())
        status, summary = self.finalize("commit-only")
        self.assertEqual(status, 0, summary)
        self.assertTrue(summary["commit"]["created"])
        self.assertFalse(summary["push"]["executed"])
        self.assertEqual(self.git("status", "--porcelain"), "")

    def test_module_constant_and_container_field_pass_default_preflight(self) -> None:
        paths = ("apps/worker_service/tests/test_worker_container.py",
                 "tests/platform/control_plane_admin/test_docker_runtime.py")
        for path, raw in zip(paths, additional_directory_forms(), strict=True):
            self.path = path
            self.source = self.repo / path
            self.source.parent.mkdir(parents=True, exist_ok=True)
            for content in (raw, raw.replace(b'"', b"'")):
                with self.subTest(path=path, content=content):
                    self.write(content)
                    status, summary = self.finalize()
                    self.assertEqual(status, 0, summary)
                    self.assertNotIn("diagnostics", summary)
            self.source.unlink()

    def test_refusal_diagnostics_bind_raw_bytes_without_echoing_values(self) -> None:
        cases = (
            (b'password = "' + b'SYNTHETIC_VALUE_012345' + b'"\n',
             "ASSIGNMENT", "credential-assignment-v1"),
            (b'# -----BEGIN ' + b'PRIVATE KEY-----\n', "PRIVATE", "private-key-header-v1"),
            (b'# gh' + b'p_' + b'A' * 32 + b'\n', "KNOWN", "known-token-v1"),
            (b'# ssh-' + b'ed25519 ' + b'A' * 48 + b'\n', "SSH", "ssh-key"),
        )
        for raw, verdict, detector in cases:
            with self.subTest(detector=detector):
                summary = self.assert_blocked(raw)
                diagnostic = summary["diagnostics"]["content_scan"]
                self.assertEqual(diagnostic, {
                    "path": self.path, "stage": "worktree", "verdict": verdict,
                    "detector": detector, "byte_length": len(raw), "binary": False,
                    "sha256": hashlib.sha256(raw).hexdigest(), "blob_oid": None,
                })
                runtime = summary["diagnostics"]["runtime"]
                for key, name in (("entrypoint", "git-finalize"),
                                  ("content_scanner", "src/git_finalizer/delivery/content_scan.py")):
                    source = (ROOT / name).resolve()
                    self.assertEqual(runtime[key], {
                        "path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                    })
                self.assertNotIn(raw.decode().strip(), json.dumps(summary))

    def test_additional_directory_forms_require_exact_static_context(self) -> None:
        constant, field = additional_directory_forms()
        for raw in (
            b"def function():\n    " + constant,
            constant.replace(b"CREDENTIALS =", b"alias = CREDENTIALS ="),
            constant.replace(b"CREDENTIALS =", b"credentials ="),
            field.replace(b"container_credentials", b"credentials"),
            field.replace(b"container_credentials", b"other_credentials"),
            field.replace(b"container_credentials", b"CONTAINER_CREDENTIALS").replace(b"config = ", b""),
            field.replace(b"container_credentials", b"container_creden\\u0074ials"),
            field.replace(b"}", b", **other}"),
            field.replace(b"}", b', "container_credentials": "SYNTHETIC_VALUE_012345"}'),
        ):
            with self.subTest(raw=raw):
                self.assertEqual(SCANNER.mask_systemd_directory_references(raw), raw)
                self.assertEqual(SCANNER.scan(raw), "ASSIGNMENT")
        annotated = constant.replace(b"CREDENTIALS =", b"CREDENTIALS: str =")
        self.assertEqual(SCANNER.mask_systemd_directory_references(annotated), annotated)
        for raw in (constant, field):
            for prefix in (b"r", b"u", b"f"):
                content = raw.replace(b'"' + DIRECTORY.encode() + b'"',
                                      prefix + b'"' + DIRECTORY.encode() + b'"')
                with self.subTest(prefix=prefix, raw=raw):
                    self.assertEqual(SCANNER.mask_systemd_directory_references(content), content)

    def test_additional_directory_forms_reject_credential_files_and_secret_values(self) -> None:
        for value in (DIRECTORY + "/password", DIRECTORY + "/", OPAQUE_VALUE,
                      "/run/credentials/../worker-production-docker.service"):
            for raw in additional_directory_forms(value):
                with self.subTest(raw=raw):
                    self.assertEqual(SCANNER.scan(raw), "ASSIGNMENT")

    def test_additional_directory_forms_preserve_all_default_detectors(self) -> None:
        guards = (
            (b'password = "SYNTHETIC_VALUE_012345"\n', "ASSIGNMENT"),
            (b'# -----BEGIN ' + b'PRIVATE KEY-----\n', "PRIVATE"),
            (b'# gh' + b'p_' + b'A' * 32 + b'\n', "KNOWN"),
            (b'# ssh-' + b'ed25519 ' + b'A' * 48 + b'\n', "SSH"),
            (b'metadata = {"description": "github\\u005fpat_' + b'A' * 32 + b'"}\n', "KNOWN"),
        )
        for raw in additional_directory_forms():
            for suffix, verdict in guards:
                with self.subTest(raw=raw, verdict=verdict):
                    self.assertEqual(SCANNER.scan(raw + suffix), verdict)
        for raw in additional_directory_forms("/run/credentials/gh" + "p_" + OPAQUE_VALUE + ".service"):
            self.assertEqual(SCANNER.scan(raw), "KNOWN")

    def test_resolved_merge_preflight_and_commit_accept_application_directory(self) -> None:
        self.application_test_path()
        self.write(environment_item())
        extra_paths = ("apps/worker_service/tests/test_worker_container.py",
                       "tests/platform/control_plane_admin/test_docker_runtime.py")
        for path, raw in zip(extra_paths, additional_directory_forms(), strict=True):
            source = self.repo / path
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(raw)
        self.git("add", "--", *extra_paths)
        self.commit_fixture("Synthetic environment baseline")
        self.git("switch", "-qc", "feature/imports")
        self.write(environment_item() + b"# Synthetic import-only edit\n")
        for path in extra_paths:
            source = self.repo / path
            source.write_bytes(source.read_bytes() + b"# Synthetic import-only edit\n")
        self.git("add", "--", *extra_paths)
        self.commit_fixture("Synthetic application import change")
        parent = self.git("rev-parse", "HEAD")
        self.git("switch", "-q", "main")
        (self.repo / "README.md").write_text("Synthetic parallel mainline change\n")
        self.git("add", "--", "README.md")
        self.git("commit", "-qm", "Synthetic mainline change")
        first_parent = self.git("rev-parse", "HEAD")
        self.git("merge", "--no-commit", "--no-ff", "feature/imports")
        for mode in ("verify-only", "commit-only"):
            before = self.state()
            command = [str(ROOT / "git-finalize"), "--summary", "--repo", str(self.repo),
                       "--mode", mode, "--merge-parent", parent]
            if mode == "commit-only":
                command += ["--message", "Synthetic resolved merge"]
            result = subprocess.run([*command, "--", self.path, *extra_paths], env=self.env,
                                    capture_output=True, text=True, timeout=30)
            summary = json.loads(result.stdout)
            self.assertEqual(result.returncode, 0, summary)
            self.assertFalse(summary["push"]["executed"])
            if mode == "verify-only":
                self.assertEqual(self.state(), before)
                self.assertEqual(self.git("rev-parse", "MERGE_HEAD"), parent)
            else:
                self.assertTrue(summary["commit"]["created"])
                self.assertEqual(self.git("rev-list", "--parents", "-n", "1", "HEAD").split()[1:],
                                 [first_parent, parent])

    def test_directory_classification_requires_canonical_service_directory(self) -> None:
        for value in (
            DIRECTORY + "/password", DIRECTORY + "/", DIRECTORY + " ",
            "/run/credentials/../worker-docker.service",
            "/run/credentials/.hidden.service", "/run/credentials/unit.socket",
            "/run/credentials/unit name.service", "run/credentials/unit.service",
            "file:///run/credentials/unit.service", "/run/credentials/" + "a" * 248 + ".service",
            OPAQUE_VALUE,
        ):
            with self.subTest(value=value):
                self.assert_blocked(environment_item(value))
        self.write(environment_item("/run/credentials/" + "a" * 247 + ".service"))
        status, summary = self.finalize()
        self.assertEqual(status, 0, summary)

    def test_directory_value_does_not_exempt_other_sensitive_names(self) -> None:
        for key in ("credentials", "SERVICE_CREDENTIAL", "service_credentials",
                    "SERVICE_PASSWORD", "SERVICE_TOKEN", "SERVICE_CLIENT_SECRET"):
            with self.subTest(key=key):
                self.assert_blocked(environment_item(key=key))
        self.assert_blocked(f'{DIRECTORY_KEY} = "{DIRECTORY}"\n'.encode())

    def test_directory_requires_complete_unambiguous_python_dictionary(self) -> None:
        item = json.dumps(DIRECTORY_KEY) + ": " + json.dumps(DIRECTORY)
        for raw in (
            ("{" + item + "}\n").encode(),
            ("{" + item + ",}\n").encode(),
            ("environment = {" + item + ", " + item + "}\n").encode(),
            ("environment = {" + item + ", **other}\n").encode(),
            ("environment = {" + item + ", other_key: 'public'}\n").encode(),
            environment_item() + b"if broken syntax\n",
            environment_item() + b"\xff",
        ):
            with self.subTest(raw=raw):
                self.assert_blocked(raw)

    def test_nonliteral_directory_forms_receive_no_classification(self) -> None:
        raw = environment_item()
        value = json.dumps(DIRECTORY).encode()
        for replacement in (
            b"r" + value, b"u" + value, b"f" + value,
            value + b' ""', b'"""' + DIRECTORY.encode() + b'"""',
            value.replace(b"worker-", b"\\u0077orker-"),
        ):
            content = raw.replace(value, replacement)
            with self.subTest(replacement=replacement):
                self.assertEqual(SCANNER.mask_systemd_directory_references(content), content)
        escaped_key = raw.replace(b"SERVICE", b"SERV\\u0049CE")
        self.assertEqual(SCANNER.mask_systemd_directory_references(escaped_key), escaped_key)
        self.assert_blocked(escaped_key)
        # These raw assignments were already detected and must stay detected.
        self.assert_blocked(raw.replace(value, value + b' ""'))
        self.assert_blocked(raw.replace(value, value.replace(b"worker-", b"\\u0077orker-")))

    def test_directory_masking_preserves_adjacent_assignments_and_comments(self) -> None:
        for suffix in (
            b'password = "SYNTHETIC_VALUE_012345"\n',
            b'# password = "SYNTHETIC_VALUE_012345"\n',
            b'metadata = {"pass\\u0077ord": "SYNTHETIC_VALUE_012345"}\n',
        ):
            with self.subTest(suffix=suffix):
                self.assert_blocked(environment_item() + suffix)

    def test_known_signatures_override_directory_classification(self) -> None:
        for value in (
            "github" + "_pat_" + OPAQUE_VALUE,
            "-----BEGIN " + "PRIVATE KEY-----",
            " ssh-" + "ed25519 " + "A" * 48,
        ):
            with self.subTest(value=value[:12]):
                extra = json.dumps({"description": value}).encode()
                for suffix in (extra, extra.replace(b"github_pat_", b"github\\u005fpat_")):
                    self.assert_blocked(environment_item() + b"metadata = " + suffix)
        self.assert_blocked(environment_item("/run/credentials/github" + "_pat_" + OPAQUE_VALUE + ".service"))

    def test_initial_branch_publish_accepts_directory_blobs_in_history(self) -> None:
        self.prepare_publication()
        self.application_test_path()
        self.write(environment_item())
        self.commit_fixture("Synthetic directory environment")
        self.write(environment_item() + b"# Synthetic import-only edit\n")
        status, summary = self.finalize("initial-branch")
        self.assertEqual(status, 0, summary)
        self.assertTrue(summary["commit"]["created"])
        self.assertTrue(summary["push"]["executed"])
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/feature/schema").split()[0],
                         self.git("rev-parse", "HEAD"))

    def test_directory_in_history_cannot_hide_a_credential_file(self) -> None:
        self.prepare_publication()
        self.application_test_path()
        self.write(environment_item(DIRECTORY + "/password"))
        self.commit_fixture("Synthetic unsafe historical credential file")
        unsafe_oid = self.git("rev-parse", "HEAD:" + self.path)
        self.write(environment_item())
        self.commit_fixture("Synthetic safe directory replacement")
        summary = self.assert_blocked(environment_item() + b"# Synthetic edit\n", mode="initial-branch")
        diagnostic = summary["diagnostics"]["content_scan"]
        self.assertEqual(diagnostic["stage"], "history")
        self.assertEqual(diagnostic["path"], self.path)
        self.assertEqual(diagnostic["blob_oid"], unsafe_oid)
        self.assertEqual(diagnostic["sha256"],
                         hashlib.sha256(environment_item(DIRECTORY + "/password")).hexdigest())
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/feature/schema"), "")

    def test_candidate_clean_filter_cannot_hide_secret_behind_directory(self) -> None:
        self.application_test_path()
        target = self.base / "filtered.py"
        target.write_bytes(environment_item() + b'password = "SYNTHETIC_VALUE_012345"\n')
        script = self.base / "filter.py"
        script.write_text("import pathlib, sys\nsys.stdin.buffer.read()\n"
                          "sys.stdout.buffer.write(pathlib.Path(sys.argv[1]).read_bytes())\n")
        (self.repo / ".gitattributes").write_text(self.path + " filter=synthetic\n")
        self.git("add", "--", ".gitattributes")
        self.git("commit", "-qm", "Synthetic filter configuration")
        self.git("config", "filter.synthetic.clean", shlex.join([sys.executable, str(script), str(target)]))
        self.git("config", "filter.synthetic.required", "true")
        for raw in (environment_item(), *additional_directory_forms()):
            target.write_bytes(raw + b'password = "SYNTHETIC_VALUE_012345"\n')
            for mode, stage in (("verify-only", "candidate"), ("commit-only", "index")):
                with self.subTest(mode=mode, raw=raw):
                    self.write(raw)
                    status, summary = self.finalize(mode)
                    self.assertEqual(status, 1, summary)
                    self.assertFalse(summary["commit"]["created"])
                    self.assertFalse(summary["push"]["executed"])
                    diagnostic = summary["diagnostics"]["content_scan"]
                    self.assertEqual(diagnostic["stage"], stage)
                    self.assertEqual(diagnostic["path"], self.path)
                    self.assertEqual(diagnostic["detector"], "credential-assignment-v1")
                    self.assertEqual(diagnostic["sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
                    self.assertEqual(diagnostic["blob_oid"],
                                     self.git("hash-object", "--no-filters", str(target)))

    def test_word_suffixes_do_not_start_secret_key_signatures(self) -> None:
        for prefix in ("task-", "predispatch-risk-", "TASK-"):
            identifier = prefix + "validation-policy-regression"
            document = json.dumps({"description": identifier}).encode()
            for raw in (
                identifier.encode(),
                document,
                document.replace(b"sk-", b"s\\u006b-"),
                document.replace(b"sk-", b"s\\u006b-")[:-1],
            ):
                with self.subTest(prefix=prefix, raw=raw):
                    self.write(raw)
                    status, summary = self.finalize()
                    self.assertEqual(status, 0, summary)
                    self.assertEqual(summary["status"], "success", summary)
                    self.assertEqual(summary["mode_result"]["local_validation"], "passed", summary)

    def test_secret_key_signatures_at_token_boundaries_remain_blocked(self) -> None:
        for prefix in ("s" + "k-", "s" + "k-proj-"):
            value = prefix + OPAQUE_VALUE
            document = json.dumps({"description": value}).encode()
            for raw in (
                value.encode(),
                b"Bearer " + value.encode(),
                b"`" + value.encode() + b"`",
                document,
                document.replace(b"sk-", b"s\\u006b-"),
                document.replace(b"sk-", b"s\\u006b-")[:-1],
            ):
                with self.subTest(prefix=prefix, raw=raw):
                    self.assert_blocked(raw)

    def test_decayed_branch_retirement_v3_declaration_is_not_exempted(self) -> None:
        decayed = {
            "schema_version": 1,
            "tool_name": "worktree-controller",
            "tool_version": "1.13.3",
            "contract_version": 39,
            "branch_retirement_execution_version": 1,
            "branch_retirement_contract": {
                "contract": "branch-retirement/v3",
                "execution_authorization": {
                    "contract": "branch-retirement-execution/v1",
                    "capability": "branch_retirement_execution_version",
                    "to" + "ken": "synthetic long " + "token prose value for retirement contract",
                },
            },
        }
        self.assertIn("硬编码", self.assert_blocked(decayed)["reason"])

    def test_other_sensitive_assignments_remain_blocked(self) -> None:
        for key in ("token", "password", "passwd", "secret", "credential", "credentials", "api_key", "client_secret"):
            with self.subTest(key=key):
                document = metadata_document()
                document["extra"] = {key: OPAQUE_VALUE}
                self.assertIn("硬编码", self.assert_blocked(document)["reason"])

    def test_masking_preserves_raw_assignment_length(self) -> None:
        # Raw escapes exceed the detector threshold; decoded value does not.
        raw = json.dumps(metadata_document()).encode()
        extra = b', "password": "1234' + b'\\u0061' * 3 + b'"}'
        self.assert_blocked(raw[:-1] + extra)

    def test_masking_preserves_raw_assignment_with_escaped_apostrophe(self) -> None:
        raw = json.dumps(metadata_document()).encode()
        extra = b', "password": "abc' + b'\\u0027' + b'SYNTHETIC_VALUE_0123456789"}'
        self.assert_blocked(raw[:-1] + extra)

    def test_escaped_sensitive_field_name_does_not_hide_an_assignment(self) -> None:
        document = metadata_document()
        document["extra"] = {"password": OPAQUE_VALUE}
        raw = json.dumps(document).encode().replace(b'"password"', b'"pass\\u0077ord"')
        self.assert_blocked(raw)

    def test_escaped_key_and_quoted_value_cannot_hide_an_assignment(self) -> None:
        base = json.dumps(metadata_document()).encode()
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
                document = metadata_document()
                document["description"] = value
                self.assert_blocked(document)

    def test_escaped_signatures_cannot_hide_in_schema_strings(self) -> None:
        document = metadata_document()
        document["description"] = "github" + "_pat_" + OPAQUE_VALUE
        raw = json.dumps(document).encode().replace(b"github_pat_", b"github\\u005fpat_")
        self.assert_blocked(raw)

    def test_fully_escaped_public_and_sensitive_keys_remain_blocked(self) -> None:
        document = metadata_document()
        document["extra"] = {"password": OPAQUE_VALUE}
        raw = json.dumps(document).encode().replace(b'"tool_name"', b'"to\\u006fl_name"')
        raw = raw.replace(b'"password"', b'"pass\\u0077ord"')
        self.assert_blocked(raw)
        document = metadata_document()
        document["extra"] = {"token": OPAQUE_VALUE}
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
        document = metadata_document()
        document["extra"] = {"password": OPAQUE_VALUE}
        variants.append(json.dumps(document).encode().replace(b'"tool_name"', b'"to\\u006fl_name"')
                        .replace(b'"password"', b'"pass\\u0077ord"'))
        for value, literal, escaped in (
            ("github" + "_pat_" + OPAQUE_VALUE, b"github_pat_", b"github\\u005fpat_"),
            ("s" + "k-proj-" + OPAQUE_VALUE, b"sk-", b"s\\u006b-"),
            ("-----BEGIN " + "PRIVATE KEY-----", b"PRIVATE", b"PRIV\\u0041TE"),
            (" ssh-" + "ed25519 " + "A" * 48, b"ssh-", b"ss\\u0068-"),
        ):
            document = metadata_document()
            document["description"] = value
            variants.append(json.dumps(document).encode().replace(literal, escaped))
        for raw in variants:
            with self.subTest(kind=variants.index(raw)):
                target.write_bytes(raw)
                self.write(metadata_document())
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
        unsafe = metadata_document()
        unsafe["extra"] = {"password": OPAQUE_VALUE}
        raw = json.dumps(unsafe).encode()
        for content in (
            raw[:-1],
            raw.replace(b'"tool_name":', b'"tool_name": "other", "tool_name":', 1),
            raw[:-1] + b', "extra2": NaN}',
            raw + b'\xff',
        ):
            with self.subTest(content=content[-10:]):
                self.assert_blocked(content)

    def test_classifier_process_failure_keeps_the_strict_result(self) -> None:
        pyroot = self.base / "scan-error"
        pyroot.mkdir()
        (pyroot / "sitecustomize.py").write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(ROOT / 'src')!r})\n"
            "import git_finalizer.delivery.content_scan as cs\n"
            "cs.scan = lambda *a, **kw: 'ERROR'\n"
        )
        old_pp = self.env.get("PYTHONPATH")
        self.env["PYTHONPATH"] = str(pyroot)
        try:
            summary = self.assert_blocked(metadata_document())
            self.assertEqual(summary["diagnostics"]["content_scan"]["detector"], "content-scan-error")
        finally:
            if old_pp is None:
                self.env.pop("PYTHONPATH", None)
            else:
                self.env["PYTHONPATH"] = old_pp

    def test_scanner_reports_preserve_legacy_verdict_and_candidate_protocol(self) -> None:
        self.write(environment_item())
        self.commit_fixture("Synthetic scanner report fixture")
        oid = self.git("rev-parse", "HEAD:" + self.path)
        raw = self.source.read_bytes()
        expected_sha = hashlib.sha256(raw).hexdigest()
        cand_oid, cand_bytes = SCANNER.hash_candidate_clean(self.repo, self.path, self.source)
        self.assertEqual(SCANNER.scan(raw), "CLEAR")
        self.assertIsNone(SCANNER.scan_diagnostic(raw, path=self.path, stage="worktree"))
        self.assertEqual(cand_bytes, raw)
        self.assertEqual(cand_oid, oid)
        blocked_raw = b'pass' + b'word = "SYNTHETIC_VALUE_012345"\n'
        diag = SCANNER.scan_diagnostic(blocked_raw, path=self.path, stage="index", blob_oid=oid)
        self.assertIsNotNone(diag)
        assert diag is not None
        self.assertEqual(diag["verdict"], "ASSIGNMENT")
        self.assertEqual(diag["sha256"], hashlib.sha256(blocked_raw).hexdigest())
        self.assertEqual(diag["blob_oid"], oid)
        self.assertNotEqual(expected_sha, diag["sha256"])

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
        self.write(metadata_document())
        self.commit_fixture("Synthetic public schema")
        self.write(metadata_document("2.0.1"))
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
        unsafe = metadata_document()
        unsafe["extra"] = {"token": OPAQUE_VALUE}
        self.write(unsafe)
        self.commit_fixture("Synthetic unsafe historical blob")
        self.write(metadata_document())
        self.commit_fixture("Synthetic safe schema")
        self.assert_blocked(metadata_document("2.0.1"), mode="initial-branch")
        self.assertEqual(self.git("ls-remote", "origin", "refs/heads/feature/schema"), "")

    def test_clean_filter_cannot_replace_descriptor_with_staged_secret(self) -> None:
        script = self.base / "filter.py"
        script.write_text(
            "import json, sys\n"
            "data = json.load(sys.stdin)\n"
            "data['token'] = 'A' * 32\n"
            "print(json.dumps(data))\n"
        )
        (self.repo / ".gitattributes").write_text("metadata.json filter=synthetic\n")
        self.git("add", "--", ".gitattributes")
        self.git("commit", "-qm", "Synthetic filter configuration")
        self.git("config", "filter.synthetic.clean", shlex.join([sys.executable, str(script)]))
        self.git("config", "filter.synthetic.required", "true")
        self.write(metadata_document())
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
