# learning/

Los cinco scripts que sirvieron para construir el agente paso a paso. **No forman parte de la solución**: el código
final está en `app/`. Se dejan porque muestran cómo se llegó a cada pieza.

| Script | Qué enseña |
|---|---|
| `step1_hello_llm.py` | Llamar a un LLM desde Python |
| `list_models.py` | Ver qué modelos tiene la cuenta, para elegir `LLM_MODEL` |
| `step2_tool_call.py` | El LLM pide una herramienta y el código la valida y la ejecuta |
| `step3_agent_loop.py` | Bucle completo, registro de tools y tope de vueltas |
| `step4_confirmation.py` | Proponer, confirmar y ejecutar (roles, caducidad, idempotencia) |

Se ejecutan con el entorno virtual y el `.env` configurados, por ejemplo: `python learning/step3_agent_loop.py`.
