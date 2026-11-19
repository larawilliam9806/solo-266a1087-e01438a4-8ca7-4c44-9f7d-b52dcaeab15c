"""Preview and publish orchestration.

Consistency model
-----------------
* A ``publication`` row names exactly three immutable blobs (original +
  settlement + ridership) and is inserted in one SQLite transaction together
  with the day's ``current_rev`` pointer.  Readers therefore never observe a
  revision with files from another revision.
* Blobs are written and fsynced *before* the transaction commits.  A crash or
  failed write leaves unreferenced blobs only; the previous revision remains
  current and fully downloadable.
* Confirmations take ``BEGIN IMMEDIATE`` and re-validate the preview snapshot
  (day revision + both contract versions) inside the lock, so out of two
  concurrent confirmations for one day exactly one succeeds.
* Publishing never reads the contract at download time: a revision's column
  contract is frozen into the row and content-addressed files.

交件 (shared-directory intake)
------------------------------
* Receipt binds a 交件号 to its batch date and original bytes exactly once;
  the original is copied into the blob store at receipt, so previews and
  publishes never depend on the mutable shared directory again.
* The intake row flips ``received -> ack_pending`` inside the same
  transaction as the publication, so a 交件号 publishes at most once and a
  failed publish can never leave an ack behind.
* The ack file is written only after the publish commits; a failed write
  leaves ``ack_pending`` （待补写） and the next scan rewrites the receipt
  with the revision recorded at publish time.
"""

import hashlib
import json
import os
import sqlite3
import uuid

from . import convert, db, domain
from .errors import AppError, Conflict, NotFound, Unprocessable, Unavailable
from .inbox import InboxProblem, InboxStore, validate_intake_id
from .storage import BlobStore

KINDS = ("original", "settlement", "ridership")

# A committed publish must never be reported as failed just because the ack
# bookkeeping (shared-dir write or status update) ran into trouble; the intake
# stays "ack_pending" and the next scan heals it.
_ACK_FAILURES = (OSError, sqlite3.Error)


def _check_day(day):
    if not isinstance(day, str):
        raise AppError("日期格式必须为 YYYY-MM-DD", code="invalid_day")
    from datetime import date

    if len(day) != 10:
        raise AppError("日期格式必须为 YYYY-MM-DD", code="invalid_day")
    try:
        parsed = date.fromisoformat(day)
    except ValueError:
        raise AppError("日期不是有效日期", code="invalid_day") from None
    if parsed.isoformat() != day:
        raise AppError("日期格式必须为 YYYY-MM-DD", code="invalid_day")
    return day


def _safe_name(name):
    name = (name or "").replace("\\", "/").split("/")[-1].strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in "\r\n")
    if not name:
        name = "tickets.csv"
    return name[:255]


def _publication_payload(row):
    return {
        "day": row["day"],
        "rev": row["rev"],
        "created_at": row["created_at"],
        "row_count": row["row_count"],
        "original": {
            "name": row["original_name"],
            "size": row["original_size"],
            "sha256": row["original_sha256"],
        },
        "downstreams": {
            "settlement": {
                "sha256": row["settlement_sha256"],
                "size": row["settlement_size"],
                "fingerprint": row["settlement_fingerprint"],
                "contract_version": row["settlement_version"],
                "columns": json.loads(row["settlement_columns"]),
            },
            "ridership": {
                "sha256": row["ridership_sha256"],
                "size": row["ridership_size"],
                "fingerprint": row["ridership_fingerprint"],
                "contract_version": row["ridership_version"],
                "columns": json.loads(row["ridership_columns"]),
            },
        },
        "downloads": {
            kind: f"/download/{row['day']}/{row['rev']}/{kind}" for kind in KINDS
        },
    }


def _preview_public_payload(preview):
    result = preview["result"]
    return {
        "preview_id": preview["id"],
        "day": preview["day"],
        "base_rev": preview["base_rev"],
        "convertible": bool(preview["convertible"]),
        "intake_id": preview.get("intake_id"),
        "original": result["original"],
        "contracts": {
            name: {
                "version": result["contracts"][name]["version"],
                "fingerprint": result["contracts"][name]["fingerprint"],
                "columns": result["contracts"][name]["columns"],
            }
            for name in domain.DOWNSTREAM_ORDER
        },
        "downstreams": result["downstreams"],
        "created_at": preview["created_at"],
        "expires_at": preview["expires_at"],
    }


class Service:
    def __init__(self, conn_factory, store: BlobStore, inbox: InboxStore = None):
        self.conn_factory = conn_factory
        self.store = store
        self.inbox = inbox

    # ------------------------------------------------------------------
    # preview
    # ------------------------------------------------------------------
    def preview(self, day, filename, raw_bytes, intake_id=None):
        day = _check_day(day)
        max_bytes = convert.MAX_FILE_BYTES
        if raw_bytes is None:
            raise AppError("缺少文件", code="missing_file")
        if len(raw_bytes) > max_bytes:
            raise AppError(
                f"文件超过 1 MB 上限（{len(raw_bytes)} 字节）", status=413, code="file_too_large"
            )
        try:
            parsed = convert.parse_csv_bytes(raw_bytes)
        except convert.CsvProblem as problem:
            raise Unprocessable(
                problem.message, code="batch_rejected", details=problem.to_dict()
            ) from None

        conn = self.conn_factory()
        try:
            # One read transaction gives a consistent snapshot of both
            # contracts and the day pointer even if they change mid-preview.
            conn.execute("BEGIN")
            try:
                contracts = db.get_contracts(conn)
                base_rev = db.day_revision(conn, day)
            finally:
                conn.execute("COMMIT")
            db.purge_expired_previews(conn)
            results = {}
            convertible = True
            for name in domain.DOWNSTREAM_ORDER:
                outcome = convert.convert(parsed, contracts[name]["columns"])
                results[name] = outcome
                if outcome["status"] != "ok":
                    convertible = False
            original_name = _safe_name(filename)
            original_sha = self.store.put_bytes(raw_bytes)
            for name in domain.DOWNSTREAM_ORDER:
                outcome = results[name]
                outcome["output_size"] = len(outcome["output_bytes"])
                if outcome["output_bytes"]:
                    self.store.put_bytes(outcome["output_bytes"])
                outcome.pop("output_bytes", None)

            snapshot = {
                "original": {
                    "name": original_name,
                    "size": len(raw_bytes),
                    "sha256": original_sha,
                    "row_count": parsed.row_count,
                    "header": parsed.header,
                },
                "contracts": {
                    name: {
                        "version": contracts[name]["version"],
                        "fingerprint": contracts[name]["fingerprint"],
                        "columns": contracts[name]["columns"],
                    }
                    for name in domain.DOWNSTREAM_ORDER
                },
                "downstreams": results,
                "convertible": convertible,
            }
            preview_id = uuid.uuid4().hex
            with db.transaction(conn):
                db.create_preview(
                    conn,
                    preview_id=preview_id,
                    day=day,
                    base_rev=base_rev,
                    contract_versions={
                        name: contracts[name]["version"] for name in domain.DOWNSTREAM_ORDER
                    },
                    original_name=original_name,
                    original_size=len(raw_bytes),
                    original_sha256=original_sha,
                    result=snapshot,
                    convertible=convertible,
                    intake_id=intake_id,
                )
            preview = db.get_preview(conn, preview_id)
            return 201, _preview_public_payload(preview)
        finally:
            conn.close()

    def preview_intake(self, intake_id):
        """Preview a received 交件 using the original stored at receipt.

        The bytes always come from the blob store (the immutable copy made
        when the 交件号 was first received), never from the shared directory,
        so an expired preview can be recreated at any time even if the
        station has since rewritten or deleted its source files.
        """
        if self.inbox is None:
            raise NotFound("未配置共享目录", code="inbox_disabled")
        conn = self.conn_factory()
        try:
            intake = db.get_intake(conn, intake_id)
            if intake is None:
                raise NotFound("交件不存在，请先扫描共享目录", code="intake_not_found")
            if intake["status"] != db.INTAKE_RECEIVED:
                raise Conflict(
                    f"交件 {intake_id} 已发布（修订 r{intake['published_rev']}），"
                    "不能重复发布",
                    code="intake_already_published",
                )
        finally:
            conn.close()
        raw = self.store.read_bytes(intake["original_sha256"])
        return self.preview(
            intake["day"], intake["original_name"], raw, intake_id=intake_id
        )

    def get_preview(self, preview_id):
        conn = self.conn_factory()
        try:
            preview = db.get_preview(conn, preview_id)
            if preview is None:
                raise NotFound("预览不存在或已失效，请重新上传", code="preview_not_found")
            return _preview_public_payload(preview)
        finally:
            conn.close()

    def discard_preview(self, preview_id):
        conn = self.conn_factory()
        try:
            with db.transaction(conn, immediate=True):
                preview = db.get_preview(conn, preview_id)
                if preview is None:
                    raise NotFound("预览不存在或已失效", code="preview_not_found")
                db.delete_preview(conn, preview_id)
            return 200, {"discarded": preview_id}
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # confirm / publish
    # ------------------------------------------------------------------
    def confirm(self, preview_id):
        intake_id = None
        conn = self.conn_factory()
        try:
            with db.transaction(conn, immediate=True):
                preview = db.get_preview(conn, preview_id)
                if preview is None:
                    raise NotFound("预览不存在或已失效，请重新上传并预览", code="preview_not_found")
                if preview["consumed_at"]:
                    raise Conflict("该预览已用于发布，不能重复确认", code="preview_used")
                now = db.iso(db.utcnow())
                if preview["expires_at"] < now:
                    db.delete_preview(conn, preview_id)
                    raise Conflict("预览已过期，请重新上传并预览", code="preview_expired")

                result = preview["result"]
                day = preview["day"]
                current_rev = db.day_revision(conn, day)
                if current_rev != preview["base_rev"]:
                    raise Conflict(
                        f"该日已发布到修订号 {current_rev}，预览基于修订号 "
                        f"{preview['base_rev']}，请重新预览后再确认",
                        code="stale_day_revision",
                    )

                contracts = db.get_contracts(conn)
                for name in domain.DOWNSTREAM_ORDER:
                    snapshot = result["contracts"][name]
                    if contracts[name]["version"] != preview[name + "_version"] or (
                        contracts[name]["fingerprint"] != snapshot["fingerprint"]
                    ):
                        raise Conflict(
                            f"{domain.label_for(name)} 契约在预览后已变更，"
                            "请重新预览后再确认",
                            code="stale_contract",
                        )

                if not preview["convertible"] or not result.get("convertible"):
                    raise Unprocessable(
                        "存在不兼容的列或行，不能发布；请修正 CSV 或契约后重新预览",
                        code="incompatible",
                    )

                intake_id = preview["intake_id"]
                if intake_id is not None:
                    # The intake row is the 交件号-level guard against double
                    # publishing: only the transaction that flips the status
                    # out of "received" may publish this 交件， however many
                    # previews or concurrent scans exist for it.
                    intake = db.get_intake(conn, intake_id)
                    if intake is None:
                        raise Conflict(
                            "交件记录不存在，请重新扫描共享目录",
                            code="intake_missing",
                        )
                    if intake["status"] != db.INTAKE_RECEIVED:
                        raise Conflict(
                            f"交件 {intake_id} 已发布（修订 "
                            f"r{intake['published_rev']}），同一交件只能发布一次",
                            code="intake_already_published",
                        )
                    if (
                        intake["day"] != day
                        or intake["original_sha256"] != preview["original_sha256"]
                    ):
                        raise Conflict(
                            "交件绑定与预览不一致，请重新预览",
                            code="intake_mismatch",
                        )

                # Re-materialise from the stored original bytes and the frozen
                # contract snapshot, and verify it matches what was previewed.
                original_bytes = self.store.read_bytes(preview["original_sha256"])
                if hashlib.sha256(original_bytes).hexdigest() != preview["original_sha256"]:
                    raise Unavailable("预览原件校验失败，请重新上传并预览")
                try:
                    parsed = convert.parse_csv_bytes(original_bytes)
                except convert.CsvProblem:
                    raise Unavailable(
                        "预览原件无法再次解析，请重新上传并预览"
                    ) from None

                outputs = {}
                for name in domain.DOWNSTREAM_ORDER:
                    snapshot = result["contracts"][name]
                    outcome = convert.convert(parsed, snapshot["columns"])
                    if outcome["status"] != "ok":
                        raise Conflict(
                            "当前契约与预览结果不一致，请重新预览", code="stale_contract"
                        )
                    expected = result["downstreams"][name]["output_sha256"]
                    actual = hashlib.sha256(outcome["output_bytes"]).hexdigest()
                    if expected != actual:
                        raise Unavailable("预览结果校验失败，请重新预览")
                    outputs[name] = (actual, len(outcome["output_bytes"]), outcome["output_bytes"])
                    if not self.store.exists(actual):
                        self.store.put_bytes(outcome["output_bytes"])

                # Blobs are durable; now commit the row + pointer atomically.
                new_rev = current_rev + 1
                created_at = db.iso(db.utcnow())
                settlement_cols = json.dumps(result["contracts"]["settlement"]["columns"], ensure_ascii=False)
                ridership_cols = json.dumps(result["contracts"]["ridership"]["columns"], ensure_ascii=False)
                conn.execute(
                    """INSERT INTO publications
                       (day, rev, original_name, original_size, original_sha256,
                        settlement_sha256, settlement_size, ridership_sha256, ridership_size,
                        settlement_fingerprint, ridership_fingerprint,
                        settlement_version, ridership_version,
                        settlement_columns, ridership_columns, row_count, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        day,
                        new_rev,
                        preview["original_name"],
                        preview["original_size"],
                        preview["original_sha256"],
                        outputs["settlement"][0],
                        outputs["settlement"][1],
                        outputs["ridership"][0],
                        outputs["ridership"][1],
                        result["contracts"]["settlement"]["fingerprint"],
                        result["contracts"]["ridership"]["fingerprint"],
                        result["contracts"]["settlement"]["version"],
                        result["contracts"]["ridership"]["version"],
                        settlement_cols,
                        ridership_cols,
                        parsed.row_count,
                        created_at,
                    ),
                )
                if current_rev == 0:
                    conn.execute(
                        "INSERT INTO days (day, current_rev, updated_at) VALUES (?, ?, ?)",
                        (day, new_rev, created_at),
                    )
                else:
                    conn.execute(
                        "UPDATE days SET current_rev = ?, updated_at = ? WHERE day = ?",
                        (new_rev, created_at, day),
                    )
                if intake_id is not None:
                    # Same transaction as the publication: if the publish
                    # rolls back, the 交件 stays "received" and no ack will
                    # ever be written for it.
                    db.mark_intake_published(conn, intake_id, day, new_rev, created_at)
                conn.execute(
                    "UPDATE previews SET consumed_at = ? WHERE id = ?",
                    (created_at, preview_id),
                )
                # The preview row is now consumed; publications are the only
                # thing downloads resolve against, so the preview can go.
                db.delete_preview(conn, preview_id)
                row = db.get_publication(conn, day, new_rev)
        finally:
            conn.close()
        payload = _publication_payload(row)
        if intake_id is not None:
            # The publish is committed; the ack is best-effort afterwards.
            # A failed write leaves the intake in "ack_pending" （待补写） and
            # the next scan rewrites the receipt with the same revision.
            payload["intake"] = self._finalize_ack(intake_id)
        return 201, payload

    # ------------------------------------------------------------------
    # inbox scanning / 交件
    # ------------------------------------------------------------------
    def _require_inbox(self):
        if self.inbox is None:
            raise Unavailable("未配置共享目录（INBOX_DIR）", code="inbox_disabled")
        return self.inbox

    def scan_inbox(self):
        """Scan the shared directory once.

        * CSVs without a ``.ready`` marker are skipped (station still writing).
        * New 交件号 are received: the batch date and the original bytes are
          bound once and never re-bound.
        * Already-known 交件号 are reconciled: a changed date or content is
          reported as a conflict; the bound original stays authoritative.
        * Intakes whose ack was never written （待补写） get the receipt
          rewritten with the revision recorded at publish time.
        """
        inbox = self._require_inbox()
        result = {
            "scanned_at": db.iso(db.utcnow()),
            "inbox_dir": inbox.root,
            "received": [],
            "known": [],
            "conflicts": [],
            "skipped_no_ready": [],
            "invalid": [],
            "ack_rewritten": [],
            "ack_failed": [],
            "errors": [],
        }
        conn = self.conn_factory()
        try:
            # -- 1. ack receipts we still owe ----------------------------
            for intake in db.list_intakes(conn):
                iid = intake["intake_id"]
                if intake["status"] == db.INTAKE_ACK_PENDING:
                    self._rewrite_ack(conn, intake, result)
                elif intake["status"] == db.INTAKE_PUBLISHED:
                    # Heal a receipt that disappeared or was damaged after we
                    # recorded it — but only while the station's source files
                    # are still there (a cleaned-up drop needs nothing).
                    if not self.inbox.sources_present(iid):
                        continue
                    expected = self._ack_payload(conn, intake)
                    if self.inbox.read_ack(iid) != expected:
                        self._rewrite_ack(conn, intake, result)

            # -- 2. directory reconciliation ------------------------------
            try:
                entries = inbox.scan_entries()
            except InboxProblem as exc:
                raise Unavailable(exc.message, code="inbox_unreadable") from None
            known = {
                row["intake_id"]: row for row in db.list_intakes(conn)
            }
            for stem in sorted(entries):
                item = entries[stem]
                if not item["csv"]:
                    continue  # stray .ready/.ack.json without a CSV: ignore
                problem = validate_intake_id(stem)
                if problem:
                    result["invalid"].append({"file": stem + ".csv", "reason": problem})
                    continue
                if not item["ready"]:
                    # Not ready yet; only mention drops we never received.
                    if stem not in known:
                        result["skipped_no_ready"].append(stem)
                    continue
                try:
                    ready_day = inbox.read_ready_date(inbox.ready_path(stem))
                except InboxProblem as exc:
                    result["errors"].append({"intake_id": stem, "error": exc.message})
                    continue
                existing = known.get(stem)
                if existing is None:
                    existing = self._receive_intake(conn, inbox, stem, ready_day, result)
                    if existing is None:
                        continue
                    known[stem] = existing
                else:
                    self._reconcile_intake(conn, inbox, existing, ready_day, result)
            result["intakes"] = [
                self._intake_payload(conn, row) for row in db.list_intakes(conn)
            ]
            return result
        finally:
            conn.close()

    def _receive_intake(self, conn, inbox, stem, ready_day, result):
        """Bind a new 交件号 to its date and original bytes (first receipt)."""
        try:
            raw = inbox.read_csv(inbox.csv_path(stem))
        except InboxProblem as exc:
            result["errors"].append({"intake_id": stem, "error": exc.message})
            return None
        sha = hashlib.sha256(raw).hexdigest()
        try:
            # The received original is copied into the immutable blob store,
            # so later previews and the publish never depend on the shared
            # directory again.
            self.store.put_bytes(raw)
        except OSError as exc:
            result["errors"].append(
                {"intake_id": stem, "error": f"原件入库失败：{exc}"}
            )
            return None
        with db.transaction(conn, immediate=True):
            created = db.create_intake_if_absent(
                conn,
                intake_id=stem,
                day=ready_day,
                original_name=stem + ".csv",
                original_size=len(raw),
                original_sha256=sha,
            )
            intake = db.get_intake(conn, stem)
        if created:
            result["received"].append(stem)
            return intake
        # A concurrent scan received it first; reconcile against the winner's
        # binding instead of creating a second record.
        self._reconcile_intake(conn, inbox, intake, ready_day, result)
        return intake

    def _reconcile_intake(self, conn, inbox, intake, ready_day, result):
        """Compare a known 交件号 with what is currently in the directory."""
        iid = intake["intake_id"]
        if ready_day != intake["day"]:
            self._flag_conflict(
                conn,
                result,
                intake,
                "day_changed",
                f"共享目录中的批次日期 {ready_day} 与收件绑定的 {intake['day']} 不一致",
            )
            return
        try:
            current_sha = inbox.hash_csv(inbox.csv_path(iid))
        except InboxProblem as exc:
            result["errors"].append({"intake_id": iid, "error": exc.message})
            return
        if current_sha != intake["original_sha256"]:
            self._flag_conflict(
                conn,
                result,
                intake,
                "content_changed",
                "共享目录中的 CSV 内容与收件时的原件不一致；收件原件不变",
            )
            return
        if intake["conflict_kind"]:
            with db.transaction(conn, immediate=True):
                db.clear_intake_conflict(conn, iid)
            intake["conflict_kind"] = None
            intake["conflict_detail"] = None
            intake["conflict_seen_at"] = None
        result["known"].append(iid)

    def _flag_conflict(self, conn, result, intake, kind, detail):
        iid = intake["intake_id"]
        with db.transaction(conn, immediate=True):
            db.set_intake_conflict(conn, iid, kind, detail)
        intake["conflict_kind"] = kind
        intake["conflict_detail"] = detail
        result["conflicts"].append(
            {"intake_id": iid, "kind": kind, "detail": detail}
        )

    # ------------------------------------------------------------------
    # ack receipts
    # ------------------------------------------------------------------
    def _ack_payload(self, conn, intake):
        publication = None
        if intake["published_day"] and intake["published_rev"]:
            publication = db.get_publication(
                conn, intake["published_day"], intake["published_rev"]
            )
        return {
            "intake_id": intake["intake_id"],
            "day": intake["published_day"],
            "revision": intake["published_rev"],
            "original_sha256": intake["original_sha256"],
            "original_size": intake["original_size"],
            "row_count": publication["row_count"] if publication else None,
            "published_at": intake["published_at"],
        }

    def _write_ack_and_mark(self, conn, intake):
        """Write the ack file, then record it; raises _ACK_FAILURES on failure."""
        self.inbox.write_ack(intake["intake_id"], self._ack_payload(conn, intake))
        with db.transaction(conn, immediate=True):
            db.mark_ack_written(conn, intake["intake_id"])

    def _rewrite_ack(self, conn, intake, result):
        """Scan-time rewrite of a missing receipt with the original revision."""
        iid = intake["intake_id"]
        try:
            self._write_ack_and_mark(conn, intake)
        except _ACK_FAILURES as exc:
            result["ack_failed"].append({"intake_id": iid, "error": str(exc)})
        else:
            result["ack_rewritten"].append(iid)

    def _finalize_ack(self, intake_id):
        """Post-publish ack attempt; the publish itself is already durable."""
        conn = self.conn_factory()
        try:
            intake = db.get_intake(conn, intake_id)
            if intake is None or intake["published_rev"] is None:
                return {
                    "intake_id": intake_id,
                    "ack_written": False,
                    "ack_error": "交件没有已发布的修订",
                }
            try:
                self._write_ack_and_mark(conn, intake)
            except _ACK_FAILURES as exc:
                return {
                    "intake_id": intake_id,
                    "ack_written": False,
                    "ack_error": str(exc),
                }
            return {
                "intake_id": intake_id,
                "ack_written": True,
                "ack_file": intake_id + ".ack.json",
                "revision": intake["published_rev"],
            }
        finally:
            conn.close()

    def retry_ack(self, intake_id):
        """Manual rewrite of a pending receipt from the 交件 page."""
        self._require_inbox()
        conn = self.conn_factory()
        try:
            intake = db.get_intake(conn, intake_id)
            if intake is None:
                raise NotFound("交件不存在", code="intake_not_found")
            if intake["published_rev"] is None:
                raise Conflict("该交件尚未发布，没有可补写的回执", code="not_published")
            try:
                self._write_ack_and_mark(conn, intake)
            except _ACK_FAILURES as exc:
                raise Unavailable(
                    f"回执写入失败：{exc}", code="ack_write_failed"
                ) from None
            return {
                "intake_id": intake_id,
                "ack_written": True,
                "ack_file": intake_id + ".ack.json",
                "revision": intake["published_rev"],
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # intake reads
    # ------------------------------------------------------------------
    def _intake_payload(self, conn, row):
        iid = row["intake_id"]
        files = {"csv": False, "ready": False, "ack": False}
        if self.inbox is not None:
            files = {
                "csv": os.path.isfile(self.inbox.csv_path(iid)),
                "ready": os.path.isfile(self.inbox.ready_path(iid)),
                "ack": os.path.isfile(self.inbox.ack_path(iid)),
            }
        return {
            "intake_id": iid,
            "day": row["day"],
            "status": row["status"],
            "original": {
                "name": row["original_name"],
                "size": row["original_size"],
                "sha256": row["original_sha256"],
            },
            "received_at": row["received_at"],
            "published_day": row["published_day"],
            "published_rev": row["published_rev"],
            "published_at": row["published_at"],
            "ack_written_at": row["ack_written_at"],
            "conflict": {
                "kind": row["conflict_kind"],
                "detail": row["conflict_detail"],
                "seen_at": row["conflict_seen_at"],
            }
            if row["conflict_kind"]
            else None,
            "files": files,
            "cleaned": not files["csv"] and not files["ready"],
        }

    def list_intakes(self):
        conn = self.conn_factory()
        try:
            return [self._intake_payload(conn, row) for row in db.list_intakes(conn)]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def list_days(self):
        conn = self.conn_factory()
        try:
            return db.list_days(conn)
        finally:
            conn.close()

    def day_detail(self, day):
        day = _check_day(day)
        conn = self.conn_factory()
        try:
            revisions = db.list_publications(conn, day)
            return {
                "day": day,
                "current_rev": db.day_revision(conn, day),
                "revisions": [_publication_payload(r) for r in revisions],
            }
        finally:
            conn.close()

    def resolve_download(self, day, rev, kind):
        day = _check_day(day)
        if kind not in KINDS:
            raise NotFound("未知的下载类型", code="unknown_kind")
        # Resolve the revision against the database row, then open only the
        # blobs referenced by that row.  Directories are never scanned, so a
        # half-finished publish cannot leak into a reader.
        conn = self.conn_factory()
        try:
            if rev == "latest" or rev is None:
                current = db.day_revision(conn, day)
                if current == 0:
                    raise NotFound("该日尚无可下载版本", code="no_revision")
                row = db.get_publication(conn, day, current)
            else:
                try:
                    rev_int = int(rev)
                except (TypeError, ValueError):
                    raise AppError("修订号必须是数字", code="invalid_rev") from None
                if rev_int < 1:
                    raise AppError("修订号必须大于 0", code="invalid_rev")
                row = db.get_publication(conn, day, rev_int)
            if row is None:
                raise NotFound("该日该修订号不存在", code="revision_not_found")

            # Guard against a damaged revision: never fall back to another
            # revision or serve a partial set.
            for other in ("original_sha256", "settlement_sha256", "ridership_sha256"):
                if not self.store.exists(row[other]):
                    raise Unavailable(
                        "该版本文件缺失，无法提供一致的下载；请联系管理员",
                        code="revision_blob_missing",
                    )
            digest = row[kind + "_sha256"]
            size = row["original_size"] if kind == "original" else row[kind + "_size"]
            name = row["original_name"] if kind == "original" else f"{day}-{kind}.csv"
            path = self.store.path_for(digest)
            # The stored size is what Content-Length will promise; refuse to
            # start a transfer whose blob no longer matches it.
            try:
                if os.path.getsize(path) != size:
                    raise Unavailable(
                        "该版本文件大小与登记不一致，无法提供完整下载；请联系管理员",
                        code="revision_blob_corrupt",
                    )
            except OSError:
                raise Unavailable(
                    "该版本文件缺失，无法提供一致的下载；请联系管理员",
                    code="revision_blob_missing",
                ) from None
            return {
                "path": path,
                "digest": digest,
                "size": size,
                "day": row["day"],
                "rev": row["rev"],
                "kind": kind,
                "name": name,
            }
        finally:
            conn.close()


