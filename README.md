# 🫧 bubble-wand

**The self-hosted bridge behind [Bubbles](https://get.darn-tech.com) — the Wear OS wrist co-pilot for Claude Code.**

> Run your own bridge — your Anthropic key and your code never leave your machine.
> Docs & install: **https://get.darn-tech.com**

Run this on the machine where your Claude Code sessions live. It exposes each
running session to the [Bubbles](#the-bubbles-watch-app) watch app so you can — from
your wrist — see which sessions need you, read a summary, dictate a reply by voice,
and tap through menus. **Your Anthropic key and your code never leave your box.**
Nothing is hosted by anyone else; you bring your own key.

```
   Wear OS watch  ──HTTP(+bearer)──▶  bubble-wand      ──tmux──▶  your Claude Code panes
   (Bubbles app)                      (this repo, on YOUR box)     (session "dev", window 1)
```

---

## Why it's built this way

A "thread" on your wrist *is* a tmux pane running `claude` on your machine. The bridge
reads and writes those panes directly (`tmux capture-pane` / `send-keys`), so **it must
run on the same box as your Claude Code sessions.** That constraint is also the privacy
model: there's no server in the middle, no shared multi-tenant host, and no one else
ever holds your key.

**This is for people who run their own Claude Code sessions in tmux.** It is not a hosted
SaaS and not turnkey for non-developers — and that's deliberate.

---

## Requirements

- **Python 3.11+**
- **tmux**, with your Claude Code sessions running as panes in one window (see
  [Mapping your sessions](#mapping-your-sessions))
- A machine that stays on (homelab box, dev server, always-on laptop)
- Your own **Anthropic API key** (`sk-ant-…`) from <https://console.anthropic.com> →
  API keys — pasted once during setup, stored only on this box

---

## Quick start

```bash
git clone https://github.com/GreenTilden/bubble-wand
cd bubble-wand
./install.sh
```

The installer:
1. creates a virtualenv and installs dependencies (`fastapi`, `uvicorn`, `anthropic`);
2. installs a **systemd `--user` service** (`clawatch-bridge`, bubble-wand's engine) with lingering enabled,
   so it starts on boot and keeps running after you log out;
3. writes a config file **with no secrets in it**; and
4. prints a one-time **setup URL** for your box's LAN address, e.g.

   ```
   ✓ clawatch-bridge is running.
     Finish setup from a phone/laptop on the SAME network as this box:
         http://<your-box-LAN-IP>:8793/setup?t=<one-time-code>
   ```

**Open that URL from a phone or laptop on the same network**, paste your Anthropic key,
and click *Provision this bridge*. The page mints a **bearer token** and shows you the
**URL + token** to enter in the watch app's Settings. Done — pair the watch and go.

> No key is ever typed into a file by hand. The `/setup` page writes it (and the token)
> to a `0600` file on this box and applies it live — no restart needed.

Prefer to run it yourself without systemd? `CLAWATCH_NO_SERVICE=1 ./install.sh` prints the
exact `uvicorn` command instead.

---

## Mapping your sessions

By default the bridge reads panes in the tmux target **`dev:1`** — session named `dev`,
window `1`. Each pane in that window becomes one thread on your wrist:

```bash
tmux new-session -s dev          # a session called "dev"
# in window 1, split into panes and run `claude` in each:
tmux split-window -h             # pane 2
tmux split-window -v             # pane 3, etc.
# run your Claude Code sessions in those panes
```

Point the bridge at wherever your Claude Code panes actually live by setting
`CLAWATCH_TMUX_WINDOW` (e.g. `work:2`). The bridge derives each thread's status —
**working**, **needs input**, or **idle** — from the pane, and surfaces the ones needing
you first.

---

## Configuration

Config is read from the environment (the installer writes it to `clawatch.env`). Secrets
are added by `/setup`, not by hand.

| Variable | Default | What it does |
|----------|---------|--------------|
| `CLAWATCH_HOST` | `0.0.0.0` | Bind address. `0.0.0.0` lets any device on your LAN reach it; set a specific LAN IP to restrict it. |
| `CLAWATCH_PORT` | `8793` | Port to listen on. |
| `CLAWATCH_TMUX_WINDOW` | `dev:1` | The tmux window whose panes are your Claude Code threads. |
| `ANTHROPIC_API_KEY` | *(unset)* | **Your** key. Written by `/setup`. Powers voice-suggestion features; when unset those simply return empty. |
| `CLAWATCH_TOKEN` | *(minted)* | Bearer token required on every `/api` call. Minted by `/setup`; pin it yourself to keep it stable. |
| `CLAWATCH_TAIL_LINES` | `40` | Default number of recent lines returned per thread. |
| `CLAWATCH_SUGGEST_MODEL` | `claude-haiku-4-5-20251001` | Model used for wrist reply suggestions (uses your key). |
| `CLAWATCH_SUGGEST_MAX_TOKENS` | `150` | Cap on suggestion output. |

Restart after editing config: `systemctl --user restart clawatch-bridge`.

---

## Remote access (off your LAN)

The bridge is **LAN-first** — the simplest, safest setup is the watch and the bridge on
the same network. To reach your sessions from cellular or away from home, front the bridge
with a **named [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)**:
one `cloudflared` process on this box publishes a stable hostname with no inbound ports or
firewall changes on your end. The bearer token is mandatory once you do this, and the
tunnel hostname should be treated as a secret.

> Onboarding (`/setup`) is refused over a tunnel or proxy on purpose — you provision on the
> LAN, then expose the already-configured bridge remotely.

---

## Security & privacy

- **Your key stays local.** It is written to a `0600` file on this box by `/setup` and is
  never transmitted anywhere except to Anthropic on your behalf. No telemetry.
- **Injection-safe tmux.** Every tmux call uses an argv list (`shell=False`) — no shell, no
  interpolation. The pane target is built from an **integer index** verified against the
  live pane list, so no client string ever reaches the tmux `-t` argument.
- **Dictated text can't execute.** Sends use `send-keys -l -- <text>` (literal) followed by
  a **separate** Enter, so key names and control sequences in dictated text (`C-c`, `Enter`,
  `;kill-server`) are inserted as plain characters, never run.
- **Auth on everything.** Every `/api` route requires `Authorization: Bearer <token>`
  (constant-time compare). Soft keys (Esc / interrupt / clear / enter) are an allowlist,
  never a raw key name.
- **Onboarding is LAN-locked.** `/setup` works only from loopback/private addresses, only
  with the one-time code, and only while unconfigured; any proxy/tunnel hop or a
  provisioned bridge is rejected.

---

## API reference

All `/api` routes require `Authorization: Bearer <CLAWATCH_TOKEN>`.

| Method | Path | Notes |
|--------|------|-------|
| GET | `/healthz` | Liveness, no auth → `{"ok": true}` |
| GET | `/api/threads` | List panes as threads with derived status |
| GET | `/api/threads/{index}/tail?lines=40&scrollback=false` | Recent output + any parsed menu prompt |
| POST | `/api/threads/{index}/send` | Body `{"text": "...", "submit": true}` — dictated reply |
| POST | `/api/threads/{index}/key` | Body `{"action": "escape\|interrupt\|clear\|enter"}` |
| POST | `/api/threads/{index}/suggest` | Haiku-generated reply suggestions (needs your key; else `[]`) |
| GET | `/api/usage` | Suggestion token/cost tally |
| GET | `/setup` · POST `/api/setup` | One-time LAN onboarding (see above) |

`index` is the integer pane index (`1..N`), verified against the live pane list.

### Smoke test

```bash
TOKEN=...                    # the token from /setup
BASE=http://<your-box-LAN-IP>:8793
curl -s $BASE/healthz
curl -s -H "Authorization: Bearer $TOKEN" $BASE/api/threads | jq
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/api/threads/1/tail?lines=20" | jq
```

---

## Managing the service

```bash
systemctl --user status  clawatch-bridge     # is it running?
systemctl --user restart clawatch-bridge     # after a config change
journalctl --user -u clawatch-bridge -f      # live logs
```

To re-provision with a different key: clear `ANTHROPIC_API_KEY` / `CLAWATCH_TOKEN` from
`clawatch.env` and restart — `/setup` becomes available again.

---

## The Bubbles watch app

`clawatch-bridge` is the server half. The client is **Bubbles**, a Wear OS app that lists
your threads, shows live status, reads the tail, and takes voice replies. Install it on
your watch and enter the URL + token from `/setup` in its Settings. *(The watch app ships
separately.)*

---

## License

[Apache License 2.0](./LICENSE) © 2026 DArnTech LLC.
