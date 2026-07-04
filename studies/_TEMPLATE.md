# Study pre-registration: <name>

**Asset:** EQ | BTC · **Evaluator:** car | ts | portfolio · **Tier:** alpha | policy | premium
**Primary horizon:** <sessions/days> · **Registered:** <date> · **Author:** owner

## Hypothesis
One paragraph: what edge, why it should exist (who is on the other side and why
they lose), and the microstructure/behavioral prior.

## Event definition (exact)
The precise, code-equivalent rule that emits an event. Filters, thresholds,
windows, data sources. Any later change to this section re-registers as
`<name>-v2` (old results freeze).

## Gate
Tier gate per §5.5 (thresholds copied here verbatim so the registration is
self-contained). Event-count floor: 100 default / 60 quarterly / 40 BTC-ts.

## Known contaminations / caveats
Sample-window limits (e.g. SEP 2016+), sign conventions, collision candidates,
structural breaks (BTC on-chain: mandatory pre/post-2024-01 split).

## Kill criteria
What KILLED looks like beyond the gate (e.g. placebo dirty, sign flip across
the split, survives-only-with-collisions).
