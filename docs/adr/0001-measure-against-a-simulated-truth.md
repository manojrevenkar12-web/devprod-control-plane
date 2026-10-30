# 1. Measure the platform against a simulated organisation with a known truth

**Status:** accepted

**Context.** A DevProd platform claims to save engineering time, catch more regressions and stop bad
releases. In a real company you cannot know which tests are really flaky or which merged changes
really broke something. So you cannot tell a good flake detector from a lucky one, or a triage agent
from one that says "flaky" every time.

**Decision.** Simulate the organisation: 42 engineers, 25 modules, 1,134 pull requests, flaky
tests with known rates, regressions with known reach, registry outages and 260 customer accounts.
Run it twice, under the baseline and under the control plane, with common random numbers. The same
test then flakes on the same commit and attempt in both worlds, and the same change is a regression
in both. The platform sees only what GitHub, the CI and AWS would record. `devprod.evaluation` scores
it against the truth.

**Consequences.** Differences between the worlds come from the platform, not from the dice.
Numbers are only as good as the simulation's assumptions, which live in `config/org.yaml`. Held-out
CI failures written by someone else (ADR 4) keep the triage agent from fitting the simulator's own
log formats. That fitting is exactly what the first held-out set found.
