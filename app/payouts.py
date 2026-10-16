"""Payout math: splitting the pot, deals and the "round numbers" ladder.

All money is integer cents. Percentages are integer basis points (1% = 100 bp,
100% = 10000 bp) so a deal like 33.33 / 33.33 / 33.34 is exact.
"""
from __future__ import annotations

from fractions import Fraction

CENT = 100
FULL_BP = 10_000

# "Nice" rounding steps in whole currency units. The round button walks up this ladder.
NICE_STEPS = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1_000, 2_000, 2_500, 5_000,
              10_000, 20_000, 25_000, 50_000, 100_000, 200_000, 250_000, 500_000, 1_000_000]


def allocate(total: int, weights: list[int], step: int, prefer_low_on_tie: bool = False) -> list[int]:
    """Split `total` proportionally to `weights` into multiples of `step` (largest remainder).

    Every share is a multiple of `step` except that whatever cannot be expressed in
    whole steps (total % step) goes to the largest share, so the sum is always exactly
    `total`. Ties in the remainder go to the earlier index, or to the smaller share when
    `prefer_low_on_tie` (used by rounding so 250/150 -> 200/200 rather than 300/100).
    """
    if total < 0 or step <= 0:
        raise ValueError("total must be >= 0 and step > 0")
    wsum = sum(weights)
    n = len(weights)
    if n == 0:
        return []
    if wsum <= 0:
        raise ValueError("weights must sum to a positive number")
    ideal = [Fraction(total * w, wsum) for w in weights]
    shares = [int(x // step) * step for x in ideal]
    rema = [x - s for x, s in zip(ideal, shares)]
    left = total - sum(shares)
    units = left // step
    if prefer_low_on_tie:
        order = sorted(range(n), key=lambda i: (-rema[i], ideal[i], i))
    else:
        order = sorted(range(n), key=lambda i: (-rema[i], i))
    # Never hand a step to a zero-weight entry.
    order = [i for i in order if weights[i] > 0]
    for i in order[:units]:
        shares[i] += step
    left = total - sum(shares)
    if left:
        biggest = max(range(n), key=lambda i: (ideal[i], -i))
        shares[biggest] += left
    return shares


def split_pot(pot: int, bps: list[int]) -> list[int]:
    """Split the pot by basis points into whole currency units (cents only if unavoidable)."""
    if sum(bps) != FULL_BP:
        raise ValueError("percentages must add up to 100")
    return allocate(pot, bps, CENT)


def first_step(buy_in: int) -> int:
    """First rounding step in cents: the largest nice step <= half a buy-in (at least 1 unit).

    A 100 buy-in starts at 50s, so 258/142 becomes 250/150.
    """
    target = max(CENT, buy_in // 2)
    best = NICE_STEPS[0] * CENT
    for s in NICE_STEPS:
        if s * CENT <= target:
            best = s * CENT
    return best


def round_more(payouts: list[int], buy_in: int, last_step: int | None) -> tuple[int, list[int]] | None:
    """Round the winners' payouts one notch further, keeping the total unchanged.

    `payouts` are the current amounts (zeros are non-winners and stay zero). Returns
    (step, new_payouts) or None if no larger step (at most the smallest prize) changes
    anything without wiping out a winner's whole payout.
    """
    total = sum(payouts)
    if total <= 0 or sum(1 for p in payouts if p > 0) < 1:
        return None
    start = first_step(buy_in) if last_step is None else last_step + 1
    smallest = min(p for p in payouts if p > 0)
    for s in NICE_STEPS:
        step = s * CENT
        if step < start:
            continue
        # Never round in steps bigger than the smallest prize: 300/200/100 must not become 200/200/200.
        if step > smallest:
            return None
        new = allocate(total, payouts, step, prefer_low_on_tie=True)
        if any(p > 0 and q <= 0 for p, q in zip(payouts, new)):
            return None
        if new != payouts:
            return step, new
    return None


def deal_prefill(players: list[dict], payout_bp: list[int]) -> dict[int, int]:
    """Suggested deal: busted players keep their place's percentage, the rest is chopped
    evenly between the players still in. `players` items need user_id, place, busted."""
    shares: dict[int, int] = {}
    alive = [p for p in players if not p["busted"]]
    used = 0
    for p in players:
        if p["busted"] and p["place"] and p["place"] <= len(payout_bp):
            shares[p["user_id"]] = payout_bp[p["place"] - 1]
            used += payout_bp[p["place"] - 1]
        elif p["busted"]:
            shares[p["user_id"]] = 0
    rest = FULL_BP - used
    if alive:
        base, extra = divmod(rest, len(alive))
        for i, p in enumerate(alive):
            shares[p["user_id"]] = base + (1 if i < extra else 0)
    return shares
