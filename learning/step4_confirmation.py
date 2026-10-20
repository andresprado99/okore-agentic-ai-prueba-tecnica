"""Paso 4: acciones que MODIFICAN el expediente, siempre con confirmación explícita (caso 2).

Idea central: separar PROPONER de EJECUTAR.
  - El LLM solo puede PROPONER (tool propose_action). Eso crea una "acción pendiente" y no toca nada.
  - La confirmación NO la gestiona el LLM: la pide el programa y la lee el programa (input / endpoint).
    No existe ninguna tool "confirmar" que el modelo pueda llamar por su cuenta.
  - EJECUTAR (confirm_and_execute) es código determinista que usa los parámetros GUARDADOS en la
    propuesta, nunca algo que diga el LLM después.

Novedades respecto al paso 3 (marcadas con NUEVO):
  - Usuarios con rol: "ana" (operator, puede modificar) y "luis" (viewer, solo consulta).
  - Tool de escritura propose_action, oculta para los viewer y además bloqueada en el control.
  - Acciones pendientes con caducidad, un solo uso y clave de idempotencia.

Uso:  python learning/step4_confirmation.py "Pide al taller la foto de matrícula que falta del expediente EXP-10234"
      python learning/step4_confirmation.py --user luis "..."     (usuario sin permiso de modificar)
"""
import argparse
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Literal

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

load_dotenv()

MAX_STEPS = 6
PENDING_TTL = timedelta(minutes=5)  # una propuesta no confirmada caduca

# --- Datos de mentira (más adelante: API FastAPI simulada + SQLite) ------------------------
FAKE_CLAIMS = {
    "EXP-10234": {"claim_id": "EXP-10234", "status": "REPAIRING", "vehicle": {"plate": "1234ABC", "brand": "BMW", "model": "X1"},
                  "workshop_id": "T-228", "missing_documents": ["PHOTO_PLATE"], "last_update": "2026-09-22T10:32:00"},
    "EXP-10567": {"claim_id": "EXP-10567", "status": "CLOSED", "vehicle": {"plate": "5678XYZ", "brand": "Seat", "model": "Ibiza"},
                  "workshop_id": "T-101", "missing_documents": [], "last_update": "2026-08-30T16:05:00"},
    "EXP-20000": {"claim_id": "EXP-20000", "status": "REPAIRING", "vehicle": {"plate": "9999ZZZ", "brand": "Audi", "model": "A3"},
                  "workshop_id": "T-228", "missing_documents": ["PHOTO_PLATE"], "last_update": "2026-09-30T09:00:00"},
}
FAKE_WORKSHOPS = {
    "T-228": {"workshop_id": "T-228", "name": "Talleres Norte", "contact": "norte@example.com", "status": "ACTIVE", "channels": ["EMAIL", "PORTAL"]},
    "T-101": {"workshop_id": "T-101", "name": "Taller Centro", "contact": "centro@example.com", "status": "ACTIVE", "channels": ["EMAIL"]},
}
FAKE_EVENTS = {
    "EXP-10234": [
        {"event_date": "2026-09-15", "event_type": "CLAIM_OPENED", "description": "Expediente abierto"},
        {"event_date": "2026-09-22", "event_type": "REPAIR_STARTED", "description": "Reparación iniciada"},
    ],
}


# --- NUEVO: usuarios y permisos ------------------------------------------------------------
@dataclass(frozen=True)
class User:
    id: str
    role: Literal["operator", "viewer"]
    allowed_claims: frozenset[str]


USERS = {
    "ana": User("ana", "operator", frozenset({"EXP-10234", "EXP-10567"})),
    "luis": User("luis", "viewer", frozenset({"EXP-10234", "EXP-10567"})),
}


# --- NUEVO: API de acciones simulada, con idempotencia -------------------------------------
ACTIONS_API_CALLS: list[dict] = []  # registro de llamadas REALES a la API (para comprobar que no se duplican)
_IDEMPOTENCY: dict[str, dict] = {}


def actions_api(claim_id: str, payload: dict, idempotency_key: str) -> dict:
    """Simula POST /api/claims/{id}/actions. Si recibe dos veces la misma clave, NO repite el efecto."""
    ACTIONS_API_CALLS.append({"claim_id": claim_id, "payload": payload, "key": idempotency_key})
    if idempotency_key in _IDEMPOTENCY:
        return {**_IDEMPOTENCY[idempotency_key], "replayed": True}
    FAKE_EVENTS.setdefault(claim_id, []).append(
        {"event_date": datetime.now().strftime("%Y-%m-%d"), "event_type": "DOCUMENT_REQUESTED",
         "description": f"Solicitado al taller el documento {payload['document']}"}
    )
    response = {"status": "ACCEPTED", "action_id": f"ACT-{len(_IDEMPOTENCY) + 1:04d}"}
    _IDEMPOTENCY[idempotency_key] = response
    return response


# --- NUEVO: acciones pendientes ------------------------------------------------------------
@dataclass
class PendingAction:
    id: str
    user_id: str
    claim_id: str
    action: str
    document: str
    workshop_id: str
    workshop_name: str
    channel: str
    expires_at: datetime
    status: str = "PENDING"  # PENDING -> EXECUTING -> EXECUTED | FAILED ; o CANCELLED | EXPIRED

    def summary(self) -> str:
        """Texto escrito por NUESTRO código a partir de lo guardado: es exactamente lo que se ejecutará."""
        return (f"Solicitar al taller {self.workshop_id} ({self.workshop_name}) por {self.channel} "
                f"el documento {self.document} del expediente {self.claim_id}")


PENDING: dict[str, PendingAction] = {}


# --- Argumentos de cada tool ---------------------------------------------------------------
class StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClaimIdArgs(StrictArgs):
    claim_id: str = Field(pattern=r"^EXP-\d{5}$", description="Identificador del expediente, formato EXP-12345.")


class WorkshopIdArgs(StrictArgs):
    workshop_id: str = Field(pattern=r"^T-\d{3}$", description="Identificador del taller, formato T-228.")


class ProposeActionArgs(StrictArgs):
    claim_id: str = Field(pattern=r"^EXP-\d{5}$", description="Identificador del expediente, formato EXP-12345.")
    action: Literal["REQUEST_DOCUMENT"] = Field(description="Tipo de acción. Solo se permite REQUEST_DOCUMENT.")
    document: Literal["PHOTO_PLATE", "PHOTO_DAMAGE", "REPAIR_QUOTE"] = Field(description="Documento que se pide al taller.")


# --- Las tools (ahora reciben el usuario como primer argumento) ----------------------------
def get_claim(user: User, claim_id: str) -> dict:
    return FAKE_CLAIMS.get(claim_id) or {"error": f"Expediente {claim_id} no encontrado."}


def get_workshop(user: User, workshop_id: str) -> dict:
    return FAKE_WORKSHOPS.get(workshop_id) or {"error": f"Taller {workshop_id} no encontrado."}


def get_claim_events(user: User, claim_id: str) -> dict:
    return {"claim_id": claim_id, "events": FAKE_EVENTS.get(claim_id, [])}


def propose_action(user: User, claim_id: str, action: str, document: str, now: datetime | None = None) -> dict:
    """NUEVO. PREPARA una acción; no ejecuta nada. Todas las comprobaciones son deterministas."""
    now = now or datetime.now(timezone.utc)
    claim = FAKE_CLAIMS.get(claim_id)
    if claim is None:
        return {"error": f"Expediente {claim_id} no encontrado."}
    # ¿El documento realmente falta? Lo decide el dato, no el LLM.
    if document not in claim["missing_documents"]:
        return {"error": f"El documento {document} no figura como pendiente en {claim_id}. No se propone ninguna acción."}
    # ¿Podemos contactar con el taller?
    workshop = FAKE_WORKSHOPS.get(claim["workshop_id"])
    if workshop is None or workshop["status"] != "ACTIVE" or not workshop["channels"]:
        return {"error": f"El taller {claim['workshop_id']} no está disponible para recibir solicitudes."}
    # Evitar duplicados: si ya hay una propuesta igual viva, se reutiliza.
    for p in PENDING.values():
        if (p.status == "PENDING" and p.expires_at > now
                and (p.user_id, p.claim_id, p.action, p.document) == (user.id, claim_id, action, document)):
            pending = p
            break
    else:
        pending = PendingAction(
            id=f"PA-{uuid.uuid4().hex[:8]}", user_id=user.id, claim_id=claim_id, action=action, document=document,
            workshop_id=workshop["workshop_id"], workshop_name=workshop["name"], channel=workshop["channels"][0],
            expires_at=now + PENDING_TTL,
        )
        PENDING[pending.id] = pending
    return {
        "status": "PENDING_CONFIRMATION",
        "pending_action_id": pending.id,
        "summary": pending.summary(),
        "instruction": "NO se ha ejecutado nada. Explica al usuario lo que se va a hacer y dile que debe confirmarlo; "
                       "la confirmación la gestiona el sistema, no tú.",
    }


# --- NUEVO: confirmar / cancelar (código determinista; el LLM no participa) -----------------
def confirm_and_execute(pending_id: str, user: User, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    p = PENDING.get(pending_id)
    if p is None or p.user_id != user.id:  # solo quien propuso puede confirmar
        return {"ok": False, "message": "Acción pendiente no encontrada."}
    if p.status != "PENDING":  # un solo uso: no se puede repetir
        return {"ok": False, "message": f"La acción {p.id} ya no está pendiente (estado: {p.status}); no se repite."}
    if now > p.expires_at:
        p.status = "EXPIRED"
        return {"ok": False, "message": "La propuesta ha caducado. Vuelve a pedirla."}
    # Los permisos se comprueban OTRA VEZ ahora, no solo al proponer.
    if user.role != "operator" or p.claim_id not in user.allowed_claims:
        return {"ok": False, "message": "No tienes permiso para ejecutar esta acción."}
    # El estado puede haber cambiado desde la propuesta: se vuelve a comprobar.
    claim = FAKE_CLAIMS.get(p.claim_id)
    if claim is None or p.document not in claim["missing_documents"]:
        p.status = "CANCELLED"
        return {"ok": False, "message": "El expediente ha cambiado: el documento ya no figura como pendiente. Acción cancelada."}

    p.status = "EXECUTING"  # bloquea reentradas antes de llamar a la API
    try:
        response = actions_api(p.claim_id, {"action": p.action, "document": p.document}, idempotency_key=p.id)
    except Exception as err:  # noqa: BLE001
        p.status = "FAILED"
        return {"ok": False, "message": f"No se ha podido confirmar la ejecución ({type(err).__name__}). "
                                        "No se da por hecha; revisa el expediente antes de reintentar."}
    p.status = "EXECUTED"
    return {"ok": True, "message": f"Acción ejecutada: {p.summary()}. Referencia {response['action_id']}."}


def cancel_pending(pending_id: str, user: User) -> dict:
    p = PENDING.get(pending_id)
    if p is None or p.user_id != user.id or p.status != "PENDING":
        return {"ok": False, "message": "Acción pendiente no encontrada."}
    p.status = "CANCELLED"
    return {"ok": True, "message": "Acción cancelada. No se ha modificado nada."}


# --- Registro de tools ---------------------------------------------------------------------
@dataclass
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    func: Callable[..., dict]
    writes: bool = False  # NUEVO: ¿puede modificar algo? Solo los operator pueden usarla.


REGISTRY: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool("get_claim", "Consulta el estado de un expediente: estado, vehículo, taller y documentos que faltan.", ClaimIdArgs, get_claim),
        Tool("get_workshop", "Consulta los datos de un taller: nombre, contacto, estado y canales disponibles.", WorkshopIdArgs, get_workshop),
        Tool("get_claim_events", "Histórico de eventos de un expediente.", ClaimIdArgs, get_claim_events),
        Tool("propose_action", "Prepara la solicitud de un documento al taller. NO la ejecuta: requiere confirmación posterior "
                               "del usuario. Úsala solo cuando el usuario pida pedir/solicitar un documento.",
             ProposeActionArgs, propose_action, writes=True),
    ]
}


def tools_for_llm(user: User) -> list[dict]:
    """NUEVO. Mínimo privilegio: un viewer ni siquiera ve las tools de escritura."""
    return [
        {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.args_model.model_json_schema()}}
        for t in REGISTRY.values()
        if not t.writes or user.role == "operator"
    ]


# --- Punto de control único ----------------------------------------------------------------
def execute_tool_call(user: User, name: str, raw_arguments: str) -> dict:
    tool = REGISTRY.get(name)
    if tool is None:
        return {"error": f"Tool no permitida: {name}"}
    # NUEVO: aunque el LLM pida una tool que no le mostramos, aquí se vuelve a comprobar (defensa en profundidad).
    if tool.writes and user.role != "operator":
        return {"error": f"Tool {name} no disponible para tu rol."}
    try:
        args = tool.args_model.model_validate_json(raw_arguments)
    except ValidationError as err:
        return {"error": f"Argumentos inválidos para {name}: {err.errors(include_url=False, include_input=False)}"}
    claim_id = getattr(args, "claim_id", None)
    if claim_id and claim_id not in user.allowed_claims:
        return {"error": f"Sin permiso para acceder al expediente {claim_id}."}
    try:
        return tool.func(user, **args.model_dump())
    except Exception as err:  # noqa: BLE001
        return {"error": f"Fallo interno ejecutando {name}: {type(err).__name__}"}


SYSTEM = (
    "Eres un asistente de un taller de reparación de vehículos. Responde en español, breve. "
    "Usa únicamente la información que devuelvan las herramientas. Si una herramienta devuelve un error "
    "o falta información, dilo claramente; no inventes datos ni supongas nada. "
    "Tú NUNCA ejecutas acciones: solo puedes prepararlas con propose_action y pedir al usuario que las confirme. "
    "No afirmes nunca que una acción se ha realizado."
)
SYSTEM_VIEWER = (
    " IMPORTANTE: este usuario tiene rol de SOLO CONSULTA y no tiene permisos para solicitar ni modificar nada. "
    "Si pide una acción (por ejemplo, pedir un documento al taller), dile claramente que no tiene permisos para ello. "
    "Puedes informarle del estado del expediente, pero no ofrezcas ni propongas ejecutar acciones."
)


def system_prompt(user: User) -> str:
    """El LLM debe saber qué puede hacer este usuario para explicarlo bien. Es solo redacción:
    la barrera real (tool oculta + control en execute_tool_call) no depende de este texto."""
    return SYSTEM + (SYSTEM_VIEWER if user.role == "viewer" else "")


def run_agent(client: OpenAI, model: str, question: str, user: User) -> tuple[str, list[str]]:
    """Devuelve (respuesta del LLM, ids de acciones pendientes creadas en esta ejecución)."""
    messages = [{"role": "system", "content": system_prompt(user)}, {"role": "user", "content": question}]
    tools = tools_for_llm(user)
    pending_ids: list[str] = []

    for step in range(1, MAX_STEPS + 1):
        msg = client.chat.completions.create(model=model, messages=messages, tools=tools).choices[0].message
        if not msg.tool_calls:
            return msg.content or "", pending_ids
        messages.append({
            "role": "assistant", "content": msg.content,
            "tool_calls": [{"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                           for tc in msg.tool_calls],
        })
        for tc in msg.tool_calls:
            result = execute_tool_call(user, tc.function.name, tc.function.arguments)
            print(f"  [vuelta {step}] {tc.function.name}({tc.function.arguments}) -> {json.dumps(result, ensure_ascii=False)[:150]}")
            if result.get("status") == "PENDING_CONFIRMATION" and result["pending_action_id"] not in pending_ids:
                pending_ids.append(result["pending_action_id"])
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result, ensure_ascii=False)})

    return "No he podido completar la consulta (demasiados pasos). Inténtalo de nuevo o contacta con un operador.", pending_ids


def ask_confirmation(user: User, pending_id: str) -> None:
    """La confirmación la pide y la lee el PROGRAMA. El texto mostrado sale de lo guardado, no del LLM."""
    p = PENDING[pending_id]
    print(f"\n--- Acción pendiente {p.id} (caduca en {int(PENDING_TTL.total_seconds() // 60)} min) ---")
    print(p.summary())
    while True:
        try:
            answer = input("¿Confirmas que ejecute la acción? [s/n]: ").strip().lower()
        except EOFError:  # sin terminal interactiva no hay quien confirme: se cancela, nunca se ejecuta
            print("\nSin respuesta del usuario.")
            result = cancel_pending(p.id, user)
            break
        if answer in ("s", "si", "sí"):
            result = confirm_and_execute(p.id, user)
            break
        if answer in ("n", "no"):
            result = cancel_pending(p.id, user)
            break
        print("Respuesta no válida. Escribe 's' para confirmar o 'n' para cancelar.")  # cualquier otra cosa: se vuelve a preguntar
    print(result["message"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("question", nargs="?", default="Pide al taller la foto de matrícula que falta del expediente EXP-10234.")
    parser.add_argument("--user", choices=sorted(USERS), default="ana")
    args = parser.parse_args()

    api_key, model = os.environ.get("LLM_API_KEY"), os.environ.get("LLM_MODEL")
    if not model or model.startswith("pon-aqui") or not api_key or api_key.startswith("pon-aqui"):
        raise SystemExit("Configura LLM_API_KEY y LLM_MODEL en .env (mira .env.example).")
    client = OpenAI(api_key=api_key, base_url=os.environ.get("LLM_BASE_URL"))

    user = USERS[args.user]
    print(f"Usuario: {user.id} ({user.role})\nPregunta: {args.question}\n")
    answer, pending_ids = run_agent(client, model, args.question, user)
    print(answer)
    for pending_id in pending_ids:
        ask_confirmation(user, pending_id)


if __name__ == "__main__":
    main()
