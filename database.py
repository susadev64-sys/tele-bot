"""
database.py - PostgreSQL (Supabase) storage for the Anonymous Messaging & Social Hub bot.

Same public API as the old SQLite version, so bot.py needs (almost) no changes.
Data now lives in Supabase, NOT on the Railway container -> redeploys / restarts / crashes
never delete users, subscriptions, links, messages, blocks, bans or payments.

Safety notes
  * Payments + Pro activation (and gifts) run in ONE transaction: a crash can never leave
    "payment recorded but Pro not activated".
  * Row Level Security is enabled on every table with no policies, so the public Supabase
    REST API (anon key) cannot read anything. The bot connects with the database role, which
    bypasses RLS.
  * SECRET_KEY must never change (it protects encrypted sender IDs and derives the hashes).
"""
import base64
import hashlib
import hmac
import logging
import re
import secrets
import time
from typing import Optional

import psycopg
from cryptography.fernet import Fernet, InvalidToken
from psycopg import errors as pg_errors
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger("anon_hub.db")

# ---- inbox filters ----------------------------------------------------------
_INBOX_BASE = "receiver_id=%s AND delivered=1 AND deleted=0 AND hidden=0"
_INBOX_FILTERS = {
    "all": "",
    "text": " AND kind='text'",
    "media": " AND kind!='text'",
    "replied": " AND replies>0",
    "noreply": " AND replies=0",
    "reacted": " AND reaction IS NOT NULL",
}
TAG_RE = re.compile(r"^U[0-9a-f]{5}$")
_EDITABLE_USER_FIELDS = {"bio", "welcome_text", "thank_sticker", "theme"}
_TABLES = ("users", "links", "messages", "blocks", "bans", "reports", "payments", "gifts")


def now() -> int:
    return int(time.time())


class Database:
    def __init__(self, dsn: str, secret_key: str):
        # Telegram IDs are > 2^31, so every id column below is BIGINT.
        # prepare_threshold=None -> works with Supabase's pooler (pgbouncer) in both modes.
        self.pool = ConnectionPool(
            conninfo=dsn,
            min_size=1,
            max_size=5,
            timeout=20,
            max_idle=300,
            max_lifetime=1800,
            check=ConnectionPool.check_connection,       # drops dead connections transparently
            kwargs={"row_factory": dict_row, "autocommit": True, "prepare_threshold": None},
            open=True,
        )
        self.pool.wait(timeout=30)                       # fail fast on a wrong DATABASE_URL
        self._init_crypto(secret_key)
        self._init_schema()
        log.info("Connected to PostgreSQL")

    def close(self) -> None:
        self.pool.close()

    # ------------------------------------------------------------ query helpers
    def _exec(self, sql: str, params=()) -> int:
        with self.pool.connection() as conn:
            return conn.execute(sql, params).rowcount

    def _one(self, sql: str, params=()):
        with self.pool.connection() as conn:
            return conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params=()):
        with self.pool.connection() as conn:
            return conn.execute(sql, params).fetchall()

    # ------------------------------------------------------------------ crypto
    def _init_crypto(self, secret_key: str) -> None:
        master = secret_key.encode()
        try:                                   # SECRET_KEY already a valid Fernet key?
            self._fernet = Fernet(master)
        except ValueError:                     # otherwise derive one from it
            derived = hashlib.sha256(b"fernet|" + master).digest()
            self._fernet = Fernet(base64.urlsafe_b64encode(derived))
        self._hash_key = hashlib.sha256(b"hash|" + master).digest()
        self._tag_key = hashlib.sha256(b"tag|" + master).digest()

    def sender_hash(self, user_id: int) -> str:
        return hmac.new(self._hash_key, str(user_id).encode(), hashlib.sha256).hexdigest()

    def sender_tag(self, receiver_id: int, sender_id: int) -> str:
        """Pro "same sender" tag (e.g. U7383a), different for every receiver."""
        digest = hmac.new(self._tag_key, f"{receiver_id}:{sender_id}".encode(), hashlib.sha256).hexdigest()
        return "U" + digest[:5]

    def encrypt_sender(self, user_id: int) -> bytes:
        return self._fernet.encrypt(str(user_id).encode())

    def decrypt_sender(self, token) -> Optional[int]:
        if not token:
            return None
        try:
            return int(self._fernet.decrypt(bytes(token)).decode())
        except (InvalidToken, ValueError):
            return None

    # ------------------------------------------------------------------ schema
    def _init_schema(self) -> None:
        # Transaction-level advisory lock: only one instance migrates at a time, and it is released
        # automatically at COMMIT (safe with Supabase's pgbouncer pooler, unlike session locks).
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(727001)")
            conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT,
                token TEXT UNIQUE NOT NULL,
                bio TEXT, welcome_text TEXT, thank_sticker TEXT,
                theme TEXT DEFAULT 'classic',
                is_pro INTEGER DEFAULT 0, pro_since BIGINT, pro_until BIGINT,
                auto_renew INTEGER DEFAULT 0, sub_charge_id TEXT, pro_notice INTEGER DEFAULT 0,
                custom_slug TEXT, hub_media_id TEXT, hub_media_type TEXT,
                state TEXT, state_data TEXT, created_at BIGINT
            )""")
            conn.execute("""
            CREATE TABLE IF NOT EXISTS links (
                id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL,
                platform TEXT NOT NULL, url TEXT NOT NULL, UNIQUE (user_id, url)
            )""")
            conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id BIGSERIAL PRIMARY KEY, receiver_id BIGINT NOT NULL,
                sender_hash TEXT NOT NULL, sender_enc BYTEA, sender_tag TEXT,
                kind TEXT NOT NULL, delivered INTEGER DEFAULT 1, deleted INTEGER DEFAULT 0,
                hidden INTEGER DEFAULT 0, card_msg_id BIGINT, media_msg_id BIGINT,
                replies INTEGER DEFAULT 0, reaction TEXT, edits INTEGER DEFAULT 0,
                created_at BIGINT
            )""")
            conn.execute("""
            CREATE TABLE IF NOT EXISTS blocks (
                receiver_id BIGINT NOT NULL, sender_hash TEXT NOT NULL,
                PRIMARY KEY (receiver_id, sender_hash)
            )""")
            conn.execute("""
            CREATE TABLE IF NOT EXISTS bans (
                sender_hash TEXT PRIMARY KEY, reason TEXT, created_at BIGINT
            )""")
            conn.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id BIGSERIAL PRIMARY KEY, message_id BIGINT NOT NULL, reporter_id BIGINT NOT NULL,
                reason TEXT, status TEXT DEFAULT 'open', created_at BIGINT,
                UNIQUE (message_id, reporter_id)
            )""")
            conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                charge_id TEXT PRIMARY KEY, user_id BIGINT, amount INTEGER,
                payload TEXT, created_at BIGINT
            )""")
            conn.execute("""
            CREATE TABLE IF NOT EXISTS gifts (
                charge_id TEXT PRIMARY KEY, message_id BIGINT, receiver_id BIGINT,
                gift_code TEXT, amount INTEGER, created_at BIGINT
            )""")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_slug ON users(custom_slug)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_receiver ON messages(receiver_id, id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_msg_sender ON messages(sender_hash, created_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_links_user ON links(user_id)")
            # Lock the tables away from Supabase's public REST API (no policies = no access).
            for t in _TABLES:
                conn.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")

    # ------------------------------------------------------------------- users
    def _new_token(self) -> str:
        while True:
            token = secrets.token_urlsafe(8)
            if not self._one("SELECT 1 FROM users WHERE token=%s", (token,)):
                return token

    def get_user(self, user_id: int):
        return self._one("SELECT * FROM users WHERE user_id=%s", (user_id,))

    def get_user_by_token(self, key: str):
        """Resolve a deep-link parameter: random token first, then a Pro custom slug."""
        if not key or len(key) > 64:
            return None
        row = self._one("SELECT * FROM users WHERE token=%s", (key,))
        if row:
            return row
        return self._one("SELECT * FROM users WHERE custom_slug=%s", (key.lower(),))

    def upsert_user(self, user_id: int, username: Optional[str], first_name: Optional[str]):
        first_name = first_name or ""
        row = self.get_user(user_id)
        if row and row["username"] == username and row["first_name"] == first_name:
            return row
        return self._one(
            "INSERT INTO users(user_id, username, first_name, token, created_at) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (user_id) DO UPDATE SET username=EXCLUDED.username, first_name=EXCLUDED.first_name "
            "RETURNING *",
            (user_id, username, first_name, self._new_token(), now()))

    def set_state(self, user_id: int, state: str, data: Optional[str] = None) -> None:
        self._exec("UPDATE users SET state=%s, state_data=%s WHERE user_id=%s", (state, data, user_id))

    def clear_state(self, user_id: int) -> None:
        self._exec("UPDATE users SET state=NULL, state_data=NULL WHERE user_id=%s", (user_id,))

    def set_field(self, user_id: int, field: str, value) -> None:
        if field not in _EDITABLE_USER_FIELDS:             # allow-list: never interpolate user input
            raise ValueError(f"field not editable: {field}")
        self._exec(f"UPDATE users SET {field}=%s WHERE user_id=%s", (value, user_id))

    def regenerate_token(self, user_id: int) -> None:
        self._exec("UPDATE users SET token=%s WHERE user_id=%s", (self._new_token(), user_id))

    # ---- custom link (Pro) ----------------------------------------------------
    def set_slug(self, user_id: int, slug: Optional[str]) -> str:
        """Returns 'ok' or 'taken'. slug=None removes it."""
        if slug is None:
            self._exec("UPDATE users SET custom_slug=NULL WHERE user_id=%s", (user_id,))
            return "ok"
        slug = slug.lower()
        if self._one("SELECT 1 FROM users WHERE token=%s", (slug,)):
            return "taken"                                 # never shadow a random token
        try:
            n = self._exec("UPDATE users SET custom_slug=%s WHERE user_id=%s", (slug, user_id))
        except pg_errors.UniqueViolation:
            return "taken"
        return "ok" if n else "taken"

    # ---- hub media (Pro) --------------------------------------------------------
    def set_hub_media(self, user_id: int, media_type: Optional[str], file_id: Optional[str]) -> None:
        self._exec("UPDATE users SET hub_media_type=%s, hub_media_id=%s WHERE user_id=%s",
                   (media_type, file_id, user_id))

    # ---- Pro / subscriptions ----------------------------------------------------
    def set_pro(self, user_id: int, days: Optional[int] = None) -> None:
        """Admin grant. days=None -> lifetime (pro_until NULL)."""
        row = self.get_user(user_id)
        until = None
        if days is not None:
            base = max(now(), (row["pro_until"] or 0) if row else 0)
            until = base + days * 86400
        self._exec("UPDATE users SET is_pro=1, pro_since=COALESCE(pro_since, %s), pro_until=%s, pro_notice=0 "
                   "WHERE user_id=%s", (now(), until, user_id))

    def record_pro_payment(self, user_id: int, charge_id: str, amount: int, payload: str, *,
                           until: Optional[int] = None, days: int = 30, subscription: bool = False) -> bool:
        """
        Idempotent (unique charge id) and ATOMIC: payment row + Pro activation commit together.
        Returns False if this charge was already processed.
        """
        with self.pool.connection() as conn, conn.transaction():
            cur = conn.execute(
                "INSERT INTO payments(charge_id, user_id, amount, payload, created_at) VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (charge_id) DO NOTHING", (charge_id, user_id, amount, payload, now()))
            if cur.rowcount == 0:
                return False
            row = conn.execute("SELECT pro_until FROM users WHERE user_id=%s FOR UPDATE", (user_id,)).fetchone()
            if until is None:
                base = max(now(), (row["pro_until"] or 0) if row else 0)
                until = base + days * 86400
            conn.execute("UPDATE users SET is_pro=1, pro_since=COALESCE(pro_since, %s), pro_until=%s, "
                         "pro_notice=0 WHERE user_id=%s", (now(), until, user_id))
            if subscription:
                conn.execute("UPDATE users SET auto_renew=1, sub_charge_id=%s WHERE user_id=%s",
                             (charge_id, user_id))
            return True

    def set_auto_renew(self, user_id: int, value: bool) -> None:
        self._exec("UPDATE users SET auto_renew=%s WHERE user_id=%s", (int(value), user_id))

    def pro_expiring(self, ts: int, within_seconds: int):
        return self._all(
            "SELECT * FROM users WHERE is_pro=1 AND auto_renew=0 AND pro_notice=0 "
            "AND pro_until IS NOT NULL AND pro_until BETWEEN %s AND %s", (ts, ts + within_seconds))

    def pro_expired(self, ts: int, grace_seconds: int = 3600):
        return self._all(
            "SELECT * FROM users WHERE is_pro=1 AND pro_notice<2 "
            "AND pro_until IS NOT NULL AND pro_until < %s", (ts - grace_seconds,))

    def set_pro_notice(self, user_id: int, level: int) -> None:
        self._exec("UPDATE users SET pro_notice=%s WHERE user_id=%s", (level, user_id))

    # ------------------------------------------------------------------- links
    def count_links(self, user_id: int) -> int:
        return self._one("SELECT COUNT(*) AS n FROM links WHERE user_id=%s", (user_id,))["n"]

    def list_links(self, user_id: int):
        return self._all("SELECT * FROM links WHERE user_id=%s ORDER BY id", (user_id,))

    def add_link(self, user_id: int, platform: str, url: str) -> bool:
        return self._exec("INSERT INTO links(user_id, platform, url) VALUES (%s,%s,%s) "
                          "ON CONFLICT (user_id, url) DO NOTHING", (user_id, platform, url)) == 1

    def delete_link(self, link_id: int, user_id: int) -> None:
        self._exec("DELETE FROM links WHERE id=%s AND user_id=%s", (link_id, user_id))

    # ---------------------------------------------------------------- messages
    def add_message(self, receiver_id: int, sender_id: int, kind: str, delivered: bool = True) -> int:
        row = self._one(
            "INSERT INTO messages(receiver_id, sender_hash, sender_enc, sender_tag, kind, delivered, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (receiver_id, self.sender_hash(sender_id), self.encrypt_sender(sender_id),
             self.sender_tag(receiver_id, sender_id), kind, int(delivered), now()))
        return row["id"]

    def get_message(self, message_id: int):
        return self._one("SELECT * FROM messages WHERE id=%s", (message_id,))

    def set_message_refs(self, mid: int, card_id: Optional[int], media_id: Optional[int]) -> None:
        self._exec("UPDATE messages SET card_msg_id=%s, media_msg_id=%s WHERE id=%s", (card_id, media_id, mid))

    def mark_undelivered(self, mid: int) -> None:
        self._exec("UPDATE messages SET delivered=0 WHERE id=%s", (mid,))

    def mark_deleted(self, mid: int) -> None:
        self._exec("UPDATE messages SET deleted=1 WHERE id=%s", (mid,))

    def hide_message(self, mid: int) -> None:
        self._exec("UPDATE messages SET hidden=1 WHERE id=%s", (mid,))

    def mark_reacted(self, mid: int, emoji: str) -> bool:
        return self._exec("UPDATE messages SET reaction=%s WHERE id=%s AND reaction IS NULL", (emoji, mid)) == 1

    def bump_replies(self, mid: int, limit: int) -> bool:
        return self._exec("UPDATE messages SET replies=replies+1 WHERE id=%s AND replies<%s", (mid, limit)) == 1

    def bump_edits(self, mid: int) -> None:
        self._exec("UPDATE messages SET edits=edits+1 WHERE id=%s", (mid,))

    def recent_message_count(self, s_hash: str, window_seconds: int) -> int:
        return self._one("SELECT COUNT(*) AS n FROM messages WHERE sender_hash=%s AND created_at>%s",
                         (s_hash, now() - window_seconds))["n"]

    # ------------------------------------------------------------------- inbox
    def _inbox_clause(self, receiver_id: int, flt: str):
        if flt in _INBOX_FILTERS:
            return _INBOX_BASE + _INBOX_FILTERS[flt], [receiver_id]
        if TAG_RE.match(flt):
            return _INBOX_BASE + " AND sender_tag=%s", [receiver_id, flt]
        return _INBOX_BASE, [receiver_id]

    def count_inbox(self, receiver_id: int, flt: str = "all") -> int:
        where, params = self._inbox_clause(receiver_id, flt)
        return self._one(f"SELECT COUNT(*) AS n FROM messages WHERE {where}", params)["n"]

    def list_inbox(self, receiver_id: int, flt: str, limit: int, offset: int):
        where, params = self._inbox_clause(receiver_id, flt)
        return self._all(f"SELECT * FROM messages WHERE {where} ORDER BY id DESC LIMIT %s OFFSET %s",
                         params + [limit, offset])

    def inbox_counts(self, receiver_id: int) -> dict:
        r = self._one(
            "SELECT COUNT(*) AS c_all, "
            "COUNT(*) FILTER (WHERE kind='text') AS c_text, "
            "COUNT(*) FILTER (WHERE kind!='text') AS c_media, "
            "COUNT(*) FILTER (WHERE replies>0) AS c_replied, "
            "COUNT(*) FILTER (WHERE replies=0) AS c_noreply, "
            "COUNT(*) FILTER (WHERE reaction IS NOT NULL) AS c_reacted "
            f"FROM messages WHERE {_INBOX_BASE}", (receiver_id,))
        return {k[2:]: (v or 0) for k, v in r.items()}

    # ------------------------------------------------------- blocks / bans / reports
    def is_blocked(self, receiver_id: int, s_hash: str) -> bool:
        return self._one("SELECT 1 AS x FROM blocks WHERE receiver_id=%s AND sender_hash=%s",
                         (receiver_id, s_hash)) is not None

    def block(self, receiver_id: int, s_hash: str) -> None:
        self._exec("INSERT INTO blocks(receiver_id, sender_hash) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                   (receiver_id, s_hash))

    def is_banned(self, s_hash: str) -> bool:
        return self._one("SELECT 1 AS x FROM bans WHERE sender_hash=%s", (s_hash,)) is not None

    def ban_sender(self, s_hash: str, reason: str) -> None:
        self._exec("INSERT INTO bans(sender_hash, reason, created_at) VALUES (%s,%s,%s) "
                   "ON CONFLICT (sender_hash) DO UPDATE SET reason=EXCLUDED.reason, created_at=EXCLUDED.created_at",
                   (s_hash, reason, now()))

    def unban_sender(self, s_hash: str) -> None:
        self._exec("DELETE FROM bans WHERE sender_hash=%s", (s_hash,))

    def add_report(self, message_id: int, reporter_id: int, reason: str) -> Optional[int]:
        row = self._one(
            "INSERT INTO reports(message_id, reporter_id, reason, created_at) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (message_id, reporter_id) DO NOTHING RETURNING id",
            (message_id, reporter_id, reason, now()))
        return row["id"] if row else None

    def get_report(self, report_id: int):
        return self._one("SELECT * FROM reports WHERE id=%s", (report_id,))

    def set_report_status(self, report_id: int, status: str) -> None:
        self._exec("UPDATE reports SET status=%s WHERE id=%s", (status, report_id))

    # ------------------------------------------------------------------- stats
    def stats(self, user_id: int) -> dict:
        r = self._one(
            "SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE created_at>%s) AS week "
            "FROM messages WHERE receiver_id=%s AND delivered=1 AND deleted=0",
            (now() - 7 * 86400, user_id))
        return {
            "total": r["total"],
            "week": r["week"],
            "gifts": self._one("SELECT COUNT(*) AS n FROM gifts WHERE receiver_id=%s", (user_id,))["n"],
            "blocked": self._one("SELECT COUNT(*) AS n FROM blocks WHERE receiver_id=%s", (user_id,))["n"],
        }

    # ---------------------------------------------------------------- payments
    def record_gift(self, user_id: int, charge_id: str, amount: int, payload: str,
                    message_id: int, receiver_id: int, code: str) -> bool:
        """Idempotent + atomic (payment row and gift row commit together)."""
        with self.pool.connection() as conn, conn.transaction():
            cur = conn.execute(
                "INSERT INTO payments(charge_id, user_id, amount, payload, created_at) VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (charge_id) DO NOTHING", (charge_id, user_id, amount, payload, now()))
            if cur.rowcount == 0:
                return False
            conn.execute(
                "INSERT INTO gifts(charge_id, message_id, receiver_id, gift_code, amount, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (charge_id, message_id, receiver_id, code, amount, now()))
            return True

    # ---------------------------------------------------------------- deletion
    def delete_account(self, user_id: int) -> None:
        """/deleteme: payment records are kept for accounting, everything else goes (one transaction)."""
        h = self.sender_hash(user_id)
        with self.pool.connection() as conn, conn.transaction():
            conn.execute("UPDATE messages SET sender_enc=NULL WHERE sender_hash=%s", (h,))
            conn.execute("DELETE FROM messages WHERE receiver_id=%s", (user_id,))
            conn.execute("DELETE FROM links WHERE user_id=%s", (user_id,))
            conn.execute("DELETE FROM blocks WHERE receiver_id=%s", (user_id,))
            conn.execute("DELETE FROM gifts WHERE receiver_id=%s", (user_id,))
            conn.execute("DELETE FROM users WHERE user_id=%s", (user_id,))