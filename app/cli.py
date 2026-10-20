"""Chat por línea de comandos.   python -m app.cli "tu pregunta" [--user ana|luis] [--llm openai|mock|jailbroken]

Antes, en OTRA terminal, arranca los servicios simulados:   python -m app.mock_services
Sin clave de API:  añade --llm mock
"""
import argparse

import httpx

from app.agent import Agent
from app.auth import USERS, User
from app.bootstrap import LLM_HELP, LLM_KINDS, build_agent, make_llm
from app.config import load_settings


def ask_confirmation(agent: Agent, user: User, pending_id: str) -> None:
    """La confirmación la pide y la lee el PROGRAMA. El texto mostrado sale de lo guardado, no del LLM."""
    p = agent.actions.pending[pending_id]
    print(f"\n--- Acción pendiente {p.id} (caduca en {agent.actions.ttl_minutes} min) ---")
    print(p.summary())
    while True:
        try:
            answer = input("¿Confirmas que ejecute la acción? [s/n]: ").strip().lower()
        except EOFError:  # sin terminal interactiva no hay quien confirme: se cancela, nunca se ejecuta
            print("\nSin respuesta del usuario.")
            result = agent.cancel(p.id, user)
            break
        if answer in ("s", "si", "sí"):
            result = agent.confirm(p.id, user)
            print(result["message"])
            if result.get("retryable"):  # no se pudo verificar: la propuesta sigue viva, se puede volver a confirmar
                continue
            return
        if answer in ("n", "no"):
            result = agent.cancel(p.id, user)
            break
        print("Respuesta no válida. Escribe 's' para confirmar o 'n' para cancelar.")
    print(result["message"])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("question", nargs="?", default="Pide al taller la foto de matrícula que falta del expediente EXP-10234.")
    parser.add_argument("--user", choices=sorted(USERS), default="ana")
    parser.add_argument("--llm", choices=LLM_KINDS, default="openai", help=LLM_HELP)
    args = parser.parse_args()

    settings = load_settings()
    llm = make_llm(args.llm, settings)
    try:
        httpx.get(f"{settings.services_base_url}/health", timeout=1.5).raise_for_status()
    except httpx.HTTPError:
        print(f"⚠ No encuentro los servicios simulados en {settings.services_base_url}. "
              "¿Has arrancado 'python -m app.mock_services' en otra terminal? El agente seguirá, pero las consultas fallarán.\n")

    agent = build_agent(settings, llm)
    user = USERS[args.user]
    print(f"Usuario: {user.id} ({user.role}) · LLM: {args.llm} · filtro de entrada: {'activado' if settings.guard_enabled else 'DESACTIVADO'}")
    print(f"Pregunta: {args.question}\n")
    result = agent.handle(user, args.question)
    print(result.answer)
    for pending_id in result.pending_ids:
        ask_confirmation(agent, user, pending_id)
    print(f"\n[traza: {settings.trace_path} · request_id={result.request_id} · decisión={result.final_decision}]")


if __name__ == "__main__":
    main()
