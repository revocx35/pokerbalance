"""Who pays whom: fewest transfers first, then the roundest amounts.

1. Split the players with a non-zero balance into as many zero-sum groups as possible
   (bitmask DP). A group of k people needs exactly k-1 transfers, so the total is
   n - groups, which is the minimum possible.
2. Inside each group, every minimal settlement is a tree, and every tree has a leaf
   whose single transfer settles that person completely. So searching "pick one
   debtor/creditor pair, settle one of them fully, recurse" enumerates all minimal
   settlements; we keep the one whose amounts are roundest (100s > 50s > 10s > 5s > whole).

Large inputs fall back to greedy (largest debtor pays largest creditor).
"""
from __future__ import annotations

from functools import lru_cache

EXACT_GROUPING_MAX = 15   # 2^15 masks: fast enough in pure Python
EXACT_TREE_MAX = 8        # members per group for the exhaustive roundness search
SEARCH_BUDGET = 200_000   # node budget for the tree search before falling back


def roundness(cents: int) -> int:
    """Higher is rounder."""
    for weight, unit in ((60, 100_00), (40, 50_00), (25, 10_00), (15, 5_00), (5, 1_00)):
        if cents % unit == 0:
            return weight
    return 0


def _groups(ids: list[int], bal: dict[int, int]) -> list[list[int]]:
    n = len(ids)
    if n > EXACT_GROUPING_MAX:
        return [ids]
    full = (1 << n) - 1
    sums = [0] * (1 << n)
    for m in range(1, 1 << n):
        low = m & -m
        sums[m] = sums[m ^ low] + bal[ids[low.bit_length() - 1]]
    dp = [0] * (1 << n)
    choice = [0] * (1 << n)
    for m in range(1, 1 << n):
        best, bi = -1, 0
        mm = m
        while mm:
            low = mm & -mm
            mm ^= low
            v = dp[m ^ low]
            if v > best:
                best, bi = v, low
        dp[m] = best + (1 if sums[m] == 0 else 0)
        choice[m] = bi
    groups: list[list[int]] = []
    cur: list[int] = []
    m = full
    while m:
        low = choice[m]
        cur.append(ids[low.bit_length() - 1])
        m ^= low
        if sums[m] == 0:
            groups.append(cur)
            cur = []
    return groups


def _greedy(bal: dict[int, int]) -> list[tuple[int, int, int]]:
    b = dict(bal)
    out = []
    while True:
        debt = [i for i in b if b[i] < 0]
        cred = [i for i in b if b[i] > 0]
        if not debt or not cred:
            return out
        d = min(debt, key=lambda i: (b[i], i))
        c = max(cred, key=lambda i: (b[i], -i))
        amt = min(-b[d], b[c])
        out.append((d, c, amt))
        b[d] += amt
        b[c] -= amt


def _best_tree(members: list[int], bal: dict[int, int]) -> list[tuple[int, int, int]]:
    if len(members) > EXACT_TREE_MAX:
        return _greedy({i: bal[i] for i in members})
    budget = [SEARCH_BUDGET]

    @lru_cache(maxsize=None)
    def solve(state: tuple[tuple[int, int], ...]) -> tuple[int, tuple]:
        budget[0] -= 1
        if budget[0] < 0:
            raise TimeoutError
        live = [(i, v) for i, v in state if v != 0]
        if not live:
            return 0, ()
        best_score, best_plan = -1, ()
        debtors = [x for x in live if x[1] < 0]
        creditors = [x for x in live if x[1] > 0]
        for d, dv in debtors:
            for c, cv in creditors:
                amt = min(-dv, cv)
                nxt = tuple(sorted((i, v + amt if i == d else v - amt if i == c else v)
                                   for i, v in live))
                score, plan = solve(nxt)
                score += roundness(amt)
                if score > best_score:
                    best_score, best_plan = score, ((d, c, amt),) + plan
        return best_score, best_plan

    try:
        return list(solve(tuple(sorted((i, bal[i]) for i in members)))[1])
    except TimeoutError:
        return _greedy({i: bal[i] for i in members})


def settle(balances: dict[int, int]) -> list[tuple[int, int, int]]:
    """balances: user_id -> net cents (positive = is owed money). Must sum to zero.

    Returns (payer, payee, amount_cents) transfers.
    """
    if sum(balances.values()) != 0:
        raise ValueError("balances must sum to zero")
    ids = sorted(i for i, v in balances.items() if v != 0)
    out: list[tuple[int, int, int]] = []
    for group in _groups(ids, balances):
        out.extend(_best_tree(sorted(group), balances))
    out.sort(key=lambda t: (-t[2], t[0], t[1]))
    return out
