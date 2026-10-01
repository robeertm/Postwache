"""Tailscale from the inside — one field and one button, no sidecar.

🔑 WHY THIS EXISTS
Tailscale was already possible here: a second compose file and a shell script
with an auth key. That assumes a command line, an editor, and somebody who
knows which directory they are standing in. It belongs where everything else
in this program lives: in the settings page.

🔑 WHY IT WORKS AT ALL — measured, not hoped
`tailscaled --tun=userspace-networking` needs neither `NET_ADMIN` nor
`/dev/net/tun`. Measured twice in a bare container on real Docker: once as
root, and once as **uid 10001**, which is what this image steps down to. Both
start cleanly and report „Logged out.", waiting for a key. Without that second
measurement this would have been a guess — the Postwache never runs as root.

🔴 THE LOGIN BELONGS TO THE OWNER
It lives under `$POSTWACHE_HOME/tailscale/` — the mounted directory. Inside the
image it would be gone at the next `docker compose pull`, leaving a dead
machine in the tailnet that somebody has to go and remove by hand.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

SOCKET = "/tmp/postwache-tailscaled.sock"
_schloss = threading.Lock()
_dienst: subprocess.Popen | None = None


def verfuegbar() -> bool:
    """Are the binaries here at all? An older image does not carry them."""
    return bool(shutil.which("tailscaled") and shutil.which("tailscale"))


def _ordner(heim) -> Path:
    p = Path(heim) / "tailscale"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _cli(*args, timeout: float = 20.0) -> tuple[int, str]:
    try:
        r = subprocess.run(["tailscale", "--socket", SOCKET, *args],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
        return r.returncode, (r.stdout or b"").decode("utf-8", "replace").strip()
    except FileNotFoundError:
        return 127, "tailscale is not in this image"
    except subprocess.TimeoutExpired:
        return 124, "tailscale did not answer in time"


def laeuft() -> bool:
    return _dienst is not None and _dienst.poll() is None


def starten(heim) -> bool:
    global _dienst
    with _schloss:
        if laeuft():
            return True
        if not verfuegbar():
            return False
        o = _ordner(heim)
        log = open(o / "tailscaled.log", "ab", buffering=0)
        _dienst = subprocess.Popen(
            ["tailscaled", "--tun=userspace-networking",
             "--state=%s" % (o / "tailscaled.state"),
             "--socket=%s" % SOCKET],
            stdout=log, stderr=log, start_new_session=True)
    # 🔑 „started" is not „answering". Ask it.
    for _ in range(25):
        code, _a = _cli("status", timeout=5)
        if code in (0, 1):                 # 1 = running but logged out
            return True
        time.sleep(0.4)
    return False


def lage(heim) -> dict:
    """Always answerable, even when nothing is running."""
    if not verfuegbar():
        return {"moeglich": False, "laeuft": False, "verbunden": False,
                "adresse": "", "grund": "this image does not carry Tailscale"}
    if not laeuft() and (Path(heim) / "tailscale" / "tailscaled.state").exists():
        starten(heim)
    if not laeuft():
        return {"moeglich": True, "laeuft": False, "verbunden": False,
                "adresse": "", "grund": ""}
    code, aus = _cli("status", "--json")
    if code != 0 or not aus.startswith("{"):
        return {"moeglich": True, "laeuft": True, "verbunden": False,
                "adresse": "", "grund": aus[:300]}
    try:
        d = json.loads(aus)
    except Exception:
        return {"moeglich": True, "laeuft": True, "verbunden": False,
                "adresse": "", "grund": "could not read the status"}
    selbst = d.get("Self") or {}
    return {"moeglich": True, "laeuft": True,
            "verbunden": (d.get("BackendState") == "Running"),
            "zustand": d.get("BackendState") or "",
            "adresse": (selbst.get("DNSName") or "").rstrip("."),
            "grund": ""}


def verbinden(heim, authkey: str, port: int, hostname: str = "postwache") -> dict:
    """Log in and offer the page over HTTPS.

    🔴 `port` is the port the Postwache REALLY listens on. Pointing Tailscale at
    a default instead is how a sibling installation became unreachable behind a
    valid certificate: the name resolves, the certificate is fine, and nobody
    is listening behind it.
    """
    # 🔑 The INPUT first, then the environment. An empty key is an empty key
    #    whatever is installed here; the other way round somebody who forgot
    #    the key gets told about the image instead of about their mistake.
    s = (authkey or "").strip()
    if not s:
        return {"ok": False, "text": "no auth key given"}
    # 🔴 THE KEYS PAGE HAS TWO BUTTONS
    # „Generate auth key…" (Auth keys) and „Generate access token…" (API access
    # tokens). The second is a key for the Tailscale API and cannot log a
    # machine in — `tailscale up` then fails with a message that never mentions
    # the button one line above. They differ at the front:
    #   tskey-auth-…   logging a machine in
    #   tskey-api-…    driving the API
    if s.startswith("tskey-api-"):
        return {"ok": False, "text":
                "that is an API access token, not an auth key. On the Keys page "
                "use the upper button, “Generate auth key…” under "
                "“Auth keys” — not “Generate access "
                "token…”. An auth key starts with tskey-auth-."}
    if not s.startswith("tskey-"):
        return {"ok": False, "text":
                "that does not look like a Tailscale key — they start with "
                "tskey-auth-. On the Keys page: “Generate auth key…” "
                "under “Auth keys”."}
    if not verfuegbar():
        return {"ok": False, "text": "this image does not carry Tailscale"}
    if not starten(heim):
        return {"ok": False, "text": "the Tailscale service did not start"}
    code, aus = _cli("up", "--authkey", s, "--hostname", hostname,
                     "--accept-dns=false", timeout=90)
    if code != 0:
        return {"ok": False, "text": aus[:400] or "tailscale up failed"}
    code, aus = _cli("serve", "--bg", "--https=443",
                     "http://127.0.0.1:%d" % port, timeout=60)
    if code != 0:
        return {"ok": False,
                "text": "joined, but could not publish the page: %s" % aus[:300]}
    l = lage(heim)
    return {"ok": True, "adresse": l.get("adresse", ""),
            "text": "https://%s" % l.get("adresse", "")}


def trennen(heim) -> dict:
    """Log out and take the offer back — the machine disappears again."""
    if not laeuft():
        return {"ok": True}
    _cli("serve", "reset", timeout=30)
    code, aus = _cli("down", timeout=30)
    return {"ok": code == 0, "text": "" if code == 0 else aus[:300]}


def beim_start(heim, port: int) -> None:
    """Carry on by itself after a restart.

    🔴 Without this the login would be saved and still need the button pressed
    after every update — and updates arrive hourly.
    """
    if not verfuegbar():
        return
    if not (Path(heim) / "tailscale" / "tailscaled.state").exists():
        return
    if not starten(heim):
        return
    _cli("serve", "--bg", "--https=443", "http://127.0.0.1:%d" % port, timeout=60)
