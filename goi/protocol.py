"""Version 1 of the bounded HTTP/binary protocol shared with the kernel."""

import errno
import struct

STATUS = struct.Struct("<q")
META = struct.Struct("<QQQIIQ")  # id, size, mtime_ns, mode, flags, revision
COUNT = struct.Struct("<I")
NAME_LEN = struct.Struct("<H")
READONLY = 1
BRANCH = 2
CHUNK_SIZE = 32768
MAX_FILE_SIZE = 64 * 1024 * 1024
MAX_REQUEST = MAX_FILE_SIZE


class Error(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def require(condition, code=errno.EINVAL, message="invalid request"):
    if not condition:
        raise Error(code, message)


def metadata(node):
    return META.pack(node["id"], node["size"], node["mtime_ns"], node["mode"],
                     node.get("flags", 0) | (0 if node["writable"] else READONLY)
                     | (BRANCH if node["kind"] == "branch" else 0), node["revision"])
