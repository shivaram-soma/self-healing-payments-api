"""Where payments are saved.

Two stores share the same methods:
- PostgresStore: the real one, used inside Kubernetes.
- InMemoryStore: a fake one, used by the unit tests so they need no database.

Money is stored as whole cents (an integer), never as a float,
so 0.1 + 0.2 can never turn into 0.30000000000000004.
"""

import threading
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras


class StoreUnavailable(Exception):
    """Raised when the database cannot be reached."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    id              SERIAL PRIMARY KEY,
    amount_cents    BIGINT      NOT NULL CHECK (amount_cents > 0),
    currency        CHAR(3)     NOT NULL,
    description     TEXT        NOT NULL DEFAULT '',
    status          TEXT        NOT NULL,
    risk_score      INTEGER     NOT NULL,
    idempotency_key TEXT        UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

COLUMNS = ("id, amount_cents, currency, description, status, "
           "risk_score, idempotency_key, created_at")


def _row_to_dict(row):
    created = row["created_at"]
    return {
        "id": row["id"],
        "amount_cents": row["amount_cents"],
        "currency": row["currency"].strip(),
        "description": row["description"],
        "status": row["status"],
        "risk_score": row["risk_score"],
        "idempotency_key": row["idempotency_key"],
        "created_at": created.isoformat() if hasattr(created, "isoformat")
        else created,
    }


class PostgresStore:
    """Saves payments in PostgreSQL.

    It opens a new connection for each operation. That is slower than a
    connection pool, but it means the app recovers on its own after the
    database restarts, with no stale connections to clean up.
    """

    def __init__(self, host, port, dbname, user, password, connect_timeout=3):
        self.params = dict(host=host, port=port, dbname=dbname, user=user,
                           password=password, connect_timeout=connect_timeout)
        self._schema_ready = False

    def _connect(self):
        try:
            return psycopg2.connect(**self.params)
        except psycopg2.OperationalError as error:
            raise StoreUnavailable(str(error)) from error

    def _ensure_schema(self, conn):
        if not self._schema_ready:
            with conn.cursor() as cur:
                cur.execute(SCHEMA)
            conn.commit()
            self._schema_ready = True

    def ping(self):
        """Return normally if the database answers, else raise."""
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        except psycopg2.Error as error:
            raise StoreUnavailable(str(error)) from error
        finally:
            conn.close()

    def create(self, amount_cents, currency, description, status, risk_score,
               idempotency_key=None):
        """Save a payment. Returns (payment, created_now)."""
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            with conn.cursor(
                    cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""INSERT INTO payments (amount_cents, currency,
                            description, status, risk_score, idempotency_key)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (idempotency_key) DO NOTHING
                        RETURNING {COLUMNS}""",
                    (amount_cents, currency, description, status, risk_score,
                     idempotency_key),
                )
                row = cur.fetchone()
                created_now = row is not None
                if not created_now:
                    # Same idempotency key seen before: return the original.
                    cur.execute(
                        f"SELECT {COLUMNS} FROM payments "
                        "WHERE idempotency_key = %s",
                        (idempotency_key,),
                    )
                    row = cur.fetchone()
            conn.commit()
            return _row_to_dict(row), created_now
        except psycopg2.Error as error:
            conn.rollback()
            raise StoreUnavailable(str(error)) from error
        finally:
            conn.close()

    def get(self, payment_id):
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            with conn.cursor(
                    cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(f"SELECT {COLUMNS} FROM payments WHERE id = %s",
                            (payment_id,))
                row = cur.fetchone()
            return _row_to_dict(row) if row else None
        except psycopg2.Error as error:
            raise StoreUnavailable(str(error)) from error
        finally:
            conn.close()

    def list_recent(self, limit=20):
        """Return (total_count, newest payments first)."""
        conn = self._connect()
        try:
            self._ensure_schema(conn)
            with conn.cursor(
                    cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT count(*) AS n FROM payments")
                total = cur.fetchone()["n"]
                cur.execute(f"SELECT {COLUMNS} FROM payments "
                            "ORDER BY id DESC LIMIT %s", (limit,))
                rows = cur.fetchall()
            return total, [_row_to_dict(r) for r in rows]
        except psycopg2.Error as error:
            raise StoreUnavailable(str(error)) from error
        finally:
            conn.close()


class InMemoryStore:
    """A fake store for tests. Same methods, data kept in a Python list."""

    def __init__(self):
        self.payments = []
        self.available = True  # tests set this to False to fake an outage
        self._lock = threading.Lock()

    def _check(self):
        if not self.available:
            raise StoreUnavailable("database is down (simulated)")

    def ping(self):
        self._check()

    def create(self, amount_cents, currency, description, status, risk_score,
               idempotency_key=None):
        self._check()
        with self._lock:
            if idempotency_key:
                for payment in self.payments:
                    if payment["idempotency_key"] == idempotency_key:
                        return dict(payment), False
            payment = {
                "id": len(self.payments) + 1,
                "amount_cents": amount_cents,
                "currency": currency,
                "description": description,
                "status": status,
                "risk_score": risk_score,
                "idempotency_key": idempotency_key,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self.payments.append(payment)
            return dict(payment), True

    def get(self, payment_id):
        self._check()
        for payment in self.payments:
            if payment["id"] == payment_id:
                return dict(payment)
        return None

    def list_recent(self, limit=20):
        self._check()
        newest_first = list(reversed(self.payments))[:limit]
        return len(self.payments), [dict(p) for p in newest_first]
