"""Executed only inside the disposable QEMU guest with /persist on a virtual disk."""
import os
from pathlib import Path
import subprocess
import sys
import threading

sys.path.insert(0, '/lab')
from goi.storage import Store
from goi.server import Server
from goi.journal import Journal, JournalHandler
from fake_github import FakeGitHub

TOKEN = 'crash-test-token'
store = Store('/persist/service', 'client', FakeGitHub())
if not store.db.execute('SELECT 1 FROM accounts').fetchone():
    store.add_account('fixture')
    store.activate(store.resolve_path('alice/demo')['id'])
    store.db.commit()
remote = Server(('127.0.0.1', 8080), store, TOKEN)
threading.Thread(target=remote.serve_forever, daemon=True).start()
journal = Journal('/persist/journal', 'http://127.0.0.1:8080', TOKEN)
local = Server(('127.0.0.1', 8081), journal, TOKEN)
local.RequestHandlerClass = JournalHandler
threading.Thread(target=local.serve_forever, daemon=True).start()
if sys.argv[1] == 'recover':
    # The remote database never received the write before the VM was killed.
    assert store.resolve_path('alice/demo/main/README.md')['data'] != b'power-loss-safe\n'
    assert journal.recover() == {'restored': 1}
subprocess.run(['insmod', '/lab/source/vtfs.ko'], check=True)
Path('/mnt/goi').mkdir(exist_ok=True)
subprocess.run(['mount', '-t', 'vtfs', 'none', '/mnt/goi', '-o',
                f'server=127.0.0.1,port=8080,token={TOKEN},autosync=0,journal_port=8081'], check=True)
path = Path('/mnt/goi/alice/demo/main/README.md')
if sys.argv[1] == 'write':
    path.write_bytes(b'power-loss-safe\n')  # No fsync, autosync or umount follows.
    assert journal.db.execute('SELECT data FROM writes').fetchone()[0] == b'power-loss-safe\n'
    assert store.resolve_path('alice/demo/main/README.md')['data'] != b'power-loss-safe\n'
    print('GOI_CRASH_READY', flush=True)
    threading.Event().wait()
else:
    assert path.read_bytes() == b'power-loss-safe\n'
    assert not journal.db.execute('SELECT 1 FROM writes').fetchone()
    subprocess.run(['umount', '/mnt/goi'], check=True)
    subprocess.run(['rmmod', 'vtfs'], check=True)
    print('GOI_CRASH_RECOVERED', flush=True)
