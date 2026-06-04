"""Central configuration for the incident agent.

All tunables come from environment variables (or a local ``.env`` file) so the
same image runs unchanged locally and in-cluster. See ``.env.example``.

The LLM backend is abstracted here: ``LLM_BACKEND`` switches between Groq
(default, free tier), OpenAI, or a local Ollama model. ``get_llm()`` returns a
ready-to-use LangChain chat model regardless of backend.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LLMBackend = Literal["groq", "openai", "ollama"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- LLM backend ---
    llm_backend: LLMBackend = "groq"
    # Groq: GPT-OSS models support STRICT structured output (verified 2026-06-03).
    # Llama 3.3 70B only does best-effort JSON, so it is NOT the default.
    groq_model: str = "openai/gpt-oss-20b"
    groq_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    openai_api_key: str | None = None
    ollama_model: str = "llama3"
    ollama_base_url: str = "http://localhost:11434"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 1500

    # --- Prometheus / Alertmanager ---
    alertmanager_url: str = "http://localhost:9093"
    prometheus_url: str = "http://localhost:9090"
    poll_interval_seconds: int = 60  # FR-1 default

    # --- Kubernetes ---
    # When running in-cluster, the ServiceAccount token is used automatically.
    # Locally, the default kubeconfig (~/.kube/config) is used.
    in_cluster: bool = False
    default_namespace: str = "default"

    # --- Vector store (Chroma) ---
    chroma_persist_dir: str = "./.chroma"
    runbook_dir: str = "./runbooks"
    runbook_collection: str = "runbooks"
    incident_collection: str = "incident_memory"
    embedding_model: str = "all-MiniLM-L6-v2"  # local sentence-transformers
    retrieval_top_k: int = 3  # FR-3

    # --- Remediation safety (FR-5) ---
    # Only these actions may ever be executed automatically, and only on LOW.
    safe_actions: list[str] = Field(default_factory=lambda: ["restart_pod", "scale_deployment"])
    enable_auto_remediation: bool = True  # master kill-switch

    # --- Output (FR-7) ---
    report_log_path: str = "./incident-reports.jsonl"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_llm(settings: Settings | None = None):
    """Return a LangChain chat model for the configured backend.

    Imports are local so we only require the SDK for the backend in use.
    """
    s = settings or get_settings()

    if s.llm_backend == "groq":
        from langchain_groq import ChatGroq

        return ChatGroq(
            model=s.groq_model,
            temperature=s.llm_temperature,
            max_tokens=s.llm_max_tokens,
            api_key=s.groq_api_key,
        )

    if s.llm_backend == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=s.openai_model,
            temperature=s.llm_temperature,
            max_tokens=s.llm_max_tokens,
            api_key=s.openai_api_key,
        )

    if s.llm_backend == "ollama":
        from langchain_ollama import ChatOllama

        return ChatOllama(
            model=s.ollama_model,
            temperature=s.llm_temperature,
            base_url=s.ollama_base_url,
        )

    raise ValueError(f"Unknown LLM_BACKEND: {s.llm_backend!r}")
