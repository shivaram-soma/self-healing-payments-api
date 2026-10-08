# Postmortem: <drill name, e.g. "Database restart">

Copy this file to `docs/postmortem.md` and fill it in after running the drill. Use your own measured numbers from `drills/results.md`. Blameless: describe what the system did, not who did what.

**Date:** <YYYY-MM-DD>
**Duration of impact:** <e.g. 14 seconds>
**Impact:** <e.g. 42 of 310 requests failed with 503; no payments lost>

## Summary

<Two or three sentences: what broke, what users saw, how it recovered.>

## Timeline

| Time | What happened |
| --- | --- |
| 00:00 | <the drill started / the pod was deleted> |
| 00:0? | <first failed request> |
| 00:0? | <Kubernetes reacted: probe failed, pod rescheduled, ...> |
| 00:0? | <service fully recovered> |

## What went well

- <e.g. the PersistentVolume kept all the data>

## What went wrong, or could be better

- <e.g. all API pods went not-ready together, so every request failed for N seconds>

## Why it happened

<The underlying reason, not just the trigger. For example: readiness depends on the database, so a database restart takes every API pod out of traffic at once.>

## Action items

| Action | Why |
| --- | --- |
| <e.g. return a clearer error to clients during database restarts> | <...> |
| <e.g. add a retry with backoff in the client> | <...> |
