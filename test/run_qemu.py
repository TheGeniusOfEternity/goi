"""Build an ephemeral initramfs and test VTFS in QEMU without host root access.

Requires aarch64 qemu-system-aarch64, a matching readable kernel image, busybox,
cpio, and the already built module. No host mounts or kernel module loads occur.
"""

import argparse
import gzip
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--qemu", default="qemu-system-aarch64")
    parser.add_argument("--kernel", required=True, help="matching uncompressed ARM64 Image (not an EFI vmlinuz)")
    parser.add_argument("--log", default="/tmp/goi-qemu.log")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--crash-test", action="store_true", help="kill a VM before fsync and recover from a persistent ext4 disk")
    args = parser.parse_args()
    with open(args.kernel, "rb") as kernel:
        header = kernel.read(64)
    if header[56:60] != b"ARM\x64":
        parser.error("--kernel must be an uncompressed ARM64 Image, not an EFI/compressed vmlinuz")
    lab = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="goi-initramfs-") as temporary:
        root = Path(temporary) / "root"
        root.mkdir()

        def copy(source, target=None):
            source = Path(source)
            destination = root / str(target or source).lstrip("/")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination, follow_symlinks=True)

        def executable(path, target=None):
            copy(path, target)
            result = subprocess.run(["ldd", path], capture_output=True, text=True)
            for library in re.findall(r"(?:=>\s*)?(/[^\s]+)", result.stdout):
                copy(library)

        copy("/usr/bin/busybox", "/bin/busybox")
        for applet in subprocess.check_output(["busybox", "--list"], text=True).splitlines():
            link = root / "bin" / applet
            if not link.exists():
                link.symlink_to("busybox")
        executable("/bin/bash")
        if zsh := shutil.which("zsh"):
            executable(zsh, "/usr/bin/zsh")
        executable("/usr/bin/python3.14")
        executable("/usr/bin/findmnt")
        executable("/usr/bin/mountpoint")
        executable("/usr/bin/git")
        (root / "bin/mountpoint").symlink_to("/usr/bin/mountpoint")
        (root / "bin/python3").symlink_to("/usr/bin/python3.14")
        (root / "usr/bin/env").symlink_to("/bin/env")
        (root / "bin/findmnt").unlink(missing_ok=True)
        (root / "bin/findmnt").symlink_to("/usr/bin/findmnt")
        shutil.copytree("/usr/lib/python3.14", root / "usr/lib/python3.14",
                        ignore=shutil.ignore_patterns("__pycache__", "test", "tests", "idlelib", "tkinter"))
        for extension in Path("/usr/lib/python3.14/lib-dynload").glob("*.so"):
            executable(str(extension))
        shutil.copytree(lab / "test", root / "lab/test", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(lab / "goi", root / "lab/goi", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(lab / "bin", root / "lab/bin")
        shutil.copytree(lab / "shell", root / "lab/shell")
        copy(lab / "source/vtfs.ko", "/lab/source/vtfs.ko")
        for directory in ("proc", "sys", "dev", "tmp", "etc", "mnt", "root", "persist"):
            (root / directory).mkdir(exist_ok=True)
        (root / "etc/passwd").write_text("root:x:0:0:root:/root:/bin/sh\n")
        (root / "etc/group").write_text("root:x:0:\n")
        init = root / "init"
        init.write_text("""#!/bin/sh
export PATH=/bin:/usr/bin
export HOME=/root
export NO_COLOR=1
mount -t proc proc /proc
mount -t sysfs sys /sys
mount -t devtmpfs dev /dev
case "$(cat /proc/cmdline)" in
    *goi_crash=write*) phase=write ;;
    *goi_crash=recover*) phase=recover ;;
    *) phase= ;;
esac
if [ -n "$phase" ]; then
    ifconfig lo 127.0.0.1 up
    mount -t ext4 /dev/vda /persist || poweroff -f
    python3 -B /lab/test/crash_journal.py "$phase"
    echo GOI_CRASH_EXIT=$?
    poweroff -f
fi
echo GOI_QEMU_BEGIN
bash /lab/test/run-vm-tests.sh
result=$?
if [ "$result" = 0 ]; then
    ifconfig lo 127.0.0.1 up
    python3 -B /lab/test/test_remote.py
    result=$?
fi
echo GOI_QEMU_EXIT=$result
poweroff -f
""")
        init.chmod(0o755)
        archive = Path(temporary) / "initramfs.gz"
        names = subprocess.check_output(["find", ".", "-print0"], cwd=root)
        cpio = subprocess.run(["cpio", "--null", "-o", "--format=newc", "--owner=0:0"],
                              cwd=root, input=names, capture_output=True, check=True).stdout
        with gzip.open(archive, "wb", compresslevel=1) as output:
            output.write(cpio)
        command = [args.qemu, "-machine", "virt", "-cpu", "cortex-a72", "-accel", "tcg",
                   "-smp", "2", "-m", "768", "-nographic", "-no-reboot", "-nic", "none",
                   "-kernel", args.kernel, "-initrd", str(archive),
                   "-append", "console=ttyAMA0 rdinit=/init panic=-1"]
        with open(args.log, "w") as output:
            try:
                result = subprocess.run(command, stdout=output, stderr=subprocess.STDOUT, timeout=args.timeout)
            except subprocess.TimeoutExpired:
                raise SystemExit(f"QEMU timed out; inspect {args.log}")
        log = Path(args.log).read_text(errors="replace")
        print(log[log.find("GOI_QEMU_BEGIN"):])
        if result.returncode or "GOI_QEMU_EXIT=0" not in log or re.search(r"BUG:|Kernel panic|Oops:|WARNING: CPU:", log):
            raise SystemExit(f"Kernel test failed; inspect {args.log}")
        if args.crash_test:
            disk = Path(temporary) / "journal.ext4"
            with disk.open("wb") as file:
                file.truncate(64 * 1024 * 1024)
            subprocess.run(["/usr/sbin/mkfs.ext4", "-q", "-F", str(disk)], check=True)
            crash_log = Path(args.log + ".crash")
            base = command[:-2] + ["-drive", f"file={disk},format=raw,if=none,id=journal,cache=none",
                                  "-device", "virtio-blk-device,drive=journal"]
            with crash_log.open("w") as output:
                process = subprocess.Popen(base + ["-append", command[-1] + " goi_crash=write"],
                                           stdout=output, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic() + 120
                    while "GOI_CRASH_READY" not in crash_log.read_text(errors="replace"):
                        if process.poll() is not None or time.monotonic() > deadline:
                            raise SystemExit(f"Crash preparation failed; inspect {crash_log}")
                        time.sleep(.2)
                finally:
                    process.kill()  # No guest shutdown, fsync, or unmount.
                    process.wait()
            with crash_log.open("a") as output:
                result = subprocess.run(base + ["-append", command[-1] + " goi_crash=recover"],
                                        stdout=output, stderr=subprocess.STDOUT, timeout=120)
            crash = crash_log.read_text(errors="replace")
            if result.returncode or "GOI_CRASH_RECOVERED" not in crash or "GOI_CRASH_EXIT=0" not in crash or re.search(r"BUG:|Kernel panic|Oops:|WARNING: CPU:", crash):
                raise SystemExit(f"Crash recovery failed; inspect {crash_log}")
            print("GOI_CRASH_RECOVERED: write survived a killed VM before fsync; remote baseline unchanged before replay")



if __name__ == "__main__":
    main()
