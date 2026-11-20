# From mount to commit

This walkthrough follows a file from mounting through publication, with links to the implementation.

## 1. Build and load

Build the module against matching kernel headers. insmod loads its code and calls vtfs_init, which registers the vtfs filesystem type. It does not create a mount.

```bash
make W=1
sudo insmod source/vtfs.ko
```

Implementation: [source/vtfs.c](source/vtfs.c). Start at `vtfs_init`.

## 2. Start the service

The local Python server opens SQLite and exposes HTTP. The user systemd unit starts at login and restarts after failure. Mount can start it on demand and waits for authenticated readiness.

```bash
goi service install --client-id YOUR_CLIENT_ID
goi service status
```

Implementation: [goi/service.py](goi/service.py), [goi/server.py](goi/server.py), [goi/storage.py](goi/storage.py). Start at `ensure`.

## 3. Mount

The CLI prepares the mountpoint, main service and client journal, then invokes mount. VFS builds a filesystem context; get_tree calls fill_super. HTTP hello returns the root object metadata.

```bash
goi mount ~/github
```

Implementation: [goi/cli.py](goi/cli.py), [source/vtfs.c](source/vtfs.c), [source/remote.c](source/remote.c). Start at `main`.

## 4. Authenticate

Device Flow displays a code and a browser URL. After approval, the service stores the GitHub token and exposes owner directories. Before login, root listing is denied.

```bash
goi auth ~/github
```

Implementation: [goi/cli.py](goi/cli.py), [goi/server.py](goi/server.py), [goi/github.py](goi/github.py). Start at `device_start`.

## 5. Activate a repository

Explicit activation retrieves branches. Shell cd/ls wrappers also activate the single repository addressed by a path, before resolving nested branch directories. IDE lookups and owner listings do not trigger activation. Each branch is a directory. Other repositories remain inactive; directory discovery does not eagerly fetch every blob.

```bash
goi activate ~/github/YOUR_LOGIN/YOUR_REPO
```

Implementation: [goi/storage.py](goi/storage.py), [goi/server.py](goi/server.py). Start at `activate`.

## 6. Look up and list

VFS resolves path components using lookup. The server loads directory metadata when needed. Listing uses iterate rather than reading file contents.

```bash
cd ~/github/YOUR_LOGIN/YOUR_REPO/main
ls
```

Implementation: [source/remote.c](source/remote.c), [goi/storage.py](goi/storage.py). Start at `goi_lookup`.

## 7. Open and read

The module loads a file into page cache. The service fetches its GitHub blob if it is not cached. HTTP transfers data in bounded chunks; generic_file_read_iter returns bytes to the application.

```bash
cat README.md
```

Implementation: [source/remote.c](source/remote.c), [goi/storage.py](goi/storage.py), [goi/github.py](goi/github.py). Start at `goi_load`.

## 8. Write and journal

A write changes cached file contents. With journaling enabled, the full snapshot is committed to the local SQLite journal before write succeeds. This protects content before the main service has acknowledged fsync.

```bash
printf '\nHello from GOI\n' >> README.md
```

Implementation: [source/remote.c](source/remote.c), [goi/journal.py](goi/journal.py). Start at `goi_write_iter`.

## 9. Fsync and autosync

Fsync sends the complete file to the main service, waits for SQLite commit, and acknowledges the journal. Autosync schedules the same kind of saving through delayed work. Saving is separate from publication.

```bash
goi sync README.md
```

Implementation: [source/remote.c](source/remote.c), [goi/server.py](goi/server.py), [goi/storage.py](goi/storage.py). Start at `goi_fsync`.

## 10. Inspect changes

Status and diff compare service working state against the recorded baseline. The kernel dirty flag tracks unsaved bytes; service changes track unpublished edits. Ignore rules affect only new unpublished objects.

```bash
goi status
goi diff
```

Implementation: [goi/cli.py](goi/cli.py), [goi/publish.py](goi/publish.py), [goi/ignore.py](goi/ignore.py). Start at `status`.

## 11. Publish a commit

Commit synchronizes pending writes, checks the remote branch, creates Git objects, and updates its ref without force. Publication intent is persisted before updating the ref so an uncertain result can be recovered.

```bash
goi commit README.md -m "Update README"
```

Implementation: [goi/publish.py](goi/publish.py), [goi/github.py](goi/github.py), [goi/recovery.py](goi/recovery.py). Start at `commit`.

## 12. Resolve conflicts

Independent text edits can merge. Ambiguous changes create a separate conflict copy. Edit that copy and resolve it before reopening the working file and publishing. The working file is preserved until resolution.

```bash
$EDITOR .goi/commit/changes/README.md
goi sync .goi/commit/changes/README.md
goi resolve README.md
```

Implementation: [goi/merge.py](goi/merge.py), [goi/publish.py](goi/publish.py). Start at `resolve`.

## 13. Refresh and restore

Refresh updates clean files and merges changes while preserving locally changed directory subtrees. Restore discards selected changes back to the baseline. Reopen affected files after either operation.

```bash
goi refresh
goi restore README.md
```

Implementation: [goi/workspace.py](goi/workspace.py), [goi/storage.py](goi/storage.py). Start at `restore`.

## 14. Switch and recover

The shell cd wrapper synchronizes branch transitions and releases eligible caches. Open and dirty files remain protected. Journal replay checks revisions before restoring data; ambiguous snapshots can be exported.

```bash
goi switch main
goi journal status
goi journal export --output ~/goi-rescue
```

Implementation: [shell/goi.sh](shell/goi.sh), [goi/cli.py](goi/cli.py), [goi/journal.py](goi/journal.py). Start at `switch_branch`.

## 15. Unmount and unload

Unmount synchronizes before releasing the filesystem instance. Open references can prevent it. rmmod unloads the filesystem type only after mounts are gone. The main service and its SQLite data remain independent.

```bash
cd ~
goi unmount ~/github
sudo rmmod vtfs
```

Implementation: [goi/cli.py](goi/cli.py), [source/remote.c](source/remote.c), [source/vtfs.c](source/vtfs.c). Start at `vtfs_exit`.

## Glossary

**inode:** A kernel object describing a filesystem object: ID, type, permissions, timestamps, operations, and mapping to cached content. The filename is represented separately.

**dentry:** A directory-entry cache object connecting a name under a parent directory to an inode. Path lookup walks dentries.

**VFS:** Linux Virtual Filesystem: the common layer that routes ordinary file system calls to filesystem-specific operations.

**super_block:** The kernel object describing one mounted filesystem instance, including its root, operations, and private backend state.

**page cache:** Kernel memory pages containing file data. GOI loads remote bytes here and uses generic file helpers for reads and writes.

**SQLite:** The service database holding working state. WAL and synchronous=FULL provide durable commits, subject to the underlying storage guarantees.

**fsync:** A system call asking a filesystem to complete durable saving for a file. In GOI HTTP mode it saves to the main service; it does not publish to GitHub.

**journal:** A local SQLite store of full-file snapshots retained until the main service acknowledges them. It is distinct from the publication-intent journal.

**baseline:** The recorded version used to compare working files with their starting state and to perform three-way merges.

**ref:** A Git reference, such as a branch name, pointing to a commit. Publication advances the branch ref after constructing Git objects.

**revision:** A service-side change counter used for clean-cache invalidation and conditional journal recovery.

**workqueue:** Kernel infrastructure for running callbacks in worker threads. GOI delayed work may sleep while waiting for HTTP or a mutex; it is not an interrupt handler or a new goi process.
