"""Servicios simulados de OKORE (sección 5 del enunciado) con FALLOS INYECTABLES para probar el caso 3.

  GET  /api/claims/{claim_id}
  GET  /api/workshops/{workshop_id}
  POST /api/claims/{claim_id}/actions      (exige cabecera Idempotency-Key)

Control de fallos (no forma parte de la API "real"; solo para pruebas y demos):
  PUT    /_faults/{claims|workshops|actions}   {"mode": "...", "delay_s": 10, "times": 2}
  GET    /_faults
  DELETE /_faults                              (quita fallos)
  POST   /_reset                               (quita fallos, olvida idempotencia y restaura los datos)

Modos de fallo:
  ok                 funciona con normalidad
  error_500          responde 500 sin hacer nada
  slow               tarda delay_s segundos en responder (con un timeout menor, el cliente se rinde)
  execute_then_500   SOLO actions: ejecuta la acción de verdad y DESPUÉS responde 500 (respuesta perdida)
"""
import asyncio
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.config import load_settings
from app.mock_services.data import CLAIMS, WORKSHOPS
from app.mock_services.db import add_event, init_db, reset_db

SETTINGS = load_settings()
FAULTS: dict[str, dict] = {}
IDEMPOTENCY: dict[str, dict] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(SETTINGS.db_path)
    yield


app = FastAPI(title="OKORE - servicios simulados", lifespan=lifespan)


# --- Fallos inyectables --------------------------------------------------------------------
class FaultIn(BaseModel):
    mode: Literal["ok", "error_500", "slow", "execute_then_500"]
    delay_s: float = Field(default=10.0, ge=0, le=120)
    times: int | None = Field(default=None, ge=1, description="Nº de peticiones afectadas; vacío = siempre")


async def apply_fault(service: str) -> str | None:
    """Aplica el fallo configurado. Devuelve el modo si el endpoint debe terminar el trabajo (execute_then_500)."""
    fault = FAULTS.get(service)
    if not fault or fault["mode"] == "ok":
        return None
    if fault["remaining"] is not None:
        if fault["remaining"] <= 0:
            FAULTS.pop(service, None)
            return None
        fault["remaining"] -= 1
    if fault["mode"] == "error_500":
        raise HTTPException(status_code=500, detail="Fallo simulado")
    if fault["mode"] == "slow":
        await asyncio.sleep(fault["delay_s"])
        return None
    return fault["mode"]


@app.put("/_faults/{service}")
async def set_fault(service: Literal["claims", "workshops", "actions"], fault: FaultIn):
    FAULTS[service] = {"mode": fault.mode, "delay_s": fault.delay_s, "remaining": fault.times}
    return {"service": service, **FAULTS[service]}


@app.get("/_faults")
async def get_faults():
    return FAULTS


@app.delete("/_faults")
async def clear_faults():
    FAULTS.clear()
    return {"cleared": True}


@app.post("/_reset")
async def reset():
    FAULTS.clear()
    IDEMPOTENCY.clear()
    reset_db(SETTINGS.db_path)
    return {"reset": True}


@app.get("/health")
async def health():
    return {"status": "ok"}


# --- API simulada --------------------------------------------------------------------------
@app.get("/api/claims/{claim_id}")
async def get_claim(claim_id: str):
    await apply_fault("claims")
    claim = CLAIMS.get(claim_id)
    if claim is None:
        raise HTTPException(status_code=404, detail="Expediente no encontrado")
    return claim


@app.get("/api/workshops/{workshop_id}")
async def get_workshop(workshop_id: str):
    await apply_fault("workshops")
    workshop = WORKSHOPS.get(workshop_id)
    if workshop is None:
        raise HTTPException(status_code=404, detail="Taller no encontrado")
    return workshop


class ActionIn(BaseModel):
    action: Literal["REQUEST_DOCUMENT"]
    document: str


@app.post("/api/claims/{claim_id}/actions")
async def post_action(claim_id: str, body: ActionIn, idempotency_key: str | None = Header(default=None)):
    mode = await apply_fault("actions")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Falta la cabecera Idempotency-Key")
    claim = CLAIMS.get(claim_id)
    if claim is None:
        raise HTTPException(status_code=404, detail="Expediente no encontrado")

    if idempotency_key in IDEMPOTENCY:
        # Misma clave = misma acción: se devuelve el resultado anterior y NO se repite el efecto.
        result = {**IDEMPOTENCY[idempotency_key], "replayed": True}
    else:
        if body.document not in claim["missing_documents"]:
            raise HTTPException(status_code=409, detail=f"El documento {body.document} no está pendiente")
        add_event(SETTINGS.db_path, claim_id, "DOCUMENT_REQUESTED", f"Solicitado al taller el documento {body.document}")
        result = {"status": "ACCEPTED", "action_id": f"ACT-{len(IDEMPOTENCY) + 1:04d}", "claim_id": claim_id, "replayed": False}
        IDEMPOTENCY[idempotency_key] = result

    if mode == "execute_then_500":  # la acción YA se ha hecho, pero el cliente no lo sabrá
        raise HTTPException(status_code=500, detail="Fallo simulado tras ejecutar")
    return result
