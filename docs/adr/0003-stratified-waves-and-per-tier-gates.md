# 3. Stratified waves, gates per tier, alpha per rollout

**Status:** accepted

**Context.** The usual canary is the quietest accounts, gated on the pooled error rate. A fault in a
path that only enterprise customers use (an enterprise-only connector, say) never runs in that canary.
The pooled rate also dilutes it in later waves. Every extra wave and extra look at a quiet canary is
another chance to roll back a good release.

**Decision.** Every customer wave takes the same share of every tier, quietest accounts first. Its
gate is a one-sided binomial test per tier against the same accounts' rate before the deploy. Quiet
waves bake longer until they have traffic. The rollout's alpha (0.02) is split across waves, tiers
and looks (Bonferroni). Customers' deploy windows are respected.

**Consequences.** In the simulation, all 16 bad rollouts were stopped by a gate. 8 customer
accounts ran a bad release, against 141 with the usual canary and 260 with staging-then-everyone.
There were no false rollbacks. A version with alpha 0.01 per wave had 2–3 false rollbacks in about
105 rollouts; that is why alpha is per rollout. The price: the median release reaches 95% of
customers about three hours later, and lead time for changes rises from 13.3 to 16.2 hours.
