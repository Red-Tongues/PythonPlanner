"""genQueue and evaQueue in SQL (M3a part c/d; R6 OQ2-OQ12, OQ25, OQ31).

Every number here is an **integer** computed with integer operators only, identical on SQLite and
PostgreSQL 16 (PM rule: no float intermediates):

* settings are read as ``CAST(... AS INTEGER)``;
* time is whole seconds since the epoch (``_EPOCH``: SQLite ``strftime('%s')``, PostgreSQL
  ``FLOOR(EXTRACT(EPOCH ...))``), so a wait is an integer number of seconds;
* every division is of non-negative integers (truncation = floor), except the feedback halving,
  which uses an explicit floor for negative values (``_half``);
* rounding is ``(x + 5000) / 10000`` on non-negative ``x``.

Views (in dependency order), each also available with a bound ``now`` for tests:

``v_packet_mandatory_fields`` (OQ10/OQ11) → ``v_packet_field_health`` (OQ4) →
``v_packet_relational_centrality`` (OQ8) → ``v_packet_user_feedback`` (OQ9) →
``v_packet_queue_score`` (OQ5-OQ7) → ``v_gen_queue`` (OQ1, OQ2) → ``v_eva_queue`` (OQ3, OQ5,
OQ31: the lockout is read from the newest ``queue_epoch_snapshot``, never from live positions).
"""

from __future__ import annotations

#: universally mandatory pre-phase fields (R6 OQ10: clarity + specificity → requirement_alignment,
#: scope_fit → acceptance_criteria_coverage, testability kept)
UNIVERSAL_FIELDS: tuple[str, ...] = (
    "requirement_alignment",
    "testability",
    "acceptance_criteria_coverage",
)
#: relationally mandatory fields by trigger (R6 OQ10)
RELATIONAL_FIELDS: dict[str, tuple[str, ...]] = {
    "has_dependencies": ("dependency_validity", "interface_stability"),
    "has_dependents": ("downstream_contract_clarity", "risk_assessment"),
    "charted_repo_target": ("risk_assessment", "sonar_hotspot_alignment"),
}
#: statuses whose packets carry a queue score (developing or running)
SCORED_STATUSES = ("draft", "evaluating_pre", "ready", "queued", "running")

_EPOCH = {
    "sqlite": "CAST(strftime('%s', {x}) AS INTEGER)",
    "postgresql": "CAST(FLOOR(EXTRACT(EPOCH FROM {x})) AS BIGINT)",
}


def _int(key: str, default: int) -> str:
    return (
        f"CAST(COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st "
        f"WHERE st.key = '{key}'), {default}) AS INTEGER)"
    )


def _half(x: str) -> str:
    """floor(x / 2) for any integer x, identical on both dialects."""
    return f"(CASE WHEN ({x}) >= 0 THEN ({x}) / 2 ELSE -((1 - ({x})) / 2) END)"


def _min(a: str, b: str) -> str:
    return f"(CASE WHEN ({a}) < ({b}) THEN ({a}) ELSE ({b}) END)"


def _names(names: tuple[str, ...]) -> str:
    return " UNION ALL ".join(f"SELECT '{n}' AS field_name" for n in names)


#: an active (unrevoked) dependency edge ``e``
ACTIVE_EDGE = (
    "NOT EXISTS (SELECT 1 FROM revocations rv WHERE rv.target_type = 'dependency' "
    "AND rv.target_id = {e}.id)"
)
#: the lineage an edge targets (Argon K1 amending R6 OQ13: edges resolve to the target's
#: ``lineage_key``, never ``root_id``, so split siblings stay separate lineages)
LINEAGE = "{p}.lineage_key"


def satisfied_sql(edge: str) -> str:
    """R6 OQ13 with Argon K1: an edge is satisfied when some member of the target lineage
    (``lineage_key = depends_on_id``) is sealed with ``composite_grade_e4 >=
    dep_satisfaction_grade_e4``. Nothing else satisfies it — a completed or archived split
    parent does not."""
    grade = _int("dep_satisfaction_grade_e4", 7000)
    return (
        f"EXISTS (SELECT 1 FROM packets m JOIN sealed_bundles sb ON sb.packet_id = m.id "
        f"WHERE m.lineage_key = {edge}.depends_on_id AND m.status = 'sealed' "
        f"AND sb.composite_grade_e4 >= {grade})"
    )


def mandatory_fields() -> str:
    has_deps = (
        "EXISTS (SELECT 1 FROM packet_dependencies e WHERE e.packet_id = pk.id AND "
        + ACTIVE_EDGE.format(e="e")
        + ")"
    )
    has_dependents = (
        "EXISTS (SELECT 1 FROM packet_dependencies e JOIN packets dx ON dx.id = e.packet_id "
        f"WHERE e.depends_on_id = {LINEAGE.format(p='pk')} "
        f"AND dx.lineage_key <> {LINEAGE.format(p='pk')} AND " + ACTIVE_EDGE.format(e="e") + ")"
    )
    charted = "pk.repo_target_path IS NOT NULL"
    r = RELATIONAL_FIELDS
    return f"""CREATE VIEW v_packet_mandatory_fields AS
WITH pk AS (SELECT p.id, p.lineage_key, p.repo_target_path FROM packets p WHERE NOT p.locked)
SELECT pk.id AS packet_id, u.field_name, 'universal' AS reason
FROM pk CROSS JOIN ({_names(UNIVERSAL_FIELDS)}) u
UNION
SELECT pk.id, u.field_name, 'has_dependencies'
FROM pk CROSS JOIN ({_names(r["has_dependencies"])}) u WHERE {has_deps}
UNION
SELECT pk.id, u.field_name, 'has_dependents'
FROM pk CROSS JOIN ({_names(r["has_dependents"])}) u WHERE {has_dependents}
UNION
SELECT pk.id, u.field_name, 'charted_repo_target'
FROM pk CROSS JOIN ({_names(r["charted_repo_target"])}) u WHERE {charted}
UNION
SELECT pk.id, ps.field_name, 'preset_required'
FROM pk CROSS JOIN eva_presets ps
WHERE ps.phase = 'pre' AND ps.enabled AND ps.required AND ps.tier IN (1, 2)
  AND ps.revision = (SELECT MAX(p2.revision) FROM eva_presets p2
                     WHERE p2.field_name = ps.field_name AND p2.phase = ps.phase)"""


def field_health() -> str:
    cap = _int("qctx.cap_e4", 2250)
    latest = (
        "ps.phase = 'pre' AND ps.enabled AND ps.revision = (SELECT MAX(p2.revision) "
        "FROM eva_presets p2 WHERE p2.field_name = ps.field_name AND p2.phase = ps.phase)"
    )
    missing = "(fs.status IS NULL OR fs.status = 'pending')"
    return f"""CREATE VIEW v_packet_field_health AS
SELECT p.id AS packet_id,
       (SELECT COUNT(DISTINCT m.field_name) FROM v_packet_mandatory_fields m
          LEFT JOIN v_field_status fs ON fs.packet_id = p.id AND fs.field_name = m.field_name
           AND fs.phase = 'pre'
         WHERE m.packet_id = p.id AND {missing}) AS missing_mandatory_fields,
       (SELECT COUNT(*) FROM eva_presets ps
          LEFT JOIN v_field_status fs ON fs.packet_id = p.id AND fs.field_name = ps.field_name
           AND fs.phase = 'pre'
         WHERE {latest} AND {missing}
           AND ps.field_name NOT IN (SELECT m.field_name FROM v_packet_mandatory_fields m
                                     WHERE m.packet_id = p.id)) AS missing_optional_fields,
       -- @ORCHESTRATOR:PLACEHOLDER (M3b: wire the local-drafts trigger). Reported only: it is
       -- not a v_eva_queue need condition until a drafting channel exists.
       CASE WHEN p.examples_hash IS NULL THEN 1 ELSE 0 END AS missing_local_drafts,
       COALESCE((SELECT SUM(fs.question_context_count) FROM v_field_status fs
                 WHERE fs.packet_id = p.id AND fs.phase = 'pre'), 0) AS question_context_total,
       {_min("COALESCE((SELECT SUM(fs.question_weight_e4) FROM v_field_status fs WHERE fs.packet_id = p.id AND fs.phase = 'pre'), 0)", cap)} AS question_score_e4
FROM packets p
WHERE NOT p.locked"""  # noqa: E501


def _dependents(target: str, alias: str) -> str:
    """Unlocked packets ``alias`` of *other* lineages with an active edge to ``target``'s
    lineage, whose own lineage has no sealed member graded at or above the satisfaction grade
    (R6 OQ8 with Argon K1)."""
    grade = _int("dep_satisfaction_grade_e4", 7000)
    return (
        f"FROM packets {alias} JOIN packet_dependencies e{alias} ON e{alias}.packet_id = "
        f"{alias}.id WHERE e{alias}.depends_on_id = {LINEAGE.format(p=target)} "
        f"AND {alias}.lineage_key <> {LINEAGE.format(p=target)} AND NOT {alias}.locked AND "
        + ACTIVE_EDGE.format(e=f"e{alias}")
        + f" AND NOT EXISTS (SELECT 1 FROM sealed_bundles sx JOIN packets mx ON mx.id = "
        f"sx.packet_id WHERE mx.lineage_key = {alias}.lineage_key "
        f"AND sx.composite_grade_e4 >= {grade})"
    )


def centrality() -> str:
    """OQ8, counting **distinct dependent lineages** (Argon K1)."""
    direct = f"(SELECT COUNT(DISTINCT d1.lineage_key) {_dependents('p', 'd1')})"
    two_hop = (
        f"(SELECT COUNT(DISTINCT d2.lineage_key) FROM packets h "
        f"JOIN packet_dependencies eh ON eh.depends_on_id = h.lineage_key "
        f"JOIN packets d2 ON d2.id = eh.packet_id "
        f"WHERE h.id IN (SELECT d1.id {_dependents('p', 'd1')}) "
        f"AND d2.lineage_key <> p.lineage_key AND d2.lineage_key <> h.lineage_key "
        f"AND NOT d2.locked AND "
        + ACTIVE_EDGE.format(e="eh")
        + f" AND d2.lineage_key NOT IN (SELECT d1.lineage_key {_dependents('p', 'd1')}))"
    )
    critical = (
        "(CASE WHEN COALESCE((SELECT o.priority_level FROM objectives o "
        f"WHERE o.id = p.objective_id), 0) >= {_int('centrality.critical_priority', 8)} "
        "THEN 1 ELSE 0 END)"
    )
    total = (
        f"{_int('centrality.direct_e4', 3000)} * c.direct_dependents + "
        f"{_int('centrality.two_hop_e4', 1500)} * c.two_hop_dependents + "
        f"{_int('centrality.critical_e4', 2500)} * c.is_critical"
    )
    return f"""CREATE VIEW v_packet_relational_centrality AS
WITH c AS (
  SELECT p.id AS packet_id, {direct} AS direct_dependents, {two_hop} AS two_hop_dependents,
         {critical} AS is_critical
  FROM packets p WHERE NOT p.locked
)
SELECT c.*, {_min(total, "10000")} AS centrality_e4 FROM c"""


def feedback_select(dialect: str, now: str) -> str:
    epoch = _EPOCH[dialect]
    age = f"({epoch.format(x=now)} - {epoch.format(x='f.created_at')})"
    weight = (
        f"CASE f.kind WHEN 'boost' THEN {_int('feedback.boost_e4', 1500)} "
        f"WHEN 'nudge' THEN {_int('feedback.nudge_e4', 500)} "
        f"WHEN 'demote' THEN {_int('feedback.demote_e4', -1500)} "
        f"ELSE {_int('feedback.pin_e4', 3000)} END"
    )
    clamp = _int("feedback.clamp_e4", 3000)
    return f"""WITH latest AS (
  SELECT f.packet_id, f.kind, {age} AS age_sec, {weight} AS w
  FROM feedback_events f
  WHERE NOT EXISTS (SELECT 1 FROM feedback_events g WHERE g.packet_id = f.packet_id
                    AND g.actor = f.actor AND (g.created_at > f.created_at
                         OR (g.created_at = f.created_at AND g.id > f.id)))
),
decayed AS (
  SELECT latest.packet_id,
         CASE WHEN latest.age_sec < 86400 THEN latest.w
              WHEN latest.age_sec < 259200 THEN {_half("latest.w")}
              ELSE 0 END AS v
  FROM latest
),
s AS (SELECT decayed.packet_id, SUM(decayed.v) AS total FROM decayed GROUP BY decayed.packet_id)
SELECT s.packet_id,
       CASE WHEN s.total > {clamp} THEN {clamp} WHEN s.total < -{clamp} THEN -{clamp}
            ELSE s.total END AS feedback_e4
FROM s"""


def queue_score_select(dialect: str, now: str) -> str:
    """Per scored packet: the inputs and ``score_e4`` (R6 OQ7) plus ``eva_priority_e4`` (OQ5)."""
    epoch = _EPOCH[dialect]
    wait = f"({epoch.format(x=now)} - {epoch.format(x='COALESCE(p.ready_since, p.created_at)')})"
    statuses = ", ".join(f"'{s}'" for s in SCORED_STATUSES)
    overrides = (
        "EXISTS (SELECT 1 FROM queue_overrides o WHERE o.packet_ref = p.ref "
        "AND o.kind IN ('block', 'hold') AND NOT EXISTS (SELECT 1 FROM revocations rv "
        "WHERE rv.target_type = 'override' AND rv.target_id = o.id) "
        "AND (o.scope = 'lineage' OR o.target_version = p.version) "
        f"AND (o.expires_at IS NULL OR {epoch.format(x='o.expires_at')} > {epoch.format(x=now)}))"
    )
    unsatisfied = (
        "EXISTS (SELECT 1 FROM packet_dependencies e WHERE e.packet_id = p.id AND "
        + ACTIVE_EDGE.format(e="e")
        + f" AND NOT {satisfied_sql('e')})"
    )
    unhealthy = (
        "EXISTS (SELECT 1 FROM route_health rh WHERE rh.channel = p.channel "
        f"AND rh.unhealthy_until IS NOT NULL AND {epoch.format(x='rh.unhealthy_until')} > "
        f"{epoch.format(x=now)})"
    )
    cooling = (
        f"(p.cooldown_until IS NOT NULL AND {epoch.format(x='p.cooldown_until')} > "
        f"{epoch.format(x=now)})"
    )
    ageing = _min(
        f"(x.wait_sec / 60) * {_int('gen_queue.ageing_per_minute_e4', 10)}",
        _int("gen_queue.ageing_cap_e4", 1500),
    )
    base = (
        f"(({_int('gen_queue.w_grade_e4', 6000)} * (10000 - x.parent_grade_e4) "
        f"+ {_int('gen_queue.w_centrality_e4', 7000)} * x.centrality_e4 + 5000) / 10000)"
    )
    feedback = feedback_select(dialect, now)
    return f"""WITH x AS (
  SELECT p.id AS packet_id, p.ref AS packet_ref, p.status, p.channel, p.root_id,
         p.lineage_key,
         p.repo_target_path, p.created_at, p.ready_since, p.crash_count,
         {epoch.format(x="COALESCE(p.ready_since, p.created_at)")} AS ready_epoch,
         CASE WHEN {wait} < 0 THEN 0 ELSE {wait} END AS wait_sec,
         CASE WHEN p.parent_bundle_id IS NULL
              THEN {_int("gen_queue.root_parent_grade_e4", 5000)}
              ELSE COALESCE((SELECT sb.composite_grade_e4 FROM sealed_bundles sb
                             WHERE sb.id = p.parent_bundle_id),
                            {_int("gen_queue.root_parent_grade_e4", 5000)}) END
           AS parent_grade_e4,
         COALESCE(c.centrality_e4, 0) AS centrality_e4,
         COALESCE(fh.question_score_e4, 0) AS question_score_e4,
         COALESCE(fh.question_context_total, 0) AS question_context_count,
         COALESCE(fh.missing_mandatory_fields, 0) AS missing_mandatory_fields,
         COALESCE(fh.missing_optional_fields, 0) AS missing_optional_fields,
         COALESCE(fh.missing_local_drafts, 0) AS missing_local_drafts,
         COALESCE(uf.feedback_e4, 0) AS feedback_e4,
         CASE WHEN p.status = 'held' OR {overrides} THEN 1 ELSE 0 END AS is_held,
         CASE WHEN {unsatisfied} THEN 0 ELSE 1 END AS deps_satisfied,
         CASE WHEN {unhealthy} THEN 0 ELSE 1 END AS route_healthy,
         CASE WHEN {cooling} THEN 1 ELSE 0 END AS cooling
  FROM packets p
  LEFT JOIN v_packet_relational_centrality c ON c.packet_id = p.id
  LEFT JOIN v_packet_field_health fh ON fh.packet_id = p.id
  LEFT JOIN ({feedback}) uf ON uf.packet_id = p.id
  WHERE NOT p.locked AND p.status IN ({statuses})
    AND NOT EXISTS (SELECT 1 FROM packets s WHERE s.supersedes_id = p.id)
)
SELECT x.*, {ageing} AS ageing_e4, {base} AS base_score_e4,
       {base} - x.question_score_e4 + x.feedback_e4 + {ageing} AS score_e4,
       {base} + 2 * x.question_score_e4 + x.feedback_e4 + {ageing} AS eva_priority_e4
FROM x"""


def gen_queue_select(dialect: str, now: str) -> str:
    """R6 OQ1/OQ2/OQ7: running (position 0) plus queued, then ready, dispatchable packets."""
    floor = _int("admission.floor_e4", 3500)
    low = _int("admission.low_floor_e4", 2500)
    depth = _int("admission.low_depth", 3)
    bypass = _int("admission.bypass_wait_sec", 7200)
    return f"""WITH s AS ({queue_score_select(dialect, now)}),
g AS (
  SELECT s.* FROM s
  WHERE s.status = 'running'
     OR (s.status IN ('queued', 'ready') AND s.is_held = 0 AND s.deps_satisfied = 1
         AND s.route_healthy = 1 AND s.cooling = 0)
),
d AS (SELECT COUNT(*) AS queued_depth FROM g WHERE g.status = 'queued')
SELECT g.*, d.queued_depth,
       CASE WHEN d.queued_depth < {depth} THEN {low} ELSE {floor} END AS floor_e4,
       CASE WHEN g.status = 'running' THEN 1
            WHEN g.wait_sec >= {bypass} THEN 1
            WHEN g.score_e4 >= CASE WHEN d.queued_depth < {depth} THEN {low} ELSE {floor} END
            THEN 1 ELSE 0 END AS admissible,
       CASE WHEN g.status = 'running' THEN 0
            ELSE ROW_NUMBER() OVER (
              PARTITION BY CASE WHEN g.status = 'running' THEN 1 ELSE 0 END
              ORDER BY CASE WHEN g.status = 'queued' THEN 0 ELSE 1 END,
                       g.score_e4 DESC, g.ready_epoch ASC, g.packet_id ASC)
       END AS gen_position
FROM g CROSS JOIN d"""


def eva_queue_select(dialect: str, now: str) -> str:
    """R6 OQ3/OQ5/OQ31: developing packets needing enrichment, minus the lockout of the newest
    ``queue_epoch_snapshot``. ``ready``/``queued`` rows are also ``v_gen_queue`` rows.

    @ORCHESTRATOR:PLACEHOLDER (M3b: wire the local-drafts trigger): ``missing_local_drafts`` is
    deliberately not a need condition yet — nothing in M3a can produce local drafts."""
    return f"""WITH s AS ({queue_score_select(dialect, now)}),
g AS ({gen_queue_select(dialect, now)}),
latest AS (SELECT MAX(q.epoch_id) AS epoch_id FROM queue_epoch_snapshot q)
SELECT s.packet_id, s.packet_ref, s.status, s.missing_mandatory_fields,
       s.missing_optional_fields, s.missing_local_drafts, s.question_context_count,
       s.question_score_e4, s.score_e4, s.eva_priority_e4, s.ready_epoch
FROM s
WHERE s.status IN ('draft', 'evaluating_pre', 'ready', 'queued')
  AND (s.missing_mandatory_fields > 0 OR s.missing_optional_fields > 0
       OR s.question_context_count > 0)
  AND (s.status IN ('draft', 'evaluating_pre')
       OR s.packet_id IN (SELECT g.packet_id FROM g))
  AND NOT EXISTS (SELECT 1 FROM queue_epoch_snapshot q JOIN latest ON q.epoch_id = latest.epoch_id
                  WHERE q.packet_id = s.packet_id AND q.is_locked_out)"""


def statements(dialect: str) -> list[str]:
    now = "CURRENT_TIMESTAMP"
    return [
        mandatory_fields(),
        field_health(),
        centrality(),
        "CREATE VIEW v_packet_user_feedback AS\n" + feedback_select(dialect, now),
        "CREATE VIEW v_packet_queue_score AS\n" + queue_score_select(dialect, now),
        "CREATE VIEW v_gen_queue AS\n" + gen_queue_select(dialect, now),
        "CREATE VIEW v_eva_queue AS\n" + eva_queue_select(dialect, now),
    ]


VIEW_NAMES: tuple[str, ...] = (
    "v_packet_mandatory_fields",
    "v_packet_field_health",
    "v_packet_relational_centrality",
    "v_packet_user_feedback",
    "v_packet_queue_score",
    "v_gen_queue",
    "v_eva_queue",
)
