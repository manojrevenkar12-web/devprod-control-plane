# 2. Quarantine flaky tests from flips, with a Beta posterior

**Status:** accepted

**Context.** "Failed sometimes" is not evidence of flakiness: a broken test fails every time, and a
change can break a test intermittently. A test that fails and then passes on the *same commit* is.

**Decision.** Count flips per test. Each test gets a Beta(0.5, 60) prior on its flip rate. It is
quarantined after at least two flips, when P(rate > 1%) ≥ 0.9. Never quarantine a test in a module
the change under test touches. A quarantined test keeps running, but its result does not block
merges. It gates again after 60 clean runs. The owning team (from CODEOWNERS) gets ten days to fix it.

**Consequences.** In the simulation, precision is 1.0: 16 quarantined, none wrongly. It finds 57%
of the truly flaky tests that failed at least once; the rest flipped too rarely in ten weeks to tell
from noise. Quarantine never hid a regression. Counting executions per job, not per test, keeps
the register cheap enough for millions of test executions a week.
