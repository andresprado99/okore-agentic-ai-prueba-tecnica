"""Caso 3 - Fallo de un servicio: no inventar, no dar por hecha una operación, no ocultar el error.

Los fallos se provocan en el servicio simulado real (500, lentitud, respuesta perdida...), no con mocks de Python.
"""
import dataclasses
import time

from app.auth import USERS
from tests.fakes import ScriptedLLM, call

ANA = USERS["ana"]
GET_CLAIM = call("get_claim", {"claim_id": "EXP-10234"})
GET_WORKSHOP = call("get_workshop", {"workshop_id": "T-228"})
PROPOSE = call("propose_action", {"claim_id": "EXP-10234", "action": "REQUEST_DOCUMENT", "document": "PHOTO_PLATE"})


def service_call(rec, service):
    return next(c for c in rec["service_calls"] if c["service"] == service)


# --- Lecturas ---------------------------------------------------------------------------------
def test_persistent_500_is_retried_then_reported_not_hidden(make_agent, faults, records):
    faults.set("workshops", "error_500")
    agent = make_agent(ScriptedLLM([GET_CLAIM, GET_WORKSHOP], "Respuesta con lo que hay."))
    result = agent.handle(ANA, "¿Cómo contacto con el taller de EXP-10234?")
    rec = records()[-1]

    call_info = service_call(rec, "workshops")
    assert call_info["attempts"] == 3 and call_info["error_kind"] == "server_error"
    assert result.final_decision == "SERVICE_DEGRADED"
    assert "Aviso del sistema" in result.answer and "talleres" in result.answer  # lo añade el CÓDIGO, no el LLM
    assert any(e["where"] == "service:workshops" for e in rec["errors"])
    # Respuesta parcial: expedientes sí respondió.
    assert next(t for t in rec["tools_called"] if t["name"] == "get_claim")["status"] == "ok"
    # Al LLM se le ordena no inventar.
    assert "no la inventes" in agent.llm.calls[1][0][-1]["content"]


def test_transient_failure_is_absorbed_by_retry(make_agent, faults, records):
    faults.set("workshops", "error_500", times=1)
    result = make_agent(ScriptedLLM([GET_WORKSHOP], "ok")).handle(ANA, "taller?")
    info = service_call(records()[-1], "workshops")
    assert info["attempts"] == 2 and info["outcome"] == "ok"
    assert result.final_decision == "ANSWERED" and "Aviso" not in result.answer


def test_slow_service_times_out_without_waiting_forever(make_agent, faults, records):
    faults.set("workshops", "slow", delay_s=3)
    start = time.perf_counter()
    result = make_agent(ScriptedLLM([GET_WORKSHOP], "x")).handle(ANA, "taller?")
    elapsed = time.perf_counter() - start

    info = service_call(records()[-1], "workshops")
    assert info["error_kind"] == "timeout" and info["attempts"] == 3
    assert elapsed < 4 and result.final_decision == "SERVICE_DEGRADED"


def test_not_found_is_definitive_and_not_retried(make_agent, records):
    result = make_agent(ScriptedLLM([call("get_workshop", {"workshop_id": "T-999"})], "x")).handle(ANA, "?")
    info = service_call(records()[-1], "workshops")
    assert info["error_kind"] == "not_found" and info["attempts"] == 1
    assert result.final_decision == "TOOL_CALLS_REJECTED"  # un 404 no es "servicio caído"


def test_unreachable_service(make_agent, records):
    agent = make_agent(ScriptedLLM([GET_CLAIM], "x"), services_base_url="http://127.0.0.1:9")
    result = agent.handle(ANA, "?")
    info = records()[-1]["service_calls"][0]
    assert info["error_kind"] in ("connection", "timeout") and info["attempts"] == 3  # Windows tarda en rechazar y lo trata como timeout
    assert result.final_decision == "SERVICE_DEGRADED"


def test_missing_database_is_reported(make_agent, tmp_path):
    agent = make_agent(ScriptedLLM([call("get_claim_events", {"claim_id": "EXP-10234"})], "x"), db_path=tmp_path / "no_existe.db")
    result = agent.handle(ANA, "?")
    assert result.final_decision == "SERVICE_DEGRADED" and "histórico de eventos" in result.answer


# --- Escrituras --------------------------------------------------------------------------------
def test_no_action_is_proposed_if_the_workshop_service_is_down(make_agent, faults):
    faults.set("workshops", "error_500")
    agent = make_agent(ScriptedLLM([PROPOSE], "No puedo."))
    result = agent.handle(ANA, "Pide la foto")
    assert result.pending_ids == [] and not agent.actions.pending
    assert result.final_decision == "SERVICE_DEGRADED"


def test_lost_response_is_recovered_by_retrying_with_the_same_key(make_agent, faults, records, db_events):
    """La API ejecuta la acción y falla al responder UNA vez: el reintento recupera el resultado sin duplicar."""
    faults.set("actions", "execute_then_500", times=1)
    agent = make_agent(ScriptedLLM([PROPOSE], "ok"))
    result = agent.handle(ANA, "Pide la foto")
    outcome = agent.confirm(result.pending_ids[0], ANA)

    assert outcome["ok"] and outcome["replayed"]
    assert service_call(records()[-1], "actions")["attempts"] == 2
    assert db_events().count("DOCUMENT_REQUESTED") == 1  # NO se duplicó


def test_permanent_lost_response_is_reported_as_unknown_never_as_success(make_agent, faults, records, db_events):
    faults.set("actions", "execute_then_500")
    agent = make_agent(ScriptedLLM([PROPOSE], "ok"))
    result = agent.handle(ANA, "Pide la foto")
    outcome = agent.confirm(result.pending_ids[0], ANA)

    assert not outcome["ok"] and outcome["status"] == "UNKNOWN"
    assert "No he podido confirmar" in outcome["message"]
    assert service_call(records()[-1], "actions")["attempts"] == 3
    assert db_events().count("DOCUMENT_REQUESTED") == 1  # tres intentos, un solo efecto: la idempotencia funcionó
    assert not agent.confirm(result.pending_ids[0], ANA)["ok"]  # y no se reintenta a ciegas desde el estado desconocido


def test_action_slower_than_the_timeout_still_produces_a_single_effect(make_agent, faults, db_events):
    faults.set("actions", "slow", delay_s=1.5, times=1)
    agent = make_agent(ScriptedLLM([PROPOSE], "ok"))
    result = agent.handle(ANA, "Pide la foto")
    agent.confirm(result.pending_ids[0], ANA)
    time.sleep(1.8)  # deja terminar la petición lenta del servidor
    assert db_events().count("DOCUMENT_REQUESTED") == 1


# --- El LLM y el propio agente -----------------------------------------------------------------
def test_llm_failure_gives_a_controlled_answer_and_a_trace(make_agent, records):
    result = make_agent(ScriptedLLM(RuntimeError("boom"))).handle(ANA, "?")
    assert result.final_decision == "LLM_ERROR" and "No se ha realizado ninguna acción" in result.answer
    assert records()[-1]["errors"][0]["where"] == "llm"


def test_agent_stops_after_max_steps(make_agent):
    result = make_agent(ScriptedLLM([GET_CLAIM])).handle(ANA, "?")  # un LLM que nunca termina
    assert result.final_decision == "MAX_STEPS_EXCEEDED"


def test_retry_policy_is_configurable(make_agent, faults, records):
    faults.set("workshops", "error_500")
    make_agent(ScriptedLLM([GET_WORKSHOP], "x"), service_max_attempts=1).handle(ANA, "?")
    assert service_call(records()[-1], "workshops")["attempts"] == 1
