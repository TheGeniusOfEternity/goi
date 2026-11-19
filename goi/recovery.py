"""Durable intent before publishing a Git ref; retries preserve subsequent edits."""

import errno
import json

from .github import blob_sha
from .protocol import require


def prepare(store, branch, base, target, saved):
    payload = [{"id": node["id"], "path": path, "sha": sha, "mode": mode,
                "revision": node["revision"], "deleted": bool(node["deleted"])}
               for node, path, sha, mode in saved]
    store.db.execute("INSERT INTO publications VALUES (?,?,?,?,?)",
                     (branch, base, target, json.dumps(payload), "pending"))
    store.db.commit()


def finish(store, branch, head, payload):
    for item in payload:
        node = store.node(item["id"], deleted=True)
        unchanged = (store.path(node) == item["path"] and bool(node["deleted"]) == item["deleted"]
                     and (node["deleted"] or node["data"] is not None and blob_sha(node["data"]) == item["sha"]
                          and ("100755" if node["mode"] & 0o111 else "100644") == item["mode"]))
        store.update(node["id"], base_path=item["path"], remote_sha=item["sha"],
                     base_mode=item["mode"], dirty=int(not unchanged))
        if unchanged:
            store.db.execute("UPDATE nodes SET deleted=1 WHERE source_id=?", (node["id"],))
    store.db.execute("UPDATE branches SET head=? WHERE id=?", (head, branch))
    store.db.execute("DELETE FROM publications WHERE branch=?", (branch,))
    store.db.commit()


def recover(store, branch_id):
    row = store.db.execute("SELECT * FROM publications WHERE branch=?", (branch_id,)).fetchone()
    if row is None:
        return {"recovered": False}
    branch = store.branch(branch_id)
    repo = store.repo(branch["repo"])
    api = store.github(repo["account"])
    current = api.head(repo["owner"], repo["name"], branch["name"])
    payload = json.loads(row["payload"])
    target = row["target"]
    if target is None:
        # Contents DELETE creates its own commit. Only acknowledge a deletion
        # when the resulting tree is empty; never replay an uncertain DELETE.
        tree = api.commit(repo["owner"], repo["name"], current)["tree"]["sha"]
        require(current != row["base"] and not api.full_tree(repo["owner"], repo["name"], tree),
                errno.EAGAIN, "uncertain deletion; inspect GitHub before goi recover --forget")
    else:
        if current == row["base"]:
            permissions = api.repository(repo["owner"], repo["name"])
            require(permissions.get("permissions", {}).get("push") and not permissions.get("archived"),
                    errno.EACCES, "GitHub does not grant write access")
            api.update_head(repo["owner"], repo["name"], branch["name"], target)
            current = target
        cursor = current
        for _ in range(128):
            if cursor == target:
                break
            if cursor == row["base"]:
                # A competing fast-forward won before our ref update. Retain
                # every local edit and let the next commit rebuild on that head.
                store.db.execute("DELETE FROM publications WHERE branch=?", (branch_id,))
                store.db.commit()
                return {"recovered": False}
            parents = api.commit(repo["owner"], repo["name"], cursor).get("parents", [])
            if not parents:
                break
            parent = parents[0]
            cursor = parent["sha"] if isinstance(parent, dict) else parent
        require(cursor == target, errno.EAGAIN,
                "remote branch diverged from pending publication; inspect GitHub before goi recover --forget")
    finish(store, branch_id, current, payload)
    return {"recovered": True, "sha": current}
