# Notes for AI agents

Read `ARCHITECTURE.md` first (design, data model, threat model). `README.md` is the user guide.

## Commands

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q                      # unit + API tests (in-process TestClient)
node --check app/static/app.js                     # JS syntax check (CI does this too)
docker build -t pokerbalance:dev .                 # image
POKER_DATA_DIR=./data POKER_TLS=off POKER_COOKIE_SECURE=false .venv/bin/python -m app   # local dev on http://localhost:8443
python3 tools/make_icons.py                        # regenerate PWA icons (needs rsvg-convert)
```

Browser e2e (not in CI): run the container on a Docker network and drive it with
`mcr.microsoft.com/playwright/python:v1.55.0-noble` (`pip install --break-system-packages playwright==1.55.0`
inside it). Service worker / installability can only be checked over `http://localhost` (run Playwright
with `--network container:<app>` and `POKER_TLS=off POKER_COOKIE_SECURE=false`), not with the
self-signed cert. CDP `Page.getInstallabilityErrors` must return `[]`.

## Conventions

- Money = integer cents, percentages = integer basis points (10000 = 100%). Never floats on the server.
  The JS `allocate()` in `app.js` mirrors `payouts.allocate` for live previews — keep them in sync.
- Pure logic lives in `payouts.py` / `settle.py` with no I/O; state changes in `games.py`; HTTP in `api.py`.
- Every write endpoint: `Admin` dependency, pydantic model with `extra="forbid"` and bounds, `db.tx()`.
- Error messages are user-facing sentences (the UI shows `detail` verbatim).
- UI: no inline JS, no inline `style=` attributes, no `innerHTML` with data — build DOM with `h()`.
  The CSP will break anything else.
- Bump `__version__` in `app/config.py` and `CACHE` in `app/static/sw.js` together on release.
- Releases: tag `vX.Y.Z` → CI pushes `ghcr.io/revocx35/pokerbalance:X.Y.Z`, `X.Y` and `latest` (main);
  also create a GitHub Release entry.

## Gotchas

- SQLite connections are opened per request with `check_same_thread=False`, because FastAPI runs the
  yield-dependency teardown in a different worker thread than the endpoint.
- Tests replace `auth._ph` with cheap argon2 params (conftest) — never do that in app code.
- Don't verify login throttles by looping requests against a running server; use the unit tests
  (fake clock) / in-process client.
- `games.round_stack` holds the previous payouts for Revert; any change to payouts outside rounding
  must reset it (finish/deal/reopen do).

- Sign-in on an `http://` page silently fails (browser drops the `Secure` cookie, login "just refreshes").
  `Guard` 308-redirects when a trusted proxy reports `X-Forwarded-Proto: http`; app.js also jumps to https.
- Native `Element.append()` does NOT flatten arrays (an array of links becomes their hrefs as text).
  Pass arrays through `h()` (which flattens) or spread them: `el.append(...items)`.

## Don't touch / don't commit

- `data/` (database, setup token, TLS key), `.env`, real hostnames/IPs of the deployment.
- Deployment specifics for the maintainer's server are kept out of the repo.
