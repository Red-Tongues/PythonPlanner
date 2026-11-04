"""Pass plans loaded from ``pass_plans.yaml`` (data, not code — B12)."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml

from orchestrator.contracts import FieldKind, PassPlanName

PLAN_FILE = Path(__file__).with_name("pass_plans.yaml")


@dataclass(frozen=True)
class PassSpec:
    no: int
    model: str  # "a" | "b"
    role: str  # extract | cross | self
    target: int | None = None  # the extraction pass whose value this pass evaluates


@dataclass(frozen=True)
class PassPlan:
    name: PassPlanName
    version: str
    passes: tuple[PassSpec, ...]
    text_passes: frozenset[int]
    calls: tuple[str, ...]  # per evaluation pass: ("evaluate",) or ("accuracy", "evidence")

    @property
    def label(self) -> str:
        return f"{self.name.value}@{self.version}"

    def passes_for(self, kind: FieldKind) -> tuple[PassSpec, ...]:
        if kind is FieldKind.TEXT:
            return tuple(p for p in self.passes if p.no in self.text_passes)
        return self.passes


@cache
def load_plan(name: PassPlanName | str) -> PassPlan:
    data = yaml.safe_load(PLAN_FILE.read_text())
    plan_name = PassPlanName(name)
    return PassPlan(
        name=plan_name,
        version=str(data["version"]),
        passes=tuple(
            PassSpec(no=p["number"], model=p["model"], role=p["role"], target=p.get("target"))
            for p in data["passes"]
        ),
        text_passes=frozenset(data["text_passes"]),
        calls=tuple(data["plans"][plan_name.value]["calls"]),
    )
