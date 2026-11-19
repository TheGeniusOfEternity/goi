"""Small, explicit ignore syntax; only unpublished additions may be ignored."""
import fnmatch


def rules(store, branch):
    result = ["*.swp", "*.swx", "*~", "4913", "__pycache__/", "*.pyc"]
    node = store.child(branch, ".goiignore")
    if node and node["kind"] == "file":
        result += store.read(node["id"], 0, 65536).decode("utf-8").splitlines()
    return result


def ignored(store, node, patterns):
    if node["base_path"] or node["remote_sha"] or node["name"] == ".goiignore":
        return False
    path = store.path(node)
    parts = path.split("/")
    matched = False
    for line in patterns:
        pattern = line.strip()
        if not pattern or pattern.startswith("#"):
            continue
        negate = pattern.startswith("!")
        pattern = pattern[1:] if negate else pattern
        directory = pattern.endswith("/")
        anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
        pattern = pattern.strip("/")
        if not pattern:
            continue
        for end in range(1, len(parts) + 1):
            if directory and end == len(parts) and node["kind"] != "dir":
                continue
            candidate = "/".join(parts[:end]) if anchored else parts[end - 1]
            # Slash-separated patterns match components; * does not cross /.
            left, right = candidate.split("/"), pattern.split("/")
            if len(left) == len(right) and all(fnmatch.fnmatchcase(a, b) for a, b in zip(left, right)):
                matched = not negate
                break
    return matched
