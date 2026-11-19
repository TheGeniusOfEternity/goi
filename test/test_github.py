"""Exercise the real HTTP adapter against a local GitHub API fixture."""
import base64
import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from goi.github import GitHub, blob_sha
from goi.protocol import Error


class APIHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.respond()

    def do_POST(self):
        self.respond()

    def do_DELETE(self):
        self.respond()

    def do_PATCH(self):
        self.respond()

    def respond(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.requests.append((self.command, self.path, dict(self.headers), body))
        status, value = self.server.route(self.command, self.path, body)
        payload = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), APIHandler)
        self.server.requests = []
        self.server.route = lambda *_args: (200, {})
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        endpoint = f"http://127.0.0.1:{self.server.server_port}"
        self.api = GitHub("test-token", endpoint, endpoint)

    def test_binary_blob_checks_git_hash(self):
        data = bytes(range(256))
        self.server.route = lambda *_args: (200, {"encoding": "base64", "content": base64.b64encode(data).decode()})
        self.assertEqual(self.api.blob("alice", "demo", blob_sha(data)), data)
        with self.assertRaises(Error) as error:
            self.api.blob("alice", "demo", "0" * 40)
        self.assertEqual(error.exception.code, errno.EIO)

    def test_branch_encoding_and_non_forced_update(self):
        self.api.update_head("alice", "demo", "feature/test branch", "abc")
        method, path, headers, body = self.server.requests[-1]
        self.assertEqual(method, "PATCH")
        self.assertTrue(path.endswith("feature%2Ftest%20branch"))
        self.assertEqual(headers["Authorization"], "Bearer test-token")
        self.assertEqual(json.loads(body), {"sha": "abc", "force": False})

    def test_delete_file_and_empty_tree(self):
        self.server.route = lambda *_args: (200, {"commit": {"sha": "empty-commit"}})
        self.assertEqual(self.api.delete_file("alice", "demo", "main", "README.md",
                                             "blob-id", "Delete", {"name": "Alice"}), "empty-commit")
        method, path, _, body = self.server.requests[-1]
        self.assertEqual((method, path), ("DELETE", "/repos/alice/demo/contents/README.md"))
        self.assertEqual(json.loads(body)["sha"], "blob-id")
        self.assertEqual(json.loads(body)["branch"], "main")
        count = len(self.server.requests)
        self.assertEqual(self.api.full_tree("alice", "demo",
                         "4b825dc642cb6eb9a060e54bf8d69288fbee4904"), {})
        self.assertEqual(len(self.server.requests), count)

    def test_device_flow_uses_form_and_never_sends_access_token(self):
        self.api.device_start("app-client")
        _, _, headers, body = self.server.requests[-1]
        self.assertNotIn("Authorization", headers)
        self.assertEqual(parse_qs(body.decode())["client_id"], ["app-client"])
        self.api.device_poll("app-client", "device-code")
        self.assertIn("device_code=device-code", self.server.requests[-1][3].decode())

    def test_pagination_loads_second_page(self):
        self.server.route = lambda _m, path, _b: (200, list(range(100)) if parse_qs(urlsplit(path).query)["page"] == ["1"] else [100])
        self.assertEqual(len(self.api.pages("/user/repos?affiliation=owner")), 101)

    def test_truncated_tree_is_never_silently_accepted(self):
        self.server.route = lambda *_args: (200, {"tree": [], "truncated": True})
        with self.assertRaises(Error) as error:
            self.api.tree("alice", "demo", "tree")
        self.assertEqual(error.exception.code, errno.EOVERFLOW)

    def test_error_mapping(self):
        for status, code in ((401, errno.EACCES), (404, errno.ENOENT), (409, errno.EAGAIN), (429, errno.EAGAIN), (500, errno.EIO)):
            with self.subTest(status=status):
                self.server.route = lambda *_args: (status, {"message": "fixture rejection"})
                with self.assertRaises(Error) as error:
                    self.api.profile()
                self.assertEqual(error.exception.code, code)

    def test_multifile_git_data_payload(self):
        self.server.route = lambda *_args: (200, {"sha": "tree-id"})
        entries = [{"path": "a", "mode": "100644", "type": "blob", "sha": "a"},
                   {"path": "b", "mode": "100644", "type": "blob", "sha": None}]
        self.assertEqual(self.api.create_tree("alice", "demo", "parent-tree", entries), "tree-id")
        self.assertEqual(json.loads(self.server.requests[-1][3]), {"base_tree": "parent-tree", "tree": entries})


if __name__ == "__main__":
    unittest.main(verbosity=2)
