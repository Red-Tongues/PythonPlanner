"""Every SQL view of the schema, on SQLite and PostgreSQL (part of the master schema).

``orchestrator.db.schema`` assembles these with the tables and triggers; the baseline migration
holds the generated text, and ``tests/test_schema.py`` fails if the two ever differ.

Every dialect difference is a token in ``_DIALECT`` (golden rule 7); the view text is otherwise
identical. Semantic contracts (columns, types, NULL semantics, ordering) are documented in
``docs/ARCHITECTURE.md`` (B6). Decision-making logic lives here, in SQL; the pure Python oracle
in ``orchestrator.evaluation.consensus`` must agree with it row for row (D5, parity tests).

Numeric conventions (D2 ruling 6, M2.1 fix 7): consensus arithmetic is integer fixed point
scaled by 10⁴ (``*_e4`` columns), so SQLite and PostgreSQL decide identically; the
``confidence`` columns are those integers divided by 10000 for display. Booleans in view output
are INTEGER 0/1.
"""

from __future__ import annotations

from dataclasses import dataclass

from orchestrator.sql import genqueue


@dataclass(frozen=True)
class Dialect:
    #: FROM-clause fragment exploding ``e.item_keys`` (a JSON array of strings) into ``k.value``
    item_keys: str
    #: hours between two timestamps: format with ``a`` (later) and ``b`` (earlier)
    hours: str
    #: comparable form of a timestamp expression
    ts: str
    #: newline character literal
    newline: str


_DIALECT: dict[str, Dialect] = {
    "sqlite": Dialect(
        item_keys="json_each(e.item_keys) AS k",
        hours="((julianday({a}) - julianday({b})) * 24.0)",
        ts="julianday({x})",
        newline="char(10)",
    ),
    "postgresql": Dialect(
        item_keys="json_array_elements_text(e.item_keys) AS k(value)",
        hours="(EXTRACT(EPOCH FROM ({a} - {b})) / 3600.0)",
        ts="{x}",
        newline="chr(10)",
    ),
}

#: the M2 decision views, in dependency order
VIEW_NAMES: tuple[str, ...] = (
    "v_eva_progress",
    "v_eva_item_consensus",
    "v_context_link_status",
    "v_field_consensus",
    "v_field_status",
    "v_packet_stats",
    "v_branch_stats",
    "v_branch_model_usage",
    "v_branch_triggers",
    "v_ready_candidates",
)


def _setting(key: str, default: str) -> str:
    return (
        f"COALESCE((SELECT CAST(CAST(st.value AS TEXT) AS NUMERIC) FROM settings st "
        f"WHERE st.key = '{key}'), {default})"
    )


# --------------------------------------------------------------------------- consensus


def _eva_progress() -> str:
    """Per evaPacket: which passes are done and whether every *applicable* pass is (ruling 2)."""
    done = "".join(
        f"  MAX(CASE WHEN ep.pass_no = {n} THEN 1 ELSE 0 END) AS done{n},\n" for n in range(1, 7)
    )
    return f"""CREATE VIEW v_eva_progress AS
WITH base AS (
  SELECT e.id AS eva_packet_id, e.packet_id, e.field_name, e.phase, e.state, e.created_at,
         CASE WHEN d.kind = 'text' THEN 1 ELSE 0 END AS is_text,
{done}  CASE WHEN EXISTS (SELECT 1 FROM evaluation_pass_items i WHERE i.eva_packet_id = e.id
                     AND i.pass_no = 1) THEN 1 ELSE 0 END AS a_present,
  CASE WHEN EXISTS (SELECT 1 FROM evaluation_pass_items i WHERE i.eva_packet_id = e.id
                     AND i.pass_no = 2) THEN 1 ELSE 0 END AS b_present
  FROM eva_packets e
  JOIN candidate_field_definitions d ON d.name = e.field_name
  LEFT JOIN evaluation_passes ep ON ep.eva_packet_id = e.id
  GROUP BY e.id, e.packet_id, e.field_name, e.phase, e.state, e.created_at, d.kind
)
SELECT base.*,
  CASE WHEN done1 = 1 AND done2 = 1
        AND (a_present = 0 OR (done4 = 1 AND (is_text = 1 OR done5 = 1)))
        AND (b_present = 0 OR (done3 = 1 AND (is_text = 1 OR done6 = 1)))
       THEN 1 ELSE 0 END AS complete
FROM base"""


def _e4(expr: str) -> str:
    """A setting as an integer scaled by 10⁴ (fixed point, M2.1 fix 7): 0.6 → 6000."""
    return f"CAST(ROUND({expr} * 10000) AS INTEGER)"


def _eva_item_consensus(d: Dialect) -> str:
    """Per evaPacket × item: the B2/B10 decision table. Applies to every evaPacket (history too);
    ``v_field_consensus`` selects the current one per packet × field × phase.

    All arithmetic is **integer fixed point scaled by 10⁴** (M2.1 fix 7): weights and thresholds
    are quantised to 4 dp once, ``conf = (cw·cross + sw·self + 5) / 10`` (integer division,
    half up), agreement mean ``(a + b + 1) / 2``. SQLite and PostgreSQL then decide identically
    and match the oracle exactly — no REAL rounding at a boundary.
    """
    joins = "".join(
        f"  LEFT JOIN evaluation_pass_items i{n} ON i{n}.eva_packet_id = e.id "
        f"AND i{n}.pass_no = {n} AND i{n}.item_key = k.value\n"
        for n in range(1, 7)
    )
    cw = _e4(_setting("aggregation.cross_weight", "0.6"))
    sw = _e4(_setting("aggregation.self_weight", "0.4"))
    conf_a = (
        "CASE WHEN x.va IS NULL OR x.s4 IS NULL THEN NULL WHEN x.is_text = 1 THEN x.s4 * 1000 "
        f"WHEN x.s5 IS NULL THEN NULL ELSE ({cw} * x.s4 + {sw} * x.s5 + 5) / 10 END"
    )
    conf_b = (
        "CASE WHEN x.vb IS NULL OR x.s3 IS NULL THEN NULL WHEN x.is_text = 1 THEN x.s3 * 1000 "
        f"WHEN x.s6 IS NULL THEN NULL ELSE ({cw} * x.s3 + {sw} * x.s6 + 5) / 10 END"
    )
    agree_min = _e4(_setting("consensus.agreement_min", "0.60"))
    winner_min = _e4(_setting("consensus.winner_min", "0.70"))
    one_min = _e4(_setting("consensus.one_sided_min", "0.70"))
    margin = _e4(_setting("aggregation.dispute_margin", "0.15"))
    min_sup = _setting("consensus.min_supports", "1")
    high = (
        "CASE WHEN s.ca IS NULL THEN s.cb WHEN s.cb IS NULL THEN s.ca "
        "WHEN s.ca >= s.cb THEN s.ca ELSE s.cb END"
    )
    return f"""CREATE VIEW v_eva_item_consensus AS
WITH x AS (
  SELECT e.id AS eva_packet_id, e.packet_id, e.packet_version, e.cycle_id AS eval_cycle_id,
         e.field_name, e.phase, e.created_at, d.kind,
         CASE WHEN d.kind = 'text' THEN 1 ELSE 0 END AS is_text,
         k.value AS item_key, pr.complete,
         i1.value_norm AS va, i2.value_norm AS vb,
         i3.score AS s3, i4.score AS s4, i5.score AS s5, i6.score AS s6,
         COALESCE(i3.supports, 0) AS sup3, COALESCE(i4.supports, 0) AS sup4,
         COALESCE(i5.supports, 0) AS sup5, COALESCE(i6.supports, 0) AS sup6,
         COALESCE(i3.contradictions, 0) AS con3, COALESCE(i4.contradictions, 0) AS con4,
         COALESCE(i5.contradictions, 0) AS con5, COALESCE(i6.contradictions, 0) AS con6,
         COALESCE(i3.unverified, 0) AS unv3, COALESCE(i4.unverified, 0) AS unv4,
         COALESCE(i5.unverified, 0) AS unv5, COALESCE(i6.unverified, 0) AS unv6
  FROM eva_packets e
  JOIN candidate_field_definitions d ON d.name = e.field_name
  JOIN v_eva_progress pr ON pr.eva_packet_id = e.id
  CROSS JOIN {d.item_keys}
{joins}),
c AS (
  SELECT x.*, {conf_a} AS ca, {conf_b} AS cb FROM x
),
w AS (
  SELECT c.*,
    CASE WHEN c.va IS NULL AND c.vb IS NULL THEN 'both_null'
         WHEN c.va IS NULL THEN 'b_only'
         WHEN c.vb IS NULL THEN 'a_only'
         WHEN c.va = c.vb THEN 'agree'
         ELSE 'disagree' END AS shape,
    CASE WHEN c.va IS NULL AND c.vb IS NULL THEN NULL
         WHEN c.va IS NULL THEN 'b'
         WHEN c.vb IS NULL THEN 'a'
         WHEN c.va = c.vb THEN 'both'
         WHEN c.ca > c.cb THEN 'a'
         WHEN c.cb > c.ca THEN 'b'
         ELSE NULL END AS winner
  FROM c
),
v AS (
  SELECT w.*,
    CASE w.winner WHEN 'a' THEN w.sup4 + w.sup5 WHEN 'b' THEN w.sup3 + w.sup6
         WHEN 'both' THEN w.sup3 + w.sup4 + w.sup5 + w.sup6 ELSE 0 END AS supports,
    CASE w.winner WHEN 'a' THEN w.con4 + w.con5 WHEN 'b' THEN w.con3 + w.con6
         WHEN 'both' THEN w.con3 + w.con4 + w.con5 + w.con6 ELSE 0 END AS contradictions,
    CASE w.winner WHEN 'a' THEN w.unv4 + w.unv5 WHEN 'b' THEN w.unv3 + w.unv6
         WHEN 'both' THEN w.unv3 + w.unv4 + w.unv5 + w.unv6 ELSE 0 END AS unverified,
    CASE WHEN w.shape = 'agree' THEN (w.ca + w.cb + 1) / 2
         WHEN w.shape = 'a_only' THEN w.ca
         WHEN w.shape = 'b_only' THEN w.cb
         WHEN w.shape = 'disagree' THEN CASE WHEN w.ca >= w.cb THEN w.ca ELSE w.cb END
         ELSE NULL END AS decision_e4,
    CASE WHEN w.shape = 'disagree' THEN
           CASE WHEN w.ca >= w.cb THEN w.ca - w.cb ELSE w.cb - w.ca END
         ELSE NULL END AS margin_e4
  FROM w
),
s AS (
  SELECT v.*,
    CASE WHEN v.complete = 0 THEN 'pending'
         WHEN v.shape = 'both_null' THEN 'null'
         WHEN v.supports >= {min_sup} AND v.contradictions <= v.supports AND (
              (v.shape = 'agree' AND v.decision_e4 >= {agree_min})
           OR (v.shape IN ('a_only', 'b_only') AND v.decision_e4 >= {one_min})
           OR (v.shape = 'disagree' AND v.winner IS NOT NULL
               AND v.decision_e4 >= {winner_min} AND v.margin_e4 >= {margin}))
         THEN 'accepted'
         ELSE 'disputed' END AS status
  FROM v
),
r AS (
  SELECT s.*,
    CASE WHEN s.status = 'pending' THEN NULL
         WHEN s.status = 'null' THEN 0
         WHEN s.status = 'accepted' AND s.shape = 'agree' THEN s.decision_e4
         ELSE {high} END AS confidence_e4
  FROM s
)
SELECT r.eva_packet_id, r.packet_id, r.packet_version, r.eval_cycle_id, r.field_name, r.phase,
       r.kind, r.item_key, r.complete, r.created_at,
       r.va AS value_a, r.vb AS value_b,
       r.ca AS conf_a_e4, r.cb AS conf_b_e4, r.confidence_e4,
       r.ca / 10000.0 AS conf_a, r.cb / 10000.0 AS conf_b,
       r.confidence_e4 / 10000.0 AS confidence,
       CASE WHEN r.shape = 'agree' THEN 1 ELSE 0 END AS agreement,
       r.status,
       CASE WHEN r.complete = 0 THEN 0 ELSE r.supports END AS supports,
       CASE WHEN r.complete = 0 THEN 0 ELSE r.contradictions END AS contradictions,
       CASE WHEN r.complete = 0 THEN 0 ELSE r.unverified END AS unverified,
       CASE WHEN r.complete = 1 AND r.winner = 'a' THEN r.va
            WHEN r.complete = 1 AND r.winner IN ('b', 'both') THEN r.vb
            ELSE NULL END AS winning_value
FROM r"""


def _context_link_status() -> str:
    """Every context link with ``questioned`` (0/1): the link was flagged (quote broken, carried
    to a child, OQ24/OQ34) or its source's revision moved past the one it read (R6 OQ23)."""
    current = (
        "CASE l.source_type "
        "WHEN 'project' THEN (SELECT x.content_revision FROM projects x WHERE x.id = l.source_id) "
        "WHEN 'objective' THEN (SELECT x.content_revision FROM objectives x "
        "WHERE x.id = l.source_id) "
        "WHEN 'packet' THEN (SELECT x.content_revision FROM packets x WHERE x.id = l.source_id) "
        "ELSE COALESCE((SELECT d.version FROM candidate_field_definitions d "
        "WHERE d.name = l.source_id), (SELECT MAX(ps.revision) FROM eva_presets ps "
        "WHERE 'preset:' || ps.field_name || ':' || ps.phase = l.source_id)) END"
    )
    return f"""CREATE VIEW v_context_link_status AS
SELECT l.id AS link_id, l.eva_packet_id, l.packet_id, l.field_name, l.source_type,
       l.source_id, l.source_ref, l.source_revision, l.link_strength, l.link_reason,
       {current} AS current_revision,
       CASE WHEN l.is_questioned OR {current} > l.source_revision THEN 1 ELSE 0 END
         AS questioned
FROM eva_context_links l"""


def _field_consensus() -> str:
    """Current consensus per head packet × field × phase × item (strict binding, ruling 7).

    M3a (R6 OQ23, T1-C v3 §2): an evaPacket is *current* while it targets the packet's own
    intra-packet ``input_hash`` (pre) or the sha256 of the packet's newest artifact (post), on
    the same packet row, with its preset and definition still enabled. Upstream context and
    definition/preset version bumps never null a result; they raise ``question_context``:

    - ``question_context`` (0/1) and ``question_context_count`` (questioned links) of the chosen
      evaPacket, both 0 for ``waiting_for_children`` and ``archived`` packets;
    - ``question_weight_e4`` (OQ4): ``qctx.w_direct_e4`` if a direct link is questioned, else
      ``qctx.w_periph_e4`` if a peripheral one is, else 0.

    The most relevant evaPacket is chosen: current first, then complete, then the highest
    escalation, then newest. If it is not current the row reports ``pending`` with no
    confidence or value, so a result for another packet definition never satisfies readiness.
    """
    w_direct = _setting("qctx.w_direct_e4", "750")
    w_periph = _setting("qctx.w_periph_e4", "250")
    quiet = "p.status IN ('waiting_for_children', 'archived')"
    return f"""CREATE VIEW v_field_consensus AS
WITH art AS (
  SELECT a.packet_id, a.sha256,
         ROW_NUMBER() OVER (PARTITION BY a.packet_id ORDER BY a.created_at DESC, a.id DESC)
           AS rn
  FROM artifacts a
),
bound AS (
  SELECT e.id, e.packet_id, e.field_name, e.phase, e.escalation, e.created_at, pr.complete,
         CASE WHEN ((e.phase = 'pre' AND e.target_hash = p.input_hash)
                    OR (e.phase = 'post' AND e.target_hash = la.sha256))
                   AND ps.enabled
                   AND d.enabled
              THEN 1 ELSE 0 END AS is_current
  FROM eva_packets e
  JOIN packets p ON p.id = e.packet_id
  JOIN v_eva_progress pr ON pr.eva_packet_id = e.id
  JOIN eva_presets ps ON ps.id = e.preset_id
  JOIN candidate_field_definitions d ON d.name = e.field_name
  LEFT JOIN art la ON la.packet_id = p.id AND la.rn = 1
  WHERE e.packet_version = p.version
    AND e.state <> 'superseded'
    AND NOT EXISTS (SELECT 1 FROM packets s WHERE s.supersedes_id = p.id)
),
cand AS (
  SELECT bound.*,
         ROW_NUMBER() OVER (PARTITION BY bound.packet_id, bound.field_name, bound.phase
                            ORDER BY bound.is_current DESC, bound.complete DESC,
                                     bound.escalation DESC, bound.created_at DESC,
                                     bound.id DESC) AS rn
  FROM bound
),
q AS (
  SELECT ls.eva_packet_id, SUM(ls.questioned) AS qn,
         MAX(CASE WHEN ls.questioned = 1 AND ls.link_strength = 'direct' THEN 1 ELSE 0 END)
           AS qd
  FROM v_context_link_status ls
  GROUP BY ls.eva_packet_id
)
SELECT p.id AS packet_id, p.ref AS packet_ref, p.version AS packet_version,
       c.field_name, c.phase, c.item_key, c.kind,
       c.value_a, c.value_b,
       CASE WHEN cand.is_current = 1 THEN c.conf_a ELSE NULL END AS conf_a,
       CASE WHEN cand.is_current = 1 THEN c.conf_b ELSE NULL END AS conf_b,
       CASE WHEN cand.is_current = 1 THEN c.confidence ELSE NULL END AS confidence,
       CASE WHEN cand.is_current = 1 THEN c.confidence_e4 ELSE NULL END AS confidence_e4,
       c.agreement,
       CASE WHEN cand.is_current = 1 THEN c.status ELSE 'pending' END AS status,
       CASE WHEN cand.is_current = 1 AND c.status = 'accepted' THEN c.winning_value
            ELSE NULL END AS final_value,
       c.supports, c.contradictions, c.unverified, c.eva_packet_id, c.eval_cycle_id,
       cand.is_current,
       CASE WHEN {quiet} THEN 0 WHEN COALESCE(q.qn, 0) > 0 THEN 1 ELSE 0 END
         AS question_context,
       CASE WHEN {quiet} THEN 0 ELSE COALESCE(q.qn, 0) END AS question_context_count,
       CAST(CASE WHEN {quiet} THEN 0 WHEN q.qd = 1 THEN {w_direct}
                 WHEN COALESCE(q.qn, 0) > 0 THEN {w_periph} ELSE 0 END AS INTEGER)
         AS question_weight_e4
FROM cand
JOIN packets p ON p.id = cand.packet_id
JOIN v_eva_item_consensus c ON c.eva_packet_id = cand.id
LEFT JOIN q ON q.eva_packet_id = cand.id
WHERE cand.rn = 1"""


def _field_status() -> str:
    """Field-level roll-up of ``v_field_consensus`` (a tag set is as weak as its weakest tag)."""
    return """CREATE VIEW v_field_status AS
SELECT c.packet_id, c.packet_ref, c.packet_version, c.field_name, c.phase, c.kind,
       CASE WHEN SUM(CASE WHEN c.status = 'pending' THEN 1 ELSE 0 END) > 0 THEN 'pending'
            WHEN SUM(CASE WHEN c.status = 'disputed' THEN 1 ELSE 0 END) > 0 THEN 'disputed'
            WHEN SUM(CASE WHEN c.status = 'accepted' THEN 1 ELSE 0 END) = 0 THEN 'null'
            ELSE 'accepted' END AS status,
       COUNT(*) AS items,
       SUM(CASE WHEN c.status = 'accepted' THEN 1 ELSE 0 END) AS accepted_items,
       SUM(CASE WHEN c.status = 'disputed' THEN 1 ELSE 0 END) AS disputed_items,
       MIN(c.confidence) AS min_confidence,
       MAX(c.eva_packet_id) AS eva_packet_id,
       MAX(c.question_context) AS question_context,
       MAX(c.question_context_count) AS question_context_count,
       MAX(c.question_weight_e4) AS question_weight_e4
FROM v_field_consensus c
GROUP BY c.packet_id, c.packet_ref, c.packet_version, c.field_name, c.phase, c.kind"""


# --------------------------------------------------------------------------- statistics


def _packet_stats(d: Dialect) -> str:
    text_expr = f"TRIM(REPLACE(p.prompt_description, {d.newline}, ' '))"
    hours = d.hours.format(a="COALESCE(p.completed_at, CURRENT_TIMESTAMP)", b="p.created_at")
    threshold = _setting("stats.score_threshold", "7")
    return f"""CREATE VIEW v_packet_stats AS
SELECT p.id AS packet_id, p.ref AS packet_ref, p.version AS packet_version,
       CASE WHEN {text_expr} = '' THEN 0
            ELSE LENGTH({text_expr}) - LENGTH(REPLACE({text_expr}, ' ', '')) + 1 END
         AS prompt_word_count,
       (SELECT COUNT(*) FROM eva_packets e WHERE e.packet_id = p.id) AS evaluation_runs,
       (SELECT COUNT(*) FROM channel_runs r WHERE r.packet_id = p.id
          AND r.channel = 'quickfire_evaluation') AS evaluation_attempts,
       COALESCE((SELECT SUM(ep.latency_ms) FROM evaluation_passes ep
                 WHERE ep.packet_id = p.id), 0) AS evaluation_ms,
       {hours} AS lifecycle_hours,
       (SELECT COUNT(*) FROM v_field_consensus c WHERE c.packet_id = p.id AND c.kind = 'score'
          AND c.status = 'accepted' AND CAST(c.final_value AS NUMERIC) >= {threshold})
         AS scores_above_threshold,
       COALESCE((SELECT SUM(i.supports + i.contradictions) FROM evaluation_pass_items i
                 JOIN eva_packets e ON e.id = i.eva_packet_id
                 WHERE e.packet_id = p.id), 0) AS evidence_items,
       0 AS generation_ms
FROM packets p"""


def _branch_stats() -> str:
    """Federated statistics for each trunk (a root packet lineage with descendants)."""
    return """CREATE VIEW v_branch_stats AS
WITH fed AS (
  SELECT r.ref AS root_ref, m.id AS member_id, m.ref AS member_ref
  FROM packets r
  JOIN packets m ON m.root_id = r.id OR m.id = r.id
  WHERE r.root_id IS NULL OR r.root_id = r.id
),
scores AS (
  SELECT fed.root_ref, CAST(c.final_value AS NUMERIC) AS v
  FROM fed JOIN v_field_consensus c ON c.packet_id = fed.member_id
  WHERE c.kind = 'score' AND c.status = 'accepted'
)
SELECT fed.root_ref,
       (SELECT MIN(p.created_at) FROM packets p WHERE p.ref = fed.root_ref) AS created_at,
       COUNT(DISTINCT CASE WHEN fed.member_ref <> fed.root_ref THEN fed.member_ref END)
         AS descendant_count,
       (SELECT AVG(s.v * s.v) - AVG(s.v) * AVG(s.v) FROM scores s
         WHERE s.root_ref = fed.root_ref) AS score_variance,
       COUNT(DISTINCT fed.member_id) - COUNT(DISTINCT fed.member_ref) AS revision_count,
       (SELECT COUNT(*) FROM channel_runs r JOIN fed f2 ON f2.member_id = r.packet_id
         WHERE f2.root_ref = fed.root_ref AND r.status IN ('failed', 'invalid')) AS failed_runs,
       (SELECT COALESCE(SUM(i.contradictions), 0) FROM evaluation_pass_items i
          JOIN eva_packets e ON e.id = i.eva_packet_id
          JOIN fed f3 ON f3.member_id = e.packet_id
         WHERE f3.root_ref = fed.root_ref) AS contradiction_count,
       (SELECT COUNT(DISTINCT ep.model) FROM evaluation_passes ep
          JOIN fed f4 ON f4.member_id = ep.packet_id
         WHERE f4.root_ref = fed.root_ref) AS distinct_models
FROM fed
GROUP BY fed.root_ref
HAVING COUNT(DISTINCT CASE WHEN fed.member_ref <> fed.root_ref THEN fed.member_ref END) > 0"""


def _branch_model_usage() -> str:
    return """CREATE VIEW v_branch_model_usage AS
SELECT r.ref AS root_ref, ep.model, COUNT(*) AS passes
FROM packets r
JOIN packets m ON m.root_id = r.id OR m.id = r.id
JOIN evaluation_passes ep ON ep.packet_id = m.id
WHERE r.root_id IS NULL OR r.root_id = r.id
GROUP BY r.ref, ep.model"""


# --------------------------------------------------------------------------- triggers


def _branch_triggers() -> str:
    """Enabled ``thresholds`` rules that fire on a head packet's current pre-phase consensus."""
    numeric = "CAST(c.final_value AS NUMERIC)"
    rule = "CAST(t.value AS NUMERIC)"
    return f"""CREATE VIEW v_branch_triggers AS
SELECT c.packet_id, c.packet_ref, c.packet_version,
       t.id AS threshold_id, t.name AS threshold_name, t.version AS threshold_version,
       t.action, t.precedence, COUNT(*) AS matched_items, MIN(c.field_name) AS field_name
FROM v_field_consensus c
JOIN thresholds t ON t.enabled
  AND (t.field_name = c.field_name
       OR (t.field_name = '@key_fields' AND c.field_name IN
           (SELECT d.name FROM candidate_field_definitions d WHERE d.is_key_field)))
WHERE c.phase = 'pre' AND (
     (t.operator = 'status_is' AND c.status = t.value)
  OR (t.operator = 'has_tag' AND c.kind = 'tagset' AND c.status = 'accepted'
      AND c.item_key = t.value AND c.final_value = 'on')
  OR (c.kind = 'score' AND c.status = 'accepted' AND (
         (t.operator = '>=' AND {numeric} >= {rule}) OR (t.operator = '>' AND {numeric} > {rule})
      OR (t.operator = '<=' AND {numeric} <= {rule}) OR (t.operator = '<' AND {numeric} < {rule})
      OR (t.operator = '=' AND {numeric} = {rule}) OR (t.operator = '!=' AND {numeric} <> {rule})))
  OR (c.kind IN ('bool', 'text') AND c.status = 'accepted' AND (
         (t.operator = '=' AND c.final_value = t.value)
      OR (t.operator = '!=' AND c.final_value <> t.value))))
GROUP BY c.packet_id, c.packet_ref, c.packet_version, t.id, t.name, t.version, t.action,
         t.precedence"""


# --------------------------------------------------------------------------- readiness, queue


def _ready_candidates() -> str:
    """Explanatory only (D1 point 4): the authoritative readiness rule is the pure Python
    predicate ``orchestrator.evaluation.readiness``, re-checked at admission and claim."""
    return """CREATE VIEW v_ready_candidates AS
WITH req AS (
  SELECT ps.field_name, ps.phase, ps.tier, ps.hard
  FROM eva_presets ps
  WHERE ps.enabled AND ps.required AND ps.tier IN (1, 2)
    AND ps.revision = (SELECT MAX(p2.revision) FROM eva_presets p2
                       WHERE p2.field_name = ps.field_name AND p2.phase = ps.phase)
),
heads AS (
  SELECT p.* FROM packets p
  WHERE NOT EXISTS (SELECT 1 FROM packets s WHERE s.supersedes_id = p.id)
    AND NOT p.locked
)
SELECT h.id AS packet_id, h.ref AS packet_ref, h.version AS packet_version, h.status,
       COUNT(req.field_name) AS required_fields,
       SUM(CASE WHEN fs.status = 'accepted' THEN 1 ELSE 0 END) AS accepted,
       SUM(CASE WHEN fs.status = 'disputed' THEN 1 ELSE 0 END) AS disputed,
       SUM(CASE WHEN fs.status = 'null' THEN 1 ELSE 0 END) AS null_fields,
       SUM(CASE WHEN fs.status = 'pending' THEN 1 ELSE 0 END) AS pending,
       SUM(CASE WHEN fs.status IS NULL THEN 1 ELSE 0 END) AS missing,
       (SELECT COUNT(*) FROM waivers wv WHERE wv.packet_id = h.id
          AND wv.target_version = h.version
          AND NOT EXISTS (SELECT 1 FROM revocations rv WHERE rv.target_type = 'waiver'
                          AND rv.target_id = wv.id)) AS active_waivers,
       (SELECT COUNT(*) FROM packet_dependencies dep
          JOIN packets dp ON dp.id = dep.depends_on_id
         WHERE dep.packet_id = h.id
           AND NOT EXISTS (SELECT 1 FROM revocations rd WHERE rd.target_type = 'dependency'
                           AND rd.target_id = dep.id)
           AND NOT EXISTS (SELECT 1 FROM packets dh WHERE dh.ref = dp.ref
                           AND dh.status = 'completed')) AS unmet_dependencies,
       (SELECT COUNT(*) FROM confirmations cf WHERE cf.entity_id = h.id
          AND cf.status = 'pending') AS pending_approvals
FROM heads h
CROSS JOIN req
LEFT JOIN v_field_status fs ON fs.packet_id = h.id AND fs.field_name = req.field_name
  AND fs.phase = req.phase
WHERE req.phase = 'pre'
GROUP BY h.id, h.ref, h.version, h.status"""


# --------------------------------------------------------------------------- entry points


def view_statements(dialect: str) -> list[str]:
    """The M2 decision views (``VIEW_NAMES``), in dependency order."""
    d = _DIALECT[dialect]
    return [
        _eva_progress(),
        _eva_item_consensus(d),
        _context_link_status(),
        _field_consensus(),
        _field_status(),
        _packet_stats(d),
        _branch_stats(),
        _branch_model_usage(),
        _branch_triggers(),
        _ready_candidates(),
        *genqueue.statements(dialect),
    ]


# --------------------------------------------------------------------------- lineage heads (M1, A2)

#: view → table: for every row, the current head of its lineage, following ``supersedes_id``.
#: ``is_head`` is 1/0 on SQLite, true/false on PostgreSQL.
HEAD_VIEWS: dict[str, str] = {
    "v_project_heads": "projects",
    "v_objective_heads": "objectives",
    "v_packet_heads": "packets",
}


def _head_view(view: str, table: str) -> str:
    return (
        f"CREATE VIEW {view} AS\n"
        "WITH RECURSIVE chain(row_id, current_id) AS (\n"
        f"  SELECT id, id FROM {table}\n"
        "  UNION ALL\n"
        f"  SELECT chain.row_id, nxt.id FROM chain JOIN {table} nxt "
        "ON nxt.supersedes_id = chain.current_id\n"
        ")\n"
        "SELECT chain.row_id AS id, head.id AS head_id, head.ref AS head_ref,\n"
        "       head.version AS head_version, (chain.row_id = head.id) AS is_head\n"
        f"FROM chain JOIN {table} head ON head.id = chain.current_id\n"
        f"WHERE NOT EXISTS (SELECT 1 FROM {table} s WHERE s.supersedes_id = head.id)"
    )


# --------------------------------------------------------------------------- runs (M3, D10 Q3)

#: the console's view of each generation run's latest attempt and its outcome
RUN_VIEWS: dict[str, str] = {
    "v_run_attempts": (
        "CREATE VIEW v_run_attempts AS\n"
        "SELECT r.id AS generation_run_id, r.packet_id, r.packet_version, r.state, r.channel,\n"
        "       r.fencing_token, r.attempts, r.max_attempts, c.id AS channel_run_id, c.run_id,\n"
        "       c.attempt, c.operation_id, c.created_at AS authorised_at, o.outcome,\n"
        "       o.exit_code, o.error, o.created_at AS outcome_at\n"
        "FROM generation_runs r\n"
        "LEFT JOIN channel_runs c ON c.generation_run_id = r.id\n"
        "  AND NOT EXISTS (SELECT 1 FROM channel_runs c2\n"
        "                  WHERE c2.generation_run_id = r.id AND c2.attempt > c.attempt)\n"
        "LEFT JOIN channel_run_outcomes o ON o.channel_run_id = c.id"
    ),
}

#: every view of the schema, in creation order
ALL_VIEW_NAMES: tuple[str, ...] = (*HEAD_VIEWS, *VIEW_NAMES, *genqueue.VIEW_NAMES, *RUN_VIEWS)


def all_view_statements(dialect: str) -> list[str]:
    """Every ``CREATE VIEW`` of the schema, in dependency order (golden rule 7: the dialect
    differences are the ``_DIALECT`` tokens)."""
    heads = [_head_view(view, table) for view, table in HEAD_VIEWS.items()]
    return heads + view_statements(dialect) + list(RUN_VIEWS.values())
