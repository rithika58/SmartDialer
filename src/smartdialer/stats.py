"""
Small, explainable statistics.

The assignment says: "We don't need a fancy ML model. A sensible statistical or
rule-based approach is completely fine."  So everything here is transparent and
every number the pacing engine produces can be traced back to an input.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class BetaBinomialEstimate:
    """Answer-rate estimate with shrinkage towards a prior.

    p_hat = (prior_successes + successes) / (prior_trials + trials)

    Why shrinkage: with 3 observed calls, a 100% answer rate is noise, not
    signal.  Pulling towards the prior stops the dialer from suddenly dialing
    very aggressively on a tiny sample -- which is exactly when it is most
    likely to generate abandoned calls.
    """

    successes: float = 0.0
    trials: float = 0.0
    prior_successes: float = 2.0
    prior_trials: float = 10.0

    def update(self, success: bool, weight: float = 1.0) -> None:
        self.trials += weight
        if success:
            self.successes += weight

    def decay(self, factor: float) -> None:
        """Exponentially forget old observations so the estimate tracks drift."""
        self.successes *= factor
        self.trials *= factor

    @property
    def p(self) -> float:
        denom = self.prior_trials + self.trials
        if denom <= 0:
            return self.prior_successes / max(self.prior_trials, 1.0)
        return (self.prior_successes + self.successes) / denom

    @property
    def confidence(self) -> float:
        """0..1 -- how much the data (rather than the prior) is driving p."""
        total = self.prior_trials + self.trials
        if total <= 0:
            return 0.0
        return self.trials / total

    def as_dict(self) -> dict:
        return {
            "p": round(self.p, 4),
            "successes": round(self.successes, 2),
            "trials": round(self.trials, 2),
            "prior_trials": self.prior_trials,
            "confidence": round(self.confidence, 3),
        }


def binom_sf(k: int, n: int, p: float) -> float:
    """P(X > k) for X ~ Binomial(n, p).  Exact via the regularised beta
    function, with a normal approximation fallback for large n.

    This is the abandonment guard: if we have `n` calls in flight and only `k`
    agents that could take them, P(X > k) is the probability that at least one
    connected borrower has nobody to talk to.  That is the compliance event the
    Safety Controller exists to prevent.
    """
    if n <= 0 or p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0 if k < n else 0.0
    if k < 0:
        return 1.0
    if k >= n:
        return 0.0

    # Exact: P(X > k) = I_p(k+1, n-k)
    try:
        return _betainc_regularized(k + 1, n - k, p)
    except (ValueError, ZeroDivisionError, OverflowError):
        pass

    # Fallback: normal approximation with continuity correction.
    mean = n * p
    var = n * p * (1 - p)
    z = (k + 0.5 - mean) / math.sqrt(var) if var > 0 else 0.0
    return 1.0 - 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _betainc_regularized(a: int, b: int, x: float) -> float:
    """Regularised incomplete beta I_x(a, b) via the continued fraction.

    Numerical Recipes' betacf.  Exact for the integer a, b we need and fast
    enough for the call volumes we simulate.
    """
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0

    lbeta = (
        math.lgamma(a + b)
        - math.lgamma(a)
        - math.lgamma(b)
        + a * math.log(x)
        + b * math.log1p(-x)
    )
    front = math.exp(lbeta)

    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + b * math.log1p(-x)
        + a * math.log(x)
    ) * _betacf(b, a, 1.0 - x) / b


def _betacf(a: float, b: float, x: float, itmax: int = 300, eps: float = 3e-16) -> float:
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d

    for m in range(1, itmax + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c

        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def ewma(previous: float, sample: float, alpha: float) -> float:
    return alpha * sample + (1.0 - alpha) * previous


def percentile(values, pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    idx = (len(ordered) - 1) * pct
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return float(ordered[lo])
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * (idx - lo))


def binom_pmf_array(n: int, p: float) -> list:
    """Full PMF of Binomial(n, p) via the standard recurrence.

    O(n) and numerically stable enough for the sizes we use.  Building the whole
    PMF once is far cheaper than evaluating the tail many times.
    """
    if n <= 0:
        return [1.0]
    p = min(max(p, 0.0), 1.0)
    q = 1.0 - p
    pmf = [0.0] * (n + 1)
    if p == 0.0:
        pmf[0] = 1.0
        return pmf
    if p == 1.0:
        pmf[n] = 1.0
        return pmf
    # start from the mode to keep the recurrence well conditioned
    pmf[0] = math.exp(n * math.log(q))
    for k in range(n):
        pmf[k + 1] = pmf[k] * (n - k) / (k + 1) * (p / q)
    return pmf


def survival_from_pmf(pmf: list) -> list:
    """sf[k] = P(X > k), computed by a single reverse cumulative pass."""
    n = len(pmf) - 1
    sf = [0.0] * (n + 2)
    acc = 0.0
    for k in range(n, -1, -1):
        acc += pmf[k]
        sf[k] = acc - pmf[k] if k <= n else 0.0
    # fix up: sf[k] must exclude pmf[k]
    acc = 0.0
    for k in range(n, -1, -1):
        sf[k] = acc
        acc += pmf[k]
    sf[n + 1] = 0.0
    return sf


def abandon_probability(
    in_flight: int,
    p_answer: float,
    free_now: int,
    at_risk_agents: int,
    q_free: float,
) -> float:
    """P(borrowers who answer > agents able to take them).

    A ~ Binomial(in_flight, p_answer)   -- how many of the dialed borrowers pick up
    F ~ Binomial(at_risk, q_free)       -- how many *additional* agents free up

    The one-sided model (F fixed) is the naive version and it underestimates
    risk, because agent availability is itself random -- a run of long calls
    starves the floor exactly when a run of answers arrives.  Modelling both
    sides and convolving is what makes the guard honest.

    `at_risk_agents` are agents already busy whose expected free time is beyond
    the horizon; they might still finish early.  Agents expected to free inside
    the horizon are already counted in `free_now` by the caller.
    """
    if in_flight <= 0 or p_answer <= 0.0:
        return 0.0
    p_answer = min(max(p_answer, 0.0), 1.0)

    pmf_a = binom_pmf_array(in_flight, p_answer)
    sf_a = survival_from_pmf(pmf_a)

    def _sf(k: int) -> float:
        k = int(k)
        if k < 0:
            return 1.0
        if k >= in_flight:
            return 0.0
        return sf_a[k]

    if at_risk_agents <= 0 or q_free <= 0.0:
        return _sf(free_now)

    q_free = min(max(q_free, 0.0), 1.0)
    pmf_f = binom_pmf_array(at_risk_agents, q_free)

    mean = at_risk_agents * q_free
    sd = math.sqrt(at_risk_agents * q_free * (1.0 - q_free))
    lo = max(0, int(mean - 6 * sd))
    hi = min(at_risk_agents, int(mean + 6 * sd) + 1)

    total = 0.0
    for f in range(lo, hi + 1):
        pf = pmf_f[f]
        if pf <= 1e-15:
            continue
        total += pf * _sf(free_now + f)
    return min(max(total, 0.0), 1.0)
