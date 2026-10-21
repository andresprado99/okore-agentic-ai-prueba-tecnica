"""Interfaz web mínima (la caja "API / Chat" del esquema):   python -m app.web --llm mock   ->  http://127.0.0.1:8000

Es una capa FINA sobre el mismo agente que usa el CLI: no añade lógica de negocio ni de seguridad. Lo importante:
  - La confirmación de una acción la pide el SERVIDOR (tarjeta con el resumen generado por el código, no por el LLM)
    y solo se ejecuta al pulsar "Confirmar". No existe ningún endpoint que el modelo pueda llamar para confirmar.
  - La identidad es SIMULADA (selector de usuario). En producción vendría de un token validado en el servidor.
  - Todo lo que escribe el modelo se muestra como TEXTO (nunca como HTML): una respuesta maliciosa no puede inyectar código.
  - El panel de demostración (provocar fallos) es solo para pruebas y se desactiva con --no-demo-controls.
"""
import argparse
import json
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Path as PathParam
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.agent import Agent
from app.auth import USERS, User
from app.bootstrap import LLM_HELP, LLM_KINDS, build_agent, make_llm
from app.config import Settings, load_settings

STATIC = Path(__file__).parent / "static"

# Fallos que el panel de demostración puede provocar en los servicios simulados.
FAULT_PRESETS: dict[str, tuple[str, dict]] = {
    "workshops_down": ("workshops", {"mode": "error_500"}),
    "workshops_slow": ("workshops", {"mode": "slow", "delay_s": 15}),
    "claims_down": ("claims", {"mode": "error_500"}),
    "actions_lost_once": ("actions", {"mode": "execute_then_500", "times": 1}),
    "actions_lost_always": ("actions", {"mode": "execute_then_500"}),
}


class ChatIn(BaseModel):
    user: str
    message: str = Field(min_length=1, max_length=4000)


class UserIn(BaseModel):
    user: str


def create_app(agent: Agent, settings: Settings, llm_name: str, demo_controls: bool = True) -> FastAPI:
    app = FastAPI(title="Agente de expedientes", docs_url=None, redoc_url=None, openapi_url=None)
    lock = threading.Lock()  # el estado (acciones pendientes) vive en memoria: se atiende una petición cada vez

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'"  # sin scripts en línea ni externos
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Sin esto, el navegador puede reutilizar un app.js o un index.html antiguos (caché heurística) y mezclarlos con los nuevos.
        # "no-cache" no significa "no guardar": obliga a preguntar al servidor si el archivo cambió (ETag) antes de usarlo.
        response.headers["Cache-Control"] = "no-cache"
        return response

    def get_user(user_id: str) -> User:
        user = USERS.get(user_id)
        if user is None:
            raise HTTPException(status_code=422, detail="Usuario desconocido")
        return user

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/config")
    def config():
        return {
            "users": [{"id": u.id, "role": u.role} for u in USERS.values()],
            "llm": llm_name,
            "guard_enabled": settings.guard_enabled,
            "demo_controls": demo_controls,
        }

    @app.post("/api/chat")
    def chat(body: ChatIn):
        user = get_user(body.user)
        with lock:
            result = agent.handle(user, body.message)
            pending = [
                {"id": pid, "summary": agent.actions.pending[pid].summary(), "expires_minutes": agent.actions.ttl_minutes}
                for pid in result.pending_ids
            ]
        return {"request_id": result.request_id, "answer": result.answer, "decision": result.final_decision, "pending": pending}

    def resolve(pending_id: str, body: UserIn, decision: str) -> dict:
        user = get_user(body.user)
        with lock:
            p = agent.actions.pending.get(pending_id)
            request_id = p.request_id if p else None
            result = agent.confirm(pending_id, user) if decision == "confirm" else agent.cancel(pending_id, user)
        return {**result, "request_id": request_id}

    pending_id_param = PathParam(pattern=r"^PA-[0-9a-f]{8}$")

    @app.post("/api/actions/{pending_id}/confirm")
    def confirm(body: UserIn, pending_id: str = pending_id_param):
        return resolve(pending_id, body, "confirm")

    @app.post("/api/actions/{pending_id}/cancel")
    def cancel(body: UserIn, pending_id: str = pending_id_param):
        return resolve(pending_id, body, "cancel")

    @app.get("/api/trace/{request_id}")
    def trace(request_id: str = PathParam(pattern=r"^req-[0-9a-f]{12}$")):
        path = settings.trace_path
        if not path.exists():
            return []
        records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [r for r in records if r.get("request_id") == request_id]

    if demo_controls:
        def mock_call(method: str, path: str, **kwargs):
            try:
                resp = httpx.request(method, f"{settings.services_base_url}{path}", timeout=5, **kwargs)
                resp.raise_for_status()
                return resp.json()
            except httpx.HTTPError as err:
                raise HTTPException(status_code=502, detail=f"Servicios simulados no disponibles: {type(err).__name__}") from err

        @app.get("/api/demo/faults")
        def demo_faults():
            return mock_call("GET", "/_faults")

        @app.post("/api/demo/{preset}")
        def demo_preset(preset: str = PathParam(pattern=r"^[a-z_]{1,30}$")):
            if preset == "clear":
                return mock_call("DELETE", "/_faults")
            if preset == "reset":
                return mock_call("POST", "/_reset")
            if preset not in FAULT_PRESETS:
                raise HTTPException(status_code=404, detail="Preajuste desconocido")
            service, payload = FAULT_PRESETS[preset]
            return mock_call("PUT", f"/_faults/{service}", json=payload)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


def ensure_mock_services(settings: Settings) -> bool:
    """Si los servicios simulados no están en marcha, los arranca en este mismo proceso. Devuelve True si los arrancó."""
    health = f"{settings.services_base_url}/health"
    try:
        httpx.get(health, timeout=1).raise_for_status()
        return False
    except httpx.HTTPError:
        pass
    from app.mock_services.main import app as mock_app  # se importa aquí: lee la configuración al importarse

    url = urlparse(settings.services_base_url)
    server = uvicorn.Server(uvicorn.Config(mock_app, host=url.hostname or "127.0.0.1", port=url.port or 8001, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        try:
            httpx.get(health, timeout=0.5).raise_for_status()
            return True
        except httpx.HTTPError:
            time.sleep(0.1)
    raise SystemExit("No se pudieron arrancar los servicios simulados.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Interfaz web del agente de expedientes")
    parser.add_argument("--llm", choices=LLM_KINDS, default="openai", help=LLM_HELP)
    parser.add_argument("--host", default="127.0.0.1", help="por defecto solo accesible desde este equipo")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-demo-controls", action="store_true", help="oculta el panel para provocar fallos")
    args = parser.parse_args()

    settings = load_settings()
    llm = make_llm(args.llm, settings)
    started = ensure_mock_services(settings)
    agent = build_agent(settings, llm)
    app = create_app(agent, settings, args.llm, demo_controls=not args.no_demo_controls)

    print(f"Servicios simulados: {'arrancados en este proceso' if started else 'ya estaban en marcha'} ({settings.services_base_url})")
    print(f"LLM: {args.llm} · filtro de entrada: {'activado' if settings.guard_enabled else 'DESACTIVADO'}")
    print(f"\nAbre  http://{args.host}:{args.port}   (Ctrl+C para parar)\n")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
