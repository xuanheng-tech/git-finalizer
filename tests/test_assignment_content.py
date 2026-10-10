from __future__ import annotations

import importlib.util
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
DESCRIPTION = (
    "a 256-bit Controller-generated capability returned only by authorize and "
    "compared constant-time at complete or abandon; it is excluded from the "
    "identity so it cannot change the authorization address"
)
OPAQUE_VALUE = "A" * 32  # Artificial values, never caller credentials.
DIRECTORY = "/run/credentials/jq-production-docker.service"
DIRECTORY_KEY = "QUANT_JQ_CONTAINER_CREDENTIALS"
SCANNER_SPEC = importlib.util.spec_from_file_location("content_scan", ROOT / "git-finalize-content-scan.py")
SCANNER = importlib.util.module_from_spec(SCANNER_SPEC)
SCANNER_SPEC.loader.exec_module(SCANNER)


def environment_item(value: str = DIRECTORY, key: str = DIRECTORY_KEY) -> bytes:
    return f"environment = {{{json.dumps(key)}: {json.dumps(value)}}}\n".encode()


def additional_directory_forms(value: str = DIRECTORY) -> tuple[bytes, bytes]:
    quoted = json.dumps(value)
    return (f"CREDENTIALS = {quoted}\n".encode(),
            f'config = {{"container_credentials": {quoted}}}\n'.encode())


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

    def application_test_path(self) -> None:
        self.path = "apps/quant_orchestration/tests/test_jq_breakglass.py"
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
        paths = ("apps/quant_orchestration/tests/test_jq_container.py",
                 "tests/platform/control_plane_admin/test_docker_runtime.py")
        for path, raw in zip(paths, additional_directory_forms()):
            self.path = path
            self.source = self.repo / path
            self.source.parent.mkdir(parents=True, exist_ok=True)
            for content in (raw, raw.replace(b'"', b"'")):
                with self.subTest(path=path, content=content):
                    self.write(content)
                    status, summary = self.finalize()
                    self.assertEqual(status, 0, summary)
                    self.assertIsNone(summary["diagnostics"]["content_scan"])
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
                                  ("content_scanner", "git-finalize-content-scan.py")):
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
                      "/run/credentials/../jq-production-docker.service"):
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
        extra_paths = ("apps/quant_orchestration/tests/test_jq_container.py",
                       "tests/platform/control_plane_admin/test_docker_runtime.py")
        for path, raw in zip(extra_paths, additional_directory_forms()):
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
            "/run/credentials/../jq-production-docker.service",
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
        for key in ("credentials", "QUANT_JQ_CREDENTIAL", "quant_jq_credentials",
                    "QUANT_JQ_PASSWORD", "QUANT_JQ_TOKEN", "QUANT_JQ_CLIENT_SECRET"):
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
            value.replace(b"jq-", b"j\\u0071-"),
        ):
            content = raw.replace(value, replacement)
            with self.subTest(replacement=replacement):
                self.assertEqual(SCANNER.mask_systemd_directory_references(content), content)
        escaped_key = raw.replace(b"QUANT", b"QU\\u0041NT")
        self.assertEqual(SCANNER.mask_systemd_directory_references(escaped_key), escaped_key)
        self.assert_blocked(escaped_key)
        # These raw assignments were already detected and must stay detected.
        self.assert_blocked(raw.replace(value, value + b' ""'))
        self.assert_blocked(raw.replace(value, value.replace(b"jq-", b"j\\u0071-")))

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
            ("s" + "k-proj-" + OPAQUE_VALUE, b"sk-", b"s\\u006b-"),
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
                    'case "$3" in file|blob|report-file|report-blob)\n'
                    f'  exit {exit_code}\n'
                    'esac\n'
                    f'exec {shlex.quote(sys.executable)} "$@"\n'
                )
                wrapper.chmod(0o755)
                self.env["PATH"] = str(binary) + os.pathsep + os.defpath
                summary = self.assert_blocked(schema())
                self.assertEqual(summary["diagnostics"]["content_scan"]["detector"],
                                 "content-scan-error")

    def test_scanner_reports_preserve_legacy_verdict_and_candidate_protocol(self) -> None:
        self.write(environment_item())
        self.commit_fixture("Synthetic scanner report fixture")
        oid = self.git("rev-parse", "HEAD:" + self.path)
        for kind, source in (("file", str(self.source)), ("blob", oid), ("candidate", self.path)):
            with self.subTest(kind=kind):
                command = [sys.executable, str(ROOT / "git-finalize-content-scan.py")]
                arguments = [source, str(self.repo), "", ""]
                legacy = subprocess.check_output([*command, kind, *arguments], text=True).split()
                reported = subprocess.check_output([*command, "report-" + kind, *arguments],
                                                   text=True).split()
                self.assertEqual(legacy, reported[:5] if kind == "candidate" else reported[:1])
                self.assertEqual(reported[3], hashlib.sha256(self.source.read_bytes()).hexdigest())
                self.assertEqual(reported[4], "-" if kind == "file" else oid)
                self.assertEqual(reported[5],
                                 hashlib.sha256((ROOT / "git-finalize-content-scan.py").read_bytes()).hexdigest())

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
