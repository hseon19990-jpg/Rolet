import asyncio
import sqlite3
import threading
import time

from config import DEFAULTS

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
 user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, lang TEXT DEFAULT 'ar',
 is_premium INTEGER DEFAULT 0, verified INTEGER DEFAULT 0, banned INTEGER DEFAULT 0,
 blocked_bot INTEGER DEFAULT 0, captcha_answer TEXT, payload TEXT, created_at INTEGER, last_seen INTEGER);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS mandatory_channels(
 chat_id INTEGER PRIMARY KEY, title TEXT, username TEXT, url TEXT, added_at INTEGER);
CREATE TABLE IF NOT EXISTS channels(
 chat_id INTEGER PRIMARY KEY, owner_id INTEGER, title TEXT, username TEXT,
 active INTEGER DEFAULT 1, added_at INTEGER);
CREATE TABLE IF NOT EXISTS invite_links(
 code TEXT PRIMARY KEY, owner_id INTEGER, channel_id INTEGER, title TEXT, price REAL, threshold INTEGER,
 is_default INTEGER DEFAULT 0, active INTEGER DEFAULT 1, notified90 INTEGER DEFAULT 0,
 notified100 INTEGER DEFAULT 0, created_at INTEGER);
CREATE UNIQUE INDEX IF NOT EXISTS ux_link_default ON invite_links(owner_id) WHERE is_default=1;
CREATE UNIQUE INDEX IF NOT EXISTS ux_link_chan ON invite_links(owner_id, channel_id) WHERE channel_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS referrals(
 user_id INTEGER PRIMARY KEY, owner_id INTEGER, link_code TEXT, source TEXT, source_id INTEGER,
 created_at INTEGER, status TEXT DEFAULT 'pending', claim_id INTEGER);
CREATE INDEX IF NOT EXISTS ix_ref_link ON referrals(link_code, status);
CREATE TABLE IF NOT EXISTS payouts(
 id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, amount REAL, count INTEGER,
 status TEXT DEFAULT 'pending', note TEXT, created_at INTEGER, decided_at INTEGER, decided_by INTEGER);
CREATE TABLE IF NOT EXISTS events(
 id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER, kind TEXT, title TEXT, prize TEXT,
 winners INTEGER DEFAULT 1, status TEXT DEFAULT 'active', channel_id INTEGER, message_id INTEGER,
 created_at INTEGER, end_at INTEGER, need_premium INTEGER DEFAULT 0, comment_chat INTEGER,
 comment_text TEXT, need_bot_url TEXT, reminded INTEGER DEFAULT 0, spike_alerted INTEGER DEFAULT 0, proof TEXT,
 invite_bonus INTEGER DEFAULT 0, featured INTEGER DEFAULT 0, notify_owner INTEGER DEFAULT 1,
 promo_text TEXT, min_members INTEGER DEFAULT 0, max_channels INTEGER DEFAULT 10, comment_url TEXT, vote_url TEXT, draw_mode TEXT DEFAULT 'auto', draw_notified INTEGER DEFAULT 0, approve_mode INTEGER DEFAULT 0, vote_goal INTEGER DEFAULT 0, join_goal INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS event_entries(
 event_id INTEGER, chat_id INTEGER, user_id INTEGER, title TEXT, members INTEGER, message_id INTEGER,
 created_at INTEGER, PRIMARY KEY(event_id, chat_id));
CREATE TABLE IF NOT EXISTS entry_removals(
 event_id INTEGER, chat_id INTEGER, user_id INTEGER, title TEXT, members INTEGER,
 removed_at INTEGER, repost_msg_id INTEGER, PRIMARY KEY(event_id, chat_id));
CREATE TABLE IF NOT EXISTS event_channels(
 event_id INTEGER, chat_id INTEGER, title TEXT, username TEXT, url TEXT, PRIMARY KEY(event_id, chat_id));
CREATE TABLE IF NOT EXISTS participants(
 event_id INTEGER, user_id INTEGER, joined_at INTEGER, PRIMARY KEY(event_id, user_id));
CREATE TABLE IF NOT EXISTS candidates(
 id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER, user_id INTEGER, name TEXT, created_at INTEGER, approved INTEGER DEFAULT 1,
 UNIQUE(event_id, user_id));
CREATE TABLE IF NOT EXISTS votes(
 event_id INTEGER, voter_id INTEGER, candidate_id INTEGER, created_at INTEGER, PRIMARY KEY(event_id, voter_id));
CREATE INDEX IF NOT EXISTS ix_votes_cand ON votes(candidate_id, created_at);
CREATE TABLE IF NOT EXISTS winners(
 event_id INTEGER, user_id INTEGER, place INTEGER, PRIMARY KEY(event_id, user_id));
CREATE TABLE IF NOT EXISTS comment_hits(event_id INTEGER, user_id INTEGER, PRIMARY KEY(event_id, user_id));
CREATE TABLE IF NOT EXISTS confirms(event_id INTEGER, user_id INTEGER, PRIMARY KEY(event_id, user_id));
CREATE TABLE IF NOT EXISTS vote_confirms(event_id INTEGER, user_id INTEGER, PRIMARY KEY(event_id, user_id));
CREATE TABLE IF NOT EXISTS admin_overrides(event_id INTEGER, user_id INTEGER, code TEXT, PRIMARY KEY(event_id, user_id, code));
CREATE TABLE IF NOT EXISTS event_invites(
 event_id INTEGER, user_id INTEGER, inviter_id INTEGER, PRIMARY KEY(event_id, user_id));
CREATE TABLE IF NOT EXISTS event_managers(event_id INTEGER, user_id INTEGER, PRIMARY KEY(event_id, user_id));
CREATE TABLE IF NOT EXISTS flags(owner_id INTEGER PRIMARY KEY, reason TEXT, created_at INTEGER);
CREATE TABLE IF NOT EXISTS reports(
 id INTEGER PRIMARY KEY AUTOINCREMENT, reporter_id INTEGER, event_id INTEGER, reason TEXT, created_at INTEGER);
CREATE TABLE IF NOT EXISTS audit(
 id INTEGER PRIMARY KEY AUTOINCREMENT, actor_id INTEGER, action TEXT, detail TEXT, created_at INTEGER);
"""


class DB:
    def __init__(self, path):
        self.path = path
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        for col in ("invite_bonus INTEGER DEFAULT 0", "featured INTEGER DEFAULT 0", "notify_owner INTEGER DEFAULT 1",
                    "promo_text TEXT", "min_members INTEGER DEFAULT 0", "max_channels INTEGER DEFAULT 10", "comment_url TEXT", "vote_url TEXT",
                    "draw_mode TEXT DEFAULT 'auto'", "draw_notified INTEGER DEFAULT 0", "approve_mode INTEGER DEFAULT 0", "vote_goal INTEGER DEFAULT 0", "join_goal INTEGER DEFAULT 0"):  # ترقية قواعد قديمة
            try:
                self.conn.execute(f"ALTER TABLE events ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass
        try:
            self.conn.execute("ALTER TABLE candidates ADD COLUMN approved INTEGER DEFAULT 1")
        except sqlite3.OperationalError:
            pass
        self.cache = dict(DEFAULTS)
        for r in self.conn.execute("SELECT key,value FROM settings"):
            self.cache[r["key"]] = r["value"]

    # ---- settings
    def get(self, key):
        return self.cache.get(key, DEFAULTS.get(key, ""))

    async def set(self, key, value):
        self.cache[key] = str(value)
        await self.run(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            key, str(value))

    # ---- queries
    def _q(self, sql, args, mode):
        with self.lock:
            cur = self.conn.execute(sql, args)
            if mode == "all":
                return [dict(r) for r in cur.fetchall()]
            if mode == "one":
                r = cur.fetchone()
                return dict(r) if r else None
            if mode == "val":
                r = cur.fetchone()
                return r[0] if r else None
            if mode == "insert":
                return cur.lastrowid
            return cur.rowcount

    async def all(self, sql, *a):
        return await asyncio.to_thread(self._q, sql, a, "all")

    async def one(self, sql, *a):
        return await asyncio.to_thread(self._q, sql, a, "one")

    async def val(self, sql, *a):
        return await asyncio.to_thread(self._q, sql, a, "val")

    async def run(self, sql, *a):
        return await asyncio.to_thread(self._q, sql, a, "run")

    async def insert(self, sql, *a):
        return await asyncio.to_thread(self._q, sql, a, "insert")

    def _tx(self, fn):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                r = fn(self.conn)
                self.conn.execute("COMMIT")
                return r
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    async def tx(self, fn):
        return await asyncio.to_thread(self._tx, fn)

    def backup_to(self, dest):
        with self.lock:
            out = sqlite3.connect(dest)
            self.conn.backup(out)
            out.close()

    # ---- users
    async def touch_user(self, u):
        """يسجل/يحدّث المستخدم. يرجع True إذا هو جديد (أول تفعيل للبوت)."""
        code = getattr(u, "language_code", None) or "ar"
        lang = "ar" if code.startswith("ar") else "en"
        t = int(time.time())
        premium = int(bool(getattr(u, "is_premium", False)))

        def fn(c):
            row = c.execute("SELECT 1 FROM users WHERE user_id=?", (u.id,)).fetchone()
            if row:
                c.execute(
                    "UPDATE users SET username=?,first_name=?,is_premium=?,last_seen=?,blocked_bot=0 WHERE user_id=?",
                    (u.username, u.first_name, premium, t, u.id))
                return False
            c.execute(
                "INSERT INTO users(user_id,username,first_name,lang,is_premium,created_at,last_seen) VALUES(?,?,?,?,?,?,?)",
                (u.id, u.username, u.first_name, lang, premium, t, t))
            return True

        return await self.tx(fn)
