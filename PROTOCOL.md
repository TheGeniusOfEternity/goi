# GOI HTTP protocol

The kernel uses source/http.c and vtfs_http_call. The adapted template retains
fill_request, receive_all, parse_http_response, and encode. The call accepts mount
configuration, an encoded query, and an optional POST body, and returns the
received byte count for completeness checks.

Transport: IPv4, HTTP/1.1, one connection per request. Authentication:
Authorization: Bearer <server-token>. The service also accepts a token query
parameter for compatibility.

## Binary API

Every /api/<method> response begins with a little-endian int64 status: zero or
a negative Linux errno, followed by payload. Content-Length is required.
The client rejects truncated responses, excessive lengths, and Transfer-Encoding.
The kernel timeout is 10 seconds.

Metadata uses <QQQIIQ (40 bytes): ID, size, mtime in nanoseconds, mode, flags,
and revision. Flag bit 0 means read-only; bit 1 means branch root.
Revisions invalidate clean caches after changes; they are not a general write lock.

| Method | Parameters / body | Payload after status |
|---|---|---|
| hello | — | Root metadata |
| stat | id | Metadata |
| lookup | parent, name | Metadata |
| list | id, offset | u32 count; entries: metadata, u16 name length, UTF-8 name; up to 100 |
| read | id, offset, count | Bytes, at most 32768 per request |
| write | id, mode; POST entire file | Metadata after SQLite commit |
| create | parent, name, mode | Metadata |
| unlink / rmdir | parent, name | Empty |
| rename | old_parent, old_name, new_parent, new_name, flags | Metadata |
| setattr | id, optional mode and size | Metadata |

Names are percent-encoded, up to 255 UTF-8 bytes, excluding slash, NUL, dot,
and dot-dot. Files and write bodies are limited to 64 MiB. The server reads the
entire body before changing a row and committing SQLite. Partial requests do not
change the file. Repeated writes replace the complete file; there are no chunked
upload sessions.

## Control API

POST /control accepts a JSON object with operation and its arguments.
Responses are {"ok":true,"result":...} or
{"ok":false,"errno":N,"error":"..."}.

Operations include auth_start, auth_poll, activate(path), status(branch),
diff(branch), commit(branch,paths,message), resolve(branch,path,choice?),
refresh(branch), restore(branch,paths), and recover(branch,forget?).

Fsync performs write, not commit. GitHub publication records intent in SQLite
before changing the ref. Recovery confirms a published SHA or retries that ref
update while preserving subsequent edits. Forget removes intent without
reverting local files. Restore returns selected changes to their baseline.
Refresh returns head, conflicts, and preserved_directories. Ref updates are not
forced; GitHub rejects a divergent update.

Accounts returns client_id and login/authenticated records. Logout removes the
token for a login while retaining working data. Status accepts ignored; commit
accepts force to include ignored new files.

## Kernel control

Ioctl 0x4701 synchronizes files in the mount. Ioctl 0x4702, issued on a branch
directory, saves changes and releases clean, closed files of other branches.
Inodes inherit branch IDs from their parent. Open files retain their cache;
mmap also retains an open-file reference.

## Server cache

The cache control operation returns cached_bytes, removed_bytes, and limit_bytes.
With clear: true, it removes reproducible clean blobs across the service.
It does not change metadata/revisions or require client RAM synchronization.
The server applies its LRU limit after requests and at startup. New/modified files,
conflicts, and unfinished publications are excluded. Default: 256 MiB;
configure with --cache-mib.

## Client journal

Mount options: cache_mib (1..4096, default 256) and journal_port (local loopback
port; zero disables journaling). CLI enables journaling by default.

The journal uses the same binary status and its session token:

- POST /api/record?id=N&revision=R&mode=M with a full-file body.
- POST /api/ack?id=N with an empty body.

Responses contain status only, after SQLite commit. Record retains the first
revision and latest snapshot; ack removes the record.

The main /api/write accepts expected_revision. A different revision is permitted
only if contents and mode already match, allowing idempotent recovery.
Otherwise it returns ESTALE and leaves the journal intact.

Local control operations: info, replay, export (one ID, base64), and discard
(explicit ID). CLI rejects replay/discard on mounted filesystems.
The daemon is bound to its backend address and session token.
