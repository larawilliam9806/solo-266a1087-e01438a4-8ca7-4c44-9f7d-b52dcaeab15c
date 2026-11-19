"""Shared-directory intake （交件） protocol.

A station program writes the daily settlement extract into a shared
directory:

  1. ``<交件号>.csv``     the full extract for one batch
  2. ``<交件号>.ready``   a marker file whose content is the batch date
                          (``YYYY-MM-DD``), written *after* the CSV is complete

Only after a formal publish does this service place

  3. ``<交件号>.ack.json``  the receipt the station verifies before cleaning
                          up its source files.  It records the batch date, the
                          SHA-256 of the received original and the formal
                          revision number.

The scanner never touches a CSV whose ``.ready`` marker is missing, so a
half-written file is never received.  Ack files are written to a temporary
name and atomically renamed, so the station never reads a partial receipt.
"""

import hashlib
import json
import os
import re
import tempfile
from datetime import date

from .convert import MAX_FILE_BYTES

READY_SUFFIX = ".ready"
CSV_SUFFIX = ".csv"
ACK_SUFFIX = ".ack.json"

_MAX_READY_BYTES = 4096
# Upper bound when hashing an already-received file for conflict detection;
# anything larger is reported instead of read fully.
_MAX_HASH_BYTES = 64 * 1024 * 1024

_ID_FORBIDDEN = re.compile(r"[\\/\x00-\x1f\x7f]")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class InboxProblem(Exception):
    """A problem with one inbox file; reported per 交件号， never fatal."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message


def validate_intake_id(stem):
    """Return an error message if the 交件号 (file stem) is unusable."""
    if not stem:
        return "交件号为空"
    if len(stem) > 128:
        return "交件号超过 128 个字符"
    if stem != stem.strip():
        return "交件号首尾不能是空白字符"
    if stem.startswith("."):
        return "交件号不能以点开头"
    if stem in (".", ".."):
        return "交件号无效"
    if _ID_FORBIDDEN.search(stem):
        return "交件号包含路径分隔符或控制字符"
    return None


def parse_ready_date(text):
    """Validate the .ready file content and return the batch date."""
    if not _DATE_RE.match(text):
        raise InboxProblem(f".ready 内容不是有效的批次日期（YYYY-MM-DD）：{text[:40]!r}")
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        raise InboxProblem(f".ready 中的日期不存在：{text!r}") from None
    if parsed.isoformat() != text:
        raise InboxProblem(f".ready 内容不是有效的批次日期（YYYY-MM-DD）：{text!r}")
    return text


class InboxStore:
    """File-level access to the shared intake directory."""

    def __init__(self, root):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)

    # -- paths -----------------------------------------------------------
    def csv_path(self, intake_id):
        return os.path.join(self.root, intake_id + CSV_SUFFIX)

    def ready_path(self, intake_id):
        return os.path.join(self.root, intake_id + READY_SUFFIX)

    def ack_path(self, intake_id):
        return os.path.join(self.root, intake_id + ACK_SUFFIX)

    def ack_exists(self, intake_id):
        return os.path.isfile(self.ack_path(intake_id))

    def sources_present(self, intake_id):
        """True while the station's source files (csv/ready) are still there."""
        return os.path.isfile(self.csv_path(intake_id)) or os.path.isfile(
            self.ready_path(intake_id)
        )

    # -- scanning ----------------------------------------------------------
    def scan_entries(self):
        """Return ``{stem: {"csv": bool, "ready": bool}}`` for the directory."""
        entries = {}
        try:
            iterator = os.scandir(self.root)
        except OSError as exc:
            raise InboxProblem(f"无法读取共享目录：{exc}") from None
        with iterator as it:
            for entry in it:
                try:
                    if not entry.is_file():
                        continue
                except OSError:
                    continue
                name = entry.name
                if name.endswith(CSV_SUFFIX):
                    stem = name[: -len(CSV_SUFFIX)]
                    entries.setdefault(stem, {"csv": False, "ready": False})["csv"] = True
                elif name.endswith(READY_SUFFIX):
                    stem = name[: -len(READY_SUFFIX)]
                    entries.setdefault(stem, {"csv": False, "ready": False})["ready"] = True
                # .ack.json, temp files and anything else are ignored here.
        return entries

    # -- reading -----------------------------------------------------------
    def read_ready_date(self, path):
        try:
            with open(path, "rb") as handle:
                raw = handle.read(_MAX_READY_BYTES + 1)
        except OSError as exc:
            raise InboxProblem(f"无法读取 .ready 文件：{exc}") from None
        if len(raw) > _MAX_READY_BYTES:
            raise InboxProblem(".ready 文件过大，不是有效的批次日期标记")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise InboxProblem(".ready 文件不是合法的 UTF-8 文本") from None
        return parse_ready_date(text.strip())

    def read_csv(self, path):
        """Read a CSV for first receipt; enforces the same 1 MB cap as uploads."""
        try:
            size = os.path.getsize(path)
        except OSError as exc:
            raise InboxProblem(f"无法读取 CSV 文件：{exc}") from None
        if size > MAX_FILE_BYTES:
            raise InboxProblem(f"CSV 超过 1 MB 上限（{size} 字节），未收件")
        if size == 0:
            raise InboxProblem("CSV 为空文件，未收件")
        try:
            with open(path, "rb") as handle:
                return handle.read()
        except OSError as exc:
            raise InboxProblem(f"无法读取 CSV 文件：{exc}") from None

    def hash_csv(self, path):
        """Stream-hash a CSV that is already on record (conflict check)."""
        hasher = hashlib.sha256()
        total = 0
        try:
            with open(path, "rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_HASH_BYTES:
                        raise InboxProblem("CSV 过大，无法核对内容")
                    hasher.update(chunk)
        except InboxProblem:
            raise
        except OSError as exc:
            raise InboxProblem(f"无法读取 CSV 文件：{exc}") from None
        return hasher.hexdigest()

    # -- ack ---------------------------------------------------------------
    def write_ack(self, intake_id, payload):
        """Atomically write ``<交件号>.ack.json``.  Raises OSError on failure."""
        data = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        fd, tmp_path = tempfile.mkstemp(
            prefix=".ack-", suffix=".tmp", dir=self.root
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.ack_path(intake_id))
            self._fsync_dir()
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def read_ack(self, intake_id):
        """Return the parsed ack payload, or None if missing/unreadable."""
        try:
            with open(self.ack_path(intake_id), "rb") as handle:
                return json.loads(handle.read().decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return None

    def _fsync_dir(self):
        try:
            fd = os.open(self.root, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)
