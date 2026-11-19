import errno
import json
import os
from pathlib import Path
import stat
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from goi.protocol import Error, META, STATUS
from goi.server import Server
from goi.storage import Store
from goi import cli, publish, workspace, recovery, service
from goi.merge import merge
from fake_github import FakeGitHub


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.github = FakeGitHub()
        self.store = Store(self.temp.name, "test-client", self.github)
        self.addCleanup(lambda: self.store.close())
        self.account = self.store.add_account("test-token")
        self.repo = self.store.resolve_path("alice/demo")
        self.store.activate(self.repo["id"])
        self.branch = self.store.resolve_path("alice/demo/main")
        self.store.db.commit()

    def file(self, name="README.md"):
        return self.store.resolve_path("alice/demo/main/" + name)

    def write(self, node, data):
        result = self.store.write(node["id"], data, node["mode"])
        self.store.db.commit()  # The HTTP handler commits before acknowledging fsync.
        return result

    def test_ignore_only_untracked_files_and_force_commit(self):
        rules = self.store.create(self.branch["id"], ".goiignore", stat.S_IFREG | 0o644)
        self.write(rules, b"build/\n*.log\n!keep.log\nREADME.md\n")
        build = self.store.create(self.branch["id"], "build", stat.S_IFDIR | 0o755)
        hidden = self.store.create(build["id"], "output", stat.S_IFREG | 0o644)
        keep = self.store.create(self.branch["id"], "keep.log", stat.S_IFREG | 0o644)
        self.write(hidden, b"artifact")
        self.write(keep, b"kept")
        self.write(self.file(), b"tracked changes")
        shown = {item["path"] for item in publish.status(self.store, self.branch["id"])}
        self.assertIn("README.md", shown)
        self.assertIn("keep.log", shown)
        self.assertNotIn("build/output", shown)
        self.assertTrue(any(n["state"] == "ignored" for n in publish.status(self.store, self.branch["id"], True)))
        publish.commit(self.store, self.branch["id"], [], "ignore")
        self.assertTrue(self.store.node(hidden["id"])["dirty"])
        publish.commit(self.store, self.branch["id"], ["build/output"], "explicit", force=True)
        self.assertFalse(self.store.node(hidden["id"])["dirty"])

    def test_logout_keeps_workspace_and_reauthentication_reuses_account(self):
        self.write(self.file(), b"saved work")
        self.store.db.execute("UPDATE accounts SET token=NULL")
        with self.assertRaises(Error):
            self.store.github(self.account["id"])
        self.assertEqual(self.store.read(self.file()["id"], 0, 100), b"saved work")
        account = self.store.add_account("new-token")
        self.assertEqual(account["id"], self.account["id"])
        self.assertTrue(self.file()["dirty"])

    def journal_fixture(self):
        from goi.journal import Journal
        server = Server(("127.0.0.1", 0), self.store, "session")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        journal = Journal(Path(self.temp.name) / "journal", f"http://127.0.0.1:{server.server_port}", "session")
        self.addCleanup(journal.close)
        return journal

    def test_client_journal_replay_and_idempotence(self):
        journal = self.journal_fixture()
        node = self.file()
        journal.record(node["id"], node["revision"], 0o644, b"durable")
        journal.db.commit()
        self.assertEqual(journal.recover(), {"restored": 1})
        self.assertEqual(self.file()["data"], b"durable")
        revision = self.file()["revision"]
        # Simulate loss of the acknowledgement after the remote SQLite commit.
        journal.record(node["id"], node["revision"], 0o644, b"durable")
        journal.recover()
        self.assertEqual(self.file()["revision"], revision)

    def test_client_journal_rejects_changed_remote_and_retains_snapshot(self):
        journal = self.journal_fixture()
        node = self.file()
        journal.record(node["id"], node["revision"], 0o644, b"local")
        journal.db.commit()
        self.write(node, b"new remote")
        with self.assertRaises(Error) as error:
            journal.recover()
        self.assertEqual(error.exception.code, errno.ESTALE)
        self.assertEqual(journal.db.execute("SELECT data FROM writes").fetchone()[0], b"local")
        self.assertEqual(self.file()["data"], b"new remote")

    def test_client_journal_survives_restart_and_keeps_first_baseline(self):
        from goi.journal import Journal
        directory = Path(self.temp.name) / "journal"
        first = Journal(directory, "http://localhost:1234", "token")
        first.record(10, 3, 0o644, b"first")
        first.record(10, 5, 0o755, b"second")
        first.db.commit()
        first.close()
        second = Journal(directory, "http://localhost:1234", "token")
        try:
            row = second.db.execute("SELECT * FROM writes").fetchone()
            self.assertEqual((row["revision"], row["data"], row["mode"]), (3, b"second", 0o755))
        finally:
            second.close()
        with self.assertRaises(Error):
            Journal(directory, "http://other:1234", "token")

    def test_cache_evicts_oldest_and_redownloads_without_changing_metadata(self):
        first, second = self.file(), self.file("src/main.c")
        self.store.read(first["id"], 0, 100)
        data = self.store.read(second["id"], 0, 100)
        self.store.cache_bytes = len(data)
        result = self.store.trim_cache()
        self.assertEqual(result["cached_bytes"], len(data))
        self.assertIsNone(self.store.node(first["id"])["data"])
        self.assertEqual(self.store.node(first["id"])["revision"], first["revision"])
        self.assertEqual(self.store.read(first["id"], 0, 100), b"hello\n")
        self.assertIsNone(self.store.node(second["id"])["data"])

    def test_cache_clear_preserves_edits_additions_and_conflicts(self):
        node = self.write(self.file(), b"local")
        added = self.store.create(self.branch["id"], "new", stat.S_IFREG | 0o644)
        self.write(added, b"new data")
        self.github.external_edit("README.md", b"remote")
        self.store.refresh(self.branch["id"])
        conflict = self.store.db.execute("SELECT mirror FROM conflicts").fetchone()[0]
        before = self.store.read(conflict, 0, 10000)
        self.store.trim_cache(0)
        self.assertEqual(self.store.read(node["id"], 0, 100), b"local")
        self.assertEqual(self.store.read(added["id"], 0, 100), b"new data")
        self.assertEqual(self.store.read(conflict, 0, 10000), before)

    def test_cache_preserves_pending_publication_branch(self):
        node = self.file()
        self.store.read(node["id"], 0, 100)
        self.store.db.execute("INSERT INTO publications VALUES (?,?,?,?,?)",
                              (self.branch["id"], "base", "target", "[]", "pending"))
        self.store.trim_cache(0)
        self.assertEqual(self.store.node(node["id"])["data"], b"hello\n")

    def test_disabled_cache_returns_content_without_retaining_blob(self):
        self.store.cache_bytes = 0
        node = self.file()
        self.assertEqual(self.store.read(node["id"], 1, 3), b"ell")
        self.assertIsNone(self.store.node(node["id"])["data"])
        self.assertEqual(self.store.read(node["id"], 4, 2), b"o\n")

    def test_cache_cli_over_http(self):
        self.store.read(self.file()["id"], 0, 100)
        self.store.db.commit()
        server = Server(("127.0.0.1", 0), self.store, "session")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        token = Path(self.temp.name) / "token"
        token.write_text("session")
        token.chmod(0o600)
        client = cli.Client(Path(self.temp.name), {
            "server": f"http://127.0.0.1:{server.server_port}", "token_file": str(token)})
        self.assertEqual(client.call("cache")["cached_bytes"], 6)
        with patch.object(cli, "connection", return_value=client), patch("builtins.print") as output:
            self.assertEqual(cli.main(["cache", "--clear"]), 0)
            self.assertIn("freed 6 bytes", output.call_args.args[0])
        self.assertIsNone(self.file()["data"])

    def test_cache_configuration_and_existing_database(self):
        self.store.read(self.file()["id"], 0, 100)
        self.store.db.commit()
        reopened = Store(self.temp.name, github_factory=self.github, cache_bytes=0)
        try:
            reopened.trim_cache()
            self.assertIsNone(reopened.node(self.file()["id"])["data"])
        finally:
            reopened.close()
        self.assertTrue(cli.parser().parse_args(["cache", "--clear"]).clear)

    def test_three_way_merge_handles_independent_and_overlapping_edits(self):
        base = b"one\ntwo\nthree\n"
        self.assertEqual(merge(base, b"ONE\ntwo\nthree\n", b"one\ntwo\nTHREE\n"),
                         b"ONE\ntwo\nTHREE\n")
        self.assertIsNone(merge(base, b"ONE\ntwo\nthree\n", b"other\ntwo\nthree\n"))
        self.assertIsNone(merge(b"a\0", b"b\0", b"c\0"))
        self.assertEqual(merge(b"a\nb\n", b"a\n", b"A\nb\n"), b"A\n")
        self.assertIsNone(merge(b"a\n", b"first\na\n", b"second\na\n"))

    def test_commit_merges_independent_remote_edit(self):
        self.github.external_edit("README.md", b"one\ntwo\nthree\n")
        self.store.refresh(self.branch["id"])
        self.write(self.file(), b"ONE\ntwo\nthree\n")
        self.github.external_edit("README.md", b"one\ntwo\nTHREE\n")
        result = publish.commit(self.store, self.branch["id"], [], "merge")
        tree = self.github.full_tree("alice", "demo", self.github.commits[result["sha"]]["tree"]["sha"])
        self.assertEqual(self.github.blobs[tree["README.md"]["sha"]], b"ONE\ntwo\nTHREE\n")

    def test_refresh_preserves_edits_and_updates_clean_file(self):
        self.write(self.file(), b"local edit")
        self.github.external_edit("src/main.c", b"remote edit")
        self.store.refresh(self.branch["id"])
        self.assertEqual(self.store.read(self.file()["id"], 0, 100), b"local edit")
        self.assertTrue(self.file()["dirty"])
        self.assertEqual(self.store.read(self.file("src/main.c")["id"], 0, 100), b"remote edit")

    def test_refresh_conflict_preserves_working_file(self):
        node = self.write(self.file(), b"local edit")
        self.github.external_edit("README.md", b"remote edit")
        result = self.store.refresh(self.branch["id"])
        self.assertEqual(result["conflicts"], ["README.md"])
        self.assertEqual(self.store.read(node["id"], 0, 100), b"local edit")
        publish.resolve(self.store, node["id"], "theirs")
        self.assertEqual(self.store.read(node["id"], 0, 100), b"remote edit")

    def test_restore_modified_deleted_and_renamed_file(self):
        original = self.file()
        self.write(original, b"modified")
        workspace.restore(self.store, self.branch["id"], ["README.md"])
        self.assertEqual(self.store.read(original["id"], 0, 100), b"hello\n")
        self.store.remove(self.branch["id"], "README.md")
        workspace.restore(self.store, self.branch["id"], ["README.md"])
        self.assertEqual(self.file()["id"], original["id"])
        self.store.rename(self.branch["id"], "README.md", self.branch["id"], "renamed")
        workspace.restore(self.store, self.branch["id"], ["renamed"])
        self.assertEqual(self.file()["id"], original["id"])
        self.assertIsNone(self.store.child(self.branch["id"], "renamed"))

    def test_restore_new_and_moved_directory(self):
        folder = self.store.create(self.branch["id"], "local", stat.S_IFDIR | 0o755)
        self.store.create(folder["id"], "file", stat.S_IFREG | 0o644)
        workspace.restore(self.store, self.branch["id"], ["local"])
        self.assertIsNone(self.store.child(self.branch["id"], "local"))
        self.store.rename(self.branch["id"], "src", self.branch["id"], "moved")
        workspace.restore(self.store, self.branch["id"], ["moved"])
        self.assertEqual(self.store.read(self.file("src/main.c")["id"], 0, 100), b"int main(void) { return 0; }\n")

    def test_restore_conflict_and_keep_unselected_changes(self):
        node = self.write(self.file(), b"ours")
        self.github.external_edit("README.md", b"theirs")
        with self.assertRaises(Error):
            publish.commit(self.store, self.branch["id"], [], "conflict")
        self.write(self.file("src/main.c"), b"keep")
        workspace.restore(self.store, self.branch["id"], ["README.md"])
        self.assertEqual(self.store.read(node["id"], 0, 100), b"hello\n")
        self.assertFalse(self.store.db.execute("SELECT 1 FROM conflicts").fetchone())
        self.assertEqual(self.file("src/main.c")["data"], b"keep")

    def test_recovery_before_ref_update_survives_restart(self):
        self.write(self.file(), b"publish")
        with patch.object(self.github, "update_head", side_effect=Error(errno.EIO, "offline")):
            with self.assertRaises(Error):
                publish.commit(self.store, self.branch["id"], [], "once")
        count = len(self.github.commits)
        self.store.close()
        self.store = Store(self.temp.name, "test-client", self.github)
        self.assertTrue(recovery.recover(self.store, self.branch["id"])["recovered"])
        self.assertEqual(len(self.github.commits), count)
        self.assertEqual(publish.status(self.store, self.branch["id"]), [])

    def test_recovery_keeps_edits_after_published_head_advanced(self):
        self.write(self.file(), b"published")
        self.github.fail_update_after_success = True
        with self.assertRaises(Error):
            publish.commit(self.store, self.branch["id"], [], "once")
        self.write(self.file(), b"next local edit")
        self.github.external_edit("other.txt", b"other contributor")
        recovery.recover(self.store, self.branch["id"])
        self.assertEqual(self.file()["data"], b"next local edit")
        self.assertTrue(self.file()["dirty"])

    def test_recovery_preserves_later_permission_change(self):
        node = self.write(self.file(), b"published")
        self.github.fail_update_after_success = True
        with self.assertRaises(Error):
            publish.commit(self.store, self.branch["id"], [], "once")
        self.store.setattr(node["id"], mode=0o755)
        recovery.recover(self.store, self.branch["id"])
        self.assertTrue(self.file()["dirty"])
        self.assertEqual(self.file()["mode"] & 0o777, 0o755)

    def test_refresh_merges_independent_lines(self):
        self.github.external_edit("README.md", b"one\ntwo\nthree\n")
        self.store.refresh(self.branch["id"])
        self.write(self.file(), b"ONE\ntwo\nthree\n")
        self.github.external_edit("README.md", b"one\ntwo\nTHREE\n")
        result = self.store.refresh(self.branch["id"])
        self.assertEqual(result["conflicts"], [])
        self.assertEqual(self.file()["data"], b"ONE\ntwo\nTHREE\n")
        self.assertTrue(self.file()["dirty"])

    def test_restore_collision_rolls_back_without_losing_data(self):
        self.store.rename(self.branch["id"], "src", self.branch["id"], "moved")
        occupied = self.store.create(self.branch["id"], "src", stat.S_IFREG | 0o644)
        self.write(occupied, b"keep")
        self.store.db.commit()
        with self.assertRaises(Error):
            with self.store.db:
                workspace.restore(self.store, self.branch["id"], ["moved"])
        self.assertEqual(self.file("src")["data"], b"keep")
        self.assertIsNotNone(self.file("moved/main.c"))

    def test_activation_and_listing_do_not_download_files(self):
        self.store.list(self.branch["id"])
        self.file("src/main.c")
        self.assertEqual(self.github.blob_reads, 0)
        self.assertEqual(self.store.read(self.file()["id"], 0, 6), b"hello\n")
        self.assertEqual(self.github.blob_reads, 1)

    def test_owners_and_organization_permissions(self):
        self.assertEqual([n["name"] for n in self.store.list(1)], ["alice", "example-org"])
        self.assertEqual([n["name"] for n in self.store.list(self.account["id"])], ["demo"])
        self.github.writable = False
        repo = self.store.resolve_path("example-org/demo")
        self.store.activate(repo["id"])
        branch = self.store.resolve_path("example-org/demo/main")
        file = self.store.resolve_path("example-org/demo/main/README.md")
        self.assertEqual(self.store.read(file["id"], 0, 6), b"hello\n")
        self.assertFalse(file["mode"] & 0o222)
        with self.assertRaises(Error) as caught:
            self.store.create(branch["id"], "new", stat.S_IFREG | 0o644)
        self.assertEqual(caught.exception.code, errno.EACCES)
        client = type("Mount", (), {"mountpoint": Path("/github")})()
        for owner in ("alice", "example-org"):
            relative, root = cli.branch_context(client, "/github/" + owner + "/demo/main/src")
            self.assertEqual(relative, owner + "/demo/main")
            self.assertEqual(root, Path("/github") / relative)

    def test_refresh_discovers_repositories_without_losing_changes(self):
        file = self.file()
        self.write(file, b"keep this edit")
        org = self.store.resolve_path("example-org")
        self.store.list(org["id"])
        with patch.object(self.github, "repositories", side_effect=lambda owner, personal=False: [
                self.github.repository(owner, "demo"), self.github.repository(owner, "new-repo")]):
            for owner in ("alice", "example-org"):
                self.store.refresh_owners(owner)
                self.assertEqual(self.store.resolve_path(owner + "/new-repo")["kind"], "repo")
                self.store.refresh_owners(owner)  # Repeated refresh must not duplicate nodes.
        with patch.object(self.github, "organizations", return_value=[{"login": "new-org"}]):
            self.store.refresh_owners("")
            self.assertEqual(self.store.resolve_path("new-org")["kind"], "org")
        self.assertEqual(self.file()["id"], file["id"])
        self.assertEqual(self.file()["data"], b"keep this edit")
        self.assertTrue(self.file()["dirty"])

    def test_refresh_preserves_local_directory_changes(self):
        for operation in ("mkdir", "rename", "chmod", "rmdir"):
            with self.subTest(operation=operation):
                # Isolate each mutation, including persisted metadata and head.
                self.store.db.execute("SAVEPOINT refresh_case")
                directory = self.store.create(self.branch["id"], "empty", stat.S_IFDIR | 0o755)
                if operation != "mkdir":
                    self.store.update(directory["id"], dirty=0, base_path="empty")
                if operation == "rename":
                    self.store.rename(self.branch["id"], "empty", self.branch["id"], "moved")
                elif operation == "chmod":
                    self.store.setattr(directory["id"], mode=0o700)
                elif operation == "rmdir":
                    self.store.remove(self.branch["id"], "empty", directory=True)
                before = dict(self.store.node(directory["id"], deleted=True))
                self.store.refresh(self.branch["id"])
                after = dict(self.store.node(directory["id"], deleted=True))
                for key in ("name", "parent", "mode", "deleted", "dirty"):
                    self.assertEqual(before[key], after[key])
                self.store.db.execute("ROLLBACK TO refresh_case")
                self.store.db.execute("RELEASE refresh_case")

    def test_refresh_after_cancelled_directory_creation(self):
        directory = self.store.create(self.branch["id"], "temporary", stat.S_IFDIR | 0o755)
        self.store.remove(self.branch["id"], "temporary", directory=True)
        self.store.refresh(self.branch["id"])
        self.assertIsNone(self.store.child(self.branch["id"], "temporary"))
        self.assertEqual(self.store.read(self.file()["id"], 0, 6), b"hello\n")

    def test_cli_refresh_owner_and_root(self):
        root = Path(self.temp.name) / "mount"
        owner = root / "alice"
        owner.mkdir(parents=True)
        client = type("Mount", (), {
            "mountpoint": root,
            "relative": lambda _, path: str(path.relative_to(root)),
            "call": lambda _, operation, **values: (
                self.assertEqual(operation, "refresh_owners"),
                self.store.refresh_owners(values["path"]))[1],
        })()
        for directory in (root, owner):
            with patch.object(cli, "connection", return_value=client), \
                    patch.object(Path, "cwd", return_value=directory), patch("builtins.print"):
                self.assertEqual(cli.main(["refresh"]), 0)

    def test_old_layout_migration_preserves_dirty_files(self):
        file = self.file()
        self.write(file, b"local change")
        org_repo = self.store.resolve_path("example-org/demo")
        personal = self.store.new_node(self.account["id"], "personal", "personal",
                                       stat.S_IFDIR | 0o755, loaded=1)
        orgs = self.store.new_node(self.account["id"], "orgs", "orgs",
                                   stat.S_IFDIR | 0o755, loaded=1)
        org = self.store.child(1, "example-org")
        self.store.update(self.repo["id"], parent=personal["id"])
        self.store.update(org["id"], parent=orgs["id"])
        self.store.db.commit()
        self.store.close()
        self.store = Store(self.temp.name, "test-client", self.github)
        migrated = self.file()
        self.assertEqual(migrated["id"], file["id"])
        self.assertEqual(migrated["data"], b"local change")
        self.assertTrue(migrated["dirty"])
        self.assertEqual(self.store.resolve_path("example-org/demo")["id"], org_repo["id"])
        self.assertIsNone(self.store.child(self.account["id"], "personal"))
        self.assertIsNone(self.store.child(self.account["id"], "orgs"))
        self.assertEqual([n["name"] for n in self.store.list(1)], ["alice", "example-org"])

    def test_branch_container_migration_preserves_edits(self):
        file = self.file()
        self.write(file, b"saved edit")
        container = self.store.new_node(self.repo["id"], "branches", "branches",
                                         stat.S_IFDIR | 0o755, loaded=1)
        for node in self.store.children(self.repo["id"]):
            if node["kind"] == "branch":
                self.store.update(node["id"], parent=container["id"])
        # A branch called branches must also survive flattening.
        self.store.update(self.branch["id"], name="branches")
        self.store.db.commit()
        self.store.close()
        self.store = Store(self.temp.name, "test-client", self.github)
        migrated = self.store.resolve_path("alice/demo/branches/README.md")
        self.assertEqual(migrated["id"], file["id"])
        self.assertEqual(migrated["data"], b"saved edit")
        self.assertTrue(migrated["dirty"])
        self.assertEqual(self.store.child(self.repo["id"], "branches")["kind"], "branch")
        self.assertIsNone(self.store.db.execute(
            "SELECT 1 FROM nodes WHERE kind='branches'").fetchone())

    def test_empty_repository_activation_can_be_retried(self):
        repo = self.store.resolve_path("example-org/demo")
        with patch.object(self.github, "branches", return_value=[]):
            with self.assertRaisesRegex(Error, "repository has no branches"):
                self.store.activate(repo["id"])
        self.assertFalse(self.store.repo(repo["id"])["active"])
        self.assertIsNone(self.store.child(repo["id"], "branches"))
        self.store.activate(repo["id"])
        self.assertEqual(self.store.resolve_path("example-org/demo/main")["kind"], "branch")

    def test_retry_repository_activated_empty_by_old_version(self):
        repo = self.store.resolve_path("example-org/demo")
        parent = self.store.new_node(repo["id"], "branches", "branches",
                                     stat.S_IFDIR | 0o755, loaded=1)
        self.store.db.execute("UPDATE repos SET active=1 WHERE id=?", (repo["id"],))
        self.store.db.commit()
        self.store.close()
        self.store = Store(self.temp.name, "test-client", self.github)
        self.store.activate(repo["id"])
        self.assertIsNone(self.store.child(repo["id"], "branches"))
        self.assertEqual(self.store.resolve_path("example-org/demo/main")["kind"], "branch")

    def test_user_service_install_preserves_settings_and_quotes_paths(self):
        root = Path(self.temp.name) / 'user with % and $'
        with patch.dict(os.environ, {"HOME": str(root), "XDG_CONFIG_HOME": str(root / "config")}), \
             patch.object(service, "systemctl") as ctl:
            path = service.install("app-id")
            text = path.read_text()
            self.assertIn('Restart=on-failure', text)
            self.assertIn('WantedBy=default.target', text)
            self.assertIn('127.0.0.1', text)
            self.assertIn('%% and $$', text)
            self.assertIn('"app-id"', text)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            service.install()
            self.assertEqual(path.read_text(), text)
            self.assertEqual(ctl.call_count, 4)
            service.install("updated-app")
            self.assertIn('"updated-app"', path.read_text())
        with self.assertRaises(Error):
            service.argument("bad\nargument")

    def test_user_service_start_waits_for_readiness_and_reuses_running_server(self):
        with patch.object(service, "ready", side_effect=[False, False, True]), \
             patch.object(service.socket, "create_connection", side_effect=ConnectionRefusedError), \
             patch.object(service, "install") as install, patch.object(service, "systemctl") as ctl, \
             patch.object(service, "unit_path", return_value=Path(self.temp.name) / "missing.service"), \
             patch.object(service.time, "sleep"):
            service.ensure()
            install.assert_called_once()
            ctl.assert_called_once_with("start", "goi.service")
        with patch.object(service, "ready", return_value=True), patch.object(service, "systemctl") as ctl:
            service.ensure()
            ctl.assert_not_called()

    def test_user_service_rejects_occupied_port_without_starting_second_server(self):
        from unittest.mock import MagicMock
        with patch.object(service, "ready", return_value=False), \
             patch.object(service.socket, "create_connection", return_value=MagicMock()), \
             patch.object(service, "systemctl") as ctl:
            with self.assertRaises(Error) as error:
                service.ensure()
            self.assertEqual(error.exception.code, errno.EADDRINUSE)
            ctl.assert_not_called()

    def test_user_service_health_checks_authenticated_protocol(self):
        server = Server(("127.0.0.1", 0), self.store, "session")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        token = Path(self.temp.name) / "server-token"
        token.write_text("session")
        with patch.object(service, "data_directory", return_value=Path(self.temp.name)), \
             patch.object(service, "URL", f"http://127.0.0.1:{server.server_port}"):
            self.assertTrue(service.ready())
            token.write_text("wrong")
            self.assertFalse(service.ready())

    def test_cli_mount_defaults_saved_settings_and_overrides(self):
        root = Path(self.temp.name)
        token = root / ".local/share/goi/server-token"
        token.parent.mkdir(parents=True)
        token.write_text("a" * 64)
        token.chmod(0o600)
        custom_token = root / "custom-token"
        custom_token.write_text("b" * 64)
        custom_token.chmod(0o600)
        mount = str(root / "mount")
        self.assertEqual(cli.parser().parse_args(["mount", mount]).mountpoint, Path(mount))
        with patch.dict(os.environ, {"HOME": str(root), "XDG_CONFIG_HOME": str(root / "config")}), \
             patch.object(cli.subprocess, "run") as run, patch("builtins.print"), \
             patch.object(service, "ensure") as ensure_service, \
             patch("goi.journal.ensure", return_value=(Mock(call=Mock(return_value={"restored": 0})), 12345)):
            self.assertEqual(cli.main(["mount", mount]), 0)
            self.assertTrue(Path(mount).is_dir())
            self.assertIn("port=8080", run.call_args.args[0][-1])
            self.assertIn("autosync=1", run.call_args.args[0][-1])
            self.assertEqual(cli.main(["mount", mount, "--server", "http://127.0.0.1:9999",
                                       "--token-file", str(custom_token), "--autosync", "0"]), 0)
            self.assertEqual(cli.main(["mount", mount]), 0)
            self.assertIn("port=9999", run.call_args.args[0][-1])
            self.assertIn("token=" + "b" * 64, run.call_args.args[0][-1])
            self.assertIn("autosync=0", run.call_args.args[0][-1])
            self.assertEqual(cli.main(["mount", mount, "--server", "http://127.0.0.1:8888",
                                       "--autosync", "3"]), 0)
            self.assertIn("port=8888", run.call_args.args[0][-1])
            self.assertIn("autosync=3", run.call_args.args[0][-1])
            saved = cli.configurations()[mount]
            self.assertEqual(saved["server"], "http://127.0.0.1:8888")
            self.assertEqual(saved["token_file"], str(custom_token))
            self.assertEqual(saved["autosync"], 3)
            self.assertFalse(saved["managed_local"])
            ensure_service.assert_called_once()

    def test_binary_save_and_restart(self):
        node = self.file("binary")
        payload = bytes(range(256)) * 500
        updated = self.write(node, payload)
        self.store.db.commit()
        self.store.close()
        self.store = Store(self.temp.name, "test-client", self.github)
        self.assertEqual(self.store.read(updated["id"], 0, len(payload)), payload)

    def test_incomplete_http_write_is_not_visible(self):
        node = self.file()
        server = Server(("127.0.0.1", 0), self.store, "session")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with socket.create_connection(server.server_address) as client:
            request = (f"POST /api/write?id={node['id']}&mode=420 HTTP/1.1\r\n"
                       "Host: localhost\r\nAuthorization: Bearer session\r\n"
                       "Content-Length: 6\r\nConnection: close\r\n\r\n").encode()
            client.sendall(request + b"new")  # Declared six bytes, delivered three.
            client.shutdown(socket.SHUT_WR)
            response = bytearray()
            while data := client.recv(4096):
                response.extend(data)
        body = response.split(b"\r\n\r\n", 1)[1]
        self.assertEqual(STATUS.unpack_from(body)[0], -errno.EPROTO)
        self.assertEqual(self.store.read(node["id"], 0, 100), b"hello\n")

    def test_later_whole_file_save_replaces_previous_save(self):
        node = self.file()
        self.write(node, b"first complete file")
        self.write(node, b"x")
        self.assertEqual(self.store.read(node["id"], 0, 100), b"x")

    def test_repeating_fsync_keeps_the_same_bytes(self):
        node = self.write(self.file(), b"saved bytes")
        self.write(node, b"saved bytes")
        self.assertEqual(self.store.read(node["id"], 0, 100), b"saved bytes")

    def test_multiple_files_one_commit(self):
        first, second = self.file(), self.file("src/main.c")
        self.write(first, b"new readme")
        self.write(second, b"new code")
        before = len(self.github.commits)
        result = publish.commit(self.store, self.branch['id'], ['README.md', 'src/main.c'], 'both')
        self.assertEqual(len(self.github.commits), before + 1)
        self.assertEqual(self.github.heads["main"], result["sha"])
        self.assertEqual(publish.status(self.store, self.branch['id']), [])

    def test_unselected_file_stays_dirty(self):
        self.write(self.file(), b"one")
        self.write(self.file("src/main.c"), b"two")
        publish.commit(self.store, self.branch['id'], ['README.md'], 'one')
        self.assertEqual([s["path"] for s in publish.status(self.store, self.branch['id'])], ["src/main.c"])

    def test_delete_last_file_and_add_again(self):
        self.file("src/main.c")
        self.file("binary")
        self.store.remove(self.branch["id"], "binary")
        src = self.store.resolve_path("alice/demo/main/src")
        self.store.remove(src["id"], "main.c")
        publish.commit(self.store, self.branch["id"], [], "Keep only README")
        self.store.remove(self.branch["id"], "README.md")
        with patch.object(self.github, "delete_file", wraps=self.github.delete_file) as delete:
            result = publish.commit(self.store, self.branch["id"], [], "Delete last file")
            delete.assert_called_once()
        tree = self.github.commits[result["sha"]]["tree"]["sha"]
        self.assertEqual(self.github.full_tree("alice", "demo", tree), {})
        self.assertEqual(publish.status(self.store, self.branch["id"]), [])
        self.assertIsNone(publish.commit(self.store, self.branch["id"], [], "Retry")["sha"])
        file = self.store.create(self.branch["id"], "new.txt", stat.S_IFREG | 0o644)
        self.write(file, b"new content")
        publish.commit(self.store, self.branch["id"], [], "Add after empty commit")

    def test_noop_does_not_create_commit(self):
        self.write(self.file(), b"hello\n")
        before = len(self.github.commits)
        publish.commit(self.store, self.branch['id'], [], 'noop')
        self.assertEqual(len(self.github.commits), before)

    def test_lost_push_reply_does_not_duplicate_commit(self):
        self.write(self.file(), b"publish once")
        self.github.fail_update_after_success = True
        before = len(self.github.commits)
        with self.assertRaises(Error):
            publish.commit(self.store, self.branch['id'], [], 'once')
        self.store.close()
        self.store = Store(self.temp.name, "test-client", self.github)
        result = publish.commit(self.store, self.branch['id'], [], 'once')
        self.assertEqual(len(self.github.commits), before + 1)
        self.assertEqual(result["sha"], self.github.heads["main"])
        self.assertEqual(publish.status(self.store, self.branch['id']), [])

    def test_conflict_preserves_original_until_resolve(self):
        node = self.write(self.file(), b"local\n")
        self.github.external_edit("README.md", b"remote\n")
        with self.assertRaises(Error) as error:
            publish.commit(self.store, self.branch['id'], [], 'conflict')
        self.assertEqual(error.exception.code, errno.EAGAIN)
        self.assertEqual(self.store.read(node["id"], 0, 100), b"local\n")
        row = self.store.db.execute("SELECT * FROM conflicts WHERE node=?", (node["id"],)).fetchone()
        mirror = self.store.node(row["mirror"])
        self.assertIn(b"<<<<<<< local", mirror["data"])
        with self.assertRaises(Error):
            publish.resolve(self.store, node['id'])
        self.write(mirror, b"resolved\n")
        publish.resolve(self.store, node['id'])
        self.assertEqual(self.store.read(node["id"], 0, 100), b"resolved\n")
        publish.commit(self.store, self.branch['id'], [], 'resolved')

    def test_rename_and_commit(self):
        node = self.file()
        src = self.file("src")
        self.store.rename(self.branch["id"], "README.md", src["id"], "renamed.md")
        result = publish.commit(self.store, self.branch['id'], ['src/renamed.md'], 'move')
        tree = self.github.full_tree("alice", "demo", self.github.commits[result["sha"]]["tree"]["sha"])
        self.assertNotIn("README.md", tree)
        self.assertIn("src/renamed.md", tree)
        self.assertEqual(self.store.read(node["id"], 0, 100), b"hello\n")

    def test_readonly_permissions_checked_at_publish(self):
        self.write(self.file(), b"local")
        self.github.writable = False
        with self.assertRaises(Error) as error:
            publish.commit(self.store, self.branch['id'], [], 'denied')
        self.assertEqual(error.exception.code, errno.EACCES)

    def test_editor_replacement_includes_destination_deletion(self):
        temporary = self.store.create(self.branch["id"], ".save", stat.S_IFREG | 0o600)
        self.write(temporary, b"atomic save")
        self.store.rename(self.branch["id"], ".save", self.branch["id"], "README.md")
        result = publish.commit(self.store, self.branch['id'], ['README.md'], 'save')
        tree = self.github.full_tree("alice", "demo", self.github.commits[result["sha"]]["tree"]["sha"])
        self.assertEqual(self.github.blobs[tree["README.md"]["sha"]], b"atomic save")
        self.assertNotIn(".save", tree)

    def vim_save(self):
        self.file()
        self.store.rename(self.branch["id"], "README.md", self.branch["id"], "README.md~")
        replacement = self.store.create(self.branch["id"], "README.md", stat.S_IFREG | 0o644)
        self.write(replacement, b"hello\nVim edit\n")
        self.store.remove(self.branch["id"], "README.md~")
        for name in (".README.md.swp", ".README.md.swx", "4913"):
            self.store.create(self.branch["id"], name, stat.S_IFREG | 0o600)
            self.store.remove(self.branch["id"], name)
        return replacement

    def test_vim_backup_save_is_one_modification(self):
        replacement = self.vim_save()
        self.assertEqual(publish.status(self.store, self.branch["id"]), [{
            "id": replacement["id"], "path": "README.md",
            "old_path": "README.md", "state": "modified"}])
        diff = publish.diff(self.store, self.branch["id"])
        self.assertNotIn("README.md~", diff)
        self.assertIn("+Vim edit", diff)
        publish.commit(self.store, self.branch["id"], ["README.md"], "Vim save")
        self.assertEqual(publish.status(self.store, self.branch["id"]), [])

    def test_vim_backup_save_still_detects_real_remote_conflict(self):
        replacement = self.vim_save()
        self.github.external_edit("README.md", b"remote edit")
        with self.assertRaises(Error):
            publish.commit(self.store, self.branch["id"], [], "Vim save")
        self.assertEqual([row[0] for row in self.store.db.execute("SELECT node FROM conflicts")],
                         [replacement["id"]])
        publish.resolve(self.store, replacement["id"], "ours")
        publish.commit(self.store, self.branch["id"], ["README.md"], "Resolve")
        self.assertEqual(publish.status(self.store, self.branch["id"]), [])

    def test_existing_false_vim_conflict_can_be_resolved(self):
        replacement = self.vim_save()
        original = self.store.db.execute(
            "SELECT * FROM nodes WHERE branch=? AND base_path='README.md'",
            (self.branch["id"],)).fetchone()
        publish.make_conflict(self.store, self.store.node(replacement["id"]),
                              b"hello\n", original["remote_sha"],
                              self.github.heads["main"], original["base_mode"])
        publish.status(self.store, self.branch["id"])
        publish.resolve(self.store, replacement["id"], "ours")
        publish.commit(self.store, self.branch["id"], ["README.md"], "Resolve old false conflict")
        self.assertEqual(publish.status(self.store, self.branch["id"]), [])

    def test_remote_mode_change_conflicts(self):
        node = self.write(self.file(), b"new text")
        parent = self.github.heads["main"]
        base = self.github.commits[parent]["tree"]["sha"]
        sha = self.file()["remote_sha"]
        tree = self.github.create_tree("alice", "demo", base,
                                       [{"path": "README.md", "mode": "100755", "type": "blob", "sha": sha}])
        self.github.heads["main"] = self.github.create_commit("alice", "demo", parent, tree, "chmod", {})
        with self.assertRaises(Error):
            publish.commit(self.store, self.branch['id'], [], 'local')
        publish.resolve(self.store, node['id'], 'theirs')
        self.assertTrue(self.store.node(node["id"])["mode"] & 0o111)

    def test_editor_replacement_remote_conflict_resolves_once(self):
        temporary = self.store.create(self.branch["id"], ".save", stat.S_IFREG | 0o600)
        self.write(temporary, b"editor save")
        replacement = self.store.rename(self.branch["id"], ".save", self.branch["id"], "README.md")
        self.github.external_edit("README.md", b"remote changed")
        with self.assertRaises(Error):
            publish.commit(self.store, self.branch['id'], ['README.md'], 'save')
        conflicts = list(self.store.db.execute("SELECT node FROM conflicts"))
        self.assertEqual([row[0] for row in conflicts], [replacement["id"]])
        publish.resolve(self.store, replacement['id'], 'ours')
        publish.commit(self.store, self.branch['id'], ['README.md'], 'resolve')
        self.assertEqual(publish.status(self.store, self.branch['id']), [])

    def test_remote_advance_allows_manual_retry_without_force(self):
        self.write(self.file(), b"local")
        original = self.github.update_head
        def race(owner, repo, branch, sha):
            self.github.external_edit("other.txt", b"external")
            raise Error(errno.EAGAIN, "branch advanced")
        self.github.update_head = race
        with self.assertRaises(Error):
            publish.commit(self.store, self.branch["id"], [], "local")
        self.github.update_head = original
        result = publish.commit(self.store, self.branch["id"], [], "retry")
        tree = self.github.full_tree("alice", "demo", self.github.commits[result["sha"]]["tree"]["sha"])
        self.assertIn("other.txt", tree)
        self.assertEqual(self.github.blobs[tree["README.md"]["sha"]], b"local")

    def test_new_edit_after_lost_reply_is_preserved(self):
        self.write(self.file(), b"first published version")
        self.github.fail_update_after_success = True
        with self.assertRaises(Error):
            publish.commit(self.store, self.branch["id"], [], "first")
        self.assertTrue(publish.status(self.store, self.branch["id"]))
        self.write(self.file(), b"second published version")
        result = publish.commit(self.store, self.branch["id"], [], "second")
        tree = self.github.full_tree("alice", "demo", self.github.commits[result["sha"]]["tree"]["sha"])
        self.assertEqual(self.github.blobs[tree["README.md"]["sha"]], b"second published version")
        self.assertEqual(publish.status(self.store, self.branch["id"]), [])

    def test_reserved_names_and_cross_branch_rename(self):
        with self.assertRaises(Error):
            self.store.create(self.branch["id"], ".git", stat.S_IFDIR | 0o755)
        other = self.store.resolve_path("alice/demo/feature%2Fvfs")
        with self.assertRaises(Error) as error:
            self.store.rename(self.branch["id"], "README.md", other["id"], "moved")
        self.assertEqual(error.exception.code, errno.EXDEV)

    def test_http_binary_protocol_and_authentication(self):
        self.store.close()
        self.store = Store(Path(self.temp.name) / "unauthenticated", "test-client", self.github)
        server = Server(("127.0.0.1", 0), self.store, "session")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}"
        with urlopen(url + "/api/hello?token=wrong") as response:
            self.assertEqual(STATUS.unpack(response.read(8))[0], -errno.EACCES)
        with urlopen(url + "/api/hello?token=session") as response:
            data = response.read()
        self.assertEqual(STATUS.unpack_from(data)[0], 0)
        self.assertEqual(META.unpack_from(data, 8)[0], 1)
        for method in ("list?id=1", "lookup?parent=1&name=alice"):
            with urlopen(url + "/api/" + method + "&token=session") as response:
                self.assertEqual(STATUS.unpack(response.read(8))[0], -errno.EACCES)
        with urlopen(url + "/api/lookup?token=session&parent=1&name=%FF") as response:
            self.assertEqual(STATUS.unpack(response.read(8))[0], -errno.EINVAL)
        request = Request(url + "/control", json.dumps({"operation": "auth_start"}).encode(),
                          {"Authorization": "Bearer session"})
        with urlopen(request) as response:
            flow = json.load(response)["result"]["flow"]
        request = Request(url + "/control", json.dumps({"operation": "auth_poll", "flow": flow}).encode(),
                          {"Authorization": "Bearer session"})
        with urlopen(request) as response:
            self.assertEqual(json.load(response)["result"]["login"], "alice")
        with urlopen(url + "/api/lookup?token=session&parent=1&name=alice") as response:
            self.assertEqual(STATUS.unpack(response.read(8))[0], 0)
        with urlopen(url + "/api/list?token=session&id=1") as response:
            self.assertEqual(STATUS.unpack(response.read(8))[0], 0)

        # Run the actual CLI against HTTP: cwd activation and an explicit path.
        mount = Path(self.temp.name) / "mount"
        directory = mount / "alice/demo"
        directory.mkdir(parents=True)
        token = Path(self.temp.name) / "token"
        token.write_text("session")
        token.chmod(0o600)
        command = [sys.executable, "-B", str(Path(__file__).resolve().parents[1] / "bin/goi")]
        environment = {**os.environ, "XDG_CONFIG_HOME": str(Path(self.temp.name) / "config")}
        with patch.dict(os.environ, environment):
            cli.save_configuration(mount, url, token)
        for arguments in (("activate",), ("activate", str(directory))):
            result = subprocess.run(command + list(arguments), cwd=directory, env=environment,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        repo = self.store.resolve_path("alice/demo")
        self.assertTrue(self.store.repo(repo["id"])["active"])
        self.assertEqual(self.github.blob_reads, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
