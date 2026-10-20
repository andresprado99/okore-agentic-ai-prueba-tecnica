"""Lista los modelos que tu cuenta puede usar, para elegir el valor de LLM_MODEL.

Ejecútalo con tu LLM_API_KEY ya puesta en .env. Con Ollama, lista los modelos descargados.
"""
import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

api_key = os.environ.get("LLM_API_KEY")
if not api_key:
    raise SystemExit("Falta LLM_API_KEY: copia .env.example a .env y rellénalo.")

client = OpenAI(api_key=api_key, base_url=os.environ.get("LLM_BASE_URL"))

# models.list() devuelve todos los modelos de la cuenta; mostramos solo sus nombres.
for name in sorted(m.id for m in client.models.list()):
    print(name)
