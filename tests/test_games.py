from tests.conftest import new_member


def group_with_players(admin, n=4):
    gid = admin.post("/groups", {"name": "Friday", "currency": "₺"})["id"]
    ids = []
    for i in range(n):
        uid = admin.post("/users", {"username": f"p{i}", "display_name": f"P{i}"})["user"]["id"]
        admin.post(f"/groups/{gid}/members", {"user_id": uid})
        ids.append(uid)
    return gid, ids


def test_full_game_last_standing(admin):
    gid, ids = group_with_players(admin, 4)
    game = admin.post(f"/groups/{gid}/games", {"name": "G1", "buy_in": 10000, "payout_bp": [6000, 3000, 1000],
                                               "players": ids})
    gm = f"/games/{game['id']}"
    v = admin.get(gm)
    assert v["status"] == "running" and v["pot"] == 40000 and v["prizes"] == [24000, 12000, 4000]
    v = admin.post(f"{gm}/players/{ids[0]}/rebuy")  # +1 buy-in
    assert v["pot"] == 50000
    v = admin.put(f"{gm}/players/{ids[0]}/buy-ins", {"buy_ins": 2})
    v = admin.post(f"{gm}/players/{ids[3]}/bust")
    assert [p["place"] for p in v["players"] if p["user_id"] == ids[3]] == [4]
    v = admin.post(f"{gm}/players/{ids[3]}/unbust")
    assert v["alive"] == 4
    admin.post(f"{gm}/players/{ids[3]}/bust")
    admin.post(f"{gm}/players/{ids[2]}/bust")
    v = admin.post(f"{gm}/players/{ids[1]}/bust")
    assert v["status"] == "finished" and v["finish_kind"] == "last_standing"
    by = {p["user_id"]: p for p in v["players"]}
    assert by[ids[0]]["place"] == 1 and by[ids[0]]["payout"] == 30000
    assert by[ids[1]]["payout"] == 15000 and by[ids[2]]["payout"] == 5000 and by[ids[3]]["payout"] == 0
    assert sum(p["net"] for p in v["players"]) == 0
    # transfers settle everyone
    bal = {p["user_id"]: p["net"] for p in v["players"]}
    for t in v["transfers"]:
        bal[t["from"]] += t["amount"]
        bal[t["to"]] -= t["amount"]
    assert all(x == 0 for x in bal.values())
    # changes need a reopen
    admin.post(f"{gm}/players/{ids[0]}/rebuy", expect=409)
    v = admin.post(f"{gm}/reopen")
    assert v["status"] == "running" and v["alive"] == 1
    admin.post(f"{gm}/players/{ids[1]}/unbust")
    assert admin.get(gm)["alive"] == 2


def test_deal_and_rounding(admin):
    gid, ids = group_with_players(admin, 4)
    game = admin.post(f"/groups/{gid}/games", {"name": "G", "buy_in": 10000, "payout_bp": [7000, 3000],
                                               "players": ids})
    gm = f"/games/{game['id']}"
    admin.post(f"{gm}/players/{ids[3]}/bust")
    admin.post(f"{gm}/players/{ids[2]}/bust")
    # deal must cover everyone and sum to 100%
    admin.post(f"{gm}/deal", {"shares": {str(ids[0]): 6450, str(ids[1]): 3550}}, expect=400)
    admin.post(f"{gm}/deal", {"shares": {str(ids[0]): 6450, str(ids[1]): 3500, str(ids[2]): 0, str(ids[3]): 0}},
               expect=400)
    v = admin.post(f"{gm}/deal", {"shares": {str(ids[0]): 6450, str(ids[1]): 3550, str(ids[2]): 0,
                                             str(ids[3]): 0}})
    assert v["finish_kind"] == "deal"
    by = {p["user_id"]: p for p in v["players"]}
    assert by[ids[0]]["payout"] == 25800 and by[ids[1]]["payout"] == 14200
    assert by[ids[0]]["place"] == 1 and by[ids[1]]["place"] == 2
    assert v["rounding"] == {"step": None, "can_revert": False, "next_step": 5000}

    v = admin.post(f"{gm}/round", {"action": "more"})
    by = {p["user_id"]: p for p in v["players"]}
    assert (by[ids[0]]["payout"], by[ids[1]]["payout"]) == (25000, 15000)
    assert by[ids[0]]["base_payout"] == 25800
    assert v["rounding"]["step"] == 5000 and v["rounding"]["next_step"] == 10000
    v = admin.post(f"{gm}/round", {"action": "more"})
    by = {p["user_id"]: p for p in v["players"]}
    assert (by[ids[0]]["payout"], by[ids[1]]["payout"]) == (20000, 20000)
    assert v["rounding"]["next_step"] is None
    admin.post(f"{gm}/round", {"action": "more"}, expect=409)
    v = admin.post(f"{gm}/round", {"action": "revert"})
    by = {p["user_id"]: p for p in v["players"]}
    assert (by[ids[0]]["payout"], by[ids[1]]["payout"]) == (25000, 15000)
    v = admin.post(f"{gm}/round", {"action": "revert"})
    by = {p["user_id"]: p for p in v["players"]}
    assert (by[ids[0]]["payout"], by[ids[1]]["payout"]) == (25800, 14200)
    admin.post(f"{gm}/round", {"action": "revert"}, expect=409)


def test_paid_flags_and_group_summary(admin):
    gid, ids = group_with_players(admin, 3)
    game = admin.post(f"/groups/{gid}/games", {"name": "G", "buy_in": 5000, "payout_bp": [10000], "players": ids})
    gm = f"/games/{game['id']}"
    admin.post(f"{gm}/players/{ids[2]}/bust")
    v = admin.post(f"{gm}/players/{ids[1]}/bust")
    assert len(v["transfers"]) == 2
    t = v["transfers"][0]
    v = admin.put(f"{gm}/transfers/{t['id']}", {"paid": True})
    assert [x["paid"] for x in v["transfers"] if x["id"] == t["id"]] == [True]
    g = admin.get(f"/groups/{gid}")
    st = {s["user_id"]: s for s in g["standings"]}
    assert st[ids[0]]["net"] == 10000 and st[ids[0]]["wins"] == 1
    assert st[ids[0]]["outstanding"] == 5000
    assert len(g["settle_all"]) == 1
    admin.post(f"/groups/{gid}/settle-all")
    g = admin.get(f"/groups/{gid}")
    assert g["settle_all"] == [] and g["games"][0]["unpaid"] == 0


def test_validation(admin):
    gid, ids = group_with_players(admin, 3)
    bad = [
        {"name": "G", "buy_in": 100, "payout_bp": [6000, 3000], "players": ids},             # not 100%
        {"name": "G", "buy_in": 100, "payout_bp": [3000, 7000], "players": ids},             # lower pays more
        {"name": "G", "buy_in": 100, "payout_bp": [4000, 3000, 2000, 1000], "players": ids},  # more places than players
        {"name": "G", "buy_in": 0, "payout_bp": [10000], "players": ids},
        {"name": "G", "buy_in": 100, "payout_bp": [10000], "players": [ids[0]]},
        {"name": "G", "buy_in": 100, "payout_bp": [10000], "players": ids, "extra": 1},
    ]
    for b in bad:
        admin.post(f"/groups/{gid}/games", b, expect=400)
    other = admin.post("/users", {"username": "outsider", "display_name": "O"})["user"]["id"]
    admin.post(f"/groups/{gid}/games", {"name": "G", "buy_in": 100, "payout_bp": [10000],
                                        "players": ids[:2] + [other]}, expect=400)


def test_members_see_only_their_groups_read_only(admin):
    gid, ids = group_with_players(admin, 2)
    hidden = admin.post("/groups", {"name": "Secret"})["id"]
    uid, bob = new_member(admin, "bob")
    admin.post(f"/groups/{gid}/members", {"user_id": uid})
    game = admin.post(f"/groups/{gid}/games", {"name": "G", "buy_in": 100, "payout_bp": [10000],
                                               "players": ids + [uid]})
    assert [g["id"] for g in bob.get("/groups")] == [gid]
    bob.get(f"/groups/{hidden}", expect=404)
    bob.get(f"/games/{game['id']}")
    bob.post(f"/games/{game['id']}/players/{uid}/bust", expect=403)
    bob.post(f"/games/{game['id']}/deal", {"shares": {}}, expect=403)


def test_user_with_history_cannot_be_deleted(admin):
    gid, ids = group_with_players(admin, 2)
    admin.post(f"/groups/{gid}/games", {"name": "G", "buy_in": 100, "payout_bp": [10000], "players": ids})
    admin.delete(f"/users/{ids[0]}", expect=409)
    extra = admin.post("/users", {"username": "temp", "display_name": "T"})["user"]["id"]
    admin.delete(f"/users/{extra}")
