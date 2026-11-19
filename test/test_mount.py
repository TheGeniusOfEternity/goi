"""Check an already mounted, empty VTFS without loading kernel modules."""

import argparse
import errno
import os
from pathlib import Path
import stat
import subprocess
import unittest


class MountTests(unittest.TestCase):
    mountpoint: Path

    @classmethod
    def setUpClass(cls):
        if not hasattr(cls, "mountpoint"):
            raise unittest.SkipTest("pass an explicit mounted VTFS path to this script")

    def test_filesystem_type(self):
        result = subprocess.run(
            ["findmnt", "-n", "-o", "FSTYPE", "--mountpoint", str(self.mountpoint)],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.stdout.strip(), "vtfs")

    def test_root_metadata(self):
        root = self.mountpoint.stat()
        self.assertTrue(stat.S_ISDIR(root.st_mode))
        self.assertEqual(stat.S_IMODE(root.st_mode), 0o755)
        self.assertGreater(root.st_ino, 0)
        self.assertEqual(root.st_nlink, 2)

    def test_directory_iteration(self):
        # Reopening the directory must restart enumeration.
        for _ in range(3):
            self.assertEqual(os.listdir(self.mountpoint), [])
        result = subprocess.run(
            ["ls", "-a1", "--", str(self.mountpoint)],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(set(result.stdout.splitlines()), {".", ".."})

    def test_navigation(self):
        result = subprocess.run(
            ["pwd", "-P"], cwd=self.mountpoint, check=True,
            capture_output=True, text=True,
        )
        self.assertEqual(result.stdout.strip(), str(self.mountpoint))

    def test_missing_file(self):
        # Repeat to also exercise lookup of a cached negative dentry.
        for _ in range(2):
            with self.assertRaises(OSError) as error:
                os.stat(self.mountpoint / "missing")
            self.assertEqual(error.exception.errno, errno.ENOENT)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mountpoint", type=Path)
    args = parser.parse_args()
    MountTests.mountpoint = args.mountpoint.resolve(strict=True)
    unittest.main(argv=[__file__], verbosity=2)
