"""SQLite storage. All modifications run inside short, atomic transactions."""
from __future__ import annotations
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

from .probe import Endpoint, Status

CATEGORIES = {
    'survival': 'Выживание',
    'skyblock': 'SkyBlock',
    'anarchy': 'Анархия',
    'minigames': 'Мини-игры',
    'modded': 'Моды',
    'other': 'Другое',
}

SCHEMA = '''
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, username TEXT NOT NULL DEFAULT '', joined_at INTEGER NOT NULL,
  terms_accepted_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS servers (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  edition TEXT NOT NULL CHECK(edition IN ('java', 'bedrock')),
  host TEXT NOT NULL, port INTEGER NOT NULL CHECK(port BETWEEN 1 AND 65535),
  name TEXT NOT NULL, category TEXT NOT NULL,
  added_by INTEGER NOT NULL REFERENCES users(id),
  created_at INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'unknown',
  version TEXT NOT NULL DEFAULT '', players INTEGER NOT NULL DEFAULT 0,
  max_players INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0,
  motd TEXT NOT NULL DEFAULT '', last_checked_at INTEGER NOT NULL DEFAULT 0,
  last_changed_at INTEGER NOT NULL DEFAULT 0,
  failure_streak INTEGER NOT NULL DEFAULT 0,
  promoted_until INTEGER NOT NULL DEFAULT 0,
  UNIQUE(edition, host, port)
);
CREATE INDEX IF NOT EXISTS server_catalog_idx ON servers(edition,category,status);
CREATE TABLE IF NOT EXISTS favorites (
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  server_id INTEGER NOT NULL REFERENCES servers(id) ON DELETE CASCADE,
  alerts INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(user_id, server_id)
);
CREATE TABLE IF NOT EXISTS invoices (
  payload TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
  server_id INTEGER REFERENCES servers(id) ON DELETE SET NULL,
  stars INTEGER NOT NULL, created_at INTEGER NOT NULL,
  paid_at INTEGER NOT NULL DEFAULT 0,
  charge_id TEXT UNIQUE, refunded_at INTEGER NOT NULL DEFAULT 0
);
'''


class Database:
    def __init__(self, path: str):
        if path != ':memory:':
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock, self.conn:
            self.conn.execute('PRAGMA journal_mode=WAL')
            self.conn.executescript(SCHEMA)

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    def one(self, query: str, params: tuple = ()) -> dict | None:
        with self.lock:
            row = self.conn.execute(query, params).fetchone()
            return dict(row) if row else None

    def all(self, query: str, params: tuple = ()) -> list[dict]:
        with self.lock:
            return [dict(row) for row in self.conn.execute(query, params).fetchall()]

    def register(self, user_id: int, username: str = '') -> None:
        with self.lock, self.conn:
            self.conn.execute('''INSERT INTO users(id,username,joined_at) VALUES(?,?,?)
               ON CONFLICT(id) DO UPDATE SET username=excluded.username''',
               (user_id, username[:60], int(time.time())))

    def accept_terms(self, user_id: int) -> None:
        with self.lock, self.conn:
            self.conn.execute('UPDATE users SET terms_accepted_at=? WHERE id=?',
                              (int(time.time()), user_id))

    def accepted_terms(self, user_id: int) -> bool:
        row = self.one('SELECT terms_accepted_at FROM users WHERE id=?', (user_id,))
        return bool(row and row['terms_accepted_at'])

    def my_count(self, user_id: int) -> int:
        row = self.one('SELECT COUNT(*) AS n FROM servers WHERE added_by=?', (user_id,))
        return int(row['n'])

    def add_server(self, endpoint: Endpoint, name: str, category: str, user_id: int,
                   max_per_user: int) -> tuple[int, bool]:
        if category not in CATEGORIES:
            raise ValueError('Неизвестная категория')
        with self.lock, self.conn:
            existing = self.conn.execute('''SELECT id FROM servers WHERE edition=? AND host=? AND port=?''',
                                         (endpoint.edition, endpoint.host, endpoint.port)).fetchone()
            if existing:
                return existing['id'], False
            if self.conn.execute('SELECT COUNT(*) FROM servers WHERE added_by=?', (user_id,)).fetchone()[0] >= max_per_user:
                raise ValueError(f'Лимит: не более {max_per_user} серверов на пользователя.')
            cur = self.conn.execute('''INSERT INTO servers
                (edition,host,port,name,category,added_by,created_at)
                VALUES (?,?,?,?,?,?,?)''',
                (endpoint.edition, endpoint.host, endpoint.port, name[:60],
                 category, user_id, int(time.time())))
            return int(cur.lastrowid), True

    def server(self, server_id: int) -> dict | None:
        return self.one('SELECT * FROM servers WHERE id=?', (server_id,))

    def mine(self, user_id: int) -> list[dict]:
        return self.all('SELECT * FROM servers WHERE added_by=? ORDER BY id DESC LIMIT 50', (user_id,))

    def catalog(self, edition: str = 'all', category: str = 'all', offset: int = 0,
                search: str = '', limit: int = 6) -> tuple[list[dict], bool]:
        conditions: list[str] = []
        args: list[Any] = []
        if edition in ('java', 'bedrock'):
            conditions.append('edition=?')
            args.append(edition)
        if category in CATEGORIES:
            conditions.append('category=?')
            args.append(category)
        if search:
            escaped = search[:75].replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
            conditions.append("(name LIKE ? ESCAPE '\\' OR host LIKE ? ESCAPE '\\')")
            args += [f'%{escaped}%', f'%{escaped}%']
        where = (' WHERE ' + ' AND '.join(conditions)) if conditions else ''
        now = int(time.time())
        rows = self.all('''SELECT * FROM servers''' + where + '''
           ORDER BY (promoted_until > ?) DESC,
               (status='online') DESC, players DESC, id DESC
           LIMIT ? OFFSET ?''', tuple(args + [now, limit + 1, max(0, min(500, offset))]))
        return rows[:limit], len(rows) > limit

    def all_servers(self) -> list[dict]:
        return self.all('SELECT * FROM servers ORDER BY id')

    def favorite(self, user_id: int, server_id: int) -> bool:
        with self.lock, self.conn:
            exists = self.conn.execute('SELECT 1 FROM favorites WHERE user_id=? AND server_id=?',
                                       (user_id, server_id)).fetchone()
            if exists:
                self.conn.execute('DELETE FROM favorites WHERE user_id=? AND server_id=?',
                                  (user_id, server_id))
                return False
            if not self.conn.execute('SELECT 1 FROM servers WHERE id=?', (server_id,)).fetchone():
                return False
            self.conn.execute('INSERT INTO favorites(user_id,server_id) VALUES(?,?)', (user_id, server_id))
            return True

    def favorite_info(self, user_id: int, server_id: int) -> dict | None:
        return self.one('SELECT * FROM favorites WHERE user_id=? AND server_id=?', (user_id, server_id))

    def toggle_alert(self, user_id: int, server_id: int) -> bool | None:
        with self.lock, self.conn:
            row = self.conn.execute('SELECT alerts FROM favorites WHERE user_id=? AND server_id=?',
                                    (user_id, server_id)).fetchone()
            if not row:
                return None
            enabled = 0 if row['alerts'] else 1
            self.conn.execute('UPDATE favorites SET alerts=? WHERE user_id=? AND server_id=?',
                              (enabled, user_id, server_id))
            return bool(enabled)

    def favorites(self, user_id: int) -> list[dict]:
        return self.all('''SELECT s.* FROM favorites f JOIN servers s ON s.id=f.server_id
             WHERE f.user_id=? ORDER BY s.name LIMIT 50''', (user_id,))

    def subscribers(self, server_id: int) -> list[int]:
        return [int(x['user_id']) for x in self.all('''SELECT user_id FROM favorites
             WHERE server_id=? AND alerts=1''', (server_id,))]

    def update_status(self, server_id: int, status: Status) -> tuple[str, str] | None:
        """Two failures are required to mark a previously-online server offline.

        Returns a transition ONLY if old status was known; avoids false
        notifications immediately after registration.
        """
        with self.lock, self.conn:
            row = self.conn.execute('SELECT status,failure_streak,version,motd,last_changed_at FROM servers WHERE id=?', (server_id,)).fetchone()
            if not row:
                return None
            old = row['status']
            failures = 0 if status.online else row['failure_streak'] + 1
            new = 'online' if status.online else ('offline' if failures >= 2 else old)
            now = int(time.time())
            changed = old != new
            self.conn.execute('''UPDATE servers SET status=?, failure_streak=?,
                version=?, players=?, max_players=?, latency_ms=?, motd=?,
                last_checked_at=?, last_changed_at=? WHERE id=?''',
                (new, failures, status.version if status.online else row['version'],
                 status.players if status.online else 0,
                 status.max_players if status.online else 0,
                 status.latency_ms if status.online else 0,
                 status.motd if status.online else row['motd'],
                 now, now if changed else row['last_changed_at'], server_id))
            return (old, new) if changed and old != 'unknown' else None

    def statistics(self) -> dict:
        return self.one('''SELECT COUNT(*) total,
             COALESCE(SUM(status='online'),0) online,
             COALESCE(SUM(edition='java'),0) java,
             COALESCE(SUM(edition='bedrock'),0) bedrock FROM servers''') or {}

    def create_invoice(self, payload: str, user_id: int, server_id: int, stars: int) -> bool:
        with self.lock, self.conn:
            row = self.conn.execute('SELECT added_by FROM servers WHERE id=?', (server_id,)).fetchone()
            if not row or row['added_by'] != user_id:
                return False
            self.conn.execute('INSERT INTO invoices(payload,user_id,server_id,stars,created_at) VALUES(?,?,?,?,?)',
                              (payload, user_id, server_id, stars, int(time.time())))
            return True

    def invoice_valid(self, payload: str, user_id: int, stars: int, currency: str) -> bool:
        row = self.one('''SELECT i.*,s.added_by FROM invoices i JOIN servers s ON i.server_id=s.id
                          WHERE i.payload=?''', (payload,))
        return bool(row and row['user_id'] == user_id and row['added_by'] == user_id
                    and row['stars'] == stars and currency == 'XTR' and row['paid_at'] == 0
                    and int(time.time()) - row['created_at'] <= 86400
                    and self.accepted_terms(user_id))

    def pay(self, payload: str, user_id: int, stars: int, currency: str,
            charge_id: str, days: int) -> tuple[int, bool] | None:
        """Idempotently consume a valid Telegram Stars receipt."""
        with self.lock, self.conn:
            row = self.conn.execute('SELECT * FROM invoices WHERE payload=?', (payload,)).fetchone()
            if not row or row['user_id'] != user_id or row['stars'] != stars or currency != 'XTR':
                return None
            if row['paid_at']:
                return (row['server_id'], False) if row['charge_id'] == charge_id else None
            if not charge_id or self.conn.execute('SELECT 1 FROM invoices WHERE charge_id=?', (charge_id,)).fetchone():
                return None
            now = int(time.time())
            # Once Telegram has actually charged a valid invoice, fulfillment
            # must not be rejected because delivery was delayed.
            server = self.conn.execute('SELECT added_by,promoted_until FROM servers WHERE id=?', (row['server_id'],)).fetchone()
            if not server or server['added_by'] != user_id:
                return None
            self.conn.execute('UPDATE invoices SET paid_at=?,charge_id=? WHERE payload=?',
                              (now, charge_id, payload))
            self.conn.execute('UPDATE servers SET promoted_until=? WHERE id=?',
                              (max(now, server['promoted_until']) + days*86400, row['server_id']))
            return row['server_id'], True

    def lookup_payment(self, uid: int, charge: str) -> dict | None:
        return self.one('''SELECT * FROM invoices WHERE user_id=? AND charge_id=?
                           AND paid_at>0 AND refunded_at=0''', (uid, charge))

    def mark_refunded(self, uid: int, charge: str, days: int) -> bool:
        """Call only AFTER a successful refundStarPayment API response."""
        with self.lock, self.conn:
            invoice = self.conn.execute('''SELECT * FROM invoices
               WHERE user_id=? AND charge_id=? AND paid_at>0 AND refunded_at=0''',
               (uid, charge)).fetchone()
            if not invoice:
                return False
            self.conn.execute('UPDATE invoices SET refunded_at=? WHERE payload=?',
                              (int(time.time()), invoice['payload']))
            if invoice['server_id'] is not None:
                server = self.conn.execute('SELECT promoted_until FROM servers WHERE id=?',
                                           (invoice['server_id'],)).fetchone()
                if server and server['promoted_until'] > int(time.time()):
                    new_end = max(int(time.time()), server['promoted_until'] - days*86400)
                    self.conn.execute('UPDATE servers SET promoted_until=? WHERE id=?',
                                      (new_end, invoice['server_id']))
            return True

    def delete(self, server_id: int) -> bool:
        with self.lock, self.conn:
            return bool(self.conn.execute('DELETE FROM servers WHERE id=?', (server_id,)).rowcount)


