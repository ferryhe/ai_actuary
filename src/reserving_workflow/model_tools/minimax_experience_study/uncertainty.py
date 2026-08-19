"""Approved 95% Poisson intervals and the unavailable-amount contract.

The 95% interval on the actual count A is the Garwood exact interval

    [chi^2(alpha/2, 2A)/2, chi^2(1-alpha/2, 2(A+1))/2]

with the lower bound defined as zero when A = 0. The interval on the
ratio A/E is obtained by dividing each endpoint by the deterministic
expected count E.

The amount-moment contract is intentionally not implemented here.
The benchmark does not supply a reconciled amount-moment interface,
so ``amount_uncertainty_unavailable`` returns a sentinel value
rather than an invented variance formula.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal


__all__ = [
    "AMOUNT_UNCERTAINTY_BASIS",
    "AmountUncertaintyUnavailable",
    "CountUncertaintyInterval",
    "POISSON_BASIS",
    "amount_uncertainty_unavailable",
    "count_poisson_interval",
]


POISSON_BASIS = "poisson_exact_garwood_95"
AMOUNT_UNCERTAINTY_BASIS = "amount_uncertainty_contract_absent"


@dataclass(frozen=True)
class CountUncertaintyInterval:
    """Garwood exact Poisson interval for an actual death count."""

    actual_count: int
    lower_count: float
    upper_count: float
    confidence_level: float = 0.95


@dataclass(frozen=True)
class AmountUncertaintyUnavailable:
    """Sentinel returned when the amount-moment contract is absent."""

    available: bool
    basis: str
    note: str


def amount_uncertainty_unavailable() -> AmountUncertaintyUnavailable:
    """Return the sentinel value used in lieu of an invented amount CI."""
    return AmountUncertaintyUnavailable(
        available=False,
        basis=AMOUNT_UNCERTAINTY_BASIS,
        note=(
            "No reconciled amount-moment interface is supplied to this "
            "benchmark; amount uncertainty is reported as unavailable."
        ),
    )


def count_poisson_interval(
    actual_count: int,
    expected_count: Decimal,
    confidence: float = 0.95,
) -> CountUncertaintyInterval:
    """Return the Garwood exact Poisson interval for ``actual_count``.

    The interval is on the actual count A; callers divide each endpoint
    by the deterministic ``expected_count`` to obtain an interval on
    the A/E ratio. The ``expected_count`` argument is accepted so the
    signature matches a future moment-moment contract and to make the
    caller-side division explicit.
    """
    del expected_count  # interval bounds are on the actual count only
    a = max(0, int(actual_count))
    alpha = max(0.0, min(1.0, 1.0 - confidence))
    if a <= 0:
        lower_a = 0.0
    else:
        lower_a = _chi2_quantile_even(alpha / 2.0, a) / 2.0
    upper_a = _chi2_quantile_even(1.0 - alpha / 2.0, a + 1) / 2.0
    return CountUncertaintyInterval(
        actual_count=a,
        lower_count=lower_a,
        upper_count=upper_a,
        confidence_level=confidence,
    )


# ---------------------------------------------------------------------------
# Chi-square quantile helpers (even degrees of freedom only). Implemented
# from scratch because the dependency lock pins only Polars and forbids
# extra packages.
# ---------------------------------------------------------------------------


def _normal_quantile(p: float) -> float:
    """Acklam-style approximation to the standard normal quantile.

    Accurate to about 1e-9 across the full range, sufficient for our
    chi-square quantile use.
    """
    if not 0.0 < p < 1.0:
        raise ValueError("p must lie strictly between 0 and 1")
    a = [
        -3.969683028665376e+01, 2.209460984245205e+02,
        -2.759285104469687e+02, 1.383577518672690e+02,
        -3.066479806614716e+01, 2.506628277459239e+00,
    ]
    b = [
        -5.447609879822406e+01, 1.615858368580409e+02,
        -1.556989798598866e+02, 6.680131188771972e+01,
        -1.328068155288572e+01,
    ]
    c = [
        -7.784894002430293e-03, -3.223964580411365e-01,
        -2.400758277161838e+00, -2.549732539343734e+00,
        4.374664141464968e+00, 2.938163982698783e+00,
    ]
    d = [
        7.784695709041462e-03, 3.224671290700398e-01,
        2.445134137142996e+00, 3.754408661907416e+00,
    ]
    p_low = 0.02425
    p_high = 1.0 - p_low
    if p < p_low:
        q = math.sqrt(-2.0 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (
            (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
        )
    q = math.sqrt(-2.0 * math.log(1.0 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
        (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
    )


def _chi2_cdf_even(x: float, df_half: int) -> float:
    """CDF of the chi-square distribution with df = 2 * df_half.

    Uses the closed form for even df, computed in log space to remain
    numerically stable for large df and large x. When the log
    complement of the CDF is very negative, returns 1.0 directly to
    avoid precision loss in the subtraction.
    """
    if df_half <= 0:
        return 1.0 if x > 0.0 else 0.0
    if x <= 0.0:
        return 0.0
    half_x = x / 2.0
    if half_x == 0.0:
        return 0.0
    log_terms = [
        j * math.log(half_x) - math.lgamma(j + 1) for j in range(df_half)
    ]
    m = max(log_terms)
    s_norm = sum(math.exp(lt - m) for lt in log_terms)
    log_s = m + math.log(s_norm)
    log_complement = -half_x + log_s
    if log_complement < -50.0:
        return 1.0
    return 1.0 - math.exp(log_complement)


def _chi2_quantile_even(p: float, df_half: int) -> float:
    """Quantile of the chi-square distribution with df = 2 * df_half.

    Combines a Wilson-Hilferty starting point with bisection refinement
    driven by ``_chi2_cdf_even``. Returns 0 when ``df_half <= 0``.
    """
    if df_half <= 0:
        return 0.0
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return float("inf")
    z = _normal_quantile(p)
    nu = 2.0 * df_half
    safe_nu = max(nu, 1.0)
    bracket = (
        1.0
        - 2.0 / (9.0 * safe_nu)
        + z * math.sqrt(2.0 / (9.0 * safe_nu))
    )
    if bracket <= 0.0:
        bracket = max(1e-6, abs(bracket))
    x = nu * bracket ** 3
    x = max(x, 1e-12)
    lo, hi = 1e-12, max(x * 4.0, 1.0)
    while _chi2_cdf_even(hi, df_half) < p:
        hi *= 2.0
        if hi > 1e15:
            break
    for _ in range(200):
        mid = (lo + hi) / 2.0
        if _chi2_cdf_even(mid, df_half) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-10 * max(1.0, hi):
            break
    return (lo + hi) / 2.0
