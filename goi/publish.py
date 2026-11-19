"""GitHub commands. Filesystem operations and SQLite rows live in storage.py."""

from datetime import datetime, timezone
import difflib
import errno
import stat

from .github import blob_sha
from .merge import merge
from . import recovery, ignore
from .protocol import Error, MAX_FILE_SIZE, require


def status(store, branch, show_ignored=False):
    patterns = ignore.rules(store, branch)
    conflicts = {row[0] for row in store.db.execute("SELECT node FROM conflicts WHERE branch=?", (branch,))}
    result = []
    for node in store.dirty_nodes(branch):
        is_ignored = ignore.ignored(store, node, patterns)
        if is_ignored and not show_ignored:
            continue
        path = store.path(node)
        state = "conflict" if node["id"] in conflicts else (
            "deleted" if node["deleted"] else "added" if not node["base_path"] else
            "renamed" if path != node["base_path"] else "modified")
        result.append({"id": node["id"], "path": path + ("/" if node["kind"] == "dir" else ""), "old_path": node["base_path"], "state": "ignored" if is_ignored else state})
    return result


def diff(store, branch):
    result = []
    repo = store.repo(store.branch(branch)["repo"])
    api = store.github(repo["account"])
    for item in status(store, branch):
        node = store.node(item["id"], deleted=True)
        if node["kind"] == "dir":
            continue
        before = api.blob(repo["owner"], repo["name"], node["remote_sha"]) if node["remote_sha"] else b""
        after = b"" if node["deleted"] else store.read(node["id"], 0, MAX_FILE_SIZE)
        try:
            if b"\0" in before + after:
                raise UnicodeError()
            result.extend(difflib.unified_diff(before.decode().splitlines(True), after.decode().splitlines(True),
                                              fromfile="a/" + (node["base_path"] or item["path"]),
                                              tofile="b/" + item["path"]))
        except UnicodeError:
            result.append(f"Binary file changed: {item['path']}\n")
    return "".join(result)


def make_conflict(store, node, theirs, remote_sha, remote_head, remote_mode):
    ours = store.read(node["id"], 0, MAX_FILE_SIZE) if not node["deleted"] else b""
    try:
        ours.decode("utf-8")
        theirs.decode("utf-8")
        text = b"\0" not in ours + theirs and not node["deleted"] and remote_sha is not None
    except UnicodeError:
        text = False
    if text:
        content = (b"<<<<<<< local\n" + ours + (b"" if ours.endswith(b"\n") else b"\n") +
                   b"=======\n" + theirs + (b"" if theirs.endswith(b"\n") else b"\n") + b">>>>>>> remote\n")
    else:
        content = b"Choose a version with goi resolve --ours or --theirs.\n"
    mirror = store.mirror(node, content)
    store.db.execute("INSERT OR REPLACE INTO conflicts VALUES (?,?,?,?,?,?,?,?)",
                     (node["id"], node["branch"], store.path(node), theirs, remote_sha, remote_head,
                      mirror["id"], remote_mode))


def resolve(store, number, choice=None):
    node = store.node(number, deleted=True)
    row = store.db.execute("SELECT * FROM conflicts WHERE node=?", (number,)).fetchone()
    require(row is not None, errno.ENOENT, "file has no unresolved conflict")
    if choice == "ours":
        data = node["data"] or b""
    elif choice == "theirs":
        data = row["theirs"]
    else:
        data = store.node(row["mirror"])["data"] or b""
        require(not any(marker in data for marker in (b"<<<<<<<", b"=======", b">>>>>>>")),
                errno.EAGAIN, "remove conflict markers before confirming resolution")
        require(row["remote_sha"] is not None and not node["deleted"], errno.EINVAL,
                "deletion conflicts need --ours or --theirs")
    deleted = node["deleted"] if choice != "theirs" else int(row["remote_sha"] is None)
    mode = (stat.S_IFREG | (0o755 if row["remote_mode"] == "100755" else 0o644)) if choice == "theirs" else node["mode"]
    store.update(number, data=data, size=len(data), deleted=deleted, dirty=1, mode=mode,
                 remote_sha=row["remote_sha"], base_mode=row["remote_mode"], revision=node["revision"] + 1)
    store.db.execute("DELETE FROM conflicts WHERE node=?", (number,))
    store.update(row["mirror"], writable=0, mode=stat.S_IFREG | 0o444)


def selected(store, branch, paths, force=False):
    patterns = ignore.rules(store, branch)
    nodes = [n for n in store.dirty_nodes(branch) if force or not ignore.ignored(store, n, patterns)]
    if paths:
        def matches(node, prefix):
            return any(path is not None and (prefix in ("", ".") or path == prefix or path.startswith(prefix + "/"))
                       for path in (store.path(node), node["base_path"]))
        nodes = [node for node in nodes if any(matches(node, prefix) for prefix in paths)]
        require(all(any(matches(node, prefix) for node in nodes) for prefix in paths), errno.ENOENT,
                "selected path has no publishable changes; ignored additions require --force")
    destinations = {store.path(node) for node in nodes if not node["deleted"]}
    for node in store.dirty_nodes(branch):
        if node["deleted"] and node["base_path"] in destinations and node not in nodes:
            nodes.append(node)
    return [node for node in nodes if node["kind"] == "file"]


def commit(store, branch_id, paths, message, force=False):
    """Read saved files, build one Git tree/commit, then fast-forward the branch."""
    require(isinstance(message, str) and message.strip() and len(message.encode()) <= 8192,
            message="commit message must be nonempty and at most 8192 bytes")
    recovered = recovery.recover(store, branch_id)
    require(not store.db.execute("SELECT 1 FROM conflicts WHERE branch=?", (branch_id,)).fetchone(),
            errno.EAGAIN, "resolve pending conflicts first")
    require(store.node(branch_id)["writable"], errno.EACCES, "branch is read-only")
    branch = store.branch(branch_id)
    repo = store.repo(branch["repo"])
    api = store.github(repo["account"])
    latest_repo = api.repository(repo["owner"], repo["name"])
    require(latest_repo.get("permissions", {}).get("push") and not latest_repo.get("archived", False),
            errno.EACCES, "GitHub does not grant write access")
    nodes = selected(store, branch_id, paths, force)
    if not nodes:
        return {"sha": recovered.get("sha"), "message": "No changes"}
    head = api.head(repo["owner"], repo["name"], branch["name"])
    current_commit = api.commit(repo["owner"], repo["name"], head)
    current = api.full_tree(repo["owner"], repo["name"], current_commit["tree"]["sha"])
    conflicts = []
    for node in nodes:
        path = store.path(node)
        old = node["base_path"] or path
        remote = current.get(old, {})
        ours = b"" if node["deleted"] else store.read(node["id"], 0, MAX_FILE_SIZE)
        same = (not node["deleted"] and remote.get("sha") == blob_sha(ours)) or (node["deleted"] and not remote)
        collision = path != old and path in current and not any(n["deleted"] and n["base_path"] == path for n in nodes)
        mode = "100755" if node["mode"] & 0o111 else "100644"
        mode_conflict = not node["deleted"] and remote.get("mode") not in (None, node["base_mode"], mode)
        if (remote.get("sha") != node["remote_sha"] and not same) or collision or mode_conflict:
            theirs = current.get(path if collision else old, {})
            data = api.blob(repo["owner"], repo["name"], theirs["sha"]) if theirs else b""
            if not collision and not mode_conflict and not node["deleted"] and node["remote_sha"] and theirs:
                base = api.blob(repo["owner"], repo["name"], node["remote_sha"])
                merged = merge(base, ours, data)
                if merged is not None:
                    updated = store.update(node["id"], data=merged, size=len(merged),
                                           remote_sha=theirs["sha"], base_mode=theirs["mode"],
                                           revision=node["revision"] + 1)
                    node.update(updated)
                    continue
            make_conflict(store, node, data, theirs.get("sha"), head, theirs.get("mode"))
            conflicts.append(path)
    if conflicts:
        store.db.commit()  # Keep the conflict copies even though commit reports EAGAIN.
        raise Error(errno.EAGAIN, "conflicts: " + ", ".join(conflicts))

    entries, saved = {}, []
    for node in sorted(nodes, key=lambda item: not item["deleted"]):
        path = store.path(node)
        mode = "100755" if node["mode"] & 0o111 else "100644"
        sha = None
        if node["base_path"] and (node["deleted"] or path != node["base_path"]) and node["base_path"] in current:
            entries[node["base_path"]] = {"path": node["base_path"], "mode": mode, "type": "blob", "sha": None}
        if not node["deleted"]:
            data = store.read(node["id"], 0, MAX_FILE_SIZE)
            sha = blob_sha(data)
            if current.get(path, {}).get("sha") != sha or current.get(path, {}).get("mode") != mode:
                require(api.create_blob(repo["owner"], repo["name"], data) == sha, errno.EIO, "unexpected blob id")
                entries[path] = {"path": path, "mode": mode, "type": "blob", "sha": sha}
        saved.append((node, path, sha, mode))
    if entries:
        profile = api.profile()
        author = {"name": profile.get("name") or profile["login"],
                  "email": profile.get("email") or f"{profile['id']}+{profile['login']}@users.noreply.github.com",
                  "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        if len(current) == 1 and len(entries) == 1 and next(iter(entries)) in current and next(iter(entries.values()))["sha"] is None:
            # Git Trees rejects deleting its last entry. Contents supports this
            # operation and creates the deletion commit itself.
            path = next(iter(entries))
            recovery.prepare(store, branch_id, head, None, saved)
            head = api.delete_file(repo["owner"], repo["name"], branch["name"], path,
                                   current[path]["sha"], message, author)
        else:
            tree = api.create_tree(repo["owner"], repo["name"], current_commit["tree"]["sha"], list(entries.values()))
            base_head = head
            head = api.create_commit(repo["owner"], repo["name"], head, tree, message, author)
            recovery.prepare(store, branch_id, base_head, head, saved)
            api.update_head(repo["owner"], repo["name"], branch["name"], head)
    store.db.execute("DELETE FROM publications WHERE branch=?", (branch_id,))
    for node, path, sha, mode in saved:
        store.update(node["id"], base_path=path, remote_sha=sha, base_mode=mode, dirty=0)
        store.db.execute("UPDATE nodes SET deleted=1 WHERE source_id=?", (node["id"],))
    store.db.execute("UPDATE branches SET head=? WHERE id=?", (head, branch_id))
    return {"sha": head}
