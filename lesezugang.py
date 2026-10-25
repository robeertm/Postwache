#!/usr/bin/env python3
"""Read-only access for ONE trusted reader: the latest mails, ranked.

    GET /api/lesezugang/mails?tage=3&max=20&ungelesen=0&suche=<word>
    Authorization: Bearer <token>

Made for a personal assistant or a dashboard that wants to say „three mails
came in, one of them is a bill due on Friday" without being able to do anything
else. What it gets per mail: time, sender name and domain, subject, the
watchman's class and why, a rank, the folder it lies in, an excerpt of at most
300 characters, read or unread. What it never gets: the body, attachments,
addresses other than the sender's domain, or any way to change something.

🔑 WHY IT IS ITS OWN DOOR WITH ITS OWN KEY

The page lock protects a person with a password and a session. A program has
neither, and handing it the page password would hand it every action the page
has — sending, deleting, moving. So this door has a token of its own that opens
exactly this one GET and nothing else. Only its SHA-256 is stored; the token is
printed once when it is made (`python3 lesezugang.py neu`) and never again.
No token configured = the door does not exist (404).

🔑 WHY IT IS FAST WITHOUT ASKING THE SERVER EVERY TIME

Asking the mailbox „what is new" means opening every folder: measured on a real
mailbox with 175 folders, 12–19 s. Far too slow for a spoken answer. But the
watchman already wrote down every mail it has seen — class, reason, subject,
sender and the folder it put it in — in `koepfe.json` (20 ms to read). So the
LIST comes from there, always fresh. Only „read or not" and the excerpt need
the server; they come from a small state that a separate, READ-ONLY connection
refreshes in the background (only the folders the list points at, `EXAMINE`,
`BODY.PEEK`). A mail that is no longer where the watchman put it was deleted or
moved by hand — that is reported as such, not guessed.

🔴 THE NETWORK LIST IS NOT THE LOCK

Behind Docker's port mapping every request arrives from the bridge gateway
(measured: 172.17.0.1 for every client). An allow-list of networks therefore
cannot tell one machine on the LAN from another. It still refuses public
addresses (for an install that is forwarded to the internet by mistake), and it
is a real lock where the container sees real addresses (host network). The
protection is the token.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta

try:
    import postwache as W
except Exception:          # pragma: no cover - the module is useless without it
    W = None
try:
    import klient as KL
except Exception:          # pragma: no cover
    KL = None

DATEI = "lesezugang.json"
PFAD = "/api/lesezugang/mails"
KURZ = 300                      # longest excerpt handed out
STAND_FRIST = 120               # seconds before the read/unread state is refreshed
STAND_WARTEN = 4.0              # how long a first request waits for that state
BREMSE_N, BREMSE_S = 10, 60     # wrong tokens per minute before the door slows down

# class → (rank, importance). Rank 1 is said first. The five alarm classes are
# exactly the ones the watchman reports at once (`ALARM`).
RANG = {
    "sicherheit": (1, "hoch"), "frist": (1, "hoch"),
    "amt": (2, "hoch"), "mensch": (2, "hoch"), "stoerung": (2, "hoch"),
    "unklar": (3, "mittel"),
    "automatisch": (4, "niedrig"), "newsletter": (4, "niedrig"), "werbung": (4, "niedrig"),
}

FEHLER = {401: "unauthorized", 403: "forbidden", 404: "not found", 429: "slow down"}

_FEHLVERSUCHE: list = []
_F_SCHLOSS = threading.Lock()
_STAND: dict = {}               # pf id → {"zeit", "mails": {mid: {...}}, "ordner": set}
_S_SCHLOSS = threading.Lock()
_FAEDEN: dict = {}


# ── The key ───────────────────────────────────────────────────────────────────
def _pfad() -> str:
    return os.path.join(W.STATE, DATEI)


def _lesen() -> dict:
    try:
        with open(_pfad(), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def an() -> bool:
    return bool(_lesen().get("sha256"))


def neu() -> str:
    """Make a new token, store only its hash, return it ONCE."""
    import secrets
    wort = secrets.token_urlsafe(32)
    satz = {"sha256": hashlib.sha256(wort.encode()).hexdigest(),
            "angelegt": datetime.now().isoformat(timespec="seconds")}
    ziel = _pfad()
    os.makedirs(os.path.dirname(ziel), exist_ok=True)
    tmp = ziel + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(satz, fh)
    # 🔴 `docker exec` runs as root, the page runs as the container's own user.
    #    A root-owned 0600 file is a door the page itself cannot open — so the
    #    file gets the owner of the state folder it lives in.
    try:
        st = os.stat(os.path.dirname(ziel))
        if os.geteuid() == 0:
            os.chown(tmp, st.st_uid, st.st_gid)
    except (OSError, AttributeError):
        pass
    os.replace(tmp, ziel)
    return wort


def aus() -> None:
    try:
        os.remove(_pfad())
    except FileNotFoundError:
        pass


def _netze() -> list | None:
    """The networks from `POSTWACHE_LESEZUGANG_NETZE`, or None for the default:
    every address that is NOT public (private ranges, loopback, link-local, the
    shared range Tailscale uses, unique-local IPv6) — `not is_global`, so no
    range has to be written down and none can be forgotten."""
    roh = os.environ.get("POSTWACHE_LESEZUGANG_NETZE", "").strip()
    if not roh:
        return None
    netze = []
    for teil in roh.split(","):
        try:
            netze.append(ipaddress.ip_network(teil.strip(), strict=False))
        except ValueError:
            continue
    return netze


def quelle_erlaubt(ip: str) -> bool:
    try:
        a = ipaddress.ip_address((ip or "").split("%")[0])
    except ValueError:
        return False
    if getattr(a, "ipv4_mapped", None):
        a = a.ipv4_mapped
    netze = _netze()
    if netze is None:
        return not a.is_global
    return any(a in n for n in netze if n.version == a.version)


def pruefen(kopf: str, ip: str) -> int:
    """200 = come in. Otherwise the HTTP code to answer with."""
    gespeichert = _lesen().get("sha256") or ""
    if not gespeichert:
        return 404
    if not quelle_erlaubt(ip):
        return 403
    jetzt = time.time()
    with _F_SCHLOSS:
        _FEHLVERSUCHE[:] = [t for t in _FEHLVERSUCHE if jetzt - t < BREMSE_S]
        if len(_FEHLVERSUCHE) >= BREMSE_N:
            return 429
    kopf = (kopf or "").strip()
    wort = kopf[7:].strip() if kopf[:7].lower() == "bearer " else ""
    if wort and hmac.compare_digest(hashlib.sha256(wort.encode()).hexdigest(), gespeichert):
        return 200
    with _F_SCHLOSS:
        _FEHLVERSUCHE.append(jetzt)
    return 401


# ── What the watchman wrote down ──────────────────────────────────────────────
def _koepfe(pf_id: str) -> list:
    for p in (os.path.join(W.STATE, "pf", pf_id, "koepfe.json"), os.path.join(W.STATE, "koepfe.json")):
        try:
            with open(p, encoding="utf-8") as fh:
                d = json.load(fh)
            if isinstance(d, list):
                return d
        except (OSError, ValueError):
            continue
    return []


def _zeit(e: dict) -> str:
    return (e.get("kopf") or {}).get("datum") or e.get("gesehen") or ""


def _ordner_von(e: dict) -> str:
    return e.get("verschoben_nach") or "INBOX"


def _anzeige(ordner: str) -> str:
    name = KL.utf7_dekodieren(ordner) if KL is not None else ordner
    if name.upper().startswith("INBOX.") or name.upper().startswith("INBOX/"):
        name = name[6:]
    return "Posteingang" if name.upper() == "INBOX" else name.replace(".", "/")


# ── Read or unread: a small state, refreshed in the background ───────────────
def _auffrischen(pf_id: str, ordner: set, seit: datetime) -> None:
    """Ask only the folders the list points at — on a connection of its own that
    CANNOT write (`schreiben=False`: every folder is opened with EXAMINE)."""
    mails, gesehen = {}, set()
    try:
        zug = KL._zugang(pf_id)
        if not zug:
            return
        with KL.Briefkasten(zug, schreiben=False) as k:
            datum = (seit - timedelta(days=1)).strftime("%d-%b-%Y")
            for o in sorted(ordner):
                try:
                    k.waehle(o)
                    uids = sorted(k._suchen([("SINCE " + datum).encode()]))[-150:]
                    koepfe = k.koepfe(uids, auszug=True) if uids else {}
                except Exception as e:
                    W.log("Lesezugang: Ordner %s nicht lesbar: %s" % (o, str(e)[:100]))
                    continue
                gesehen.add(o)
                for satz in koepfe.values():
                    mid = (satz.get("message_id") or "").strip()
                    if mid:
                        mails[mid] = {"gelesen": bool(satz.get("gelesen")), "ordner": o,
                                      "kurz": (satz.get("auszug") or "")[:KURZ]}
    except Exception as e:
        W.log("Lesezugang: Stand nicht auffrischbar: %s" % str(e)[:120])
        return
    finally:
        with _S_SCHLOSS:
            _FAEDEN.pop(pf_id, None)
    with _S_SCHLOSS:
        _STAND[pf_id] = {"zeit": time.time(), "mails": mails, "ordner": gesehen,
                         "seit": seit}


def _stand(pf_id: str, ordner: set, seit: datetime) -> dict:
    """The read/unread state — refreshed in the background when it is old, does
    not cover the folders asked for, or starts later than the window."""
    with _S_SCHLOSS:
        s = _STAND.get(pf_id)
        alt = (s is None or time.time() - s["zeit"] > STAND_FRIST
               or not ordner <= s["ordner"] or seit < s["seit"])
        faden = _FAEDEN.get(pf_id)
        if alt and faden is None:
            faden = threading.Thread(target=_auffrischen, args=(pf_id, set(ordner), seit),
                                     daemon=True, name="lesezugang")
            _FAEDEN[pf_id] = faden
            faden.start()
    if s is None and faden is not None:
        faden.join(STAND_WARTEN)
        with _S_SCHLOSS:
            s = _STAND.get(pf_id)
    return s or {"zeit": 0, "mails": {}, "ordner": set(), "seit": seit}


# ── The answer ────────────────────────────────────────────────────────────────
def _zahl(werte: dict, name: str, lo: int, hi: int, vorgabe: int) -> int:
    try:
        return max(lo, min(int((werte.get(name) or [vorgabe])[0]), hi))
    except (TypeError, ValueError):
        return vorgabe


def mails(abfrage: dict) -> dict:
    """`abfrage` is what `urllib.parse.parse_qs` made of the query."""
    suche = ((abfrage.get("suche") or [""])[0] or "").strip()[:80]
    tage = _zahl(abfrage, "tage", 1, 365 if suche else 14, 90 if suche else 3)
    hoechstens = _zahl(abfrage, "max", 1, 50, 20)
    nur_ungelesen = (abfrage.get("ungelesen") or ["0"])[0] in ("1", "true", "ja")
    grenze = datetime.now().astimezone() - timedelta(days=tage)
    wort = W.normal(suche) if suche else ""
    raus, gesamt = [], 0
    for pf in W.postfaecher():
        pid = pf["id"]
        eintraege = []
        for e in _koepfe(pid):
            try:
                wann = datetime.fromisoformat(_zeit(e))
                if wann.tzinfo is None:
                    wann = wann.astimezone()
            except ValueError:
                continue
            if wann < grenze:
                continue
            k = e.get("kopf") or {}
            if wort and wort not in W.normal(" ".join((k.get("betreff") or "", k.get("name") or "",
                                                         k.get("adresse") or ""))):
                continue
            eintraege.append((wann, e))
        gesamt += len(eintraege)
        if not eintraege:
            continue
        # The state only for what can make it into the answer — the newest first.
        eintraege.sort(key=lambda t: t[0], reverse=True)
        eintraege = eintraege[:300]
        stand = _stand(pid, {_ordner_von(e) for _, e in eintraege}, min(t for t, _ in eintraege))
        for wann, e in eintraege:
            k = e.get("kopf") or {}
            klasse = e.get("klasse") or "unklar"
            rang, wichtig = RANG.get(klasse, (3, "mittel"))
            mid = (k.get("message_id") or "").strip()
            ort = _ordner_von(e)
            jetzt_da = stand["mails"].get(mid) if mid else None
            if jetzt_da:
                gelesen, im_postfach, ort = jetzt_da["gelesen"], True, jetzt_da["ordner"]
            elif ort in stand["ordner"]:
                # Asked, and not there: deleted (the bin marks it read) or moved by hand.
                gelesen, im_postfach = True, False
            else:
                gelesen, im_postfach = None, None
            # Unknown (its folder not asked yet — usually a mail of the last
            # minutes) counts as unread: better once too often than missing.
            if nur_ungelesen and gelesen is True:
                continue
            adresse = (k.get("adresse") or "").lower()
            raus.append({
                "zeit": wann.isoformat(timespec="seconds"),
                "von_name": (k.get("name") or "")[:120],
                "von_domain": adresse.split("@")[-1][:120] if "@" in adresse else "",
                "betreff": (k.get("betreff") or "")[:200],
                "klasse": klasse, "wichtigkeit": wichtig, "rang": rang,
                "grund": (e.get("grund") or "")[:160],
                "ordner": _anzeige(ort),
                "kurz": (jetzt_da or {}).get("kurz") or "",
                "gelesen": gelesen, "im_postfach": im_postfach,
                "phishing": (e.get("phishing") or "")[:160],
            })
    raus.sort(key=lambda m: m["zeit"], reverse=True)
    raus.sort(key=lambda m: m["rang"])
    return {"ok": True, "fassung": _version(), "stand": datetime.now().astimezone().isoformat(timespec="seconds"),
            "tage": tage, "gesamt": gesamt, "anzahl": min(len(raus), hoechstens),
            "gekuerzt": len(raus) > hoechstens, "mails": raus[:hoechstens]}


def _version() -> str:
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION"), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return "?"


# ── Managing the key, from a shell inside the container ──────────────────────
if __name__ == "__main__":
    befehl = (sys.argv[1:] or ["stand"])[0]
    if W is None:
        sys.stderr.write("postwache.py not found next to this file\n")
        sys.exit(2)
    if befehl == "neu":
        # 🔑 stdout carries the token and NOTHING else, so it can be piped
        #    straight into the reader's secret file without ever being shown.
        sys.stdout.write(neu() + "\n")
        sys.stderr.write("read-only token created; it is shown only this once\n")
    elif befehl == "aus":
        aus()
        sys.stderr.write("read-only access switched off\n")
    elif befehl == "stand":
        d = _lesen()
        print("on since %s" % d.get("angelegt") if d.get("sha256") else "off")
    else:
        sys.stderr.write("usage: lesezugang.py neu | aus | stand\n")
        sys.exit(2)
