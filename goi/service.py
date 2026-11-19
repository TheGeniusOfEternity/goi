"""Manage the optional per-user systemd service for the default local backend."""
import errno
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import Request, urlopen

from .protocol import Error, META, STATUS, require

URL = "http://127.0.0.1:8080"


def data_directory():
    # Keep the same default as goi-server and existing mount configurations.
    return Path.home() / ".local/share/goi"


def unit_path():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user/goi.service"


def systemctl(*args):
    try:
        result = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)
    except FileNotFoundError:
        raise Error(errno.ENOSYS, "systemd is unavailable; start goi-server manually and pass --server URL") from None
    require(result.returncode == 0, errno.EIO,
            "user systemd operation failed; check systemctl --user status goi and journalctl --user -u goi. "
            "Without a user systemd session, start goi-server manually and pass --server URL")
    return result.stdout


def argument(value):
    value = str(value)
    require(not any(c in value for c in "\n\r\0"), message="invalid systemd argument")
    # ExecStart is not a shell; protect systemd specifiers and variable expansion too.
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def install(client_id=None):
    path = unit_path()
    if path.exists() and client_id is None:
        systemctl("daemon-reload")
        systemctl("enable", "goi.service")
        return path  # Preserve the existing OAuth App and user edits.
    client_id = client_id if client_id is not None else os.environ.get("GOI_GITHUB_CLIENT_ID", "")
    executable = Path(__file__).resolve().parents[1] / "bin/goi-server"
    command = [sys.executable, executable, "--data", data_directory(), "--listen", "127.0.0.1",
               "--port", "8080", "--client-id", client_id]
    contents = ("[Unit]\nDescription=GOI local GitHub filesystem service\n\n[Service]\nType=simple\n"
                "ExecStart=" + " ".join(map(argument, command)) + "\n"
                "Restart=on-failure\nRestartSec=2\nUMask=0077\n\n[Install]\nWantedBy=default.target\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(contents)
    temporary.chmod(0o600)
    temporary.replace(path)
    systemctl("daemon-reload")
    systemctl("enable", "goi.service")
    return path


def ready():
    try:
        token = (data_directory() / "server-token").read_text().strip()
        request = Request(URL + "/api/hello", headers={"Authorization": "Bearer " + token})
        with urlopen(request, timeout=1) as response:
            data = response.read(STATUS.size + META.size + 1)
        return len(data) == STATUS.size + META.size and STATUS.unpack_from(data)[0] == 0
    except (OSError, ValueError):
        return False


def ensure():
    if ready():
        return  # A manually started compatible server is also usable.
    try:
        with socket.create_connection(("127.0.0.1", 8080), timeout=1):
            occupied = True
    except OSError:
        occupied = False
    require(not occupied, errno.EADDRINUSE,
            "port 8080 is occupied but GOI authentication failed; check the server/token before mounting")
    if not unit_path().exists():
        install()
    systemctl("start", "goi.service")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if ready():
            return
        time.sleep(.2)
    raise Error(errno.ETIMEDOUT, "GOI service did not become ready; inspect journalctl --user -u goi")
