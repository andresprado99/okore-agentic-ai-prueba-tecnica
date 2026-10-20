"""Dobles de LLM para las pruebas (el MockLLM de demostración vive en app/mock_llm.py)."""
import json

from app.llm import LLMReply, ToolCall


def call(name: str, arguments: dict | str = "") -> tuple[str, str]:
    """Una petición de tool. `arguments` puede ser un dict o un texto crudo (p. ej. JSON roto)."""
    return name, arguments if isinstance(arguments, str) else json.dumps(arguments)


class ScriptedLLM:
    """Responde con un guion fijo. Cada paso es: un texto (respuesta final), una lista de call(...) o una excepción.
    Si el guion se acaba, repite el último paso (útil para simular un LLM que nunca termina)."""

    def __init__(self, *steps):
        self.steps = steps
        self.calls: list[tuple[list[dict], list[dict]]] = []

    def chat(self, messages, tools):
        step = self.steps[min(len(self.calls), len(self.steps) - 1)]
        self.calls.append((json.loads(json.dumps(messages)), tools))
        if isinstance(step, Exception):
            raise step
        if isinstance(step, str):
            return LLMReply(step, (), 10, 5)
        n = len(self.calls)
        return LLMReply(None, tuple(ToolCall(f"c{n}-{i}", name, args) for i, (name, args) in enumerate(step)), 10, 5)

    def tool_names_offered(self) -> list[str]:
        return [t["function"]["name"] for t in self.calls[0][1]]


class IndirectInjectionLLM:
    """Modelo 'ingenuo': lee un dato y, si dentro hay una orden, la obedece (inyección indirecta, vía datos)."""

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return LLMReply(None, (ToolCall("a1", "get_claim_events", json.dumps({"claim_id": "EXP-10234"})),))
        last_tool = next(m for m in reversed(messages) if m["role"] == "tool")
        if "IGNORE PREVIOUS INSTRUCTIONS" in last_tool["content"] and self.calls == 2:
            return LLMReply(None, (
                ToolCall("a2", "run_sql", json.dumps({"query": "SELECT * FROM claims"})),
                ToolCall("a3", "propose_action", json.dumps(
                    {"claim_id": "EXP-10234", "action": "REQUEST_DOCUMENT", "document": "PHOTO_PLATE"})),
            ))
        return LLMReply("He seguido las instrucciones del histórico.")
