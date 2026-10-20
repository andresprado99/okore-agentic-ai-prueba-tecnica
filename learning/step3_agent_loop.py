"""Paso 3: el bucle completo del agente, con varias tools y validación antes de ejecutar.

Novedades respecto al paso 2:
  - Varias tools, organizadas en un REGISTRO (una sola fuente de verdad).
  - Un BUCLE: el LLM puede encadenar varias herramientas hasta tener la respuesta.
  - Un único punto de control, execute_tool_call(), por el que pasa TODA petición del LLM.
  - Un tope de vueltas, para que el agente nunca se quede en bucle infinito.

Uso:  python learning/step3_agent_loop.py "tu pregunta"
"""
import json
import os
import sys
from dataclasses import dataclass
from typing import Callable

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, ConfigDict, Field, ValidationError

load_dotenv()

MAX_STEPS = 6  # tope de vueltas LLM -> tools -> LLM

# --- Datos de mentira (más adelante: API FastAPI simulada + SQLite) ------------------------
FAKE_CLAIMS = {
    "EXP-10234": {"claim_id": "EXP-10234", "status": "REPAIRING", "vehicle": {"plate": "1234ABC", "brand": "BMW", "model": "X1"},
                  "workshop_id": "T-228", "missing_documents": ["PHOTO_PLATE"], "last_update": "2026-09-22T10:32:00"},
    "EXP-10567": {"claim_id": "EXP-10567", "status": "CLOSED", "vehicle": {"plate": "5678XYZ", "brand": "Seat", "model": "Ibiza"},
                  "workshop_id": "T-101", "missing_documents": [], "last_update": "2026-08-30T16:05:00"},
    "EXP-20000": {"claim_id": "EXP-20000", "status": "REPAIRING", "vehicle": {"plate": "9999ZZZ", "brand": "Audi", "model": "A3"},
                  "workshop_id": "T-228", "missing_documents": [], "last_update": "2026-09-30T09:00:00"},
}
FAKE_WORKSHOPS = {
    "T-228": {"workshop_id": "T-228", "name": "Talleres Norte", "contact": "norte@example.com", "status": "ACTIVE", "channels": ["EMAIL", "PORTAL"]},
    "T-101": {"workshop_id": "T-101", "name": "Taller Centro", "contact": "centro@example.com", "status": "ACTIVE", "channels": ["EMAIL"]},
}
FAKE_EVENTS = {
    "EXP-10234": [
        {"event_date": "2026-09-15", "event_type": "CLAIM_OPENED", "description": "Expediente abierto"},
        {"event_date": "2026-09-18", "event_type": "VEHICLE_RECEIVED", "description": "Vehículo recibido en el taller T-228"},
        {"event_date": "2026-09-22", "event_type": "REPAIR_STARTED", "description": "Reparación iniciada"},
    ],
}

# Simulación del usuario autenticado: solo puede ver estos expedientes.
# (En producción vendría de la identidad del usuario y un sistema de permisos.)
CURRENT_USER_ALLOWED_CLAIMS = {"EXP-10234", "EXP-10567"}


# --- Argumentos de cada tool (esquema + validación, con Pydantic) --------------------------
class StrictArgs(BaseModel):
    """Base común: rechaza cualquier argumento que no esté declarado (por defecto Pydantic lo ignoraría)."""

    model_config = ConfigDict(extra="forbid")


class ClaimIdArgs(StrictArgs):
    claim_id: str = Field(pattern=r"^EXP-\d{5}$", description="Identificador del expediente, formato EXP-12345.")


class WorkshopIdArgs(StrictArgs):
    workshop_id: str = Field(pattern=r"^T-\d{3}$", description="Identificador del taller, formato T-228.")


# --- Las tools: funciones normales que solo hacen su trabajo -------------------------------
def get_claim(claim_id: str) -> dict:
    return FAKE_CLAIMS.get(claim_id) or {"error": f"Expediente {claim_id} no encontrado."}


def get_workshop(workshop_id: str) -> dict:
    return FAKE_WORKSHOPS.get(workshop_id) or {"error": f"Taller {workshop_id} no encontrado."}


def get_claim_events(claim_id: str) -> dict:
    return {"claim_id": claim_id, "events": FAKE_EVENTS.get(claim_id, [])}


# --- El REGISTRO: una sola lista define qué tools existen -----------------------------------
# Lo que no esté aquí, no existe para el agente (es la allowlist).
@dataclass
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    func: Callable[..., dict]


REGISTRY: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool("get_claim", "Consulta el estado de un expediente: estado, vehículo, taller y documentos que faltan.", ClaimIdArgs, get_claim),
        Tool("get_workshop", "Consulta los datos de un taller: nombre, contacto, estado y canales disponibles.", WorkshopIdArgs, get_workshop),
        Tool("get_claim_events", "Histórico de eventos de un expediente, para saber qué ha ocurrido y cuándo.", ClaimIdArgs, get_claim_events),
    ]
}

# Lo que se le envía al LLM se GENERA a partir del registro: nunca se desincroniza.
TOOLS_FOR_LLM = [
    {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.args_model.model_json_schema()}}
    for t in REGISTRY.values()
]


# --- El punto de control: TODA petición del LLM pasa por aquí ------------------------------
def execute_tool_call(name: str, raw_arguments: str) -> dict:
    """Valida y ejecuta una petición del LLM. Nunca lanza una excepción hacia fuera:
    cualquier problema se devuelve como {"error": ...} para que el LLM lo lea y lo explique."""
    # 1. ¿La tool existe? (allowlist)
    tool = REGISTRY.get(name)
    if tool is None:
        return {"error": f"Tool no permitida: {name}"}
    # 2. ¿Los argumentos son JSON válido y cumplen el esquema?
    try:
        args = tool.args_model.model_validate_json(raw_arguments)
    except ValidationError as err:
        return {"error": f"Argumentos inválidos para {name}: {err.errors(include_url=False, include_input=False)}"}
    # 3. ¿El usuario tiene permiso sobre ese expediente? (autorización, en código, no en el prompt)
    #    Se comprueba ANTES de mirar si existe: así no revelamos qué expedientes existen.
    claim_id = getattr(args, "claim_id", None)
    if claim_id and claim_id not in CURRENT_USER_ALLOWED_CLAIMS:
        return {"error": f"Sin permiso para acceder al expediente {claim_id}."}
    # 4. Ejecutar. Si la tool falla por dentro, tampoco tumbamos el agente.
    try:
        return tool.func(**args.model_dump())
    except Exception as err:  # noqa: BLE001 - frontera deliberada: el agente sigue vivo
        return {"error": f"Fallo interno ejecutando {name}: {type(err).__name__}"}


SYSTEM = (
    "Eres un asistente de un taller de reparación de vehículos. Responde en español, breve. "
    "Usa únicamente la información que devuelvan las herramientas. Si una herramienta devuelve un error "
    "o falta información, dilo claramente; no inventes datos ni supongas nada."
)


def run_agent(client: OpenAI, model: str, question: str) -> str:
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}]

    for step in range(1, MAX_STEPS + 1):
        response = client.chat.completions.create(model=model, messages=messages, tools=TOOLS_FOR_LLM)
        msg = response.choices[0].message

        # Si el LLM no pide herramientas, ha terminado: esa es la respuesta final.
        if not msg.tool_calls:
            return msg.content or ""

        # Guardar en el historial lo que pidió el LLM...
        messages.append(
            {
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ],
            }
        )
        # ...y contestar CADA petición (el LLM puede pedir varias a la vez).
        for tc in msg.tool_calls:
            result = execute_tool_call(tc.function.name, tc.function.arguments)
            print(f"  [vuelta {step}] {tc.function.name}({tc.function.arguments}) -> {json.dumps(result, ensure_ascii=False)[:150]}")
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result, ensure_ascii=False)})

    # Si llegamos aquí, el LLM no ha terminado en MAX_STEPS vueltas: cortamos nosotros.
    return "No he podido completar la consulta (demasiados pasos). Inténtalo de nuevo o contacta con un operador."


def main() -> None:
    api_key = os.environ.get("LLM_API_KEY")
    model = os.environ.get("LLM_MODEL")
    if not model or model.startswith("pon-aqui") or not api_key or api_key.startswith("pon-aqui"):
        raise SystemExit("Configura LLM_API_KEY y LLM_MODEL en .env (mira .env.example).")
    client = OpenAI(api_key=api_key, base_url=os.environ.get("LLM_BASE_URL"))

    question = sys.argv[1] if len(sys.argv) > 1 else "¿En qué estado está el expediente EXP-10234 y qué estamos esperando?"
    print(f"Pregunta: {question}\n")
    print(run_agent(client, model, question))


if __name__ == "__main__":
    main()
