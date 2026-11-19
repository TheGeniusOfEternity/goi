"""Durable, lazy GitHub working copies. SQLite transactions are the fsync boundary."""

import errno
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time
from urllib.parse import quote

from .github import GitHub, blob_sha
from .protocol import MAX_FILE_SIZE, require


class Store:
    def __init__(self, directory, client_id="", github_factory=GitHub, cache_bytes=256 * 1024 * 1024):
        require(cache_bytes >= 0, message="cache limit must be nonnegative")
        self.cache_bytes = cache_bytes
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.db = sqlite3.connect(self.directory / "goi.sqlite3", check_same_thread=False)
        os.chmod(self.directory / "goi.sqlite3", 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(Path(__file__).with_name("schema.sql").read_text())
        self.lock = threading.RLock()
        self.client_id, self.github_factory = client_id, github_factory
        if not self.db.execute("SELECT 1 FROM nodes WHERE id=1").fetchone():
            self.new_node(None, "", "root", stat.S_IFDIR | 0o755)
        with self.db:
            self.migrate_owners()
            self.migrate_branches()
            self.db.execute("UPDATE nodes SET name='.info.json' "
                            "WHERE kind='info' AND name='info.json'")
        self.db.commit()

    def migrate_owners(self):
        """Move old containers without changing repository IDs or working files."""
        containers = self.db.execute(
            "SELECT * FROM nodes WHERE kind IN ('personal','orgs') AND deleted=0").fetchall()
        for container in containers:
            target = container["parent"] if container["kind"] == "personal" else 1
            for child in self.children(container["id"]):
                require(self.child(target, child["name"]) is None, errno.EEXIST,
                        "owner migration has duplicate names; original data retained")
                self.update(child["id"], parent=target)
            if container["kind"] == "personal":
                self.update(target, loaded=container["loaded"])
            self.db.execute("DELETE FROM nodes WHERE id=?", (container["id"],))
        if containers:
            self.update(1, loaded=0)

    def migrate_branches(self):
        """Flatten branch containers while preserving node IDs and local edits."""
        containers = self.db.execute(
            "SELECT id,parent FROM nodes WHERE kind='branches'").fetchall()
        for container in containers:
            children = self.children(container["id"])
            self.db.execute("DELETE FROM nodes WHERE id=?", (container["id"],))
            for child in children:
                require(self.child(container["parent"], child["name"]) is None,
                        errno.EEXIST, "branch migration has duplicate names; original data retained")
                self.update(child["id"], parent=container["parent"])

    def close(self):
        self.db.close()

    def new_node(self, parent, name, kind, mode, **values):
        if parent is not None:
            self.check_name(name)
        fields = {"parent": parent, "name": name, "kind": kind, "mode": mode,
                  "mtime_ns": time.time_ns(), **values}
        cursor = self.db.execute(
            "INSERT INTO nodes (" + ",".join(fields) + ") VALUES (" + ",".join("?" for _ in fields) + ")",
            tuple(fields.values()),
        )
        return self.node(cursor.lastrowid)

    @staticmethod
    def check_name(name):
        require(isinstance(name, str) and name not in ("", ".", "..") and "/" not in name
                and "\0" not in name, message="invalid filename")
        require(len(name.encode()) <= 255, errno.ENAMETOOLONG, "filename exceeds 255 bytes")

    def node(self, number, *, deleted=False):
        row = self.db.execute("SELECT * FROM nodes WHERE id=?", (int(number),)).fetchone()
        require(row is not None and (deleted or not row["deleted"]), errno.ENOENT, "object not found")
        return dict(row)

    def update(self, number, **values):
        self.db.execute("UPDATE nodes SET " + ",".join(key + "=?" for key in values) + " WHERE id=?",
                        (*values.values(), number))
        return self.node(number, deleted=True)

    def child(self, parent, name):
        row = self.db.execute("SELECT id FROM nodes WHERE parent=? AND name=? AND deleted=0",
                              (parent, name)).fetchone()
        return self.node(row[0]) if row else None

    def children(self, parent):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM nodes WHERE parent=? AND deleted=0 ORDER BY name COLLATE BINARY", (parent,))]

    def github(self, account):
        row = self.db.execute("SELECT token FROM accounts WHERE id=?", (account,)).fetchone()
        require(row is not None and row[0], errno.EACCES, "account is not authenticated; run goi auth")
        return self.github_factory(row[0])

    def repo(self, number):
        row = self.db.execute("SELECT * FROM repos WHERE id=?", (number,)).fetchone()
        require(row is not None, errno.ENOENT, "repository not found")
        return dict(row)

    def branch(self, number):
        row = self.db.execute("SELECT * FROM branches WHERE id=?", (number,)).fetchone()
        require(row is not None, errno.ENOENT, "branch not found")
        return dict(row)

    def add_account(self, token):
        profile = self.github_factory(token).profile()
        login = profile["login"]
        existing = self.child(1, login)
        if existing:
            self.db.execute("UPDATE accounts SET token=? WHERE id=?", (token, existing["id"]))
            return existing
        account = self.new_node(1, login, "account", stat.S_IFDIR | 0o755)
        self.update(account["id"], account=account["id"])
        self.db.execute("INSERT INTO accounts VALUES (?,?,?)", (account["id"], login, token))
        self.update(1, loaded=0)
        return self.node(account["id"])

    def add_repo(self, parent, item):
        existing = self.child(parent["id"], item["name"])
        if existing:
            return existing
        permissions = item.get("permissions", {})
        writable = bool(permissions.get("push") or permissions.get("admin")) and not item.get("archived", False)
        node = self.new_node(parent["id"], item["name"], "repo", stat.S_IFDIR | 0o755,
                             account=parent["account"], writable=0, loaded=1)
        self.update(node["id"], repo=node["id"])
        self.db.execute("INSERT INTO repos(id,account,owner,name) VALUES (?,?,?,?)",
                        (node["id"], parent["account"], item["owner"]["login"], item["name"]))
        info = {"owner": item["owner"]["login"], "name": item["name"], "active": False,
                "writable": writable, "hint": "goi activate <repository-path>"}
        self.new_node(node["id"], ".info.json", "info", stat.S_IFREG | 0o444,
                      data=json.dumps(info, indent=2).encode(), size=len(json.dumps(info, indent=2).encode()),
                      account=parent["account"], repo=node["id"], loaded=1)
        return self.node(node["id"])

    def load_directory(self, node):
        require(stat.S_ISDIR(node["mode"]), errno.ENOTDIR, "not a directory")
        if node["kind"] == "root":
            require(self.db.execute("SELECT 1 FROM accounts LIMIT 1").fetchone() is not None,
                    errno.EACCES, "sign in with goi auth before browsing /github")
        if node["loaded"]:
            return
        kind = node["kind"]
        if kind == "root":
            for account in self.db.execute("SELECT id FROM accounts WHERE token IS NOT NULL").fetchall():
                for org in self.github(account["id"]).organizations():
                    if self.child(1, org["login"]) is None:
                        self.new_node(1, org["login"], "org", stat.S_IFDIR | 0o755,
                                      account=account["id"])
            self.update(1, loaded=1)
            return
        api = self.github(node["account"])
        if kind in ("account", "org"):
            owner = node["name"]
            for repo in api.repositories(owner, personal=kind == "account"):
                if repo["owner"]["login"].casefold() == owner.casefold():
                    self.add_repo(node, repo)
        elif kind in ("branch", "dir"):
            repo = self.repo(node["repo"])
            for entry in api.tree(repo["owner"], repo["name"], node["tree_sha"]):
                # Symbolic links and submodules are intentionally not regular writable files.
                if entry["type"] not in ("tree", "blob") or entry["mode"] == "120000":
                    continue
                if entry["path"] in (".goi", ".git"):
                    continue
                isdir = entry["type"] == "tree"
                path = (node["base_path"] + "/" if node["base_path"] else "") + entry["path"]
                permissions = (0o755 if isdir or entry["mode"] == "100755" else 0o644)
                if not node["writable"]:
                    permissions &= ~0o222
                self.new_node(node["id"], entry["path"], "dir" if isdir else "file",
                              (stat.S_IFDIR if isdir else stat.S_IFREG) | permissions,
                              account=node["account"], repo=node["repo"], branch=node["branch"],
                              remote_sha=None if isdir else entry["sha"],
                              base_mode=entry["mode"],
                              tree_sha=entry["sha"] if isdir else None, base_path=path,
                              size=entry.get("size", 0), writable=node["writable"])
        self.update(node["id"], loaded=1)

    def refresh_owners(self, path):
        node = self.resolve_path(path)
        require(node["kind"] in ("root", "account", "org"),
                message="expected /github or an owner directory")
        self.load_directory(self.update(node["id"], loaded=0))
        return {"path": path}

    def lookup(self, parent, name):
        self.check_name(name)
        node = self.node(parent)
        self.load_directory(node)
        result = self.child(parent, name)
        require(result is not None, errno.ENOENT, "object not found")
        return result

    def list(self, number, offset=0, limit=100):
        node = self.node(number)
        self.load_directory(node)
        return self.children(number)[offset:offset + limit]

    def resolve_path(self, path):
        current = self.node(1)
        require(isinstance(path, str) and not path.startswith("/"), message="expected a mount-relative path")
        for component in path.split("/"):
            if component:
                current = self.lookup(current["id"], component)
        return current

    def activate(self, number):
        repo = self.repo(number)
        if repo["active"] and self.db.execute(
                "SELECT 1 FROM branches WHERE repo=?", (number,)).fetchone():
            return self.node(number)
        api = self.github(repo["account"])
        item = api.repository(repo["owner"], repo["name"])
        writable = bool(item.get("permissions", {}).get("push")) and not item.get("archived", False)
        branches = api.branches(repo["owner"], repo["name"])
        require(branches, errno.ENOENT,
                "repository has no branches; create the first commit on GitHub "
                "(for example, add README.md), then run goi activate again")
        parent = self.node(number)
        for branch in branches:
            head = branch["commit"]["sha"]
            commit = api.commit(repo["owner"], repo["name"], head)
            # Protection may allow PRs but not direct commits: conservatively expose read-only.
            can_write = writable and not branch.get("protected", False)
            node = self.new_node(parent["id"], quote(branch["name"], safe=""), "branch",
                                 stat.S_IFDIR | (0o755 if can_write else 0o555),
                                 account=repo["account"], repo=number, writable=can_write,
                                 tree_sha=commit["tree"]["sha"], base_path="")
            self.update(node["id"], branch=node["id"])
            self.db.execute("INSERT INTO branches VALUES (?,?,?,?,?)",
                            (node["id"], number, branch["name"], head, commit["tree"]["sha"]))
            self.make_controls(self.node(node["id"]))
        self.db.execute("UPDATE repos SET active=1 WHERE id=?", (number,))
        info = self.child(number, ".info.json")
        data = json.loads(info["data"])
        data["active"] = True
        raw = json.dumps(data, indent=2).encode()
        self.update(info["id"], data=raw, size=len(raw), revision=info["revision"] + 1)
        return self.node(number)

    def make_controls(self, branch):
        attrs = {"account": branch["account"], "repo": branch["repo"], "branch": branch["id"], "loaded": 1}
        goi = self.new_node(branch["id"], ".goi", "control_dir", stat.S_IFDIR | 0o755, **attrs)
        commit = self.new_node(goi["id"], "commit", "control_dir", stat.S_IFDIR | 0o755, **attrs)
        self.new_node(commit["id"], "changes", "changes", stat.S_IFDIR | 0o755, **attrs)
        self.new_node(commit["id"], "message", "message", stat.S_IFREG | 0o600,
                      writable=branch["writable"], data=b"", **attrs)
        data = b"gitdir: /GOI-GIT-IS-DISABLED-use-goi-instead\n"
        self.new_node(branch["id"], ".git", "git_trap", stat.S_IFREG | 0o444,
                      size=len(data), data=data, **attrs)

    def read(self, number, offset, count):
        node = self.node(number)
        require(stat.S_ISREG(node["mode"]), errno.EISDIR, "not a regular file")
        if node["data"] is None:
            repo = self.repo(node["repo"])
            require(node["size"] <= MAX_FILE_SIZE, errno.EFBIG, "file exceeds configured size limit")
            data = self.github(node["account"]).blob(repo["owner"], repo["name"], node["remote_sha"])
            node = self.update(number, data=data, size=len(data))
        result = node["data"][offset:offset + count]
        if node["kind"] == "file":
            self.db.execute("INSERT OR REPLACE INTO cache_access VALUES (?,?)", (number, time.time_ns()))
        self.trim_cache()
        return result

    def trim_cache(self, limit=None):
        """Discard only reproducible GitHub blobs; working copies are never cache."""
        limit = self.cache_bytes if limit is None else limit
        rows = self.db.execute(
            "SELECT n.id,length(n.data) AS bytes FROM nodes n "
            "LEFT JOIN cache_access c ON c.node=n.id "
            "WHERE n.kind='file' AND n.dirty=0 AND n.deleted=0 "
            "AND n.remote_sha IS NOT NULL AND n.data IS NOT NULL "
            "AND n.id NOT IN (SELECT node FROM conflicts) "
            "AND n.branch NOT IN (SELECT branch FROM publications) "
            "ORDER BY COALESCE(c.used_ns,0),n.id").fetchall()
        remaining = sum(row["bytes"] for row in rows)
        removed = 0
        for row in rows:
            if remaining <= limit:
                break
            self.db.execute("UPDATE nodes SET data=NULL WHERE id=?", (row["id"],))
            self.db.execute("DELETE FROM cache_access WHERE node=?", (row["id"],))
            remaining -= row["bytes"]
            removed += row["bytes"]
        return {"cached_bytes": remaining, "removed_bytes": removed, "limit_bytes": self.cache_bytes}

    def create(self, parent, name, mode):
        self.check_name(name)
        directory = self.node(parent)
        self.load_directory(directory)
        require(directory["kind"] in ("branch", "dir") and directory["writable"], errno.EACCES,
                "this directory is not writable")
        require(name not in (".git", ".goi"), errno.EPERM, "reserved GOI name; use goi")
        require(self.child(parent, name) is None, errno.EEXIST, "name already exists")
        kind = stat.S_IFMT(mode)
        require(kind in (stat.S_IFREG, stat.S_IFDIR), errno.EOPNOTSUPP, "unsupported object type")
        return self.new_node(parent, name, "file" if kind == stat.S_IFREG else "dir", mode,
                             account=directory["account"], repo=directory["repo"], branch=directory["branch"],
                             writable=1, loaded=1, dirty=1, data=b"" if kind == stat.S_IFREG else None)

    def remove(self, parent, name, directory=False):
        node = self.lookup(parent, name)
        require(node["kind"] in ("file", "dir") and node["writable"], errno.EACCES, "object is read-only")
        require(stat.S_ISDIR(node["mode"]) == directory,
                errno.ENOTDIR if directory else errno.EISDIR, "wrong object type")
        if directory:
            self.load_directory(node)
            require(not self.children(node["id"]), errno.ENOTEMPTY, "directory is not empty")
        self.update(node["id"], deleted=1, dirty=1, revision=node["revision"] + 1)

    def path(self, node):
        parts = []
        while node["kind"] != "branch":
            require(node["parent"] is not None, message="object is not in a branch")
            parts.append(node["name"])
            node = self.node(node["parent"], deleted=True)
        return "/".join(reversed(parts))

    def rename(self, old_parent, old_name, new_parent, new_name, flags=0):
        self.check_name(new_name)
        require(new_name not in (".git", ".goi"), errno.EPERM, "reserved GOI name")
        require(flags in (0, 1), errno.EINVAL, "unsupported rename flags")
        source, target_dir = self.lookup(old_parent, old_name), self.node(new_parent)
        self.load_directory(target_dir)
        require(source["kind"] in ("file", "dir") and source["writable"]
                and target_dir["writable"] and target_dir["kind"] in ("branch", "dir"),
                errno.EACCES, "rename is not allowed")
        require(source["branch"] == target_dir["branch"], errno.EXDEV, "cross-branch move is not atomic")
        if old_parent == new_parent and old_name == new_name:
            return source
        ancestor = target_dir
        while ancestor["kind"] != "branch":
            require(ancestor["id"] != source["id"], errno.EINVAL, "directory cannot contain itself")
            ancestor = self.node(ancestor["parent"])
        target = self.child(new_parent, new_name)
        if target:
            require(not flags, errno.EEXIST, "destination exists")
            require(stat.S_ISDIR(target["mode"]) == stat.S_ISDIR(source["mode"]),
                    errno.EISDIR if stat.S_ISDIR(target["mode"]) else errno.ENOTDIR, "type mismatch")
            self.remove(new_parent, new_name, directory=stat.S_ISDIR(target["mode"]))
        # Materialize directory metadata before moving, never the file contents.
        if source["kind"] == "dir":
            self.materialize(source)
        baseline = {}
        if target and not source["base_path"] and target["base_path"]:
            # An editor's unpublished temporary file replaces the original's baseline.
            baseline = {key: target[key] for key in ("base_path", "remote_sha", "base_mode")}
            self.update(target["id"], dirty=0)
        source = self.update(source["id"], parent=new_parent, name=new_name, dirty=1,
                             revision=source["revision"] + 1, **baseline)
        for child in self.descendants(source["id"]):
            if child["kind"] in ("dir", "file"):
                self.update(child["id"], dirty=1)
        return source

    def materialize(self, node):
        self.load_directory(node)
        for child in self.children(node["id"]):
            if child["kind"] == "dir":
                self.materialize(child)

    def descendants(self, number):
        return [dict(row) for row in self.db.execute("""
          WITH RECURSIVE subtree(id) AS (SELECT id FROM nodes WHERE parent=?
            UNION ALL SELECT n.id FROM nodes n JOIN subtree s ON n.parent=s.id)
          SELECT n.* FROM nodes n JOIN subtree s ON n.id=s.id
        """, (number,))]

    def mirror(self, node, content):
        """A separate editable file is created only when GitHub reports a conflict."""
        goi = self.child(node["branch"], ".goi")
        commit = self.child(goi["id"], "commit")
        parent = self.child(commit["id"], "changes")
        attrs = {"branch": node["branch"], "repo": node["repo"], "account": node["account"], "loaded": 1}
        parts = self.path(node).split("/")
        for part in parts[:-1]:
            parent = self.child(parent["id"], part) or self.new_node(
                parent["id"], part, "control_dir", stat.S_IFDIR | 0o755, **attrs)
        mirror = self.child(parent["id"], parts[-1]) or self.new_node(
            parent["id"], parts[-1], "change", stat.S_IFREG | 0o600, source_id=node["id"], **attrs)
        return self.update(mirror["id"], data=content, size=len(content), writable=1,
                           mode=stat.S_IFREG | 0o600, revision=mirror["revision"] + 1)

    def dirty_nodes(self, branch):
        nodes = [dict(row) for row in self.db.execute(
            "SELECT * FROM nodes WHERE branch=? AND kind IN ('file','dir') AND dirty=1 ORDER BY id", (branch,))]
        replacements = {self.path(node): node for node in nodes
                        if node["kind"] == "file" and not node["deleted"] and not node["base_path"]}
        for node in nodes:
            if not node["deleted"]:
                continue
            if not node["base_path"]:
                # Created and removed locally: there is nothing to publish.
                self.update(node["id"], dirty=0)
                node["dirty"] = 0
            elif node["kind"] == "file" and node["base_path"] in replacements:
                # Vim moves the original to a backup, writes a new file, then
                # removes the backup. Keep the original Git baseline on its replacement.
                replacement = replacements.pop(node["base_path"])
                baseline = {key: node[key] for key in ("base_path", "remote_sha", "base_mode")}
                self.update(replacement["id"], **baseline)
                replacement.update(baseline)
                self.update(node["id"], dirty=0)
                node["dirty"] = 0
        return [node for node in nodes if node["dirty"]]

    def setattr(self, number, mode=None, size=None):
        node = self.node(number)
        require(node["kind"] in ("file", "dir") and node["writable"], errno.EACCES, "read-only object")
        values = {"revision": node["revision"] + 1, "mtime_ns": time.time_ns(), "dirty": 1}
        if mode is not None:
            values["mode"] = stat.S_IFMT(node["mode"]) | (mode & 0o777)
        if size is not None:
            require(node["kind"] == "file", errno.EISDIR, "cannot resize a directory")
            require(0 <= size <= MAX_FILE_SIZE, errno.EFBIG, "file exceeds configured size limit")
            data = self.read(number, 0, MAX_FILE_SIZE)
            values.update(data=data[:size].ljust(size, b"\0"), size=size)
        return self.update(number, **values)

    def refresh(self, branch_id):
        from .workspace import refresh
        return refresh(self, branch_id)

    def write(self, number, data, mode):
        """One fsync request replaces one complete file inside the HTTP transaction."""
        node = self.node(number, deleted=True)
        require(node["writable"] and node["kind"] in ("file", "change", "message"),
                errno.EACCES, "file is read-only")
        require(len(data) <= MAX_FILE_SIZE, errno.EFBIG, "file exceeds configured size limit")
        return self.update(number, data=data, size=len(data), mode=stat.S_IFREG | (mode & 0o777),
                           revision=node["revision"] + 1, mtime_ns=time.time_ns(),
                           dirty=node["dirty"] if node["deleted"] else int(node["kind"] == "file"))
