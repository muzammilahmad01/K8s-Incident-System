# Runbook: OOMKilled

## Symptom
A container is killed by the kernel out-of-memory killer. `kubectl describe pod`
shows `Reason: OOMKilled` and the container exit code is `137`.

## Common Causes
- Container memory limit set too low for the actual workload.
- Memory leak in the application.
- A sudden traffic spike increasing per-request memory.
- Large in-memory caches or unbounded buffers.

## Investigation Steps
1. `kubectl describe pod <name>` — confirm `OOMKilled` and which container.
2. `kubectl logs <name> --previous` — look for the last activity before the kill.
3. Compare the container `resources.limits.memory` against observed usage
   (`query_prometheus` with `container_memory_working_set_bytes`).
4. Check whether usage grows monotonically (leak) or spikes (load).

## Remediation
- **Limit slightly too low, stable workload:** raise `resources.limits.memory`
  in the deployment — requires human approval (MEDIUM).
- **Memory leak:** roll back to the previous image and file a bug — HIGH severity.
- **Replica pressure under load:** `scale_deployment` to add replicas and spread
  memory — safe to automate only if classified LOW.

## Notes
OOMKilled is rarely LOW because it implies a resourcing or code problem that
will recur. Default to MEDIUM and require approval unless clearly a one-off.
