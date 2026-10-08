# Runbook: payments-api

What to do when an alert fires. All commands assume the `payments` namespace.

## First look (any alert)

```bash
kubectl get pods,hpa -n payments -o wide          # what is running, ready, restarting?
kubectl get events -n payments --sort-by=.lastTimestamp | tail -20
kubectl logs -n payments -l app=payments-api --tail=50 --prefix
kubectl rollout history deployment/payments-api -n payments
```

## PaymentsHighErrorRate

**Meaning:** more than 5% of API requests returned a 5xx error for at least 1 minute.

1. Is the database up? `kubectl get pod postgres-0 -n payments`. If it is not `Running` and `1/1` ready, go to [Database down](#database-down).
2. Look for errors in the logs: `kubectl logs -n payments -l app=payments-api --tail=100 --prefix | findstr /i error` (Windows) or `| grep -i error` (Linux/macOS). Each log line is JSON with a `request_id`, so you can follow one request.
3. Did a release just happen? `kubectl rollout history deployment/payments-api -n payments`. If the errors started with the newest revision, roll back: `kubectl rollout undo deployment/payments-api -n payments`.

## PaymentsApiPodDown

**Meaning:** Prometheus has not been able to scrape a pod for 30 seconds: the pod is stuck, starting, or gone.

1. `kubectl get pods -n payments -l app=payments-api`. A stuck pod usually shows `0/1` ready.
2. `kubectl describe pod <name> -n payments` and read "Events": failed liveness checks mean Kubernetes is already restarting it.
3. If it keeps happening on new pods, check the logs of the previous container: `kubectl logs <name> -n payments --previous`.

## PaymentsApiRestarted

**Meaning:** an API container restarted in the last 10 minutes. One restart is usually the liveness probe doing its job; several are a problem.

1. `kubectl get pods -n payments` and check the RESTARTS column.
2. `kubectl describe pod <name> -n payments`: "Last State" shows why it stopped (for example `OOMKilled` means it ran out of memory; raise the memory limit in `k8s/03-api.yaml`).
3. `kubectl logs <name> -n payments --previous` shows the logs from before the restart.

## Database down

1. `kubectl describe pod postgres-0 -n payments` and read "Events".
2. Check the volume is still bound: `kubectl get pvc -n payments` (STATUS should be `Bound`).
3. While the database is down, the API pods report not-ready and requests get a 503. They recover on their own when the database is back; no restart is needed.

## Bad release

```bash
kubectl rollout status deployment/payments-api -n payments   # stuck or failed?
kubectl rollout undo deployment/payments-api -n payments     # go back one revision
```
