"""Comprobación de fundamento de la respuesta: ¿menciona identificadores que NO salen de las fuentes consultadas?

El enunciado pide responder "únicamente con información disponible en las fuentes autorizadas". El prompt lo pide, pero
un prompt no es una garantía. Esta comprobación es determinista: busca identificadores concretos (expedientes, talleres,
matrículas, DNI, emails) en el texto final, y si alguno no aparece ni en la pregunta del usuario ni en los resultados de
las herramientas, lo marca como NO verificado.

Alcance (honesto): solo cubre identificadores. No detecta un estado inventado ("reparación completada") ni una fecha
falsa; para eso harían falta respuestas generadas a partir de plantillas o un verificador semántico (ver docs).
"""
import re

IDENTIFIER_PATTERNS = [
    re.compile(r"\bEXP-\d{5}\b", re.IGNORECASE),
    re.compile(r"\bT-\d{3}\b", re.IGNORECASE),
    re.compile(r"\b\d{4}[A-Z]{3}\b"),  # matrícula
    re.compile(r"\b\d{8}[A-Z]\b"),  # DNI
    re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),  # email (sin incluir el punto final de la frase)
]


def find_ungrounded(answer: str, sources: list[str]) -> list[str]:
    haystack = "\n".join(sources).casefold()
    found: set[str] = set()
    for pattern in IDENTIFIER_PATTERNS:
        for match in pattern.findall(answer):
            if match.casefold() not in haystack:
                found.add(match)
    return sorted(found)
