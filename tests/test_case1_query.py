"""Caso 1 - Consulta: identificar el expediente, consultar, responder sin inventar y NO ejecutar nada."""
from app.auth import USERS
from app.mock_llm import MockLLM
from tests.fakes import ScriptedLLM, call

ANA = USERS["ana"]
QUESTION = "¿En qué estado está el expediente EXP-10234 y qué estamos esperando?"


def test_status_query_answers_from_data_and_takes_no_action(make_agent, records, db_events):
    result = make_agent(MockLLM()).handle(ANA, QUESTION)

    assert result.final_decision == "ANSWERED"
    assert "en reparación" in result.answer and "matrícula" in result.answer
    assert result.pending_ids == []
    assert "DOCUMENT_REQUESTED" not in db_events()
    rec = records()[-1]
    assert rec["detected_intent"] == "QUERY" and rec["action_executed"] is False and rec["action_requested"] is None


def test_events_are_read_from_sqlite(make_agent, records):
    agent = make_agent(ScriptedLLM([call("get_claim", {"claim_id": "EXP-10234"}), call("get_claim_events", {"claim_id": "EXP-10234"})], "ok"))
    agent.handle(ANA, QUESTION)

    rec = records()[-1]
    events = rec["tools_called"][1]["result"]["events"]
    assert [e["event_type"] for e in events] == ["CLAIM_OPENED", "VEHICLE_RECEIVED", "REPAIR_STARTED"]
    assert {c["service"] for c in rec["service_calls"]} == {"claims", "database"}


def test_history_question_with_mock_llm(make_agent):
    result = make_agent(MockLLM()).handle(ANA, "Dame el histórico de eventos del expediente EXP-10234")
    assert "Expediente abierto" in result.answer and "Reparación iniciada" in result.answer


def test_contact_question_chains_two_tools(make_agent, records):
    result = make_agent(MockLLM()).handle(ANA, "¿Cómo contacto con el taller del expediente EXP-10234?")
    assert "norte@example.com" in result.answer
    assert [t["name"] for t in records()[-1]["tools_called"]] == ["get_claim", "get_workshop"]


def test_question_without_claim_id_asks_for_it_and_calls_no_tool(make_agent, records):
    result = make_agent(MockLLM()).handle(ANA, "¿Cómo va mi coche?")
    assert "identificador" in result.answer
    assert records()[-1]["tools_called"] == []


def test_trace_has_all_required_fields_and_full_results(make_agent, records):
    make_agent(ScriptedLLM([call("get_claim", {"claim_id": "EXP-10234"})], "ok")).handle(ANA, QUESTION)
    rec = records()[-1]

    required = ["request_id", "timestamp", "user_request", "detected_intent", "tools_called", "final_decision",
                "action_requested", "action_executed", "errors", "latency_ms"]
    assert [k for k in required if k not in rec] == []
    # El resultado de la tool se guarda ENTERO, sin recortar.
    assert rec["tools_called"][0]["result"]["missing_documents"] == ["PHOTO_PLATE"]
    assert rec["tools_called"][0]["parameters"] == {"claim_id": "EXP-10234"}
