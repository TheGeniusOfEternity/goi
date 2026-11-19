"""GitHub HTTPS adapter. No Git executable or third-party packages required."""

import base64
import errno
import hashlib
import json
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .protocol import Error


def blob_sha(data):
    return hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()


class GitHub:
    def __init__(self, token="", api="https://api.github.com",
                 oauth="https://github.com", timeout=20):
        self.token, self.api, self.oauth, self.timeout = token, api.rstrip("/"), oauth.rstrip("/"), timeout

    def request(self, method, path, data=None, *, oauth=False):
        headers = {"Accept": "application/json", "User-Agent": "goi-vtfs/1"}
        if self.token and not oauth:
            headers["Authorization"] = "Bearer " + self.token
        if data is not None:
            if oauth:
                payload = urlencode(data).encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                payload = json.dumps(data).encode()
                headers["Content-Type"] = "application/json"
        else:
            payload = None
        url = (self.oauth if oauth else self.api) + path
        try:
            with urlopen(Request(url, payload, headers, method=method), timeout=self.timeout) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except HTTPError as exc:
            codes = {401: errno.EACCES, 403: errno.EACCES, 404: errno.ENOENT,
                     409: errno.EAGAIN, 422: errno.EINVAL, 429: errno.EAGAIN}
            # Never include response headers or access tokens in errors/logs.
            try:
                detail = json.loads(exc.read(8192)).get("message", "request rejected")
            except (ValueError, AttributeError):
                detail = "request rejected"
            finally:
                exc.close()
            raise Error(codes.get(exc.code, errno.EIO), f"GitHub HTTP {exc.code} ({method} {path}): {detail}") from None
        except (URLError, TimeoutError, socket.timeout, OSError):
            raise Error(errno.EHOSTUNREACH, "GitHub is unreachable; saved changes are retained") from None
        except (ValueError, KeyError):
            raise Error(errno.EPROTO, "invalid GitHub response") from None

    def pages(self, path):
        result = []
        for page in range(1, 10001):
            separator = "&" if "?" in path else "?"
            batch = self.request("GET", f"{path}{separator}per_page=100&page={page}")
            if not isinstance(batch, list):
                raise Error(errno.EPROTO, "expected a GitHub list")
            result.extend(batch)
            if len(batch) < 100:
                return result
        raise Error(errno.EOVERFLOW, "GitHub pagination limit exceeded")

    @staticmethod
    def repo_path(owner, repo):
        return "/repos/" + quote(owner, safe="") + "/" + quote(repo, safe="")

    def profile(self):
        return self.request("GET", "/user")

    def repositories(self, owner, personal=False):
        if personal:
            return self.pages("/user/repos?affiliation=owner&visibility=all")
        return self.pages("/orgs/" + quote(owner, safe="") + "/repos?type=all")

    def organizations(self):
        return self.pages("/user/orgs")

    def repository(self, owner, repo):
        return self.request("GET", self.repo_path(owner, repo))

    def branches(self, owner, repo):
        return self.pages(self.repo_path(owner, repo) + "/branches")

    def head(self, owner, repo, branch):
        return self.request("GET", self.repo_path(owner, repo) + "/git/ref/heads/" + quote(branch, safe=""))["object"]["sha"]

    def commit(self, owner, repo, sha):
        return self.request("GET", self.repo_path(owner, repo) + "/git/commits/" + sha)

    def tree(self, owner, repo, sha):
        # GitHub can return 404 for the canonical empty Git tree.
        if sha == hashlib.sha1(b"tree 0\0").hexdigest():
            return []
        result = self.request("GET", self.repo_path(owner, repo) + "/git/trees/" + sha)
        if result.get("truncated"):
            raise Error(errno.EOVERFLOW, "GitHub directory response was truncated")
        return result["tree"]

    def full_tree(self, owner, repo, sha):
        # Traverse non-recursively returned trees: no silently truncated recursive response.
        result, pending = {}, [("", sha)]
        while pending:
            prefix, tree = pending.pop()
            for entry in self.tree(owner, repo, tree):
                path = prefix + entry["path"]
                if entry["type"] == "tree":
                    pending.append((path + "/", entry["sha"]))
                else:
                    result[path] = entry
        return result

    def blob(self, owner, repo, sha):
        item = self.request("GET", self.repo_path(owner, repo) + "/git/blobs/" + sha)
        if item.get("encoding") != "base64":
            raise Error(errno.EPROTO, "unsupported blob encoding")
        data = base64.b64decode(item["content"])
        if blob_sha(data) != sha:
            raise Error(errno.EIO, "GitHub blob checksum mismatch")
        return data

    def create_blob(self, owner, repo, data):
        return self.request("POST", self.repo_path(owner, repo) + "/git/blobs",
                            {"content": base64.b64encode(data).decode(), "encoding": "base64"})["sha"]

    def create_tree(self, owner, repo, base, entries):
        return self.request("POST", self.repo_path(owner, repo) + "/git/trees",
                            {"base_tree": base, "tree": entries})["sha"]

    def create_commit(self, owner, repo, parent, tree, message, author):
        return self.request("POST", self.repo_path(owner, repo) + "/git/commits",
                            {"parents": [parent], "tree": tree, "message": message,
                             "author": author, "committer": author})["sha"]

    def update_head(self, owner, repo, branch, sha):
        self.request("PATCH", self.repo_path(owner, repo) + "/git/refs/heads/" + quote(branch, safe=""),
                     {"sha": sha, "force": False})

    def delete_file(self, owner, repo, branch, path, sha, message, author):
        result = self.request("DELETE", self.repo_path(owner, repo) + "/contents/" + quote(path, safe="/"),
                              {"branch": branch, "sha": sha, "message": message,
                               "author": author, "committer": author})
        return result["commit"]["sha"]

    def device_start(self, client_id):
        return self.request("POST", "/login/device/code",
                            {"client_id": client_id, "scope": "repo read:org read:user"}, oauth=True)

    def device_poll(self, client_id, device_code):
        return self.request("POST", "/login/oauth/access_token",
                            {"client_id": client_id, "device_code": device_code,
                             "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}, oauth=True)
