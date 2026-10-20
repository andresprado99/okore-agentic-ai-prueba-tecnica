"""Las herramientas del agente y el PUNTO DE CONTROL por el que pasa toda petición del LLM (ver paso 3 y 4)."""
from dataclasses import dataclass
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.actions import ActionManager
from app.audit import Trace
from app.auth import User
from app.services import KIND_ES, LABELS, ServiceError, Services, call, read_claim_events


@dataclass
class Ctx:
    """Todo lo que una tool necesita saber de la petición en curso."""

    user: User
    trace: Trace
    services: Services
    actions: ActionManager


# --- Argumentos (esquema + validación) -----------------------------------------------------
class StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")  # un argumento inesperado es señal de que el LLM improvisa


class ClaimIdArgs(StrictArgs):
    claim_id: str = Field(pattern=r"^EXP-\d{5}$", description="Identificador del expediente, formato EXP-12345.")


class WorkshopIdArgs(StrictArgs):
    workshop_id: str = Field(pattern=r"^T-\d{3}$", description="Identificador del taller, formato T-228.")


class ProposeActionArgs(StrictArgs):
    claim_id: str = Field(pattern=r"^EXP-\d{5}$", description="Identificador del expediente, formato EXP-12345.")
    action: Literal["REQUEST_DOCUMENT"] = Field(description="Tipo de acción. Solo se permite REQUEST_DOCUMENT.")
    document: Literal["PHOTO_PLATE", "PHOTO_DAMAGE", "REPAIR_QUOTE"] = Field(description="Documento que se pide al taller.")


# --- Funciones de las tools ----------------------------------------------------------------
def get_claim(ctx: Ctx, claim_id: str) -> dict:
    return call(ctx.services.claims, ctx.trace, "GET", f"/api/claims/{claim_id}")


def get_workshop(ctx: Ctx, workshop_id: str) -> dict:
    return call(ctx.services.workshops, ctx.trace, "GET", f"/api/workshops/{workshop_id}")


def get_claim_events(ctx: Ctx, claim_id: str) -> dict:
    return {"claim_id": claim_id, "events": read_claim_events(ctx.services.db_path, ctx.trace, claim_id)}


def propose_action(ctx: Ctx, claim_id: str, action: str, document: str) -> dict:
    return ctx.actions.propose(ctx.user, ctx.trace, claim_id, action, document)


# --- Registro ------------------------------------------------------------------------------
@dataclass
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    func: Callable[..., dict]
    writes: bool = False  # ¿puede modificar algo? Solo los operator la ven y la pueden usar.


REGISTRY: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool("get_claim", "Consulta el estado de un expediente: estado, vehículo, taller y documentos que faltan.", ClaimIdArgs, get_claim),
        Tool("get_workshop", "Consulta los datos de un taller: nombre, contacto, estado y canales disponibles.", WorkshopIdArgs, get_workshop),
        Tool("get_claim_events", "Histórico de eventos de un expediente, para saber qué ha ocurrido y cuándo.", ClaimIdArgs, get_claim_events),
        Tool("propose_action", "Prepara la solicitud de un documento al taller. NO la ejecuta: requiere confirmación posterior "
                               "del usuario. Úsala solo cuando el usuario pida pedir/solicitar un documento.",
             ProposeActionArgs, propose_action, writes=True),
    ]
}


def tools_for_llm(user: User) -> list[dict]:
    """Mínimo privilegio: cada usuario ve solo las tools que puede usar."""
    return [
        {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.args_model.model_json_schema()}}
        for t in REGISTRY.values()
        if not t.writes or user.can_modify
    ]


# --- Punto de control único ----------------------------------------------------------------
def execute_tool_call(ctx: Ctx, name: str, raw_arguments: str) -> dict:
    """Valida y ejecuta una petición del LLM. Nunca lanza excepciones: todo problema vuelve como {"error": ...}."""
    tool = REGISTRY.get(name)
    if tool is None:
        return {"error": f"Tool no permitida: {name}"}
    if tool.writes and not ctx.user.can_modify:  # defensa en profundidad: aunque el LLM pida una tool que no le mostramos
        return {"error": f"Tool {name} no disponible para tu rol."}
    try:
        args = tool.args_model.model_validate_json(raw_arguments)
    except ValidationError as err:
        return {"error": f"Argumentos inválidos para {name}: {err.errors(include_url=False, include_input=False)}"}
    claim_id = getattr(args, "claim_id", None)
    if claim_id and claim_id not in ctx.user.allowed_claims:  # antes de mirar si existe: no revelar qué expedientes hay
        return {"error": f"Sin permiso para acceder al expediente {claim_id}."}
    try:
        return tool.func(ctx, **args.model_dump())
    except ServiceError as err:
        return service_error_result(err)
    except Exception as err:  # noqa: BLE001 - frontera deliberada: el agente sigue vivo
        ctx.trace.add_error(f"tool:{name}", type(err).__name__, str(err))
        return {"error": f"Fallo interno ejecutando {name}: {type(err).__name__}"}


def service_error_result(err: ServiceError) -> dict:
    """Convierte un fallo de servicio en un resultado que el LLM puede leer SIN poder confundirlo con un dato."""
    info = err.info
    if err.kind == "not_found":
        return {"error": f"No encontrado: {info.path}"}
    label = LABELS.get(info.service, info.service)
    return {
        "error": f"El servicio de {label} no está disponible ({KIND_ES.get(err.kind, err.kind)}, {info.attempts} intento(s)). "
                 "No dispongo de esa información: no la inventes ni la supongas.",
        "service_unavailable": True,
        "service": info.service,
    }
