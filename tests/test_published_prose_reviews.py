from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import unittest

import test_fixture_exceptions as fixtures


BASELINE = ('BODY_REFUSAL_REASONS = {\n'
            '    "text contains a residual authorization credential": "a credential survived redaction",\n'
            '}\n')
PREPARED = BASELINE + '# Additional task-owned implementation\n'


class PublishedProseReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.FixtureExceptionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        f.path = 'src/collect.py'
        (f.repo / 'src').mkdir()
        (f.repo / f.path).write_text(BASELINE)
        f.commit()
        f.git('push', '--quiet', '--set-upstream', 'origin', 'main')
        (f.repo / f.path).write_text(PREPARED)
        self.review()

    def review(self) -> None:
        f = self.fixture
        raw = (f.repo / f.path).read_bytes()
        f.approval['exceptions'] = [{
            'path': f.path, 'blob_oid': f.git('hash-object', '--no-filters', '--', f.path),
            'sha256': hashlib.sha256(raw).hexdigest(), 'detector': 'credential-assignment-v1',
            'reason': 'Preserve the already-published ordinary refusal-reason literals',
        }]
        f.write_approval()

    def run_finalizer(self, *, approved=True, resume=False):
        f = self.fixture
        command = [str(fixtures.FINALIZER), '--summary', '--repo', str(f.repo)]
        command += ['--resume-publish', f.git('rev-parse', 'HEAD')] if resume else ['--message', 'Preserve published prose']
        if approved:
            command += ['--fixture-exceptions', str(f.file)]
        if not resume:
            command += ['--', f.path]
        result = subprocess.run(command, env=f.env, capture_output=True, text=True, timeout=30)
        return result.returncode, json.loads(result.stdout), result.stdout + result.stderr

    def assert_blocked(self, **kwargs):
        f = self.fixture
        head = f.git('rev-parse', 'HEAD')
        remote = f.git('--git-dir=' + str(f.remote), 'rev-parse', 'refs/heads/main')
        code, data, output = self.run_finalizer(**kwargs)
        self.assertNotEqual(code, 0, data)
        self.assertFalse(data['push']['executed'])
        self.assertEqual(head, f.git('rev-parse', 'HEAD'))
        self.assertEqual(remote, f.git('--git-dir=' + str(f.remote), 'rev-parse', 'refs/heads/main'))
        return output

    def test_normal_and_resume_reuse_only_exact_published_literals(self):
        self.assert_blocked(approved=False)
        code, data, _ = self.run_finalizer()
        self.assertEqual(code, 0, data)
        self.assertEqual(data['mode_result']['post_verify'], 'passed')
        f = self.fixture
        (f.repo / f.path).write_text(PREPARED + '# Second implementation change\n')
        self.review()
        f.commit()
        head = f.git('rev-parse', 'HEAD')
        code, data, _ = self.run_finalizer(resume=True)
        self.assertEqual(code, 0, data)
        self.assertEqual(head, f.git('rev-parse', 'HEAD'))
        self.assertEqual(head, f.git('--git-dir=' + str(f.remote), 'rev-parse', 'refs/heads/main'))

    def test_changed_or_added_published_literals_are_refused(self):
        f = self.fixture
        for content in (
            PREPARED.replace('survived redaction', 'escaped the redaction'),
            PREPARED.replace('text contains a residual', 'text preserves a previously residual'),
            PREPARED + BASELINE,
        ):
            (f.repo / f.path).write_text(content)
            self.review()
            self.assert_blocked()

    def test_ordinary_assignments_and_same_line_trailers_are_refused(self):
        f = self.fixture
        synthetic = 'token' + ' = "' + 'SYNTHETIC_VALUE_FOR_TEST_ONLY' + '"\n'
        for content in (PREPARED + synthetic, BASELINE.rstrip() + '; ' + synthetic):
            (f.repo / f.path).write_text(content)
            self.review()
            output = self.assert_blocked()
            self.assertNotIn('SYNTHETIC_VALUE_FOR_TEST_ONLY', output)

    def test_comments_between_published_literals_do_not_qualify(self):
        f = self.fixture
        content = BASELINE.replace(': "a credential',
            ': # password = "SYNTHETIC_COMMENT_VALUE"\n    "a credential')
        (f.repo / f.path).write_text(content)
        f.commit()
        f.git('push', '--quiet', 'origin', 'main')
        (f.repo / f.path).write_text(content + '# Implementation change\n')
        self.review()
        output = self.assert_blocked()
        self.assertNotIn('SYNTHETIC_COMMENT_VALUE', output)

    def test_config_keys_and_non_refusal_declarations_are_refused(self):
        f = self.fixture
        for content in (
            PREPARED.replace('BODY_REFUSAL_REASONS', 'settings'),
            PREPARED.replace('text contains a residual authorization credential', 'credential'),
            'def local():\n' + ''.join('    ' + line for line in PREPARED.splitlines(keepends=True)),
        ):
            (f.repo / f.path).write_text(content)
            self.review()
            self.assert_blocked()

    def test_other_detectors_and_source_review_rules_remain_strict(self):
        f = self.fixture
        (f.repo / f.path).write_text(PREPARED + '# ' + fixtures.SYNTHETIC)
        self.review()
        self.assert_blocked()
        f.approval['exceptions'][0]['detector'] = 'private-key-header-v1'
        f.write_approval()
        self.assert_blocked()

    def test_missing_published_file_is_refused(self):
        f = self.fixture
        old = f.repo / f.path
        f.path = 'src/new.py'
        (f.repo / f.path).write_bytes(old.read_bytes())
        old.write_text(BASELINE)
        self.review()
        self.assert_blocked()

    def test_invalid_encoding_and_syntax_do_not_leak_source(self):
        f = self.fixture
        for raw in (PREPARED.encode() + b'\xff', PREPARED.encode() + b'SYNTHETIC_PRIVATE_TEXT (\n'):
            (f.repo / f.path).write_bytes(raw)
            self.review()
            output = self.assert_blocked()
            self.assertNotIn('SYNTHETIC_PRIVATE_TEXT', output)

    def test_absolute_source_review_is_rejected_before_file_read(self):
        f = self.fixture
        outside = f.base / 'outside.py'
        outside.write_text(PREPARED)
        marker = f.base / 'unauthorized-read'
        bindir = f.base / 'guard-bin'
        bindir.mkdir()
        interpreter = bindir / 'python3'
        interpreter.write_text(f"#!{sys.executable}\n" + """
import os, sys
def audit(event, args):
    if event == 'open' and args[0] == os.environ['GUARDED_TEST_PATH']:
        with open(os.environ['GUARDED_TEST_MARKER'], 'w') as marker:
            marker.write('read attempted')
        raise ValueError('unauthorized fixture read')
if sys.argv[1:3] == ['-B', '-']:
    sys.addaudithook(audit)
    sys.argv = sys.argv[2:]
    exec(compile(sys.stdin.read(), '<fixture-interpreter>', 'exec'))
else:
    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])
""")
        interpreter.chmod(0o755)
        f.env.update(PATH=str(bindir) + os.pathsep + f.env['PATH'],
                     GUARDED_TEST_PATH=str(outside), GUARDED_TEST_MARKER=str(marker))
        f.approval['exceptions'][0]['path'] = str(outside)
        f.write_approval()
        self.assert_blocked()
        self.assertFalse(marker.exists(), 'absolute review path was read before rejection')

    def test_published_prose_review_remains_unavailable_in_local_modes(self):
        f = self.fixture

        def state():
            return (
                f.git('rev-parse', 'HEAD'), f.git('ls-files', '--stage'),
                f.git('status', '--porcelain=v1', '--untracked-files=all'),
                f.git('--git-dir=' + str(f.remote), 'rev-parse', 'refs/heads/main'),
                hashlib.sha256((f.repo / f.path).read_bytes()).hexdigest(),
            )

        for mode in ('verify-only', 'commit-only'):
            with self.subTest(mode=mode):
                before = state()
                command = [str(fixtures.FINALIZER), '--summary', '--repo', str(f.repo),
                           '--mode', mode]
                if mode == 'commit-only':
                    command += ['--message', 'Preserve published prose locally']
                command += ['--fixture-exceptions', str(f.file), '--', f.path]
                result = subprocess.run(command, env=f.env, capture_output=True,
                                        text=True, timeout=30)
                data = json.loads(result.stdout)
                self.assertNotEqual(result.returncode, 0, data)
                self.assertEqual(data['status'], 'blocked')
                self.assertFalse(data['commit']['created'])
                self.assertFalse(data['push']['executed'])
                self.assertEqual(data['final_phase'], 'preflight')
                self.assertEqual(data['reason'], 'fixture exception 文件无效；未执行 push')
                self.assertEqual(before, state())

    def test_repository_blob_hash_and_file_size_still_bind_review(self):
        f = self.fixture
        original = copy.deepcopy(f.approval)
        for key, value in (('blob_oid', 'f' * 40), ('sha256', 'f' * 64), ('path', 'README.md')):
            f.approval = copy.deepcopy(original)
            f.approval['exceptions'][0][key] = value
            f.write_approval()
            self.assert_blocked()
        (f.repo / f.path).write_text(PREPARED + '#' * (1024 * 1024))
        self.review()
        self.assert_blocked()
