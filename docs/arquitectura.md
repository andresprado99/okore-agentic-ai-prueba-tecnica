# Arquitectura

Un solo agente, un solo proceso, y una idea: **el LLM propone, el código dispone**. El modelo interpreta la petición y
elige herramientas de una lista cerrada; todo lo que no debe fallar nunca (permisos, validación, confirmación,
ejecución, reintentos, avisos) es código determinista.

## Componentes

```mermaid
flowchart TB
    U([Usuario]) --> UI["Interfaz web o CLI<br/>app/web.py, app/cli.py"]
    UI --> G{"Filtro de entrada<br/>guard.py"}
    G -- "sospechoso" --> REF["Rechazo fijo<br/>sin llamar al LLM"]
    G -- "ok" --> AG["Agente (bucle)<br/>agent.py"]
    AG <--> LLM["LLMClient<br/>OpenAI, Ollama o Mock"]
    AG --> CTRL["Punto de control<br/>tools.py<br/>allowlist, esquema, permisos"]
    CTRL --> RD["Tools de lectura<br/>get_claim, get_workshop,<br/>get_claim_events"]
    CTRL --> PR["Tool de escritura<br/>propose_action"]
    RD --> CL["API expedientes"]
    RD --> WK["API talleres"]
    RD --> DB[("SQLite claim_events<br/>solo lectura")]
    PR --> PEND["Acciones pendientes<br/>actions.py"]
    PEND --> CONF{"Confirmación humana<br/>la pide y la lee el programa"}
    CONF -- "sí" --> EXE["Ejecución con<br/>Idempotency-Key"]
    CONF -- "no" --> CAN["Cancelada"]
    EXE --> ACT["API acciones"]
    AG --> TR[("Traza JSONL<br/>audit.py")]
```

Las APIs de expedientes, talleres y acciones, y la base SQLite, son el servicio simulado (`app/mock_services/`), con
fallos inyectables para probar el caso 3.

## Flujo de una acción con confirmación (caso 2)

```mermaid
sequenceDiagram
    actor U as Usuario
    participant A as Agente
    participant L as LLM
    participant K as Control de tools
    participant S as APIs
    U->>A: Pide al taller la foto de matrícula de EXP-10234
    A->>L: mensajes y SOLO las tools que este usuario puede usar
    L-->>A: propose_action(EXP-10234, PHOTO_PLATE)
    A->>K: validar tool, esquema y permisos
    K->>S: GET expediente y GET taller (¿falta el documento? ¿taller activo?)
    K-->>A: PENDING_CONFIRMATION, no se ha ejecutado nada
    A-->>U: resumen redactado por el código y pregunta [s/n]
    U->>A: s
    A->>S: revalidar el expediente y POST de la acción con Idempotency-Key
    S-->>A: ACCEPTED
    A-->>U: mensaje de éxito escrito por el código, no por el LLM
```

## Capas de defensa

Ninguna depende de que el modelo se porte bien; cada capa asume que las anteriores han fallado.

| # | Capa | Dónde | ¿Depende del LLM? |
|---|---|---|---|
| 1 | Filtro de entrada (inyección, datos masivos, datos personales, longitud) | `app/guard.py` | No, pero es esquivable |
| 2 | Prompt de sistema con reglas y rol del usuario | `app/agent.py` | Sí: solo guía |
| 3 | Tools mínimas: sin listados, sin datos personales, sin SQL libre | `app/tools.py` | No |
| 4 | Allowlist, esquemas estrictos, permisos por usuario y rol | `execute_tool_call` | No |
| 5 | Confirmación humana, un solo uso, caducidad, revalidación | `app/actions.py` | No |
| 6 | Idempotencia en la API de acciones | `ServiceClient`, `mock_services` | No |
| 7 | Avisos de fallo y comprobación de identificadores en la salida | `agent.py`, `grounding.py` | No |

## Estados de una acción pendiente

```mermaid
stateDiagram-v2
    [*] --> PENDING: propose_action
    PENDING --> EXECUTING: el usuario confirma
    PENDING --> CANCELLED: el usuario cancela, o el expediente cambió
    PENDING --> EXPIRED: pasan 5 minutos
    EXECUTING --> EXECUTED: la API responde ACCEPTED
    EXECUTING --> FAILED: la API rechaza la acción
    EXECUTING --> UNKNOWN: no hay respuesta tras los reintentos
```

`UNKNOWN` significa que no se sabe si la acción se hizo. No se informa de éxito ni se repite a ciegas; en producción
lo cerraría un proceso de reconciliación (ver README).
