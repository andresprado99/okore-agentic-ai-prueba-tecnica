"""Caso 2 - Acción con confirmación: comprobar, preparar, pedir confirmación y ejecutar SOLO tras confirmar."""
from datetime import timedelta

import pytest

from app.audit import Trace
from app.auth import USERS
from app.mock_llm import MockLLM
from tests.fakes import ScriptedLLM, call

ANA, LUIS = USERS["ana"], USERS["luis"]
PROPOSE = call("propose_action", {"claim_id": "EXP-10234", "action": "REQUEST_DOCUMENT", "document": "PHOTO_PLATE"})
ASK = "Pide al taller la foto de matrícula que falta del expediente EXP-10234."


def propose(make_agent, user=ANA):
    agent = make_agent(ScriptedLLM([PROPOSE], "Se va a solicitar."))
    return agent, agent.handle(user, ASK)


def test_proposing_changes_nothing(make_agent, records, db_events):
    agent, result = propose(make_agent)

    assert result.final_decision == "ACTION_PROPOSED" and len(result.pending_ids) == 1
    assert agent.actions.pending[result.pending_ids[0]].status == "PENDING"
    assert "DOCUMENT_REQUESTED" not in db_events()
    rec = records()[-1]
    assert rec["detected_intent"] == "ACTION_REQUEST" and rec["action_executed"] is False
    assert rec["action_requested"][0]["document"] == "PHOTO_PLATE"


def test_confirm_executes_exactly_once(make_agent, records, db_events):
    agent, result = propose(make_agent)
    first = agent.confirm(result.pending_ids[0], ANA)
    second = agent.confirm(result.pending_ids[0], ANA)

    assert first["ok"] and first["status"] == "EXECUTED"
    assert not second["ok"]  # la segunda confirmación no repite nada
    assert db_events().count("DOCUMENT_REQUESTED") == 1
    confirmation = [r for r in records() if r["type"] == "action_confirmation"][0]
    assert confirmation["request_id"] == result.request_id and confirmation["action_executed"] is True


def test_idempotency_key_is_the_pending_action_id(make_agent, records):
    agent, result = propose(make_agent)
    agent.confirm(result.pending_ids[0], ANA)
    post = [c for r in records() for c in r["service_calls"] if c["method"] == "POST"]
    assert len(post) == 1 and post[0]["attempts"] == 1


def test_only_the_proposer_can_confirm(make_agent, db_events):
    agent, result = propose(make_agent)
    assert not agent.confirm(result.pending_ids[0], LUIS)["ok"]
    assert "DOCUMENT_REQUESTED" not in db_events()


def test_cancel_prevents_later_confirmation(make_agent, db_events):
    agent, result = propose(make_agent)
    assert agent.cancel(result.pending_ids[0], ANA)["ok"]
    assert not agent.confirm(result.pending_ids[0], ANA)["ok"]
    assert "DOCUMENT_REQUESTED" not in db_events()


def test_identical_proposal_is_reused_not_duplicated(make_agent):
    agent, first = propose(make_agent)
    agent.llm = ScriptedLLM([PROPOSE], "ok")
    second = agent.handle(ANA, ASK)
    assert second.pending_ids == first.pending_ids and len(agent.actions.pending) == 1


def test_expired_proposal_is_rejected(make_agent, db_events):
    agent, result = propose(make_agent)
    p = agent.actions.pending[result.pending_ids[0]]
    outcome = agent.actions.confirm(p.id, ANA, Trace(), now=p.expires_at + timedelta(seconds=1))
    assert outcome["status"] == "EXPIRED" and "DOCUMENT_REQUESTED" not in db_events()


def test_document_that_is_not_missing_is_rejected(make_agent, db_events):
    agent = make_agent(ScriptedLLM([call("propose_action", {"claim_id": "EXP-10567", "action": "REQUEST_DOCUMENT", "document": "PHOTO_PLATE"})], "ok"))
    result = agent.handle(ANA, "Pide la foto de EXP-10567")
    assert result.pending_ids == [] and result.final_decision == "TOOL_CALLS_REJECTED"
    assert "no figura como pendiente" in agent.llm.calls[1][0][-1]["content"]


def test_state_change_between_proposal_and_confirmation_cancels(make_agent, db_events, settings):
    from app.mock_services import data

    agent, result = propose(make_agent)
    data.CLAIMS["EXP-10234"]["missing_documents"] = []  # alguien subió la foto mientras tanto
    try:
        outcome = agent.confirm(result.pending_ids[0], ANA)
    finally:
        data.CLAIMS["EXP-10234"]["missing_documents"] = ["PHOTO_PLATE"]
    assert outcome["status"] == "CANCELLED" and "DOCUMENT_REQUESTED" not in db_events()


def test_confirmation_is_not_executed_if_claim_cannot_be_verified(make_agent, faults, db_events):
    agent, result = propose(make_agent)
    faults.set("claims", "error_500")
    outcome = agent.confirm(result.pending_ids[0], ANA)
    assert not outcome["ok"] and outcome["retryable"] and "DOCUMENT_REQUESTED" not in db_events()

    faults.clear()  # al recuperarse el servicio, la misma propuesta se puede confirmar
    assert agent.confirm(result.pending_ids[0], ANA)["ok"]


# --- No volver a pedir un documento que ya se solicitó -----------------------------------------
def test_a_document_already_requested_cannot_be_requested_again(make_agent, db_events):
    agent, first = propose(make_agent)
    assert agent.confirm(first.pending_ids[0], ANA)["ok"]

    agent.llm = ScriptedLLM([PROPOSE], "ok")
    second = agent.handle(ANA, ASK)  # el mismo usuario insiste
    assert second.pending_ids == [] and second.final_decision == "TOOL_CALLS_REJECTED"
    assert "ya se solicitó" in agent.llm.calls[1][0][-1]["content"]
    assert db_events().count("DOCUMENT_REQUESTED") == 1


def test_claim_reports_what_has_already_been_requested(make_agent, settings):
    import httpx

    agent, result = propose(make_agent)
    url = f"{settings.services_base_url}/api/claims/EXP-10234"
    assert httpx.get(url).json()["requested_documents"] == []
    agent.confirm(result.pending_ids[0], ANA)
    assert httpx.get(url).json()["requested_documents"] == ["PHOTO_PLATE"]

    httpx.post(f"{settings.services_base_url}/_reset")  # restaurar datos: vuelve a poder pedirse
    assert httpx.get(url).json()["requested_documents"] == []


def test_a_second_pending_action_is_cancelled_if_the_first_one_already_ran(make_agent, db_events):
    """Dos propuestas vivas a la vez (p. ej. de dos sesiones): solo se ejecuta la primera que se confirma."""
    agent_a, first = propose(make_agent)
    agent_b, second = propose(make_agent)
    assert agent_a.confirm(first.pending_ids[0], ANA)["ok"]

    outcome = agent_b.confirm(second.pending_ids[0], ANA)
    assert not outcome["ok"] and outcome["status"] == "CANCELLED"
    assert db_events().count("DOCUMENT_REQUESTED") == 1


def test_the_system_of_record_also_refuses_a_duplicate_request(settings):
    """Defensa en profundidad: aunque el agente fallara, la API de acciones rechaza el duplicado (409)."""
    import httpx

    url = f"{settings.services_base_url}/api/claims/EXP-10234/actions"
    body = {"action": "REQUEST_DOCUMENT", "document": "PHOTO_PLATE"}
    assert httpx.post(url, json=body, headers={"Idempotency-Key": "k1"}).status_code == 200
    assert httpx.post(url, json=body, headers={"Idempotency-Key": "k2"}).status_code == 409   # otra acción, mismo documento
    assert httpx.post(url, json=body, headers={"Idempotency-Key": "k1"}).json()["replayed"]    # la repetición de la misma sigue siendo válida


def test_after_an_unknown_outcome_the_claim_shows_the_action_did_happen(make_agent, faults):
    """UNKNOWN deja de ser un callejón sin salida: el expediente cuenta si la solicitud llegó, y no se duplica."""
    faults.set("actions", "execute_then_500")
    agent, result = propose(make_agent)
    assert agent.confirm(result.pending_ids[0], ANA)["status"] == "UNKNOWN"
    faults.clear()

    again = make_agent(ScriptedLLM([PROPOSE], "ok")).handle(ANA, ASK)
    assert again.pending_ids == [] and again.final_decision == "TOOL_CALLS_REJECTED"


def test_mock_llm_tells_the_user_what_was_already_requested(make_agent):
    agent, result = propose(make_agent)
    agent.confirm(result.pending_ids[0], ANA)
    answer = make_agent(MockLLM()).handle(ANA, "¿En qué estado está el expediente EXP-10234 y qué estamos esperando?").answer
    assert "Ya se ha solicitado al taller" in answer and "matrícula" in answer


def test_mock_llm_does_not_invent_customer_data(make_agent, records):
    result = make_agent(MockLLM()).handle(ANA, "Como se llama el cliente del expediente EXP-10234")
    assert "No dispongo de datos personales de clientes" in result.answer
    assert records()[-1]["tools_called"] == []


# --- Permisos ---------------------------------------------------------------------------------
def test_viewer_does_not_see_the_write_tool_and_cannot_use_it(make_agent, db_events):
    agent = make_agent(ScriptedLLM([PROPOSE], "ok"))
    result = agent.handle(LUIS, ASK)

    assert "propose_action" not in agent.llm.tool_names_offered()
    assert "SOLO CONSULTA" in agent.llm.calls[0][0][0]["content"]
    assert result.pending_ids == [] and result.final_decision == "TOOL_CALLS_REJECTED"
    assert "no disponible para tu rol" in agent.llm.calls[1][0][-1]["content"]


def test_viewer_gets_a_clear_refusal_from_the_mock_llm(make_agent):
    result = make_agent(MockLLM()).handle(LUIS, ASK)
    assert "No tienes permisos" in result.answer and result.pending_ids == []


def test_claim_outside_the_users_scope_is_rejected_before_any_service_call(make_agent, records):
    agent = make_agent(ScriptedLLM([call("propose_action", {"claim_id": "EXP-20000", "action": "REQUEST_DOCUMENT", "document": "PHOTO_PLATE"})], "ok"))
    result = agent.handle(ANA, "Pide la foto de EXP-20000")
    assert result.pending_ids == [] and records()[-1]["service_calls"] == []


@pytest.mark.parametrize("arguments", [
    {"claim_id": "EXP-10234", "action": "DELETE_CLAIM", "document": "PHOTO_PLATE"},                       # acción inventada
    {"claim_id": "EXP-10234", "action": "REQUEST_DOCUMENT", "document": "BANK_ACCOUNT"},                  # documento inventado
    {"claim_id": "EXP-10234", "action": "REQUEST_DOCUMENT", "document": "PHOTO_PLATE", "force": True},    # argumento extra
    {"claim_id": "EXP-1; DROP", "action": "REQUEST_DOCUMENT", "document": "PHOTO_PLATE"},                 # identificador malformado
    '{"claim_id": ',                                                                                      # JSON roto
])
def test_invented_or_malformed_parameters_are_rejected(make_agent, arguments):
    agent = make_agent(ScriptedLLM([call("propose_action", arguments)], "ok"))
    result = agent.handle(ANA, ASK)
    assert result.pending_ids == [] and not agent.actions.pending


def test_full_flow_with_mock_llm(make_agent, db_events):
    agent = make_agent(MockLLM())
    result = agent.handle(ANA, ASK)
    assert "todavía" in result.answer and len(result.pending_ids) == 1
    assert "DOCUMENT_REQUESTED" not in db_events()

    assert agent.confirm(result.pending_ids[0], ANA)["ok"]
    assert db_events().count("DOCUMENT_REQUESTED") == 1
