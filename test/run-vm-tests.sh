#!/usr/bin/env bash
# Run from a normal VM terminal as root, after building the module.
set -euo pipefail

lab_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
module_path="$lab_dir/source/vtfs.ko"

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo bash $0 in the VM terminal." >&2
    exit 1
fi
if [[ -d /sys/module/vtfs ]]; then
    echo "vtfs is already loaded; unmount and unload it before this test." >&2
    exit 1
fi
if [[ ! -f $module_path ]]; then
    echo "Build the module with make W=1 in $lab_dir first." >&2
    exit 1
fi

for tool in insmod rmmod mount umount mountpoint python3 findmnt; do
    command -v "$tool" >/dev/null
done

test_dir=$(mktemp -d /tmp/goi-vtfs-tests.XXXXXX)
first="$test_dir/first"
second="$test_dir/second"
mkdir "$first" "$second"
loaded=0

cleanup() {
    local result=$?
    trap - EXIT
    for path in "$second" "$first"; do
        if mountpoint -q "$path"; then
            umount "$path" || result=1
        fi
    done
    if [[ $loaded == 1 ]]; then
        rmmod vtfs || result=1
    fi
    rmdir "$first" "$second" "$test_dir" || result=1
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for cycle in 1 2; do
    echo "VTFS load/mount/test/unmount cycle $cycle"
    insmod "$module_path"
    loaded=1
    mount -t vtfs none "$first"
    mount -t vtfs none "$second"

    python3 -B "$lab_dir/test/test_mount.py" "$first"
    python3 -B "$lab_dir/test/test_ram.py" "$first"

    # An independent mount must not expose the first mount's data.
    printf 'first mount only' > "$first/isolation"
    test ! -e "$second/isolation"
    python3 -B "$lab_dir/test/test_mount.py" "$second"
    umount "$first"
    mount -t vtfs none "$first"
    # The previous instance, including its isolation file, is gone.
    python3 -B "$lab_dir/test/test_mount.py" "$first"

    umount "$second"
    umount "$first"
    rmmod vtfs
    loaded=0
done

echo "VTFS tests passed; both mount instances and the module were removed."
