"""SQLite access helpers and schema."""

import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import domain

SCHEMA = """
CREATE TABLE IF NOT EXISTS contracts (
    downstream    TEXT PRIMARY KEY,
    columns_json  TEXT NOT NULL,
    fingerprint   TEXT NOT NULL,
    version       INTEGER NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS days (
    day          TEXT PRIMARY KEY,
    current_rev  INTEGER NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publications (
    day                    TEXT NOT NULL,
    rev                    INTEGER NOT NULL,
    original_name          TEXT NOT NULL,
    original_size          INTEGER NOT NULL,
    original_sha256        TEXT NOT NULL,
    settlement_sha256      TEXT NOT NULL,
    settlement_size        INTEGER NOT NULL,
    ridership_sha256       TEXT NOT NULL,
    ridership_size         INTEGER NOT NULL,
    settlement_fingerprint TEXT NOT NULL,
    ridership_fingerprint  TEXT NOT NULL,
    settlement_version     INTEGER NOT NULL,
    ridership_version      INTEGER NOT NULL,
    settlement_columns     TEXT NOT NULL,
    ridership_columns      TEXT NOT NULL,
    row_count              INTEGER NOT NULL,
    created_at             TEXT NOT NULL,
    PRIMARY KEY (day, rev)
);

CREATE TABLE IF NOT EXISTS previews (
    id                     TEXT PRIMARY KEY,
    day                    TEXT NOT NULL,
    base_rev               INTEGER NOT NULL,
    settlement_version     INTEGER NOT NULL,
    ridership_version      INTEGER NOT NULL,
    original_name          TEXT NOT NULL,
    original_size          INTEGER NOT NULL,
    original_sha256        TEXT NOT NULL,
    result_json            TEXT NOT NULL,
    convertible            INTEGER NOT NULL,
    intake_id              TEXT,
    created_at             TEXT NOT NULL,
    expires_at             TEXT NOT NULL,
    consumed_at            TEXT
);

-- 交件（共享目录收件）。交件号在收件时绑定批次日期与原件哈希；
-- 状态机：received -> ack_pending -> published。
CREATE TABLE IF NOT EXISTS intakes (
    intake_id        TEXT PRIMARY KEY,
    day              TEXT NOT NULL,
    original_name    TEXT NOT NULL,
    original_size    INTEGER NOT NULL,
    original_sha256  TEXT NOT NULL,
    status           TEXT NOT NULL,
    published_day    TEXT,
    published_rev    INTEGER,
    published_at     TEXT,
    ack_written_at   TEXT,
    conflict_kind    TEXT,
    conflict_detail  TEXT,
    conflict_seen_at TEXT,
    received_at      TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_publications_day ON publications(day, rev);
CREATE INDEX IF NOT EXISTS idx_previews_expiry ON previews(expires_at);
CREATE INDEX IF NOT EXISTS idx_intakes_status ON intakes(status);
"""

INTAKE_RECEIVED = "received"
INTAKE_ACK_PENDING = "ack_pending"
INTAKE_PUBLISHED = "published"
INTAKE_STATUSES = (INTAKE_RECEIVED, INTAKE_ACK_PENDING, INTAKE_PUBLISHED)

PREVIEW_TTL_SECONDS = 2 * 60 * 60


def utcnow():
    return datetime.now(timezone.utc).replace(microsecond=0)


def iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


def connect(path):
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


@contextmanager
def transaction(conn, *, immediate=False):
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def init_db(conn):
    conn.executescript(SCHEMA)
    _migrate(conn)
    now = iso(utcnow())
    for downstream in domain.DOWNSTREAM_ORDER:
        columns = domain.DEFAULT_CONTRACTS[downstream]
        conn.execute(
            "INSERT OR IGNORE INTO contracts VALUES (?, ?, ?, ?, ?)",
            (
                downstream,
                json.dumps(columns, ensure_ascii=False),
                domain.fingerprint(columns),
                1,
                now,
            ),
        )


def _migrate(conn):
    """Bring databases created by older versions up to date, in place."""
    preview_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(previews)")
    }
    if "intake_id" not in preview_columns:
        conn.execute("ALTER TABLE previews ADD COLUMN intake_id TEXT")


def get_contract(conn, downstream):
    row = conn.execute(
        """SELECT downstream, columns_json, fingerprint, version, updated_at
           FROM contracts WHERE downstream = ?""",
        (downstream,),
    ).fetchone()
    if row is None:
        return None
    return {
        "downstream": row["downstream"],
        "columns": json.loads(row["columns_json"]),
        "fingerprint": row["fingerprint"],
        "version": int(row["version"]),
        "updated_at": row["updated_at"],
    }


def get_contracts(conn):
    return {name: get_contract(conn, name) for name in domain.DOWNSTREAM_ORDER}


def update_contract(conn, downstream, columns):
    """Replace a contract and bump its version.  Never touches publications.

    A no-op edit (identical canonical columns) does not bump the version, so
    it cannot invalidate previews that are still pending confirmation.
    """
    fp = domain.fingerprint(columns)
    current = get_contract(conn, downstream)
    if current is not None and current["fingerprint"] == fp:
        return current
    conn.execute(
        """UPDATE contracts
           SET columns_json = ?, fingerprint = ?, version = version + 1, updated_at = ?
           WHERE downstream = ?""",
        (json.dumps(columns, ensure_ascii=False), fp, iso(utcnow()), downstream),
    )
    return get_contract(conn, downstream)


def day_revision(conn, day):
    row = conn.execute("SELECT current_rev FROM days WHERE day = ?", (day,)).fetchone()
    return int(row["current_rev"]) if row else 0


def get_publication(conn, day, rev):
    row = conn.execute(
        "SELECT * FROM publications WHERE day = ? AND rev = ?", (day, rev)
    ).fetchone()
    return dict(row) if row else None


def list_publications(conn, day, limit=200):
    rows = conn.execute(
        "SELECT * FROM publications WHERE day = ? ORDER BY rev DESC LIMIT ?", (day, limit)
    ).fetchall()
    return [dict(r) for r in rows]


def list_days(conn, limit=200):
    rows = conn.execute(
        """SELECT d.day, d.current_rev, d.updated_at,
                  (SELECT count(*) FROM publications p WHERE p.day = d.day) AS revision_count
           FROM days d ORDER BY d.day DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def create_preview(conn, *, preview_id, day, base_rev, contract_versions,
                   original_name, original_size, original_sha256, result,
                   convertible, intake_id=None):
    now = utcnow()
    expires = now + timedelta(seconds=PREVIEW_TTL_SECONDS)
    conn.execute(
        """INSERT INTO previews (id, day, base_rev, settlement_version,
                                 ridership_version, original_name, original_size,
                                 original_sha256, result_json, convertible,
                                 intake_id, created_at, expires_at, consumed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
        (
            preview_id,
            day,
            base_rev,
            contract_versions["settlement"],
            contract_versions["ridership"],
            original_name,
            original_size,
            original_sha256,
            json.dumps(result, ensure_ascii=False),
            1 if convertible else 0,
            intake_id,
            iso(now),
            iso(expires),
        ),
    )
    return iso(expires)


def get_preview(conn, preview_id):
    row = conn.execute("SELECT * FROM previews WHERE id = ?", (preview_id,)).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["result"] = json.loads(item.pop("result_json"))
    return item


def delete_preview(conn, preview_id):
    conn.execute("DELETE FROM previews WHERE id = ?", (preview_id,))


def purge_expired_previews(conn, now=None):
    now = now or utcnow()
    rows = conn.execute(
        "SELECT id FROM previews WHERE expires_at < ?", (iso(now),)
    ).fetchall()
    ids = [r["id"] for r in rows]
    if ids:
        conn.executemany("DELETE FROM previews WHERE id = ?", [(i,) for i in ids])
    return ids


# ----------------------------------------------------------------------
# intakes （交件）
# ----------------------------------------------------------------------
def create_intake_if_absent(conn, *, intake_id, day, original_name,
                            original_size, original_sha256):
    """Insert a freshly received 交件； returns False if it already existed."""
    now = iso(utcnow())
    cursor = conn.execute(
        """INSERT OR IGNORE INTO intakes
           (intake_id, day, original_name, original_size, original_sha256,
            status, received_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            intake_id,
            day,
            original_name,
            original_size,
            original_sha256,
            INTAKE_RECEIVED,
            now,
            now,
        ),
    )
    return cursor.rowcount == 1


def get_intake(conn, intake_id):
    row = conn.execute(
        "SELECT * FROM intakes WHERE intake_id = ?", (intake_id,)
    ).fetchone()
    return dict(row) if row else None


def list_intakes(conn, status=None):
    if status is not None:
        rows = conn.execute(
            "SELECT * FROM intakes WHERE status = ? ORDER BY received_at DESC, intake_id",
            (status,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM intakes ORDER BY received_at DESC, intake_id"
        ).fetchall()
    return [dict(r) for r in rows]


def set_intake_conflict(conn, intake_id, kind, detail):
    conn.execute(
        """UPDATE intakes
           SET conflict_kind = ?, conflict_detail = ?, conflict_seen_at = ?,
               updated_at = ?
           WHERE intake_id = ?""",
        (kind, detail, iso(utcnow()), iso(utcnow()), intake_id),
    )


def clear_intake_conflict(conn, intake_id):
    conn.execute(
        """UPDATE intakes
           SET conflict_kind = NULL, conflict_detail = NULL,
               conflict_seen_at = NULL, updated_at = ?
           WHERE intake_id = ?""",
        (iso(utcnow()), intake_id),
    )


def mark_intake_published(conn, intake_id, day, rev, published_at):
    """Record the formal revision inside the publish transaction.

    The status moves to ``ack_pending``: the publication is durable, but the
    station has no receipt yet.  Only a successful ack-file write moves the
    intake to ``published``.
    """
    cursor = conn.execute(
        """UPDATE intakes
           SET status = ?, published_day = ?, published_rev = ?,
               published_at = ?, updated_at = ?
           WHERE intake_id = ? AND status = ?""",
        (
            INTAKE_ACK_PENDING,
            day,
            rev,
            published_at,
            iso(utcnow()),
            intake_id,
            INTAKE_RECEIVED,
        ),
    )
    return cursor.rowcount == 1


def mark_ack_written(conn, intake_id):
    conn.execute(
        """UPDATE intakes
           SET status = ?, ack_written_at = ?, updated_at = ?
           WHERE intake_id = ? AND status = ?""",
        (INTAKE_PUBLISHED, iso(utcnow()), iso(utcnow()), intake_id,
         INTAKE_ACK_PENDING),
    )


def wait_for_write_lock(conn, attempts=200, delay=0.05):
    """Best-effort helper used by tests to serialise around a busy database."""
    for _ in range(attempts):
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("ROLLBACK")
            return
        except sqlite3.OperationalError:
            time.sleep(delay)
    raise sqlite3.OperationalError("database is locked")
