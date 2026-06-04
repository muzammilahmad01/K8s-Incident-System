# Runbook: CrashLoopBackOff

## Symptom
A pod repeatedly starts, crashes, and is restarted by Kubernetes. `kubectl get pod`
shows status `CrashLoopBackOff` and a rising `RESTARTS` count.

## Common Causes
- Application exits non-zero on startup (bad config, missing env var, failed migration).
- Misconfigured liveness probe killing a healthy-but-slow container.
- Missing dependency or unreachable downstream service at boot.
- Image entrypoint that runs once and exits (e.g. a script with no long-running process).

## Investigation Steps
1. `kubectl describe pod <name>` — read the Events for the exit reason.
2. `kubectl logs <name> --previous` — inspect the crashed container's last output.
3. Check the container's exit code: code 1 = app error, 137 = OOMKilled/SIGKILL, 143 = SIGTERM.
4. Verify required ConfigMaps/Secrets/env vars exist and are mounted.

## Remediation
- **Transient / single test pod (LOW severity):** delete the pod to force a clean
  restart (`restart_pod`). Safe to automate.
- **Bad config or image:** roll back the deployment to the last good revision —
  requires human approval (MEDIUM/HIGH).
- **Liveness probe too aggressive:** increase `initialDelaySeconds` / `timeoutSeconds`.

## Notes
A crashing pod that recovers after a restart is LOW severity. A crash that recurs
immediately after restart, or affects a production workload, is MEDIUM or higher.
