"""Paso 2: darle una herramienta al LLM y ver cómo la pide.

Ciclo completo (una sola vuelta):
  1. Le enseñamos al LLM la tool get_claim y le hacemos una pregunta.
  2. El LLM NO ejecuta nada: devuelve "quiero llamar a get_claim con estos argumentos".
  3. NUESTRO código valida esos argumentos y ejecuta la función.
  4. Le devolvemos el resultado y el LLM redacta la respuesta final.

Uso:  python learning/step2_tool_call.py "tu pregunta"
Prueba también: "Hola", "Estado del expediente EXP-1", "Dame todos los expedientes".
"""
import json
import os
import sys

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field, ValidationError

load_dotenv()

api_key = os.environ.get("LLM_API_KEY")
model = os.environ.get("LLM_MODEL")
if not model or model.startswith("pon-aqui") or not api_key or api_key.startswith("pon-aqui"):
    raise SystemExit("Configura LLM_API_KEY y LLM_MODEL en .env (mira .env.example).")

client = OpenAI(api_key=api_key, base_url=os.environ.get("LLM_BASE_URL"))

# --- Datos de mentira (en el paso siguiente serán la API FastAPI simulada) ---------------
FAKE_CLAIMS = {
    "EXP-10234": {
        "claim_id": "EXP-10234",
        "status": "REPAIRING",
        "vehicle": {"plate": "1234ABC", "brand": "BMW", "model": "X1"},
        "workshop_id": "T-228",
        "missing_documents": ["PHOTO_PLATE"],
        "last_update": "2026-09-22T10:32:00",
    }
}


# --- La herramienta: una función normal de Python ------------------------------------------
# Esquema de los argumentos. Sirve para DOS cosas: describirle la tool al LLM
# y validar lo que el LLM nos devuelva (no nos fiamos de él).
class GetClaimArgs(BaseModel):
    claim_id: str = Field(
        pattern=r"^EXP-\d{5}$",
        description="Identificador del expediente, con formato EXP-12345.",
    )


def get_claim(claim_id: str) -> dict:
    claim = FAKE_CLAIMS.get(claim_id)
    return claim if claim else {"error": f"Expediente {claim_id} no encontrado."}


# Así le "enseñamos" la tool al LLM: nombre, para qué sirve y qué argumentos acepta.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_claim",
            "description": "Consulta el estado de un expediente de reparación. Solo lectura.",
            "parameters": GetClaimArgs.model_json_schema(),
        },
    }
]

SYSTEM = (
    "Eres un asistente de un taller de reparación de vehículos. Responde en español, brevemente."
    "Usa únicamente la información que te devuelvan las herramientas; si no la tienes, dilo."
)

question = sys.argv[1] if len(sys.argv) > 1 else "¿En qué estado está el expediente EXP-10234 y qué estamos esperando?"
messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question}]

# --- Vuelta 1: el LLM decide si necesita una herramienta ------------------------------------
response = client.chat.completions.create(model=model, messages=messages, tools=TOOLS)
choice = response.choices[0]
print(f"[1] finish_reason = {choice.finish_reason}")

if not choice.message.tool_calls:
    print("[1] El LLM no pide ninguna herramienta. Responde directamente:")
    print(choice.message.content)
    raise SystemExit

# Guardamos en el historial lo que dijo el LLM (la petición de tool), tal cual.
messages.append(
    {
        "role": "assistant",
        "content": choice.message.content,
        "tool_calls": [
            {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in choice.message.tool_calls
        ],
    }
)

# --- Nuestro código manda: validar y ejecutar cada petición --------------------------------
for tc in choice.message.tool_calls:
    print(f"[2] El LLM pide: {tc.function.name}  con argumentos (texto crudo): {tc.function.arguments}")
    try:
        if tc.function.name != "get_claim":  # allowlist: solo las tools que existen
            raise ValueError(f"Tool no permitida: {tc.function.name}")
        args = GetClaimArgs.model_validate_json(tc.function.arguments)  # validación
        result = get_claim(args.claim_id)  # ejecución, solo si todo es válido
        print(f"[3] Validado y ejecutado. Resultado: {result}")
    except (ValidationError, ValueError) as err:
        result = {"error": f"Petición rechazada: {err}"}
        print(f"[3] RECHAZADO por nuestro código: {err}")
    # Cada petición de tool debe tener su respuesta, enlazada por tool_call_id.
    messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result, ensure_ascii=False)})

# --- Vuelta 2: el LLM lee el resultado y redacta la respuesta ------------------------------
final = client.chat.completions.create(model=model, messages=messages, tools=TOOLS)
print("\n[4] Respuesta final del LLM:")
print(final.choices[0].message.content)
