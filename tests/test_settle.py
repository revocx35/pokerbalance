import itertools
import random

import pytest

from app.settle import roundness, settle


def apply(bal, transfers):
    b = dict(bal)
    for f, t, a in transfers:
        assert a > 0
        b[f] += a
        b[t] -= a
    return b


def min_transfers_bruteforce(vals):
    """n - (max number of zero-sum groups), by trying all partitions (small n only)."""
    nz = [v for v in vals if v]
    best = 0

    def rec(items, groups):
        nonlocal best
        if not items:
            best = max(best, groups)
            return
        first, rest = items[0], items[1:]
        for r in range(len(rest) + 1):
            for combo in itertools.combinations(range(len(rest)), r):
                if first + sum(rest[i] for i in combo) == 0:
                    rec([x for i, x in enumerate(rest) if i not in combo], groups + 1)

    rec(nz, 0)
    return len(nz) - best


def test_settles_everyone_with_minimum_transfers():
    random.seed(11)
    for _ in range(300):
        n = random.randint(2, 8)
        vals = [random.choice([-3, -2, -1, 0, 1, 2, 3]) * random.choice([50, 100]) * 100 for _ in range(n - 1)]
        vals.append(-sum(vals))
        bal = dict(enumerate(vals))
        tr = settle(bal)
        assert all(v == 0 for v in apply(bal, tr).values())
        assert len(tr) == min_transfers_bruteforce(vals)


def test_prefers_round_amounts():
    # A owes 150, B owes 50; C gets 100, D gets 100. Minimal = 3 transfers either way;
    # the roundest is A->C 100, A->D 50, B->D 50 (or symmetric), never e.g. odd splits.
    bal = {1: -15000, 2: -5000, 3: 10000, 4: 10000}
    tr = settle(bal)
    assert len(tr) == 3
    assert sorted(a for _, _, a in tr) == [5000, 5000, 10000]


def test_pairs_found_first():
    bal = {1: -10000, 2: 10000, 3: -4200, 4: 4200}
    tr = settle(bal)
    assert sorted(tr) == [(1, 2, 10000), (3, 4, 4200)]


def test_large_inputs_fall_back_quickly():
    random.seed(5)
    vals = [random.randrange(-500, 500) * 100 for _ in range(39)]
    vals.append(-sum(vals))
    bal = dict(enumerate(vals))
    tr = settle(bal)
    assert all(v == 0 for v in apply(bal, tr).values())
    assert len(tr) <= len([v for v in vals if v]) - 1


def test_rejects_unbalanced():
    with pytest.raises(ValueError):
        settle({1: 100, 2: -50})


def test_roundness_order():
    assert roundness(10000) > roundness(5000) > roundness(1000) > roundness(500) > roundness(100) > roundness(1)
