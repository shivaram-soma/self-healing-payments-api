"""Create, update, inspect and delete the local Kubernetes cluster.

Works the same on Windows, macOS, Linux and in GitHub Actions.

    python scripts/cluster.py up       # create cluster, build image, deploy everything
    python scripts/cluster.py status   # show pods, services, autoscaler...
    python scripts/cluster.py down     # delete the cluster (and all its data)

Needs: Docker (running), k3d and kubectl on your PATH.
"""

import argparse
import json
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

CLUSTER = "payments"
CONTEXT = f"k3d-{CLUSTER}"      # the kubectl context k3d creates
NAMESPACE = "payments"
IMAGE = "payments-api:1.0.0"
BASE_URL = "http://localhost:8080"
ROOT = Path(__file__).resolve().parent.parent

MANIFESTS = [
    "k8s/01-configmap.yaml",
    "k8s/02-postgres.yaml",
    "k8s/03-api.yaml",
    "k8s/04-hpa.yaml",
    "k8s/05-ingress.yaml",
    "k8s/monitoring/prometheus.yaml",
]


def run(cmd, check=True, capture=False):
    """Run a command, printing it first so you can see what happens."""
    print("$ " + " ".join(cmd), flush=True)
    result = subprocess.run(cmd, cwd=ROOT, text=True,
                            capture_output=capture)
    if check and result.returncode != 0:
        if capture:
            print(result.stdout, result.stderr)
        sys.exit(f"Command failed (exit code {result.returncode}). "
                 "See the output above.")
    return result


def kubectl(*args, check=True, capture=False):
    return run(["kubectl", "--context", CONTEXT, *args],
               check=check, capture=capture)


def require_tools():
    missing = [t for t in ("docker", "k3d", "kubectl") if not shutil.which(t)]
    if missing:
        sys.exit("Missing tools: " + ", ".join(missing) +
                 "\nInstall them first (see docs/getting-started.md).")
    if run(["docker", "info"], check=False, capture=True).returncode != 0:
        sys.exit("Docker is not running. Start Docker Desktop and try again.")


def cluster_exists():
    result = run(["k3d", "cluster", "list", "-o", "json"], capture=True)
    return any(c.get("name") == CLUSTER for c in json.loads(result.stdout or "[]"))


def wait_for_url(url, timeout=180):
    print(f"Waiting for {url} to answer...", flush=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                if response.status == 200:
                    print("  it answered with 200 OK")
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(3)
    sys.exit(f"{url} did not answer within {timeout}s. "
             "Run `python scripts/cluster.py status` to see what is wrong.")


def up(_args):
    require_tools()

    # 1. Cluster: one k3s node. Port 8080 on this machine -> the ingress.
    if cluster_exists():
        print(f"Cluster '{CLUSTER}' already exists, reusing it.")
    else:
        run(["k3d", "cluster", "create", CLUSTER,
             "--agents", "0",
             "--port", "8080:80@loadbalancer",
             "--wait", "--timeout", "300s"])

    # 2. Build the app image and copy it into the cluster (no registry needed).
    run(["docker", "build", "-t", IMAGE, "app"])
    run(["k3d", "image", "import", IMAGE, "--cluster", CLUSTER])

    # 3. Namespace and the database password Secret (random, never in Git).
    kubectl("apply", "-f", "k8s/00-namespace.yaml")
    has_secret = kubectl("get", "secret", "db-credentials", "-n", NAMESPACE,
                         check=False, capture=True).returncode == 0
    if has_secret:
        print("Secret db-credentials already exists, keeping it.")
    else:
        password = secrets.token_urlsafe(18)
        print("$ kubectl create secret generic db-credentials ... (random password)")
        subprocess.run(["kubectl", "--context", CONTEXT, "create", "secret",
                        "generic", "db-credentials", "-n", NAMESPACE,
                        f"--from-literal=password={password}"],
                       check=True, cwd=ROOT)

    # 4. Everything else. If the API already existed, restart it so it picks
    #    up the image we just rebuilt (the tag stays the same).
    api_existed = kubectl("get", "deployment", "payments-api", "-n", NAMESPACE,
                          check=False, capture=True).returncode == 0
    for manifest in MANIFESTS:
        kubectl("apply", "-f", manifest)
    if api_existed:
        kubectl("rollout", "restart", "deployment/payments-api", "-n", NAMESPACE)

    # 5. Wait until everything is running.
    for workload in ("statefulset/postgres", "deployment/payments-api",
                     "deployment/prometheus"):
        kubectl("rollout", "status", workload, "-n", NAMESPACE,
                "--timeout=300s")
    wait_for_url(f"{BASE_URL}/payments")

    print(f"""
All set. Try it:
  GET  {BASE_URL}/payments
  Prometheus:  kubectl port-forward -n {NAMESPACE} svc/prometheus 9090:9090
               then open http://localhost:9090
  Drills:      python scripts/drills.py all
""")


def status(_args):
    kubectl("get", "pods,svc,hpa,ingress,pvc", "-n", NAMESPACE, "-o", "wide")


def down(_args):
    run(["k3d", "cluster", "delete", CLUSTER])


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["up", "status", "down"])
    args = parser.parse_args()
    {"up": up, "status": status, "down": down}[args.command](args)


if __name__ == "__main__":
    main()
