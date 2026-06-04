# Runbook: Node Pressure (Memory / Disk / PID)

## Symptom
A node reports a pressure condition. `kubectl get nodes` shows a condition such as
`MemoryPressure=True`, `DiskPressure=True`, or `PIDPressure=True`. The kubelet may
begin evicting pods.

## Common Causes
- Node oversubscribed: sum of pod requests/usage exceeds capacity.
- Disk filling from logs, images, or ephemeral storage.
- A runaway pod consuming memory or spawning processes.

## Investigation Steps
1. `get_node_conditions` — identify which node and which pressure type.
2. `query_prometheus` — `node_memory_MemAvailable_bytes`, `node_filesystem_avail_bytes`.
3. Identify the heaviest pods on the node (`kubectl top pods` if metrics-server present).
4. Check for evicted pods (`kubectl get pods --field-selector status.phase=Failed`).

## Remediation
- **Disk pressure from images/logs:** prune unused images, rotate logs —
  human approval (MEDIUM).
- **Memory pressure from one workload:** `scale_deployment` down the offender or
  reschedule — approval required if MEDIUM/HIGH.
- **Chronic oversubscription:** add a node / raise capacity — HIGH, human only.

## Notes
Node-level pressure is almost never LOW: it can cascade into multi-pod evictions.
Treat as MEDIUM minimum and require human approval before any action.
