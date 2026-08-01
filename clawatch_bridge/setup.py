"""Self-serve onboarding for a customer-run bridge instance.

A freshly-installed bridge starts UNCONFIGURED (no ANTHROPIC_API_KEY). It serves
a small LAN-only page at /setup where the owner pastes their OWN Anthropic key.
The bridge then mints a bearer token, persists both to its env file, and applies
them live -- no restart, no hand-editing, no secret ever leaving the box.

Security posture (defense in depth):
  1. a one-time setup_token, minted per boot, required on POST;
  2. the caller must be on loopback/private LAN and not via a proxy/tunnel;
  3. it only works while unconfigured -- a provisioned bridge returns 409.
"""
from __future__ import annotations

import html
import ipaddress
import logging
import os
import secrets
import tempfile

from fastapi import Request

from . import suggest
from .config import settings

log = logging.getLogger("clawatch.setup")

# Any of these headers means the request traversed a proxy/tunnel hop, so the
# reported client address is not the real caller -- disqualify for onboarding.
_PROXY_HEADERS = ("x-forwarded-for", "cf-connecting-ip", "x-real-ip", "forwarded")


def caller_is_local(request: Request) -> bool:
    """True only for a direct connection from loopback/private space. Any proxy
    header (a tunnel / reverse-proxy hop) disqualifies -- onboarding is LAN-only
    unless CLAWATCH_ALLOW_REMOTE_SETUP is explicitly set."""
    if settings.allow_remote_setup:
        return True
    if any(h in request.headers for h in _PROXY_HEADERS):
        return False
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def persist_env(updates: dict[str, str]) -> None:
    """Merge key=value pairs into the env file atomically (temp + rename), 0600.
    Existing lines/comments are preserved; a key is replaced in place or appended."""
    path = settings.env_file
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)

    existing: list[str] = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            existing = f.read().splitlines()

    remaining = dict(updates)
    out: list[str] = []
    for line in existing:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")

    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".clawatch.env.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def provision(api_key: str) -> str:
    """Mint a bearer token, persist it + the key, apply live. Returns the token."""
    token = secrets.token_urlsafe(24)
    persist_env({"ANTHROPIC_API_KEY": api_key, "CLAWATCH_TOKEN": token})
    settings.apply_provision(api_key=api_key, token=token)
    suggest.reset_client()
    log.info("provisioned via /setup: bearer token minted, key persisted to %s", settings.env_file)
    return token


# --- Pages (self-contained, no external assets) ---------------------------- #

def setup_page(prefill_token: str = "") -> str:
    tok = html.escape(prefill_token, quote=True)
    return _PAGE.replace("__TOKEN__", tok)


_PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>\U0001FAE7 Bubbles — Set up your bridge</title>
<style>
  :root { color-scheme: dark; }
  body { margin:0; font:16px/1.5 system-ui,sans-serif; background:#0b1220; color:#e8eef7;
         display:flex; min-height:100vh; align-items:center; justify-content:center; }
  .card { width:min(440px,92vw); background:#141d2e; border:1px solid #24304a; border-radius:16px;
          padding:28px; box-shadow:0 12px 40px #0007; }
  h1 { margin:0 0 4px; font-size:22px; }
  p.sub { margin:0 0 20px; color:#9fb0c8; font-size:14px; }
  label { display:block; margin:16px 0 6px; font-size:13px; color:#9fb0c8; }
  input { width:100%; box-sizing:border-box; padding:11px 12px; border-radius:10px;
          border:1px solid #2c3a58; background:#0b1220; color:#e8eef7; font-size:15px; }
  button { margin-top:22px; width:100%; padding:12px; border:0; border-radius:10px; cursor:pointer;
           background:linear-gradient(90deg,#2dd4bf,#3b82f6); color:#04121f; font-weight:700; font-size:15px; }
  button:disabled { opacity:.55; cursor:default; }
  .msg { margin-top:16px; font-size:14px; }
  .err { color:#fca5a5; }
  .ok  { color:#86efac; }
  code { background:#0b1220; border:1px solid #2c3a58; border-radius:6px; padding:2px 6px;
         font-size:13px; word-break:break-all; display:inline-block; }
  .field { margin-top:10px; }
  .hint { color:#6b7c99; font-size:12px; margin-top:4px; }
</style></head><body>
<div class="card">
  <h1>\U0001FAE7 Set up your bridge</h1>
  <p class="sub">Your Anthropic key stays on this box. Nothing is sent anywhere but Anthropic.</p>
  <form id="f">
    <label for="tok">Setup code</label>
    <input id="tok" value="__TOKEN__" autocomplete="off" spellcheck="false">
    <div class="hint">Shown in the install output / server log at first start.</div>
    <label for="key">Anthropic API key</label>
    <input id="key" type="password" placeholder="sk-ant-..." autocomplete="off" spellcheck="false">
    <div class="hint">From console.anthropic.com → API keys.</div>
    <button id="go" type="submit">Provision this bridge</button>
  </form>
  <div id="msg" class="msg"></div>
</div>
<script>
const f=document.getElementById('f'),msg=document.getElementById('msg'),go=document.getElementById('go');
f.addEventListener('submit',async e=>{
  e.preventDefault(); msg.className='msg'; msg.textContent=''; go.disabled=true; go.textContent='Provisioning…';
  try{
    const r=await fetch('/api/setup',{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({setup_token:document.getElementById('tok').value.trim(),api_key:document.getElementById('key').value.trim()})});
    const d=await r.json().catch(()=>({}));
    if(!r.ok){ msg.className='msg err'; msg.textContent=(d.detail||('Error '+r.status)); go.disabled=false; go.textContent='Provision this bridge'; return; }
    f.style.display='none';
    msg.className='msg ok';
    msg.innerHTML='<b>✓ Done.</b> Enter these in the watch app’s Settings, then pair:'
      +'<div class="field">URL<br><code>'+d.connectUrl+'</code></div>'
      +'<div class="field">Token<br><code>'+d.token+'</code></div>';
  }catch(err){ msg.className='msg err'; msg.textContent='Network error: '+err; go.disabled=false; go.textContent='Provision this bridge'; }
});
</script></body></html>"""


ALREADY_HTML = """<!doctype html><meta charset="utf-8">
<title>Bubbles — already set up</title>
<body style="font:16px system-ui,sans-serif;background:#0b1220;color:#e8eef7;display:flex;
min-height:100vh;align-items:center;justify-content:center;text-align:center">
<div><h1>\U0001FAE7 Already configured</h1>
<p style="color:#9fb0c8">This bridge already has a key. To re-provision, clear its env file and restart.</p></div>
</body>"""


FORBIDDEN_HTML = """<!doctype html><meta charset="utf-8">
<title>Bubbles — LAN only</title>
<body style="font:16px system-ui,sans-serif;background:#0b1220;color:#e8eef7;display:flex;
min-height:100vh;align-items:center;justify-content:center;text-align:center">
<div><h1>\U0001F512 Local network only</h1>
<p style="color:#9fb0c8">Onboarding must be done from a device on the same LAN as the bridge,
not over the internet or a tunnel.</p></div>
</body>"""
