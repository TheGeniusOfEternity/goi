# Setup and command reference

GOI consists of a C kernel module, a Python storage service, and a Python CLI.
See [README](README.md), [the walkthrough](WALKTHROUGH.md), and
[the HTTP protocol](PROTOCOL.md).

## Requirements and build

Verified platform: Ubuntu ARM64, Linux 7.0.0-43-generic, Python 3.14.
Python components require Python 3.10+ with no third-party libraries.
The kernel module requires GCC, make, and matching kernel headers. Linux VFS
interfaces change between versions; other kernels may require porting.

Use a dedicated Linux VM for kernel development.

```bash
make W=1
export PATH="$PWD/bin:$PATH"
source "$PWD/shell/goi.sh"
sudo insmod source/vtfs.ko
```

The shell integration supports Bash and Zsh. It wraps cd to synchronize branch
transitions and makes goi switch change the current shell directory.

## Authentication and local service

Create a GitHub OAuth App with Device Flow enabled. Use its Client ID;
Device Flow does not require a client secret.

```bash
goi service install --client-id YOUR_CLIENT_ID
goi mount ~/github
goi auth ~/github
# On a headless machine:
goi auth ~/github --no-browser
```

The mount command creates its mountpoint and uses sudo for privileged mounting.
It starts the default local service and waits for authenticated HTTP readiness.
The service also starts at user login and restarts after failure.

The unit is stored in ~/.config/systemd/user/goi.service, respecting XDG_CONFIG_HOME.
Its executable path is absolute: after moving the checkout, install again with
the Client ID and restart. If the unit does not exist, mount creates it and uses
GOI_GITHUB_CLIENT_ID from the environment. Startup before user login is not configured.

```bash
goi service status
goi service restart
journalctl --user -u goi -f
# Unmount before stopping the server:
goi unmount ~/github
goi service stop
goi service disable
```

Disable prevents automatic startup at login; a later mount may still start the unit.
The default service binds to 127.0.0.1:8080. Only one process can own that port
on a machine. Separate machines can each run their own independent instance.

Service data is stored in ~/.local/share/goi. The directory is private (0700);
the database and server-token use 0600. The GOI session token authenticates
clients to this service. It is distinct from the GitHub access token stored by
the service.

## Manual or remote server

```bash
GOI_GITHUB_CLIENT_ID=YOUR_CLIENT_ID ./bin/goi-server \
  --data "$HOME/.local/share/goi" --listen 127.0.0.1 --port 8080

# In another terminal:
goi mount ~/github --server http://127.0.0.1:8080 \
  --token-file "$HOME/.local/share/goi/server-token"
```

An explicit --server or --token-file selects manual management for that mount.
The selection is saved in ~/.config/goi/mounts.json, respecting XDG_CONFIG_HOME.
Subsequent mounts reuse it. A remote service is started independently.
Use a trusted network or an SSH tunnel: the kernel transport uses HTTP, not TLS.

Default mount settings are localhost:8080, the standard token file, autosync
every second, a 256 MiB kernel file cache, and an enabled local write journal.
Flags override and persist settings:

```bash
goi mount ~/github --autosync 0 --cache-mib 512
```

## Repository activation and editing

Before authentication, listing the mount root returns EACCES.
Users and organizations appear directly below the mount root.
Repository activation is explicit; listing other repositories does not download
their file contents.

```bash
goi activate ~/github/YOUR_LOGIN/YOUR_REPO
# Or, from the repository directory:
goi activate
cd ~/github/YOUR_LOGIN/YOUR_REPO/main
cat README.md
printf '\nHello from GOI\n' >> README.md
goi sync README.md
goi status
goi diff
goi commit README.md -m "Update README"
```

Create a first commit on GitHub before activating an empty repository.
Each branch is a directory; slash characters in branch names become %2F.
Metadata is stored in .info.json; conflict copies are under .goi/commit/changes.
The .git entry prevents ordinary Git from treating GOI as a normal worktree.

Sync saves to the service; commit publishes to GitHub. Without paths, commit
selects all non-ignored changes. A directory selects changes below it.
Deletions and renames are included; rename across branches returns EXDEV.
The goida alias invokes goi commit.

## Branches and caches

```bash
goi switch main
cd ../feature%2Fexample/src
goi cache
goi cache --clear
```

With shell integration, cd into another branch synchronizes first and asks the
kernel to release clean, closed files from other branches. Open files and mmap
references keep their data alive. The CLI alone cannot change its parent shell's
directory; the shell wrapper performs that final step.

The server LRU cache defaults to 256 MiB; change it with goi-server --cache-mib.
Eviction removes reproducible clean blobs, preserving local edits, conflicts,
and unfinished publications. This limit is not a bound on the physical database
size or on all working data.

The kernel cache quota applies to file pages, not inode/dentry metadata or
temporary buffers. Dirty or open files are protected; allocation may return ENOSPC
when eligible pages cannot be released.

## Refresh, restore, and conflicts

```bash
goi refresh
goi restore README.md
goi restore src/
```

Refresh updates clean files and merges independent text edits. Locally changed
directory subtrees are preserved and reported. Ambiguous changes produce conflicts;
file/directory type collisions roll back the refresh. Restore discards selected
changes back to their recorded baseline, including deletions and renames.

Conflict markers are written to a separate copy. The working file is preserved
until resolution:

```bash
$EDITOR .goi/commit/changes/README.md
goi sync .goi/commit/changes/README.md
goi resolve README.md
# Reopen the working file before using it:
goi commit README.md -m "Resolve conflict"
```

Alternatively, goi resolve README.md --ours or --theirs selects a side.
Reopen files after refresh, restore, or resolve to obtain the new contents.

Publication intent is stored before changing the GitHub ref. On restart, the
next commit, or goi recover, the service checks whether that SHA was published
and can finish the update while preserving subsequent local edits.

```bash
goi recover
# Only after inspecting the outcome:
goi recover --forget
```

Forget removes publication intent without reverting working files.
Recovery examines up to 128 first-parent commits. An ambiguous deletion of the
last file is not retried automatically. Ref updates are not forced.

## Ignore rules and accounts

Create .goiignore at the branch root:

```text
build/
node_modules/
*.log
!important.log
```

Supported syntax: *, ?, character classes, # comments, ! negation, a trailing /
for directories, and a leading / to anchor a pattern. The last matching rule wins.
A star does not cross /; Git's ** and escaping syntax are not implemented.
Default rules cover *.swp, *.swx, *~, 4913, __pycache__/, and *.pyc.

Rules affect only unpublished additions in status/diff/commit. Tracked files,
their deletions, and their renames remain visible. Ignored files remain in the
filesystem and are still saved to the service.

```bash
goi status --ignored
goi status --json
goi commit --force build/result.txt -m "Publish artifact"
goi accounts
goi logout YOUR_LOGIN
goi auth ~/github
```

Logout synchronizes first and removes the stored GitHub token. It preserves
working data and activation state; it does not revoke the OAuth grant on GitHub
or block access to previously stored local data.

## Local write journal

The client journal is a separate local Python process even when the main service
is remote. By default, a successful write records a full-file snapshot with
SQLite FULL durability before returning to the application. Fsync sends the
snapshot to the main service and acknowledges the journal after success.

The journal holds up to 512 MiB of unacknowledged snapshots. Small edits to large
files are expensive because the whole file is recorded. Full capacity returns
ENOSPC. Disabling it with --no-journal leaves writes before fsync only in RAM.

```bash
goi journal status
goi journal export --output ~/goi-rescue
goi unmount ~/github
goi journal recover --mountpoint ~/github
# Only after preserving any needed copy:
goi journal discard --mountpoint ~/github --id 123
```

Export creates private files named by object ID and retains journal entries.
Replay checks the expected server revision; identical data and mode allow an
idempotent acknowledgement. A different newer revision returns ESTALE and retains
the snapshot for manual recovery. Completed renames or deletions may also need
manual inspection.

Journals live under ~/.config/goi/journals/ (or XDG_CONFIG_HOME). A lock prevents
duplicate daemons, and restarts reuse the previous port. Each journal is bound
to its backend address and token hash. Use a new mount configuration when changing
backends, or preserve and resolve pending data first. Journal storage must be on
a durable local disk. Metadata operations still require the main service.

## Unmounting

```bash
cd ~
goi unmount ~/github
sudo rmmod vtfs
```

Unmount synchronizes before invoking umount. Sync failure or open references can
prevent unmounting. Module unloading is separate and requires unused mounts.
Neither operation publishes a GitHub commit. The main service remains running.

## Tests

```bash
make test
make W=1
```

Python tests use temporary databases and a controlled GitHub fixture, without
GitHub credentials. CI runs these tests on Python 3.12 and 3.14.

For kernel tests in a dedicated compatible VM, start with vtfs unloaded:

```bash
sudo bash test/run-vm-tests.sh
sudo python3 -B test/test_remote.py
```

The ARM64 QEMU runner avoids loading the module into the host kernel:

```bash
python3 -B test/run_qemu.py --kernel /path/to/matching/uncompressed/Image \
  --qemu qemu-system-aarch64 --timeout 600 --crash-test --log /tmp/goi-qemu.log
```

It requires qemu-system-aarch64, busybox-static, cpio, Git, Python 3.14, and a
kernel matching the module. Pass an uncompressed ARM64 Image, not EFI/compressed
vmlinuz. Crash testing also requires mkfs.ext4 and ext4/virtio-blk support.
The runner terminates a VM before fsync, then recovers with the same virtual disk.

## Limits

One writer per working state. Store operations use a shared lock, including some
GitHub calls, so a slow network request can delay other operations.
Files are limited to 64 MiB; fsync and journaling allocate whole-file buffers.
Shared writable mmap is rejected in HTTP mode.

The RAM-only backend loses data on unmount and returns EOPNOTSUPP for fsync.
Hard links, symbolic links, issues/PR views, submodules, branch creation, and the
first commit of an empty repository are not implemented. Protected branches are
conservatively read-only. GitHub permissions are checked again at publication.
Full POSIX permissions and empty directories remain service metadata.
