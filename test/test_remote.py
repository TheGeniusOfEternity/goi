"""Root-only kernel/HTTP/SQLite/CLI integration tests, intended for the QEMU VM."""

import errno
import json
import mmap
import os
from pathlib import Path
import socket
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from goi.cli import save_configuration
from goi.server import Handler, Server
from goi.storage import Store
from fake_github import FakeGitHub

LAB = Path(__file__).resolve().parents[1]
TOKEN = "0123456789abcdef0123456789abcdef"


class FaultHandler(Handler):
    def api(self, method, args, body):
        if method == "read":
            self.server.file_reads = getattr(self.server, "file_reads", 0) + 1
        return super().api(method, args, body)

    def send(self, payload, *args, **kwargs):
        if self.path.startswith("/api/write?") and getattr(self.server, "drop_write", False):
            self.server.drop_write = False
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        if self.path.startswith("/api/write?") and getattr(self.server, "short_write", False):
            self.server.short_write = False
            payload = payload[:18]  # Valid HTTP, incomplete metadata after the status word.
        super().send(payload, *args, **kwargs)


class RemoteTests(unittest.TestCase):
    enabled = False

    @classmethod
    def setUpClass(cls):
        if not cls.enabled:
            raise unittest.SkipTest("run this script explicitly for kernel integration in a dedicated VM")
        if os.geteuid() != 0:
            raise unittest.SkipTest("kernel integration requires root in a dedicated Linux VM")
        if Path("/sys/module/vtfs").exists():
            raise RuntimeError("vtfs already loaded; finish existing mounts and unload it before testing")
        subprocess.run(["insmod", str(LAB / "source/vtfs.ko")], check=True)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["rmmod", "vtfs"], check=True)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="goi-remote-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.mount = self.root / "mount"
        self.mount.mkdir()
        self.github = FakeGitHub()
        self.store = Store(self.root / "state", "test-client", self.github)
        self.addCleanup(self.store.close)
        self.store.add_account("fake-token")
        self.store.db.commit()
        self.server = Server(("127.0.0.1", 0), self.store, TOKEN)
        self.server.RequestHandlerClass = FaultHandler
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.token_file = self.root / "token"
        self.token_file.write_text(TOKEN)
        self.token_file.chmod(0o600)
        self.old_config = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(self.root / "config")
        self.addCleanup(self.restore_config)
        save_configuration(self.mount, f"http://127.0.0.1:{self.server.server_port}", self.token_file)
        self.mount_fs()
        self.addCleanup(lambda: subprocess.run(["umount", str(self.mount)], check=True))
        self.repo = self.mount / "alice/demo"
        self.cli("activate", str(self.repo), cwd=self.mount)
        self.branch = self.repo / "main"

    def test_mount_cli_starts_journal_and_reports_pending_writes(self):
        subprocess.run(["umount", str(self.mount)], check=True)
        self.cli("mount", str(self.mount), "--autosync", "0", cwd=self.root)
        path = self.branch / "README.md"
        path.write_bytes(b"CLI journal")
        status = json.loads(self.cli("journal").stdout)
        self.assertEqual(len(status["files"]), 1)
        self.assertEqual(status["files"][0]["bytes"], len(b"CLI journal"))
        output = self.root / "export"
        self.cli("journal", "export", "--output", str(output))
        self.assertEqual((output / str(status["files"][0]["id"])).read_bytes(), b"CLI journal")
        self.assertNotEqual(self.cli("journal", "recover", success=False).returncode, 0)
        self.sync(path)
        self.assertEqual(json.loads(self.cli("journal").stdout)["files"], [])

    def test_ram_limit_evicts_clean_files_but_preserves_dirty_and_open(self):
        subprocess.run(["umount", str(self.mount)], check=True)
        self.mount_fs(cache_mib=1)
        first, second = self.branch / "first", self.branch / "second"
        payload = b"x" * (700 * 1024)
        first.write_bytes(payload)
        self.sync(first)
        second.write_bytes(payload)
        with self.assertRaises(OSError) as error:
            first.read_bytes()  # The second file is still dirty and cannot be evicted.
        self.assertEqual(error.exception.errno, errno.ENOSPC)
        self.sync(second)
        with first.open("rb") as file:
            self.assertEqual(file.read(), payload)
            with self.assertRaises(OSError) as error:
                second.read_bytes()
            self.assertEqual(error.exception.errno, errno.ENOSPC)
        self.assertEqual(second.read_bytes(), payload)
        with second.open("ab") as file:
            with self.assertRaises(OSError) as error:
                os.write(file.fileno(), b"y" * (400 * 1024))
            self.assertEqual(error.exception.errno, errno.ENOSPC)
        self.assertEqual(second.read_bytes(), payload)

    def test_kernel_journal_persists_before_fsync_and_clears_after_ack(self):
        from goi.journal import Journal, JournalHandler
        journal = Journal(self.root / "journal", f"http://127.0.0.1:{self.server.server_port}", TOKEN)
        self.addCleanup(journal.close)
        local = Server(("127.0.0.1", 0), journal, TOKEN)
        local.RequestHandlerClass = JournalHandler
        threading.Thread(target=local.serve_forever, daemon=True).start()
        self.addCleanup(local.server_close)
        self.addCleanup(local.shutdown)
        subprocess.run(["umount", str(self.mount)], check=True)
        self.mount_fs(journal_port=local.server_port)
        path = self.branch / "README.md"
        path.write_bytes(b"journal first")
        with journal.lock:
            row = journal.db.execute("SELECT * FROM writes").fetchone()
            self.assertEqual(row["data"], b"journal first")
        with self.store.lock:
            self.assertNotEqual(self.store.node(row["id"])["data"], b"journal first")
        self.sync(path)
        with journal.lock:
            self.assertEqual(journal.db.execute("SELECT COUNT(*) FROM writes").fetchone()[0], 0)
        with self.store.lock:
            self.assertEqual(self.store.node(row["id"])["data"], b"journal first")
        self.assertIn("Client ID:", self.cli("accounts").stdout)
        self.cli("logout", "alice")
        self.assertIn("logged out", self.cli("accounts").stdout)
        self.assertEqual(path.read_bytes(), b"journal first")

    def test_refresh_preserves_local_empty_directory(self):
        directory = self.branch / "local-empty"
        directory.mkdir()
        self.cli("refresh")
        self.assertTrue(directory.is_dir())
        # The directory also survives dropping the kernel's cache by remounting.
        subprocess.run(["umount", str(self.mount)], check=True)
        self.mount_fs()
        self.assertTrue(directory.is_dir())
        directory.rmdir()
        self.cli("refresh")
        self.assertTrue((self.branch / "README.md").is_file())

    def test_restore_cli_reopens_original_and_undoes_move(self):
        path = self.branch / "README.md"
        original = path.read_bytes()
        path.write_bytes(b"edited")
        self.cli("restore", "README.md")
        self.assertEqual(path.read_bytes(), original)
        path.rename(self.branch / "moved")
        self.cli("restore", "moved")
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.branch / "moved").exists())

    def test_refresh_keeps_local_file_and_reloads_remote(self):
        path = self.branch / "README.md"
        path.write_bytes(b"my edit")
        other = self.branch / "src/main.c"
        other.read_bytes()
        with self.store.lock:
            self.github.external_edit("src/main.c", b"remote version")
        self.cli("refresh")
        self.assertEqual(path.read_bytes(), b"my edit")
        self.assertEqual(other.read_bytes(), b"remote version")

    def restore_config(self):
        if self.old_config is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = self.old_config

    def mount_fs(self, autosync=0, cache_mib=256, journal_port=0):
        options = f"server=127.0.0.1,port={self.server.server_port},token={TOKEN},autosync={autosync},cache_mib={cache_mib},journal_port={journal_port}"
        subprocess.run(["mount", "-t", "vtfs", "none", str(self.mount), "-o", options], check=True)

    def cli(self, *arguments, cwd=None, success=True):
        result = subprocess.run([sys.executable, "-B", str(LAB / "bin/goi"), *arguments],
                                cwd=cwd or self.branch, capture_output=True, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def sync(self, path):
        with path.open("rb") as file:
            os.fsync(file.fileno())

    def test_lazy_read_and_binary_fsync(self):
        self.assertEqual(self.github.blob_reads, 0)
        self.assertIn("README.md", os.listdir(self.branch))
        self.assertIn("main.c", os.listdir(self.branch / "src"))
        self.assertEqual(self.github.blob_reads, 0)
        path = self.branch / "binary"
        self.assertEqual(path.read_bytes(), bytes(range(256)))
        payload = bytes(range(256)) * 300
        path.write_bytes(payload)
        self.sync(path)
        with self.store.lock:
            node = self.store.resolve_path("alice/demo/main/binary")
            self.assertEqual(self.store.read(node["id"], 0, len(payload)), payload)

    def test_create_move_truncate_and_reopen(self):
        folder = self.branch / "new"
        folder.mkdir()
        path = folder / "original"
        path.write_bytes(b"abcdefgh")
        with path.open("r+b", buffering=0) as file:
            file.seek(2)
            file.write(b"XY")
            file.truncate(12)
            os.fsync(file.fileno())
        target = self.branch / "moved"
        path.rename(target)
        self.assertEqual(target.read_bytes(), b"abXYefgh\0\0\0\0")
        self.sync(target)
        folder.rmdir()
        subprocess.run(["umount", str(self.mount)], check=True)
        self.mount_fs()
        self.assertEqual(target.read_bytes(), b"abXYefgh\0\0\0\0")

    def test_cli_multi_file_commit(self):
        (self.branch / "README.md").write_bytes(b"readme updated")
        (self.branch / "src/main.c").write_bytes(b"code updated")
        before = len(self.github.commits)
        self.cli("commit", "README.md", "src/main.c", "-m", "both files")
        self.assertEqual(len(self.github.commits), before + 1)
        self.assertEqual(json.loads(self.cli("status", "--json").stdout), [])

    def test_conflict_workspace_and_explicit_resolution(self):
        path = self.branch / "README.md"
        path.write_bytes(b"local\n")
        self.sync(path)
        with self.store.lock:
            self.github.external_edit("README.md", b"remote\n")
        result = self.cli("commit", "README.md", "-m", "conflict", success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(path.read_bytes(), b"local\n")
        conflict = self.branch / ".goi/commit/changes/README.md"
        self.assertIn(b"<<<<<<< local", conflict.read_bytes())
        conflict.write_bytes(b"combined\n")
        self.sync(conflict)
        self.cli("resolve", "README.md")
        self.assertEqual(path.read_bytes(), b"combined\n")
        self.cli("commit", "README.md", "-m", "resolved")

    def test_background_sync(self):
        subprocess.run(["umount", str(self.mount)], check=True)
        self.mount_fs(autosync=1)
        path = self.branch / "auto"
        path.write_bytes(b"automatic")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with self.store.lock:
                node = self.store.resolve_path("alice/demo/main/auto")
                if node["data"] == b"automatic":
                    return
            time.sleep(0.1)
        self.fail("background sync did not reach server")

    def test_switch_reclaims_closed_files_and_keeps_mappings(self):
        self.cli("switch", "main")
        path = self.branch / "README.md"
        path.write_bytes(b"saved by switch\n")  # Closed, but not explicitly fsynced.
        mapped_path = self.branch / "src/main.c"
        with mapped_path.open("rb") as file:
            mapping = mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ)
        self.addCleanup(mapping.close)
        before_reads = self.server.file_reads
        before_blobs = self.github.blob_reads
        before_commits = len(self.github.commits)
        result = self.cli("switch", "feature/vfs")
        self.assertEqual(Path(result.stdout.strip()), self.repo / "feature%2Fvfs")
        self.assertEqual(self.server.file_reads, before_reads)  # No target files prefetched.
        with self.store.lock:
            node = self.store.resolve_path("alice/demo/main/README.md")
            self.assertEqual(node["data"], b"saved by switch\n")
        self.assertEqual(len(self.github.commits), before_commits)
        self.assertEqual(path.read_bytes(), b"saved by switch\n")
        self.assertEqual(self.server.file_reads, before_reads + 1)  # Closed cache was dropped.
        self.assertEqual(mapping[:], b"int main(void) { return 0; }\n")
        mapping.close()
        self.assertEqual(mapped_path.read_bytes(), b"int main(void) { return 0; }\n")
        self.assertEqual(self.server.file_reads, before_reads + 2)
        self.assertEqual(self.github.blob_reads, before_blobs)  # Reloaded from SQLite, not GitHub.

    def test_shell_cd_switch_and_failed_save(self):
        script = r'''
set -eu
export PATH="$GOI_LAB/bin:$PATH"
. "$GOI_LAB/shell/goi.sh"
goi switch "$GOI_MAIN"
printf 'keep local data\n' > README.md
if cd ../feature%2Fvfs; then exit 31; fi
test "$PWD" = "$GOI_MAIN"
test "$(cat README.md)" = 'keep local data'
cd ../feature%2Fvfs
test "$PWD" = "$GOI_OTHER"
cd ../main/src
test "$PWD" = "$GOI_MAIN/src"
cd - >/dev/null
test "$PWD" = "$GOI_OTHER"
mkdir -p 'directory with spaces'
cd 'directory with spaces'
goi switch main
test "$PWD" = "$GOI_MAIN"
goi switch --help >/dev/null
test "$PWD" = "$GOI_MAIN"
if cd ../missing-branch; then exit 32; fi
test "$PWD" = "$GOI_MAIN"
cd /tmp
test "$PWD" = /tmp
cd "$GOI_OTHER/src"
test "$PWD" = "$GOI_OTHER/src"
'''
        for shell in ("bash", "zsh"):
            executable = shutil.which(shell)
            if executable is None:
                continue
            with self.subTest(shell=shell):
                self.server.drop_write = True
                result = subprocess.run([executable, "-f", "-c", script], cwd=self.branch,
                                        env={**os.environ, "GOI_LAB": str(LAB), "GOI_MAIN": str(self.branch),
                                             "GOI_OTHER": str(self.repo / "feature%2Fvfs")},
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_git_trap_and_cross_branch_move(self):
        self.assertIn(b"use-goi", (self.branch / ".git").read_bytes())
        git = subprocess.run(["git", "-C", str(self.branch), "status"], capture_output=True)
        self.assertNotEqual(git.returncode, 0)
        self.assertIn(b"use-goi-instead", git.stderr)
        with self.assertRaises(OSError):
            (self.branch / ".git").unlink()
        other = self.repo / "feature%2Fvfs"
        with self.assertRaises(OSError) as error:
            (self.branch / "README.md").rename(other / "moved")
        self.assertEqual(error.exception.errno, errno.EXDEV)

    def test_device_flow_cli(self):
        result = self.cli("auth", str(self.mount), "--no-browser")
        self.assertIn("TEST-CODE", result.stdout)
        self.assertIn("Connected:", result.stdout)

    def test_fsync_error_and_manual_retry(self):
        path = self.branch / "README.md"
        with path.open("r+b", buffering=0) as file:
            file.write(b"first\n")
            self.server.drop_write = True
            with self.assertRaises(OSError):
                os.fsync(file.fileno())
            self.server.short_write = True
            with self.assertRaises(OSError):
                os.fsync(file.fileno())
            file.seek(0)
            file.write(b"second version\n")
            file.truncate()
            os.fsync(file.fileno())
        with self.store.lock:
            node = self.store.resolve_path("alice/demo/main/README.md")
            self.assertEqual(node["data"], b"second version\n")

    def test_atomic_editor_save_and_delete_commit(self):
        old_file = (self.branch / "README.md").open("r+b", buffering=0)
        self.addCleanup(old_file.close)
        temporary = self.branch / ".README.tmp"
        temporary.write_bytes(b"atomic replacement\n")
        temporary.replace(self.branch / "README.md")
        self.cli("commit", "README.md", "-m", "editor save")
        old_file.write(b"old descriptor")
        os.fsync(old_file.fileno())
        self.assertEqual(json.loads(self.cli("status", "--json").stdout), [])
        (self.branch / "README.md").unlink()
        self.cli("commit", "README.md", "-m", "remove")
        with self.store.lock:
            head = self.github.heads["main"]
            tree = self.github.full_tree("alice", "demo", self.github.commits[head]["tree"]["sha"])
            self.assertNotIn("README.md", tree)

    def test_nested_directory_commit_flushes_descendants(self):
        (self.branch / "src/main.c").write_bytes(b"not fsynced explicitly\n")
        self.cli("commit", "src", "-m", "directory selection")
        self.assertEqual(json.loads(self.cli("status", "--json").stdout), [])

    def test_mount_is_private_to_owner(self):
        self.root.chmod(0o711)  # The mount, rather than its temporary parent, must deny access.
        result = subprocess.run([sys.executable, "-c", "import os,sys; os.listdir(sys.argv[1])", str(self.mount)],
                                user=12345, group=12345, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"PermissionError", result.stderr)

    def test_metadata_change_preserves_unloaded_file_content(self):
        path = self.branch / "README.md"
        path.chmod(0o755)
        os.utime(path, None)
        self.sync(path)
        self.assertEqual(path.read_bytes(), b"hello\n")
        with self.store.lock:
            node = self.store.resolve_path("alice/demo/main/README.md")
            self.assertEqual(node["data"], b"hello\n")
            self.assertTrue(node["mode"] & 0o111)

    def test_sparse_offsets_mmap_and_paged_unicode_listing(self):
        path = self.branch / "данные % &.bin"
        with path.open("w+b", buffering=0) as file:
            os.pwrite(file.fileno(), b"end", 70000)
            os.fsync(file.fileno())
            self.assertEqual(os.pread(file.fileno(), 3, 70000), b"end")
            self.assertEqual(os.pread(file.fileno(), 10, 1000), b"\0" * 10)
            with mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ) as mapping:
                self.assertEqual(mapping[-3:], b"end")
            with self.assertRaises(OSError):
                mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_WRITE)
        with self.store.lock:
            branch = self.store.resolve_path("alice/demo/main")
            for index in range(105):
                self.store.create(branch["id"], f"entry-{index:03}", stat.S_IFREG | 0o600)
            self.store.db.commit()
        expected = {f"entry-{index:03}" for index in range(105)}
        self.assertTrue(expected.issubset(set(os.listdir(self.branch))))
        self.assertEqual(path.read_bytes(), b"\0" * 70000 + b"end")


if __name__ == "__main__":
    RemoteTests.enabled = True
    unittest.main(verbosity=2)
