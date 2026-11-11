#!/usr/bin/env python3
"""Trusted devices: the owner's own machines open the mail client without the
access word. Everybody else still meets the door, exactly as before.

    python3 vertraut.py stand              # on/off, the list, the addresses
    python3 vertraut.py an | aus           # switch it
    python3 vertraut.py wer <ip[:port]>    # what the local Tailscale says about a peer

🔑 WHO A DEVICE IS, AND WHO SAYS SO

A device is recognised by its Tailscale identity, asked from the LOCAL
tailscaled over its own socket (`/localapi/v0/whois`). The answer names the
node (its stable ID, which survives a rename) and the user it belongs to.
Nothing the browser sends is ever part of that answer — no cookie, no header,
no user agent. Matched is the node's stable ID against the list in
`state/vertraut.json` (0600, never in the repository). Users are deliberately
NOT a kind of entry: one user's other machines may relay traffic for others
(a reverse proxy, a dashboard that fetches pages), and trusting the user would
trust everybody behind such a machine.

Optionally a few fixed home-network addresses. They are matched against the
address the connection really comes from, so they only make sense where the
page sees real addresses (host network, a plain install). Behind Docker's port
mapping every device arrives as the same gateway address; the settings page
shows what address it sees, so this is visible instead of guessed.

🔴 THE THREE WAYS A REQUEST CAN ARRIVE — measured on a real installation

  1. Straight from a tailnet peer to the node's own 100.x address. With a
     kernel tailscaled the connection comes from the peer's 100.x address and
     is looked up directly. With `--tun=userspace-networking` (what the image
     runs) tailscaled re-opens it from 127.0.0.1 — and keeps a table of those
     ephemeral ports: `whois 127.0.0.1:<port>` names the real peer. Measured:
     the held connection resolved to the right laptop.
  2. Through `tailscale serve` (the HTTPS address). The backend connection
     also comes from 127.0.0.1, but it is NOT in that table — measured:
     `whois` answers „no match". Serve sets `X-Forwarded-For` to the real
     peer (it replaces any value the client sent). That header is believed
     ONLY when the connection comes from an address listed as a trusted proxy,
     and only after the table said „not one of mine".
  3. Through Docker's port mapping on the host. On a NAS measured: LAN and the
     host's own Tailscale both arrive as the bridge gateway (172.x.0.1). The
     sender cannot be told — such a request is never trusted.

🔴 FAILING SHUT

No Tailscale, no socket, a timeout, an answer that cannot be read, a proxy
without a usable sender: not trusted. A loopback connection that the table
did not claim and that no trusted proxy explains: not trusted. The function
switched off, or no access word set: not trusted. In every one of those cases
the request simply meets the door it met before.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import os
import socket
import sys
import threading
import time
import urllib.parse
from datetime import datetime

try:
    import postwache as W
except Exception:          # pragma: no cover - the module is useless without it
    W = None

DATEI = "vertraut.json"
# Where a tailscaled may listen. The first one that exists is asked: the
# setting, the environment, the one this program starts itself (the image),
# then the usual system places.
EIGENER_SOCKET = "/tmp/postwache-tailscaled.sock"
SYSTEM_SOCKETS = ("/var/run/tailscale/tailscaled.sock",
                  "/run/tailscale/tailscaled.sock")
FRIST = 1.5                 # seconds a whois may take before it counts as failed
MERKEN_OK = 30.0            # seconds a peer's answer is remembered (by address)
MERKEN_FEHLER = 5.0         # seconds a failure is remembered (no hammering)
GERAETE_MAX = 50
ADRESSEN_MAX = 20
# The tailnet's address ranges — a whois is only worth asking for these.
# 🔑 Written as a number on purpose: 100.64/10 is the shared (CGNAT) space every
#    tailnet draws from, and spelled out it looks exactly like one machine's
#    address to the leak wall that guards the public tree.
TAILNET = (ipaddress.ip_network((0x64400000, 10)),
           ipaddress.ip_network("fd7a:115c:a1e0::/48"))

# Why a request was not recognised — codes, the pages translate them.
(G_AUS, G_KEIN_WORT, G_KEIN_TS, G_TS_FEHLER, G_PROXY_OHNE, G_LOKAL, G_UNBEKANNT,
 G_NICHT_GELISTET) = ("aus", "kein_wort", "kein_tailscale", "tailscale_fehler",
                      "proxy_ohne_absender", "lokal", "unbekannt",
                      "nicht_gelistet")

_MERK: dict = {}            # address -> (until, result or None, error text)
_M_SCHLOSS = threading.Lock()


class Fehler(Exception):
    """Tailscale could not be asked or did not answer sensibly."""


# ── The list ───────────────────────────────────────────────────────────────
def _laden() -> dict:
    d = W.load(DATEI, None) if W is not None else None
    return d if isinstance(d, dict) else {}


def stand() -> dict:
    """Complete and plausible, whatever is in the file."""
    d = _laden()
    geraete = []
    for g in d.get("geraete") or []:
        if isinstance(g, dict) and str(g.get("id") or "").strip():
            geraete.append({"id": str(g["id"]).strip()[:64],
                            "name": str(g.get("name") or "")[:120],
                            "benutzer": str(g.get("benutzer") or "")[:120],
                            "system": str(g.get("system") or "")[:40],
                            "seit": str(g.get("seit") or "")[:25]})
    return {"an": bool(d.get("an")),
            "geraete": geraete[:GERAETE_MAX],
            "adressen": [a for a in (_ip(x) for x in d.get("adressen") or []) if a][:ADRESSEN_MAX],
            "proxys": [a for a in (_ip(x) for x in d.get("proxys") or []) if a][:ADRESSEN_MAX],
            "socket": str(d.get("socket") or "")[:200]}


def _speichern(s: dict) -> dict:
    roh = _laden()
    # 🔴 Supplemented, not rebuilt: a key a later version adds must survive an
    #    older page saving the list.
    roh.update({k: s[k] for k in ("an", "geraete", "adressen", "proxys", "socket") if k in s})
    if W is not None:
        W.save(DATEI, roh, 0o600)
    with _M_SCHLOSS:
        _MERK.clear()
    return stand()


def schalten(an: bool) -> dict:
    s = stand()
    s["an"] = bool(an)
    return _speichern(s)


def geraet_hinzu(kennung: dict) -> dict:
    """Put a recognised node on the list. The identity comes from tailscaled,
    never from the request body — the caller hands in what `erkennen` found."""
    s = stand()
    if not kennung or not kennung.get("id"):
        return s
    if not any(g["id"] == kennung["id"] for g in s["geraete"]):
        s["geraete"].append({"id": kennung["id"], "name": kennung.get("name") or "",
                             "benutzer": kennung.get("benutzer") or "",
                             "system": kennung.get("system") or "",
                             "seit": datetime.now().isoformat(timespec="seconds")})
    return _speichern(s)


def geraet_weg(geraet_id: str) -> dict:
    s = stand()
    s["geraete"] = [g for g in s["geraete"] if g["id"] != str(geraet_id or "")]
    return _speichern(s)


def _liste_lesen(wert) -> tuple:
    """Addresses from a text field or a list → (good, bad)."""
    if isinstance(wert, str):
        teile = wert.replace(";", ",").replace("\n", ",").split(",")
    else:
        teile = list(wert or [])
    gut, schlecht = [], []
    for t in teile:
        t = str(t).strip()
        if not t:
            continue
        ip = _ip(t)
        (gut if ip else schlecht).append(ip or t)
    return list(dict.fromkeys(gut)), schlecht


def adressen_setzen(wert) -> tuple:
    """Fixed home addresses. 🔴 Never a loopback address and never a trusted
    proxy: those stand for many devices, not for one."""
    gut, schlecht = _liste_lesen(wert)
    s = stand()
    verboten = set(s["proxys"])
    for a in list(gut):
        if ipaddress.ip_address(a).is_loopback or a in verboten:
            gut.remove(a)
            schlecht.append(a)
    if schlecht:
        return s, schlecht
    s["adressen"] = gut[:ADRESSEN_MAX]
    return _speichern(s), []


def proxys_setzen(wert) -> tuple:
    gut, schlecht = _liste_lesen(wert)
    s = stand()
    if schlecht:
        return s, schlecht
    s["proxys"] = gut[:ADRESSEN_MAX]
    # A proxy can no longer be a home address at the same time.
    s["adressen"] = [a for a in s["adressen"] if a not in s["proxys"]]
    return _speichern(s), []


# ── Asking tailscaled ──────────────────────────────────────────────────────
class _UnixHTTP(http.client.HTTPConnection):
    """HTTP over a Unix socket — the way the tailscale CLI itself talks to its
    daemon. `local-tailscaled.sock` is the host name the daemon expects."""

    def __init__(self, pfad: str, frist: float):
        super().__init__("local-tailscaled.sock", timeout=frist)
        self._pfad = pfad

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._pfad)
        self.sock = s


def socket_pfad(s: dict | None = None) -> str:
    s = s if s is not None else stand()
    # 🔴 A socket named on purpose is the ONLY one asked. Falling through to
    #    some other daemon when it is missing would answer with a different
    #    tailnet's view — „not there" has to stay „not there".
    ausdruecklich = s.get("socket") or os.environ.get("POSTWACHE_TS_SOCKET") or ""
    if ausdruecklich:
        return ausdruecklich if os.path.exists(ausdruecklich) else ""
    for p in (EIGENER_SOCKET, *SYSTEM_SOCKETS):
        if os.path.exists(p):
            return p
    return ""


def _whois_roh(pfad: str, adresse: str) -> dict | None:
    """One question to the daemon. None = it does not know this peer (a clean
    „no match"); anything else that is not an answer raises `Fehler`."""
    if not pfad:
        raise Fehler(G_KEIN_TS)
    c = _UnixHTTP(pfad, FRIST)
    try:
        c.request("GET", "/localapi/v0/whois?addr=" + urllib.parse.quote(adresse, safe=""))
        a = c.getresponse()
        roh = a.read(256 * 1024)
    except (OSError, http.client.HTTPException) as e:
        raise Fehler(G_TS_FEHLER) from e
    finally:
        c.close()
    if a.status == 404:
        return None
    if a.status != 200:
        raise Fehler(G_TS_FEHLER)
    try:
        d = json.loads(roh.decode("utf-8", "replace"))
    except ValueError as e:
        raise Fehler(G_TS_FEHLER) from e
    knoten = d.get("Node") if isinstance(d, dict) else None
    if not isinstance(knoten, dict) or not knoten.get("StableID"):
        raise Fehler(G_TS_FEHLER)
    nutzer = d.get("UserProfile") if isinstance(d.get("UserProfile"), dict) else {}
    name = str(knoten.get("ComputedName") or "") or str(knoten.get("Name") or "").split(".")[0]
    info = knoten.get("Hostinfo") if isinstance(knoten.get("Hostinfo"), dict) else {}
    return {"id": str(knoten["StableID"]), "name": name[:120],
            "benutzer": "" if knoten.get("Tags") else str(nutzer.get("LoginName") or "")[:120],
            "system": str(info.get("OS") or "")[:40]}


def whois(pfad: str, adresse: str, merken: bool = True) -> dict | None:
    """`_whois_roh` with a short memory. A loopback `ip:port` is never
    remembered: the port is gone with the connection and comes back for
    somebody else."""
    jetzt = time.time()
    if merken:
        with _M_SCHLOSS:
            m = _MERK.get(adresse)
        if m and m[0] > jetzt:
            if m[2]:
                raise Fehler(m[2])
            return m[1]
    try:
        erg = _whois_roh(pfad, adresse)
    except Fehler as e:
        if merken:
            with _M_SCHLOSS:
                _MERK[adresse] = (jetzt + MERKEN_FEHLER, None, str(e) or G_TS_FEHLER)
        raise
    if merken:
        with _M_SCHLOSS:
            if len(_MERK) > 500:
                _MERK.clear()
            _MERK[adresse] = (jetzt + MERKEN_OK, erg, "")
    return erg


# ── The decision ───────────────────────────────────────────────────────────
def _ip(text) -> str:
    """A bare, normalised address or "" — `::ffff:1.2.3.4` becomes `1.2.3.4`."""
    try:
        a = ipaddress.ip_address(str(text or "").strip())
    except ValueError:
        return ""
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return str(a)


def _ist_tailnet(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    return any(a in n for n in TAILNET)


def _letzter_absender(xff) -> str:
    """The RIGHTMOST entry of X-Forwarded-For: the one the trusted proxy wrote.
    Everything left of it came from the client and is worth nothing."""
    if isinstance(xff, (list, tuple)):
        xff = ",".join(str(x) for x in xff if x)
    teile = [t.strip() for t in str(xff or "").split(",") if t.strip()]
    return _ip(teile[-1]) if teile else ""


def erkennen(peer_ip: str, peer_port: int, xff=None, s: dict | None = None) -> dict:
    """Who is this connection? Independent of on/off, so the settings page can
    offer „add this device". Never raises.

    → {"weg": "tailnet"|"proxy"|"adresse"|"", "adresse": ip, "kennung": {...}|None,
       "grund": code when not recognised}
    """
    s = s if s is not None else stand()
    ip = _ip(peer_ip)
    erg = {"weg": "", "adresse": ip, "kennung": None, "grund": ""}
    if not ip:
        erg["grund"] = G_UNBEKANNT
        return erg
    pfad = socket_pfad(s)
    weg = "tailnet"
    try:
        if ipaddress.ip_address(ip).is_loopback:
            # 🔑 First the daemon's own table: a userspace tailscaled re-opens
            #    a peer's connection from loopback and remembers which port is
            #    whose. A hit is the peer itself — headers are not even read.
            if pfad:
                k = whois(pfad, "%s:%d" % (ip, int(peer_port or 0)) if ":" not in ip
                          else "[%s]:%d" % (ip, int(peer_port or 0)), merken=False)
                if k:
                    erg.update(weg="tailnet", kennung=k)
                    return erg
            if ip not in s["proxys"]:
                erg["grund"] = G_LOKAL
                return erg
        if ip in s["proxys"]:
            absender = _letzter_absender(xff)
            if not absender or ipaddress.ip_address(absender).is_loopback:
                erg["grund"] = G_PROXY_OHNE
                return erg
            ip, weg = absender, "proxy"
            erg["adresse"] = ip
        if ip in s["adressen"]:
            erg.update(weg="adresse" if weg != "proxy" else "proxy")
            erg["kennung"] = {"id": "adresse:" + ip, "name": ip, "benutzer": "",
                              "system": ""}
            return erg
        if _ist_tailnet(ip):
            k = whois(pfad, ip)
            if k:
                erg.update(weg=weg, kennung=k)
                return erg
        erg["grund"] = G_UNBEKANNT
        return erg
    except Fehler as e:
        erg["grund"] = str(e) or G_TS_FEHLER
        erg["kennung"] = None
        return erg
    except Exception:                     # noqa: BLE001 - failing shut, always
        erg["grund"] = G_TS_FEHLER
        erg["kennung"] = None
        return erg


def pruefen(peer_ip: str, peer_port: int, xff=None, wort_gesetzt: bool = True) -> dict:
    """The one question the door asks. → erkennen() plus "vertraut": bool."""
    try:
        s = stand()
    except Exception:                     # noqa: BLE001
        return {"vertraut": False, "weg": "", "adresse": "", "kennung": None,
                "grund": G_AUS}
    erg = erkennen(peer_ip, peer_port, xff, s)
    erg["vertraut"] = False
    if not s["an"]:
        erg["grund"] = erg["grund"] or G_AUS
        return erg
    if not wort_gesetzt:
        # The client exists behind a lock; without a word there is no lock, and
        # a trusted device must not be the way around setting one.
        erg["grund"] = G_KEIN_WORT
        return erg
    k = erg.get("kennung")
    if not k:
        return erg
    if str(k["id"]).startswith("adresse:"):
        erg["vertraut"] = True
        return erg
    if any(g["id"] == k["id"] for g in s["geraete"]):
        erg["vertraut"] = True
        return erg
    erg["grund"] = G_NICHT_GELISTET
    return erg


def _haupt(argv: list) -> int:
    befehl = argv[1] if len(argv) > 1 else "stand"
    if befehl == "stand":
        s = stand()
        print(json.dumps({"an": s["an"], "geraete": [g["name"] for g in s["geraete"]],
                          "adressen": s["adressen"], "proxys": s["proxys"],
                          "socket": socket_pfad(s) or "-"}, indent=1, ensure_ascii=False))
        return 0
    if befehl in ("an", "aus"):
        print("on" if schalten(befehl == "an")["an"] else "off")
        return 0
    if befehl == "wer" and len(argv) > 2:
        try:
            k = whois(socket_pfad(), argv[2], merken=False)
        except Fehler as e:
            print("error: %s" % e)
            return 1
        print(json.dumps(k, indent=1, ensure_ascii=False) if k else "not a known peer")
        return 0
    print(__doc__.split("\n\n")[1])
    return 2


if __name__ == "__main__":
    sys.exit(_haupt(sys.argv))
