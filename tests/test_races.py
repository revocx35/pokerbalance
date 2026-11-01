"""Checks done before the write transaction must be repeated inside it (double taps run concurrently)."""
from app import api
from tests.test_games import group_with_players


def _stale(monkeypatch, game_row, player_row):
    """Make the pre-transaction checks see the state from before the other request committed."""
    real_player = api._player
    calls = {"n": 0}

    def player(conn, game_id, uid):
        calls["n"] += 1
        return player_row if calls["n"] == 1 else real_player(conn, game_id, uid)

    monkeypatch.setattr(api, "_game_for", lambda ctx, gid: game_row)
    monkeypatch.setattr(api, "_player", player)


def test_double_tapped_final_bust_does_not_finish_the_game_twice(admin, monkeypatch):
    gid, ids = group_with_players(admin, 2)
    game = admin.post(f"/groups/{gid}/games", {"name": "G", "buy_in": 1000, "payout_bp": [10000], "players": ids})
    gm = f"/games/{game['id']}"
    conn = api.db.connect()
    stale_game = conn.execute("SELECT * FROM games WHERE id = ?", (game["id"],)).fetchone()
    stale_player = conn.execute("SELECT * FROM game_players WHERE game_id = ? AND user_id = ?",
                                (game["id"], ids[1])).fetchone()
    v = admin.post(f"{gm}/players/{ids[1]}/bust")  # first tap: game over
    assert v["status"] == "finished"
    t = v["transfers"][0]
    admin.put(f"{gm}/transfers/{t['id']}", {"paid": True})

    _stale(monkeypatch, stale_game, stale_player)  # second tap, checked before the first one committed
    admin.post(f"{gm}/players/{ids[1]}/bust", expect=409)
    monkeypatch.undo()
    v = admin.get(gm)
    assert [x["paid"] for x in v["transfers"]] == [True]  # results were not regenerated
    assert {p["user_id"]: p["busted"] for p in v["players"]} == {ids[0]: False, ids[1]: True}
    conn.close()


def test_double_tapped_deal_is_refused(admin, monkeypatch):
    gid, ids = group_with_players(admin, 3)
    game = admin.post(f"/groups/{gid}/games", {"name": "G", "buy_in": 1000, "payout_bp": [10000], "players": ids})
    gm = f"/games/{game['id']}"
    admin.post(f"{gm}/players/{ids[2]}/bust")
    conn = api.db.connect()
    stale_game = conn.execute("SELECT * FROM games WHERE id = ?", (game["id"],)).fetchone()
    shares = {"shares": {str(ids[0]): 5000, str(ids[1]): 5000, str(ids[2]): 0}}
    v = admin.post(f"{gm}/deal", shares)
    admin.put(f"{gm}/transfers/{v['transfers'][0]['id']}", {"paid": True})
    monkeypatch.setattr(api, "_game_for", lambda ctx, g: stale_game)
    admin.post(f"{gm}/deal", shares, expect=409)
    monkeypatch.undo()
    assert admin.get(gm)["transfers"][0]["paid"] is True
    conn.close()


def test_concurrent_round_more_keeps_the_revert_history(admin, monkeypatch):
    gid, ids = group_with_players(admin, 3)
    game = admin.post(f"/groups/{gid}/games", {"name": "G", "buy_in": 1300, "payout_bp": [6000, 4000],
                                               "players": ids})
    gm = f"/games/{game['id']}"
    admin.post(f"{gm}/players/{ids[2]}/bust")
    v = admin.post(f"{gm}/players/{ids[1]}/bust")
    original = {p["user_id"]: p["payout"] for p in v["players"]}
    conn = api.db.connect()
    stale_game = conn.execute("SELECT * FROM games WHERE id = ?", (game["id"],)).fetchone()
    admin.post(f"{gm}/round", {"action": "more"})
    monkeypatch.setattr(api, "_game_for", lambda ctx, g: stale_game)
    r = admin.c.post(f"/api{gm}/round", json={"action": "more"}, headers={"X-CSRF-Token": admin.csrf})
    monkeypatch.undo()
    while admin.get(gm)["rounding"]["can_revert"]:
        admin.post(f"{gm}/round", {"action": "revert"})
    assert {p["user_id"]: p["payout"] for p in admin.get(gm)["players"]} == original, r.text
    conn.close()
