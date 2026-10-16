"""Game state transitions and the JSON views the UI renders."""
from __future__ import annotations

import json
import sqlite3

from fastapi import HTTPException

from . import db, payouts, settle

STRUCTURE_MAX = 20


def err(status: int, msg: str) -> HTTPException:
    return HTTPException(status_code=status, detail=msg)


def get_game(conn: sqlite3.Connection, gid: int) -> sqlite3.Row:
    g = conn.execute("SELECT * FROM games WHERE id = ?", (gid,)).fetchone()
    if not g:
        raise err(404, "Game not found.")
    return g


def player_rows(conn: sqlite3.Connection, gid: int) -> list[dict]:
    rows = conn.execute(
        "SELECT gp.*, u.display_name AS name, u.username FROM game_players gp "
        "JOIN users u ON u.id = gp.user_id WHERE gp.game_id = ? ORDER BY u.display_name COLLATE NOCASE, u.id",
        (gid,)).fetchall()
    return [dict(r) for r in rows]


def live_places(players: list[dict]) -> None:
    """While running, the k-th player to bust (of N) is in place N-k+1."""
    n = len(players)
    busted = sorted((p for p in players if p["bust_seq"] is not None), key=lambda p: p["bust_seq"])
    for k, p in enumerate(busted):
        p["place"] = n - k
    for p in players:
        p["busted"] = p["bust_seq"] is not None
        if not p["busted"]:
            p["place"] = None


def pot_of(game, players: list[dict]) -> int:
    return game["buy_in"] * sum(p["buy_ins"] for p in players)


def check_structure(bps: list[int], player_count: int) -> None:
    if not bps or len(bps) > STRUCTURE_MAX:
        raise err(400, f"Enter between 1 and {STRUCTURE_MAX} paid places.")
    if any(b <= 0 for b in bps):
        raise err(400, "Every paid place needs a percentage above 0.")
    if sum(bps) != payouts.FULL_BP:
        raise err(400, "Payout percentages must add up to 100%.")
    if any(bps[i] < bps[i + 1] for i in range(len(bps) - 1)):
        raise err(400, "A lower place can't pay more than a higher one.")
    if len(bps) > player_count:
        raise err(400, "There are more paid places than players.")


def regenerate_transfers(conn: sqlite3.Connection, game, players: list[dict]) -> None:
    conn.execute("DELETE FROM transfers WHERE game_id = ?", (game["id"],))
    nets = {p["user_id"]: (p["payout"] or 0) - p["buy_ins"] * game["buy_in"] for p in players}
    for frm, to, amount in settle.settle(nets):
        conn.execute("INSERT INTO transfers(game_id, from_user, to_user, amount) VALUES (?,?,?,?)",
                     (game["id"], frm, to, amount))


def _store_results(conn, game, players: list[dict], kind: str) -> None:
    for p in players:
        conn.execute("UPDATE game_players SET place = ?, deal_bp = ?, base_payout = ?, payout = ? "
                     "WHERE game_id = ? AND user_id = ?",
                     (p["place"], p.get("deal_bp"), p["payout"], p["payout"], game["id"], p["user_id"]))
    conn.execute("UPDATE games SET status = 'finished', finish_kind = ?, round_stack = '[]', finished_at = ? "
                 "WHERE id = ?", (kind, db.now(), game["id"]))
    regenerate_transfers(conn, game, players)


def finish_last_standing(conn: sqlite3.Connection, game) -> None:
    players = player_rows(conn, game["id"])
    live_places(players)
    alive = [p for p in players if not p["busted"]]
    if len(alive) != 1:
        raise err(409, "The game ends when one player is left.")
    alive[0]["place"] = 1
    bps = json.loads(game["payout_bp"])
    bps = bps[: len(players)]
    if sum(bps) != payouts.FULL_BP:  # more paid places than players: unpaid share goes to the winner
        bps[0] += payouts.FULL_BP - sum(bps)
    amounts = payouts.split_pot(pot_of(game, players), bps)
    for p in players:
        p["payout"] = amounts[p["place"] - 1] if p["place"] <= len(amounts) else 0
        p["deal_bp"] = None
    _store_results(conn, game, players, "last_standing")


def apply_deal(conn: sqlite3.Connection, game, shares: dict[int, int]) -> None:
    players = player_rows(conn, game["id"])
    live_places(players)
    ids = {p["user_id"] for p in players}
    if set(shares) != ids:
        raise err(400, "The deal must list every player in the game.")
    if any(v < 0 or v > payouts.FULL_BP for v in shares.values()):
        raise err(400, "Each share must be between 0% and 100%.")
    if sum(shares.values()) != payouts.FULL_BP:
        raise err(400, "Deal percentages must add up to 100%.")
    alive = [p for p in players if not p["busted"]]
    if len(alive) < 2:
        raise err(409, "A deal needs at least two players still in.")
    # Remaining players are ranked by their share of the deal.
    alive.sort(key=lambda p: (-shares[p["user_id"]], p["name"].lower(), p["user_id"]))
    for i, p in enumerate(alive, start=1):
        p["place"] = i
    weights = [shares[p["user_id"]] for p in players]
    amounts = payouts.allocate(pot_of(game, players), weights, payouts.CENT)
    for p, a in zip(players, amounts):
        p["payout"] = a
        p["deal_bp"] = shares[p["user_id"]]
    _store_results(conn, game, players, "deal")


def reopen(conn: sqlite3.Connection, game) -> None:
    conn.execute("UPDATE game_players SET place = NULL, deal_bp = NULL, base_payout = NULL, payout = NULL "
                 "WHERE game_id = ?", (game["id"],))
    conn.execute("DELETE FROM transfers WHERE game_id = ?", (game["id"],))
    conn.execute("UPDATE games SET status = 'running', finish_kind = NULL, round_stack = '[]', "
                 "finished_at = NULL WHERE id = ?", (game["id"],))


def round_action(conn: sqlite3.Connection, game, action: str) -> None:
    players = player_rows(conn, game["id"])
    stack = json.loads(game["round_stack"])
    if action == "revert":
        if not stack:
            raise err(409, "Nothing to revert.")
        prev = {int(k): v for k, v in stack.pop()["prev"].items()}
        for p in players:
            p["payout"] = prev.get(p["user_id"], p["payout"])
    elif action == "more":
        cur = [p["payout"] or 0 for p in players]
        res = payouts.round_more(cur, game["buy_in"], stack[-1]["step"] if stack else None)
        if res is None:
            raise err(409, "The payouts can't be rounded any further.")
        step, new = res
        stack.append({"step": step, "prev": {str(p["user_id"]): p["payout"] for p in players}})
        for p, a in zip(players, new):
            p["payout"] = a
    else:
        raise err(400, "Unknown action.")
    for p in players:
        conn.execute("UPDATE game_players SET payout = ? WHERE game_id = ? AND user_id = ?",
                     (p["payout"], game["id"], p["user_id"]))
    conn.execute("UPDATE games SET round_stack = ? WHERE id = ?", (json.dumps(stack), game["id"]))
    regenerate_transfers(conn, game, players)


def game_view(conn: sqlite3.Connection, game) -> dict:
    players = player_rows(conn, game["id"])
    finished = game["status"] == "finished"
    if not finished:
        live_places(players)
    else:
        for p in players:
            p["busted"] = p["bust_seq"] is not None
    bps = json.loads(game["payout_bp"])
    pot = pot_of(game, players)
    grp = conn.execute("SELECT id, name, currency FROM groups WHERE id = ?", (game["group_id"],)).fetchone()
    out_players = []
    for p in players:
        invested = p["buy_ins"] * game["buy_in"]
        out_players.append({
            "user_id": p["user_id"], "name": p["name"], "buy_ins": p["buy_ins"], "invested": invested,
            "busted": p["busted"], "place": p["place"], "deal_bp": p["deal_bp"],
            "base_payout": p["base_payout"], "payout": p["payout"],
            "net": (p["payout"] - invested) if finished else None,
        })
    view = {
        "id": game["id"], "name": game["name"], "status": game["status"], "finish_kind": game["finish_kind"],
        "group": dict(grp), "buy_in": game["buy_in"], "payout_bp": bps, "pot": pot,
        "created_at": game["created_at"], "finished_at": game["finished_at"],
        "players": out_players, "alive": sum(1 for p in players if not p["busted"]),
    }
    if finished:
        stack = json.loads(game["round_stack"])
        cur = [p["payout"] or 0 for p in players]
        nxt = payouts.round_more(cur, game["buy_in"], stack[-1]["step"] if stack else None)
        view["rounding"] = {
            "step": stack[-1]["step"] if stack else None,
            "can_revert": bool(stack),
            "next_step": nxt[0] if nxt else None,
        }
        names = {p["user_id"]: p["name"] for p in players}
        view["transfers"] = [
            {"id": t["id"], "from": t["from_user"], "from_name": names.get(t["from_user"], "?"),
             "to": t["to_user"], "to_name": names.get(t["to_user"], "?"), "amount": t["amount"],
             "paid": bool(t["paid"])}
            for t in conn.execute("SELECT * FROM transfers WHERE game_id = ? ORDER BY amount DESC, id",
                                  (game["id"],))]
    else:
        n = len(players)
        eff = bps[:n] if n else bps
        if n and sum(eff) != payouts.FULL_BP:
            eff = [eff[0] + payouts.FULL_BP - sum(eff)] + eff[1:]
        view["prizes"] = payouts.split_pot(pot, eff) if pot and eff else [0 for _ in eff]
        view["deal_prefill"] = {str(k): v for k, v in payouts.deal_prefill(players, bps).items()}
    return view


def group_summary(conn: sqlite3.Connection, gid: int) -> dict:
    """Per-player totals over finished games plus what is still owed (unpaid transfers)."""
    stats: dict[int, dict] = {}

    def row(uid: int, name: str) -> dict:
        return stats.setdefault(uid, {"user_id": uid, "name": name, "games": 0, "invested": 0, "won": 0,
                                      "net": 0, "wins": 0, "outstanding": 0})

    for r in conn.execute(
            "SELECT gp.*, g.buy_in, u.display_name AS name FROM game_players gp JOIN games g ON g.id = gp.game_id "
            "JOIN users u ON u.id = gp.user_id WHERE g.group_id = ? AND g.status = 'finished'", (gid,)):
        s = row(r["user_id"], r["name"])
        inv = r["buy_ins"] * r["buy_in"]
        s["games"] += 1
        s["invested"] += inv
        s["won"] += r["payout"] or 0
        s["net"] += (r["payout"] or 0) - inv
        if r["place"] == 1:
            s["wins"] += 1
    for t in conn.execute(
            "SELECT t.*, uf.display_name AS fn, ut.display_name AS tn FROM transfers t "
            "JOIN games g ON g.id = t.game_id JOIN users uf ON uf.id = t.from_user JOIN users ut ON ut.id = t.to_user "
            "WHERE g.group_id = ? AND t.paid = 0", (gid,)):
        row(t["from_user"], t["fn"])["outstanding"] -= t["amount"]
        row(t["to_user"], t["tn"])["outstanding"] += t["amount"]
    names = {uid: s["name"] for uid, s in stats.items()}
    plan = settle.settle({uid: s["outstanding"] for uid, s in stats.items()})
    return {
        "standings": sorted(stats.values(), key=lambda s: (-s["net"], s["name"].lower())),
        "settle_all": [{"from": f, "from_name": names[f], "to": t, "to_name": names[t], "amount": a}
                       for f, t, a in plan],
    }
