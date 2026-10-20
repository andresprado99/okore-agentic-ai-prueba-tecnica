"""Trazabilidad: un registro JSON por línea (JSONL) por cada ejecución del agente y por cada confirmación.

Con estos registros se puede reconstruir qué pasó: qué pidió el usuario, qué herramientas se llamaron y con qué
parámetros, qué devolvieron (completo, sin recortar), cuántos intentos hizo cada servicio, qué decidió el sistema
y si se ejecutó alguna acción. Los registros de un mismo caso comparten request_id.

Nota de producción: los resultados pueden contener datos personales (matrículas...). En un entorno real habría que
enmascararlos, cifrar el fichero y fijar una política de retención.
"""
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


class Trace:
    def __init__(self, kind: str = "agent_run", request_id: str | None = None, user_id: str | None = None, user_request: str | None = None):
        self.kind = kind
        self.request_id = request_id or f"req-{uuid.uuid4().hex[:12]}"
        self.user_id = user_id
        self.user_request = user_request
        self.timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        self._t0 = time.perf_counter()
        self.tools_called: list[dict] = []
        self.service_calls: list[dict] = []
        self.errors: list[dict] = []
        self.llm_calls = 0
        self.tokens_in = 0
        self.tokens_out = 0

    def add_tool_call(self, name: str, parameters, result: dict, status: str, latency_ms: int) -> None:
        self.tools_called.append({"name": name, "parameters": parameters, "result": result, "status": status, "latency_ms": latency_ms})

    def add_service_call(self, info) -> None:
        self.service_calls.append(info.as_dict())

    def add_error(self, where: str, kind: str, message: str) -> None:
        self.errors.append({"where": where, "kind": kind, "message": message})

    def add_llm_usage(self, reply) -> None:
        self.llm_calls += 1
        self.tokens_in += reply.input_tokens
        self.tokens_out += reply.output_tokens

    def latency_ms(self) -> int:
        return int((time.perf_counter() - self._t0) * 1000)

    def record(self, **fields) -> dict:
        """Campos comunes + los específicos de cada tipo de registro."""
        return {
            "type": self.kind,
            "request_id": self.request_id,
            "timestamp": self.timestamp,
            "user_id": self.user_id,
            **fields,
            "service_calls": self.service_calls,
            "errors": self.errors,
            "latency_ms": self.latency_ms(),
        }


class TraceWriter:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()

    def write(self, record: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock, self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
