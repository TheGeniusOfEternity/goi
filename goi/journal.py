"""Client-side durable write journal. No GitHub tokens or filesystem logic here."""
import argparse
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.request import Request, urlopen

from .protocol import Error, STATUS, META, MAX_FILE_SIZE, require
from .server import Handler, Server

LIMIT = 512 * 1024 * 1024


class Journal:
    def __init__(self, directory, server, token):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.server, self.token = server.rstrip('/'), token
        self.identity = hashlib.sha256((self.server + '\n' + token).encode()).hexdigest()
        self.db = sqlite3.connect(self.directory / 'journal.sqlite3', check_same_thread=False)
        os.chmod(self.directory / 'journal.sqlite3', 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('CREATE TABLE IF NOT EXISTS identity (value TEXT); '
                             'CREATE TABLE IF NOT EXISTS writes (id INTEGER PRIMARY KEY, revision INTEGER, '
                             'mode INTEGER, data BLOB NOT NULL);')
        existing = self.db.execute('SELECT value FROM identity').fetchone()
        if existing is not None and existing[0] != self.identity:
            self.db.close()
            raise Error(errno.EXDEV, 'journal belongs to another server/token; export pending data before changing configuration')
        if existing is None:
            self.db.execute('INSERT INTO identity VALUES (?)', (self.identity,))
        self.db.commit()
        self.lock = threading.RLock()

    def trim_cache(self):
        pass

    def close(self):
        self.db.close()

    def record(self, number, revision, mode, data):
        size = self.db.execute('SELECT COALESCE(SUM(length(data)),0) FROM writes WHERE id!=?', (number,)).fetchone()[0]
        require(size + len(data) <= LIMIT, errno.ENOSPC, 'client journal is full; sync pending files')
        # Keep the first remote baseline while successive local writes replace the snapshot.
        self.db.execute('INSERT INTO writes VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE '
                        'SET mode=excluded.mode,data=excluded.data', (number, revision, mode, data))

    def recover(self):
        restored = 0
        for row in list(self.db.execute('SELECT * FROM writes ORDER BY id')):
            query = f"id={row['id']}&mode={row['mode']}&expected_revision={row['revision']}"
            request = Request(self.server + '/api/write?' + query, row['data'],
                              {'Authorization': 'Bearer ' + self.token}, method='POST')
            with urlopen(request, timeout=120) as response:
                data = response.read(STATUS.size + META.size + 4096)
            require(len(data) >= STATUS.size, errno.EPROTO, 'short replay response')
            code = STATUS.unpack_from(data)[0]
            require(code == 0, -code if code < 0 else errno.EPROTO,
                    f"journal file {row['id']} cannot be replayed; export it before resolving the remote change")
            require(len(data) == STATUS.size + META.size, errno.EPROTO, 'short replay metadata')
            self.db.execute('DELETE FROM writes WHERE id=?', (row['id'],))
            self.db.commit()  # Lost replies are safe: server compares both revision and bytes.
            restored += 1
        return {'restored': restored}


class JournalHandler(Handler):
    def api(self, method, args, body):
        require(self.command == 'POST', message='journal changes require POST')
        number = int(args['id'])
        require(number > 0, message='invalid inode')
        if method == 'record':
            revision, mode = int(args['revision']), int(args['mode'])
            require(revision >= 0 and 0 <= mode <= 0o777 and len(body) <= MAX_FILE_SIZE)
            self.server.store.record(number, revision, mode, body)
        elif method == 'ack':
            self.server.store.db.execute('DELETE FROM writes WHERE id=?', (number,))
        else:
            raise Error(errno.ENOSYS, 'unknown journal operation')
        return b''

    def control(self, request):
        store = self.server.store
        operation = request['operation']
        if operation == 'info':
            return {'identity': store.identity, 'files': [
                {'id': row['id'], 'revision': row['revision'], 'bytes': row['bytes']}
                for row in store.db.execute('SELECT id,revision,length(data) AS bytes FROM writes ORDER BY id')]}
        if operation == 'discard':
            store.db.execute('DELETE FROM writes WHERE id=?', (int(request['id']),))
            return {'discarded': int(request['id'])}
        if operation == 'replay':
            return store.recover()
        if operation == 'export':
            # The CLI chooses a local destination; the daemon never writes arbitrary paths.
            import base64
            row = store.db.execute('SELECT data FROM writes WHERE id=?', (int(request['id']),)).fetchone()
            require(row is not None, errno.ENOENT, 'journal entry not found')
            return {'data': base64.b64encode(row['data']).decode()}
        raise Error(errno.ENOSYS, 'unknown journal operation')


def ensure(client):
    from .cli import Client, configuration_path
    key = hashlib.sha256(str(client.mountpoint).encode()).hexdigest()[:24]
    directory = configuration_path().parent / 'journals' / key
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    identity = hashlib.sha256((client.server.rstrip('/') + '\n' + client.token).encode()).hexdigest()
    token_path = directory / 'token'
    # Refuse a changed backend before replacing the token of a running journal.
    dbpath = directory / 'journal.sqlite3'
    if dbpath.exists():
        db = sqlite3.connect(dbpath)
        try:
            row = db.execute('SELECT value FROM identity').fetchone()
            require(row is None or row[0] == identity, errno.EXDEV,
                    'journal backend changed; retain the old configuration and export/recover its data first')
        finally:
            db.close()
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as file:
        file.write(client.token)
        file.flush()
        os.fsync(file.fileno())
    def connect():
        try:
            port = int((directory / 'port').read_text())
            local = Client(client.mountpoint, {'server': f'http://127.0.0.1:{port}', 'token_file': str(token_path)})
            require(local.call('info')['identity'] == identity, errno.EXDEV, 'journal identity mismatch')
            return local, port
        except (OSError, ValueError, Error):
            return None
    running = connect()
    if running:
        return running
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    with (directory / 'daemon.log').open('ab') as log:
        subprocess.Popen([sys.executable, '-m', 'goi.journal', '--data', str(directory),
                          '--server', client.server, '--token-file', str(token_path)],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True, env=env)
    for _ in range(50):
        time.sleep(.1)
        running = connect()
        if running:
            return running
    raise Error(errno.EIO, f'client journal failed to start; inspect {directory / "daemon.log"}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', required=True)
    parser.add_argument('--server', required=True)
    parser.add_argument('--token-file', required=True)
    args = parser.parse_args()
    directory = Path(args.data)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / 'daemon.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = Journal(directory, args.server, Path(args.token_file).read_text().strip())
        port_path = directory / 'port'
        port = int(port_path.read_text()) if port_path.exists() else 0
        server = Server(('127.0.0.1', port), store, store.token)
        server.RequestHandlerClass = JournalHandler
        temporary = directory / 'port.tmp'
        temporary.write_text(str(server.server_port))
        temporary.replace(directory / 'port')
        try:
            server.serve_forever()
        finally:
            server.server_close()
            store.close()


if __name__ == '__main__':
    main()
