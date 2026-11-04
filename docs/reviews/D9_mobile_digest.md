# D9 — Mobile-first digest (design reference for M3 Stage B)

**Author:** Gemini Argon (design), 2026-11-04. **Reconciled by the PM** (rulings D9-R1–R7 at the end). Triage is in `P1_product_brainstorm.md` (revised triage).
**Stack:** FastAPI + Jinja2 + vendored HTMX; no CDN, no JS framework; bound to `127.0.0.1`; tests use `TestClient`.
**Status:** reference for Stage B. The engineer may deviate, with a reason in the PR.

## 1. Page: `/digest` (the default route) — a single column, max-width 600 px, no horizontal scroll at 360 px
| # | Section | Shows | Reads from |
|---|---|---|---|
| 0 | **Heartbeat banner** (sticky) | Kernel RUNNING / PAUSED (draining) / PAUSED; off-hours indicator (display only, C33/V7); three channel lines (`#QE…`, `#LG…`, `#WP…`, C24); a full-width **Pause & Drain / Resume** button ≥ 48 px tall; a collapsible "Interrupt with prompt" drawer | `control_state`; latest `channel_runs` per channel; injected clock + `governor_timezone` |
| 1 | **Warnings strip**, hidden when all healthy | Pills for metrics over threshold; tap to expand values and thresholds | `v_digest_warnings` (union of §5 views) |
| 2 | **Action required**: top 5, then "showing 5 of N" | **A**: stage confirmations (repo_write with a collapsible unified diff + manifest prefix; branching with proposed child titles; quality gate with an issue summary; failure budget). **B**: parked run (exit 3: run ID, conversation reference, the requested tool/reason). **C**: needs_review (field, A vs B values, fixed-point confidence, quotes). Each card shows a **NEW** pill. | new `v_digest_action_queue`: pending `confirmations` ∪ packet heads in `awaiting_confirmation`/`needs_review` ∪ `generation_runs` in `awaiting_confirmation` ∪ disputed consensus. Order: is_new DESC, priority DESC, created_at ASC |
| 3 | **Governor interventions** | signal, value vs threshold (fixed point), scope, time; buttons Resume scope / Top up / Kill branch | `governor_events` ⋈ `governor_states` since the watermark |
| 4 | **Completed and committed** (since last visit, max 10) | ref, version, channel, COMPLETED 🔒 / EVALUATING_POST, scan badge (passed/unscanned/skipped), short SHA, path, manifest prefix | packet heads ⋈ accepted `artifacts` ⋈ `quality_reports` |
| 5 | **Budget meter** | Per-channel run bars (hour/day) and per-objective packets/tokens; Top-up button once ≥ 80% used | `channel_budget_trackers`, `governor_states` |
| 6 | **Quiet queue** | Counts per status, and blocked counts grouped by exact reason (`objective_token_budget`, `no workbench consumer`, `no isolated runner`, …) | `v_queue`, packet heads, readiness reasons |

The desktop Miller-column view stays at `/tree` for evening sessions.

## 2. "New since last visit" — deterministic, and never a "human touch"
- `GET /digest` and the HTMX polls are **read-only**: they write nothing.
- The **visit watermark** is an append-only `events` row, `event_type='digest_ack'` (actor owner UI). It is written when the owner taps **Mark all seen**, *and* inside the same transaction as any one-tap decision.
- `v_owner_watermark.last_visit_at` = MAX(created_at) of `digest_ack` plus the owner decision events.
- An item is NEW iff its creation time > `last_visit_at`.
- **Human touch (C33)** is updated only by owner *decisions* (§3 actions and CLI equivalents), never by `digest_ack` or page views.

## 3. One-tap actions (HTMX `POST`; card swapped with `outerHTML`)
**Shared protocol (all actions).**
- **Hidden fields:** `expected_gate_revision`, `target_version`, and a deterministic `idempotency_token` (e.g. `confirm:{id}:{G}`).
- **Guarded write.** A guarded write that changes one row records the token in its event and returns a green **✓ receipt** card.
- **Repeat tap.** If no row matched and the token already exists, it was a repeat tap: return the **same receipt** (no duplicate effect).
- **Changed state.** If no row matched and the token is unknown, the state changed underneath: return an amber **"State changed — refresh"** card (`hx-get` the card), with no side effect.

| Action | Path | Preset reasons | Guard (spec) | Event |
|---|---|---|---|---|
| Approve / reject confirmation | `/digest/actions/confirm/{confirmation_id}` | verified output · too broad · wrong approach · not needed now | pending; G and version match; C25 bump; failure-budget approve → allowance (C27); reject → `held` (C27, D8 R3); repo_write approval uses the **full publication guard (D8 E4)** | `confirmation_decided` |
| Approve-resume / cancel parked run | `/digest/actions/parked-run/{run_id}` | tool approved · unsafe tool | run `awaiting_confirmation`; run G = packet G = expected → both +1 (D8 R1); dispatch guard (D8 E3); cancel → run `failed`, no budget charge (C27) | `parked_run_resumed` / `parked_run_cancelled` |
| Waive soft field | `/digest/actions/waive/{packet_id}` | brief clear enough · accept higher-confidence pass · not relevant | soft fields only (hard constraints and required execution gates never: C13/C31/V5); bound to version and phase (+ manifest for post) | `override_added` (waiver) |
| Hold | `/digest/actions/hold/{packet_id}` | revisit later · waiting on something external · low priority | non-terminal; G match; HOLD bound to the version; packet → `held`; an in-flight run becomes stale only per C25/E7 | `override_added` (hold) + an Undo button (revoke) |
| Kill branch | `/digest/actions/kill-branch/{packet_id}` | rabbit hole · unnecessary split · wrong direction | **BLOCK at branch scope** (D9-R1); G bumped on every non-terminal packet in the branch | `branch_killed` |
| Close unverified | `/digest/actions/close-unverified/{packet_id}` | out of scope · done elsewhere · invalid task (**required**) | non-terminal; G match; → `closed_unverified`, locked (C23) | `packet_closed_unverified` |
| Top up objective budget | `/digest/actions/topup/{objective_id}` | owner top-up | C36; idempotency by token (D9-R3) | `budget_topup` + `governor_events` |
| Pause / resume kernel | `/digest/actions/kernel-pause` | — | serialised with dispatch (C26) | `kernel_paused` / `kernel_resumed` |

## 4. Templates
`base.html`: viewport meta; vendored `htmx.min.js` from `/static`; one small inline stylesheet.
- The sticky banner holds a full-width Pause button (min-height 48 px).
- Diffs sit in `<pre class="diff-box">` with `overflow-x: auto`, so long lines scroll *inside* the box.
- Representative cards: a repo_write confirmation (collapsible diff, Approve / Reject + reason select) and the budget meter.

## 5. Early-warning metrics: read-only views, integer fixed point ×10,000 (C28)
Each view returns `metric_name, value_fp, threshold_fp`, using integer division with a zero-denominator guard. Ship **only the ones computable from the Stage A schema** (D9-R5):
1. `v_warn_confirmation_gridlock`: confirmations per completed packet (warn > 20,000).
2. `v_warn_score_clustering`: share of pass scores in {6, 7} (warn > 6,000), and WP routing share (warn > 7,500).
3. `v_warn_stale_waste`: share of runs ending `stale` (warn > 1,000). A count proxy until runs record elapsed time and tokens.
4. `v_warn_digest_staleness`: hours since the oldest un-actioned action card was created (warn > 48 h).
5. **`v_warn_busy_but_useless`** (GPT-6.1): completed packets per elapsed day and per owner decision. Warn if zero completions in 3 days while > 20 runs happened.
6. Hollow-output and quote-starvation metrics: **deferred** until deterministic analysis results and structured rejection reasons exist.

## 6. Tests (`tests/test_gui_digest.py`)
- `GET /digest` ×3 changes nothing: no events, watermark unchanged, human touch unchanged; NEW pills still shown.
- **Repeat tap**: the same confirm POST twice gives two ✓ receipts; G incremented once; one `confirmation_decided` event.
- **Stale G**: a background hold bumps G, then a waive with the old G returns the amber card; no waiver inserted; G unchanged.
- **D8 R1 regression**: approve-resume of a parked run: run G and packet G both +1, run → `dispatched`, same conversation ID.
- **360 px structure, no browser**: parse the HTML/CSS with the stdlib parser and check:
  - the viewport meta is present;
  - no fixed width > 340 px;
  - `.diff-box` has `overflow-x: auto`;
  - the Pause rule has `min-height: 48px; width: 100%`;
  - there are no external `http(s)` script or style URLs.
- Every action endpoint calls the **same domain service as the CLI** (no separate business logic in the GUI layer); a test asserts identical events from both paths.

## 7. PM rulings
- **D9-R1 Kill branch = BLOCK at branch scope (reversible)**, not closed_unverified. Reversibility protects history (K5), and closing remains a separate, explicit action. *This corrects the D8 row "non-terminal → closed_unverified (close or branch kill)": branch kill now goes to `blocked`.*
- **D9-R2 There is no auto-expiring "snooze".** A HOLD never clears itself (C33). The label is "Hold", with an Undo (revoke) button.
- **D9-R3 Top-up idempotency uses the token**, not Argon's "sum of gate revisions". A top-up is a C25 governance write, so in-flight runs in that objective may go stale. Accepted for v1; watch it with the stale-waste metric.
- **D9-R4 Same domain services.** GUI and CLI share one service layer per action; guards are never duplicated in route handlers.
- **D9-R5 Metrics.** Ship only those computable from the Stage A schema, plus "busy but useless". Hollow output and quote starvation wait for their inputs. `digest_ack` is the only new event type the page introduces.
- **D9-R6 No packet-scope default.** Hold, waive and kill take their scope from the button pressed (a packet card → packet; a branch action → branch). The GUI offers no objective/project-wide action in v1. *This answers GPT-6.1's concern without a hidden default.*
- **D9-R7 Stub rejection** is not in Stage B until the CFO decides (Q-P6). Stage B may *display* a "stub-only" flag computed by the static validator.
