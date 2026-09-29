from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
import urllib.parse


ROOT = Path(__file__).resolve().parents[1]


class GiteaReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="gf-release-publisher-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "release-notes.md").write_text("Synthetic release notes\n")
        dist = self.root / "dist"
        dist.mkdir()
        (dist / "git-finalizer-1.6.0.tar.gz").write_bytes(b"synthetic archive bytes")
        digest = hashlib.sha256(b"synthetic archive bytes").hexdigest()
        (dist / "SHA256SUMS.txt").write_text(f"{digest}  git-finalizer-1.6.0.tar.gz\n")
        self.assets: dict[str, bytes] = {}
        self.downloads = 0
        self.published = False
        self.existing = False
        self.corrupt = False
        self.extra_asset = False
        self.redirect_url: str | None = None
        self.foreign_authorization: str | None = None
        self.foreign_downloads = 0
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_arguments: object) -> None:
                pass

            def send(self, status: int, value: object) -> None:
                body = value if isinstance(value, bytes) else json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                path = urllib.parse.urlsplit(self.path).path
                if "/tags/" in path:
                    self.send(200 if fixture.existing else 404, {})
                elif path.endswith("/assets"):
                    listing = [{"name": name} for name in fixture.assets]
                    if fixture.extra_asset:
                        listing.append({"name": "unexpected.txt"})
                    self.send(200, listing)
                elif path.startswith("/download/"):
                    fixture.downloads += 1
                    if fixture.redirect_url:
                        self.send_response(302)
                        self.send_header("Location", fixture.redirect_url)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                    else:
                        name = urllib.parse.unquote(path.removeprefix("/download/"))
                        self.send(200, b"corrupted" if fixture.corrupt else fixture.assets[name])
                else:
                    self.send(404, {})

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if "/assets?" in self.path:
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                    name = query["name"][0]
                    fixture.assets[name] = body
                    self.send(201, {"name": name, "browser_download_url": fixture.url + "/download/" + name})
                else:
                    self.send(201, {"id": 1})

            def do_PATCH(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                fixture.published = True
                self.send(200, {"draft": False})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def publish(self) -> subprocess.CompletedProcess[str]:
        workflow = (ROOT / ".gitea/workflows/release.yml").read_text()
        script = textwrap.dedent(workflow.split("python3 -B - <<'PY'\n", 1)[1].split("\n          PY", 1)[0])
        return subprocess.run(
            [sys.executable, "-B", "-"], input=script, cwd=self.root,
            env={"PATH": os.defpath, "GITEA_API_URL": self.url + "/api/v1",
                 "GITEA_REPOSITORY": "synthetic/example", "GITEA_TOKEN": "synthetic-fixture",
                 "RELEASE_TAG": "v1.6.0", "RELEASE_SHA": "a" * 40},
            text=True, capture_output=True, timeout=15,
        )

    def test_every_asset_is_downloaded_and_verified_before_publication(self) -> None:
        result = self.publish()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.downloads, 2)
        self.assertTrue(self.published)

    def test_corrupted_download_retains_the_draft(self) -> None:
        self.corrupt = True
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.published)
        self.assertIn("draft release retained", result.stderr)

    def test_unexpected_remote_asset_retains_the_draft(self) -> None:
        self.extra_asset = True
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.downloads, 2)
        self.assertFalse(self.published)

    def test_existing_release_is_never_replayed(self) -> None:
        self.existing = True
        result = self.publish()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.assets)
        self.assertFalse(self.published)

    def test_download_redirect_does_not_forward_the_token(self) -> None:
        fixture = self

        class DownloadHandler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_arguments: object) -> None:
                pass

            def do_GET(self) -> None:
                fixture.foreign_authorization = self.headers.get("Authorization")
                fixture.foreign_downloads += 1
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"synthetic archive bytes")

        server = ThreadingHTTPServer(("127.0.0.1", 0), DownloadHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.redirect_url = f"http://127.0.0.1:{server.server_port}/asset"
            result = self.publish()
            self.assertNotEqual(result.returncode, 0)  # checksum asset bytes differ
            self.assertEqual(self.foreign_downloads, 1)
            self.assertIsNone(self.foreign_authorization)
            self.assertFalse(self.published)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == "__main__":
    unittest.main()
