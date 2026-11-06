"""Statistics: stratified comparison, multiple-testing correction, simple rate tests. No LLM anywhere."""
from __future__ import annotations

import math


def norm_sf(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2))


def cmh(strata: list[tuple[int, int, int, int]]) -> dict:
    """Cochran-Mantel-Haenszel for arm A vs arm B over strata of (nA, sA, nB, sB).

    Only strata where both arms ran contribute, so only concurrent data counts as evidence.
    Returns the MH risk ratio (lift = RR - 1), z, and the sample sizes that actually contributed.
    """
    num = var = rr_n = rr_d = 0.0
    n_a = n_b = s_a = s_b = 0
    for na, sa, nb, sb in strata:
        n = na + nb
        if na == 0 or nb == 0 or n < 2:
            continue
        s = sa + sb
        num += sa - na * s / n
        var += na * nb * s * (n - s) / (n * n * (n - 1))
        rr_n += sa * nb / n
        rr_d += sb * na / n
        n_a += na; n_b += nb; s_a += sa; s_b += sb
    z = num / math.sqrt(var) if var > 0 else 0.0
    rr = rr_n / rr_d if rr_d > 0 else 1.0
    return {"z": round(z, 3), "lift": round(rr - 1, 4), "n_a": n_a, "n_b": n_b,
            "rate_a": round(s_a / n_a, 4) if n_a else None, "rate_b": round(s_b / n_b, 4) if n_b else None,
            "p": 2 * norm_sf(abs(z))}


def bh_select(pvals: list[float], q: float) -> list[bool]:
    """Benjamini-Hochberg: which hypotheses are rejected at false discovery rate q."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    cut = 0
    for rank, i in enumerate(order, 1):
        if pvals[i] <= q * rank / m:
            cut = rank
    out = [False] * m
    for rank, i in enumerate(order, 1):
        if rank <= cut:
            out[i] = True
    return out


def rate_diff_z(k1: int, n1: int, k2: int, n2: int) -> float:
    if n1 == 0 or n2 == 0:
        return 0.0
    p = (k1 + k2) / (n1 + n2)
    if p <= 0 or p >= 1:
        return 0.0
    return (k1 / n1 - k2 / n2) / math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))


def poisson_excess_z(k: int, expected: float) -> float:
    """Excess of an observed count over its expectation, on a variance-stabilised (Anscombe) scale."""
    if expected <= 0:
        return 0.0
    return 2 * (math.sqrt(k + 0.375) - math.sqrt(expected + 0.375))
