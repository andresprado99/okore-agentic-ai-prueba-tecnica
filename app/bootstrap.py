"""Montaje común del agente: lo usan el CLI (app/cli.py) y la interfaz web (app/web.py)."""
from app.actions import ActionManager
from app.agent import Agent
from app.audit import TraceWriter
from app.config import Settings
from app.llm import LLMClient, OpenAICompatibleLLM
from app.mock_llm import JailbrokenMockLLM, MockLLM
from app.services import Services

LLM_KINDS = ["openai", "mock", "jailbroken"]
LLM_HELP = ("openai = LLM real configurado en .env | mock = simulado, sin clave | "
            "jailbroken = modelo simulado comprometido que intenta atacar (caso 4)")


def make_llm(kind: str, settings: Settings) -> LLMClient:
    if kind == "mock":
        return MockLLM()
    if kind == "jailbroken":
        return JailbrokenMockLLM()
    bad = lambda v: not v or v.startswith("pon-aqui")  # noqa: E731
    if bad(settings.llm_model) or bad(settings.llm_api_key):
        raise SystemExit("Configura LLM_API_KEY y LLM_MODEL en .env (mira .env.example), o usa --llm mock para probar sin clave.")
    return OpenAICompatibleLLM(settings.llm_api_key, settings.llm_model, settings.llm_base_url)


def build_agent(settings: Settings, llm: LLMClient) -> Agent:
    services = Services(settings)
    return Agent(llm, services, ActionManager(services, settings), TraceWriter(settings.trace_path), settings)
