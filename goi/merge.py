"""Conservative line-based three-way merge, without external executables."""

import difflib


def merge(base, ours, theirs):
    """Return merged bytes, or None when edits overlap or content is binary."""
    if ours == theirs or theirs == base:
        return ours
    if ours == base:
        return theirs
    try:
        if b"\0" in base + ours + theirs:
            return None
        original, local, remote = [data.decode("utf-8").splitlines(True)
                                   for data in (base, ours, theirs)]
    except UnicodeError:
        return None

    def edits(lines):
        return [(a, b, lines[c:d]) for tag, a, b, c, d in
                difflib.SequenceMatcher(None, original, lines, autojunk=False).get_opcodes()
                if tag != "equal"]

    left, right = edits(local), edits(remote)
    combined = list(left)
    for change in right:
        if change in left:
            continue
        a, b, _ = change
        for c, d, _ in left:
            # Insertions at an edited boundary are deliberately conflicts:
            # there is no unambiguous ordering for the new lines.
            if (max(a, c) < min(b, d) or
                    a == b and c <= a <= d or c == d and a <= c <= b):
                return None
        combined.append(change)
    result = list(original)
    for a, b, replacement in sorted(combined, key=lambda item: (item[0], item[1]), reverse=True):
        result[a:b] = replacement
    return "".join(result).encode("utf-8")
