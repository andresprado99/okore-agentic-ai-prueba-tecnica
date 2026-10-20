"""Frontera con el LLM. El agente solo conoce esta interfaz (LLMClient), no ningún SDK concreto.

Cambiar de proveedor = escribir otra clase con el método chat(). Un MockLLM para tests es otra clase más.
"""
from dataclasses import dataclass
from typing import Protocol

from openai import OpenAI


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # texto JSON crudo, tal como lo devuelve el modelo: NO se fía hasta validarlo


@dataclass(frozen=True)
class LLMReply:
    content: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    input_tokens: int = 0
    output_tokens: int = 0


class LLMClient(Protocol):
    def chat(self, messages: list[dict], tools: list[dict]) -> LLMReply: ...


class OpenAICompatibleLLM:
    """OpenAI o cualquier servidor con su misma API (Ollama, vLLM...) cambiando base_url."""

    def __init__(self, api_key: str, model: str, base_url: str | None = None, timeout_s: float = 60.0):
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout_s)
        self._model = model

    def chat(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        resp = self._client.chat.completions.create(model=self._model, messages=messages, tools=tools)
        msg = resp.choices[0].message
        usage = resp.usage
        return LLMReply(
            content=msg.content,
            tool_calls=tuple(ToolCall(tc.id, tc.function.name, tc.function.arguments) for tc in (msg.tool_calls or [])),
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
        )
