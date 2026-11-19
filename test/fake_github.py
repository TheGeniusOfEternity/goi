"""Deterministic in-memory GitHub for offline protocol/integration tests."""

import errno
import hashlib
import json

from goi.github import blob_sha
from goi.protocol import Error


class FakeGitHub:
    def __init__(self):
        self.blobs, self.trees, self.commits = {}, {}, {}
        self.blob_reads = 0
        self.fail_update_after_success = False
        self.writable = True
        files = {"README.md": b"hello\n", "src/main.c": b"int main(void) { return 0; }\n",
                 "binary": bytes(range(256))}
        entries = {}
        for path, data in files.items():
            sha = self.create_blob("alice", "demo", data)
            entries[path] = {"path": path, "mode": "100644", "type": "blob", "sha": sha}
        tree = self.make_tree(entries)
        sha = self.create_commit("alice", "demo", None, tree, "initial", {"name": "Alice"})
        self.heads = {"main": sha, "feature/vfs": sha}

    def __call__(self, token=""):
        return self

    def profile(self):
        return {"id": 123, "login": "alice", "name": "Alice", "email": None}

    def repository(self, owner, repo):
        return {"name": repo, "owner": {"login": owner}, "permissions": {"push": self.writable}}

    def repositories(self, owner, personal=False):
        return [self.repository(owner, "demo")]

    def organizations(self):
        return [{"login": "example-org"}]

    def branches(self, owner, repo):
        return [{"name": name, "commit": {"sha": sha}, "protected": False} for name, sha in self.heads.items()]

    def head(self, owner, repo, branch):
        return self.heads[branch]

    def commit(self, owner, repo, sha):
        return self.commits[sha]

    def tree(self, owner, repo, sha):
        return self.trees[sha]["entries"]

    def full_tree(self, owner, repo, sha):
        return {path: {**entry, "size": len(self.blobs[entry["sha"]])}
                for path, entry in self.trees[sha]["flat"].items()}

    def blob(self, owner, repo, sha):
        self.blob_reads += 1
        return self.blobs[sha]

    def create_blob(self, owner, repo, data):
        sha = blob_sha(data)
        self.blobs[sha] = data
        return sha

    def make_tree(self, flat):
        tree, directories = [], {}
        for path, entry in flat.items():
            first, separator, rest = path.partition("/")
            if separator:
                directories.setdefault(first, {})[rest] = {**entry, "path": rest}
            else:
                tree.append({**entry, "path": first, "size": len(self.blobs[entry["sha"]])})
        for name, children in directories.items():
            tree.append({"path": name, "mode": "040000", "type": "tree", "sha": self.make_tree(children)})
        tree.sort(key=lambda item: item["path"])
        sha = hashlib.sha1(json.dumps(tree, sort_keys=True).encode()).hexdigest()
        self.trees[sha] = {"entries": tree, "flat": dict(flat)}
        return sha

    def delete_file(self, owner, repo, branch, path, sha, message, author):
        parent = self.heads[branch]
        base = self.commits[parent]["tree"]["sha"]
        assert self.trees[base]["flat"][path]["sha"] == sha
        tree = self.create_tree(owner, repo, base, [
            {"path": path, "mode": "100644", "type": "blob", "sha": None}])
        head = self.create_commit(owner, repo, parent, tree, message, author)
        self.update_head(owner, repo, branch, head)
        return head

    def create_tree(self, owner, repo, base, entries):
        flat = self.full_tree(owner, repo, base)
        for entry in entries:
            if entry["sha"] is None:
                flat.pop(entry["path"], None)
            else:
                flat[entry["path"]] = entry
        return self.make_tree(flat)

    def create_commit(self, owner, repo, parent, tree, message, author):
        payload = {"parents": [parent] if parent else [], "tree": {"sha": tree}, "message": message, "author": author}
        sha = hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        self.commits[sha] = payload
        return sha

    def update_head(self, owner, repo, branch, sha):
        if self.commits[sha]["parents"] != [self.heads[branch]]:
            raise Error(errno.EAGAIN, "not a fast-forward")
        self.heads[branch] = sha
        if self.fail_update_after_success:
            self.fail_update_after_success = False
            raise Error(errno.EHOSTUNREACH, "reply lost after successful update")

    def contains(self, owner, repo, head, target):
        while head:
            if head == target:
                return True
            parents = self.commits[head]["parents"]
            head = parents[0] if parents else None
        return False

    def external_edit(self, path, data):
        parent = self.heads["main"]
        base = self.commits[parent]["tree"]["sha"]
        sha = self.create_blob("alice", "demo", data)
        tree = self.create_tree("alice", "demo", base,
                                [{"path": path, "mode": "100644", "type": "blob", "sha": sha}])
        commit = self.create_commit("alice", "demo", parent, tree, "external", {"name": "Other"})
        self.heads["main"] = commit
        return commit

    def device_start(self, client_id):
        return {"device_code": "private-device-code", "user_code": "TEST-CODE",
                "verification_uri": "https://github.com/login/device", "expires_in": 900, "interval": 0}

    def device_poll(self, client_id, code):
        return {"access_token": "test-token"}
