#!/usr/bin/env python3
"""Read-only access for ONE trusted reader: the latest mails, ranked.

    GET /api/lesezugang/mails?tage=3&max=20&ungelesen=0&suche=<word>
    Authorization: Bearer <token>

And, behind a SECOND door with a key of its own, one action for the same reader:

    POST /api/lesezugang/loeschen        {"message_id": "<id>"}
    Authorization: Bearer <action token>     or {"message_ids": ["<id>", ...]}

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

🔑 DELETING IS A SECOND DOOR, NOT A WIDER FIRST ONE

The reader may also be allowed to delete a mail on command — and „delete" here
means exactly one thing: move it into the trash, where it can be fetched back.
That is a different power than reading, so it gets a different key
(`python3 lesezugang.py neu-aktion`, stored as SHA-256 in
`lesezugang_aktion.json`). The read key never opens it, the action key never
opens the read door, and neither opens anything of the page. No action key
configured = the action door does not exist (404).

🔴 THE TRASH IS OUT OF REACH, BOTH WAYS

The read door never lists a mail that lies in the trash and never asks the
trash folder for anything. The action door never operates on the trash: a mail
already there is reported as such and left alone. There is no path here that
sets `\\Deleted`, runs `EXPUNGE` or deletes for good — the move is done with
`MOVE` only. A server without `MOVE` (where a move would be COPY + `\\Deleted`
+ `EXPUNGE`) and a mailbox without a trash folder are refused, never served by
a permanent delete. And a mail is only moved after its Message-ID was found
LIVE in the folder; a stale pointer is never trusted.

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
import re
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
AKTION_DATEI = "lesezugang_aktion.json"     # the action key: a file of its own
PFAD = "/api/lesezugang/mails"
AKTION_PFAD = "/api/lesezugang/loeschen"
AKTION_MAX = 25                 # most mails one action request may name
AKTION_KOERPER = 64 * 1024      # largest action request body accepted
KORB_FRIST = 600                # seconds the trash folder's name is remembered
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

FEHLER = {400: "bad request", 401: "unauthorized", 403: "forbidden", 404: "not found",
          413: "too large", 429: "slow down"}

# What the action door may answer per mail — nothing else.
VERSCHOBEN, NICHT_DA, SCHON_IM_KORB, KEIN_KORB, KEIN_MOVE, FEHLGESCHLAGEN = (
    "moved to trash", "not found", "already in trash", "no trash folder",
    "move not supported", "error")

_FEHLVERSUCHE: list = []
_F_SCHLOSS = threading.Lock()
_STAND: dict = {}               # pf id → {"zeit", "mails": {mid: {...}}, "ordner": set}
_S_SCHLOSS = threading.Lock()
_FAEDEN: dict = {}
_KORB: dict = {}                # pf id → (time, trash folder name)


# ── The keys ──────────────────────────────────────────────────────────────────
# Two doors, two files, two hashes. Every helper takes the file it works on, so
# the read key and the action key share code but never a secret.
def _pfad(datei: str = DATEI) -> str:
    return os.path.join(W.STATE, datei)


def _lesen(datei: str = DATEI) -> dict:
    try:
        with open(_pfad(datei), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def an() -> bool:
    return bool(_lesen().get("sha256"))


def an_aktion() -> bool:
    return bool(_lesen(AKTION_DATEI).get("sha256"))


def _anlegen(datei: str) -> str:
    """Make a new token, store only its hash, return it ONCE."""
    import secrets
    wort = secrets.token_urlsafe(32)
    satz = {"sha256": hashlib.sha256(wort.encode()).hexdigest(),
            "angelegt": datetime.now().isoformat(timespec="seconds")}
    ziel = _pfad(datei)
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


def _entfernen(datei: str) -> None:
    try:
        os.remove(_pfad(datei))
    except FileNotFoundError:
        pass


def neu() -> str:
    """The READ key: new token, only its hash stored, returned ONCE."""
    return _anlegen(DATEI)


def aus() -> None:
    _entfernen(DATEI)


def neu_aktion() -> str:
    """The ACTION key: new token, only its hash stored, returned ONCE."""
    return _anlegen(AKTION_DATEI)


def aus_aktion() -> None:
    _entfernen(AKTION_DATEI)


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


def _pruefen(kopf: str, ip: str, datei: str, andere: str) -> int:
    """200 = come in. Otherwise the HTTP code to answer with.

    🔴 A token that also matches the OTHER door's hash opens neither: the two
    keys must stay two, even if somebody copied one file over the other."""
    gespeichert = _lesen(datei).get("sha256") or ""
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
    if wort:
        h = hashlib.sha256(wort.encode()).hexdigest()
        fremd = _lesen(andere).get("sha256") or ""
        if hmac.compare_digest(h, gespeichert) and not (fremd and hmac.compare_digest(h, fremd)):
            return 200
    with _F_SCHLOSS:
        _FEHLVERSUCHE.append(jetzt)
    return 401


def pruefen(kopf: str, ip: str) -> int:
    """The read door: only the read key opens it."""
    return _pruefen(kopf, ip, DATEI, AKTION_DATEI)


def pruefen_aktion(kopf: str, ip: str) -> int:
    """The action door: only the action key opens it. Same network list, same
    brake (one shared count of wrong tokens for both doors)."""
    return _pruefen(kopf, ip, AKTION_DATEI, DATEI)


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


# ── The trash: known, so it can be kept out ───────────────────────────────────
def _korb(pf_id: str, frisch: bool = False) -> str:
    """The folder that plays the trash role in this mailbox, or "".

    Remembered for KORB_FRIST — finding it means listing the folders, and the
    read door must stay fast. The action asks fresh (`frisch=True`) and lets a
    failure through, so it never moves anything on a guess."""
    with _S_SCHLOSS:
        alt = _KORB.get(pf_id)
    # „No trash" is kept only a minute: a trash made later must not stay unseen.
    if not frisch and alt and time.time() - alt[0] < (KORB_FRIST if alt[1] else 60):
        return alt[1]
    try:
        name = KL._rollen_ordner(pf_id, "papierkorb") or ""
    except Exception as e:
        W.log("Lesezugang: Papierkorb nicht bestimmbar: %s" % type(e).__name__)
        if frisch:
            raise
        return alt[1] if alt else ""
    with _S_SCHLOSS:
        _KORB[pf_id] = (time.time(), name)
    return name


def _ist_korb(ordner: str, korb: str) -> bool:
    """The trash role's folder — or, should the role be unknown or set
    elsewhere, a folder that is NAMED like a trash. Keeping one folder too many
    out is harmless; letting the trash in is not."""
    if not ordner:
        return False
    if korb and ordner == korb:
        return True
    letzte = re.split(r"[./]", ordner)[-1]
    return KL.SONDER_NAMEN.get(KL.utf7_dekodieren(letzte).lower()) == "papierkorb"


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
        alle = _koepfe(pid)
        # 🔴 The trash is not the reader's: a mail the watchman filed there is
        #    not listed, and the trash folder is never handed to `_stand`.
        korb = _korb(pid) if alle else ""
        for e in alle:
            if _ist_korb(_ordner_von(e), korb):
                continue
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
            if jetzt_da and _ist_korb(jetzt_da["ordner"], korb):
                continue
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
                # The handle the action door takes — the raw header, as recorded.
                "message_id": mid,
            })
    raus.sort(key=lambda m: m["zeit"], reverse=True)
    raus.sort(key=lambda m: m["rang"])
    return {"ok": True, "fassung": _version(), "stand": datetime.now().astimezone().isoformat(timespec="seconds"),
            "tage": tage, "gesamt": gesamt, "anzahl": min(len(raus), hoechstens),
            "gekuerzt": len(raus) > hoechstens, "mails": raus[:hoechstens]}


# ── The one action: into the trash, and nothing more ─────────────────────────
def _mid_sauber(x) -> str:
    """A Message-ID that may go into a search command: printable ASCII without
    blanks, quotes or backslashes. Anything else is not looked for at all."""
    if not isinstance(x, str):
        return ""
    x = x.strip()
    if not x or len(x) > 250 or any(not (32 < ord(c) < 127) or c in '"\\' for c in x):
        return ""
    return x


def _person(name: str, adresse: str) -> tuple:
    """(name, domain) — never a full address, not even one standing in the name."""
    name = (name or "").strip()
    adresse = (adresse or "").strip().lower()
    if "@" in name:
        name = ""
    return name[:120], (adresse.split("@")[-1][:120] if "@" in adresse else "")


def _finden(k, ordner: str, mid: str) -> list:
    """The UIDs in `ordner` whose Message-ID is `mid` — searched AND confirmed
    by reading the header back. The folder is opened read-only for this."""
    try:
        k.waehle(ordner)
    except RuntimeError:
        return []                       # the folder is gone: not there
    uids = k._suchen([b"HEADER", b"Message-ID", b'"' + mid.encode("ascii") + b'"'])
    if not uids:
        return []
    koepfe = k.koepfe(sorted(uids)[-10:])
    return sorted(u for u, satz in koepfe.items()
                  if (satz.get("message_id") or "").strip() == mid)


def _eins(k, mid: str, eintraege: list, korb: str) -> tuple:
    """One mail → (outcome, source folder, live header or None)."""
    orte = []
    for e in reversed(eintraege):               # where it was seen last comes first
        o = _ordner_von(e)
        if o not in orte:
            orte.append(o)
    if "INBOX" not in orte:
        orte.append("INBOX")                    # moved back by hand
    for o in orte:
        if _ist_korb(o, korb):
            continue                            # never opened, never touched here
        uids = _finden(k, o, mid)
        if not uids:
            continue
        kopf = k.koepfe(uids[:1]).get(uids[0])
        if not korb:
            return KEIN_KORB, o, kopf
        if not k.kann("MOVE"):
            return KEIN_MOVE, o, kopf
        # 🔴 The client's own reversible primitive: `\Seen`, then MOVE into the
        #    trash. `nur_move=True` takes away its COPY + `\Deleted` + EXPUNGE
        #    fallback — this door never sets `\Deleted` and never expunges.
        erg = KL._in_den_korb(k, o, uids, korb, nur_move=True)
        return (VERSCHOBEN if erg.get("ok") and erg.get("n") else FEHLGESCHLAGEN), o, kopf
    if any(_ist_korb(o, korb) for o in orte):
        return SCHON_IM_KORB, korb or orte[0], None
    # Not where it was seen: perhaps an earlier request put it into the trash.
    # Looked at read-only (EXAMINE), only to say so — nothing is done there.
    if korb and _finden(k, korb, mid):
        return SCHON_IM_KORB, korb, None
    return NICHT_DA, "", None


def loeschen(body: dict) -> dict:
    """Move the named mails into the trash — and only that.

    `body`: {"message_id": "<id>"} or {"message_ids": [...]} (at most
    AKTION_MAX). An "ordner" hint is ignored on purpose: where a mail lies is
    found out live, never taken from the caller or from an old pointer.
    🔴 Without a Message-ID confirmed live, nothing is deleted."""
    body = body if isinstance(body, dict) else {}
    roh = body.get("message_ids") if "message_ids" in body else [body.get("message_id")]
    if not isinstance(roh, list):
        roh = [roh]
    gefragt = []
    for x in roh:
        x = x.strip() if isinstance(x, str) else ""
        if x and x not in gefragt:
            gefragt.append(x)
    gekappt = len(gefragt) > AKTION_MAX
    gefragt = gefragt[:AKTION_MAX]
    if not gefragt:
        return {"ok": False, "fehler": "no message_id"}

    # Which mailbox knows each one, and where the watchman saw it.
    wo = {}                                     # mid → (pf id, [entries])
    for pf in W.postfaecher():
        for e in _koepfe(pf["id"]):
            m = ((e.get("kopf") or {}).get("message_id") or "").strip()
            if m in gefragt and wo.get(m, (pf["id"],))[0] == pf["id"]:
                wo.setdefault(m, (pf["id"], []))[1].append(e)
    erg = {}                                    # mid → (outcome, folder, live header)
    for mid in gefragt:
        if not _mid_sauber(mid) or mid not in wo:
            erg[mid] = (NICHT_DA, "", None)
    for pid in sorted({wo[m][0] for m in gefragt if m not in erg}):
        posten = [m for m in gefragt if m not in erg and wo[m][0] == pid]
        try:
            korb = _korb(pid, frisch=True)
            zug = KL._zugang(pid)
            if not zug:
                raise RuntimeError("no mailbox")
            with KL.Briefkasten(zug, schreiben=True) as k:
                if not korb:
                    # The client keeps its folder list for 30 s: a trash made
                    # a moment ago is asked for once more before refusing.
                    korb = next((o["name"] for o in k.baum(frisch=True)
                                 if o["rolle"] == "papierkorb"), "")
                for mid in posten:
                    # One failure must not cost the others.
                    try:
                        erg[mid] = _eins(k, mid, wo[mid][1], korb)
                    except Exception as e:
                        W.log("Lesezugang: Loeschen fehlgeschlagen: %s" % type(e).__name__)
                        erg[mid] = (FEHLGESCHLAGEN, "", None)
        except Exception as e:
            W.log("Lesezugang: Postfach nicht erreichbar: %s" % type(e).__name__)
        for mid in posten:
            erg.setdefault(mid, (FEHLGESCHLAGEN, "", None))

    raus = []
    for mid in gefragt:
        ergebnis, ordner, live = erg[mid]
        k = ((wo.get(mid) or ("", [{}]))[1][-1].get("kopf") or {})
        name, domain = _person(k.get("name") or "", k.get("adresse") or "")
        if live and not (name or domain):
            name, domain = _person(live.get("von") or "", live.get("adresse") or "")
        raus.append({
            "message_id": mid[:250],
            "von_name": name, "von_domain": domain,
            "betreff": ((k.get("betreff") or (live or {}).get("betreff") or ""))[:200],
            "ordner": _anzeige(ordner) if ordner else "",
            "ergebnis": ergebnis,
        })
    n = sum(1 for m in raus if m["ergebnis"] == VERSCHOBEN)
    zaehlung = {}
    for m in raus:
        zaehlung[m["ergebnis"]] = zaehlung.get(m["ergebnis"], 0) + 1
    # The aggregate only: no subject, no sender, no Message-ID, no token.
    W.log("Lesezugang: %d Mail(s) angefragt, %s" % (
        len(raus), ", ".join("%s %d" % (a, b) for a, b in sorted(zaehlung.items()))))
    if n:
        W.chronik("lesezugang_geloescht", n=n, text="%d in den Papierkorb (Lesezugang)" % n)
    return {"ok": True, "fassung": _version(), "verschoben": n, "anzahl": len(raus),
            "gekappt": gekappt, "hoechstens": AKTION_MAX, "mails": raus}


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
    elif befehl == "neu-aktion":
        # 🔑 The same rule for the action key: the token on stdout, alone, once.
        #    Running it again replaces the key (rotation); the old one stops working.
        sys.stdout.write(neu_aktion() + "\n")
        sys.stderr.write("action token (move to trash) created; it is shown only this once\n")
    elif befehl == "aus-aktion":
        aus_aktion()
        sys.stderr.write("action access switched off\n")
    elif befehl == "stand":
        for tuer, datei in (("read", DATEI), ("action", AKTION_DATEI)):
            d = _lesen(datei)
            print("%s: %s" % (tuer, "on since %s" % d.get("angelegt") if d.get("sha256") else "off"))
    else:
        sys.stderr.write("usage: lesezugang.py neu | aus | neu-aktion | aus-aktion | stand\n")
        sys.exit(2)
