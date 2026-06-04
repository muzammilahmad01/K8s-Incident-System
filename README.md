# K8s Incident Response Agent

A LangGraph AI agent that triages Kubernetes incidents — and, unlike K8sGPT or
HolmesGPT, **remembers past incidents** via a persistent vector store so each
alert benefits from what was learned last time.

When an alert fires, the agent investigates the cluster (`kubectl`), consults
runbooks (RAG), reasons about root cause with an LLM, and either **auto-remediates
low-risk issues** or **escalates to a human** — with the safety boundary enforced
in the graph, never by the prompt.

## How it works

```
poll_alerts → fetch_cluster_state → retrieve_runbook → reason → decide_action
   (Alertmanager)   (kubectl)          (Chroma RAG)     (LLM)        │
                                                                     ├─ LOW + safe action → execute_remediation ┐
                                                                     └─ MEDIUM/HIGH ──────── human approval ─────┤
                                                                                          persist_incident → report
```

The agent **polls** Prometheus Alertmanager (it is not triggered by Prometheus),
investigates each firing alert, and runs one structured LLM call to produce
`root_cause`, `severity`, `recommended_actions`, and `confidence`.

## Stack

| Concern | Choice |
|---|---|
| Agent framework | LangGraph + LangChain |
| LLM | **Groq** `openai/gpt-oss-20b` (free tier, strict structured output) — OpenAI/Ollama optional |
| Embeddings | **local** `all-MiniLM-L6-v2` (Groq has no embeddings API → zero cloud cost) |
| Vector store | ChromaDB (runbooks + incident memory) |
| Cluster | kind + Prometheus/Alertmanager |
| Python | 3.11+ (isolated via `uv`) |

> **Deviations from SRS v1.0** (intentional): local embeddings instead of OpenAI;
> lean Prometheus instead of full kube-prometheus-stack; added `once`/`watch` run
> modes; Groq instead of OpenAI as the default LLM.

## Quick start (local, no cluster needed)

```bash
# 1. Install uv (manages an isolated Python 3.11), then deps
uv sync

# 2. Configure
cp .env.example .env        # add your free GROQ_API_KEY

# 3. Index the runbooks into Chroma
uv run incident-agent load-runbooks

# 4. Run one cycle against a fixture alert (no cluster required)
uv run incident-agent once --alert-file fixtures/crashloop.json
```

## Against a real cluster

```bash
kind create cluster --name incident-lab
kubectl apply -f k8s/test-workloads/crashloop-pod.yaml   # trigger a real crash
# port-forward Alertmanager/Prometheus, then:
uv run incident-agent watch
```

## Deploy into the cluster (final step)

```bash
docker build -t k8s-incident-agent:dev .
kind load docker-image k8s-incident-agent:dev --name incident-lab
kubectl apply -f k8s/rbac.yaml
kubectl apply -f k8s/configmap-runbooks.yaml
kubectl create secret generic incident-agent-secrets --from-env-file=.env
kubectl apply -f k8s/agent-deployment.yaml
```

## Tests

```bash
uv run pytest          # mocked kubectl/HTTP — no cluster or API key needed
```

## Layout

```
agent/      graph.py · state.py · nodes.py · tools.py · memory.py · config.py · __main__.py
runbooks/   crashloop.md · oomkilled.md · node-pressure.md
k8s/        rbac.yaml · agent-deployment.yaml · configmap-runbooks.yaml · test-workloads/
fixtures/   sample alert JSON for `once` mode
tests/      test_tools.py · test_graph.py
```
