"""HTTP storage server. Run: python3 -m goi.server --data ./state."""

import argparse
import errno
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import parse_qs, urlsplit

from .protocol import CHUNK_SIZE, COUNT, Error, MAX_REQUEST, NAME_LEN, STATUS, metadata, require
from .storage import Store
from . import publish, workspace, recovery


class Server(ThreadingHTTPServer):
    # Threads receive request bodies and send replies independently. Only the
    # storage transaction is serialized: one SQLite connection and one writer.
    # GitHub calls inside that transaction may delay other storage operations;
    # releasing the lock there would allow edits against an outdated baseline.
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store, token):
        self.store, self.token = store, token
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        # Query strings contain capability tokens in the kernel compatibility API.
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def send(self, payload, content_type="application/octet-stream", status=200):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def handle_request(self):
        control = self.path.split("?", 1)[0] == "/control"
        try:
            require(len(self.path) <= 16384, errno.E2BIG, "request URL too long")
            url = urlsplit(self.path)
            query = parse_qs(url.query, keep_blank_values=True, strict_parsing=False, errors="strict")
            args = {key: value[-1] for key, value in query.items()}
            authorization = self.headers.get("Authorization", "")
            token = authorization[7:] if authorization.startswith("Bearer ") else args.pop("token", "")
            require(bool(token) and hmac.compare_digest(token, self.server.token), errno.EACCES, "invalid GOI session token")
            length = int(self.headers.get("Content-Length", "0"))
            require(0 <= length <= MAX_REQUEST, errno.E2BIG, "request body too large")
            body = self.rfile.read(length)
            require(len(body) == length, errno.EPROTO, "incomplete request body")
            with self.server.store.lock:
                try:
                    if control:
                        require(self.command == "POST", message="control requests require POST")
                        result = self.control(json.loads(body))
                    else:
                        require(url.path.startswith("/api/"), errno.ENOENT, "unknown endpoint")
                        result = self.api(url.path[5:], args, body)
                    self.server.store.trim_cache()
                    self.server.store.db.commit()
                except Exception:
                    self.server.store.db.rollback()
                    raise
            if control:
                self.send(json.dumps({"ok": True, "result": result}).encode(), "application/json")
            else:
                self.send(STATUS.pack(0) + result)
        except (Error, ValueError, TypeError, KeyError) as exc:
            code = exc.code if isinstance(exc, Error) else errno.EINVAL
            message = str(exc) if isinstance(exc, Error) else "invalid request"
            if control:
                self.send(json.dumps({"ok": False, "errno": code, "error": message}).encode(), "application/json")
            else:
                self.send(STATUS.pack(-code) + message.encode()[:4096])
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            self.close_connection = True
        except Exception as exc:
            # Keep implementation exceptions out of client responses and secret-bearing logs.
            print(f"GOI internal error: {type(exc).__name__}", flush=True)
            if control:
                self.send(b'{"ok":false,"errno":5,"error":"internal server error"}', "application/json")
            else:
                self.send(STATUS.pack(-errno.EIO))

    def api(self, method, args, body):
        store = self.server.store

        def integer(key, default=None):
            value = args.get(key, default)
            require(value is not None, message=f"missing {key}")
            number = int(value)
            require(0 <= number < 2**63, message=f"invalid {key}")
            return number

        if method in ("hello", "stat"):
            return metadata(store.node(integer("id", 1)))
        if method == "lookup":
            return metadata(store.lookup(integer("parent"), args["name"]))
        if method == "list":
            nodes = store.list(integer("id"), integer("offset", 0), 100)
            result = bytearray(COUNT.pack(len(nodes)))
            for node in nodes:
                name = node["name"].encode()
                result.extend(metadata(node) + NAME_LEN.pack(len(name)) + name)
            return bytes(result)
        if method == "read":
            count = integer("count")
            require(count <= CHUNK_SIZE, errno.E2BIG, "read exceeds chunk size")
            return store.read(integer("id"), integer("offset"), count)
        if method == "create":
            return metadata(store.create(integer("parent"), args["name"], integer("mode")))
        if method in ("unlink", "rmdir"):
            store.remove(integer("parent"), args["name"], directory=method == "rmdir")
            return b""
        if method == "rename":
            return metadata(store.rename(integer("old_parent"), args["old_name"], integer("new_parent"),
                                          args["new_name"], integer("flags", 0)))
        if method == "setattr":
            return metadata(store.setattr(integer("id"), integer("mode") if "mode" in args else None,
                                           integer("size") if "size" in args else None))
        if method == "write":
            require(self.command == "POST", message="file writes require POST")
            if "expected_revision" in args:
                node = store.node(integer("id"))
                same = (store.read(node["id"], 0, MAX_REQUEST) == body
                        and node["mode"] & 0o777 == integer("mode"))
                require(node["revision"] == integer("expected_revision") or same, errno.ESTALE,
                        "remote file changed since journal snapshot")
                if same:
                    return metadata(node)
            return metadata(store.write(integer("id"), body, integer("mode")))
        raise Error(errno.ENOSYS, "unknown filesystem operation")

    def control(self, request):
        store = self.server.store
        operation = request["operation"]
        if operation == "accounts":
            return {"client_id": store.client_id, "accounts": [
                {"login": row["login"], "authenticated": bool(row["token"])}
                for row in store.db.execute("SELECT login,token FROM accounts ORDER BY login")]}
        if operation == "logout":
            cursor = store.db.execute("UPDATE accounts SET token=NULL WHERE login=?", (request["login"],))
            require(cursor.rowcount, errno.ENOENT, "account not found")
            return {"logged_out": request["login"]}
        if operation == "cache":
            return store.trim_cache(0 if request.get("clear") else None)
        if operation == "auth_start":
            require(bool(store.client_id), errno.EINVAL, "configure --client-id for your GitHub OAuth App (Device Flow enabled)")
            data = store.github_factory("").device_start(store.client_id)
            require("device_code" in data, errno.EACCES, data.get("error_description", "Device Flow rejected"))
            flow = secrets.token_hex(16)
            interval = int(data.get("interval", 5))
            store.db.execute("INSERT INTO devices VALUES (?,?,?,?,?)",
                              (flow, data["device_code"], time.time() + data["expires_in"], interval, time.time()))
            return {"flow": flow, "user_code": data["user_code"], "verification_uri": data["verification_uri"],
                    "interval": interval, "expires_in": data["expires_in"]}
        if operation == "auth_poll":
            row = store.db.execute("SELECT * FROM devices WHERE id=?", (request["flow"],)).fetchone()
            require(row is not None and time.time() < row["expires"], errno.ETIMEDOUT, "authorization expired")
            if time.time() < row["next_poll"]:
                return {"pending": True, "interval": row["interval"]}
            data = store.github_factory("").device_poll(store.client_id, row["code"])
            if data.get("error") in ("authorization_pending", "slow_down"):
                interval = row["interval"] + (5 if data["error"] == "slow_down" else 0)
                store.db.execute("UPDATE devices SET interval=?,next_poll=? WHERE id=?",
                                  (interval, time.time() + interval, request["flow"]))
                return {"pending": True, "interval": interval}
            require("access_token" in data, errno.EACCES, data.get("error_description", "authorization rejected"))
            account = store.add_account(data["access_token"])
            store.db.execute("DELETE FROM devices WHERE id=?", (request["flow"],))
            return {"login": account["name"]}
        if operation == "refresh_owners":
            return store.refresh_owners(request["path"])
        if operation == "activate":
            node = store.resolve_path(request["path"])
            store.activate(node["id"])
            return {"path": request["path"], "active": True}
        if operation in ("status", "diff", "commit", "refresh", "resolve", "restore", "recover"):
            branch = store.resolve_path(request["branch"])
            require(branch["kind"] == "branch", message="expected a branch directory")
            if operation == "status":
                return publish.status(store, branch["id"], request.get("ignored", False))
            if operation == "diff":
                return {"diff": publish.diff(store, branch["id"])}
            if operation == "commit":
                return publish.commit(store, branch["id"], request.get("paths", []), request["message"], request.get("force", False))
            if operation == "recover":
                if request.get("forget"):
                    store.db.execute("DELETE FROM publications WHERE branch=?", (branch["id"],))
                    return {"forgotten": True}
                return recovery.recover(store, branch["id"])
            require(not store.db.execute("SELECT 1 FROM publications WHERE branch=?", (branch["id"],)).fetchone()
                    or operation in ("status", "diff", "commit"), errno.EBUSY,
                    "recover pending publication before changing the workspace")
            if operation == "restore":
                return workspace.restore(store, branch["id"], request.get("paths", []))
            if operation == "refresh":
                return store.refresh(branch["id"])
            path = request["path"]
            conflicted = {row[0] for row in store.db.execute("SELECT node FROM conflicts WHERE branch=?", (branch["id"],))}
            candidates = [n for n in store.dirty_nodes(branch["id"])
                          if n["id"] in conflicted and (store.path(n) == path or n["base_path"] == path)]
            require(len(candidates) == 1, errno.ENOENT, "conflicted file not found")
            publish.resolve(store, candidates[0]["id"], request.get("choice"))
            return {"resolved": path}
        raise Error(errno.ENOSYS, "unknown control operation")


def session_token(directory):
    path = Path(directory) / "server-token"
    if not path.exists():
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w") as file:
                file.write(secrets.token_hex(32) + "\n")
                file.flush()
                os.fsync(file.fileno())
    token = path.read_text().strip()
    require(len(token) == 64 and all(c in "0123456789abcdef" for c in token), message="invalid server-token file")
    os.chmod(path, 0o600)
    return token


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default=str(Path.home() / ".local/share/goi"))
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--client-id", default=os.environ.get("GOI_GITHUB_CLIENT_ID", ""))
    parser.add_argument("--cache-mib", type=int, default=256, help="clean blob cache limit; 0 disables caching")
    args = parser.parse_args()
    if args.cache_mib < 0:
        parser.error("--cache-mib must be nonnegative")
    store = Store(args.data, args.client_id, cache_bytes=args.cache_mib * 1024 * 1024)
    with store.db:
        store.trim_cache()
    for row in list(store.db.execute("SELECT branch FROM publications")):
        try:
            recovery.recover(store, row["branch"])
        except Exception as exc:
            store.db.rollback()
            print(f"Pending publication in branch {row['branch']}: {type(exc).__name__}; use goi recover", flush=True)
    server = Server((args.listen, args.port), store, session_token(args.data))
    print(f"GOI server on {args.listen}:{server.server_port}; session token file: {args.data}/server-token", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
