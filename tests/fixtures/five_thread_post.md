---
channel: C0BOGUS0001
session_id: fixture-uuid-0000
---

Sorry for the delay @stakeholder, updates below (adapted from [source](https://example.com), lmk if anything doesn't make sense):

=== mfu

1. Latest MFU (still 6%, but chip-wide now)

+++

Previously-quoted ≈6% was one core of four; now measured across the full chip.

+++ @grayjh

Interesting — is the 6% stable across runs, or is there variance?

+++

See the [profiling thread](#profiling) for methodology.

=== tflops-q

2. Q: what per-core TFLOP/s should I divide by for MFU?

+++

Bogus values from datasheet vs measured differ ~15%; see [MFU thread](#mfu) for the numerator.

=== profiling

3. Profiling / improving MFU

+++

Currently at 6%; target is 20%+.

+++

Two candidate bottlenecks under investigation.

=== ce

4. `bogus-library`, plugin integration?

+++

Blocker: no upstream tags.

+++

Proposed workaround in the [segfault thread](#segfault).

+++

ETA next week.

=== segfault

5. `bogus-compiler` segfault in `lowering-pass`

+++

- MWE gist: <https://example.com/gist>
- One flag flips crash → compile.
