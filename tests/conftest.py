"""Entorno de pruebas: servicios simulados REALES (FastAPI en un hilo) con base de datos y traza en una carpeta temporal.

Ninguna prueba necesita clave de API ni red: el LLM siempre es un doble (MockLLM, ScriptedLLM...).
"""
import dataclasses
import os
import socket
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

# --- El entorno se fija ANTES de importar la app (los servicios simulados leen su configuración al importarse) ---
_TMP = Path(tempfile.mkdtemp(prefix="okore_tests_"))
with socket.socket() as _s:
    _s.bind(("127.0.0.1", 0))
    _PORT = _s.getsockname()[1]
os.environ.update({
    "SERVICES_BASE_URL": f"http://127.0.0.1:{_PORT}",
    "DB_PATH": str(_TMP / "claims.db"),
    "TRACE_PATH": str(_TMP / "trace.jsonl"),
    "SERVICE_CONNECT_TIMEOUT_S": "1",
    "SERVICE_READ_TIMEOUT_S": "0.5",   # tiempos cortos para que las pruebas de fallos sean rápidas
    "SERVICE_MAX_ATTEMPTS": "3",
    "SERVICE_BACKOFF_S": "0.05",
    "GUARD_ENABLED": "true",
})
os.environ.pop("LLM_BASE_URL", None)

from app.actions import ActionManager  # noqa: E402
from app.agent import Agent  # noqa: E402
from app.audit import TraceWriter  # noqa: E402
from app.config import Settings, load_settings  # noqa: E402
from app.mock_services.main import app as mock_app  # noqa: E402
from app.services import Services  # noqa: E402


@pytest.fixture(scope="session")
def settings() -> Settings:
    return load_settings()


@pytest.fixture(scope="session", autouse=True)
def mock_server(settings):
    server = uvicorn.Server(uvicorn.Config(mock_app, host="127.0.0.1", port=_PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            httpx.get(f"{settings.services_base_url}/health", timeout=0.5).raise_for_status()
            break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        raise RuntimeError("Los servicios simulados no arrancaron")
    yield
    server.should_exit = True


@pytest.fixture(autouse=True)
def clean_state(mock_server, settings):
    """Cada prueba empieza con datos restaurados, sin fallos activos y con la traza vacía."""
    httpx.post(f"{settings.services_base_url}/_reset", timeout=5).raise_for_status()
    settings.trace_path.unlink(missing_ok=True)
    yield


@pytest.fixture
def make_agent(settings):
    created: list[Services] = []

    def _make(llm, **overrides) -> Agent:
        s = dataclasses.replace(settings, **overrides) if overrides else settings
        services = Services(s)
        created.append(services)
        return Agent(llm, services, ActionManager(services, s), TraceWriter(s.trace_path), s)

    yield _make
    for services in created:
        services.close()


@pytest.fixture
def records(settings):
    """Devuelve todos los registros de la traza como lista de dicts."""
    import json

    def _read() -> list[dict]:
        if not settings.trace_path.exists():
            return []
        return [json.loads(line) for line in settings.trace_path.read_text(encoding="utf-8").splitlines()]

    return _read


@pytest.fixture
def db_events(settings):
    def _read(claim_id: str = "EXP-10234") -> list[str]:
        conn = sqlite3.connect(settings.db_path)
        try:
            return [r[0] for r in conn.execute("SELECT event_type FROM claim_events WHERE claim_id = ? ORDER BY id", (claim_id,))]
        finally:
            conn.close()

    return _read


class Faults:
    def __init__(self, base_url: str):
        self._base = base_url

    def set(self, service: str, mode: str, **kwargs) -> None:
        httpx.put(f"{self._base}/_faults/{service}", json={"mode": mode, **kwargs}, timeout=5).raise_for_status()

    def clear(self) -> None:
        httpx.delete(f"{self._base}/_faults", timeout=5).raise_for_status()


@pytest.fixture
def faults(settings) -> Faults:
    return Faults(settings.services_base_url)
