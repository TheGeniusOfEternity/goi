"""Local workspace operations; callers hold the Store lock and transaction."""

import errno
import stat

from .protocol import MAX_FILE_SIZE, require


def restore(store, branch_id, paths):
    """Discard selected local changes back to their recorded baseline."""
    require(paths and all(path and path != "." for path in paths),
            message="restore requires explicit paths")
    nodes = [node for node in store.dirty_nodes(branch_id)
             if any(value == path or value.startswith(path + "/")
                    for path in paths for value in (store.path(node), node["base_path"] or ""))]
    require(nodes, errno.ENOENT, "selected paths have no local changes")
    branch = store.node(branch_id)
    repo = store.repo(branch["repo"])
    api = store.github(repo["account"])
    selected = {node["id"] for node in nodes}
    content = {node["id"]: api.blob(repo["owner"], repo["name"], node["remote_sha"])
               for node in nodes if node["kind"] == "file" and node["remote_sha"]}
    # Hide all sources first so rename swaps and editor replacement can be undone.
    for node in nodes:
        store.update(node["id"], deleted=1, dirty=0, revision=node["revision"] + 1)
    for node in sorted(nodes, key=lambda item: (item["base_path"] or "").count("/")):
        if not node["base_path"]:
            continue
        parent = branch
        parts = node["base_path"].split("/")
        for part in parts[:-1]:
            child = store.child(parent["id"], part)
            if child is None:
                child = store.new_node(parent["id"], part, "dir", stat.S_IFDIR | 0o755,
                                       branch=branch_id, repo=branch["repo"], account=branch["account"],
                                       loaded=1, writable=branch["writable"])
            require(child["kind"] == "dir", errno.EEXIST, "restore parent is occupied by a file")
            parent = child
        other = store.child(parent["id"], parts[-1])
        require(other is None or other["id"] in selected, errno.EEXIST,
                "restore destination has other local data")
        values = dict(parent=parent["id"], name=parts[-1], deleted=0, dirty=0)
        if node["kind"] == "file":
            data = content.get(node["id"], b"")
            values.update(data=data, size=len(data), loaded=1,
                          mode=stat.S_IFREG | (0o755 if node["base_mode"] == "100755" else 0o644))
        else:
            values.update(mode=stat.S_IFDIR | 0o755, loaded=1)
        store.update(node["id"], **values)
    for node in nodes:
        store.db.execute("DELETE FROM conflicts WHERE node=?", (node["id"],))
        store.db.execute("UPDATE nodes SET deleted=1 WHERE source_id=?", (node["id"],))
    return {"restored": len(nodes)}


def refresh(store, branch_id):
    """Update remote metadata while preserving local edits and object identities."""
    from .github import blob_sha
    from .merge import merge
    from .publish import make_conflict

    require(not store.db.execute("SELECT 1 FROM conflicts WHERE branch=?", (branch_id,)).fetchone(),
            errno.EBUSY, "resolve pending conflicts before refreshing")
    branch = store.node(branch_id)
    repo = store.repo(branch["repo"])
    api = store.github(repo["account"])
    head = api.head(repo["owner"], repo["name"], store.branch(branch_id)["name"])
    tree = api.commit(repo["owner"], repo["name"], head)["tree"]["sha"]
    remote = {path: entry for path, entry in api.full_tree(repo["owner"], repo["name"], tree).items()
              if entry["type"] == "blob" and entry["mode"] in ("100644", "100755")
              and not any(part in (".git", ".goi") for part in path.split("/"))}
    store.materialize(branch)
    dirty = store.dirty_nodes(branch_id)
    # A renamed/deleted local directory owns its whole subtree until restored or
    # published. Do not resurrect the old remote path underneath that operation.
    protected = [value for node in dirty if node["kind"] == "dir"
                 and (node["deleted"] or node["base_path"] != store.path(node))
                 for value in (node["base_path"], store.path(node)) if value]

    def protected_path(path):
        return any(path == prefix or path.startswith(prefix + "/") for prefix in protected)

    reserved, conflicts = set(), []
    for node in dirty:
        if node["kind"] != "file":
            continue
        path = store.path(node)
        old = node["base_path"] or path
        reserved.update((path, old))
        entry = remote.get(old)
        if protected_path(path):
            continue
        ours = b"" if node["deleted"] else store.read(node["id"], 0, MAX_FILE_SIZE)
        theirs = api.blob(repo["owner"], repo["name"], entry["sha"]) if entry else b""
        local_mode = "100755" if node["mode"] & 0o111 else "100644"
        collision = path != old and path in remote
        changed = (entry or {}).get("sha") != node["remote_sha"]
        remote_mode = (entry or {}).get("mode")
        mode_conflict = remote_mode not in (None, node["base_mode"], local_mode) and local_mode != node["base_mode"]
        merged = None
        if not node["deleted"] and entry and not collision and not mode_conflict:
            base = api.blob(repo["owner"], repo["name"], node["remote_sha"]) if node["remote_sha"] else b""
            merged = merge(base, ours, theirs)
        if collision or mode_conflict or changed and merged is None and not (node["deleted"] and entry is None):
            target = remote.get(path) if collision else entry
            data = api.blob(repo["owner"], repo["name"], target["sha"]) if target else b""
            make_conflict(store, node, data, (target or {}).get("sha"), head, (target or {}).get("mode"))
            conflicts.append(path)
        elif merged is not None:
            mode = remote_mode if local_mode == node["base_mode"] else local_mode
            store.update(node["id"], data=merged, size=len(merged), remote_sha=entry["sha"],
                         base_path=old, base_mode=remote_mode,
                         mode=stat.S_IFREG | (0o755 if mode == "100755" else 0o644),
                         dirty=int(path != old or blob_sha(merged) != entry["sha"] or mode != remote_mode),
                         revision=node["revision"] + 1)
        elif node["deleted"] and entry is None:
            store.update(node["id"], dirty=0)

    attrs = dict(branch=branch_id, repo=branch["repo"], account=branch["account"],
                 writable=branch["writable"], loaded=1)

    def parent_for(path):
        parent = branch
        prefix = []
        for part in path.split("/")[:-1]:
            prefix.append(part)
            child = store.child(parent["id"], part)
            require(child is None or child["kind"] == "dir", errno.EBUSY,
                    "remote directory collides with a local file; restore or commit first")
            parent = child or store.new_node(parent["id"], part, "dir", stat.S_IFDIR | 0o755,
                                             base_path="/".join(prefix), **attrs)
        return parent

    for node in store.descendants(branch_id):
        if node["kind"] != "file" or node["deleted"]:
            continue
        path = store.path(node)
        if path in reserved or protected_path(path):
            continue
        entry = remote.get(path)
        if entry is None:
            store.update(node["id"], deleted=1, revision=node["revision"] + 1)
        else:
            mode = stat.S_IFREG | (0o755 if entry["mode"] == "100755" else 0o644)
            if not node["writable"]:
                mode &= ~0o222
            store.update(node["id"], remote_sha=entry["sha"], base_mode=entry["mode"],
                         data=node["data"] if entry["sha"] == node["remote_sha"] else None,
                         size=entry.get("size", 0), mode=mode, revision=node["revision"] + 1)
    for path, entry in remote.items():
        if path in reserved or protected_path(path):
            continue
        parent = parent_for(path)
        child = store.child(parent["id"], path.split("/")[-1])
        require(child is None or child["kind"] == "file", errno.EBUSY,
                "remote file collides with a local directory; restore or commit first")
        if child is None:
            mode = stat.S_IFREG | (0o755 if entry["mode"] == "100755" else 0o644)
            if not branch["writable"]:
                mode &= ~0o222
            store.new_node(parent["id"], path.split("/")[-1], "file", mode, base_path=path,
                           remote_sha=entry["sha"], base_mode=entry["mode"],
                           size=entry.get("size", 0), **attrs)
    remote_dirs = {path.rsplit("/", depth)[0] for path in remote for depth in range(1, path.count("/") + 1)}
    directories = [n for n in store.descendants(branch_id) if n["kind"] == "dir" and not n["deleted"]]
    for node in sorted(directories, key=lambda n: store.path(n).count("/"), reverse=True):
        path = store.path(node)
        if not node["dirty"] and not protected_path(path) and path not in remote_dirs and not store.children(node["id"]):
            store.update(node["id"], deleted=1)
    store.update(branch_id, tree_sha=tree, loaded=1)
    store.db.execute("UPDATE branches SET head=?,tree_sha=? WHERE id=?", (head, tree, branch_id))
    return {"head": head, "conflicts": conflicts, "preserved_directories": sorted(set(protected))}
