"""Typed actors (R1 fix 11).

Who did something is a typed value, not a string the caller formats. Authority follows the
*kind*: only a ``user`` actor may complete or archive a packet outside the transition table.
Actors are created at trusted boundaries — the CLI (and from M3 the GUI) creates ``user``
actors; the kernel creates ``system`` actors; channel code wraps every model call in an ``llm``
actor. LLM output never becomes an ``Actor``, so a model cannot claim user authority by writing
"user:" into its output. Full authentication arrives with the M3 GUI.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,47}$")


class ActorKind(StrEnum):
    USER = "user"
    SYSTEM = "system"
    LLM = "llm"


@dataclass(frozen=True, slots=True)
class Actor:
    kind: ActorKind
    id: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ActorKind):
            raise TypeError("Actor.kind must be an ActorKind")
        if not _ID.match(self.id):
            raise ValueError(f"invalid actor id {self.id!r}")

    @classmethod
    def user(cls, id: str) -> Actor:
        return cls(ActorKind.USER, id)

    @classmethod
    def system(cls, id: str) -> Actor:
        return cls(ActorKind.SYSTEM, id)

    @classmethod
    def llm(cls, id: str) -> Actor:
        return cls(ActorKind.LLM, id)

    @property
    def is_user(self) -> bool:
        return self.kind is ActorKind.USER

    def __str__(self) -> str:
        """The form stored in ``events.actor``, e.g. ``user:cli``."""
        return f"{self.kind.value}:{self.id}"
