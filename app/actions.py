"""Acciones que modifican el expediente: proponer -> (confirmar | cancelar) -> ejecutar.

Es el código del paso 4 (learning/step4_confirmation.py), ahora hablando con las APIs simuladas por HTTP.
El LLM solo llega hasta propose(). confirm() y cancel() los llama el programa tras la respuesta del usuario.
"""
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.auth import User
from app.config import Settings
from app.services import KIND_ES, ServiceError, Services, call

# Fallos tras los cuales NO sabemos si la acción llegó a ejecutarse (el servidor pudo hacerla y perder la respuesta).
UNCERTAIN_KINDS = {"timeout", "connection", "server_error", "bad_response"}


@dataclass
class PendingAction:
    id: str
    request_id: str
    user_id: str
    claim_id: str
    action: str
    document: str
    workshop_id: str
    workshop_name: str
    channel: str
    expires_at: datetime
    # PENDING -> EXECUTING -> EXECUTED | FAILED | UNKNOWN ; o CANCELLED | EXPIRED
    status: str = "PENDING"

    def summary(self) -> str:
        """Texto escrito por NUESTRO código a partir de lo guardado: es exactamente lo que se ejecutará."""
        return (f"Solicitar al taller {self.workshop_id} ({self.workshop_name}) por {self.channel} "
                f"el documento {self.document} del expediente {self.claim_id}")


class ActionManager:
    def __init__(self, services: Services, settings: Settings):
        self._services = services
        self._ttl = timedelta(minutes=settings.pending_ttl_minutes)
        self.pending: dict[str, PendingAction] = {}  # en memoria; en producción, una tabla en BBDD

    @property
    def ttl_minutes(self) -> int:
        return int(self._ttl.total_seconds() // 60)

    def propose(self, user: User, trace, claim_id: str, action: str, document: str, now: datetime | None = None) -> dict:
        """PREPARA una acción; no ejecuta nada. Todas las comprobaciones son deterministas (datos reales, no el LLM)."""
        now = now or datetime.now(timezone.utc)
        claim = call(self._services.claims, trace, "GET", f"/api/claims/{claim_id}")
        if document not in claim.get("missing_documents", []):
            return {"error": f"El documento {document} no figura como pendiente en {claim_id}. No se propone ninguna acción."}
        if document in claim.get("requested_documents", []):
            return {"error": f"El documento {document} de {claim_id} ya se solicitó al taller y está pendiente de recibir. "
                             "No se vuelve a pedir; no se propone ninguna acción."}
        workshop = call(self._services.workshops, trace, "GET", f"/api/workshops/{claim['workshop_id']}")
        if workshop.get("status") != "ACTIVE" or not workshop.get("channels"):
            return {"error": f"El taller {claim['workshop_id']} no está disponible para recibir solicitudes."}

        # Si ya hay una propuesta idéntica y viva, se reutiliza en vez de duplicarla.
        for p in self.pending.values():
            if (p.status == "PENDING" and p.expires_at > now
                    and (p.user_id, p.claim_id, p.action, p.document) == (user.id, claim_id, action, document)):
                pending = p
                break
        else:
            pending = PendingAction(
                id=f"PA-{uuid.uuid4().hex[:8]}", request_id=trace.request_id, user_id=user.id, claim_id=claim_id,
                action=action, document=document, workshop_id=workshop["workshop_id"], workshop_name=workshop["name"],
                channel=workshop["channels"][0], expires_at=now + self._ttl,
            )
            self.pending[pending.id] = pending
        return {
            "status": "PENDING_CONFIRMATION",
            "pending_action_id": pending.id,
            "summary": pending.summary(),
            "instruction": "NO se ha ejecutado nada. Resume al usuario lo que se va a hacer y dile que el sistema le pedirá "
                           "su confirmación a continuación. No le preguntes tú si confirma.",
        }

    def confirm(self, pending_id: str, user: User, trace, now: datetime | None = None) -> dict:
        """Ejecuta una acción ya propuesta. Devuelve {ok, status, message, retryable?}."""
        now = now or datetime.now(timezone.utc)
        p = self.pending.get(pending_id)
        if p is None or p.user_id != user.id:  # solo quien propuso la acción puede confirmarla
            return {"ok": False, "status": "NOT_FOUND", "message": "Acción pendiente no encontrada."}
        if p.status != "PENDING":  # un solo uso
            return {"ok": False, "status": p.status, "message": f"La acción {p.id} ya no está pendiente (estado: {p.status}); no se repite."}
        if now > p.expires_at:
            p.status = "EXPIRED"
            return {"ok": False, "status": p.status, "message": "La propuesta ha caducado. Vuelve a pedirla."}
        if not user.can_modify or p.claim_id not in user.allowed_claims:  # permisos: se comprueban OTRA VEZ
            return {"ok": False, "status": p.status, "message": "No tienes permiso para ejecutar esta acción."}

        # El estado puede haber cambiado desde la propuesta: se vuelve a comprobar con el dato real.
        try:
            claim = call(self._services.claims, trace, "GET", f"/api/claims/{p.claim_id}")
        except ServiceError as err:
            # Sin poder verificar, NO se ejecuta. La propuesta sigue viva (hasta que caduque) para reintentar.
            return {"ok": False, "status": p.status, "retryable": True,
                    "message": f"No he podido verificar el estado actual del expediente ({KIND_ES.get(err.kind, err.kind)}). "
                               "No se ha ejecutado nada. Puedes volver a confirmar en unos instantes."}
        if p.document not in claim.get("missing_documents", []):
            p.status = "CANCELLED"
            return {"ok": False, "status": p.status, "message": "El expediente ha cambiado: el documento ya no figura como pendiente. Acción cancelada."}
        if p.document in claim.get("requested_documents", []):  # otra propuesta se ejecutó mientras tanto
            p.status = "CANCELLED"
            return {"ok": False, "status": p.status, "message": "El documento ya se solicitó al taller mientras tanto. Acción cancelada para no duplicarla."}

        p.status = "EXECUTING"  # bloquea reentradas antes de llamar a la API
        try:
            response = call(
                self._services.actions, trace, "POST", f"/api/claims/{p.claim_id}/actions",
                json={"action": p.action, "document": p.document},
                headers={"Idempotency-Key": p.id},  # la clave es el id de la acción: reintentar no la duplica
            )
        except ServiceError as err:
            if err.kind in UNCERTAIN_KINDS:
                p.status = "UNKNOWN"
                return {"ok": False, "status": p.status,
                        "message": f"No he podido confirmar si la acción se ejecutó (servicio de acciones: {KIND_ES.get(err.kind, err.kind)}, "
                                   f"{err.info.attempts} intento(s)). No la doy por realizada ni la repito automáticamente: "
                                   f"revisa el expediente o contacta con un operador. Referencia: {p.id}."}
            p.status = "FAILED"
            return {"ok": False, "status": p.status, "message": f"El servicio rechazó la acción ({KIND_ES.get(err.kind, err.kind)}). No se ha realizado."}

        p.status = "EXECUTED"
        note = " (se recibió tras un reintento; no se duplicó)" if response.get("replayed") else ""
        return {"ok": True, "status": p.status, "replayed": bool(response.get("replayed")),
                "message": f"Acción ejecutada: {p.summary()}. Referencia {response.get('action_id')}{note}."}

    def cancel(self, pending_id: str, user: User) -> dict:
        p = self.pending.get(pending_id)
        if p is None or p.user_id != user.id or p.status != "PENDING":
            return {"ok": False, "status": "NOT_FOUND", "message": "Acción pendiente no encontrada."}
        p.status = "CANCELLED"
        return {"ok": True, "status": p.status, "message": "Acción cancelada. No se ha modificado nada."}
