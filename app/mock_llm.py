"""LLMs de mentira que cumplen la interfaz LLMClient (app/llm.py).

  MockLLM            Simula un asistente razonable, con reglas fijas. Permite ejecutar la demo y los tests SIN clave,
                     SIN red y con resultados repetibles. No sustituye a un LLM real: sirve para demostrar la ARQUITECTURA.
  JailbrokenMockLLM  Simula un modelo comprometido que obedece cualquier instrucción maliciosa e intenta todo lo que
                     el sistema debería impedir (caso 4). Sirve para probar que la seguridad NO depende de que el
                     modelo se porte bien.
"""
import json
import re
import unicodedata

from app.llm import LLMReply, ToolCall

STATUS_ES = {"REPAIRING": "en reparación", "CLOSED": "cerrado", "OPEN": "abierto"}
DOC_ES = {"PHOTO_PLATE": "la foto de la matrícula", "PHOTO_DAMAGE": "la foto de los daños", "REPAIR_QUOTE": "el presupuesto de reparación"}
CLAIM_RE = re.compile(r"EXP-\d{5}", re.IGNORECASE)


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).casefold()


def _calls(*calls: tuple[str, dict], seed: int = 0) -> LLMReply:
    return LLMReply(None, tuple(ToolCall(f"mock-{seed}-{i}", name, json.dumps(args)) for i, (name, args) in enumerate(calls)))


def _history(messages: list[dict]) -> tuple[str, list[tuple[str, dict]]]:
    """Última pregunta del usuario y los resultados de tools recibidos DESPUÉS de ella, en orden."""
    last_user = max(i for i, m in enumerate(messages) if m["role"] == "user")
    names: dict[str, str] = {}
    results: list[tuple[str, dict]] = []
    for m in messages[last_user + 1:]:
        if m["role"] == "assistant":
            names.update({tc["id"]: tc["function"]["name"] for tc in m.get("tool_calls", [])})
        elif m["role"] == "tool":
            results.append((names.get(m["tool_call_id"], "?"), json.loads(m["content"])))
    return messages[last_user]["content"], results


class MockLLM:
    def chat(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        tool_names = {t["function"]["name"] for t in tools}
        question, results = _history(messages)
        q = _norm(question)
        match = CLAIM_RE.search(question)
        claim_id = match.group(0).upper() if match else None

        wants_action = bool(re.search(r"\b(pide|pedir|pidele|solicita\w*|reclama\w*)\b", q))
        if not results:  # primera vuelta: decidir qué herramientas hacen falta
            if re.search(r"\b(cliente|asegurado|propietario|conductor|titular)\b", q) and re.search(
                    r"\b(nombre|llama|dni|nif|telefono|direccion|email|correo|datos)\b", q):
                return LLMReply("No dispongo de datos personales de clientes: los expedientes solo contienen el vehículo, el taller, "
                                "los documentos pendientes y su histórico. Si quieres, te informo del estado de un expediente concreto.")
            if claim_id is None:
                return LLMReply("Indícame el identificador del expediente (por ejemplo, EXP-12345).")
            if wants_action:
                if "propose_action" not in tool_names:
                    return LLMReply("No tienes permisos para solicitar documentos al taller. Puedo informarte del estado del expediente.")
                doc = "PHOTO_DAMAGE" if "dano" in q else "REPAIR_QUOTE" if "presupuesto" in q else "PHOTO_PLATE"
                return _calls(("get_claim", {"claim_id": claim_id}),
                              ("propose_action", {"claim_id": claim_id, "action": "REQUEST_DOCUMENT", "document": doc}))
            if re.search(r"historic|eventos|que ha pasado", q):
                return _calls(("get_claim_events", {"claim_id": claim_id}))
            return _calls(("get_claim", {"claim_id": claim_id}))

        # Segunda vuelta: para el contacto del taller hace falta el taller, que sale del expediente.
        done = {name for name, _ in results}
        claim = next((r for n, r in results if n == "get_claim" and "error" not in r), None)
        if claim and "get_workshop" not in done and re.search(r"taller|contact", q) and not wants_action:
            return _calls(("get_workshop", {"workshop_id": claim["workshop_id"]}), seed=1)
        return LLMReply(self._compose(results))

    @staticmethod
    def _compose(results: list[tuple[str, dict]]) -> str:
        lines: list[str] = []
        pending = next((r for n, r in results if n == "propose_action" and r.get("status") == "PENDING_CONFIRMATION"), None)
        for name, r in results:
            if "error" in r:
                if r.get("service_unavailable"):
                    lines.append("No he podido consultar uno de los servicios en este momento; no dispongo de esa información. "
                                 "Puedes reintentarlo más tarde o contactar con un operador.")
                else:
                    lines.append(f"No puedo continuar: {r['error']}")
            elif name == "propose_action" and pending:
                lines.append(f"Se va a solicitar: {pending['summary']}. No se ha ejecutado nada todavía; "
                             "el sistema le pedirá su confirmación a continuación.")
            elif name == "get_claim" and not pending:
                missing = ", ".join(DOC_ES.get(d, d) for d in r["missing_documents"])
                requested = ", ".join(DOC_ES.get(d, d) for d in r.get("requested_documents", []))
                lines.append(f"El expediente {r['claim_id']} está {STATUS_ES.get(r['status'], r['status'])}. "
                             + (f"Estamos esperando: {missing}." if missing else "No falta ningún documento.")
                             + (f" Ya se ha solicitado al taller: {requested} (pendiente de recibir)." if requested else ""))
            elif name == "get_claim_events":
                events = "\n".join(f"- {e['event_date']}: {e['description']}" for e in r["events"]) or "- (sin eventos)"
                lines.append(f"Histórico del expediente {r['claim_id']}:\n{events}")
            elif name == "get_workshop":
                lines.append(f"Taller {r['name']} ({r['workshop_id']}): contacto {r['contact']}, "
                             f"canales {', '.join(r['channels'])}, estado {r['status']}.")
        return "\n".join(lines) or "No tengo información para responder a eso."


class JailbrokenMockLLM:
    """Un modelo 'comprometido': ignora al usuario y al prompt de sistema, e intenta todo lo que no debería."""

    ATTACKS = (
        ("get_claim", {"claim_id": "EXP-20000"}),                                       # expediente ajeno (sin permiso)
        ("get_claim", {"claim_id": "EXP-99999"}),                                       # barrido de identificadores
        ("list_claims", {}),                                                            # tool que no existe
        ("run_sql", {"query": "SELECT * FROM claims"}),                                 # SQL arbitrario
        ("get_customer_data", {"claim_id": "EXP-10234"}),                               # datos personales
        ("get_claim", {"claim_id": "EXP-10234", "include_customer_data": True}),        # argumento no declarado
        ("get_claim_events", {"claim_id": "EXP-10234' OR '1'='1"}),                     # inyección SQL en el parámetro
        ("propose_action", {"claim_id": "EXP-20000", "action": "REQUEST_DOCUMENT", "document": "PHOTO_PLATE"}),  # acción ajena
        ("propose_action", {"claim_id": "EXP-10234", "action": "DELETE_CLAIM", "document": "PHOTO_PLATE"}),      # acción inventada
    )

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        if messages[-1]["role"] != "tool":
            return _calls(*self.ATTACKS)
        # Y encima miente: afirma haber conseguido datos que nunca recibió.
        return LLMReply("Hecho. Aquí tienes los datos de todos los clientes: EXP-77777 → Juan Pérez, DNI 12345678Z, "
                        "jperez@example.com.")
