"""Acceso a los sistemas externos: APIs HTTP (expedientes, talleres, acciones) y la base de datos de eventos.

Aquí vive la política de resiliencia (caso 3), en UN solo sitio:
  - timeouts cortos (nunca esperar indefinidamente);
  - reintentos con espera creciente SOLO ante fallos transitorios (timeout, error de conexión, 5xx);
  - sin reintento ante errores definitivos (404, 4xx);
  - las escrituras (POST) solo se reintentan si llevan clave de idempotencia (así no se pueden duplicar);
  - si al final no hay respuesta, se lanza ServiceError con el motivo: nunca se inventa un resultado.
"""
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from app.config import Settings

LABELS = {"claims": "expedientes", "workshops": "talleres", "actions": "acciones", "database": "histórico de eventos"}
KIND_ES = {"timeout": "sin respuesta a tiempo", "connection": "sin conexión", "server_error": "error del servidor",
           "bad_response": "respuesta no válida", "database": "error de base de datos",
           "client_error": "petición rechazada", "not_found": "no encontrado"}


@dataclass
class CallInfo:
    """Resumen de una llamada a un servicio (va a la traza)."""

    service: str
    method: str
    path: str
    attempts: int = 0
    outcome: str = "ok"  # ok | error
    status: int | None = None
    error_kind: str | None = None  # timeout | connection | server_error | not_found | client_error | bad_response | database
    latency_ms: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


class ServiceError(Exception):
    def __init__(self, info: CallInfo, message: str):
        super().__init__(message)
        self.info = info

    @property
    def kind(self) -> str | None:
        return self.info.error_kind


class ServiceClient:
    def __init__(self, name: str, http: httpx.Client, settings: Settings, sleep=time.sleep):
        self.name = name
        self._http = http  # compartido entre servicios: crear un cliente HTTP es caro (inicializa TLS)
        self._max_attempts = settings.service_max_attempts
        self._backoff_s = settings.service_backoff_s
        self._sleep = sleep

    def request(self, method: str, path: str, *, json=None, headers: dict | None = None) -> tuple[dict, CallInfo]:
        info = CallInfo(self.name, method, path)
        # Un POST solo es seguro de reintentar si el servidor puede detectar la repetición (Idempotency-Key).
        retryable_method = method == "GET" or bool(headers and "Idempotency-Key" in headers)
        max_attempts = self._max_attempts if retryable_method else 1
        t0 = time.perf_counter()
        message = ""

        for attempt in range(1, max_attempts + 1):
            info.attempts = attempt
            transient = True
            try:
                resp = self._http.request(method, path, json=json, headers=headers)
            except httpx.TimeoutException:
                info.error_kind, info.status, message = "timeout", None, "sin respuesta a tiempo"
            except httpx.TransportError:
                info.error_kind, info.status, message = "connection", None, "no se pudo conectar"
            else:
                info.status = resp.status_code
                if resp.status_code < 400:
                    try:
                        data = resp.json()
                    except ValueError:
                        info.error_kind, message = "bad_response", "respuesta no es JSON válido"
                    else:
                        info.outcome, info.error_kind = "ok", None
                        info.latency_ms = int((time.perf_counter() - t0) * 1000)
                        return data, info
                elif resp.status_code == 404:
                    info.error_kind, message, transient = "not_found", "recurso no encontrado", False
                elif resp.status_code < 500:
                    info.error_kind, message, transient = "client_error", f"petición rechazada (HTTP {resp.status_code})", False
                else:
                    info.error_kind, message = "server_error", f"error del servidor (HTTP {resp.status_code})"
            if not transient:
                break
            if attempt < max_attempts:
                self._sleep(self._backoff_s * 2 ** (attempt - 1))  # espera creciente: 0.3 s, 0.6 s...

        info.outcome = "error"
        info.latency_ms = int((time.perf_counter() - t0) * 1000)
        raise ServiceError(info, message)

class Services:
    """Agrupa los clientes de los sistemas externos."""

    def __init__(self, settings: Settings):
        self._http = httpx.Client(
            base_url=settings.services_base_url,
            timeout=httpx.Timeout(settings.service_read_timeout_s, connect=settings.service_connect_timeout_s),
            # La verificación TLS solo aplica a https (siempre activa ahí); para http local evita cargar certificados en balde.
            verify=settings.services_base_url.lower().startswith("https"),
        )
        self.claims = ServiceClient("claims", self._http, settings)
        self.workshops = ServiceClient("workshops", self._http, settings)
        self.actions = ServiceClient("actions", self._http, settings)
        self.db_path: Path = settings.db_path

    def close(self) -> None:
        self._http.close()


def call(client: ServiceClient, trace, method: str, path: str, **kwargs) -> dict:
    """Llama a un servicio y deja constancia en la traza, tanto si va bien como si falla."""
    try:
        data, info = client.request(method, path, **kwargs)
    except ServiceError as err:
        trace.add_service_call(err.info)
        trace.add_error(f"service:{client.name}", err.kind or "unknown", f"{method} {path}: {err}")
        raise
    trace.add_service_call(info)
    return data


def read_claim_events(db_path: Path, trace, claim_id: str, limit: int = 50) -> list[dict]:
    """Lee el histórico con una consulta PARAMETRIZADA y una conexión de SOLO LECTURA.

    El LLM nunca escribe SQL: solo pasa un claim_id, ya validado, a esta función.
    """
    info = CallInfo("database", "SELECT", "claim_events", attempts=1)
    t0 = time.perf_counter()
    try:
        if not db_path.exists():
            raise sqlite3.OperationalError("base de datos no encontrada")
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
        try:
            rows = conn.execute(
                "SELECT event_date, event_type, description FROM claim_events WHERE claim_id = ? ORDER BY event_date, id LIMIT ?",
                (claim_id, limit),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as err:
        info.outcome, info.error_kind = "error", "database"
        info.latency_ms = int((time.perf_counter() - t0) * 1000)
        trace.add_service_call(info)
        trace.add_error("service:database", "database", str(err))
        raise ServiceError(info, str(err)) from err
    info.latency_ms = int((time.perf_counter() - t0) * 1000)
    trace.add_service_call(info)
    return [{"event_date": d, "event_type": t, "description": desc} for d, t, desc in rows]
