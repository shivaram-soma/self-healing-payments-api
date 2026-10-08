"""Failure drills: break the system on purpose and measure how it recovers.

    python scripts/drills.py pod-crash
    python scripts/drills.py hang
    python scripts/drills.py bad-release
    python scripts/drills.py traffic-spike [--duration 120]
    python scripts/drills.py db-restart
    python scripts/drills.py all

While a drill runs, a background "prober" calls GET /payments five times a
second, the way a user would, and counts every failed request.

Each result is printed and added to drills/results.md, so the numbers in
your README are numbers you measured.
"""

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

CONTEXT = "k3d-payments"
NAMESPACE = "payments"
ROOT = Path(__file__).resolve().parent.parent
RESULTS_FILE = ROOT / "drills" / "results.md"
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8080")


# ---------------------------------------------------------------- helpers

def log(message):
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def kubectl(*args, check=True, input_text=None):
    """Run kubectl in our namespace and return its output as text."""
    cmd = ["kubectl", "--context", CONTEXT, "-n", NAMESPACE, *args]
    result = subprocess.run(cmd, text=True, capture_output=True,
                            input=input_text)
    if check and result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed:\n{result.stderr.strip()}")
    return result


def kubectl_json(*args):
    return json.loads(kubectl(*args, "-o", "json").stdout)


def pods(label):
    """List pods with a label, as simple dicts."""
    items = kubectl_json("get", "pods", "-l", label)["items"]
    result = []
    for item in items:
        statuses = item["status"].get("containerStatuses", [])
        conditions = {c["type"]: c["status"]
                      for c in item["status"].get("conditions", [])}
        result.append({
            "name": item["metadata"]["name"],
            "uid": item["metadata"]["uid"],
            "terminating": "deletionTimestamp" in item["metadata"],
            "ready": conditions.get("Ready") == "True",
            "restarts": sum(s.get("restartCount", 0) for s in statuses),
        })
    return result


def api_pods():
    return pods("app=payments-api")


def pod(name):
    """One pod by name, or None if it does not exist right now."""
    for p in pods("app=payments-api") + pods("app=postgres"):
        if p["name"] == name:
            return p
    return None


def wait_until(condition, timeout, interval=1.0):
    """Call condition() until it is true. Return seconds waited, or None."""
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        try:
            if condition():
                return time.monotonic() - start
        except (RuntimeError, KeyError, TypeError, ValueError):
            pass  # a pod vanished or kubectl hiccupped mid-restart; try again
        time.sleep(interval)
    return None


def http(method, path, body=None, headers=None, timeout=3):
    """Small HTTP helper. Returns (status code or None, parsed JSON or None)."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE_URL + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, None
    except (urllib.error.URLError, OSError, ValueError):
        return None, None


def payment_count():
    status, data = http("GET", "/payments")
    return data["count"] if status == 200 and data else None


def wait_for_healthy_api(timeout=180):
    """Before and between drills: all API pods ready and the API answering."""
    kubectl("rollout", "status", "deployment/payments-api",
            f"--timeout={timeout}s")
    waited = wait_until(lambda: http("GET", "/payments")[0] == 200, timeout)
    if waited is None:
        raise RuntimeError(f"{BASE_URL}/payments is not answering. "
                           "Is the cluster up? (python scripts/cluster.py up)")


class Prober:
    """Calls GET /payments every 0.2s in the background and records results."""

    def __init__(self, interval=0.2):
        self.interval = interval
        self.samples = []          # (ok, latency_seconds)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _loop(self):
        while not self._stop.is_set():
            start = time.monotonic()
            status, _ = http("GET", "/payments", timeout=2)
            self.samples.append((status == 200, time.monotonic() - start))
            self._stop.wait(self.interval)

    def __enter__(self):
        self._thread.start()
        time.sleep(2)  # a little normal traffic before we break anything
        return self

    def __exit__(self, *exc):
        time.sleep(2)  # and a little after it recovers
        self._stop.set()
        self._thread.join()

    @property
    def total(self):
        return len(self.samples)

    @property
    def failed(self):
        return sum(1 for ok, _ in self.samples if not ok)

    def p95_latency(self):
        times = sorted(t for ok, t in self.samples if ok)
        return times[int(len(times) * 0.95) - 1] if times else None


def record(drill, passed, key_result, prober):
    outcome = "PASS" if passed else "FAIL"
    failed = f"{prober.failed} of {prober.total}"
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    if not RESULTS_FILE.exists():
        RESULTS_FILE.parent.mkdir(exist_ok=True)
        RESULTS_FILE.write_text(
            "# Drill results\n\n"
            "Written by `scripts/drills.py`. Newest at the bottom.\n\n"
            "| Date (UTC) | Drill | Outcome | Result | Failed requests during drill |\n"
            "| --- | --- | --- | --- | --- |\n", encoding="utf-8")
    with RESULTS_FILE.open("a", encoding="utf-8") as f:
        f.write(f"| {date} | {drill} | {outcome} | {key_result} | {failed} |\n")

    summary = os.environ.get("GITHUB_STEP_SUMMARY")  # shows on the Actions page
    if summary:
        with open(summary, "a", encoding="utf-8") as f:
            f.write(f"- **{drill}**: {outcome} - {key_result} "
                    f"(failed requests: {failed})\n")

    print(f"\n==> {drill}: {outcome}\n    {key_result}\n"
          f"    failed requests during drill: {failed}\n", flush=True)
    return passed


# ----------------------------------------------------------------- drills

def drill_pod_crash(_args):
    """Delete an API pod. The Deployment should replace it on its own."""
    before = {p["name"] for p in api_pods()}
    victim = sorted(before)[0]
    with Prober() as prober:
        log(f"deleting pod {victim}")
        kubectl("delete", "pod", victim, "--wait=false")
        recovered = wait_until(
            lambda: any(p["ready"] and p["name"] not in before
                        for p in api_pods()), timeout=120)
    if recovered is None:
        return record("Pod crash", False, "no replacement pod became ready "
                      "within 120s", prober)
    return record("Pod crash", True,
                  f"replacement pod ready in {recovered:.1f}s", prober)


def drill_hang(_args):
    """Make one pod stop responding. The liveness probe should restart it."""
    victim = next(p for p in api_pods() if p["ready"])
    name, restarts_before = victim["name"], victim["restarts"]
    with Prober() as prober:
        log(f"telling {name} to hang (it will stop answering, even /health)")
        kubectl("exec", name, "-c", "api", "--", "python", "-c",
                "import urllib.request as u; print(u.urlopen(u.Request("
                "'http://localhost:8000/admin/hang', method='POST'),"
                " timeout=5).read().decode())")

        def restarted():
            p = pod(name)
            return p is not None and p["restarts"] > restarts_before

        restart_after = wait_until(restarted, timeout=120)
        ready_after = wait_until(
            lambda: restarted() and pod(name)["ready"], timeout=120)
    if restart_after is None:
        return record("Hung app", False, "container was not restarted "
                      "within 120s", prober)
    total = restart_after + (ready_after or 0)
    return record("Hung app", ready_after is not None,
                  f"liveness probe restarted the container after "
                  f"{restart_after:.1f}s; ready again after {total:.1f}s", prober)


def drill_bad_release(_args):
    """Release a version that never becomes ready, then roll back."""
    with Prober() as prober:
        log("releasing a broken version (its readiness check always fails)")
        kubectl("set", "env", "deployment/payments-api", "-c", "api",
                "SIMULATE_BAD_RELEASE=true")
        log("waiting for Kubernetes to give up on the release (about 60s)...")
        rollout = kubectl("rollout", "status", "deployment/payments-api",
                          "--timeout=120s", check=False)
        blocked = rollout.returncode != 0
        log("release blocked" if blocked else "release unexpectedly succeeded")

        log("rolling back: kubectl rollout undo deployment/payments-api")
        start = time.monotonic()
        kubectl("rollout", "undo", "deployment/payments-api")
        kubectl("rollout", "status", "deployment/payments-api", "--timeout=180s")
        undo_seconds = time.monotonic() - start

    passed = blocked
    result = ("broken release was blocked by the readiness probe, old pods "
              f"kept serving; rollback completed in {undo_seconds:.1f}s"
              if blocked else "broken release was NOT blocked")
    return record("Faulty release", passed, result, prober)


def drill_traffic_spike(args):
    """Send heavy load. The HorizontalPodAutoscaler should add pods."""
    def hpa():
        status = kubectl_json("get", "hpa", "payments-api")["status"]
        cpu = None
        for metric in status.get("currentMetrics") or []:
            cpu = metric.get("resource", {}).get("current", {}).get(
                "averageUtilization", cpu)
        return status.get("currentReplicas", 0), cpu

    start_replicas, _ = hpa()
    job = (ROOT / "k8s" / "drills" / "load-test.yaml").read_text()
    job = job.replace('value: "120"', f'value: "{args.duration}"')
    kubectl("delete", "job", "load-test", "--ignore-not-found", "--wait=true")

    peak, first_scale_up = start_replicas, None
    with Prober() as prober:
        log(f"starting load for {args.duration}s with {start_replicas} pods")
        kubectl("apply", "-f", "-", input_text=job)
        start = time.monotonic()
        while time.monotonic() - start < args.duration + 30:
            try:
                replicas, cpu = hpa()
            except RuntimeError:
                replicas, cpu = peak, None
            if replicas > peak:
                peak = replicas
                if first_scale_up is None:
                    first_scale_up = time.monotonic() - start
            log(f"pods: {replicas}  average CPU: "
                f"{cpu if cpu is not None else '?'}% of request")
            time.sleep(5)
        kubectl("delete", "job", "load-test", "--ignore-not-found")

    p95 = prober.p95_latency()
    p95_text = f"; p95 latency {p95 * 1000:.0f} ms" if p95 else ""
    if peak > start_replicas:
        result = (f"autoscaler scaled {start_replicas} -> {peak} pods "
                  f"(first scale-up after {first_scale_up:.0f}s){p95_text}")
    else:
        result = f"no scale-up seen (stayed at {peak} pods){p95_text}"
    return record("Traffic spike", peak > start_replicas, result, prober)


def drill_db_restart(_args):
    """Delete the database pod. Data must survive on its persistent volume."""
    for i in range(3):  # make sure there is data worth losing
        http("POST", "/payments",
             {"amount": "10.00", "currency": "SGD", "description": "drill"},
             {"Idempotency-Key": f"drill-{uuid.uuid4()}"})
    before = payment_count()
    old_uid = pod("postgres-0")["uid"]

    with Prober() as prober:
        log(f"{before} payments stored; deleting pod postgres-0")
        kubectl("delete", "pod", "postgres-0", "--wait=false")
        db_back = wait_until(
            lambda: (pod("postgres-0") or {}).get("uid") not in (None, old_uid)
            and pod("postgres-0")["ready"], timeout=180)
        api_back = wait_until(lambda: payment_count() is not None, timeout=120)
    after = payment_count()

    if db_back is None or api_back is None or after is None:
        return record("Database restart", False,
                      "database or API did not come back within the timeout",
                      prober)
    lost = max(0, before - after)
    return record("Database restart", lost == 0,
                  f"database back in {db_back:.1f}s; {lost} of {before} "
                  "payments lost", prober)


DRILLS = {
    "pod-crash": drill_pod_crash,
    "hang": drill_hang,
    "bad-release": drill_bad_release,
    "traffic-spike": drill_traffic_spike,
    "db-restart": drill_db_restart,
}


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("drills", nargs="+", choices=[*DRILLS, "all"])
    parser.add_argument("--duration", type=int, default=120,
                        help="seconds of load for traffic-spike (default 120)")
    args = parser.parse_args()

    names = list(DRILLS) if "all" in args.drills else args.drills
    results = []
    for name in names:
        log(f"===== drill: {name} =====")
        wait_for_healthy_api()
        try:
            results.append(DRILLS[name](args))
        except RuntimeError as error:
            log(f"drill {name} could not run: {error}")
            results.append(False)
    wait_for_healthy_api()

    print(f"Results saved to {RESULTS_FILE.relative_to(ROOT)}")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
