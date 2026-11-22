"""Update notice and one-click request — the app's half of the updater.

WHAT THIS FILE DOES, AND WHAT IT NEVER DOES
The page can say "version X is out" and the owner can press one button. That is
all this file does. It never touches Docker: a web page that may tell the Docker
socket what to do is root on the host. The button writes a request FILE into the
data folder; a small companion container (`updater` in docker-compose.yml, script
`updater.sh`) picks it up, checks, backs up, swaps and — if the new version does
not come up healthy — rolls back. The page only reads what the companion reports.

TWO SWITCHES, INDEPENDENT (settings page, or environment)
  * looking for a new version at all   — `update_suche`, env POSTWACHE_UPDATE_CHECK=0
  * the one-click button               — `update_klick`, env POSTWACHE_UPDATE_KLICK=0
With the search off nothing is ever fetched from the network by this file.

FILES under <data>/updater/ (the folder belongs to the service user):
  hinweis.json     what the last search found (version, time, ETag)
  anfrage.json     the owner pressed the button (written here, consumed by the companion)
  status.json      what the companion is doing / did (written by the companion)
  herzschlag       unix time, touched by the companion every few seconds
  gesperrt.json    a version the companion rolled back — never offered again as a one-click
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

# The release feed. Overridable so a bench (or a mirror) can stand in for GitHub.
FEED = "https://api.github.com/repos/der Dienstbenutzer/postwache/releases/latest"
# A search is repeated at most this often. A page refresh must not become a
# request to the release feed.
INTERVALL = 6 * 3600
# The companion is "there" when its heartbeat is younger than this.
HERZSCHLAG_FRIST = 120

_sperre = threading.Lock()
_laeuft = False


def _pfad(base: str, name: str) -> str:
    return os.path.join(base, "updater", name)


def _lesen(pfad: str, default):
    try:
        with open(pfad, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _schreiben(pfad: str, daten) -> None:
    os.makedirs(os.path.dirname(pfad), exist_ok=True)
    tmp = pfad + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(daten, f, ensure_ascii=False)
    os.replace(tmp, pfad)


def zahlen(v: str):
    """'5.32.2' -> (5, 32, 2). Anything that is not a plain release number
    (pre-releases, junk) -> None: such a thing is never offered."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(v or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def neuer(kandidat: str, aktuell: str) -> bool:
    """Forward only. Equal, older or unreadable is never an update."""
    a, b = zahlen(kandidat), zahlen(aktuell)
    return bool(a and b and a > b)


def _einst(base: str) -> dict:
    e = _lesen(os.path.join(base, "state", "einstellungen.json"), {})
    return e if isinstance(e, dict) else {}


def suche_an(base: str) -> bool:
    if os.environ.get("POSTWACHE_UPDATE_CHECK", "1") == "0":
        return False
    return bool(_einst(base).get("update_suche", True))


def klick_an(base: str) -> bool:
    if os.environ.get("POSTWACHE_UPDATE_KLICK", "1") == "0":
        return False
    return bool(_einst(base).get("update_klick", True))


def beiwagen_lebt(base: str, jetzt: float | None = None) -> bool:
    try:
        t = float(open(_pfad(base, "herzschlag")).read().strip())
    except Exception:
        return False
    return ((jetzt if jetzt is not None else time.time()) - t) < HERZSCHLAG_FRIST


def suchen(base: str, aktuell: str, holen=None, jetzt: float | None = None,
           erzwingen: bool = False) -> dict:
    """Ask the release feed once (conditional request, ETag) and remember the
    answer. `holen(url, etag)` -> (status, etag, body-dict) is injectable.
    Returns the stored notice. Never raises: a feed that is down is not an
    error the page should show."""
    jetzt = jetzt if jetzt is not None else time.time()
    alt = _lesen(_pfad(base, "hinweis.json"), {})
    alt = alt if isinstance(alt, dict) else {}
    if not suche_an(base):
        return alt
    if (not erzwingen and alt.get("geprueft")
            and jetzt - float(alt.get("geprueft", 0)) < INTERVALL):
        return alt
    url = os.environ.get("POSTWACHE_UPDATE_URL") or FEED
    holen = holen or _holen
    try:
        status, etag, body = holen(url, alt.get("etag") or "")
    except Exception as e:
        alt["fehler"] = str(e)[:120]
        alt["geprueft"] = jetzt
        _schreiben(_pfad(base, "hinweis.json"), alt)
        return alt
    neu = dict(alt)
    neu.pop("fehler", None)
    neu["geprueft"] = jetzt
    if status == 200 and isinstance(body, dict):
        marke = str(body.get("tag_name") or "")
        if body.get("prerelease") or body.get("draft") or not zahlen(marke):
            neu["version"] = ""
        else:
            neu["version"] = ".".join(str(x) for x in zahlen(marke))
        neu["etag"] = etag or ""
    elif status != 304:
        neu["fehler"] = "HTTP %s" % status
    _schreiben(_pfad(base, "hinweis.json"), neu)
    return neu


def _holen(url: str, etag: str):
    kopf = {"User-Agent": "postwache-update-check", "Accept": "application/vnd.github+json"}
    if etag:
        kopf["If-None-Match"] = etag
    req = urllib.request.Request(url, headers=kopf)
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, r.headers.get("ETag") or "", json.loads(r.read(200000).decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, etag, None
        raise


def im_hintergrund(base: str, aktuell: str) -> None:
    """One search in a thread of its own, at most one at a time — `lage()` is
    asked every 30 s and must never wait for the network."""
    global _laeuft
    if not suche_an(base):
        return
    with _sperre:
        if _laeuft:
            return
        alt = _lesen(_pfad(base, "hinweis.json"), {})
        if (isinstance(alt, dict) and alt.get("geprueft")
                and time.time() - float(alt.get("geprueft", 0)) < INTERVALL):
            return
        _laeuft = True

    def lauf():
        global _laeuft
        try:
            suchen(base, aktuell)
        except Exception:
            pass
        finally:
            _laeuft = False

    threading.Thread(target=lauf, daemon=True).start()


def lage(base: str, aktuell: str) -> dict:
    """What the settings card needs. Reads files only — never blocks, never raises."""
    try:
        im_hintergrund(base, aktuell)
        h = _lesen(_pfad(base, "hinweis.json"), {})
        h = h if isinstance(h, dict) else {}
        s = _lesen(_pfad(base, "status.json"), {})
        s = s if isinstance(s, dict) else {}
        sperr = _lesen(_pfad(base, "gesperrt.json"), {})
        ziel = str(h.get("version") or "") if suche_an(base) else ""
        gibt = bool(ziel) and neuer(ziel, aktuell)
        gesperrt = bool(gibt and isinstance(sperr, dict) and sperr.get("version") == ziel)
        offen = os.path.exists(_pfad(base, "anfrage.json"))
        return {
            "aktuell": aktuell,
            "suche": suche_an(base),
            "klick": klick_an(base),
            "neu": ziel if gibt else "",
            "geprueft": h.get("geprueft") or 0,
            "beiwagen": beiwagen_lebt(base),
            "gesperrt": gesperrt,
            "offen": offen,
            "status": {k: s.get(k) for k in ("phase", "ziel", "ergebnis", "grund", "zeit")} if s else {},
        }
    except Exception as e:
        return {"aktuell": aktuell, "suche": False, "klick": False, "neu": "",
                "beiwagen": False, "gesperrt": False, "offen": False,
                "status": {}, "fehler": str(e)[:120]}


def anfordern(base: str, aktuell: str) -> dict:
    """The button. Returns {"ok": bool, "grund": key-suffix}. The reasons are
    KEYS — this file does not know the page's language."""
    if not klick_an(base):
        return {"ok": False, "grund": "klick_aus"}
    if not suche_an(base):
        return {"ok": False, "grund": "suche_aus"}
    if not beiwagen_lebt(base):
        return {"ok": False, "grund": "kein_beiwagen"}
    h = _lesen(_pfad(base, "hinweis.json"), {})
    ziel = str((h or {}).get("version") or "")
    if not neuer(ziel, aktuell):
        return {"ok": False, "grund": "nichts_neues"}
    if os.path.exists(_pfad(base, "anfrage.json")):
        return {"ok": False, "grund": "schon_angefragt"}
    s = _lesen(_pfad(base, "status.json"), {})
    if isinstance(s, dict) and s.get("phase") in ("pruefen", "sichern", "tauschen", "warten"):
        return {"ok": False, "grund": "laeuft"}
    sperr = _lesen(_pfad(base, "gesperrt.json"), {})
    if isinstance(sperr, dict) and sperr.get("version") == ziel:
        return {"ok": False, "grund": "gesperrt"}
    _schreiben(_pfad(base, "anfrage.json"), {"ziel": ziel, "von": aktuell, "zeit": int(time.time())})
    return {"ok": True, "ziel": ziel}
