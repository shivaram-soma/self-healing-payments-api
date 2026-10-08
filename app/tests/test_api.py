"""API tests. They use InMemoryStore, so no database is needed."""

import pytest

from main import create_app
from store import InMemoryStore


@pytest.fixture
def store():
    return InMemoryStore()


@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.setenv("RISK_CHECK_ITERATIONS", "10")  # keep tests fast
    monkeypatch.delenv("SIMULATE_BAD_RELEASE", raising=False)
    monkeypatch.delenv("CHAOS_ENABLED", raising=False)
    return create_app(store).test_client()


def pay(client, amount="25.50", currency="SGD", headers=None):
    body = {"amount": amount, "currency": currency, "description": "coffee"}
    return client.post("/payments", json=body, headers=headers or {})


# ---------- creating payments ----------

def test_create_payment(client):
    response = pay(client)
    assert response.status_code == 201
    data = response.get_json()
    assert data["id"] == 1
    assert data["amount"] == "25.50"
    assert data["amount_cents"] == 2550
    assert data["currency"] == "SGD"
    assert data["status"] in ("completed", "flagged")


def test_amount_is_stored_as_whole_cents(client):
    # 19.99 as a float is really 19.989999999999998...; we must store 1999.
    assert pay(client, amount=19.99).get_json()["amount_cents"] == 1999
    assert pay(client, amount="0.10").get_json()["amount_cents"] == 10


@pytest.mark.parametrize("bad_amount, message", [
    (None, "amount is required"),
    ("abc", "amount must be a number"),
    ("-5", "greater than 0"),
    ("0", "greater than 0"),
    ("10.999", "at most 2 decimal places"),
    ("100000.01", "must not be more than"),
    (True, "amount is required"),
])
def test_invalid_amount_is_rejected(client, bad_amount, message):
    response = client.post("/payments",
                           json={"amount": bad_amount, "currency": "SGD"})
    assert response.status_code == 400
    assert message in response.get_json()["error"]


def test_unsupported_currency_is_rejected(client):
    response = pay(client, currency="XYZ")
    assert response.status_code == 400
    assert "currency" in response.get_json()["error"]


def test_body_must_be_json(client):
    response = client.post("/payments", data="not json")
    assert response.status_code == 400


def test_same_idempotency_key_does_not_charge_twice(client, store):
    headers = {"Idempotency-Key": "order-123"}
    first = pay(client, headers=headers)
    second = pay(client, headers=headers)
    assert first.status_code == 201
    assert second.status_code == 200
    assert first.get_json()["id"] == second.get_json()["id"]
    assert len(store.payments) == 1


# ---------- reading payments ----------

def test_list_payments_newest_first(client):
    pay(client, amount="1.00")
    pay(client, amount="2.00")
    data = client.get("/payments").get_json()
    assert data["count"] == 2
    assert [p["amount"] for p in data["payments"]] == ["2.00", "1.00"]


def test_get_one_payment(client):
    pay(client)
    assert client.get("/payments/1").status_code == 200
    assert client.get("/payments/99").status_code == 404


# ---------- health checks ----------

def test_health_is_ok_even_when_database_is_down(client, store):
    store.available = False
    assert client.get("/health").status_code == 200


def test_ready_when_database_is_up(client):
    assert client.get("/ready").status_code == 200


def test_not_ready_when_database_is_down(client, store):
    store.available = False
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.get_json()["reason"] == "database unreachable"


def test_not_ready_during_simulated_bad_release(client, monkeypatch):
    monkeypatch.setenv("SIMULATE_BAD_RELEASE", "true")
    assert client.get("/ready").status_code == 503


def test_database_outage_returns_503_not_500(client, store):
    store.available = False
    assert pay(client).status_code == 503
    assert client.get("/payments").status_code == 503


# ---------- metrics and drills ----------

def test_metrics_are_exposed(client):
    pay(client)
    text = client.get("/metrics").get_data(as_text=True)
    assert "http_requests_total" in text
    assert "payments_created_total" in text


def test_hang_drill_is_disabled_by_default(client):
    assert client.post("/admin/hang").status_code == 404


def test_request_id_is_returned(client):
    response = client.get("/payments", headers={"X-Request-ID": "abc123"})
    assert response.headers["X-Request-ID"] == "abc123"
