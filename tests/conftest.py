import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from railapp import create_app  # noqa: E402

DEFAULT_CSV = (
    "ticket_id,passenger_id,train_no,amount,travel_date,depart_station,arrive_station,passenger_count\n"
    "T1,P1,G101,55,2026-11-01,北京南,上海虹桥,1\n"
    "T2,P2,G101,120,2026-11-01,北京南,上海虹桥,1\n"
    "T3,P3,G22,88,2026-11-01,天津,北京南,2\n"
)


@pytest.fixture
def app(tmp_path):
    application = create_app({
        "DATA_DIR": str(tmp_path / "data"),
        "INBOX_DIR": str(tmp_path / "inbox"),
        "TESTING": True,
    })
    yield application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def csv_bytes():
    return DEFAULT_CSV.encode("utf-8")


@pytest.fixture
def inbox_dir(app):
    return app.config["INBOX"].root


def upload(client, day="2026-11-01", data=None, filename="tickets.csv"):
    return client.post(
        "/api/previews",
        data={
            "day": day,
            "file": (__import__("io").BytesIO(data if data is not None else DEFAULT_CSV.encode()), filename),
        },
        content_type="multipart/form-data",
        headers={"Accept": "application/json"},
    )


def preview_and_confirm(client, day="2026-11-01", data=None):
    response = upload(client, day=day, data=data)
    assert response.status_code == 201, response.get_json()
    preview = response.get_json()
    confirmed = client.post(
        f"/api/previews/{preview['preview_id']}/confirm",
        headers={"Accept": "application/json"},
    )
    return preview, confirmed


def drop_inbox_file(inbox_dir, intake_id, csv_text=None, ready_day=None):
    """Simulate the station program: write the CSV, then the .ready marker."""
    if csv_text is not None:
        with open(os.path.join(inbox_dir, intake_id + ".csv"), "wb") as handle:
            handle.write(csv_text.encode("utf-8") if isinstance(csv_text, str) else csv_text)
    if ready_day is not None:
        with open(os.path.join(inbox_dir, intake_id + ".ready"), "w", encoding="utf-8") as handle:
            handle.write(ready_day + "\n")


def scan(client):
    response = client.post("/api/inbox/scan", headers={"Accept": "application/json"})
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def receive(client, inbox_dir, intake_id, csv_text=None, ready_day="2026-11-01"):
    """Drop a complete 交件 and scan it in; returns the scan summary."""
    drop_inbox_file(inbox_dir, intake_id, csv_text if csv_text is not None else DEFAULT_CSV, ready_day)
    result = scan(client)
    assert intake_id in result["received"], result
    return result


def intake_preview(client, intake_id):
    response = client.post(
        f"/api/intakes/{intake_id}/previews", headers={"Accept": "application/json"}
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def confirm_preview(client, preview_id):
    return client.post(
        f"/api/previews/{preview_id}/confirm", headers={"Accept": "application/json"}
    )
