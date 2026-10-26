# Poker Balance

Self-hosted web app (Docker Compose) for home poker games: track buy-ins and rebuys, pay out the
winners by a percentage structure, make deals, round payouts to friendly numbers, and see who has to
pay whom with the fewest possible payments. Mobile-first and installable as a PWA.

## Features

- **First run:** the first visitor creates the admin account (protected by a one-time setup token
  printed in the container log).
- **Players & groups:** the admin creates players (each gets a temporary password and can sign in
  read-only to follow their games) and groups, and adds players to groups.
- **Games:** per group. Set the buy-in, pick the players and the payout structure (presets such as
  60/30/10 or any custom split). During the game: `+`/`−` buy-ins, rebuys, mark players **Out**
  (finishing places are assigned automatically), undo, add late players.
- **Game end:** when one player is left the pot is paid out by the structure.
- **Deal maker:** when the remaining players agree on a deal, set each player's percentage (or enter
  chip counts for a chip-chop, or press *Even chop*) and see live what everyone gets. *Apply* ends the
  game with those payouts.
- **Round numbers:** *Round* turns e.g. 258 / 142 into 250 / 150; *Round more* goes further
  (200 / 200), *Revert* steps back. The pot total never changes, nobody's prize is wiped out, and the
  step never gets bigger than the smallest prize.
- **Who pays whom:** the fewest transfers possible, preferring round amounts (100s, 50s, 10s...).
  Tick payments off as paid, share the result (WhatsApp etc.) with one tap.
- **WhatsApp results (optional):** connect a [wa_logger](https://github.com/revocx35/wa_logger) API token under
  Account → WhatsApp. Per group choose *Off*, *Post in a WhatsApp group* or *Message each player privately*
  (results plus only their own payments), automatically when a game ends and/or with "Send to WhatsApp" on the game.
- **Balances:** all-time standings per group and a combined "still to pay" list across all games.
- **Security:** argon2id passwords, lockout + rate limiting, optional TOTP 2FA, CSRF protection,
  strict CSP, HTTPS by default. See [ARCHITECTURE.md](ARCHITECTURE.md#threat-model).

## Run it

```sh
mkdir pokerbalance && cd pokerbalance
curl -fsSLO https://raw.githubusercontent.com/revocx35/pokerbalance/main/docker-compose.yml
mkdir data && sudo chown 10001:10001 data   # the container runs as uid 10001
docker compose up -d
docker compose logs pokerbalance | grep "setup token"
```

Open `https://<host>:8443`, accept the self-signed certificate, enter the setup token and create
the admin account.

### Install as an app (PWA)

Phones only install PWAs from a site with a **trusted** certificate. Put the app behind a reverse
proxy with a real certificate (e.g. Nginx Proxy Manager + Let's Encrypt):

- Forward to `https://<host>:8443` (scheme **https**; the proxy doesn't need to verify the
  self-signed certificate, the hop stays encrypted).
- Set `POKER_TRUSTED_PROXIES=<proxy IP>` in `.env` so login rate limits see the real client IP.
- Firewall port 8443 so only the proxy can reach it.
- Turn on **Force SSL** (and HSTS) for the host. The app also redirects `http://` to `https://` itself when the
  proxy is in `POKER_TRUSTED_PROXIES` and sends `X-Forwarded-Proto` - sign-in can't work on an `http://` page because
  the session cookie is `Secure`.

Then open the site on the phone and use *Add to Home screen* / *Install app*.

### Configuration (`.env`, all optional)

| Variable | Default | Meaning |
| --- | --- | --- |
| `POKER_TLS` | `self-signed` | `self-signed` (cert generated in `data/tls`), `custom`, or `off` (plain HTTP, only behind a TLS proxy) |
| `POKER_TLS_CERT` / `POKER_TLS_KEY` | | certificate + key paths for `custom` |
| `POKER_TRUSTED_PROXIES` | (none) | proxy IPs/CIDRs whose `X-Forwarded-For` is trusted, or `private` |
| `POKER_SESSION_IDLE_HOURS` | `168` | sign out after this much inactivity |
| `POKER_SESSION_MAX_DAYS` | `30` | sign out at the latest after this |
| `POKER_DEFAULT_CURRENCY` | `₺` | currency symbol suggested for new groups |
| `POKER_PORT` | `8443` | listen port inside the container |

### Backup

Everything lives in `data/pokerbalance.db` (SQLite). Back up the `data` folder, or online:
`docker compose exec pokerbalance python -c "import sqlite3; s=sqlite3.connect('/data/pokerbalance.db'); s.backup(sqlite3.connect('/tmp/b.db'))"`
then `docker compose cp pokerbalance:/tmp/b.db ./backup.db`.

### Lost admin password

Another admin can reset it under *Players*. With no other admin, reset it in the database:

```sh
docker compose exec pokerbalance python - <<'EOF'
import sqlite3; from app import auth
pw = auth.generate_password()
c = sqlite3.connect('/data/pokerbalance.db')
c.execute("UPDATE users SET password_hash=?, must_change_pw=1, totp_secret=NULL WHERE username='admin'", (auth.hash_password(pw),))
c.execute("DELETE FROM sessions"); c.commit(); print("temporary password:", pw)
EOF
```

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
POKER_DATA_DIR=./data POKER_TLS=off POKER_COOKIE_SECURE=false .venv/bin/python -m app   # http://localhost:8443
```

## License

MIT
