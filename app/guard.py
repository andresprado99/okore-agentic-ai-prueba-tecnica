"""Filtro de ENTRADA (caso 4): detecta peticiones que intentan manipular al agente o pedir datos masivos/personales.

Es una capa barata y determinista, NO la defensa principal. Se puede esquivar (otro idioma, codificación, paráfrasis),
y por eso la seguridad real no depende de ella: aunque este filtro y el LLM fallaran, ninguna tool puede listar
expedientes ni devolver datos personales, y todo acceso pasa por permisos y validación (ver app/tools.py).

Para qué sirve entonces:
  - corta el caso obvio ANTES de gastar una llamada al LLM;
  - deja un rastro claro en la traza para detectar abusos;
  - da una respuesta fija y predecible.

Limitaciones a tener en cuenta: puede haber falsos positivos (se prefiere pecar de estricto porque el agente solo
atiende expedientes concretos por identificador) y falsos negativos (por eso existen las demás capas).
"""
import re
import unicodedata
from dataclasses import dataclass

MAX_INPUT_CHARS = 2000

# Los patrones se escriben sin tildes y en minúsculas: el texto se normaliza antes de compararlo.
PATTERNS: dict[str, list[str]] = {
    "prompt_injection": [
        r"\b(ignora|ignorar|ignores|olvida|olvidar|descarta|omite|salta\w*)\b.{0,30}\b(instrucciones|reglas|normas|restricciones|limites)\b",
        r"\b(ignore|disregard|forget|override)\b.{0,30}\b(instructions|rules|restrictions|guidelines)\b",
        r"\b(system prompt|prompt del sistema|mensaje de sistema|instrucciones (internas|del sistema|iniciales))\b",
        r"\b(modo (desarrollador|dios|administrador|sin restricciones)|developer mode|jailbreak|do anything now)\b",
        r"\b(a partir de ahora|from now on)\b.{0,40}\b(eres|actuaras|you are|act as)\b",
        r"\bactua como\b.{0,30}\b(administrador|admin|root|superusuario)\b",
    ],
    "bulk_data_request": [
        r"\b(todos|todas) (los|las) (expedientes|siniestros|reclamaciones|registros|clientes|talleres)\b",
        r"\b(toda|entera|completa) (la )?base de datos\b|\bbase de datos (entera|completa)\b|\bvolcado\b|\bdump\b",
        r"\ball (the )?(claims|records|customers|clients|workshops)\b",
        r"\bselect\b.{1,60}\bfrom\b|\bdrop table\b|\bunion select\b|\binsert into\b|\bdelete from\b",
    ],
    "personal_data_request": [
        r"\bdatos personales\b|\bpersonal data\b|\bpii\b",
        r"\b(dni|nif|nie|iban|telefono|direccion|email|correo|nombre)s? de(l| los| las)? (cliente|clientes|asegurado|asegurados|propietario|propietarios|conductor|conductores)\b",
        r"\b(clientes|asegurados|propietarios)\b.{0,40}\b(datos|dni|telefono|direccion|email)\b",
    ],
}
_COMPILED = {cat: [re.compile(p) for p in pats] for cat, pats in PATTERNS.items()}


@dataclass(frozen=True)
class GuardVerdict:
    triggered: bool = False
    categories: tuple[str, ...] = ()


def _normalize(text: str) -> str:
    """Quita tildes, caracteres invisibles y espacios repetidos, y pasa a minúsculas."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[​-‏⁠﻿]", "", text)
    return re.sub(r"\s+", " ", text).casefold()


def check_input(text: str) -> GuardVerdict:
    categories: list[str] = []
    if len(text) > MAX_INPUT_CHARS:
        categories.append("input_too_long")
    normalized = _normalize(text)
    for category, patterns in _COMPILED.items():
        if any(p.search(normalized) for p in patterns):
            categories.append(category)
    return GuardVerdict(bool(categories), tuple(categories))
