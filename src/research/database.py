"""Small domain-facing APSW adapter; research uses one private SQLite engine."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import apsw


class Row:
    def __init__(self, names, values):
        self._names, self._values = names, values

    def keys(self):
        return self._names

    def __getitem__(self, key):
        return self._values[self._names.index(key)] if isinstance(key, str) else self._values[key]

    def __iter__(self):
        return iter(self._values)


class Result:
    def __init__(self, cursor, changed):
        self.cursor, self.rowcount = cursor, changed

    def __iter__(self):
        return iter(self.cursor)

    def fetchone(self):
        return next(self.cursor, None)

    def fetchall(self):
        return list(self.cursor)


@lru_cache(maxsize=1)
def verify_engine():
    if tuple(int(x) for x in apsw.sqlitelibversion().split('.')) < (3, 53, 4):
        raise RuntimeError('research requires SQLite >= 3.53.4 from the pinned APSW wheel')
    probe = apsw.Connection(':memory:')
    try:
        probe.execute("CREATE VIRTUAL TABLE capability USING fts5(body, tokenize='trigram')")
        probe.execute("INSERT INTO capability VALUES('中文研究 English research')")
        if not list(probe.execute("SELECT rowid FROM capability WHERE capability MATCH 'research'")):
            raise RuntimeError('FTS5 capability unavailable')
    finally:
        probe.close()


class Database:
    def __init__(self, path: Path, readonly=False):
        verify_engine()
        flags = apsw.SQLITE_OPEN_READONLY if readonly else apsw.SQLITE_OPEN_READWRITE | apsw.SQLITE_OPEN_CREATE
        self.connection = apsw.Connection(str(path), flags=flags)
        self.connection.set_busy_timeout(5000)
        self.connection.set_row_trace(lambda cursor, values: Row(tuple(d[0] for d in cursor.get_description()), values))
        self.connection.enable_load_extension(False)

    def execute(self, sql, bindings=()):
        cursor = self.connection.execute(sql, bindings)
        return Result(cursor, self.connection.changes())

    def executescript(self, sql):
        list(self.connection.execute(sql))

    def close(self):
        self.connection.close()

    def backup_to(self, path: Path):
        destination = apsw.Connection(str(path))
        try:
            with destination.backup('main', self.connection, 'main') as backup:
                while not backup.done:
                    backup.step(256)
        finally:
            destination.close()


STORAGE_BUSY = (apsw.BusyError, apsw.LockedError)
