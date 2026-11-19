"""旧版本数据库的就地迁移测试。"""

import os
import sqlite3

from conftest import DEFAULT_CSV, receive, intake_preview, confirm_preview
from railapp import create_app

OLD_SCHEMA = """
CREATE TABLE contracts (
    downstream    TEXT PRIMARY KEY,
    columns_json  TEXT NOT NULL,
    fingerprint   TEXT NOT NULL,
    version       INTEGER NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE TABLE days (
    day          TEXT PRIMARY KEY,
    current_rev  INTEGER NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE publications (
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
CREATE TABLE previews (
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
    created_at             TEXT NOT NULL,
    expires_at             TEXT NOT NULL,
    consumed_at            TEXT
);
"""


def test_old_database_migrates_in_place(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "app.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(OLD_SCHEMA)
    conn.commit()
    conn.close()

    app = create_app({
        "DATA_DIR": str(data_dir),
        "INBOX_DIR": str(tmp_path / "inbox"),
        "TESTING": True,
    })
    client = app.test_client()

    # The migrated schema supports the whole 交件 flow.
    inbox_dir = app.config["INBOX"].root
    receive(client, inbox_dir, "A001", DEFAULT_CSV, "2026-11-01")
    preview = intake_preview(client, "A001")
    confirmed = confirm_preview(client, preview["preview_id"])
    assert confirmed.status_code == 201, confirmed.get_json()
    assert os.path.exists(os.path.join(inbox_dir, "A001.ack.json"))

    # Upload previews (intake_id NULL) still work on the migrated table.
    upload = client.post(
        "/api/previews",
        data={
            "day": "2026-11-02",
            "file": (__import__("io").BytesIO(DEFAULT_CSV.encode()), "t.csv"),
        },
        content_type="multipart/form-data",
        headers={"Accept": "application/json"},
    )
    assert upload.status_code == 201
    assert upload.get_json()["intake_id"] is None
