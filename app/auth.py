"""Identidad y permisos (simulados). En producción: token SSO/OIDC validado en la API, no un flag."""
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class User:
    id: str
    role: Literal["operator", "viewer"]
    allowed_claims: frozenset[str]

    @property
    def can_modify(self) -> bool:
        return self.role == "operator"


USERS = {
    "ana": User("ana", "operator", frozenset({"EXP-10234", "EXP-10567"})),
    "luis": User("luis", "viewer", frozenset({"EXP-10234", "EXP-10567"})),
}
