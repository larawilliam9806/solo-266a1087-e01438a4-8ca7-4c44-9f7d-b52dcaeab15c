"""交件（共享目录收件）流程测试。"""

import hashlib
import json
import os
import threading

from conftest import (
    DEFAULT_CSV,
    confirm_preview,
    drop_inbox_file,
    intake_preview,
    preview_and_confirm,
    receive,
    scan,
)
from railapp import create_app, db


def _read_ack(inbox_dir, intake_id):
    with open(os.path.join(inbox_dir, intake_id + ".ack.json"), "rb") as handle:
        return json.loads(handle.read().decode("utf-8"))


def _intake(client, intake_id):
    payload = client.get("/api/intakes").get_json()
    for item in payload["intakes"]:
        if item["intake_id"] == intake_id:
            return item
    return None


def test_scan_skips_csv_without_ready(client, inbox_dir):
    drop_inbox_file(inbox_dir, "A001", DEFAULT_CSV)  # no .ready yet
    result = scan(client)
    assert result["received"] == []
    assert result["skipped_no_ready"] == ["A001"]
    assert _intake(client, "A001") is None
    # Once the marker lands, the next scan receives it.
    drop_inbox_file(inbox_dir, "A001", ready_day="2026-11-01")
    result = scan(client)
    assert result["received"] == ["A001"]
    assert _intake(client, "A001")["status"] == "received"


def test_receipt_binds_day_and_original(client, inbox_dir):
    receive(client, inbox_dir, "A001", ready_day="2026-11-01")
    intake = _intake(client, "A001")
    assert intake["day"] == "2026-11-01"
    assert intake["original"]["sha256"] == hashlib.sha256(DEFAULT_CSV.encode()).hexdigest()
    assert intake["original"]["name"] == "A001.csv"
    assert intake["conflict"] is None
    # A second scan with unchanged files neither re-receives nor conflicts.
    result = scan(client)
    assert result["received"] == []
    assert result["known"] == ["A001"]
    assert result["conflicts"] == []
    assert len(client.get("/api/intakes").get_json()["intakes"]) == 1


def test_rescan_reports_day_change_conflict(client, inbox_dir):
    receive(client, inbox_dir, "A001", ready_day="2026-11-01")
    drop_inbox_file(inbox_dir, "A001", ready_day="2026-11-02")
    result = scan(client)
    assert [c["intake_id"] for c in result["conflicts"]] == ["A001"]
    assert result["conflicts"][0]["kind"] == "day_changed"
    intake = _intake(client, "A001")
    assert intake["conflict"]["kind"] == "day_changed"
    # The original binding is kept.
    assert intake["day"] == "2026-11-01"
    # Restoring the original marker clears the conflict on the next scan.
    drop_inbox_file(inbox_dir, "A001", ready_day="2026-11-01")
    result = scan(client)
    assert result["conflicts"] == []
    assert _intake(client, "A001")["conflict"] is None


def test_rescan_reports_content_change_conflict(client, inbox_dir):
    receive(client, inbox_dir, "A001")
    changed = DEFAULT_CSV.replace("T1,P1", "T1,P9")
    drop_inbox_file(inbox_dir, "A001", changed, "2026-11-01")
    result = scan(client)
    assert [c["kind"] for c in result["conflicts"]] == ["content_changed"]
    intake = _intake(client, "A001")
    assert intake["conflict"]["kind"] == "content_changed"
    # The bound original is still the first-received bytes.
    assert intake["original"]["sha256"] == hashlib.sha256(DEFAULT_CSV.encode()).hexdigest()


def test_invalid_ready_and_bad_files_are_reported(client, inbox_dir):
    drop_inbox_file(inbox_dir, "B001", DEFAULT_CSV, "not-a-date")
    drop_inbox_file(inbox_dir, "B002", "", "2026-11-01")  # empty csv
    drop_inbox_file(inbox_dir, "B003", DEFAULT_CSV, "2026-13-40")  # impossible date
    result = scan(client)
    assert result["received"] == []
    errored = {e["intake_id"] for e in result["errors"]}
    assert errored == {"B001", "B002", "B003"}
    assert client.get("/api/intakes").get_json()["intakes"] == []


def test_oversized_csv_not_received(client, inbox_dir):
    big = b"a,b\n" + b"x,y\n" * (1024 * 1024 // 4 + 10)
    drop_inbox_file(inbox_dir, "BIG1", big, "2026-11-01")
    result = scan(client)
    assert result["received"] == []
    assert result["errors"][0]["intake_id"] == "BIG1"
    assert "1 MB" in result["errors"][0]["error"]


def test_preview_and_confirm_publishes_and_writes_ack(client, inbox_dir):
    receive(client, inbox_dir, "A001", ready_day="2026-11-01")
    preview = intake_preview(client, "A001")
    assert preview["intake_id"] == "A001"
    assert preview["day"] == "2026-11-01"
    assert preview["convertible"] is True

    confirmed = confirm_preview(client, preview["preview_id"])
    assert confirmed.status_code == 201, confirmed.get_json()
    published = confirmed.get_json()
    assert published["rev"] == 1
    assert published["intake"]["ack_written"] is True
    assert published["intake"]["revision"] == 1

    ack = _read_ack(inbox_dir, "A001")
    assert ack["intake_id"] == "A001"
    assert ack["day"] == "2026-11-01"
    assert ack["revision"] == 1
    assert ack["original_sha256"] == hashlib.sha256(DEFAULT_CSV.encode()).hexdigest()
    assert ack["row_count"] == 3

    intake = _intake(client, "A001")
    assert intake["status"] == "published"
    assert intake["published_rev"] == 1
    assert intake["ack_written_at"]

    # The publication is a normal revision: three files downloadable.
    for kind in ("original", "settlement", "ridership"):
        assert client.get(f"/download/2026-11-01/1/{kind}").status_code == 200
    assert client.get("/download/2026-11-01/1/original").data == DEFAULT_CSV.encode()


def test_failed_publish_writes_no_ack(client, inbox_dir):
    # Missing required columns -> preview is not convertible -> confirm 422.
    bad = "ticket_id,train_no\nT1,G1\n"
    receive(client, inbox_dir, "A001", bad)
    preview = intake_preview(client, "A001")
    assert preview["convertible"] is False
    confirmed = confirm_preview(client, preview["preview_id"])
    assert confirmed.status_code == 422
    assert confirmed.get_json()["error"] == "incompatible"
    assert not os.path.exists(os.path.join(inbox_dir, "A001.ack.json"))
    intake = _intake(client, "A001")
    assert intake["status"] == "received"
    assert intake["published_rev"] is None
    assert client.get("/api/days/2026-11-01").get_json()["current_rev"] == 0


def test_same_intake_publishes_at_most_once(client, inbox_dir):
    receive(client, inbox_dir, "A001")
    first = intake_preview(client, "A001")
    second = intake_preview(client, "A001")
    assert first["preview_id"] != second["preview_id"]

    one = confirm_preview(client, first["preview_id"])
    two = confirm_preview(client, second["preview_id"])
    assert one.status_code == 201
    assert two.status_code == 409
    assert two.get_json()["error"] in {"stale_day_revision", "intake_already_published"}

    day = client.get("/api/days/2026-11-01").get_json()
    assert day["current_rev"] == 1
    assert len(day["revisions"]) == 1
    assert _intake(client, "A001")["status"] == "published"

    # Re-previewing a published intake is refused.
    again = client.post("/api/intakes/A001/previews", headers={"Accept": "application/json"})
    assert again.status_code == 409
    assert again.get_json()["error"] == "intake_already_published"


def test_concurrent_scan_and_confirm_publish_once(client, inbox_dir, app):
    receive(client, inbox_dir, "A001")
    preview = intake_preview(client, "A001")

    results = []
    barrier = threading.Barrier(5)

    def confirm():
        local = app.test_client()
        barrier.wait()
        response = local.post(
            f"/api/previews/{preview['preview_id']}/confirm",
            headers={"Accept": "application/json"},
        )
        results.append(("confirm", response.status_code))

    def rescan():
        local = app.test_client()
        barrier.wait()
        response = local.post("/api/inbox/scan", headers={"Accept": "application/json"})
        results.append(("scan", response.status_code))

    threads = [
        threading.Thread(target=confirm),
        threading.Thread(target=confirm),
        threading.Thread(target=rescan),
        threading.Thread(target=rescan),
        threading.Thread(target=confirm),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    confirm_codes = sorted(code for kind, code in results if kind == "confirm")
    assert confirm_codes.count(201) == 1, results
    assert all(code == 200 for kind, code in results if kind == "scan")
    day = client.get("/api/days/2026-11-01").get_json()
    assert day["current_rev"] == 1
    assert len(day["revisions"]) == 1
    # Exactly one ack, for revision 1.
    ack = _read_ack(inbox_dir, "A001")
    assert ack["revision"] == 1
    assert _intake(client, "A001")["status"] == "published"


def test_ack_pending_rewritten_with_original_revision(client, inbox_dir, app, tmp_path):
    """Publish succeeds but the ack write fails -> 待补写； a later scan rewrites
    the receipt with the original revision, even across a restart and even
    after a newer revision was published for the same day."""
    receive(client, inbox_dir, "A001", ready_day="2026-11-01")
    preview = intake_preview(client, "A001")

    service = app.extensions["service"]
    failing = service.inbox.write_ack

    def broken(*args, **kwargs):
        raise OSError("shared directory read-only")

    service.inbox.write_ack = broken
    confirmed = confirm_preview(client, preview["preview_id"])
    assert confirmed.status_code == 201, confirmed.get_json()
    payload = confirmed.get_json()
    assert payload["rev"] == 1
    assert payload["intake"]["ack_written"] is False
    assert not os.path.exists(os.path.join(inbox_dir, "A001.ack.json"))

    # Web/API surface shows 待补写.
    intake = _intake(client, "A001")
    assert intake["status"] == "ack_pending"
    assert intake["published_rev"] == 1

    # A newer revision for the same day is published meanwhile (upload path).
    preview_and_confirm(client, day="2026-11-01", data=DEFAULT_CSV.replace("T1", "T9").encode())
    assert client.get("/api/days/2026-11-01").get_json()["current_rev"] == 2

    # Restart the service against the same directories; the failed-write
    # monkeypatch is gone with the old instance.
    restarted = create_app({
        "DATA_DIR": app.config["DATA_DIR"],
        "INBOX_DIR": inbox_dir,
        "TESTING": True,
    })
    client2 = restarted.test_client()
    intake = _intake(client2, "A001")
    assert intake["status"] == "ack_pending"  # survived the restart

    result = scan(client2)
    assert result["ack_rewritten"] == ["A001"]
    ack = _read_ack(inbox_dir, "A001")
    # The receipt names the original revision, not the day's current one.
    assert ack["revision"] == 1
    assert ack["day"] == "2026-11-01"
    assert ack["original_sha256"] == hashlib.sha256(DEFAULT_CSV.encode()).hexdigest()
    assert _intake(client2, "A001")["status"] == "published"
    # The day pointer is untouched by the rewrite.
    assert client2.get("/api/days/2026-11-01").get_json()["current_rev"] == 2


def test_ack_rewrite_keeps_failing_gracefully(client, inbox_dir, app):
    receive(client, inbox_dir, "A001")
    preview = intake_preview(client, "A001")
    service = app.extensions["service"]
    service.inbox.write_ack = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
    confirmed = confirm_preview(client, preview["preview_id"])
    assert confirmed.status_code == 201
    result = scan(client)
    assert result["ack_failed"] and result["ack_failed"][0]["intake_id"] == "A001"
    assert _intake(client, "A001")["status"] == "ack_pending"
    # Manual retry endpoint surfaces the failure too.
    retry = client.post("/api/intakes/A001/ack", headers={"Accept": "application/json"})
    assert retry.status_code == 503


def test_retry_ack_endpoint(client, inbox_dir):
    receive(client, inbox_dir, "A001")
    # Not published yet -> nothing to rewrite.
    early = client.post("/api/intakes/A001/ack", headers={"Accept": "application/json"})
    assert early.status_code == 409
    preview = intake_preview(client, "A001")
    confirm_preview(client, preview["preview_id"])
    ok = client.post("/api/intakes/A001/ack", headers={"Accept": "application/json"})
    assert ok.status_code == 200
    assert ok.get_json()["revision"] == 1


def test_missing_ack_rewritten_while_sources_remain(client, inbox_dir):
    receive(client, inbox_dir, "A001")
    preview = intake_preview(client, "A001")
    confirm_preview(client, preview["preview_id"])
    os.unlink(os.path.join(inbox_dir, "A001.ack.json"))
    result = scan(client)
    assert result["ack_rewritten"] == ["A001"]
    assert _read_ack(inbox_dir, "A001")["revision"] == 1


def test_corrupt_ack_rewritten(client, inbox_dir):
    receive(client, inbox_dir, "A001")
    preview = intake_preview(client, "A001")
    confirm_preview(client, preview["preview_id"])
    with open(os.path.join(inbox_dir, "A001.ack.json"), "w") as handle:
        handle.write("{not json")
    result = scan(client)
    assert result["ack_rewritten"] == ["A001"]
    assert _read_ack(inbox_dir, "A001")["revision"] == 1


def test_cleaned_drop_is_not_resurrected(client, inbox_dir):
    """Once the station verified the ack and cleaned everything up, scans
    must not recreate files in the shared directory."""
    receive(client, inbox_dir, "A001")
    preview = intake_preview(client, "A001")
    confirm_preview(client, preview["preview_id"])
    for suffix in (".csv", ".ready", ".ack.json"):
        os.unlink(os.path.join(inbox_dir, "A001" + suffix))
    result = scan(client)
    assert result["ack_rewritten"] == []
    assert not os.path.exists(os.path.join(inbox_dir, "A001.ack.json"))
    intake = _intake(client, "A001")
    assert intake["status"] == "published"
    assert intake["cleaned"] is True


def test_expired_preview_can_be_recreated_from_received_original(client, inbox_dir, app):
    receive(client, inbox_dir, "A001", ready_day="2026-11-01")
    preview = intake_preview(client, "A001")
    # Expire the preview; confirming it now fails.
    conn = db.connect(app.config["DB_PATH"])
    try:
        conn.execute(
            "UPDATE previews SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00Z", preview["preview_id"]),
        )
    finally:
        conn.close()
    expired = confirm_preview(client, preview["preview_id"])
    assert expired.status_code == 409
    assert expired.get_json()["error"] == "preview_expired"

    # The station's files are gone; the received original still serves a
    # fresh preview, which can then be published.
    os.unlink(os.path.join(inbox_dir, "A001.csv"))
    os.unlink(os.path.join(inbox_dir, "A001.ready"))
    fresh = intake_preview(client, "A001")
    assert fresh["preview_id"] != preview["preview_id"]
    confirmed = confirm_preview(client, fresh["preview_id"])
    assert confirmed.status_code == 201, confirmed.get_json()
    assert client.get("/download/2026-11-01/1/original").data == DEFAULT_CSV.encode()
    assert _read_ack(inbox_dir, "A001")["revision"] == 1


def test_intake_preview_uses_bound_original_not_directory(client, inbox_dir):
    """A conflicting rewrite in the directory does not leak into previews:
    the 交件 keeps publishing the bytes bound at receipt."""
    receive(client, inbox_dir, "A001")
    drop_inbox_file(inbox_dir, "A001", DEFAULT_CSV.replace("T1", "TX"), "2026-11-01")
    result = scan(client)
    assert [c["kind"] for c in result["conflicts"]] == ["content_changed"]
    preview = intake_preview(client, "A001")
    confirmed = confirm_preview(client, preview["preview_id"])
    assert confirmed.status_code == 201
    original = client.get("/download/2026-11-01/1/original").data
    assert original == DEFAULT_CSV.encode()
    assert _read_ack(inbox_dir, "A001")["original_sha256"] == (
        hashlib.sha256(DEFAULT_CSV.encode()).hexdigest()
    )


def test_two_intakes_same_day_get_separate_revisions(client, inbox_dir):
    receive(client, inbox_dir, "A001", ready_day="2026-11-01")
    receive(client, inbox_dir, "A002", DEFAULT_CSV.replace("T1", "T2"), "2026-11-01")
    first = confirm_preview(client, intake_preview(client, "A001")["preview_id"])
    assert first.status_code == 201 and first.get_json()["rev"] == 1
    # A002's preview was created after A001 published, so it bases on r1.
    second_preview = intake_preview(client, "A002")
    assert second_preview["base_rev"] == 1
    second = confirm_preview(client, second_preview["preview_id"])
    assert second.status_code == 201 and second.get_json()["rev"] == 2
    assert _read_ack(inbox_dir, "A001")["revision"] == 1
    assert _read_ack(inbox_dir, "A002")["revision"] == 2


def test_inbox_page_and_api_render(client, inbox_dir):
    assert client.get("/inbox").status_code == 200
    receive(client, inbox_dir, "A001")
    payload = client.get("/api/intakes").get_json()
    assert payload["inbox_dir"] == inbox_dir
    assert [i["intake_id"] for i in payload["intakes"]] == ["A001"]


def test_unknown_intake_preview_404(client):
    response = client.post("/api/intakes/NOPE/previews", headers={"Accept": "application/json"})
    assert response.status_code == 404
    assert response.get_json()["error"] == "intake_not_found"


def test_invalid_intake_id_reported(client, inbox_dir):
    drop_inbox_file(inbox_dir, ".hidden", DEFAULT_CSV, "2026-11-01")
    result = scan(client)
    assert result["received"] == []
    assert result["invalid"]
    assert result["invalid"][0]["file"] == ".hidden.csv"
