"""Exercise RAM storage on an already mounted VTFS; no GitHub access required."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import errno
import mmap
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import unittest


class RamTests(unittest.TestCase):
    mountpoint: Path

    @classmethod
    def setUpClass(cls):
        if not hasattr(cls, "mountpoint"):
            raise unittest.SkipTest("pass an explicit mounted VTFS path to this script")

    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="goi-test-", dir=self.mountpoint))
        self.addCleanup(shutil.rmtree, self.work)

    def test_create_close_reopen_and_remove(self):
        path = self.work / "file"
        path.touch()
        self.assertEqual(path.read_bytes(), b"")
        path.write_bytes(b"hello")
        self.assertEqual(path.read_bytes(), b"hello")
        path.unlink()
        self.assertFalse(path.exists())

    def test_nested_directories(self):
        nested = self.work / "one" / "two" / "three"
        nested.mkdir(parents=True)
        path = nested / "data"
        path.write_bytes(b"nested")
        self.assertEqual(path.read_bytes(), b"nested")
        path.unlink()
        nested.rmdir()

    def test_directory_enumeration(self):
        names = {f"file-{i:03}" for i in range(300)}
        for name in names:
            (self.work / name).touch()
        for _ in range(2):
            self.assertEqual(set(os.listdir(self.work)), names)

    def test_negative_lookup_then_create(self):
        path = self.work / "previously-missing"
        self.assertFalse(path.exists())
        path.write_bytes(b"now present")
        self.assertEqual(path.read_bytes(), b"now present")

    def test_all_byte_values_across_pages(self):
        path = self.work / "binary"
        payload = bytes(range(256)) * 257
        path.write_bytes(payload)
        self.assertEqual(path.stat().st_size, len(payload))
        self.assertEqual(path.read_bytes(), payload)

    def test_offset_read_write_and_eof(self):
        path = self.work / "offsets"
        path.write_bytes(b"abcdefgh")
        with path.open("r+b", buffering=0) as file:
            self.assertEqual(os.pwrite(file.fileno(), b"XY", 3), 2)
            self.assertEqual(os.pread(file.fileno(), 4, 2), b"cXYf")
            self.assertEqual(os.pread(file.fileno(), 10, 8), b"")
            self.assertEqual(file.tell(), 0)
        self.assertEqual(path.read_bytes(), b"abcXYfgh")

    def test_append_and_overwrite(self):
        path = self.work / "append"
        path.write_bytes(b"first")
        with path.open("ab") as file:
            file.write(b"second")
        self.assertEqual(path.read_bytes(), b"firstsecond")
        path.write_bytes(b"x")
        self.assertEqual(path.read_bytes(), b"x")

    def test_truncate_shrink_and_zero_fill(self):
        path = self.work / "truncate"
        path.write_bytes(b"abcdefgh")
        with path.open("r+b", buffering=0) as file:
            file.truncate(3)
            file.truncate(9)
        self.assertEqual(path.read_bytes(), b"abc" + b"\0" * 6)

    def test_sparse_write_zero_fills_gap(self):
        path = self.work / "hole"
        with path.open("wb", buffering=0) as file:
            file.seek(2 * mmap.PAGESIZE + 7)
            file.write(b"end")
        self.assertEqual(path.read_bytes(), b"\0" * (2 * mmap.PAGESIZE + 7) + b"end")

    def test_unlink_keeps_open_file_alive(self):
        path = self.work / "unlinked"
        path.write_bytes(b"still open")
        with path.open("r+b", buffering=0) as file:
            path.unlink()
            self.assertFalse(path.exists())
            self.assertEqual(file.read(), b"still open")
            file.seek(0)
            file.write(b"STILL")
            file.seek(0)
            self.assertEqual(file.read(), b"STILL open")

    def test_rename_between_directories_keeps_inode_and_open_fd(self):
        source_dir = self.work / "source"
        target_dir = self.work / "target"
        source_dir.mkdir()
        target_dir.mkdir()
        source = source_dir / "before"
        target = target_dir / "after"
        source.write_bytes(b"moving")
        ino = source.stat().st_ino
        with source.open("r+b", buffering=0) as file:
            source.rename(target)
            self.assertFalse(source.exists())
            self.assertEqual(target.stat().st_ino, ino)
            file.write(b"M")
        self.assertEqual(target.read_bytes(), b"Moving")

    def test_editor_atomic_replace(self):
        target = self.work / "document"
        temp = self.work / ".document.tmp"
        target.write_bytes(b"old")
        temp.write_bytes(b"new\0content")
        with target.open("rb") as old:
            os.replace(temp, target)
            self.assertEqual(target.read_bytes(), b"new\0content")
            self.assertEqual(old.read(), b"old")
        self.assertFalse(temp.exists())

    def test_move_directory_updates_parent_link_counts(self):
        source = self.work / "source"
        target = self.work / "target"
        source.mkdir()
        target.mkdir()
        child = source / "child"
        child.mkdir()
        (child / "data").write_bytes(b"directory data")
        self.assertEqual(source.stat().st_nlink, 3)
        self.assertEqual(target.stat().st_nlink, 2)
        child.rename(target / "child")
        self.assertEqual(source.stat().st_nlink, 2)
        self.assertEqual(target.stat().st_nlink, 3)
        self.assertEqual((target / "child" / "data").read_bytes(), b"directory data")
        self.assertEqual((target / "child" / "..").stat().st_ino, target.stat().st_ino)

    def test_cannot_remove_or_replace_nonempty_directory(self):
        target = self.work / "target"
        source = self.work / "source"
        target.mkdir()
        source.mkdir()
        (target / "data").touch()
        with self.assertRaises(OSError) as error:
            target.rmdir()
        self.assertEqual(error.exception.errno, errno.ENOTEMPTY)
        with self.assertRaises(OSError) as error:
            source.rename(target)
        self.assertEqual(error.exception.errno, errno.ENOTEMPTY)

    def test_cannot_move_directory_into_itself(self):
        parent = self.work / "parent"
        child = parent / "child"
        child.mkdir(parents=True)
        with self.assertRaises(OSError) as error:
            parent.rename(child / "parent")
        self.assertEqual(error.exception.errno, errno.EINVAL)

    def test_mode_and_timestamps(self):
        path = self.work / "metadata"
        path.touch(mode=0o600)
        path.chmod(0o640)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        stamp = 1_700_000_000_123_456_789
        os.utime(path, ns=(stamp, stamp))
        self.assertEqual(path.stat().st_mtime_ns, stamp)

    def test_long_filename_rejected(self):
        with self.assertRaises(OSError) as error:
            (self.work / ("x" * 256)).touch()
        self.assertEqual(error.exception.errno, errno.ENAMETOOLONG)

    def test_shared_memory_mapping(self):
        path = self.work / "mapped"
        path.write_bytes(b"a" * mmap.PAGESIZE)
        with path.open("r+b") as file:
            with mmap.mmap(file.fileno(), mmap.PAGESIZE) as mapping:
                mapping[10:13] = b"XYZ"
                self.assertEqual(os.pread(file.fileno(), 3, 10), b"XYZ")
        self.assertEqual(path.read_bytes()[10:13], b"XYZ")

    def test_concurrent_append(self):
        path = self.work / "parallel"
        path.touch()

        def append(worker):
            fd = os.open(path, os.O_WRONLY | os.O_APPEND)
            try:
                for _ in range(50):
                    self.assertEqual(os.write(fd, bytes([worker]) * 32), 32)
            finally:
                os.close(fd)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(append, range(4)))
        data = path.read_bytes()
        self.assertEqual(len(data), 4 * 50 * 32)
        for worker in range(4):
            self.assertEqual(data.count(bytes([worker])), 50 * 32)

    def test_shell_file_commands(self):
        subprocess.run(
            ["sh", "-eu", "-c", 'mkdir nested; printf "hello\\n" > nested/a; '
             'cp nested/a nested/b; mv nested/b nested/c; '
             'cmp nested/a nested/c; rm nested/a nested/c; rmdir nested'],
            cwd=self.work, check=True,
        )
        self.assertEqual(list(self.work.iterdir()), [])

    def test_fsync_does_not_claim_remote_durability(self):
        path = self.work / "sync"
        path.write_bytes(b"RAM only")
        with path.open("r+b") as file:
            for sync in (os.fsync, os.fdatasync):
                with self.assertRaises(OSError) as error:
                    sync(file.fileno())
                self.assertEqual(error.exception.errno, errno.EOPNOTSUPP)
        self.assertEqual(path.read_bytes(), b"RAM only")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mountpoint", type=Path)
    args = parser.parse_args()
    RamTests.mountpoint = args.mountpoint.resolve(strict=True)
    result = subprocess.run(
        ["findmnt", "-n", "-o", "FSTYPE", "--mountpoint", str(RamTests.mountpoint)],
        check=True, capture_output=True, text=True,
    )
    if result.stdout.strip() != "vtfs":
        parser.error("mountpoint must be a mounted VTFS")
    unittest.main(argv=[__file__], verbosity=2)
