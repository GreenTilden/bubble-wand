# clawatch-bridge

A thin, injection-safe HTTP bridge that exposes the `dev` tmux group on **fenton**
to the wrist co-pilot (the Wear OS `bubble-watch` app). Each pane in window
`dev:1` is a running `claude` session; the bridge lets the watch list them, read
the recent output, and send a (voice-dictated) reply.

## Run (dev)

```bash
cd ~/clawatch-bridge
python3 -m venv .venv
.venv/bin/pip install fastapi 'uvicorn[standard]'
# Pin a token (recommended) — otherwise one is generated and logged at startup:
CLAWATCH_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
echo "token: $CLAWATCH_TOKEN"
CLAWATCH_TOKEN=$CLAWATCH_TOKEN CLAWATCH_HOST=192.168.0.22 \
  .venv/bin/uvicorn clawatch_bridge.main:app --host 192.168.0.22 --port 8793
```

Must run as the user that owns the `dev` tmux group (so `capture-pane` / `send-keys`
hit the right tmux socket). For always-on, install `systemd/clawatch-bridge.service`
as a user unit and `loginctl enable-linger`.

## API

All `/api` routes require `Authorization: Bearer $CLAWATCH_TOKEN`.

| Method | Path | Notes |
|--------|------|-------|
| GET | `/healthz` | no auth, `{"ok":true}` |
| GET | `/api/threads` | list panes in `dev:1` with derived status |
| GET | `/api/threads/{index}/tail?lines=40&scrollback=false` | recent output |
| POST | `/api/threads/{index}/send` | body `{"text":"...","submit":true}` |

`index` is the integer pane index (1..N). Status is derived from the pane title's
leading glyph: braille spinner → `WORKING`, star (`✳` …) → `NEEDS_INPUT`, else `IDLE`.

## Smoke test

```bash
TOK=...   # the token
BASE=http://192.168.0.22:8793
curl -s $BASE/healthz
curl -s -H "Authorization: Bearer $TOK" $BASE/api/threads | jq
curl -s -H "Authorization: Bearer $TOK" "$BASE/api/threads/1/tail?lines=20" | jq
curl -s -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
  -X POST $BASE/api/threads/1/send -d '{"text":"echo hello from the watch","submit":true}'
```

## Security

- All tmux calls use argv lists (`shell=False`); no shell, no interpolation.
- The pane target is built from an **integer** index and verified against the live
  pane list — no client string reaches the tmux `-t` argument.
- Sends use `send-keys -l -- <text>` (literal) then a **separate** `Enter`, so key
  names / control sequences in dictated text (`C-c`, `Enter`, `;kill-server`) are
  inserted as plain characters, never executed. Newlines are collapsed to spaces.
- Bearer token required on every `/api` route (constant-time compare).
- Bind to the LAN IP (`CLAWATCH_HOST=192.168.0.22`) for MVP. Phase 2 adds a
  Tailscale Funnel for off-LAN/cellular access — the token becomes mandatory and
  the Funnel URL must be treated as a secret.
```
