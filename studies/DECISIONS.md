# Lab decisions — append-only notebook (§9.5)

Every EXTEND/KILL/registration/amendment lands here with rationale, newest last.
Git history is the tamper-evidence; never rewrite an entry.

---

## 2026-07-04 — M2 harness commissioned

- **Placebo criterion corrected (Class C, flagged to owner):** the plan's
  literal "95th pct |t| < 2" mis-fires on correct machinery (clustered t has
  ~(n_months−1) dof; its null p95 is ≈2.6–2.8 at 4–5 months, 1.96 at ∞ — at the
  realistic 5–15-month range the literal rule false-alarms ~81%). Operating
  rule: dof-aware exceedance count, dirty when >10% of shuffles exceed their
  own dof's 95% critical |t| (measured: false-alarm 3.9%, power 55% @ +1 SE
  machinery bias, ~100% @ +2 SE). The literal p95 metric is still recorded on
  every PLACEBO row for transparency.
- **Adversarial commissioning sweep** (6 probes): ts bootstrap p-calibration
  verified uniform (100 seeds); CAR entry-adjacency + session-alignment bugs
  found and fixed BEFORE any real study ran; schema NULL-dedup / verdict-
  clobber / tier-poison latents fixed. Machinery trusted from this date.

## 2026-07-04 — Registrations

- **btc_trend_policy** (POLICY, portfolio): 200-DMA ±2% hysteresis on the held
  stack; spec pins band/cost; textbook-contaminated backtest acknowledged —
  forward leg is the evidence.
- **btc_accum_policy** (POLICY, portfolio): LT-tier DCA spend-tilt
  (0.75/1.0/1.5/2.0), banked-cash mechanics, identical contributed capital.
- **insider_cluster** (ALPHA, car, h=21): ≥2 officers/directors, code-P,
  ≥$50k/14d, routine-insider exclusion; event at latest contributing filing.
  10b5-1 flag unavailable in SF2 — routine filter is the pre-registered proxy.
- **sue_pead** (ALPHA, car, h=21): SRW-SUE deciles per quarter (top=LONG,
  bottom=SHORT analytic), event at the 8-K acceptance instant. Registered
  pending the universe crawl completing.

## 2026-07-04 — First verdicts

- **btc_trend_policy → PROMOTED** (backtest window 2015–2026): ret +22,365% vs
  +22,201% B&H, maxDD 68% vs 84%. Honest note: IS-only (2016–2021) would have
  FAILED the return leg — the pass is carried by the 2022 bear. Forward leg
  re-evaluated monthly; kill criteria in the spec.
- **btc_accum_policy → PROMOTED** on razor-thin margins (+1.4pp return / 0.1pp
  DD over 8y, 422 contributions). Recorded verbatim; per the plan this
  explicitly does NOT satisfy the meta-gate ("DCA with a dashboard").
- **insider_cluster** run complete (10,444 priced events 2016–2026): OOS
  t=3.55 @ h21, n=3,823, after-tax +0.91%/event; IS consistent (+, t=3.43).
  Verdict pending the placebo suite (running).

## 2026-07-04 — insider_cluster → PROMOTED (first ALPHA promotion)

Gate legs, all passed on OOS (2022→registration): clustered t=3.55 ≥ 3.0;
n_events=3,823 ≥ 100; n_months=54 ≥ 12; after-tax +0.91%/event at modal-tier
costs; sign-consistent (IS mean +, OOS mean +); placebo CLEAN (p95|t|=1.61,
exceedance 4% — passes even the plan's literal <2 bar). Horizon profile: OOS
t 6.97/5.45/3.55/3.39 at h 5/10/21/63.

Honesty notes carried with the promotion: the hypothesis is literature-known
(CMP 2012) so OOS is less-contaminated rather than clean; the population is
2016+ (vendor window); LIVE forward evidence + the collision re-run vs
sue_pead (once it lands) are the remaining tests. Meta-gate precondition
(≥1 ALPHA-PROMOTED) now satisfied pending the live paper curve vs SPY.

## 2026-07-04 — sue_pead → EXTEND (honest miss); insider promotion collision-cleared

- **sue_pead** (17,746 decile events from an 88,506-quarter universe crawl;
  13,716 priced): IS showed the textbook short-horizon drift (t 2.9/2.9/2.2 at
  h 5/10/21) but **OOS (2022+) t=0.87 at the primary h=21** — the published
  anomaly is weak/decayed out-of-sample. Hard legs intact (after-tax +0.19%,
  sign-consistent, placebo CLEAN p95|t|=1.93/exceed 4%) ⇒ soft miss ⇒
  **EXTEND** (the one extension; next miss kills). This is the machinery
  working: a 35-year-old published edge does not get promoted on reputation.
- **Collision report** (§5.2): 1.0% of insider_cluster events (198/19,996) sit
  within ±5 sessions of a sue_pead event on the same name — the ALPHA
  promotion cannot be a PEAD duplicate; no excluded re-run required at this
  overlap. Recorded here as the robustness check.

## 2026-07-04 — clone13f → KILLED (and a machinery lesson)

Registered same-day (owner overruled the queue-pacing misread: the §6 queue was
pre-registered in the plan; only data-readiness gates registrations). 1,606
add-events by concentrated low-turnover managers, 2022+ only — pre-2022 SF3
books in this bundle export are TRUNCATED per filer (~200 vs ~720 holdings/
investor), and the turnover filter systematically excluded the unreliable era.

Verdict: **KILLED** — OOS t=1.58 at h=63 (bar 3.0), after-tax +0.73%/event,
AND the placebo suite came back DIRTY (exceedance 16% > 10%, p95|t|=2.59).
The dirty placebo is a structural finding, not noise: events sit on 16
quarterly dates (~100/date) and consecutive quarters' 63-session forward
windows OVERLAP, so month-clustered t overstates significance for this shape.
The kill is DIRECTIONALLY SAFE: the known bias inflates t, and even inflated
it reached only 1.58.

Machinery action item (Class A, before any future quarterly-cadence study can
be trusted): quarter-level clustering (or event-date clustering + a non-overlap
horizon rule) for studies whose events share filing dates. A clone13f-v2 under
corrected clustering MAY be re-registered; this record freezes.

Also fixed en route: single-era populations (no IS rows) no longer hard-fail
the sign-consistency leg — the split is "not applicable" per §5.5, with a
regression test.

## 2026-07-04 — lt_factor → WATCHLIST (unscored factor screen); verification-cleared

Registered same-day (data-ready: SF1 ART + SEP + SFP all in the lake). The
evidence-based QVM screener (value-trap gate → value/quality/momentum ranked
separately → intersection, top-30 equal-weight, monthly rebalance, SF1 ART PIT,
net turnover cost), 113 rebalances 2017-02..2026-06.

**Verdict: WATCHLIST** — port +114.6%, dead-matching the equal-weight universe
(+114.9%), and CRUSHED by a 50/50 VTV+QUAL ETF blend (+215.5%). OOS (54 months):
active-return clustered t = 0.78 vs universe, 0.25 vs ETF (bar 2.0). Fails BOTH
benchmark legs ⇒ "Watchlist (unscored factor screen)", no third state. n=54≥36,
so it is genuinely the t, not sample size. The plan's own "why not just buy the
ETFs?" test answered, for this window: **just buy VTV+QUAL.** An honest null on
the strongest-claimed free equity edge — value's lost decade + the mega-cap
quality run the equal-weight small/mid book couldn't match.

**Adversarially verified TRUSTWORTHY** (4 probes, all PASS): momentum is correct
12-1/causal/positive-signed (Spearman +1.0, picks winners not falling knives);
benchmarks fair/conservative (ETF closeadj = dividend-adjusted TR; turnover cost
nets only the portfolio); PIT clean (42d median filing lag, no look-ahead);
t-stats reproduce to full precision; active-return stdev 3.98% = real tracking
error with zero net alpha. Every bias in the code FLATTERS the strategy and it
still failed — the null is real, not a look-ahead/inversion/benchmark artifact.

Known limitation for a FUTURE lt_factor-v2 (WATCHLIST-safe, verdict-invariant —
imputation moved OOS t <0.01): the sel_fwd non-finite drop is a forward-return
survivorship shortcut; a SCORED buy-list must book mid-hold delistings at their
realized delisting return (`app.data.equities.delisting.terminal_return` exists
for this). Not fixed now because it cannot change WATCHLIST; wire it if v2 is
ever pursued.

## 2026-07-04 — Deferrals (deliberate, revisit at collector cutover)

- Legacy candle-replay backtest scripts + winrate/track-record JSON loaders
  stay until the live collector cuts over to Sharadar+SUE and the dashboard's
  swing surfaces read DB verdicts — retiring them now would blank honest
  surfaces without replacement data. (M2-acceptance letter deviation, owner-
  visible here.)
- SEP/SF1 history is a 10-year vendor window (2016+), not 1998+ as the plan
  assumed — IS is 2016–2021 (~1.2 regimes). All specs carry the caveat.

## 2026-07-04 — Dashboard redesign shipped end-to-end (P0-ops → P4 + audit fix pass)

DASHBOARD_REDESIGN.md v1.1 executed in full and deployed. The design law is
now enforced everywhere: every on-screen claim traces to a lab verdict or is
explicitly unscored context — no third kind of number.

- **IA**: 🏠 Today (decision-first home: Act rows / health strip / testing
  strip / paper book) · ₿ Bitcoin (was `/`) · 📈 Stocks · 🧪 Lab, plus the
  owner-only 📒 /book. PWA-installable.
- **One aggregation** (`/api/today` = the daily digest email, Gap D): act
  rows since previous business day 00:00 ET — PROMOTED events (stale-feed
  demoted, Gap C), tier changes + froth escalations vs the PRE-WINDOW state,
  trend flips; testing strip with concrete next-decision dates; after-tax
  paper NAV; health summary.
- **Alerting (§4/§8)**: instant push = ACT (tier/exit/flash) / RISK (froth) /
  FAIL (watchdog, always rings) with quiet hours 03-11 UTC; the stock swing
  instant alert is RETIRED — setups record silently (row still arms cooldown
  + forward test); everything else rides the 12:45 UTC weekday digest.
- **P3 retirements → archive/v1**: st_winrates / stock_st_winrates /
  stock_track_record + their generators and the M0 backtests (every alerted
  cell had measured coin-flip; the PEAD seed was look-ahead-tainted). The
  maturity "prior" rung died with them, both languages. Confidence %s render
  nowhere; sue_pead's LIVE status is the swing surface's label.
- **Paper book (meta-gate)**: after-tax NAV (harness.tax on realized legs)
  vs SPY TR; skips recorded with reasons; /book owner-only.
- **Directive 6 enforcement**: owner/team htpasswd split + X-Auth-User set
  by nginx (now also in the repo's provisioning scripts); frontend
  fail-CLOSED in production when the header is absent (lib/owner.ts).
- **GAP E**: nightly registry snapshot now pushed off-disk to origin.
- **Adversarial conformance audit** (6 dimensions → 27 findings, 27
  confirmed by refutation-verifiers, 0 false positives) — all 27 closed
  same-day. Notables: tier-change detection was window-blind (would drop
  changes >6h old), sue_pead's verdict was hardcoded copy, the owner gate
  was fail-open, froth wore verdict-red, PROMOTED chips lacked gate stats.

Deliberately NOT done: Playwright (curl smoke script instead —
scripts/smoke.sh runs post-deploy); Litestream/healthchecks (owner-input);
Lake→DuckDB streaming merge (laptop nightly stays the lab's data source,
freshness self-reported via lab_meta.last_sync).

## 2026-07-06 — Four new equity studies registered; ALL fail to certify a new pick

Owner asked what else the captured data can turn into long-term stock picks.
Four pre-registered studies were run (register → emit → car/portfolio run →
placebo → verdict). Result: **no new certified pursue surface** — the promoted
h21 `insider_cluster` remains the only equity alpha. The honesty machinery
rejected all four, three of them exactly as the M2 survey predicted (long
horizons underpowered; factors commoditized).

- **buyback → WATCHLIST** (POLICY/portfolio, `_run_buyback`). Monthly-rebalance
  long the top-30 net repurchasers (buyback yield = −ncfcommon/marketcap) vs the
  equal-weight PIT universe AND PKW. OOS active-return t = **−0.52 vs universe /
  −1.03 vs PKW** (bar 2.0) — net repurchasers *underperformed* passive 2022–2026.
  An honest null, same shape as `lt_factor`: just buy PKW.
- **insider_cluster_hi → KILLED (duplicate).** High-conviction subset of the
  promoted signal (≥$250k cluster + CEO/CFO participation). Passed the raw gate
  (OOS h21 t=3.19) but is a ~100% SUBSET of `insider_cluster` — every hi event is
  a base event — and on the same fresh lake the FULL population scored *better*
  (OOS h21 t=4.99, +1.17%/event) than the strict subset (t=3.19, +0.93%). No
  concentration benefit; killed-duplicate per §5.2. The plain ≥2-insider/≥$50k
  cluster is the better signal — extra "conviction" filters add nothing.
- **insider_cluster_q → KILLED (dirty placebo).** Same events as
  `insider_cluster`, verdict at h=63 (quarter hold). Point estimate looked huge
  (OOS h63 t=5.73, after-tax **+3.59%/event**, monotonically rising CAR) BUT the
  h63 placebo came back **DIRTY: exceedance 30% (bar 10%), p95|t|=3.02**. Insider
  clusters fire year-round, so at a 63-session hold adjacent events' forward
  windows OVERLAP and the MONTH-clustered t overstates significance — the t=5.73
  is inflated, not real. NOT "no edge" (the h21 base is promoted and its drift
  clearly continues); it is "not certifiable at a quarter under month-clustering."
- **sue_pead_confirmed → KILLED (dirty placebo + soft t).** The genuinely novel
  idea and the biggest near-miss: top-decile SUE conditioned on a LOOK-AHEAD-SAFE
  positive announcement-day reaction (revive the drift where the market confirmed
  the surprise). It **lifted the dead `sue_pead` from OOS t=0.87 → 2.98** at h21
  (after-tax +0.19% → **+1.60%/event**) — the refinement clearly works
  economically. But (a) 2.98 is a hair under the 3.0 bar, and (b) the placebo is
  badly DIRTY: **exceedance 56%, p95|t|=3.55**. Earnings events cluster in season
  and the reaction-selection concentrates them further, so overlapping h21 windows
  inflate the month-clustered null. Point estimate promising, significance not
  certifiable.

**Machinery lesson (Class A, re-affirmed and now BLOCKING two live leads).** The
`clone13f` kill already flagged that studies whose events share/overlap windows
need **quarter-level (or event-date + non-overlap-horizon) clustering** instead
of month-clustering. TWO economically-promising signals just died on this exact
issue — the quarter-hold insider drift and the confirmed-PEAD revival both have
inflated month-clustered t / dirty placebos. Building that clustering fix is the
single highest-value machinery investment: it is the ONLY thing that could
certify either signal (though the honest quarter/event-clustered t may still fall
short — the fix reduces the effective sample). Until then, neither is a pick.

Net: emitters (`emit_insider_cluster_q/_hi`, `emit_sue_pead_confirmed`, refactored
`_insider_fills`), the `_run_buyback` portfolio runner, and four specs are
committed as the reproducible record. No dashboard change — nothing promoted. The
studies live locally; a box register/emit/run would add graveyard rows to /lab
(deferred — no picks to surface).
