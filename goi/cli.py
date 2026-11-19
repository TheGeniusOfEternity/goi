"""GOI CLI: Device Flow, mounts, fsync, status and GitHub publication."""

import argparse
import errno
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen
import webbrowser

from .protocol import Error, require
from . import service

SYNC_ALL = 0x4701
SWITCH = 0x4702


def configuration_path():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "goi/mounts.json"


def configurations():
    path = configuration_path()
    return json.loads(path.read_text()) if path.exists() else {}


def save_configuration(mountpoint, server, token_file, autosync=None, cache_mib=None, journal=None, managed_local=None):
    path = configuration_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    records = configurations()
    record = records.setdefault(str(mountpoint), {})
    record.update(server=server.rstrip("/"), token_file=str(Path(token_file).resolve()))
    if autosync is not None:
        record["autosync"] = autosync
    if cache_mib is not None:
        record["cache_mib"] = cache_mib
    if journal is not None:
        record["journal"] = journal
    if managed_local is not None:
        record["managed_local"] = managed_local
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        json.dump(records, file, indent=2)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


class Client:
    def __init__(self, mountpoint, config):
        self.mountpoint, self.server = Path(mountpoint), config["server"]
        path = Path(config["token_file"])
        require(not path.stat().st_mode & 0o077, errno.EACCES, "session token file must have mode 0600")
        self.token = path.read_text().strip()

    def call(self, operation, **values):
        body = json.dumps({"operation": operation, **values}).encode()
        request = Request(self.server + "/control", body,
                          {"Content-Type": "application/json", "Authorization": "Bearer " + self.token})
        try:
            with urlopen(request, timeout=120) as response:
                result = json.load(response)
        except (URLError, OSError, TimeoutError):
            raise Error(errno.EHOSTUNREACH, "GOI server is unreachable; local changes are retained") from None
        require(result.get("ok"), result.get("errno", errno.EIO), result.get("error", "GOI request failed"))
        return result["result"]

    def relative(self, path):
        return str(Path(path).resolve().relative_to(self.mountpoint))

    def sync_all(self):
        fd = os.open(self.mountpoint, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.ioctl(fd, SYNC_ALL)
        finally:
            os.close(fd)


def connection(path, required=True):
    resolved = Path(path).resolve()
    for mountpoint, config in sorted(configurations().items(), key=lambda item: -len(item[0])):
        if resolved.is_relative_to(mountpoint):
            return Client(mountpoint, config)
    if required:
        raise Error(errno.ENOENT, "mount this filesystem with goi mount <mountpoint> --server URL --token-file FILE")
    return None


def branch_context(client, path):
    relative = Path(path).resolve().relative_to(client.mountpoint)
    parts = relative.parts
    require(len(parts) >= 3,
            message="run this command inside an activated branch")
    end = 3
    branch = Path(*parts[:end])
    return branch.as_posix(), client.mountpoint / branch


def sync_file(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def switch_branch(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.ioctl(fd, SWITCH)
    finally:
        os.close(fd)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    item = commands.add_parser("service", help="manage the local per-user systemd service")
    item.add_argument("action", choices=("install", "start", "stop", "restart", "status", "disable"))
    item.add_argument("--client-id", help="OAuth App ID to persist when installing the service")
    item = commands.add_parser("mount")
    item.add_argument("mountpoint", type=Path)
    item.add_argument("--server", help="saved address or http://127.0.0.1:8080")
    item.add_argument("--token-file", type=Path, help="saved path or ~/.local/share/goi/server-token")
    item.add_argument("--autosync", type=int, help="saved interval or 1 second; 0 disables background sync")
    item.add_argument("--cache-mib", type=int, help="kernel file-cache limit; default 256 MiB")
    journal_mode = item.add_mutually_exclusive_group()
    journal_mode.add_argument("--journal", dest="journal", action="store_true", default=None)
    journal_mode.add_argument("--no-journal", dest="journal", action="store_false")
    item = commands.add_parser("journal")
    item.add_argument("action", choices=("status", "recover", "export", "discard"), nargs="?", default="status")
    item.add_argument("--mountpoint", type=Path, default=Path.cwd())
    item.add_argument("--output", type=Path)
    item.add_argument("--id", type=int)
    item = commands.add_parser("auth")
    item.add_argument("mountpoint", nargs="?", default="/github")
    item.add_argument("--no-browser", action="store_true")
    commands.add_parser("accounts")
    item = commands.add_parser("logout")
    item.add_argument("login")
    item = commands.add_parser("cache", help="inspect or clear the server clean-file cache")
    item.add_argument("--clear", action="store_true")
    item = commands.add_parser("activate")
    item.add_argument("path", nargs="?", default=Path("."), type=Path,
                      help="repository directory; defaults to the current directory")
    item = commands.add_parser("sync")
    item.add_argument("paths", nargs="*", type=Path)
    item = commands.add_parser("status")
    item.add_argument("--json", action="store_true")
    item.add_argument("--ignored", action="store_true")
    item = commands.add_parser("restore", help="discard local changes to explicit files or directories")
    item.add_argument("paths", nargs="+", type=Path)
    commands.add_parser("diff")
    commands.add_parser("refresh")
    item = commands.add_parser("recover", help="recover an interrupted publication")
    item.add_argument("--forget", action="store_true", help="forget publication intent, retaining local edits")
    item = commands.add_parser("switch", help="save changes, drop other branch caches and print the branch root")
    item.add_argument("branch", help="branch name in the current repository, or a path to a branch")
    item.add_argument("--from-cd", action="store_true", help=argparse.SUPPRESS)
    item = commands.add_parser("commit")
    item.add_argument("paths", nargs="*", type=Path)
    item.add_argument("-m", "--message", required=True)
    item.add_argument("--force", action="store_true", help="include ignored new files")
    item = commands.add_parser("resolve")
    item.add_argument("path", type=Path)
    choice = item.add_mutually_exclusive_group()
    choice.add_argument("--ours", action="store_true")
    choice.add_argument("--theirs", action="store_true")
    item = commands.add_parser("unmount")
    item.add_argument("mountpoint", type=Path)
    return result


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if Path(sys.argv[0]).name == "goida":
        argv.insert(0, "commit")
    args = parser().parse_args(argv)
    try:
        if args.command == "service":
            require(args.client_id is None or args.action == "install", message="--client-id is only valid with service install")
            if args.action == "install":
                print(service.install(args.client_id))
                print("Enabled for login; use goi service start (or restart after changing Client ID)")
            elif args.action == "start":
                service.ensure()
                print("Local GOI service is ready")
            elif args.action == "disable":
                print(service.systemctl("disable", "goi.service"))
            elif args.action == "status":
                result = subprocess.run(["systemctl", "--user", "status", "--no-pager", "goi.service"])
                return result.returncode
            else:
                print(service.systemctl(args.action, "goi.service"))
            return 0
        if args.command == "switch":
            target = Path(args.branch)
            if not args.from_cd and not target.is_absolute() and not args.branch.startswith(("./", "../")):
                client = connection(Path.cwd())
                _, current = branch_context(client, Path.cwd())
                target = current.parent / args.branch
                if not target.is_dir():
                    target = current.parent / quote(args.branch, safe="")
            require(target.is_dir(), errno.ENOENT, "branch directory does not exist; activate the repository first")
            client = connection(target, required=not args.from_cd)
            if client is None:
                return 0
            try:
                _, directory = branch_context(client, target)
            except Error as exc:
                if args.from_cd and exc.code == errno.EINVAL:
                    return 0  # cd also visits the mount root and repository directories.
                raise
            switch_branch(directory)
            if not args.from_cd:
                print(directory)
            return 0
        if args.command == "mount":
            mountpoint = args.mountpoint.resolve()
            saved = configurations().get(str(mountpoint), {})
            server = args.server if args.server is not None else saved.get("server", "http://127.0.0.1:8080")
            token_file = args.token_file if args.token_file is not None else Path(
                saved.get("token_file", Path.home() / ".local/share/goi/server-token"))
            autosync = args.autosync if args.autosync is not None else saved.get("autosync", 1)
            require(0 <= autosync <= 3600, message="autosync must be 0..3600 seconds")
            managed_local = (args.server is None and args.token_file is None
                             and saved.get("managed_local", True)
                             and server.rstrip("/") == service.URL
                             and token_file.resolve() == (service.data_directory() / "server-token").resolve())
            require(not os.path.ismount(mountpoint), errno.EBUSY, "already mounted")
            if managed_local:
                service.ensure()
            client = Client(mountpoint, {"server": server, "token_file": str(token_file)})
            url = urlsplit(server)
            require(url.scheme == "http" and url.hostname and not url.username and not url.password,
                    message="kernel mount requires an HTTP IPv4 endpoint; use a trusted network or SSH tunnel")
            address = socket.gethostbyname(url.hostname)
            ipaddress.IPv4Address(address)
            require(client.token and all(c.isalnum() or c in "-_" for c in client.token), message="invalid session token")
            cache_mib = args.cache_mib if args.cache_mib is not None else saved.get("cache_mib", 256)
            journaling = args.journal if args.journal is not None else saved.get("journal", True)
            require(1 <= cache_mib <= 4096, message="cache-mib must be 1..4096")
            require(not os.path.ismount(mountpoint), errno.EBUSY, "already mounted")
            journal_port = 0
            if journaling:
                from .journal import ensure
                local, journal_port = ensure(client)
                result = local.call("replay")
                if result["restored"]:
                    print(f"Recovered {result['restored']} files from the client journal")
            uid, gid = os.getuid(), os.getgid()
            privilege = ["sudo"] if uid else []
            try:
                mountpoint.mkdir(parents=True, exist_ok=True)
            except PermissionError:
                subprocess.run(privilege + ["mkdir", "-p", "--", str(mountpoint)], check=True)
            options = (f"server={address},port={url.port or 80},token={client.token},"
                       f"uid={uid},gid={gid},autosync={autosync},cache_mib={cache_mib},journal_port={journal_port}")
            command = ["mount", "-t", "vtfs", "none", str(mountpoint), "-o", options]
            subprocess.run(privilege + command, check=True)
            save_configuration(mountpoint, server, token_file, autosync, cache_mib, journaling, managed_local)
            print(f"GOI mounted at {mountpoint}")
            return 0
        target = args.mountpoint if args.command in ("auth", "unmount", "journal") else (
            args.path if args.command == "activate" else Path.cwd())
        client = connection(target)
        if args.command == "journal":
            from .journal import ensure
            local, _ = ensure(client)
            if args.action == "discard":
                require(not os.path.ismount(client.mountpoint), errno.EBUSY, "unmount before discarding journal entries")
                require(args.id is not None, message="journal discard requires --id NUMBER")
                print(json.dumps(local.call("discard", id=args.id)))
            elif args.action == "recover":
                require(not os.path.ismount(client.mountpoint), errno.EBUSY, "unmount before journal recovery")
                print(json.dumps(local.call("replay")))
            elif args.action == "export":
                import base64
                require(args.output is not None, message="journal export requires --output DIRECTORY")
                args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
                info = local.call("info")
                for item in info["files"]:
                    data = local.call("export", id=item["id"])
                    fd = os.open(args.output / str(item["id"]), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as file:
                        file.write(base64.b64decode(data["data"], validate=True))
                        file.flush()
                        os.fsync(file.fileno())
                print(f"Exported {len(info['files'])} files; originals retained in journal")
            else:
                print(json.dumps(local.call("info"), indent=2))
            return 0
        if args.command == "accounts":
            result = client.call("accounts")
            print("Client ID: " + result["client_id"])
            for account in result["accounts"]:
                print(account["login"] + (": authenticated" if account["authenticated"] else ": logged out"))
            return 0
        if args.command == "logout":
            client.sync_all()
            client.call("logout", login=args.login)
            print("GitHub token removed from service; working files retained")
            return 0
        if args.command == "cache":
            result = client.call("cache", clear=args.clear)
            print(f"Server cache: {result['cached_bytes']} bytes; limit {result['limit_bytes']} bytes; "
                  f"freed {result['removed_bytes']} bytes")
            return 0
        if args.command == "auth":
            flow = client.call("auth_start")
            print(f"Open {flow['verification_uri']} and enter {flow['user_code']}", flush=True)
            if not args.no_browser:
                webbrowser.open(flow["verification_uri"])
            deadline, interval = time.monotonic() + flow["expires_in"], flow["interval"]
            while time.monotonic() < deadline:
                time.sleep(interval)
                result = client.call("auth_poll", flow=flow["flow"])
                if not result.get("pending"):
                    print(f"Connected: {client.mountpoint / result['login']}")
                    return 0
                interval = result["interval"]
            raise Error(errno.ETIMEDOUT, "Device Flow expired")
        if args.command == "activate":
            client.call("activate", path=client.relative(args.path))
            print("Repository activated; open its <branch> directory")
            return 0
        if args.command == "sync":
            if not args.paths:
                client.sync_all()
            for path in args.paths:
                sync_file(path)
            print("Saved on GOI server; no GitHub commit created")
            return 0
        if args.command == "unmount":
            client.sync_all()
            subprocess.run((["sudo"] if os.getuid() else []) + ["umount", str(client.mountpoint)], check=True)
            return 0
        if args.command == "refresh" and len(Path.cwd().relative_to(client.mountpoint).parts) <= 1:
            relative = client.relative(Path.cwd())
            client.call("refresh_owners", path="" if relative == "." else relative)
            print("Owner listing updated")
            return 0
        branch, directory = branch_context(client, Path.cwd())
        if args.command == "commit":
            paths = []
            for path in args.paths:
                relative = path.resolve().relative_to(directory).as_posix()
                paths.append(relative)
                if path.is_dir():
                    client.sync_all()
                elif path.exists():
                    sync_file(path)
            if not paths:
                client.sync_all()
            message = directory / ".goi/commit/message"
            with message.open("w") as file:
                file.write(args.message)
                file.flush()
                os.fsync(file.fileno())
            result = client.call("commit", branch=branch, paths=paths, message=args.message, force=args.force)
            print(result.get("sha") or result.get("message", "No changes"))
        elif args.command == "recover":
            client.sync_all()
            print(json.dumps(client.call("recover", branch=branch, forget=args.forget)))
        elif args.command == "restore":
            client.sync_all()
            paths = [path.resolve().relative_to(directory).as_posix() for path in args.paths]
            result = client.call("restore", branch=branch, paths=paths)
            print(f"Restored {result['restored']} objects; reopen affected files")
        elif args.command in ("status", "diff"):
            client.sync_all()
            result = client.call(args.command, branch=branch, ignored=getattr(args, "ignored", False))
            if args.command == "diff":
                print(result["diff"])
            elif args.json:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                print("\n".join(f"{item['state']:10} {item['path']}" for item in result) or "No local changes")
        elif args.command == "refresh":
            client.sync_all()
            result = client.call("refresh", branch=branch)
            print(result["head"])
            if result["conflicts"]:
                print("Conflicts: " + ", ".join(result["conflicts"]))
            if result["preserved_directories"]:
                print("Local directory subtrees retained: " + ", ".join(result["preserved_directories"]))
            print("Reopen files to see remote changes")
        elif args.command == "resolve":
            client.sync_all()
            relative = args.path.resolve().relative_to(directory).as_posix()
            choice = "ours" if args.ours else "theirs" if args.theirs else None
            client.call("resolve", branch=branch, path=relative, choice=choice)
            print("Resolution confirmed; reopen the file to see the resolved version, then goi commit")
        return 0
    except (Error, OSError, ValueError, subprocess.CalledProcessError) as exc:
        # CalledProcessError contains mount argv, including the session token.
        message = "system command failed" if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        print("goi: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
