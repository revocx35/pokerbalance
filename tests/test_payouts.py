import random

from app import payouts as P


def test_split_pot_whole_units_and_exact_total():
    assert P.split_pot(33300, [6000, 3000, 1000]) == [20000, 10000, 3300]
    assert P.split_pot(100000, [6000, 3000, 1000]) == [60000, 30000, 10000]
    random.seed(3)
    for _ in range(500):
        pot = random.randrange(1, 10**7)
        k = random.randint(1, 6)
        cuts = sorted(random.sample(range(1, 10000), k - 1))
        bps = [b - a for a, b in zip([0] + cuts, cuts + [10000])]
        out = P.split_pot(pot, bps)
        assert sum(out) == pot
        assert all(x >= 0 for x in out)


def test_round_ladder_matches_the_example():
    # 258 / 142 with a 100 buy-in: 250/150, then 200/200, revert restores 250/150.
    step, new = P.round_more([25800, 14200, 0], 10000, None)
    assert (step, new) == (5000, [25000, 15000, 0])
    step2, new2 = P.round_more(new, 10000, step)
    assert (step2, new2) == (10000, [20000, 20000, 0])
    assert P.round_more(new2, 10000, step2) is None


def test_round_step_never_exceeds_smallest_prize():
    step, new = P.round_more([28500, 18700, 12800, 0, 0], 10000, None)
    assert (step, new) == (5000, [30000, 20000, 10000, 0, 0])
    assert P.round_more(new, 10000, step) is None  # 200s would flatten the deal to 200/200/200


def test_round_keeps_total_and_never_zeroes_a_winner():
    random.seed(7)
    for _ in range(300):
        buy = random.choice([2000, 5000, 10000, 25000, 50000])
        pot = buy * random.randint(3, 40)
        k = random.randint(1, 5)
        cuts = sorted(random.sample(range(1, 10000), k - 1))
        bps = sorted([b - a for a, b in zip([0] + cuts, cuts + [10000])], reverse=True)
        cur = P.split_pot(pot, bps) + [0, 0]
        last = None
        while True:
            r = P.round_more(cur, buy, last)
            if r is None:
                break
            step, new = r
            assert sum(new) == pot
            assert all(n > 0 for c, n in zip(cur, new) if c > 0)
            assert new[-2:] == [0, 0]
            assert last is None or step > last
            cur, last = new, step


def test_first_step_tracks_buy_in():
    assert P.first_step(10000) == 5000
    assert P.first_step(5000) == 2500
    assert P.first_step(2000) == 1000
    assert P.first_step(100) == 100


def test_deal_prefill():
    players = [
        {"user_id": 1, "busted": False, "place": None},
        {"user_id": 2, "busted": False, "place": None},
        {"user_id": 3, "busted": True, "place": 3},
        {"user_id": 4, "busted": True, "place": 4},
    ]
    s = P.deal_prefill(players, [6000, 3000, 1000])
    assert s == {1: 4500, 2: 4500, 3: 1000, 4: 0}
    assert sum(s.values()) == 10000
