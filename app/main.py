"""Payments API.

Endpoints
  POST /payments          create a payment (optional Idempotency-Key header)
  GET  /payments          total count + the 20 newest payments
  GET  /payments/<id>     one payment
  GET  /health            liveness: is the process able to answer at all?
  GET  /ready             readiness: can it serve traffic (database reachable)?
  GET  /metrics           Prometheus metrics
  POST /admin/hang        failure drill: make this pod stop responding
                          (only when CHAOS_ENABLED=true, never exposed outside
                          the cluster)
"""

import hashlib
import json
import logging
import os
import sys
import threading
import time
import uuid
from decimal import Decimal, InvalidOperation

from flask import Flask, g, jsonify, request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from werkzeug.exceptions import HTTPException

from store import PostgresStore, StoreUnavailable

SUPPORTED_CURRENCIES = {"SGD", "USD", "EUR", "INR", "GBP"}
MAX_AMOUNT = Decimal("100000.00")
NOT_MEASURED = {"/health", "/ready", "/metrics"}

# ---------- metrics (created once, shared by the whole process) ----------
REQUESTS = Counter(
    "http_requests_total", "HTTP requests handled",
    ["method", "endpoint", "status"],
)
LATENCY = Histogram(
    "http_request_duration_seconds", "Time spent handling a request",
    ["endpoint"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
PAYMENTS_CREATED = Counter(
    "payments_created_total", "Payments created", ["currency"],
)

# ---------- logging: one JSON line per event, easy to search ----------
log = logging.getLogger("payments")
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(logging.Formatter("%(message)s"))
log.addHandler(handler)
log.setLevel(logging.INFO)
log.propagate = False


def log_event(**fields):
    log.info(json.dumps(fields, default=str))


# ---------- helpers ----------
def env_flag(name):
    return os.environ.get(name, "false").strip().lower() == "true"


def format_amount(cents):
    return f"{cents // 100}.{cents % 100:02d}"


def parse_amount(value):
    """Turn '25.50' or 25.5 into 2550 cents. Raise ValueError if invalid."""
    if isinstance(value, bool) or value is None:
        raise ValueError("amount is required")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("amount must be a number")
    if not amount.is_finite() or amount <= 0:
        raise ValueError("amount must be greater than 0")
    if amount > MAX_AMOUNT:
        raise ValueError(f"amount must not be more than {MAX_AMOUNT}")
    if amount.as_tuple().exponent < -2:
        raise ValueError("amount can have at most 2 decimal places")
    return int(amount * 100)


def risk_score(amount_cents, currency, description, iterations):
    """A SIMULATED fraud check. It burns a little CPU on purpose, like a
    real scoring step would, so that load tests make the autoscaler react."""
    data = f"{amount_cents}:{currency}:{description}".encode()
    digest = hashlib.pbkdf2_hmac("sha256", data, b"risk-check", iterations)
    return int.from_bytes(digest[:2], "big") % 100


def to_response(payment):
    return {
        "id": payment["id"],
        "amount": format_amount(payment["amount_cents"]),
        "amount_cents": payment["amount_cents"],
        "currency": payment["currency"],
        "description": payment["description"],
        "status": payment["status"],
        "risk_score": payment["risk_score"],
        "created_at": payment["created_at"],
    }


def store_from_env():
    return PostgresStore(
        host=os.environ.get("DB_HOST", "localhost"),
        port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ.get("DB_NAME", "payments"),
        user=os.environ.get("DB_USER", "payments"),
        password=os.environ.get("DB_PASSWORD", ""),
    )


# ---------- the app ----------
def create_app(store=None):
    app = Flask(__name__)
    app.config["STORE"] = store or store_from_env()
    hung = threading.Event()  # set by the "hang" failure drill

    version = os.environ.get("APP_VERSION", "1.0.0")
    iterations = int(os.environ.get("RISK_CHECK_ITERATIONS", "20000"))

    # ----- request hooks: request id, hang drill, metrics, logs -----
    @app.before_request
    def before():
        if hung.is_set():
            # Simulates a stuck process (deadlock, infinite loop...).
            # Nothing answers, not even /health, so the liveness probe fails
            # and Kubernetes restarts the container.
            while True:
                time.sleep(3600)
        g.start = time.perf_counter()
        g.request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]

    @app.after_request
    def after(response):
        response.headers["X-Request-ID"] = g.get("request_id", "")
        if request.path in NOT_MEASURED or request.path.startswith("/admin"):
            return response
        endpoint = request.url_rule.rule if request.url_rule else "unmatched"
        elapsed = time.perf_counter() - g.get("start", time.perf_counter())
        REQUESTS.labels(request.method, endpoint, str(response.status_code)).inc()
        LATENCY.labels(endpoint).observe(elapsed)
        log_event(event="request", request_id=g.get("request_id"),
                  method=request.method, path=request.path,
                  status=response.status_code, ms=round(elapsed * 1000, 1))
        return response

    # ----- errors always come back as JSON -----
    @app.errorhandler(StoreUnavailable)
    def database_down(error):
        log_event(event="database_unavailable", request_id=g.get("request_id"),
                  error=str(error))
        return jsonify(error="database unavailable, try again shortly"), 503

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    @app.errorhandler(Exception)
    def unexpected(error):
        log_event(event="unhandled_error", request_id=g.get("request_id"),
                  error=repr(error))
        return jsonify(error="internal server error"), 500

    # ----- business endpoints -----
    @app.get("/")
    def index():
        return jsonify(service="payments-api", version=version)

    @app.post("/payments")
    def create_payment():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="request body must be a JSON object"), 400

        try:
            amount_cents = parse_amount(body.get("amount"))
        except ValueError as error:
            return jsonify(error=str(error)), 400

        currency = str(body.get("currency", "")).upper()
        if currency not in SUPPORTED_CURRENCIES:
            options = ", ".join(sorted(SUPPORTED_CURRENCIES))
            return jsonify(error=f"currency must be one of: {options}"), 400

        description = str(body.get("description", ""))
        if len(description) > 140:
            return jsonify(error="description must be 140 characters or fewer"), 400

        key = request.headers.get("Idempotency-Key")
        if key is not None and not 1 <= len(key) <= 64:
            return jsonify(error="Idempotency-Key must be 1 to 64 characters"), 400

        score = risk_score(amount_cents, currency, description, iterations)
        status = "flagged" if score >= 95 else "completed"

        payment, created_now = app.config["STORE"].create(
            amount_cents, currency, description, status, score, key)
        if created_now:
            PAYMENTS_CREATED.labels(currency).inc()
            log_event(event="payment_created", request_id=g.request_id,
                      payment_id=payment["id"], amount_cents=amount_cents,
                      currency=currency, status=status)
        # 201 = created now; 200 = the same request was sent again
        return jsonify(to_response(payment)), 201 if created_now else 200

    @app.get("/payments")
    def list_payments():
        total, payments = app.config["STORE"].list_recent(limit=20)
        return jsonify(count=total, payments=[to_response(p) for p in payments])

    @app.get("/payments/<int:payment_id>")
    def get_payment(payment_id):
        payment = app.config["STORE"].get(payment_id)
        if payment is None:
            return jsonify(error=f"payment {payment_id} not found"), 404
        return jsonify(to_response(payment))

    # ----- health checks used by Kubernetes -----
    @app.get("/health")
    def health():
        # Liveness: only "is this process alive and answering?"
        # It deliberately does NOT check the database. If the database is
        # down, restarting every API pod would not fix anything.
        return jsonify(status="ok")

    @app.get("/ready")
    def ready():
        # Readiness: "should this pod receive traffic right now?"
        if env_flag("SIMULATE_BAD_RELEASE"):
            return jsonify(ready=False, reason="simulated bad release"), 503
        try:
            app.config["STORE"].ping()
        except StoreUnavailable:
            return jsonify(ready=False, reason="database unreachable"), 503
        return jsonify(ready=True)

    @app.get("/metrics")
    def metrics():
        return generate_latest(), 200, {"Content-Type": CONTENT_TYPE_LATEST}

    # ----- failure drill -----
    @app.post("/admin/hang")
    def admin_hang():
        if not env_flag("CHAOS_ENABLED"):
            return jsonify(error="not found"), 404
        log_event(event="hang_drill_started")
        hung.set()
        return jsonify(status="this pod will stop responding now")

    return app


if __name__ == "__main__":
    # Local development only. In the container, gunicorn runs the app.
    create_app().run(host="0.0.0.0", port=8000)
