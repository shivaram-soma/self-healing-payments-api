"""Tests for the real PostgreSQL store.

They run only when TEST_DB_HOST is set (the CI pipeline starts a Postgres
container for them). Locally they are skipped unless you have Postgres.
"""

import os
import uuid

import pytest

from store import PostgresStore, StoreUnavailable

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DB_HOST"),
    reason="set TEST_DB_HOST to run the PostgreSQL tests",
)


@pytest.fixture
def store():
    return PostgresStore(
        host=os.environ["TEST_DB_HOST"],
        port=int(os.environ.get("TEST_DB_PORT", "5432")),
        dbname=os.environ.get("TEST_DB_NAME", "payments"),
        user=os.environ.get("TEST_DB_USER", "payments"),
        password=os.environ.get("TEST_DB_PASSWORD", ""),
    )


def test_ping(store):
    store.ping()


def test_create_get_and_count(store):
    before, _ = store.list_recent()
    payment, created = store.create(1999, "SGD", "lunch", "completed", 12)
    assert created
    assert payment["amount_cents"] == 1999
    assert store.get(payment["id"])["description"] == "lunch"
    after, newest = store.list_recent()
    assert after == before + 1
    assert newest[0]["id"] == payment["id"]


def test_idempotency_key_returns_original(store):
    key = f"test-{uuid.uuid4()}"
    first, created_first = store.create(500, "USD", "", "completed", 1, key)
    second, created_second = store.create(500, "USD", "", "completed", 1, key)
    assert created_first and not created_second
    assert first["id"] == second["id"]


def test_missing_payment_returns_none(store):
    assert store.get(987654321) is None


def test_unreachable_database_raises_store_unavailable():
    broken = PostgresStore(host="127.0.0.1", port=1, dbname="x", user="x",
                           password="x", connect_timeout=1)
    with pytest.raises(StoreUnavailable):
        broken.ping()
