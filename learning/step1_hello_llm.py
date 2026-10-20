"""Paso 1: llamar a un LLM desde Python.

Un LLM es, para tu código, una función: le pasas una lista de mensajes
y te devuelve un mensaje. No recuerda nada entre llamadas: si quieres
conversación, tú reenvías el historial.

Este script usa el SDK de OpenAI, pero la URL base es configurable: apuntando
LLM_BASE_URL a Ollama (http://localhost:11434/v1) funciona igual con un modelo local.
"""
import os

from dotenv import load_dotenv
from openai import OpenAI

# Lee el archivo .env y mete su contenido en las variables de entorno,
# para que la clave API nunca esté escrita en el código.
load_dotenv()

base_url = os.environ.get("LLM_BASE_URL")  # vacío = OpenAI; con valor = Ollama u otro servidor compatible
api_key = os.environ.get("LLM_API_KEY")
model = os.environ.get("LLM_MODEL")

if not model or model.startswith("pon-aqui"):
    raise SystemExit("LLM_MODEL no está configurado en .env (sigue el valor de ejemplo). Ejemplo: LLM_MODEL=gpt-4.1-mini")
if not api_key or api_key.startswith("pon-aqui"):
    raise SystemExit("LLM_API_KEY no está configurada en .env (sigue el valor de ejemplo).")

# El cliente sabe a qué servidor hablar y con qué clave.
client = OpenAI(api_key=api_key, base_url=base_url)

response = client.chat.completions.create(
    model=model,
    messages=[
        # "system" son las instrucciones fijas del agente. Luego irán aquí las reglas.
        {"role": "system", "content": "Eres un asistente de un taller de reparación de vehículos. Responde en español, breve."},
        {"role": "user", "content": "¿Qué información necesitarías para saber en qué estado está una reparación?"},
    ],
)

# La respuesta trae una lista de "choices"; nos quedamos con la primera.
choice = response.choices[0]
print(choice.message.content)

# Datos útiles que luego iremos a la traza (observabilidad):
print("\n--- metadatos ---")
print("finish_reason:", choice.finish_reason)  # stop = terminó; tool_calls = pide una herramienta
print("tokens entrada/salida:", response.usage.prompt_tokens, "/", response.usage.completion_tokens)
