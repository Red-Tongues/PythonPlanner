"""Every tunable of the kernel, in one table (M3a.1, CFO rule: nothing hard-coded).

Each threshold, weight and window that drives a decision is a row here: its ``settings`` key,
its default (seeded into ``settings`` by ``init-db``), its validated inclusive range and what it
does. Every reader takes its fallback from this table — ``settings_store.get_int`` /
``get_decimal`` (Python) and ``sql.views._setting`` / ``sql.genqueue._int`` (SQL) take only a
key — so a Python fallback always equals the seeded default. ``repository.set_setting`` refuses
a value outside the range. ``tests/test_tunables.py`` fails if a decision literal appears in
``src/`` outside this table (lines marked ``# structural:`` are units and scales, not
decisions).

Values are integers unless the default is a ``Decimal`` (a 0–1 weight, quantised to 4 dp).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class Tunable:
    key: str
    default: int | Decimal
    minimum: int | Decimal
    maximum: int | Decimal
    description: str

    @property
    def seed_value(self) -> int | float:
        """The JSON value seeded into ``settings`` (Decimals as floats, as before M3a.1)."""
        return float(self.default) if isinstance(self.default, Decimal) else self.default


def _t(key: str, default: Any, lo: Any, hi: Any, description: str) -> Tunable:
    if isinstance(default, float):
        default, lo, hi = Decimal(str(default)), Decimal(str(lo)), Decimal(str(hi))
    return Tunable(key, default, lo, hi, description)


E4 = 10_000  # structural: fixed point scale 10^4 (M2.1 fix 7)

TUNABLES: tuple[Tunable, ...] = (
    # ---- M1
    _t(
        "aggregation.cross_weight",
        0.6,
        0.0,
        1.0,
        "Weight of the cross-check score in model confidence",
    ),
    _t(
        "aggregation.self_weight",
        0.4,
        0.0,
        1.0,
        "Weight of the self-check score in model confidence",
    ),
    _t(
        "aggregation.dispute_margin",
        0.15,
        0.0,
        1.0,
        "Confidence margin below which a disagreement is disputed",
    ),
    _t("attempts.quickfire_evaluation", 4, 1, 20, "Max attempts per Quickfire Evaluation run (QE)"),
    _t("attempts.local_generation", 3, 1, 20, "Max attempts per Local Generation run (LG)"),
    _t("attempts.workbench_production", 2, 1, 20, "Max attempts per Workbench Production run (WP)"),
    # ---- M2
    _t(
        "consensus.agreement_min",
        0.6,
        0.0,
        1.0,
        "Agreement is accepted at or above this confidence (B2)",
    ),
    _t(
        "consensus.winner_min",
        0.7,
        0.0,
        1.0,
        "A disagreement winner needs at least this confidence (B2)",
    ),
    _t(
        "consensus.one_sided_min",
        0.7,
        0.0,
        1.0,
        "A one-sided (other NULL) value needs this confidence (B2)",
    ),
    _t("consensus.min_supports", 1, 0, 10, "Verified SUPPORT quotes needed for acceptance (B2)"),
    _t("evaluation.repair_attempts", 1, 0, 5, "Targeted repairs per call before escalation (B12)"),
    _t(
        "evaluation.max_escalations",
        1,
        0,
        5,
        "Split-plan re-runs per field before needs_review (B12)",
    ),
    _t(
        "evaluation.num_ctx",
        4096,
        512,
        131072,
        "Context window sent explicitly to the evaluator (B11)",
    ),
    _t("evaluation.max_tokens", 512, 64, 8192, "Max completion tokens per evaluation call"),
    _t("evaluation.max_exemplars", 3, 0, 20, "Exemplars per field included in a prompt"),
    _t("queue.lease_minutes", 30, 1, 1440, "Claim lease length"),
    _t("branching.max_depth", 2, 0, 10, "Maximum split depth (B5)"),
    _t("branching.max_descendants", 12, 0, 1000, "Maximum descendants of one trunk (B5)"),
    _t("branching.subtask_attempts", 3, 1, 10, "Attempts to obtain a valid subtask list"),
    _t("branching.subtask_max_tokens", 1500, 64, 16384, "Completion tokens for a subtask list"),
    _t("branching.position_stride", 100, 1, 10000, "Child position = trunk position x stride + i"),
    _t("stats.score_threshold", 7, 0, 10, "Scores at or above this count in v_packet_stats"),
    # ---- M3a (R6)
    _t(
        "qctx.w_direct_e4", 750, 0, E4, "OQ4: field weight when a direct context link is questioned"
    ),
    _t(
        "qctx.w_periph_e4",
        250,
        0,
        E4,
        "OQ4: field weight when only peripheral links are questioned",
    ),
    _t("qctx.cap_e4", 2250, 0, E4, "OQ4: cap of a packet's question_score_e4"),
    _t("gen_queue.w_grade_e4", 6000, 0, E4, "OQ7: weight of (10000 - parent grade), x 10^4"),
    _t("gen_queue.w_centrality_e4", 7000, 0, E4, "OQ7: weight of relational centrality, x 10^4"),
    _t(
        "gen_queue.root_parent_grade_e4",
        5000,
        0,
        E4,
        "OQ6: effective parent grade of a root packet",
    ),
    _t("gen_queue.ageing_per_minute_e4", 10, 0, E4, "OQ6: ageing per full minute in ready"),
    _t("gen_queue.ageing_cap_e4", 1500, 0, E4, "OQ6: ageing cap"),
    _t("gen_queue.eva_question_multiplier", 2, 0, 10, "OQ5: evaQueue adds question_score x this"),
    _t("admission.floor_e4", 3500, 0, E4, "OQ7: relational admission floor (ready -> queued)"),
    _t(
        "admission.low_floor_e4",
        2500,
        0,
        E4,
        "OQ7: floor while queued depth is below admission.low_depth",
    ),
    _t("admission.low_depth", 3, 0, 1000, "OQ7: queued depth under which the low floor applies"),
    _t(
        "admission.bypass_wait_sec",
        7200,
        0,
        31_536_000,
        "OQ7: wait in ready after which the floor is bypassed",
    ),
    _t("centrality.direct_e4", 3000, 0, E4, "OQ8: per unsealed direct dependent lineage"),
    _t("centrality.two_hop_e4", 1500, 0, E4, "OQ8: per unsealed two-hop dependent lineage"),
    _t("centrality.critical_e4", 2500, 0, E4, "OQ8: on a critical objective path"),
    _t(
        "centrality.critical_priority",
        8,
        0,
        10,
        "OQ8: objective priority_level that counts as critical",
    ),
    _t("centrality.cap_e4", 10000, 0, E4, "OQ8: cap of centrality_e4"),
    _t(
        "dep_satisfaction_grade_e4",
        7000,
        0,
        E4,
        "OQ13: a sealed lineage member at this grade satisfies",
    ),
    _t(
        "lineage_stop_grade_e4",
        8500,
        0,
        E4,
        "OQ14: a child of a parent at this grade is created held",
    ),
    _t("feedback.boost_e4", 1500, -E4, E4, "OQ9: BOOST"),
    _t("feedback.nudge_e4", 500, -E4, E4, "OQ9: NUDGE"),
    _t("feedback.demote_e4", -1500, -E4, E4, "OQ9: DEMOTE"),
    _t("feedback.pin_e4", 3000, -E4, E4, "OQ9: PIN"),
    _t("feedback.clamp_e4", 3000, 0, E4, "OQ9: packet feedback sum clamped to +/- this"),
    _t(
        "feedback.full_window_sec",
        86400,
        0,
        31_536_000,
        "OQ9: feedback counts in full under this age",
    ),
    _t(
        "feedback.half_window_sec",
        259200,
        0,
        31_536_000,
        "OQ9: then scaled by decay_mid_e4 under this age; 0 after",
    ),
    _t("feedback.decay_mid_e4", 5000, 0, E4, "OQ9: scale between the two windows (x 10^4)"),
    _t(
        "lockout.max_position",
        2,
        0,
        1000,
        "OQ3: genQueue positions 0..this are locked out of evaQueue",
    ),
    _t("crash.limit", 5, 1, 1000, "OQ18: crash_count at which the packet is held (crash_limit)"),
    _t(
        "crash.cooldown_base_sec",
        30,
        0,
        86400,
        "OQ18: cooldown = min(max, base * factor^(crash_count - 1))",
    ),
    _t("crash.cooldown_factor", 2, 1, 10, "OQ18: cooldown growth factor"),
    _t("crash.cooldown_max_sec", 3600, 0, 604800, "OQ18: cooldown cap"),
    _t("route.crash_window_sec", 300, 1, 86400, "OQ18: window for counting a route's crashes"),
    _t(
        "route.crash_threshold",
        3,
        1,
        1000,
        "OQ18: crashes in the window that make a route unhealthy",
    ),
    _t("route.unhealthy_sec", 600, 0, 86400, "OQ18: how long a route stays unhealthy"),
    _t(
        "dispatch.oldest_every",
        5,
        0,
        1000,
        "OQ25: every Nth dispatch takes the oldest queued packet",
    ),
    _t(
        "dispatch.incumbency_margin_e4",
        250,
        0,
        E4,
        "OQ25: a challenger must beat the incumbent by this",
    ),
    _t(
        "gen_eva.synth_below_e4",
        5000,
        0,
        E4,
        "OQ17: below this grade the child prompt is LLM-rewritten",
    ),
    _t("gen_eva.w_requirement_alignment_e4", 3000, 0, E4, "OQ16: composite weight"),
    _t("gen_eva.w_testability_e4", 2500, 0, E4, "OQ16: composite weight"),
    _t("gen_eva.w_acceptance_criteria_coverage_e4", 2000, 0, E4, "OQ16: composite weight"),
    _t("gen_eva.w_risk_assessment_e4", 1500, 0, E4, "OQ16: composite weight"),
    _t("gen_eva.w_sonar_hotspot_alignment_e4", 1000, 0, E4, "OQ16: composite weight"),
    _t(
        "gen_eva.sonar_na_shift_e4",
        500,
        0,
        E4,
        "OQ16: without sonar, this goes to testability and to risk each",
    ),
    _t("gen_eva.critique_count", 3, 0, 50, "OQ17: lowest-scored critiques in the child prompt"),
    _t(
        "gen_eva.max_contradictions",
        5,
        0,
        50,
        "OQ17: verified contradiction quotes in the child prompt",
    ),
    _t(
        "readiness.max_ambiguity_e4",
        4000,
        0,
        E4,
        "OQ11: ambiguity_quotient x 1000 must not exceed this",
    ),
    _t(
        "readiness.max_gating_tier",
        2,
        1,
        3,
        "Presets at or below this tier gate readiness (tier 3 never)",
    ),
)

BY_KEY: dict[str, Tunable] = {t.key: t for t in TUNABLES}
assert len(BY_KEY) == len(TUNABLES), "duplicate tunable key"


def default(key: str) -> int | Decimal:
    """The single fallback for ``key`` (``KeyError`` for an unknown key: no silent default)."""
    return BY_KEY[key].default


def sql_default(key: str) -> str:
    """The default as an SQL literal."""
    value = BY_KEY[key].default
    return str(value.normalize()) if isinstance(value, Decimal) else str(value)


def validate(key: str, value: Any) -> None:
    """Raise ``ValueError`` when ``value`` is not a number within the key's range."""
    tunable = BY_KEY.get(key)
    if tunable is None:
        return  # not a tunable (free-form setting)
    try:
        number = Decimal(str(value))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{key} must be a number, got {value!r}") from exc
    if isinstance(tunable.default, int) and number != number.to_integral_value():
        raise ValueError(f"{key} must be an integer, got {value!r}")
    if not (Decimal(tunable.minimum) <= number <= Decimal(tunable.maximum)):
        raise ValueError(f"{key} must be within [{tunable.minimum}, {tunable.maximum}]")
