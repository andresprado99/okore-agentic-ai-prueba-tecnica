"""Configuración: todo sale de variables de entorno (archivo .env), con valores por defecto razonables."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    # LLM (OpenAI o cualquier servidor compatible, p. ej. Ollama)
    llm_api_key: str | None
    llm_model: str | None
    llm_base_url: str | None
    # Servicios simulados y base de datos
    services_base_url: str
    db_path: Path
    # Trazabilidad
    trace_path: Path
    # Resiliencia de las llamadas a servicios
    service_connect_timeout_s: float
    service_read_timeout_s: float
    service_max_attempts: int
    service_backoff_s: float
    # Agente
    max_agent_steps: int
    pending_ttl_minutes: int
    guard_enabled: bool


def _path(name: str, default: str) -> Path:
    p = Path(os.environ.get(name, default))
    return p if p.is_absolute() else ROOT / p


def load_settings() -> Settings:
    return Settings(
        llm_api_key=os.environ.get("LLM_API_KEY"),
        llm_model=os.environ.get("LLM_MODEL"),
        llm_base_url=os.environ.get("LLM_BASE_URL") or None,
        services_base_url=os.environ.get("SERVICES_BASE_URL", "http://127.0.0.1:8001"),
        db_path=_path("DB_PATH", "data/claims.db"),
        trace_path=_path("TRACE_PATH", "logs/trace.jsonl"),
        service_connect_timeout_s=float(os.environ.get("SERVICE_CONNECT_TIMEOUT_S", "2")),
        service_read_timeout_s=float(os.environ.get("SERVICE_READ_TIMEOUT_S", "3")),
        service_max_attempts=int(os.environ.get("SERVICE_MAX_ATTEMPTS", "3")),
        service_backoff_s=float(os.environ.get("SERVICE_BACKOFF_S", "0.3")),
        max_agent_steps=int(os.environ.get("MAX_AGENT_STEPS", "6")),
        pending_ttl_minutes=int(os.environ.get("PENDING_TTL_MINUTES", "5")),
        guard_enabled=os.environ.get("GUARD_ENABLED", "true").strip().lower() not in ("0", "false", "no"),
    )
