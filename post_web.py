#!/usr/bin/env python3
"""Postwache — overview page (port 8110).

Shows what the watchman has classified and WHY, carries the emergency stop, takes
in the mailbox credentials and undoes any move.

🔴 The page is the only place where credentials are TAKEN IN — it never hands
any out. For the password, `/api/lage` delivers a true/false and nothing else.
Whoever opens the page therefore sees THAT a mailbox is configured, but never
with what.

🔴 The message body is stored nowhere and therefore displayed nowhere here
either. Visible are subject, sender and the reason for the classification —
enough to follow it, not enough to spread the inbox across a web page without a
login.
"""
from __future__ import annotations

import base64
import imaplib
import io
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")
# Where the Postwache lives. In a container that is a mounted directory, on a
# machine the path it grew into — both without a code change.
BASE = os.path.abspath(os.environ.get("POSTWACHE_HOME")
                       or os.path.join(HOME, "scripts", "postwache"))
STATE = os.path.join(BASE, "state")
OUT = os.path.join(BASE, "out")

# 🔴 Program and state are TWO places, even where a grown installation has
# them in the same one. In a container the code sits in /app and the state in a
# mounted /data — look for the page or the version number in the state folder
# and you find nothing there and report "HTTP 500" or "?".
PROG = os.path.dirname(os.path.abspath(__file__))


def neben_dem_programm(name: str) -> str:
    """Next to the program first, otherwise in the state folder. The second place
    is the grown case, where both lie side by side."""
    q = os.path.join(PROG, name)
    return q if os.path.isfile(q) else os.path.join(BASE, name)

DISABLED = os.path.join(BASE, "DISABLED")
ENVFILE = os.path.join(BASE, "ha.env")   # fallback only; konfig.json applies
HA = "http://127.0.0.1:8123"
SCHALTER = "input_boolean.postwache_aktiv"
PORT = int(os.environ.get("POSTWACHE_WEB_PORT", "8110"))

sys.path.insert(0, BASE)
try:
    import postwache as W            # one source for drawers and rules
except Exception:                    # the page must never fail because of the watchman
    W = None
try:
    import klient as KL              # since 5.0.0: the mail client
except Exception:                    # and the watchman page must not fail because of it
    KL = None

# The client is its own page: it needs the whole window, and the watchman page is
# a dashboard with a maximum width. One file each, one job each.
KLIENT_SEITE = "post_klient.html"
# 🔴 And the phone gets its OWN page, not the wide one squeezed. Der Besitzer,
# 2026-09-28: „achte darauf das du je nach bildschirmgröße viel platz hast, baue
# auch eine extraversion für mobile geräte." A phone is a different device: one
# thing at a time, thumbs instead of a mouse, a bar at the bottom because the top
# of a six-inch screen is out of reach.
KLIENT_MOBIL = "post_mobil.html"
ANSICHT_KEKS = "pw_ansicht"
# Whoever says „Mobile" here means a phone. An iPad has the room for the wide
# page — and whoever disagrees switches, and the choice is remembered.
TELEFON = re.compile(r"(iPhone|iPod|Android.*Mobile|Windows Phone|Mobile Safari"
                     r"|Opera Mini|IEMobile)", re.I)
NICHT_TELEFON = re.compile(r"(iPad|Tablet|Silk)", re.I)
# Nobody uploads more than this in one request. Without a ceiling, a single POST
# can eat the memory of a Raspberry Pi.
POST_DECKEL = 48 * 1024 * 1024


def _version() -> str:
    try:
        with open(neben_dem_programm("VERSION"), encoding="utf-8") as fh:
            return fh.read().strip() or "?"
    except OSError:
        return "?"


def txt(schluessel: str, **werte) -> str:
    """A text in the configured language.

    🔴 Without the watchman the KEY comes back, not an empty string: an answer
    saying „a.kein_postfach“ stands out at once. An empty one never does. And it
    is called `txt`, not `t` — a `t` shadows the translation everywhere a loop
    variable is called `t`."""
    return W.txt(schluessel, **werte) if W is not None else schluessel


SCHUBLADEN = (W.SCHUBLADEN if W else {})
ALARM = (W.ALARM if W else ())
LAERM = (W.LAERM if W else ())


# ── Lesen / Schreiben ─────────────────────────────────────────────────────────
def token(key: str = "HA_TOKEN") -> str:
    v = os.environ.get(key)
    if v:
        return v.strip()
    # The same source as the watchman — two opinions about where the credentials
    # lie would mean a page that cannot find the switch which the watchman reads
    # perfectly well.
    datei = ENVFILE
    if W is not None:
        datei = W.konfig()["ha"]["token_datei"] or ENVFILE
    try:
        with open(datei, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln.startswith(key + "="):
                    return ln.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def api(path: str, tok: str, data=None, timeout: int = 15):
    kopf = {"Authorization": "Bearer " + tok}
    roh = None
    if data is not None:
        roh = json.dumps(data).encode("utf-8")
        kopf["Content-Type"] = "application/json"
    basis = (W.konfig()["ha"]["url"] if W is not None else HA) or HA
    req = urllib.request.Request(basis + path, data=roh, headers=kopf)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def lade(pfad, default):
    try:
        with open(pfad, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def st(name, default):
    """Read state — through the watchman, not around it.

    🔴 Since 3.0.0 a mailbox's state lies under `state/pf/<id>/`. The page used
    to have its own flat read function for that; had it kept it, it would see
    empty files everywhere after the move and would display that as „nothing has
    happened yet“. There is now ONE place that knows where a file lies, and it is
    in the watchman.
    """
    if W is not None:
        return W.load(name, default)
    return lade(os.path.join(STATE, name), default)


def pf_waehlen(pf_id=""):
    """Which mailbox the following reads and writes refer to."""
    if W is not None:
        W.pf_waehlen(pf_id or "")


def pf_liste():
    return W.postfaecher() if W is not None else []


def aktives_pf(wunsch=""):
    """Which mailbox the page is currently showing.

    Order: what the request brings › what was chosen last › the first in the list.
    🔴 An unknown wish falls back to the first instead of pointing into nothing —
    otherwise, after deleting a mailbox, you would see an empty page with no
    explanation.
    """
    liste = pf_liste()
    if not liste:
        return ""
    pf_waehlen("")                       # ansicht.json is global
    gemerkt = str((st("ansicht.json", {}) or {}).get("pf") or "")
    for gewuenscht in (str(wunsch or ""), gemerkt):
        for f in liste:
            if f["id"] == gewuenscht:
                return f["id"]
    return liste[0]["id"]


def pf_merken(pf_id):
    pf_waehlen("")
    schreibe("ansicht.json", {"pf": str(pf_id or "")})


def schreibe(name, daten, modus=0o644):
    if W is not None:
        W.save(name, daten, modus)
        return
    os.makedirs(STATE, exist_ok=True)
    tmp = os.path.join(STATE, name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, modus)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(daten, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(STATE, name))


def jsonl(datei, n=300):
    try:
        zeilen = open(os.path.join(OUT, datei), encoding="utf-8").read().splitlines()
    except OSError:
        return []
    raus = []
    for z in zeilen[-n:]:
        try:
            raus.append(json.loads(z))
        except ValueError:
            pass
    return raus


# ── Lage ──────────────────────────────────────────────────────────────────────
def schalter_zustand():
    tok = token()
    if not tok:
        return None
    try:
        schalter = (W.konfig()["ha"]["schalter"] if W is not None else SCHALTER)
        if not schalter:
            return None
        s = api("/api/states/" + schalter, tok)
        return s.get("state") if isinstance(s, dict) else None
    except Exception:
        return None


def seite_mit_sprache(html: str) -> str:
    """Put the text table into the page before it is delivered.

    🔴 Not via a second request: the page would otherwise stand there in keys for
    the blink of an eye („kopf.titel“ instead of „Postwache“), and that blink is
    exactly what you see on a phone. The table is in the first byte of the
    response.
    """
    if W is None:
        return html
    code = W.sprache()
    kopf = ("const T = %s;\nconst LANG = %s;\nconst SPRACHEN = %s;"
            % (json.dumps(W.alle_texte(code), ensure_ascii=False),
               json.dumps(code),
               json.dumps([[c, W.SPRACHNAMEN[c]] for c in W.SPRACHEN])))
    html = html.replace("/*SPRACHE*/", kopf, 1)
    return html.replace('<html lang="de"', '<html lang="%s"' % code, 1)


def lage():
    gewaehlt = aktives_pf()
    pf_waehlen(gewaehlt)
    status = lade(os.path.join(OUT, "status.json"), {})
    lauf = st("lauf.json", {})
    zug = W.zugang() if W else {}
    einst = W.einstellungen() if W else {}
    koepfe = st("koepfe.json", [])
    if not isinstance(koepfe, list):
        koepfe = []
    zaehler = st("zaehler.json", {})
    tag = zaehler.get("heute") if isinstance(zaehler.get("heute"), dict) else {}

    # How much WOULD be sorted out — the number he measures the learning run by.
    wuerde = sum(1 for e in koepfe[-400:] if e.get("wuerde_nach"))
    journal = [j for j in jsonl("journal.jsonl", 400) if not j.get("zurueck")]

    return {
        "version": _version(),
        "status": status,
        # Which mailboxes there are and which one is currently shown. Without
        # passwords — those never leave the 0600 file.
        "postfaecher": [{"id": f["id"], "name": f["name"], "adresse": f["adresse"],
                         "server": f["server"], "port": f["port"], "an": f["an"],
                         "passwort_gesetzt": bool(f["passwort"])}
                        for f in pf_liste()],
        "pf": gewaehlt,
        # `historie` stays out: 40 timestamps the page builds nothing from — the
        # interval has already been computed from them (`takt_s`).
        "lauf": {k: v for k, v in lauf.items()
                 if k not in ("fehler", "historie")} | {
            "fehler": str(lauf.get("fehler") or "")},
        "eingerichtet": bool(zug.get("adresse") and zug.get("passwort")),
        "adresse": str(zug.get("adresse") or ""),
        "server": str(zug.get("server") or ""),
        # 🔴 Only whether, never what.
        "passwort_gesetzt": bool(zug.get("passwort")),
        "scharf": bool(einst.get("scharf")),
        "telegram": bool(einst.get("telegram", True)),
        "tg": tg_lage(),
        "bericht_stunde": int(einst.get("bericht_stunde", 7)),
        "regeln": einst.get("regeln") or {},
        "wichtiges_bleibt": bool(einst.get("wichtiges_bleibt")),
        "ablage": ablage_kurz(),
        "absender_regeln": einst.get("absender_regeln") or {},
        "statistik": st("statistik.json", {}),
        # 🔑 Only the short version from `out/dokumente.json` — the index itself
        # has tens of thousands of entries and has no business on a page that
        # fetches itself every 30 s. Searching goes through /api/dokumente.
        "dokumente": lade(os.path.join(OUT, "dokumente.json"), {}),
        "docusort": ds_kurz(),
        "ki": ki_kurz(),
        "vorschlaege": (vorschlaege_lesen().get("liste") or [])[:40],
        "konfig": konfig_kurz(),
        "heimatlos": heimatlose(),
        "auftraege": auftraege_zeigen(),
        # 🔴 Not SCHUBLADEN directly: the names in it are the German fallback.
        # Translation happens at ONE place, in the watchman.
        "schubladen": {k: {"name": v["name"], "icon": v["icon"],
                           "alarm": k in ALARM, "laerm": k in LAERM,
                           "verschiebbar": k in LAERM}
                       for k, v in (W.schubladen_namen() if W else SCHUBLADEN).items()},
        "heute": tag,
        "heute_gesamt": sum(int(v) for v in tag.values()),
        "wuerde_aussortieren": wuerde,
        "notaus_datei": os.path.exists(DISABLED),
        "schalter": schalter_zustand(),
        "mails": list(reversed(koepfe[-120:])),
        "journal": list(reversed(journal))[:80],
        "chronik": list(reversed(jsonl("chronik.jsonl", 120))),
    }


def ablage_kurz() -> dict:
    """What the watchman has learned from his folders — in short form.

    The complete map has a few hundred entries; what belongs on the page is only
    what anyone actually reads: how much was learned, which folders exist, and
    where the filing is ragged.
    """
    k = st("ablage.json", {}) or {}
    ordner = k.get("ordner") or {}
    schw = k.get("schwaechen") or {}
    return {
        "gelernt": k.get("gelernt") or "",
        "mails": int(k.get("mails") or 0),
        "deckung": k.get("deckung") or 0,
        "absender": len(k.get("absender") or {}),
        "domains": len(k.get("domain") or {}) + len(k.get("haupt") or {}),
        # Folders with counts, biggest first — that is the list of categories.
        "ordner": sorted(ordner.items(), key=lambda kv: -kv[1]),
        "schwaechen": {
            "uneindeutig": (schw.get("uneindeutige_absender") or [])[:12],
            "uneindeutig_gesamt": len(schw.get("uneindeutige_absender") or []),
            "fast_leer": schw.get("fast_leere_ordner") or [],
            "leer": schw.get("leere_ordner") or [],
        },
    }


def auftraege_zeigen() -> dict:
    """What the Postwache has lying in the workshop — with state and age.

    Der Besitzer, 2026-09-27: he wants to be able to follow 100 % of what happens in the
    Postwache. That is exactly what this card is for. 🔴 It appears ONLY when a
    workshop is configured — for any other user of the Postwache it would be an
    empty card about a thing they do not have.
    """
    if not W or not W.konfig().get("werkstatt"):
        return {"an": False, "liste": []}
    liste = []
    for a in sorted(W.auftraege_lesen(), key=lambda x: -int(x.get("nr") or 0))[:12]:
        alter = 0
        try:
            alter = (datetime.now()
                     - datetime.fromisoformat(a["angelegt"])).total_seconds()
        except Exception:
            pass
        zustand = str(a.get("zustand") or "angelegt")
        liste.append({
            "nr": int(a.get("nr") or 0), "titel": a.get("titel") or "",
            "zustand": zustand, "alter": int(alter),
            "gestartet": bool(a.get("gestartet")),
            # 🔴 „running“ is not stuck. Stuck means: the deadline has passed and
            #    it has NEVER started.
            "haengt": bool(alter > W.AUFTRAG_FRIST and not a.get("gestartet")
                           and zustand not in ("done", "cancelled")),
        })
    offen = [a for a in liste if a["zustand"] not in ("done", "cancelled")]
    return {"an": True, "liste": liste, "offen": len(offen),
            "haengen": len([a for a in offen if a["haengt"]]),
            "sperre": len(offen) >= W.MAX_OFFENE_AUFTRAEGE}


def heimatlose(grenze: int = 2) -> list:
    """Senders who write again and again and for whom there is NO target.

    🔑 This is the real answer to „the bot has not sorted anything at all“.
    Measured 2026-09-12 against the real inbox: of 22 mails only 2 failed at a
    threshold — for 20 there was simply nothing the watchman could have gone by.
    It can only IMITATE, and where he has never filed anything there is nothing to
    imitate.

    A suggestion is not an action: all that stands here is who keeps arriving
    without a home. The decision takes one click.
    """
    if not W:
        return []
    karte = st("ablage.json", {}) or {}
    einst = W.einstellungen()
    eigene = (einst.get("absender_regeln") or {})
    prof = st("absender.json", {}) or {}
    raus = []
    for adr, e in prof.items():
        if not isinstance(e, dict) or adr in eigene:
            continue
        n = int(e.get("n") or 0)
        if n < grenze:
            continue
        ziel, warum, sicher, darf = W.ziel_finden(karte, adr)
        if ziel and darf:
            continue                       # has a home, all good
        klassen = e.get("klassen") if isinstance(e.get("klassen"), dict) else {}
        haupt = max(klassen.items(), key=lambda kv: kv[1])[0] if klassen else ""
        raus.append({
            "adresse": adr,
            "name": str(e.get("name") or ""),
            "anzahl": n,
            "zuletzt": str(e.get("zuletzt") or ""),
            "klasse": haupt,
            # When a stage SUGGESTS something but may not act, the suggestion
            # belongs here — one click makes it permanent.
            "vorschlag": ziel or "",
            "vorschlag_grund": warum if ziel else "",
            "neuer_ordner": ordnername_vorschlagen(adr, karte),
        })
    return sorted(raus, key=lambda e: -e["anzahl"])[:25]


def ordnername_vorschlagen(adresse: str, karte: dict) -> str:
    """A folder name from the address — as a pre-filled text box, not as a
    decision. He overwrites it when he wants something else.

    What is taken is the main level of the domain (`autogruppe` from
    `k.ivanov@autogruppe.example`), because that names the sender and not the
    individual person behind it.
    """
    dom = (adresse or "").partition("@")[2]
    teile = [t for t in dom.split(".") if t]
    kern = teile[-2] if len(teile) >= 2 else (teile[0] if teile else "")
    kern = re.sub(r"[^A-Za-z0-9 -]", "", kern).strip()
    if not kern:
        return ""
    name = kern[:1].upper() + kern[1:]
    # Under „Shopping“ he keeps everything bought — slotting it in there is
    # closer to his habit than a new folder at the top level.
    ordner = karte.get("ordner") or {}
    return name if name in ordner else name


def auffangorte(pf) -> list:
    """Where is post nobody has decided about yet?

    🔴 2026-09-27, his finding: he had entered auto.autohaus-nord under „create
    new“, but the mail behind it was not moved there and now he cannot find it at
    all. The mails lay in **Unsortiert**, and `nachziehen()` looked at **the inbox
    only**. Up to 4.0.0 that was right: unrecognised post STAYED in the inbox.
    Since the restructuring the Postwache clears it into the catch folder — which
    silently made the assumption „whatever is already there lies in the inbox“
    wrong, and the message said truthfully „there was nothing of that in the
    inbox“. True and useless.

    🔑 Post is only fetched from catch locations: the inbox, `Unsortiert` and
    every `…Allgemein`. Those are the places where post lies because nobody has
    decided yet. From a curated folder (`Auto.KIA`) NOTHING is fetched — there the
    decision has already been made, and a new rule must not overturn it
    retroactively.
    """
    auffang = "Unsortiert"
    try:
        auffang = _umbau().AUFFANG
    except Exception:
        pass
    raus = ["INBOX"]
    for name in pf.ordner_liste():
        if name.upper() == "INBOX":
            continue
        letztes = name.split(pf.trenner)[-1].lower()
        if letztes == auffang.lower() or letztes == "allgemein":
            raus.append(name)
    return raus


def nachziehen(adresse: str, ziel: str) -> dict:
    """Send the post that is ALREADY there after its new target.

    🔑 Without this a freshly set rule has no effect for him. He said on
    2026-09-12: „postwache erstellt zwar ordner aber die mails aus dem posteingang
    verschiebt es dann aber nicht automatisch dorthin.“ (the Postwache does create
    folders but then does not move the mails from the inbox there automatically)
    That is exactly how it was: `Seal-82` and `Autogruppe` were created and empty,
    while their 4 and 9 mails lay in the inbox. The watchman only reads NEW post
    (`UID last+1:*`) — what is already there it never sees again.

    Setting a rule is a statement about THIS SENDER, not about a point in time. So
    it applies backwards as well.

    The search runs over all `auffangorte()`, not only the inbox — the reason is
    documented there.
    """
    if not W:
        return {"bewegt": 0, "text": ""}
    einst = W.einstellungen()
    if not einst.get("scharf"):
        return {"bewegt": 0, "text": " " + txt("a.lernlauf")}
    if os.path.exists(DISABLED):
        return {"bewegt": 0, "text": " " + txt("a.notaus_kurz")}
    adresse = (adresse or "").strip().lower()
    if not adresse or not ziel:
        return {"bewegt": 0, "text": ""}
    bewegt = liegen = 0
    her = {}
    try:
        with W.Postfach(W.zugang(), True) as pf:
            voll = pf.voller_name(ziel)
            for ort in auffangorte(pf):
                # Nothing is fetched out of the TARGET itself — otherwise post
                # would move into its own folder.
                if ort == voll:
                    continue
                typ, _ = pf.m.select(pf._zitat(ort), readonly=False)
                if typ != "OK":
                    continue
                typ, dat = pf.m.uid("search", None, "HEADER", "FROM", '"%s"' % adresse)
                if typ != "OK" or not dat or not dat[0]:
                    continue
                for uid in [int(x) for x in dat[0].split()]:
                    try:
                        msg, text = pf.kopf_und_text(uid)
                    except Exception:
                        continue
                    if msg is None:
                        continue
                    kopf = W.kopf_lesen(msg, text)
                    # 🔴 The IMAP search matches SUBSTRINGS. Before anything is
                    # moved, the address has to match EXACTLY — otherwise foreign
                    # post travels along just because it carries the name in its
                    # header.
                    if (kopf.get("adresse") or "").lower() != adresse:
                        continue
                    urteil = W.einordnen(kopf, text)
                    # The same bolts as in the watchman, at the same place — a
                    # second, diverging version would be the next bug.
                    if urteil.get("phishing"):
                        liegen += 1
                        continue
                    if einst.get("wichtiges_bleibt") and urteil["klasse"] in W.ALARM:
                        liegen += 1
                        continue
                    if pf.verschieben(uid, voll):
                        bewegt += 1
                        her[ort] = her.get(ort, 0) + 1
                        W.anhaengen("journal.jsonl", {
                            "zeit": datetime.now().isoformat(timespec="seconds"),
                            "uid": uid, "von": ort, "nach": voll, "anzeige": ziel,
                            "klasse": urteil["klasse"], "betreff": kopf["betreff"],
                            "absender": kopf["adresse"], "zurueck": False},
                            W.JOURNAL_ZEILEN)
            # 🔴 Back to the inbox — a change has to return to where it came
            #    from, otherwise every following fetch grasps at nothing.
            try:
                pf.m.select("INBOX", readonly=False)
            except Exception:
                pass
    except Exception as e:
        return {"bewegt": 0, "text": " " + txt("a.nach.fehler", fehler=str(e)[:120])}
    if bewegt:
        W.chronik("nachgezogen", titel=txt("a.nach.titel"),
                  detail=txt("a.nach.detail", n=bewegt, adresse=adresse, ziel=ziel))
    teile = []
    if bewegt:
        # 🔑 WHERE FROM, not just how many. „2 mail(s) taken along“ leaves him
        #    guessing where they lay before — exactly the question this bug
        #    started with.
        woher = ", ".join("%s (%d)" % (o, n)
                          for o, n in sorted(her.items(), key=lambda x: -x[1]))
        teile.append(txt("a.nach.bewegt", n=bewegt, orte=woher))
    if liegen:
        teile.append(txt("a.nach.liegen", n=liegen))
    if not teile:
        teile.append(txt("a.nach.nichts"))
    return {"bewegt": bewegt, "text": " " + " ".join(teile)}

def ordner_anlegen(d: dict) -> dict:
    """Create a new folder in the mailbox AND bind the sender to it.

    🔴 The WATCHMAN still never creates a folder — that stays as it is. What
    creates here is his click. That is why this is in the web part and not in the
    watchman: changing a structure is a decision, not a derivation.
    """
    if not W:
        return {"ok": False, "text": txt("a.kein_waechter")}
    name = str(d.get("ordner") or "").strip().strip("/")
    adresse = str(d.get("adresse") or "").strip().lower()
    if not name:
        return {"ok": False, "text": txt("a.ordner.kein_name")}
    if len(name) > 60 or re.search(r"[\\\"\x00-\x1f]", name):
        return {"ok": False, "text": txt("a.ordnername")}
    zug = W.zugang()
    if not zug.get("adresse"):
        return {"ok": False, "text": txt("a.kein_postfach")}
    try:
        with W.Postfach(zug, True) as pf:
            vorhanden = set(pf.ordner_liste())
            # The separator is ASKED of the server (at some providers: „.“).
            # Guess it and you create a folder with a dot in its name instead of
            # a subfolder.
            voll = name.replace("/", pf.trenner)
            if voll in vorhanden:
                angelegt = False
            else:
                typ, antw = pf.m.create(pf._zitat(voll))
                if typ != "OK":
                    return {"ok": False,
                            "text": txt("a.postfach_lehnt_ab",
                                        fehler=(antw[0].decode(errors="replace")[:120]
                                                if antw else "?"))}
                try:
                    pf.m.subscribe(pf._zitat(voll))
                except Exception:
                    pass
                angelegt = True
            k = W.ablage_lernen(pf)          # so that the folder is known at once
        W.save(W.ABLAGE, k)
    except Exception as e:
        return {"ok": False, "text": txt("a.ordner.fehler", fehler=str(e)[:160])}
    # 🔴 Both sentences SPELLED OUT, not `txt("a" if x else "b")`: the language
    #    test bench only sees the first string after `txt(`.
    if angelegt:
        text = txt("a.ordner.angelegt", ordner=voll)
    else:
        text = txt("a.ordner.schon_da", ordner=voll)
    if adresse:
        antwort = einstellung_setzen({"absender": {"adresse": adresse, "ordner": voll}})
        if not antwort.get("ok"):
            return {"ok": False, "text": text + " " + str(antwort.get("text") or "")}
        # Pass the feedback of the retroactive move through — otherwise he does
        # not learn whether the existing post came along.
        text += " " + txt("a.ordner.gebunden", adresse=adresse)
        rest = str(antwort.get("text") or "").replace("Gespeichert.", "", 1).strip()
        if rest:
            text += " " + rest
    return {"ok": True, "text": text}


def statistik_neu(_d) -> dict:
    """Recompute the numbers at once. READONLY — a statistic touches nothing."""
    if not W:
        return {"ok": False, "text": txt("a.kein_waechter")}
    zug = W.zugang()
    if not zug.get("adresse"):
        return {"ok": False, "text": txt("a.kein_postfach")}
    try:
        with W.Postfach(zug, False) as pf:      # readonly
            k = W.statistik_lernen(pf)
        W.save(W.STATISTIK, k)
        return {"ok": True,
                "text": txt("a.statistik", mails=k["mails"], ordner=k["ordner"],
                            von=k["von"], bis=k["bis"],
                            schnitt="%.1f" % k["schnitt_pro_tag"])}
    except Exception as e:
        return {"ok": False, "text": txt("a.statistik_fehler", fehler=str(e)[:160])}


def neu_lernen(_d) -> dict:
    """Rebuild the map at once. Takes seconds to minutes — which is why it
    otherwise runs only once a day."""
    if not W:
        return {"ok": False, "text": txt("a.kein_waechter")}
    zug = W.zugang()
    if not zug.get("adresse"):
        return {"ok": False, "text": txt("a.kein_postfach")}
    try:
        with W.Postfach(zug, False) as pf:     # READONLY — learning never changes anything
            k = W.ablage_lernen(pf)
        W.save(W.ABLAGE, k)
        return {"ok": True,
                "text": txt("a.gelernt", mails=k["mails"], ordner=len(k["ordner"]),
                            absender=len(k["absender"]),
                            deckung="%.0f" % k["deckung"])}
    except Exception as e:
        return {"ok": False, "text": txt("a.lernen_fehler", fehler=str(e)[:160])}


def tg_lage() -> dict:
    """Which bot is radioing right now — and from which source.

    🔴 The token itself NEVER comes out here, only the name Telegram gives for
    it. The name is the only feedback he needs to see that the right bot is
    configured.
    """
    eigen = os.path.exists(os.path.join(STATE, "telegram_zugang.json"))
    tok = W.tg_zugang("TELEGRAM_BOT_TOKEN") if W else ""
    chat = W.tg_zugang("TELEGRAM_CHAT_ID") if W else ""
    name = ""
    if tok:
        try:
            with urllib.request.urlopen(
                    "https://api.telegram.org/bot%s/getMe" % tok, timeout=10) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            name = "@" + (d.get("result") or {}).get("username", "?")
        except Exception:
            name = txt("a.tg.stumm")
    return {"eigen": eigen, "bot": name, "chat_gesetzt": bool(chat),
            "chat": chat if chat else ""}


def tg_zugang_speichern(d: dict) -> dict:
    """Enter your own bot. 0600, and checked at once.

    🔴 A bot may not write first: Telegram answers with `chat not found` as long
    as the human has not started the bot themselves. That is not a configuration
    error but a missing step — and the message says exactly that instead of
    pointing at the chat id.
    """
    tok = str(d.get("token") or "").strip()
    chat = str(d.get("chat") or "").strip()
    alt = st("telegram_zugang.json", {}) or {}
    if not tok:
        tok = str(alt.get("TELEGRAM_BOT_TOKEN") or "")
    if not chat:
        chat = str(alt.get("TELEGRAM_CHAT_ID") or "") or (
            W.tg_zugang("TELEGRAM_CHAT_ID") if W else "")
    if not tok:
        return {"ok": False, "text": txt("a.tg.kein_token")}
    if not chat:
        return {"ok": False, "text": txt("a.tg.keine_chat")}
    # 1) Does the token belong to a real bot?
    try:
        with urllib.request.urlopen(
                "https://api.telegram.org/bot%s/getMe" % tok, timeout=12) as r:
            g = json.loads(r.read().decode("utf-8", "replace"))
        if not g.get("ok"):
            return {"ok": False, "text": txt("a.tg.token_unbekannt")}
        name = "@" + (g.get("result") or {}).get("username", "?")
    except urllib.error.HTTPError as e:
        return {"ok": False, "text": txt("a.tg.token_abgelehnt", code=e.code)}
    except Exception as e:
        return {"ok": False, "text": txt("a.tg.nicht_erreichbar", fehler=str(e)[:120])}
    # 2) Does it really reach him?
    try:
        req = urllib.request.Request(
            "https://api.telegram.org/bot%s/sendMessage" % tok,
            data=json.dumps({"chat_id": chat,
                             "text": "\U0001F4EC Postwache funkt ab jetzt ueber %s." % name
                             }).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=12).read()
    except urllib.error.HTTPError as e:
        grund = ""
        try:
            grund = json.loads(e.read().decode("utf-8", "replace")).get("description", "")
        except Exception:
            pass
        if "chat not found" in grund.lower():
            return {"ok": False,
                    "text": txt("a.tg.kein_start", name=name)}
        return {"ok": False, "text": txt("a.tg.sendet_nicht", name=name,
                                         fehler=grund[:140])}
    except Exception as e:
        return {"ok": False, "text": txt("a.tg.senden_fehler", fehler=str(e)[:140])}
    schreibe("telegram_zugang.json",
             {"TELEGRAM_BOT_TOKEN": tok, "TELEGRAM_CHAT_ID": chat}, 0o600)
    return {"ok": True, "text": txt("a.tg.gut", name=name)}


# ── Aktionen ──────────────────────────────────────────────────────────────────
def postfach_speichern(d: dict) -> dict:
    """Create or change a mailbox. The connection is checked IMMEDIATELY —
    credentials that only show up as wrong on the next run leave you believing it
    is set up.

    🔴 The id is the folder name of the state and is assigned ONCE at creation.
    Changing it later would make this mailbox's entire learned state unreachable
    without anyone noticing — so only the display name can be changed.
    """
    pf_waehlen("")
    liste = list(pf_liste())
    pid = re.sub(r"[^a-zA-Z0-9_-]", "", str(d.get("id") or "")).strip()
    adresse = str(d.get("adresse") or "").strip()
    vorhanden = next((f for f in liste if f["id"] == pid), None) if pid else None

    if not vorhanden:
        if not adresse or "@" not in adresse:
            return {"ok": False, "text": txt("a.keine_adresse")}
        if not pid:
            stamm = re.sub(r"[^a-z0-9]", "", adresse.split("@")[0].lower()) or "postfach"
            pid, n = stamm, 2
            while any(f["id"] == pid for f in liste):
                pid, n = "%s%d" % (stamm, n), n + 1
    if vorhanden and not adresse:
        adresse = vorhanden["adresse"]

    passwort = str(d.get("passwort") or "") or (vorhanden or {}).get("passwort") or ""
    if not passwort:
        return {"ok": False, "text": txt("a.kein_passwort")}
    server = (str(d.get("server") or "").strip()
              or (vorhanden or {}).get("server") or W._server_raten(adresse))
    try:
        port = int(d.get("port") or (vorhanden or {}).get("port") or 993)
    except (TypeError, ValueError):
        port = 993

    probe = pruefen(adresse, passwort, server, port)
    if not probe["ok"]:
        return probe

    # 🔴 NOT a fresh dictionary — the existing one, with only what this form owns
    # written over it. Built from scratch, this line silently dropped every field
    # that belongs to somebody else: since 5.0.0 those are the outgoing server and
    # the sender name, so RENAMING a mailbox deleted its way out. Found by the test
    # bench, which asked afterwards whether the setting was still there.
    eintrag = dict(vorhanden or {})
    eintrag.update({"id": pid, "name": str(d.get("name") or "").strip() or adresse,
                    "adresse": adresse, "passwort": passwort, "server": server,
                    "port": port,
                    "an": bool(d.get("an", (vorhanden or {}).get("an", True)))})
    liste = [e for e in liste if e["id"] != pid] + [eintrag]
    schreibe("postfaecher.json", {"liste": liste}, 0o600)
    return {"ok": True, "text": "Verbunden — %s. %s" % (adresse, probe["text"]),
            "id": pid}


def postfach_entfernen(d: dict) -> dict:
    """Take a mailbox out of the list.

    🔴 The learned state under `state/pf/<id>/` STAYS. Deleting it along with the
    entry would be an irreversible click: months of learned filing, sender profiles
    and attachment index gone because somebody wanted to tidy up for a moment.
    Whoever needs the space deletes the folder by hand.
    """
    pf_waehlen("")
    pid = str(d.get("id") or "").strip()
    liste = [e for e in pf_liste() if e["id"] != pid]
    if len(liste) == len(pf_liste()):
        return {"ok": False, "text": txt("a.pf_unbekannt")}
    schreibe("postfaecher.json", {"liste": liste}, 0o600)
    return {"ok": True, "text": "Entfernt. Der gelernte Stand bleibt erhalten — "
                                "wer das Postfach wieder anlegt, ist sofort wieder da."}


def postfach_schalten(d: dict) -> dict:
    """Let a mailbox rest without losing it."""
    pf_waehlen("")
    pid = str(d.get("id") or "").strip()
    liste = pf_liste()
    if not any(e["id"] == pid for e in liste):
        return {"ok": False, "text": txt("a.pf_unbekannt")}
    for e in liste:
        if e["id"] == pid:
            e["an"] = bool(d.get("an"))
    schreibe("postfaecher.json", {"liste": liste}, 0o600)
    return {"ok": True, "text": "Eingeschaltet." if d.get("an") else "Ruht."}


# ── Judgement aid ────────────────────────────────────────────
# 🔑 Who is asking right now. Set at ONE place (do_POST), read by the Ollama
# search — exactly like the mailbox. Had every action fetched the address itself,
# every action would have to know the handler.
_KLIENT = ""


def klient_merken(adresse: str) -> None:
    global _KLIENT
    _KLIENT = str(adresse or "").strip()


def ki_kurz() -> dict:
    """What the page may know about the judgement aid. 🔴 Never the key, only
    whether one is stored."""
    if W is None:
        return {"anbieter": "aus"}
    k = W.ki_konfig()
    ok, grund = W.ki_bereit()
    return {"anbieter": k["anbieter"], "url": k["url"], "modell": k["modell"],
            "schluessel_gesetzt": bool(k["schluessel"]), "bereit": ok, "grund": grund,
            "anbieter_liste": list(W.KI_ANBIETER)}


def ki_speichern(d: dict) -> dict:
    k = st("ki.json", {})
    if not isinstance(k, dict):
        k = {}
    if "anbieter" in d:
        a = str(d.get("anbieter") or "aus").strip().lower()
        if a not in (W.KI_ANBIETER if W else ("aus",)):
            return {"ok": False, "text": txt("a.ki_anbieter_unbekannt")}
        # Changing provider resets address and model to the new one's defaults —
        # otherwise the Ollama address would stay when somebody switches to OpenAI.
        if a != k.get("anbieter"):
            k["url"] = (W.KI_STANDARD_URL.get(a, "") if W else "")
            k["modell"] = (W.KI_STANDARD_MODELL.get(a, "") if W else "")
        k["anbieter"] = a
    for feld in ("url", "modell"):
        if feld in d:
            k[feld] = str(d.get(feld) or "").strip().rstrip("/") if feld == "url" \
                else str(d.get(feld) or "").strip()
    if str(d.get("schluessel") or ""):
        k["schluessel"] = str(d["schluessel"])
    if d.get("schluessel_loeschen"):
        k.pop("schluessel", None)
    schreibe("ki.json", k, 0o600)          # 🔴 0600 like any other credential
    return {"ok": True, "text": txt("a.gespeichert"), "ki": ki_kurz()}


def ki_pruefen(_d=None) -> dict:
    """A real, tiny question to the configured model. Not „reachable“ but
    „answers“ — a service that returns 200 on its start page but does not know the
    model would otherwise show green."""
    if W is None:
        return {"ok": False, "text": txt("a.kein_waechter")}
    ok, grund = W.ki_bereit()
    if not ok:
        return {"ok": False, "text": grund}
    if W.ki_konfig()["anbieter"] == "werkstatt":
        return {"ok": True, "text": txt("a.werkstatt_da")}
    t0 = time.time()
    try:
        antwort = W.ki_fragen(txt("a.ki_probe_system"), txt("a.ki_probe_frage"))
    except Exception as e:
        return {"ok": False, "text": txt("a.ki_keine_antwort", fehler=str(e)[:160])}
    dauer = time.time() - t0
    kurz = (antwort or "").strip().splitlines()[0][:60] if antwort else ""
    if not kurz:
        return {"ok": False, "text": txt("a.ki_stumm")}
    return {"ok": True, "text": txt("a.ki_antwortet", s="%.1f" % dauer, wort=kurz)}


def vorschlaege_lesen() -> dict:
    v = lade(os.path.join(OUT, "vorschlaege.json"), {})
    return v if isinstance(v, dict) else {}


def ki_suchen(_d=None) -> dict:
    """Where does the WATCHMAN find a local model?

    🔴 The search runs on its side, not in the browser. Search from the browser
    and you find the Ollama on your own machine and report „reachable“, while the
    watchman on the Pi never gets there.
    """
    if W is None:
        return {"ok": False, "text": txt("a.kein_waechter")}
    # The machine that currently has the page open is the most likely place for a
    # local model — it is asked as well, and nothing else.
    gefunden = W.ollama_suchen([_KLIENT] if _KLIENT else [])
    for e in gefunden:
        e["vorschlag"] = W.ollama_taugliches(e["modelle"])
    if not gefunden:
        return {"ok": False, "text": W.txt("ki.lokal.nichts"), "fundstellen": []}
    n = sum(len(e["modelle"]) for e in gefunden)
    return {"ok": True, "fundstellen": gefunden,
            "text": W.txt("ki.lokal.gefunden", n=n, wo=gefunden[0]["url"])}


def ki_uebernehmen(d: dict) -> dict:
    """Enter the found address and model — and ASK AT ONCE whether it really
    answers. 🔴 „saved“ is not „works“: exactly the same distinction as with a
    handover to DocuSort."""
    url = str(d.get("url") or "").strip().rstrip("/")
    modell = str(d.get("modell") or "").strip()
    if not url or not modell:
        return {"ok": False, "text": (W.txt("ki.lokal.unvollstaendig") if W
                                      else "Adresse oder Modell fehlt.")}
    a = ki_speichern({"anbieter": "ollama", "url": url, "modell": modell})
    if not a.get("ok"):
        return a
    probe = ki_pruefen()
    return {"ok": bool(probe.get("ok")),
            "text": probe.get("text") or a.get("text"), "ki": ki_kurz()}


def vorschlag_uebernehmen(d: dict) -> dict:
    """Turn a suggestion into a rule of his own — through the same door a
    hand-set rule goes through."""
    absender = str(d.get("absender") or "").strip().lower()
    schublade = str(d.get("schublade") or "").strip()
    if not absender or (W and schublade not in W.SCHUBLADEN):
        return {"ok": False, "text": "Vorschlag unvollstaendig."}
    ergebnis = einstellung_setzen({"absender_regel": {"adresse": absender,
                                                      "ziel": schublade}})
    pf_waehlen("")
    v = vorschlaege_lesen()
    v["liste"] = [x for x in (v.get("liste") or []) if x.get("absender") != absender]
    try:
        with open(os.path.join(OUT, "vorschlaege.json"), "w", encoding="utf-8") as fh:
            json.dump(v, fh, ensure_ascii=False, indent=1)
    except OSError:
        pass
    return ergebnis


def vorschlag_verwerfen(d: dict) -> dict:
    absender = str(d.get("absender") or "").strip().lower()
    v = vorschlaege_lesen()
    v["liste"] = [x for x in (v.get("liste") or []) if x.get("absender") != absender]
    try:
        with open(os.path.join(OUT, "vorschlaege.json"), "w", encoding="utf-8") as fh:
            json.dump(v, fh, ensure_ascii=False, indent=1)
    except OSError:
        pass
    return {"ok": True, "text": "Verworfen."}


def konfig_speichern(d: dict) -> dict:
    """The environment: page address, Home Assistant, workshop. Enter nothing here
    and you keep getting what was detected."""
    k = st("konfig.json", {})
    if not isinstance(k, dict):
        k = {}
    if "seite" in d:
        k["seite"] = str(d.get("seite") or "").strip().rstrip("/")
    if "imap_server" in d:
        k["imap_server"] = str(d.get("imap_server") or "").strip()
    if "sprache" in d:
        code = str(d.get("sprache") or "").strip().lower()
        if W is not None and code not in W.SPRACHEN:
            return {"ok": False, "text": txt("a.sprache_unbekannt")}
        k["sprache"] = code
    if "werkstatt" in d:
        k["werkstatt"] = str(d.get("werkstatt") or "").strip()
    if any(x in d for x in ("ha_url", "ha_token_datei", "ha_schalter")):
        ha = k.get("ha") if isinstance(k.get("ha"), dict) else {}
        for kurz, lang in (("ha_url", "url"), ("ha_token_datei", "token_datei"),
                           ("ha_schalter", "schalter")):
            if kurz in d:
                ha[lang] = str(d.get(kurz) or "").strip().rstrip("/") \
                    if lang == "url" else str(d.get(kurz) or "").strip()
        k["ha"] = ha
    schreibe("konfig.json", k)
    if W is not None:
        W._KONFIG_ZWISCHEN = None          # effective at once, not only after a restart
    return {"ok": True, "text": txt("a.gespeichert"), "konfig": konfig_kurz()}


def konfig_kurz() -> dict:
    if W is None:
        return {}
    k = W.konfig()
    return {"sprache": k["sprache"], "seite": k["seite"], "imap_server": k["imap_server"],
            "werkstatt": k["werkstatt"], "ha_url": k["ha"]["url"],
            "ha_token_datei": k["ha"]["token_datei"], "ha_schalter": k["ha"]["schalter"],
            "ha_an": W.ha_an()}


def zugang_speichern(d: dict) -> dict:
    """The old route from 2.x — it wrote `zugang.json`, which nobody has read
    since the migration.

    🔴 Leaving it in place would be a trap: the call would have answered „saved“
    and done nothing. So it goes through the same door as everything else and puts
    the mailbox into the list.
    """
    daten = dict(d)
    daten.setdefault("id", (W.zugang() or {}).get("id", "") if W else "")
    return postfach_speichern(daten)


def pruefen(adresse: str, passwort: str, server: str, port: int) -> dict:
    """Log in once and leave again. READONLY: a check must change nothing in the
    mailbox, not even the read status."""
    try:
        socket.setdefaulttimeout(25)
        m = imaplib.IMAP4_SSL(server, port)
        try:
            m.login(adresse, passwort)
            typ, daten = m.select("INBOX", readonly=True)
            anzahl = int(daten[0]) if typ == "OK" and daten and daten[0] else 0
            return {"ok": True, "text": txt("a.posteingang", n=anzahl)}
        finally:
            try:
                m.logout()
            except Exception:
                pass
    except imaplib.IMAP4.error as e:
        return {"ok": False, "text": txt("a.postfach_lehnt_ab", fehler=str(e)[:180])}
    except Exception as e:
        return {"ok": False, "text": txt("a.keine_verbindung", fehler=str(e)[:180])}


def einstellung_setzen(d: dict) -> dict:
    # Remembers whether a sender rule CAME INTO BEING in this call. Has to be
    # preset — otherwise every ordinary save breaks with an UnboundLocalError.
    nachziehen_an = None
    e = st("einstellungen.json", {})
    if not isinstance(e, dict):
        e = {}
    for schalter in ("scharf", "telegram", "wichtiges_bleibt"):
        if schalter in d:
            e[schalter] = bool(d[schalter])
    if "bericht_stunde" in d:
        try:
            h = int(d["bericht_stunde"])
            if 0 <= h <= 23:
                e["bericht_stunde"] = h
        except (TypeError, ValueError):
            pass
    if "regel" in d and isinstance(d["regel"], dict):
        k = str(d["regel"].get("klasse") or "")
        if k in SCHUBLADEN and "melden" in d["regel"]:
            regeln = e.get("regeln") if isinstance(e.get("regeln"), dict) else {}
            r = regeln.get(k) if isinstance(regeln.get(k), dict) else {}
            r["melden"] = bool(d["regel"]["melden"])
            regeln[k] = r
            e["regeln"] = regeln
    if "absender" in d and isinstance(d["absender"], dict):
        a = str(d["absender"].get("adresse") or "").strip().lower()
        ordner = str(d["absender"].get("ordner") or "").strip()
        ar = e.get("absender_regeln") if isinstance(e.get("absender_regeln"), dict) else {}
        if a and ordner:
            # 🔴 Only into a folder that REALLY exists. The watchman creates no
            # folders, and a rule pointing at a target that does not exist would
            # fail silently on every run.
            bekannt = (st("ablage.json", {}) or {}).get("ordner") or {}
            if ordner not in bekannt:
                return {"ok": False,
                        "text": txt("a.ordner_fehlt", ordner=ordner)}
            ar[a] = ordner
            # 🔑 At THIS one place all the routes on which a sender rule comes
            # into being run together: the button next to the mail, „Here“ in the
            # homeless card, and „Create“ (which calls in here). So the
            # retroactive move belongs here and not on the buttons — otherwise
            # the next route forgets it again.
            nachziehen_an = (a, ordner)
        elif a:
            ar.pop(a, None)
        e["absender_regeln"] = ar
    schreibe("einstellungen.json", e)
    if nachziehen_an:
        return {"ok": True,
                "text": txt("a.gespeichert") + nachziehen(*nachziehen_an)["text"]}
    return {"ok": True, "text": txt("a.gespeichert")}


def notaus_datei(an: bool) -> dict:
    if an:
        with open(DISABLED, "w", encoding="utf-8") as fh:
            fh.write("von der Uebersichtsseite gesetzt %s\n"
                     % datetime.now().isoformat(timespec="seconds"))
        return {"ok": True, "text": txt("a.notaus_an")}
    try:
        os.remove(DISABLED)
    except OSError:
        pass
    return {"ok": True, "text": txt("a.notaus_aus")}


def schalten(an: bool) -> dict:
    tok = token()
    if not tok:
        return {"ok": False, "text": "Kein HA-Token."}
    try:
        api("/api/services/input_boolean/turn_%s" % ("on" if an else "off"), tok,
            data={"entity_id": SCHALTER})
        return {"ok": True, "text": "Schalter %s." % ("an" if an else "aus")}
    except urllib.error.HTTPError as e:
        return {"ok": False, "text": "HA antwortet %s" % e.code}
    except Exception as e:
        return {"ok": False, "text": str(e)[:160]}


def zuruecksortieren(eintraege: list) -> dict:
    """Undo moves. The way BACK has to stay open at all times — otherwise „first
    learn, then arm“ would be a one-way street."""
    if not W:
        return {"ok": False, "text": txt("a.kein_waechter")}
    zug = W.zugang() if W else {}
    if not zug.get("adresse"):
        return {"ok": False, "text": txt("a.kein_postfach")}
    journal = jsonl("journal.jsonl", 5000)
    gesucht = {(int(e.get("uid") or 0), str(e.get("nach") or "")) for e in eintraege}
    getan, fehler = 0, 0
    try:
        with W.Postfach(dict(zug, server=zug.get("server") or "imap.beispiel.example",
                             port=int(zug.get("port") or 993)), True) as pf:
            for j in journal:
                schl = (int(j.get("uid") or 0), str(j.get("nach") or ""))
                if j.get("zurueck") or schl not in gesucht:
                    continue
                if pf.zurueck(int(j["uid"]), j["nach"]):
                    j["zurueck"] = True
                    getan += 1
                else:
                    fehler += 1
    except Exception as e:
        return {"ok": False, "text": txt("a.postfach", fehler=str(e)[:160])}
    try:
        tmp = os.path.join(OUT, "journal.jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(json.dumps(j, ensure_ascii=False) for j in journal) + "\n")
        os.replace(tmp, os.path.join(OUT, "journal.jsonl"))
    except OSError:
        pass
    return {"ok": getan > 0 or not fehler,
            "text": txt("a.zurueck", n=getan,
                        rest=(txt("a.zurueck_fehler", n=fehler) if fehler else ""))}


def aufraeumen(d: dict) -> dict:
    """Go through the inbox ONCE — including what is already lying there.

    🔑 Without this his actual annoyance stays untouched. In normal operation the
    watchman reads only NEW mails (`UID last+1:*`); everything that arrived before
    it was set up, or that it left lying under old rules, is past for it for ever.
    Exactly the 22 mails he was looking at on 2026-09-12 lay behind the pointer.

    🔴 Nothing is reported. A tidy-up is not an event: 22 wake-up calls for post
    that has been lying there for days would be noise, not a service. The UID
    pointer is NOT touched either — this run is an addition, not a replacement for
    normal operation.

    The bolt is the same as in the watchman: only where he has filed something
    himself or where his folder names the sender.
    """
    if not W:
        return {"ok": False, "text": txt("a.kein_waechter")}
    einst = W.einstellungen()
    if not einst.get("scharf"):
        return {"ok": False,
                "text": "Er ist im Lernlauf. Erst „Wirklich sortieren\u201c einschalten."}
    if os.path.exists(DISABLED):
        return {"ok": False, "text": txt("a.notaus_ist")}
    zug = W.zugang()
    if not zug.get("adresse"):
        return {"ok": False, "text": txt("a.kein_postfach")}
    grenze = 500
    try:
        with W.Postfach(zug, True) as pf:
            karte = W.ablage_frisch(pf)
            pf.m.select("INBOX", readonly=False)
            typ, dat = pf.m.uid("search", None, "ALL")
            if typ != "OK" or not dat or not dat[0]:
                return {"ok": True, "text": "Der Posteingang ist leer."}
            uids = [int(x) for x in dat[0].split()][-grenze:]
            bewegt, gesehen, blieb = 0, 0, {}
            for uid in uids:
                try:
                    msg, text = pf.kopf_und_text(uid)
                except Exception:
                    continue
                if msg is None:
                    continue
                gesehen += 1
                kopf = W.kopf_lesen(msg, text)
                urteil = W.einordnen(kopf, text)
                eigen = W.absender_regel(einst, kopf["adresse"])
                if eigen:
                    ziel, darf = eigen, True
                else:
                    ziel, _g, _s, darf = W.ziel_finden(karte, kopf["adresse"])
                darf = bool(ziel) and darf and (eigen or ziel in (karte.get("ordner") or {}))
                if urteil.get("phishing"):
                    darf = False
                if darf and einst.get("wichtiges_bleibt") and urteil["klasse"] in W.ALARM:
                    darf = False
                if not darf:
                    blieb[urteil["klasse"]] = blieb.get(urteil["klasse"], 0) + 1
                    continue
                if pf.verschieben(uid, pf.voller_name(ziel)):
                    bewegt += 1
                    W.anhaengen("journal.jsonl", {
                        "zeit": datetime.now().isoformat(timespec="seconds"),
                        "uid": uid, "von": "INBOX", "nach": pf.voller_name(ziel),
                        "anzeige": ziel, "klasse": urteil["klasse"],
                        "betreff": kopf["betreff"], "absender": kopf["adresse"],
                        "zurueck": False}, W.JOURNAL_ZEILEN)
    except Exception as e:
        return {"ok": False, "text": txt("a.aufraeumen_fehler", fehler=str(e)[:160])}
    rest = ", ".join("%d\u00d7 %s" % (n, k) for k, n in
                     sorted(blieb.items(), key=lambda kv: -kv[1])[:4])
    W.chronik("aufgeraeumt", titel="Posteingang aufger\u00e4umt",
              detail="%d von %d Mails einsortiert. Liegen geblieben: %s."
                     % (bewegt, gesehen, rest or "nichts"))
    return {"ok": True,
            "text": txt("a.aufgeraeumt", bewegt=bewegt, gesehen=gesehen,
                        rest=rest or txt("a.nichts"))}


def jetzt_pruefen() -> dict:
    try:
        r = subprocess.run(["/usr/bin/python3", os.path.join(BASE, "postwache.py")],
                           capture_output=True, text=True, timeout=180)
        letzte = (r.stdout or "").strip().splitlines()
        return {"ok": r.returncode == 0,
                "text": letzte[-1] if letzte else "Lauf beendet (rc=%s)." % r.returncode}
    except subprocess.TimeoutExpired:
        return {"ok": False, "text": txt("a.lauf_lang")}
    except Exception as e:
        return {"ok": False, "text": str(e)[:160]}


# ── Documents in the post ─────────────────────────────────
def ds_kurz() -> dict:
    """What DocuSort is allowed to put on the page. 🔴 The password NEVER — only
    THAT one is stored. The same rule as with the mailbox."""
    z = st("docusort.json", {})
    if not isinstance(z, dict):
        z = {}
    return {
        "url": str(z.get("url") or ""),
        "benutzer": str(z.get("benutzer") or ""),
        "passwort_gesetzt": bool(z.get("passwort")),
        "aktiv": bool(z.get("aktiv", True)),
        "max_mb": float(z.get("max_mb") or 25),
        "eingerichtet": bool(z.get("url") and z.get("benutzer") and z.get("passwort")),
    }


DS_MARKE = "pw1."          # how a pairing line from DocuSort starts


def im_container() -> bool:
    """Does this process run inside a container?

    In order of how much each signal can be trusted — and deliberately NOT the
    cgroup line: under cgroup v2 `/proc/1/cgroup` often says nothing but
    `0::/`.
    """
    return os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")


def ds_adresse_pruefen(url: str) -> str:
    """Empty means „fine“; otherwise this is the reason it is not.

    🔴 HTTPS stays mandatory — this account's password travels over that
    connection. Two exceptions, and only two, because neither ever leaves the
    machine:

      · **This machine itself** (`localhost`, `127.0.0.1`, `[::1]`). What never
        reaches a wire cannot be read off one.
      · **A name without a dot** (`docusort`, say) — and only when WE run in a
        container ourselves. Then it is a service name from the same compose
        file, resolved inside the Docker network, and the request crosses a
        bridge inside the host rather than the network. That case IS the
        automatic pairing when both programs were installed together.

    Everything else — an address in the home network, any name with a dot —
    stays on HTTPS. A password in the clear across the LAN is not a special
    case; it is exactly what this rule exists to prevent.
    """
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if url.startswith("https://"):
        return ""
    if not url.startswith("http://"):
        return txt("a.https")
    rest = url[len("http://"):]
    wirt = rest.split("/")[0].split("@")[-1]
    ohne_tor = wirt.rsplit(":", 1)[0] if wirt.count(":") == 1 else wirt
    ohne_tor = ohne_tor.strip("[]").lower()
    if ohne_tor in ("localhost", "127.0.0.1", "::1"):
        return ""
    if "." not in ohne_tor and ":" not in ohne_tor and im_container():
        return ""
    return txt("a.https")


def ds_zugang_speichern(d: dict) -> dict:
    z = st("docusort.json", {})
    if not isinstance(z, dict):
        z = {}
    if "url" in d:
        url = str(d.get("url") or "").strip().rstrip("/")
        grund = ds_adresse_pruefen(url)
        if grund:
            return {"ok": False, "text": grund}
        z["url"] = url
    if "benutzer" in d:
        z["benutzer"] = str(d.get("benutzer") or "").strip()
    if str(d.get("passwort") or ""):
        z["passwort"] = str(d["passwort"])
    if "aktiv" in d:
        z["aktiv"] = bool(d["aktiv"])
    if "max_mb" in d:
        try:
            z["max_mb"] = max(1.0, min(200.0, float(d["max_mb"])))
        except (TypeError, ValueError):
            pass
    schreibe("docusort.json", z, 0o600)      # 🔴 0600, like the mailbox credentials
    return {"ok": True, "text": txt("a.gespeichert")}


def ds_aus_umgebung() -> str:
    """Set the DocuSort access from the environment, at start-up.

    Der Besitzer, 29.09.2026: „wer beide programme installiert hat bekommt die
    verbindung zwischen beiden sofort gesetzt … die kunden sollen nichts machen
    muessen das ist ganz wichtig!!"

    When both programs are installed together, one `.env` holds the pairing
    word and both sides read it: DocuSort creates the account with it, and this
    is where the Postwache writes it down. Nobody types anything.

    🔑 The environment SETS UP; it does not overwrite. Whatever a person typed
    on the page stays — unless the entry came from the environment in the first
    place (`aus_umgebung`), because then a new word in the `.env` is meant to
    replace the old one, and without this the handover would break after a key
    rotation and nobody would know why.
    """
    url = (os.environ.get("POSTWACHE_DS_URL") or "").strip().rstrip("/")
    wort = os.environ.get("POSTWACHE_DS_PASSWORT") or ""
    benutzer = (os.environ.get("POSTWACHE_DS_BENUTZER") or "Postwache").strip()
    if not (url and wort):
        return ""
    grund = ds_adresse_pruefen(url)
    if grund:
        return "POSTWACHE_DS_URL abgelehnt: %s" % grund
    z = st("docusort.json", {})
    if not isinstance(z, dict):
        z = {}
    schon_da = bool(z.get("url") and z.get("benutzer") and z.get("passwort"))
    if schon_da and not z.get("aus_umgebung"):
        return ""                         # a person set this up — hands off
    if (z.get("url") == url and z.get("benutzer") == benutzer
            and z.get("passwort") == wort):
        return ""                         # nothing changed
    z.update({"url": url, "benutzer": benutzer, "passwort": wort,
              "aus_umgebung": True})
    z.setdefault("aktiv", True)
    z.setdefault("max_mb", 25.0)
    schreibe("docusort.json", z, 0o600)
    return "DocuSort-Zugang aus der Umgebung eingetragen (%s)" % url


def ds_kopplung_einloesen(d: dict) -> dict:
    """Redeem the pairing line that DocuSort shows in its settings.

    One click there (copy), one here (paste). The line carries address, user and
    pairing word, base64 around a small JSON object — 🔴 which is an encoding,
    not encryption: it is a secret and belongs in `docusort.json` with 0600,
    the same as the mailbox credentials.
    """
    zeile = str(d.get("zeile") or "").strip()
    if not zeile.startswith(DS_MARKE):
        return {"ok": False, "text": txt("a.ds_kopplung_kaputt")}
    roh = zeile[len(DS_MARKE):].strip()
    roh += "=" * (-len(roh) % 4)
    try:
        inhalt = json.loads(base64.urlsafe_b64decode(roh.encode("ascii")))
        url = str(inhalt["url"]).strip().rstrip("/")
        benutzer = str(inhalt["benutzer"]).strip()
        wort = str(inhalt["passwort"])
    except Exception:
        return {"ok": False, "text": txt("a.ds_kopplung_kaputt")}
    if not (url and benutzer and wort):
        return {"ok": False, "text": txt("a.ds_kopplung_kaputt")}
    grund = ds_adresse_pruefen(url)
    if grund:
        # DocuSort names the address the browser reached it on. If that is
        # plain http on a LAN address, it is not good enough for a password —
        # say so instead of storing it.
        return {"ok": False, "text": grund}
    z = st("docusort.json", {})
    if not isinstance(z, dict):
        z = {}
    z.update({"url": url, "benutzer": benutzer, "passwort": wort,
              "aus_umgebung": False})
    z.setdefault("aktiv", True)
    z.setdefault("max_mb", 25.0)
    schreibe("docusort.json", z, 0o600)
    # 🔑 And then really log in once. A stored access that does not work looks
    # exactly like one that does.
    erg = ds_pruefen()
    if not erg.get("ok"):
        return erg
    return {"ok": True, "text": txt("a.ds_gekoppelt", benutzer=benutzer, url=url)}


def _tor_des_wirts() -> str:
    """The default gateway — that is the host, seen from inside a container."""
    try:
        with io.open("/proc/net/route", encoding="utf-8") as f:
            for zeile in f.read().split("\n")[1:]:
                teile = zeile.split()
                if len(teile) > 2 and teile[1] == "00000000":
                    h = teile[2]
                    return ".".join(str(int(h[i:i + 2], 16))
                                    for i in (6, 4, 2, 0))
    except Exception:
        pass
    return ""


def ds_suchen(_d=None) -> dict:
    """Where is DocuSort? Asked from HERE.

    🔑 A reachability search belongs on the side that has to GET there. Asked
    from the browser, an address can answer „reachable“ while the Postwache
    cannot get to it at all — that lesson cost a round with Ollama already.

    No network is scanned. Only places that are given by how the two are
    installed get asked:
    """
    kandidaten = []
    if im_container():
        kandidaten.append("http://docusort:8080")      # same compose network
    kandidaten.append("https://docusort")              # MagicDNS short name
    kandidaten.append("http://127.0.0.1:8080")         # side by side, no container
    if im_container():
        kandidaten.append("http://host.docker.internal:8080")
        tor = _tor_des_wirts()
        if tor:
            kandidaten.append("http://%s:8080" % tor)

    gefunden = []
    for basis in kandidaten:
        v = _ist_docusort(basis)
        if v:
            gefunden.append({"url": basis, "version": v})
    if not gefunden:
        return {"ok": False, "text": txt("a.ds_nicht_gefunden"),
                "gesucht": kandidaten}
    erster = gefunden[0]
    return {"ok": True, "url": erster["url"], "gefunden": gefunden,
            "text": txt("a.ds_gefunden", url=erster["url"],
                        version=erster["version"])}


def _ist_docusort(basis: str) -> str:
    """Its version if a DocuSort answers there, otherwise empty.

    🔴 Asked at `/api/version`, which needs no login — and the answer has to
    LOOK like DocuSort. „Something answered on 8080“ is not the same as „this
    is DocuSort“.
    """
    ktx = None
    if basis.startswith("https://"):
        # A tailnet name carries a real certificate; a self-signed one on the
        # machine itself is still better than not asking at all.
        ktx = ssl.create_default_context()
        ktx.check_hostname = False
        ktx.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(basis.rstrip("/") + "/api/version",
                                     headers={"User-Agent": "Postwache/%s" % VERSION})
        with urllib.request.urlopen(req, timeout=4, context=ktx) as a:
            d = json.loads(a.read(4000).decode("utf-8", "replace"))
        v = str(d.get("current") or "")
        return v if v and ("has_update" in d or "container" in d) else ""
    except Exception:
        return ""


def ds_pruefen(_d=None) -> dict:
    """Really log in once. A stored account that does not work is worse than none —
    it looks exactly the same on the page."""
    if not W:
        return {"ok": False, "text": txt("a.kein_waechter")}
    ds, grund = W.ds_bereit()
    if ds is None:
        return {"ok": False, "text": "DocuSort ist %s." % grund}
    v = ds.version()
    if not v:
        return {"ok": False, "text": txt("a.ds_weg", fehler=ds.fehler or "?")}
    fehl = ds.anmelden()
    if fehl:
        return {"ok": False, "text": fehl}
    return {"ok": True, "text": txt("a.ds_da", version=v, benutzer=ds.benutzer)}


def dokumente_suchen(d: dict) -> dict:
    if not W:
        return {"gesamt": 0, "treffer": [], "fehler": txt("a.kein_waechter")}
    idx = W.anhang_index()
    erg = W.dokumente_suchen(idx, str(d.get("q") or ""),
                             str(d.get("art") or "dokument"),
                             str(d.get("von") or ""), str(d.get("bis") or ""),
                             grenze=int(d.get("grenze") or 200))
    erg["docusort_url"] = ds_kurz()["url"]
    return erg


def dokumente_stand(_d=None) -> dict:
    """Ask DocuSort how far it has got — NOW, not at the next one-minute run.

    🔑 Nothing is ticked off because the upload worked, only once DocuSort
    confirms the document. That is exactly what the page asks for while something
    is still in transit."""
    if not W:
        return {"ok": False, "text": txt("a.kein_waechter")}
    ds, grund = W.ds_bereit()
    if ds is None:
        return {"ok": False, "text": "DocuSort ist %s." % grund}
    idx = W.anhang_index()
    try:
        n = W.ds_stand_nachtragen(ds, idx, grenze=200)
    except Exception as e:
        return {"ok": False, "text": txt("a.nachfrage_fehler", fehler=str(e)[:160])}
    if n:
        W.anhang_index_sichern(idx)
    z = W.dokument_zaehlung(idx)
    # 🔴 ALWAYS bring the short version up to date, even when nothing has
    # changed: the page sees only that. If it lags behind, the card shows „nothing
    # in transit“ although something is — and therefore never asks again.
    idx["zahlen"] = z
    W.dokument_kurz_schreiben(idx)
    return {"ok": True, "geaendert": n,
            "unterwegs": z["unterwegs"], "uebergeben": z["uebergeben"],
            "text": (txt("a.unterwegs", n=z["unterwegs"])
                     if z["unterwegs"] else txt("a.ds_durch"))}


def dokumente_nachtragen(d: dict) -> dict:
    """Read the backlog now instead of at the next run. READONLY — a back-fill
    touches nothing in the mailbox."""
    if not W:
        return {"ok": False, "text": txt("a.kein_waechter")}
    zug = W.zugang()
    if not zug.get("adresse"):
        return {"ok": False, "text": txt("a.kein_postfach")}
    try:
        idx = W.anhang_index()
        with W.Postfach(zug, False) as pf:          # readonly
            erg = W.anhaenge_nachtragen(pf, idx, frist=int(d.get("frist") or 60))
        W.anhang_index_sichern(idx)
        return {"ok": True,
                "text": txt("a.nachtrag", ordner=erg["ordner"], mails=erg["mails"],
                            neu=erg["neu"], s="%.0f" % erg["dauer"],
                            rest=("" if not erg["offen"] else
                                  txt("a.nachtrag_offen", n=erg["offen"])))}
    except Exception as e:
        return {"ok": False, "text": txt("a.nachtrag_fehler", fehler=str(e)[:160])}


# Limits of the bulk handover. 🔴 Both are needed and mean different things: the
# COUNT protects DocuSort (every document costs OCR and a judgement there), the
# TIME protects the browser waiting for the answer. Whatever does not fit stays
# selected and is fetched on the next press — it does not disappear silently.
DOK_SAMMEL_MAX = 60
DOK_SAMMEL_FRIST = 150

def ds_klartext(stand: str) -> str:
    """A handover status in words. The same text the page shows on the mail is
    used — two lists for the same thing drift apart, and in the language nobody
    reads back."""
    fertig = txt("ds.stand." + stand)
    return stand if fertig == "ds.stand." + stand else fertig


def dokumente_geben(d: dict) -> dict:
    """Give selected mails to DocuSort — one at a time or in bulk.

    This is the „after the fact“ route: the index knows which folder the mail lies
    in and which part is the document — it is only fetched now, and only that one
    part.

    🔑 ONE connection, ONE login, ONE save of the index for the whole batch. And
    only ONE implementation: the button on a single mail comes in here with a list
    of length 1. Two routes meant to do the same thing drift apart eventually."""
    if not W:
        return {"ok": False, "text": txt("a.kein_waechter")}
    roh = d.get("schluessel")
    if isinstance(roh, str):
        roh = [roh]
    schluessel = [str(x) for x in (roh or []) if x][:400]
    if not schluessel:
        return {"ok": False, "text": "Nichts ausgewaehlt."}
    ds, grund = W.ds_bereit()
    if ds is None:
        return {"ok": False, "text": "DocuSort ist %s." % grund}
    idx = W.anhang_index()
    eintraege = idx.get("eintraege") or {}
    # New handover notes are recognised by their timestamp: everything from NOW on
    # is from this call. Without that you count the results of earlier runs too.
    beginn = datetime.now().isoformat(timespec="seconds")
    ende = time.time() + DOK_SAMMEL_FRIST
    budget = DOK_SAMMEL_MAX
    gegeben = mails = umgezogen = unbekannt = rest = 0
    staende: dict = {}
    zug = W.zugang()
    try:
        with W.Postfach(zug, False) as pf:          # readonly
            for s in schluessel:
                e = eintraege.get(s)
                if not isinstance(e, dict):
                    unbekannt += 1
                    continue
                if not int(e.get("uid") or 0):
                    umgezogen += 1
                    continue
                if budget <= 0 or time.time() >= ende:
                    rest += 1
                    continue
                n = W.ds_uebergeben(ds, pf, e, str(e.get("ordner") or "INBOX"),
                                    int(e.get("uid") or 0), offen=budget)
                budget -= n
                gegeben += n
                mails += 1 if n else 0
                for x in (e.get("ds") or []):
                    if str(x.get("zeit") or "") >= beginn:
                        k = str(x.get("stand") or "?")
                        staende[k] = staende.get(k, 0) + 1
    except Exception as ex:
        W.anhang_index_sichern(idx)       # what is already over there stays noted
        return {"ok": False, "text": txt("a.uebergabe_ab", n=gegeben,
                                         fehler=str(ex)[:140])}
    W.anhang_index_sichern(idx)
    teile = []
    if gegeben:
        teile.append(txt("a.uebergeben", n=gegeben, mails=mails))
    for stand, n in sorted(staende.items(), key=lambda kv: -kv[1]):
        if stand == "uebergeben":
            continue                      # that is already in the first line
        teile.append("%d × %s" % (n, ds_klartext(stand)))
    if rest:
        teile.append(txt("a.uebrig", n=rest))
    if umgezogen:
        teile.append(txt("a.umgezogen", n=umgezogen))
    if unbekannt:
        teile.append(txt("a.nicht_im_index", n=unbekannt))
    if not teile:
        teile.append(txt("a.nichts_zu_geben"))
    return {"ok": bool(gegeben), "text": ". ".join(teile) + "."}


# ── HTTP ───────────────────────────────────────────────────
# ── Restructuring: reorder the whole mailbox (4.0.0) ─────────────────
# der Besitzer, 2026-09-27: rebuild the Postwache so that he can then run it.
#
# 🔴 The import stands inside the function, not in the file header: `umbau`
#    imports `postwache` — at the top that would be a cycle.
# 🔑 The page only STARTS things and READS the status. It does not wait: a run
#    over 17,600 mails takes minutes, and a browser hanging on the thread that
#    long runs into its own timeout and then looks like an error.
def _umbau():
    import umbau as U
    return U


def umbau_lage(_d=None) -> dict:
    try:
        U = _umbau()
    except Exception as e:
        return {"ok": False, "text": txt("u.nicht_da", grund=str(e)[:120])}
    journal = 0
    try:
        with open(U.out_pfad("umbau_journal.jsonl"), encoding="utf-8") as f:
            journal = sum(1 for z in f if z.strip())
    except Exception:
        pass
    abdruck = ""
    plan_da = os.path.exists(U.out_pfad("umbau_plan.json.gz"))
    if plan_da:
        try:
            abdruck = U.fingerabdruck(U.out_pfad("umbau_plan.json.gz"))
        except Exception:
            pass
    return {"ok": True, "stand": U.stand_lesen(),
            "inventar": os.path.exists(U.out_pfad("inventar.json.gz")),
            "plan": plan_da, "abdruck": abdruck, "journal": journal,
            "selbst_sortieren": bool((W.einstellungen().get("selbst_sortieren")
                                      if W else False))}


def _umbau_faden(schritt: str, abdruck: str, grenze: int, trocken: bool,
                 auch_vorher_leere: bool = False) -> None:
    U = _umbau()
    try:
        if schritt == "inventar":
            U.inventar()
        elif schritt == "plan":
            U.plan()
        elif schritt == "anwenden":
            U.anwenden(abdruck, grenze, trocken)
        elif schritt == "zurueck":
            U.zurueck(grenze)
        elif schritt == "ordner":
            # 🔴 The default stays DRY. Armed only when the page says so.
            U.ordner_raeumen(trocken=trocken, auch_vorher_leere=auch_vorher_leere)
    except Exception as e:
        # 🔴 A thread that dies silently leaves „running“ true for ever — and then
        #    no button works any more. The error belongs in the status.
        U.stand_schreiben(laeuft=False, fehler=str(e)[:200],
                          text="abgebrochen: %s" % str(e)[:120])
    finally:
        if U.stand_lesen().get("laeuft"):
            U.stand_schreiben(laeuft=False, text="beendet")


def umbau_start(d: dict) -> dict:
    schritt = str(d.get("schritt") or "")
    if schritt not in ("inventar", "plan", "anwenden", "zurueck", "ordner"):
        return {"ok": False, "text": txt("u.schritt_unbekannt")}
    try:
        U = _umbau()
    except Exception as e:
        return {"ok": False, "text": txt("u.nicht_da", grund=str(e)[:120])}
    if U.stand_lesen().get("laeuft"):
        return {"ok": False, "text": txt("u.laeuft_schon")}
    grenze = int(d.get("grenze") or 0)
    # 🔴 When removing folders, DRY is the default: say nothing and you get a
    #    preview. For the other steps that would be wrong — there, pressing means
    #    „run it“.
    trocken = bool(d.get("trocken")) if schritt != "ordner" \
        else bool(d.get("trocken", True))
    abdruck = str(d.get("abdruck") or "")
    if schritt in ("anwenden",):
        pfad = U.out_pfad("umbau_plan.json.gz")
        if not os.path.exists(pfad):
            return {"ok": False, "text": txt("u.kein_plan")}
        # 🔴 The approval is the print of the plan the page HAS SHOWN. If a new
        #    plan was made in between, it no longer matches — and then nothing
        #    starts that nobody has seen.
        if abdruck != U.fingerabdruck(pfad):
            return {"ok": False, "text": txt("u.abdruck_alt")}
    if schritt == "zurueck" and not os.path.exists(U.out_pfad("umbau_journal.jsonl")):
        return {"ok": False, "text": txt("u.kein_journal")}
    # 🔴 Reset `gesamt`/`phase`/`entfernt` as well: otherwise the new step shows
    #    the numbers of the PREVIOUS one („0 / 17605“ during the folder run), and
    #    that looks like progress which does not exist.
    U.stand_schreiben(schritt=schritt, laeuft=True, fortschritt=0, gesamt=0,
                      phase="", entfernt=0, fehler="", weg=[], behalten=[],
                      text=txt("u.gestartet", schritt=schritt))
    import threading
    threading.Thread(target=_umbau_faden, name="umbau-" + schritt,
                     args=(schritt, abdruck, grenze, trocken,
                           bool(d.get("auch_vorher_leere"))), daemon=True).start()
    return {"ok": True, "text": txt("u.gestartet", schritt=schritt)}


# ── Migration: from one provider to another (4.4.0) ─────────────────
# der Besitzer, 2026-09-27 — you name two providers, then you can run them in parallel
# or say: transfer mail from provider A to provider B and sort it on the way; once
# the migration is done you can delete everything at A or set up a permanent
# redirection.
#
# Built like the restructuring: the page STARTS and READS, it never waits. A
# migration over 17,600 mails takes hours, not minutes.
def _umzug():
    import umzug as Z
    return Z


def umzug_lage(_d=None) -> dict:
    try:
        Z = _umzug()
    except Exception as e:
        return {"ok": False, "text": txt("z.nicht_da", grund=str(e)[:120])}
    journal = 0
    try:
        with open(Z.out_pfad("umzug_journal.jsonl"), encoding="utf-8") as f:
            journal = sum(1 for z in f if z.strip())
    except Exception:
        pass
    plan_da = os.path.exists(Z.out_pfad("umzug_plan.json.gz"))
    abdruck = ""
    if plan_da:
        try:
            abdruck = Z.fingerabdruck_plan()
        except Exception:
            pass
    art = Z.betriebsart()
    return {"ok": True, "stand": art["stand"], "art": art["art"],
            "postfaecher": art["postfaecher"], "umleitung": art["umleitung"],
            "plan": plan_da, "abdruck": abdruck, "journal": journal}


def _umzug_faden(schritt: str, von: str, nach: str, abdruck: str, grenze: int,
                 trocken: bool, seit: str) -> None:
    Z = _umzug()
    try:
        if schritt == "pruefen":
            Z.pruefen(von, nach)
        elif schritt == "plan":
            Z.plan(von, nach, grenze, seit)
        elif schritt == "uebertragen":
            Z.uebertragen(abdruck, grenze, trocken)
        elif schritt == "abgleich":
            Z.abgleich(von, nach)
        elif schritt == "leeren":
            # 🔴 Armed is the EXCEPTION here: without approval it stays dry.
            Z.quelle_leeren(von, nach, abdruck, not trocken)
    except Exception as e:
        Z.stand_schreiben(laeuft=False, fehler=str(e)[:200],
                          text="abgebrochen: %s" % str(e)[:120])
    finally:
        if Z.stand_lesen().get("laeuft"):
            Z.stand_schreiben(laeuft=False, text="beendet")


def umzug_start(d: dict) -> dict:
    schritt = str(d.get("schritt") or "")
    if schritt not in ("pruefen", "plan", "uebertragen", "abgleich", "leeren"):
        return {"ok": False, "text": txt("z.schritt_unbekannt")}
    try:
        Z = _umzug()
    except Exception as e:
        return {"ok": False, "text": txt("z.nicht_da", grund=str(e)[:120])}
    if Z.stand_lesen().get("laeuft"):
        return {"ok": False, "text": txt("z.laeuft_schon")}
    von, nach = str(d.get("von") or ""), str(d.get("nach") or "")
    if schritt in ("pruefen", "plan", "abgleich", "leeren"):
        _a, _b, fehler = Z.paar(von, nach)
        if fehler:
            return {"ok": False, "text": fehler}
    grenze = int(d.get("grenze") or 0)
    abdruck = str(d.get("abdruck") or "")
    # 🔴 For the transfer, pressing means „do it“. For emptying it means „show me
    #    first“ — there DRY is the default, as with clearing folders.
    trocken = bool(d.get("trocken", True)) if schritt == "leeren"         else bool(d.get("trocken"))
    if schritt == "uebertragen":
        pfad = Z.out_pfad("umzug_plan.json.gz")
        if not os.path.exists(pfad):
            return {"ok": False, "text": txt("z.kein_plan")}
        if abdruck != Z.fingerabdruck_plan():
            return {"ok": False, "text": txt("z.abdruck_alt")}
    Z.stand_schreiben(schritt=schritt, laeuft=True, fortschritt=0, gesamt=0,
                      phase="", fehler="", uebertragen=0, schon_dort=0,
                      geloescht=0, fehlt=0, weg=0,
                      text=txt("z.gestartet", schritt=schritt))
    import threading
    threading.Thread(target=_umzug_faden, name="umzug-" + schritt,
                     args=(schritt, von, nach, abdruck, grenze, trocken,
                           str(d.get("seit") or "")), daemon=True).start()
    return {"ok": True, "text": txt("z.gestartet", schritt=schritt)}


def umzug_umleitung(d: dict) -> dict:
    """Switch the permanent redirection on or off. At once, no thread."""
    try:
        Z = _umzug()
    except Exception as e:
        return {"ok": False, "text": txt("z.nicht_da", grund=str(e)[:120])}
    an = bool(d.get("an"))
    fehler = Z.umleitung_setzen(
        an, str(d.get("von") or ""), str(d.get("nach") or ""),
        loeschen=bool(d.get("loeschen", True)),
        deckel=int(d.get("deckel") or Z.DECKEL_STANDARD),
        ziel=("sortiert" if str(d.get("ziel") or "") == "sortiert"
              else "posteingang"))
    if fehler:
        return {"ok": False, "text": fehler}
    # 🔴 Both keys SPELLED OUT. The language test bench does not find
    #    `txt("a" if x else "b")` — it only sees the first string after `txt(`.
    #    A missing translation would be invisible exactly like that.
    if an:
        return {"ok": True, "text": txt("z.umleitung_an")}
    return {"ok": True, "text": txt("z.umleitung_aus")}


# ── The mail client (5.0.0) ───────────────────────────────────────────────
# 🔴 Everything here is BEHIND the access word — except the two that have to be
# reachable to get through the door at all. The list is the statement: whoever
# adds an action and forgets to enter it here has it open to anyone who reaches
# the page.
KLIENT_OFFEN = ("klient_lage", "klient_anmelden", "klient_wort")

KLIENT_AKTIONEN = {
    "klient_lage": lambda d, marke: _klient_lage(d, marke),
    "klient_anmelden": None,          # handled in the request, it sets the cookie
    "klient_abmelden": None,          # the same, it clears it
    "klient_wort": lambda d, marke: KL.wort_setzen(str(d.get("neu") or ""),
                                                   str(d.get("alt") or "")),
    "klient_einstellung": lambda d, marke: KL.einstellung_setzen(d.get("werte") or d),
    "klient_ordner": lambda d, marke: {"ok": True, "ordner": KL.tu(
        _pf(d), lambda k: k.baum(bool(d.get("frisch"))))},
    "klient_liste": lambda d, marke: KL.liste(dict(d, pf=_pf(d))),
    # 🔑 The waiting request. It comes back when the provider says something (IMAP
    # IDLE) — that is what makes new mail appear without anybody pressing
    # anything. It holds one thread of this server and NOT the mailbox lock.
    "klient_horch": lambda d, marke: KL.horch(dict(d, pf=_pf(d))),
    # Everything new, out of every folder at once — the view the watchman makes
    # necessary, because it is the one that moved the new mail away.
    "klient_neu": lambda d, marke: KL.neu_liste(dict(d, pf=_pf(d))),
    "klient_mail": lambda d, marke: KL.mail_zeigen(dict(d, pf=_pf(d))),
    "klient_flaggen": lambda d, marke: KL.flaggen(dict(d, pf=_pf(d))),
    "klient_verschieben": lambda d, marke: KL.verschieben(dict(d, pf=_pf(d))),
    "klient_loeschen": lambda d, marke: KL.loeschen(dict(d, pf=_pf(d))),
    "klient_ordner_neu": lambda d, marke: KL.ordner_neu(dict(d, pf=_pf(d))),
    # A whole folder, with its mail, its subfolders — and the watchman's memory
    # of it carried over in the same breath.
    "klient_ordner_ziehen": lambda d, marke: KL.ordner_ziehen(dict(d, pf=_pf(d))),
    # 🔴 The one action in the client that cannot be taken back — so the engine
    # asks back before it does it, with the number of mails in the question.
    "klient_ordner_loeschen": lambda d, marke: KL.ordner_loeschen(dict(d, pf=_pf(d))),
    "klient_vorlage": lambda d, marke: KL.vorlage(dict(d, pf=_pf(d))),
    "klient_senden": lambda d, marke: KL.senden(dict(d, pf=_pf(d))),
    "klient_entwurf": lambda d, marke: KL.entwurf_speichern(dict(d, pf=_pf(d))),
    "klient_adressbuch": lambda d, marke: KL.adressbuch(dict(d, pf=_pf(d))),
    "klient_smtp": lambda d, marke: KL.smtp_speichern(dict(d, pf=_pf(d))),
    "klient_smtp_pruefen": lambda d, marke: KL.smtp_pruefen(dict(d, pf=_pf(d))),
    # Filing by hand into the folder the WATCHMAN would have chosen — its map, one
    # click. Not automatic: it is still the reader who decides.
    "klient_wie_wache": lambda d, marke: _klient_wie_wache(d),
}


def _pf(d: dict) -> str:
    """Which mailbox this request is about. `do_POST` has already chosen it — this
    only passes it on, so that no client action chooses one for itself."""
    return aktives_pf(str((d or {}).get("pf") or ""))


def _klient_lage(d: dict, marke: str) -> dict:
    """The client's state — in two stages.

    🔴 Without a session ONLY the lock: whether a word is set, and nothing about
    the mailbox. Folder names alone would already say a lot about a person.
    """
    schloss = KL.lage_schloss(marke)
    if not schloss["an"]:
        return {"ok": True, "schloss": schloss, "postfaecher": [],
                "version": _version()}
    pf = _pf(d)
    lage = KL.lage(pf)
    lage["schloss"] = schloss
    lage["pf"] = pf
    lage["version"] = _version()
    lage["postfaecher"] = [{"id": f["id"], "name": f["name"], "adresse": f["adresse"],
                            "an": bool(f.get("an", True))} for f in pf_liste()]
    lage["schubladen"] = W.schubladen_namen() if W is not None else {}
    return lage


def _klient_wie_wache(d: dict) -> dict:
    """Move a mail where the watchman's learned map points — and refuse when it
    does not point anywhere. A guess would be a folder nobody asked for."""
    pf = _pf(d)
    urteil = KL.wache_urteil(pf, str(d.get("adresse") or ""))
    ziel = urteil.get("ordner") or ""
    if not ziel:
        return {"ok": False, "text": txt("k.wache_weiss_nicht")}
    erg = KL.verschieben(dict(d, pf=pf, ziel=ziel))
    erg["ziel"] = ziel
    erg["zeige"] = urteil.get("zeige") or ziel
    return erg


AKTIONEN = {
    "zugang": lambda d: zugang_speichern(d),
    "pruefen": lambda d: pruefen(str(d.get("adresse") or (W.zugang().get("adresse") if W else "")),
                                 str(d.get("passwort") or (W.zugang().get("passwort") if W else "")),
                                 str(d.get("server") or (W.zugang().get("server") if W else "")),
                                 int(d.get("port") or (W.zugang().get("port") if W else 0) or 993)),
    "einstellung": lambda d: einstellung_setzen(d),
    "notaus": lambda d: notaus_datei(bool(d.get("an"))),
    "schalter": lambda d: schalten(bool(d.get("an"))),
    "zurueck": lambda d: zuruecksortieren(d.get("eintraege") or []),
    "pruefen_jetzt": lambda d: jetzt_pruefen(),
    "telegram_zugang": lambda d: tg_zugang_speichern(d),
    "neu_lernen": neu_lernen,
    "ordner_anlegen": ordner_anlegen,
    "aufraeumen": aufraeumen,
    "statistik_neu": statistik_neu,
    "docusort": ds_zugang_speichern,
    "docusort_pruefen": ds_pruefen,
    "docusort_suchen": ds_suchen,
    "docusort_kopplung": ds_kopplung_einloesen,
    "dokumente_nachtragen": dokumente_nachtragen,
    # The single and the bulk button go through THE SAME function.
    "dokument_geben": dokumente_geben,
    "dokumente_geben": dokumente_geben,
    "dokumente_stand": dokumente_stand,
    "dokumente": dokumente_suchen,
    # ── seit 3.0.0 ──
    "postfach": postfach_speichern,
    "postfach_entfernen": postfach_entfernen,
    "postfach_schalten": postfach_schalten,
    "ki": ki_speichern,
    "ki_pruefen": ki_pruefen,
    "ki_suchen": ki_suchen,
    # ── since 4.0.0: the restructuring ──
    "umbau_lage": umbau_lage,
    "umbau": umbau_start,
    # ── since 4.4.0: migration to another provider ──
    "umzug_lage": umzug_lage,
    "umzug": umzug_start,
    "umzug_umleitung": umzug_umleitung,
    "ki_uebernehmen": ki_uebernehmen,
    "vorschlag_uebernehmen": vorschlag_uebernehmen,
    "vorschlag_verwerfen": vorschlag_verwerfen,
    "konfig": konfig_speichern,
}


# ── One-click setup for a local model ───────────────────────────
# The launcher is three lines: fetch the script, start the script, pass the
# Postwache's address. The actual work is in `ollama_einrichten.py` — ONE file
# serving all three systems instead of three that drift apart.
#
# 🔴 On macOS and Linux the launcher is packed into a ZIP. A browser throws away
# the execute bit when saving; inside a ZIP the mode survives, and without it
# macOS answers a double-click with „you don't have permission“. Windows does not
# need this — a .bat starts without the bit.
INSTALLER_SKRIPT = "ollama_einrichten.py"


def _als_zip(name: str, inhalt: str) -> bytes:
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w", zipfile.ZIP_DEFLATED) as z:
        eintrag = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
        eintrag.create_system = 3                 # Unix
        eintrag.external_attr = (0o755 << 16)     # rwxr-xr-x
        z.writestr(eintrag, inhalt)
    return puffer.getvalue()


def installer_bauen(system: str, herkunft: str):
    """(file name, type, content) for the chosen system.

    🔴 The launcher tidies up after itself. A downloaded script left lying under
    a random name in /tmp is exactly the kind of throwaway file nobody later
    remembers the purpose of."""
    kurz = herkunft.split("//")[-1].split(":")[0].replace("/", "") or "postwache"
    skript = herkunft + "/api/installer/skript"
    if system in ("mac", "macos", "darwin"):
        rumpf = "\n".join([
            "#!/bin/bash",
            "# Postwache - set up a local model (Ollama). Double-click me.",
            "# If macOS blocks the first start: right-click -> Open.",
            "set -e",
            'echo "Postwache - local model setup"',
            'ORDNER="$(mktemp -d -t postwache_ollama)"',
            "trap 'rm -rf \"$ORDNER\"' EXIT",
            'curl -fsSL "%s" -o "$ORDNER/ollama_einrichten.py"' % skript,
            '/usr/bin/env python3 "$ORDNER/ollama_einrichten.py" --postwache "%s"' % herkunft,
            "",
        ])
        return ("postwache-ollama-%s-mac.zip" % kurz, "application/zip",
                _als_zip("postwache-ollama-%s.command" % kurz, rumpf))
    if system == "linux":
        rumpf = "\n".join([
            "#!/bin/bash",
            "# Postwache - set up a local model (Ollama).",
            "set -e",
            'ORDNER="$(mktemp -d -t postwache_ollama.XXXXXX)"',
            "trap 'rm -rf \"$ORDNER\"' EXIT",
            'curl -fsSL "%s" -o "$ORDNER/ollama_einrichten.py"' % skript,
            'python3 "$ORDNER/ollama_einrichten.py" --postwache "%s"' % herkunft,
            "",
        ])
        return ("postwache-ollama-%s-linux.zip" % kurz, "application/zip",
                _als_zip("postwache-ollama-%s.sh" % kurz, rumpf))
    if system in ("win", "windows"):
        # 🔴 ONE percent sign. `%%TEMP%%` appears literally in a .bat — visible
        # only in the GENERATED file, never in the source here.
        rumpf = "\r\n".join([
            "@echo off",
            "REM Postwache -- set up a local model (Ollama). Double-click me.",
            "set ZIEL=%TEMP%\\postwache_ollama_einrichten.py",
            "powershell -NoProfile -Command \"Invoke-WebRequest '%s' -OutFile '%%ZIEL%%'\"" % skript,
            "if errorlevel 1 (echo Download failed.& pause & exit /b 1)",
            "python \"%%ZIEL%%\" --postwache \"%s\"" % herkunft,
            "del \"%ZIEL%\" >nul 2>&1",
            "pause",
            "",
        ])
        return ("postwache-ollama-%s.bat" % kurz, "application/x-bat",
                rumpf.encode("utf-8"))
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _sende(self, code, koerper, typ="application/json; charset=utf-8",
               zusatz=()):
        if isinstance(koerper, (dict, list)):
            koerper = json.dumps(koerper, ensure_ascii=False).encode("utf-8")
        elif isinstance(koerper, str):
            koerper = koerper.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(koerper)))
        self.send_header("Cache-Control", "no-store")
        for name, wert in zusatz or ():
            self.send_header(name, wert)
        self.end_headers()
        self.wfile.write(koerper)

    def _tls(self) -> bool:
        """Whether the reader is talking to us over TLS. Behind a terminating
        counterpart that is not visible in the connection, only in the header —
        and the `Secure` flag on the cookie depends on it: set on plain HTTP, the
        browser drops the cookie and nobody can log in."""
        schema = (self.headers.get("X-Forwarded-Proto") or "").split(",")[0].strip()
        return schema.lower() == "https"

    def _marke(self) -> str:
        return KL.keks_lesen(self.headers.get("Cookie") or "") if KL else ""

    def _gesperrt(self) -> bool:
        """Is the WATCHMAN page locked too? Off by default — whoever puts the page
        on the internet turns it on, and then it applies to the data as well, not
        just to the view. A lock in front of the page with an open `/api/lage`
        behind it would be decoration."""
        if KL is None:
            return False
        try:
            if not KL.einstellungen()["sperre_seite"] or not KL.wort_gesetzt():
                return False
        except Exception:
            return False
        return not KL.sitzung_gueltig(self._marke())

    def _umleiten(self, ziel: str):
        self.send_response(302)
        self.send_header("Location", ziel)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _seite(self, name: str, zusatz=()):
        try:
            with open(neben_dem_programm(name), encoding="utf-8") as fh:
                return self._sende(200, seite_mit_sprache(fh.read()),
                                   "text/html; charset=utf-8", zusatz)
        except OSError as e:
            return self._sende(500, "Seite fehlt: %s" % e,
                               "text/plain; charset=utf-8")

    def do_GET(self):
        # 🔴 The path WITHOUT the query part. Before, `self.path` was compared
        #    directly with "/" — every URL with a question mark („?standbild=1“, a
        #    cache buster, an appended referrer parameter) got a 404, and the
        #    screenshot showed `{"fehler": "not found"}` instead of the page.
        #    Found because the IMAGE was looked at, not the source.
        pfad = self.path.split("?", 1)[0]
        abfrage = urllib.parse.parse_qs(self.path.partition("?")[2])
        if pfad.startswith("/api/klient/"):
            return self._klient_datei(pfad, abfrage)
        if self.path.startswith("/api/lage"):
            if self._gesperrt():
                return self._sende(401, {"fehler": txt("k.bitte_anmelden"),
                                         "gesperrt": True})
            try:
                return self._sende(200, lage())
            except Exception as e:
                return self._sende(500, {"fehler": str(e)[:200]})
        if self.path.startswith("/api/installer"):
            return self._installer()
        if pfad.rstrip("/") in ("/post", "/postfach") and pfad != "/":
            return self._klient_seite(abfrage)
        if pfad in ("/", "/index.html"):
            if self._gesperrt():
                # The login form stands on the client page — one door, not two.
                return self._umleiten("/post?zurueck=/")
            # „Which page opens first" is a setting. Whoever lives in the mail
            # client should not have to click past the dashboard every time; the
            # way back is one link in the client's header.
            if (KL is not None and not abfrage.get("wache")
                    and KL.einstellungen()["startseite"] == "postfach"):
                return self._umleiten("/post")
            return self._seite("post_web.html")
        self._sende(404, {"fehler": txt("a.nicht_gefunden")})

    def _klient_seite(self, abfrage: dict):
        """Phone version or wide version.

        Order: what the URL asks for › what was chosen last › what the device
        looks like. 🔴 A choice made by hand is REMEMBERED (a cookie for a year) —
        without that, the link „wide view" works exactly once and every reload
        drops the reader back onto the phone page.
        """
        wunsch = str((abfrage.get("ansicht") or [""])[0]).lower()
        gemerkt = self._keks(ANSICHT_KEKS)
        kopf = self.headers.get("User-Agent") or ""
        if wunsch in ("breit", "mobil"):
            gewaehlt = wunsch
        elif gemerkt in ("breit", "mobil"):
            gewaehlt = gemerkt
        elif TELEFON.search(kopf) and not NICHT_TELEFON.search(kopf):
            gewaehlt = "mobil"
        else:
            gewaehlt = "breit"
        zusatz = []
        if wunsch in ("breit", "mobil"):
            zusatz.append(("Set-Cookie",
                           "%s=%s; Path=/; Max-Age=31536000; SameSite=Strict"
                           % (ANSICHT_KEKS, gewaehlt)))
        return self._seite(KLIENT_MOBIL if gewaehlt == "mobil" else KLIENT_SEITE,
                           zusatz)

    def _keks(self, name: str) -> str:
        for stueck in (self.headers.get("Cookie") or "").split(";"):
            schluessel, _, wert = stueck.strip().partition("=")
            if schluessel.strip() == name:
                return wert.strip()
        return ""

    def _klient_datei(self, pfad: str, abfrage: dict):
        """Attachment, inline image, source text — the three things a client cannot
        deliver as JSON.

        🔴 Behind the session, every one of them. An attachment link that works
        without the access word would be the hole the whole lock was built to
        close — and links get forwarded.
        """
        if KL is None:
            return self._sende(500, {"fehler": txt("k.klient_fehlt")})
        if not KL.sitzung_gueltig(self._marke()):
            return self._sende(401, {"fehler": txt("k.bitte_anmelden"),
                                     "gesperrt": True})

        def hol(name, vor=""):
            return str((abfrage.get(name) or [vor])[0])
        pf = aktives_pf(hol("pf"))
        ordner, uid = hol("ordner", "INBOX"), hol("uid", "0")
        try:
            uid = int(uid)
        except ValueError:
            return self._sende(400, {"fehler": "UID"})
        pf_waehlen(pf)
        try:
            art = pfad.rsplit("/", 1)[-1]
            if art == "bild":
                typ, roh = KL.bild(pf, ordner, uid, hol("cid"))
                if not roh:
                    return self._sende(404, {"fehler": txt("k.bild_weg")})
                return self._sende(200, roh, typ or "application/octet-stream")
            if art == "roh":
                roh = KL.roh_text(pf, ordner, uid)
                return self._sende(200, roh or b"", "text/plain; charset=utf-8")
            if art == "anhang":
                name, typ, roh = KL.anhang(pf, ordner, uid, hol("t", "1"),
                                           hol("k"), hol("n", "anhang"))
                if not roh:
                    return self._sende(404, {"fehler": txt("k.anhang_weg")})
                # 🔴 The file name goes into a HEADER. A line break in it would let
                # the sender of the mail write their own headers — so only what
                # cannot be one gets through.
                sicher = re.sub(r'[\r\n"\\]', "_", name)[:120]
                self.send_response(200)
                self.send_header("Content-Type", typ)
                self.send_header("Content-Length", str(len(roh)))
                self.send_header("Content-Disposition",
                                 'attachment; filename="%s"; filename*=UTF-8\'\'%s'
                                 % (sicher.encode("ascii", "replace").decode(),
                                    urllib.parse.quote(sicher)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return self.wfile.write(roh)
            return self._sende(404, {"fehler": txt("a.nicht_gefunden")})
        except Exception as e:
            return self._sende(500, {"fehler": str(e)[:200]})
        finally:
            pf_waehlen("")

    def _installer(self):
        """Deliver the setup helper — as source or as a finished launcher.

        🔴 There is no secret in it (unlike DocuSort's bridge, which passes an
        access key): the launcher carries only this Postwache's address. Still
        `no-store` — a cached address would simply be wrong after a move."""
        pfad, _, abfrage = self.path.partition("?")
        if pfad.rstrip("/") == "/api/installer/skript":
            try:
                with open(neben_dem_programm(INSTALLER_SKRIPT), encoding="utf-8") as fh:
                    return self._sende(200, fh.read(), "text/x-python; charset=utf-8")
            except OSError as e:
                return self._sende(404, "Einrichter fehlt: %s" % e,
                                   "text/plain; charset=utf-8")
        system = (urllib.parse.parse_qs(abfrage).get("os") or ["mac"])[0].lower()
        # Behind a TLS-terminating counterpart the scheme is not http.
        schema = (self.headers.get("X-Forwarded-Proto") or "http").split(",")[0].strip()
        wirt = self.headers.get("Host") or ("127.0.0.1:%d" % PORT)
        gebaut = installer_bauen(system, "%s://%s" % (schema, wirt))
        if gebaut is None:
            return self._sende(400, {"fehler": "unbekanntes System: %s" % system})
        name, typ, inhalt = gebaut
        self.send_response(200)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(inhalt)))
        self.send_header("Content-Disposition", 'attachment; filename="%s"' % name)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(inhalt)

    def do_POST(self):
        name = self.path.rsplit("/", 1)[-1]
        if name not in AKTIONEN and name not in KLIENT_AKTIONEN:
            return self._sende(404, {"ok": False, "text": "unbekannte Aktion"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = 0
        if n > POST_DECKEL:
            # An attachment arrives as base64 inside the request. Without a ceiling
            # ONE request is enough to exhaust the memory of a Raspberry Pi — and
            # that would take the watchman down with it.
            return self._sende(413, {"ok": False, "text": txt("k.zu_gross")})
        try:
            d = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            d = {}
        if name in KLIENT_AKTIONEN:
            return self._klient_aktion(name, d if isinstance(d, dict) else {})
        try:
            d = d if isinstance(d, dict) else {}
            # 🔑 ONE place chooses the mailbox — before every action. Had every
            # action done it itself, the one that forgets would be exactly the one
            # writing into the wrong folder.
            klient_merken(self.client_address[0] if self.client_address else "")
            gewaehlt = aktives_pf(d.get("pf") or "")
            if d.get("pf") and d["pf"] != (st("ansicht.json", {}) or {}).get("pf"):
                pf_merken(gewaehlt)
            pf_waehlen(gewaehlt)
            try:
                return self._sende(200, AKTIONEN[name](d))
            finally:
                pf_waehlen("")
                klient_merken("")
        except Exception as e:
            return self._sende(200, {"ok": False, "text": str(e)[:200]})

    def _klient_aktion(self, name: str, d: dict):
        """Every client action goes through here — and through the door first.

        🔑 ONE gate for all of them. Had each action asked for itself whether
        somebody is logged in, the one that forgets would be exactly the one that
        hands out the mail.
        """
        if KL is None:
            return self._sende(500, {"ok": False, "text": txt("k.klient_fehlt")})
        marke = self._marke()
        if name not in KLIENT_OFFEN and not KL.sitzung_gueltig(marke):
            return self._sende(200, {"ok": False, "gesperrt": True,
                                     "text": txt("k.bitte_anmelden")})
        adresse = self.client_address[0] if self.client_address else ""
        klient_merken(adresse)
        gewaehlt = aktives_pf(str(d.get("pf") or ""))
        if d.get("pf") and d["pf"] != (st("ansicht.json", {}) or {}).get("pf"):
            pf_merken(gewaehlt)
        pf_waehlen(gewaehlt)
        try:
            if name == "klient_anmelden":
                erg = KL.wort_pruefen(str(d.get("wort") or ""), adresse)
                if not erg.get("ok"):
                    return self._sende(200, erg)
                neue = KL.sitzung_neu(adresse)
                return self._sende(200, dict(erg, schloss=KL.lage_schloss(neue)),
                                   zusatz=[("Set-Cookie",
                                            KL.keks_setzen(neue, self._tls()))])
            if name == "klient_abmelden":
                return self._sende(200, KL.sitzung_beenden(marke),
                                   zusatz=[("Set-Cookie",
                                            KL.keks_setzen("", self._tls()))])
            return self._sende(200, KLIENT_AKTIONEN[name](d, marke))
        except Exception as e:
            log_fehler(name, e)
            return self._sende(200, {"ok": False, "text": str(e)[:200]})
        finally:
            pf_waehlen("")
            klient_merken("")


def log_fehler(name: str, e: Exception) -> None:
    """A client error belongs in the log, not only on the page — the page is gone
    with the tab, and then nobody knows what happened."""
    if W is not None:
        W.log("Klient %s: %s: %s" % (name, type(e).__name__, str(e)[:160]))


def takt_faden(sekunden: int) -> None:
    """Give the watchman its own beat instead of waiting for a cron.

    🔴 Only when explicitly wanted (`POSTWACHE_TAKT`). On a machine with cron this
    would be a SECOND beat — two runs at once on the same mailbox, and one would
    write the state out from under the other. In a container there is no cron;
    there this thread is the beat.
    """
    import threading

    def schleife():
        time.sleep(5)                     # make the page reachable first
        while True:
            t0 = time.time()
            try:
                if W is not None:
                    W.main()
            except Exception as e:
                print("Waechterlauf fehlgeschlagen: %s" % str(e)[:200], flush=True)
            # The gap measured from the END of the run, not the start: a run that
            # takes longer than the interval must not overtake itself.
            time.sleep(max(5.0, sekunden - (time.time() - t0)))

    f = threading.Thread(target=schleife, name="postwache-takt", daemon=True)
    f.start()
    print("Waechter laeuft intern alle %d s" % sekunden, flush=True)


def main():
    os.makedirs(OUT, exist_ok=True)
    # 🔑 Before anything else: if the environment carries a DocuSort access,
    # write it down. That is what makes the pairing free of clicks when both
    # programs were installed together.
    try:
        meldung = ds_aus_umgebung()
        if meldung:
            print(meldung, flush=True)
    except Exception as e:
        print("DocuSort-Zugang aus der Umgebung ging nicht: %s" % str(e)[:200],
              flush=True)
    takt = int(os.environ.get("POSTWACHE_TAKT") or 0)
    if takt > 0:
        takt_faden(takt)
    print("Postwache-Seite auf :%d" % PORT, flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
