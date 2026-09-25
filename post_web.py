#!/usr/bin/env python3
"""Postwache — Uebersichtsseite (Port 8110).

Zeigt, was der Waechter eingeordnet hat und WARUM, traegt den Notaus, nimmt den
Postfach-Zugang entgegen und macht jede Verschiebung wieder rueckgaengig.

🔴 Die Seite ist die einzige Stelle, an der Zugangsdaten ENTGEGENGENOMMEN werden —
sie gibt nie welche heraus. `/api/lage` liefert fuer das Passwort ausschliesslich
ein true/false. Wer die Seite oeffnet, sieht also, DASS ein Postfach eingerichtet
ist, aber nie womit.

🔴 Der Nachrichtentext wird nirgends gespeichert und deshalb auch hier nirgends
angezeigt. Sichtbar sind Betreff, Absender und die Begruendung der Einordnung —
genug zum Nachvollziehen, nicht genug, um den Posteingang auf einer Webseite
ohne Anmeldung auszubreiten.
"""
from __future__ import annotations

import imaplib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")
# Wo die Postwache wohnt. Im Container ist das ein eingehaengtes Verzeichnis,
# auf einem Rechner der gewachsene Pfad — beides ohne Codeaenderung.
BASE = os.path.abspath(os.environ.get("POSTWACHE_HOME")
                       or os.path.join(HOME, "scripts", "postwache"))
STATE = os.path.join(BASE, "state")
OUT = os.path.join(BASE, "out")

# 🔴 Programm und Zustand sind ZWEI Orte, auch wenn sie bei einer gewachsenen
# Installation derselbe sind. Im Container liegt der Code in /app und der
# Zustand in einem eingehaengten /data — wer die Seite oder die Versionsnummer
# im Zustandsordner sucht, findet dort nichts und meldet „HTTP 500" bzw. „?".
PROG = os.path.dirname(os.path.abspath(__file__))


def neben_dem_programm(name: str) -> str:
    """Erst neben dem Programm, sonst im Zustandsordner. Die zweite Stelle ist
    der gewachsene Fall, bei dem beides nebeneinanderliegt."""
    q = os.path.join(PROG, name)
    return q if os.path.isfile(q) else os.path.join(BASE, name)

DISABLED = os.path.join(BASE, "DISABLED")
ENVFILE = os.path.join(BASE, "ha.env")   # nur Rueckfall; konfig.json gilt
HA = "http://127.0.0.1:8123"
SCHALTER = "input_boolean.postwache_aktiv"
PORT = int(os.environ.get("POSTWACHE_WEB_PORT", "8110"))

sys.path.insert(0, BASE)
try:
    import postwache as W            # eine Quelle fuer Schubladen und Regeln
except Exception:                    # die Seite darf nie am Waechter scheitern
    W = None


def _version() -> str:
    try:
        with open(neben_dem_programm("VERSION"), encoding="utf-8") as fh:
            return fh.read().strip() or "?"
    except OSError:
        return "?"


SCHUBLADEN = (W.SCHUBLADEN if W else {})
ALARM = (W.ALARM if W else ())
LAERM = (W.LAERM if W else ())


# ── Lesen / Schreiben ─────────────────────────────────────────────────────────
def token(key: str = "HA_TOKEN") -> str:
    v = os.environ.get(key)
    if v:
        return v.strip()
    # Dieselbe Quelle wie der Waechter — zwei Meinungen darueber, wo der
    # Zugang liegt, waeren eine Seite, die den Schalter nicht findet, den der
    # Waechter sehr wohl liest.
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
    """Zustand lesen — durch den Waechter, nicht am ihm vorbei.

    🔴 Seit 3.0.0 liegt der Zustand eines Postfachs unter `state/pf/<id>/`. Die
    Seite hatte dafuer eine eigene, flache Lesefunktion; haette sie die
    behalten, saehe sie nach dem Umzug ueberall leere Dateien und wuerde das
    als „noch nichts passiert" anzeigen. Es gibt jetzt EINE Stelle, die weiss,
    wo eine Datei liegt, und die steht im Waechter.
    """
    if W is not None:
        return W.load(name, default)
    return lade(os.path.join(STATE, name), default)


def pf_waehlen(pf_id=""):
    """Welches Postfach die folgenden Lese- und Schreibzugriffe meinen."""
    if W is not None:
        W.pf_waehlen(pf_id or "")


def pf_liste():
    return W.postfaecher() if W is not None else []


def aktives_pf(wunsch=""):
    """Welches Postfach die Seite gerade zeigt.

    Reihenfolge: was der Aufruf mitbringt › was zuletzt gewaehlt wurde › das
    erste in der Liste. 🔴 Ein unbekannter Wunsch faellt auf das erste zurueck
    statt ins Leere zu zeigen — sonst saehe man nach dem Loeschen eines
    Postfachs eine leere Seite ohne Erklaerung.
    """
    liste = pf_liste()
    if not liste:
        return ""
    pf_waehlen("")                       # ansicht.json ist global
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
    """Die Texttabelle in die Seite legen, bevor sie ausgeliefert wird.

    🔴 Nicht per zweitem Abruf: die Seite wuerde sonst einen Wimpernschlag lang
    in Schluesseln dastehen („kopf.titel" statt „Postwache"), und genau dieser
    Wimpernschlag ist das, was man auf einem Telefon sieht. Die Tabelle steht im
    ersten Byte der Antwort.
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

    # Wieviel WUERDE aussortiert — die Zahl, an der der Besitzer den Lernlauf misst.
    wuerde = sum(1 for e in koepfe[-400:] if e.get("wuerde_nach"))
    journal = [j for j in jsonl("journal.jsonl", 400) if not j.get("zurueck")]

    return {
        "version": _version(),
        "status": status,
        # Welche Postfaecher es gibt und welches gerade gezeigt wird. Ohne
        # Passwoerter — die verlassen die 0600-Datei nie.
        "postfaecher": [{"id": f["id"], "name": f["name"], "adresse": f["adresse"],
                         "server": f["server"], "port": f["port"], "an": f["an"],
                         "passwort_gesetzt": bool(f["passwort"])}
                        for f in pf_liste()],
        "pf": gewaehlt,
        # `historie` bleibt draussen: 40 Zeitstempel, aus denen die Seite nichts
        # baut — der Takt ist daraus schon gerechnet (`takt_s`).
        "lauf": {k: v for k, v in lauf.items()
                 if k not in ("fehler", "historie")} | {
            "fehler": str(lauf.get("fehler") or "")},
        "eingerichtet": bool(zug.get("adresse") and zug.get("passwort")),
        "adresse": str(zug.get("adresse") or ""),
        "server": str(zug.get("server") or ""),
        # 🔴 Nur ob, nie was.
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
        # 🔑 Nur die Kurzfassung aus `out/dokumente.json` — der Index selbst hat
        # Zehntausende Eintraege und hat auf einer Seite, die sich alle 30 s
        # holt, nichts verloren. Gesucht wird ueber /api/dokumente.
        "dokumente": lade(os.path.join(OUT, "dokumente.json"), {}),
        "docusort": ds_kurz(),
        "ki": ki_kurz(),
        "vorschlaege": (vorschlaege_lesen().get("liste") or [])[:40],
        "konfig": konfig_kurz(),
        "heimatlos": heimatlose(),
        # 🔴 Nicht SCHUBLADEN direkt: die Namen darin sind der deutsche
        # Rueckfall. Uebersetzt wird an EINER Stelle, im Waechter.
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
    """Was der Waechter aus dessen Ordnern gelernt hat — in der Kurzfassung.

    Die vollstaendige Landkarte hat ein paar hundert Eintraege; auf die Seite
    gehoert nur, was man auch liest: wie viel gelernt wurde, welche Ordner es
    gibt, und woran die Ablage haekelt.
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
        # Ordner mit Anzahl, groesste zuerst — das ist die Kategorienliste.
        "ordner": sorted(ordner.items(), key=lambda kv: -kv[1]),
        "schwaechen": {
            "uneindeutig": (schw.get("uneindeutige_absender") or [])[:12],
            "uneindeutig_gesamt": len(schw.get("uneindeutige_absender") or []),
            "fast_leer": schw.get("fast_leere_ordner") or [],
            "leer": schw.get("leere_ordner") or [],
        },
    }


def heimatlose(grenze: int = 2) -> list:
    """Absender, die immer wieder schreiben und fuer die es KEIN Ziel gibt.

    🔑 Das ist die eigentliche Antwort auf „der Bot hat ja gar nichts sortiert".
    Gemessen am 12.09.2026 am echten Posteingang: von 22 Mails scheiterten nur
    2 an einer Schwelle — bei 20 gab es schlicht nichts, wonach sich der
    Waechter richten koennte. Er kann nur NACHAHMEN, und wo der Besitzer nie etwas
    abgelegt hat, gibt es nichts nachzuahmen.

    Ein Vorschlag ist keine Handlung: hier steht nur, wer auffaellig oft ohne
    Zuhause ankommt. Entschieden wird mit einem Klick.
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
            continue                       # hat ein Zuhause, alles gut
        klassen = e.get("klassen") if isinstance(e.get("klassen"), dict) else {}
        haupt = max(klassen.items(), key=lambda kv: kv[1])[0] if klassen else ""
        raus.append({
            "adresse": adr,
            "name": str(e.get("name") or ""),
            "anzahl": n,
            "zuletzt": str(e.get("zuletzt") or ""),
            "klasse": haupt,
            # Wenn eine Stufe etwas VORSCHLAEGT, aber nicht handeln darf,
            # gehoert der Vorschlag hierher — ein Klick macht ihn dauerhaft.
            "vorschlag": ziel or "",
            "vorschlag_grund": warum if ziel else "",
            "neuer_ordner": ordnername_vorschlagen(adr, karte),
        })
    return sorted(raus, key=lambda e: -e["anzahl"])[:25]


def ordnername_vorschlagen(adresse: str, karte: dict) -> str:
    """Ein Ordnername aus der Adresse — als ausgefuellter Textkasten, nicht als
    Entscheidung. Der Besitzer ueberschreibt ihn, wenn er etwas anderes will.

    Genommen wird die Hauptstufe der Domain (`hedinautomotive` aus
    `katarina.nikolic@hedinautomotive.de`), weil die den Absender benennt und
    nicht den einzelnen Menschen dahinter.
    """
    dom = (adresse or "").partition("@")[2]
    teile = [t for t in dom.split(".") if t]
    kern = teile[-2] if len(teile) >= 2 else (teile[0] if teile else "")
    kern = re.sub(r"[^A-Za-z0-9 -]", "", kern).strip()
    if not kern:
        return ""
    name = kern[:1].upper() + kern[1:]
    # Unter „Shopping" liegt bei der Besitzer alles Gekaufte — dort einzureihen ist
    # naeher an seiner Gewohnheit als ein neuer Ordner auf oberster Ebene.
    ordner = karte.get("ordner") or {}
    return name if name in ordner else name


def nachziehen(adresse: str, ziel: str) -> dict:
    """Die Post, die SCHON im Posteingang liegt, dem neuen Ziel nachschicken.

    🔑 Ohne das bleibt eine frisch gesetzte Regel für der Besitzer wirkungslos. Er
    sagte am 12.09.2026: „postwache erstellt zwar ordner aber die mails aus dem
    posteingang verschiebt es dann aber nicht automatisch dorthin." Genau so
    war es: `Seal-82` und `Hedinautomotive` waren angelegt und leer, während
    ihre 4 bzw. 9 Mails im Posteingang lagen. Der Wächter liest nur NEUE Post
    (`UID letzte+1:*`) — was schon da ist, sieht er nie wieder.

    Eine Regel zu setzen ist eine Aussage über DIESEN ABSENDER, nicht über den
    Zeitpunkt. Also gilt sie auch rückwärts.
    """
    if not W:
        return {"bewegt": 0, "text": ""}
    einst = W.einstellungen()
    if not einst.get("scharf"):
        return {"bewegt": 0, "text": " (Lernlauf — es wird nichts verschoben.)"}
    if os.path.exists(DISABLED):
        return {"bewegt": 0, "text": " (Notaus ist gesetzt.)"}
    adresse = (adresse or "").strip().lower()
    if not adresse or not ziel:
        return {"bewegt": 0, "text": ""}
    bewegt = liegen = 0
    try:
        with W.Postfach(W.zugang(), True) as pf:
            voll = pf.voller_name(ziel)
            pf.m.select("INBOX", readonly=False)
            typ, dat = pf.m.uid("search", None, "HEADER", "FROM", '"%s"' % adresse)
            if typ != "OK" or not dat or not dat[0]:
                return {"bewegt": 0, "text": " Im Posteingang lag davon nichts."}
            for uid in [int(x) for x in dat[0].split()]:
                try:
                    msg, text = pf.kopf_und_text(uid)
                except Exception:
                    continue
                if msg is None:
                    continue
                kopf = W.kopf_lesen(msg, text)
                # 🔴 Die IMAP-Suche trifft auf TEILZEICHENKETTE. Bevor etwas
                # bewegt wird, muss die Adresse GENAU stimmen — sonst wandert
                # fremde Post mit, nur weil sie den Namen im Kopf trägt.
                if (kopf.get("adresse") or "").lower() != adresse:
                    continue
                urteil = W.einordnen(kopf, text)
                # Dieselben Riegel wie im Wächter, an derselben Stelle — eine
                # zweite, abweichende Fassung wäre der nächste Fehler.
                if urteil.get("phishing"):
                    liegen += 1
                    continue
                if einst.get("wichtiges_bleibt") and urteil["klasse"] in W.ALARM:
                    liegen += 1
                    continue
                if pf.verschieben(uid, voll):
                    bewegt += 1
                    W.anhaengen("journal.jsonl", {
                        "zeit": datetime.now().isoformat(timespec="seconds"),
                        "uid": uid, "von": "INBOX", "nach": voll, "anzeige": ziel,
                        "klasse": urteil["klasse"], "betreff": kopf["betreff"],
                        "absender": kopf["adresse"], "zurueck": False},
                        W.JOURNAL_ZEILEN)
    except Exception as e:
        return {"bewegt": 0, "text": " Nachziehen fehlgeschlagen: %s" % str(e)[:120]}
    if bewegt:
        W.chronik("nachgezogen", titel="Regel r\u00fcckwirkend angewandt",
                  detail="%d Mail(s) von %s nach %s verschoben."
                         % (bewegt, adresse, ziel))
    teile = []
    if bewegt:
        teile.append("%d Mail(s) aus dem Posteingang gleich mitgenommen." % bewegt)
    if liegen:
        teile.append("%d blieb(en) liegen (gesch\u00fctzt)." % liegen)
    if not teile:
        teile.append("Im Posteingang lag davon nichts.")
    return {"bewegt": bewegt, "text": " " + " ".join(teile)}


def ordner_anlegen(d: dict) -> dict:
    """Einen neuen Ordner im Postfach anlegen UND den Absender daran binden.

    🔴 Der WAECHTER legt weiterhin nie einen Ordner an — das bleibt so. Was hier
    anlegt, ist dessen Klick. Deshalb steht das im Webteil und nicht im
    Waechter: eine Struktur zu aendern ist eine Entscheidung, keine Ableitung.
    """
    if not W:
        return {"ok": False, "text": "Waechter nicht ladbar."}
    name = str(d.get("ordner") or "").strip().strip("/")
    adresse = str(d.get("adresse") or "").strip().lower()
    if not name:
        return {"ok": False, "text": "Kein Ordnername angegeben."}
    if len(name) > 60 or re.search(r"[\\\"\x00-\x1f]", name):
        return {"ok": False, "text": "Dieser Ordnername geht nicht."}
    zug = W.zugang()
    if not zug.get("adresse"):
        return {"ok": False, "text": "Kein Postfach eingerichtet."}
    try:
        with W.Postfach(zug, True) as pf:
            vorhanden = set(pf.ordner_liste())
            # Der Trenner wird beim Server ERFRAGT (dem Anbieter: „."). Wer ihn raet,
            # legt einen Ordner mit Punkt im Namen an statt einen Unterordner.
            voll = name.replace("/", pf.trenner)
            if voll in vorhanden:
                angelegt = False
            else:
                typ, antw = pf.m.create(pf._zitat(voll))
                if typ != "OK":
                    return {"ok": False,
                            "text": "Postfach lehnt ab: %s"
                                    % (antw[0].decode(errors="replace")[:120]
                                       if antw else "?")}
                try:
                    pf.m.subscribe(pf._zitat(voll))
                except Exception:
                    pass
                angelegt = True
            k = W.ablage_lernen(pf)          # damit der Ordner sofort bekannt ist
        W.save(W.ABLAGE, k)
    except Exception as e:
        return {"ok": False, "text": "Anlegen fehlgeschlagen: %s" % str(e)[:160]}
    text = ("Ordner %s angelegt." % voll) if angelegt else ("Ordner %s gibt es schon." % voll)
    if adresse:
        antwort = einstellung_setzen({"absender": {"adresse": adresse, "ordner": voll}})
        if not antwort.get("ok"):
            return {"ok": False, "text": text + " " + str(antwort.get("text") or "")}
        # Die Rueckmeldung des Nachziehens durchreichen — sonst erfaehrt der Besitzer
        # nicht, ob die vorhandene Post mitgekommen ist.
        text += " Post von %s geht ab sofort dorthin." % adresse
        rest = str(antwort.get("text") or "").replace("Gespeichert.", "", 1).strip()
        if rest:
            text += " " + rest
    return {"ok": True, "text": text}


def statistik_neu(_d) -> dict:
    """Die Zahlen sofort neu rechnen. READONLY — eine Statistik fasst nichts an."""
    if not W:
        return {"ok": False, "text": "Waechter nicht ladbar."}
    zug = W.zugang()
    if not zug.get("adresse"):
        return {"ok": False, "text": "Kein Postfach eingerichtet."}
    try:
        with W.Postfach(zug, False) as pf:      # readonly
            k = W.statistik_lernen(pf)
        W.save(W.STATISTIK, k)
        return {"ok": True,
                "text": "%d Mails aus %d Ordnern gelesen (%s bis %s), "
                        "Schnitt %.1f pro Tag."
                        % (k["mails"], k["ordner"], k["von"], k["bis"],
                           k["schnitt_pro_tag"])}
    except Exception as e:
        return {"ok": False, "text": "Statistik fehlgeschlagen: %s" % str(e)[:160]}


def neu_lernen(_d) -> dict:
    """Die Landkarte sofort neu bauen. Dauert Sekunden bis Minuten — deshalb
    laeuft es sonst nur einmal am Tag."""
    if not W:
        return {"ok": False, "text": "Waechter nicht ladbar."}
    zug = W.zugang()
    if not zug.get("adresse"):
        return {"ok": False, "text": "Kein Postfach eingerichtet."}
    try:
        with W.Postfach(zug, False) as pf:     # READONLY — Lernen veraendert nie etwas
            k = W.ablage_lernen(pf)
        W.save(W.ABLAGE, k)
        return {"ok": True,
                "text": "%d Mails aus %d Ordnern gelesen, %d Absender eindeutig "
                        "(%.0f%% Deckung)."
                        % (k["mails"], len(k["ordner"]), len(k["absender"]),
                           k["deckung"])}
    except Exception as e:
        return {"ok": False, "text": "Lernen fehlgeschlagen: %s" % str(e)[:160]}


def tg_lage() -> dict:
    """Welcher Bot funkt gerade — und aus welcher Quelle.

    🔴 Der Token selbst kommt hier NIE heraus, nur der Name, den Telegram zu
    ihm nennt. Der Name ist die einzige Rueckmeldung, die der Besitzer braucht, um zu
    sehen, dass der richtige Bot eingetragen ist.
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
            name = "(Telegram antwortet nicht)"
    return {"eigen": eigen, "bot": name, "chat_gesetzt": bool(chat),
            "chat": chat if chat else ""}


def tg_zugang_speichern(d: dict) -> dict:
    """Eigenen Bot eintragen. 0600, und sofort geprueft.

    🔴 Ein Bot darf nicht zuerst schreiben: Telegram antwortet mit
    `chat not found`, solange der Mensch den Bot nicht selbst gestartet hat.
    Das ist kein Fehler der Einrichtung, sondern ein fehlender Schritt — und
    die Meldung sagt genau das, statt auf die Chat-ID zu zeigen.
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
        return {"ok": False, "text": "Ohne Bot-Token geht es nicht."}
    if not chat:
        return {"ok": False, "text": "Ohne Chat-ID weiss er nicht, wen er anfunken soll."}
    # 1) Gehoert der Token zu einem echten Bot?
    try:
        with urllib.request.urlopen(
                "https://api.telegram.org/bot%s/getMe" % tok, timeout=12) as r:
            g = json.loads(r.read().decode("utf-8", "replace"))
        if not g.get("ok"):
            return {"ok": False, "text": "Telegram kennt diesen Token nicht."}
        name = "@" + (g.get("result") or {}).get("username", "?")
    except urllib.error.HTTPError as e:
        return {"ok": False, "text": "Telegram lehnt den Token ab (HTTP %s)." % e.code}
    except Exception as e:
        return {"ok": False, "text": "Telegram nicht erreichbar: %s" % str(e)[:120]}
    # 2) Erreicht er der Besitzer auch wirklich?
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
                    "text": "%s erreicht dich noch nicht. Oeffne den Bot in Telegram "
                            "und druecke einmal Start — vorher darf er dir nicht "
                            "schreiben." % name}
        return {"ok": False, "text": "%s kann nicht senden: %s" % (name, grund[:140])}
    except Exception as e:
        return {"ok": False, "text": "Senden fehlgeschlagen: %s" % str(e)[:140]}
    schreibe("telegram_zugang.json",
             {"TELEGRAM_BOT_TOKEN": tok, "TELEGRAM_CHAT_ID": chat}, 0o600)
    return {"ok": True, "text": "%s ist eingetragen und hat dir gerade geschrieben." % name}


# ── Aktionen ──────────────────────────────────────────────────────────────────
def postfach_speichern(d: dict) -> dict:
    """Ein Postfach anlegen oder aendern. Die Verbindung wird SOFORT geprueft —
    ein Zugang, der erst beim naechsten Lauf auffaellt, laesst einen im Glauben,
    es sei eingerichtet.

    🔴 Die Kennung (`id`) ist der Ordnername des Zustands und wird beim Anlegen
    EINMAL vergeben. Sie spaeter zu aendern wuerde den gesamten gelernten Stand
    dieses Postfachs unerreichbar machen, ohne dass jemand es merkt — deshalb
    laesst sich nur der Anzeigename aendern.
    """
    pf_waehlen("")
    liste = list(pf_liste())
    pid = re.sub(r"[^a-zA-Z0-9_-]", "", str(d.get("id") or "")).strip()
    adresse = str(d.get("adresse") or "").strip()
    vorhanden = next((f for f in liste if f["id"] == pid), None) if pid else None

    if not vorhanden:
        if not adresse or "@" not in adresse:
            return {"ok": False, "text": "Das sieht nicht nach einer Adresse aus."}
        if not pid:
            stamm = re.sub(r"[^a-z0-9]", "", adresse.split("@")[0].lower()) or "postfach"
            pid, n = stamm, 2
            while any(f["id"] == pid for f in liste):
                pid, n = "%s%d" % (stamm, n), n + 1
    if vorhanden and not adresse:
        adresse = vorhanden["adresse"]

    passwort = str(d.get("passwort") or "") or (vorhanden or {}).get("passwort") or ""
    if not passwort:
        return {"ok": False, "text": "Ohne Passwort geht es nicht."}
    server = (str(d.get("server") or "").strip()
              or (vorhanden or {}).get("server") or W._server_raten(adresse))
    try:
        port = int(d.get("port") or (vorhanden or {}).get("port") or 993)
    except (TypeError, ValueError):
        port = 993

    probe = pruefen(adresse, passwort, server, port)
    if not probe["ok"]:
        return probe

    eintrag = {"id": pid, "name": str(d.get("name") or "").strip() or adresse,
               "adresse": adresse, "passwort": passwort, "server": server,
               "port": port, "an": bool(d.get("an", (vorhanden or {}).get("an", True)))}
    liste = [e for e in liste if e["id"] != pid] + [eintrag]
    schreibe("postfaecher.json", {"liste": liste}, 0o600)
    return {"ok": True, "text": "Verbunden — %s. %s" % (adresse, probe["text"]),
            "id": pid}


def postfach_entfernen(d: dict) -> dict:
    """Ein Postfach aus der Liste nehmen.

    🔴 Der gelernte Zustand unter `state/pf/<id>/` bleibt LIEGEN. Ihn
    mitzuloeschen waere ein unumkehrbarer Klick: Monate an gelernter Ablage,
    Absenderprofilen und Anhangindex waeren weg, weil jemand kurz aufraeumen
    wollte. Wer den Platz braucht, loescht den Ordner von Hand.
    """
    pf_waehlen("")
    pid = str(d.get("id") or "").strip()
    liste = [e for e in pf_liste() if e["id"] != pid]
    if len(liste) == len(pf_liste()):
        return {"ok": False, "text": "Dieses Postfach gibt es nicht."}
    schreibe("postfaecher.json", {"liste": liste}, 0o600)
    return {"ok": True, "text": "Entfernt. Der gelernte Stand bleibt erhalten — "
                                "wer das Postfach wieder anlegt, ist sofort wieder da."}


def postfach_schalten(d: dict) -> dict:
    """Ein Postfach ruhen lassen, ohne es zu verlieren."""
    pf_waehlen("")
    pid = str(d.get("id") or "").strip()
    liste = pf_liste()
    if not any(e["id"] == pid for e in liste):
        return {"ok": False, "text": "Dieses Postfach gibt es nicht."}
    for e in liste:
        if e["id"] == pid:
            e["an"] = bool(d.get("an"))
    schreibe("postfaecher.json", {"liste": liste}, 0o600)
    return {"ok": True, "text": "Eingeschaltet." if d.get("an") else "Ruht."}


# ── Urteilshilfe ─────────────────────────────────────────────────────────────
def ki_kurz() -> dict:
    """Was die Seite ueber die Urteilshilfe wissen darf. 🔴 Nie den Schluessel,
    nur ob einer hinterlegt ist."""
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
            return {"ok": False, "text": "Diesen Anbieter kenne ich nicht."}
        # Anbieterwechsel setzt Adresse und Modell auf die Vorgaben des neuen —
        # sonst bliebe die Ollama-Adresse stehen, wenn jemand auf OpenAI wechselt.
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
    schreibe("ki.json", k, 0o600)          # 🔴 0600 wie jeder andere Zugang
    return {"ok": True, "text": "Gespeichert.", "ki": ki_kurz()}


def ki_pruefen(_d=None) -> dict:
    """Eine echte, winzige Frage an das eingestellte Modell. Nicht „erreichbar",
    sondern „antwortet" — ein Dienst, der 200 auf die Startseite gibt, aber das
    Modell nicht kennt, waere sonst gruen."""
    if W is None:
        return {"ok": False, "text": "Waechter nicht geladen."}
    ok, grund = W.ki_bereit()
    if not ok:
        return {"ok": False, "text": grund}
    if W.ki_konfig()["anbieter"] == "werkstatt":
        return {"ok": True, "text": "Werkstatt-Ordner erreichbar."}
    t0 = time.time()
    try:
        antwort = W.ki_fragen("Antworte mit genau einem Wort.", "Sag: bereit")
    except Exception as e:
        return {"ok": False, "text": "Keine Antwort: %s" % str(e)[:160]}
    dauer = time.time() - t0
    kurz = (antwort or "").strip().splitlines()[0][:60] if antwort else ""
    if not kurz:
        return {"ok": False, "text": "Das Modell hat nichts geantwortet."}
    return {"ok": True, "text": "Antwortet in %.1f s: „%s\u201c" % (dauer, kurz)}


def vorschlaege_lesen() -> dict:
    v = lade(os.path.join(OUT, "vorschlaege.json"), {})
    return v if isinstance(v, dict) else {}


def vorschlag_uebernehmen(d: dict) -> dict:
    """Einen Vorschlag zu einer eigenen Regel machen — durch dieselbe Tuer, durch
    die auch eine von Hand gesetzte Regel geht."""
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
    """Die Umgebung: Seitenadresse, Home Assistant, Werkstatt. Wer hier nichts
    eintraegt, bekommt weiter das, was erkannt wurde."""
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
            return {"ok": False, "text": "Diese Sprache kenne ich nicht."}
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
        W._KONFIG_ZWISCHEN = None          # sofort wirksam, nicht erst beim Neustart
    return {"ok": True, "text": "Gespeichert.", "konfig": konfig_kurz()}


def konfig_kurz() -> dict:
    if W is None:
        return {}
    k = W.konfig()
    return {"sprache": k["sprache"], "seite": k["seite"], "imap_server": k["imap_server"],
            "werkstatt": k["werkstatt"], "ha_url": k["ha"]["url"],
            "ha_token_datei": k["ha"]["token_datei"], "ha_schalter": k["ha"]["schalter"],
            "ha_an": W.ha_an()}


def zugang_speichern(d: dict) -> dict:
    """Der alte Weg aus 2.x — er schrieb `zugang.json`, die seit der Migration
    niemand mehr liest.

    🔴 Ihn stehen zu lassen waere eine Falle: der Aufruf haette geantwortet
    „gespeichert" und nichts bewirkt. Er geht deshalb durch dieselbe Tuer wie
    alles andere und legt das Postfach in die Liste.
    """
    daten = dict(d)
    daten.setdefault("id", (W.zugang() or {}).get("id", "") if W else "")
    return postfach_speichern(daten)


def pruefen(adresse: str, passwort: str, server: str, port: int) -> dict:
    """Einmal anmelden und wieder gehen. READONLY: eine Pruefung darf im
    Postfach nichts veraendern, nicht einmal den Gelesen-Status."""
    try:
        socket.setdefaulttimeout(25)
        m = imaplib.IMAP4_SSL(server, port)
        try:
            m.login(adresse, passwort)
            typ, daten = m.select("INBOX", readonly=True)
            anzahl = int(daten[0]) if typ == "OK" and daten and daten[0] else 0
            return {"ok": True, "text": "%d Mails im Posteingang." % anzahl}
        finally:
            try:
                m.logout()
            except Exception:
                pass
    except imaplib.IMAP4.error as e:
        return {"ok": False, "text": "Postfach lehnt ab: %s" % str(e)[:180]}
    except Exception as e:
        return {"ok": False, "text": "Keine Verbindung: %s" % str(e)[:180]}


def einstellung_setzen(d: dict) -> dict:
    # Merkt sich, ob in diesem Aufruf eine Absenderregel ENTSTANDEN ist. Muss
    # vorbelegt sein — sonst bricht jedes normale Speichern mit UnboundLocalError.
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
            # 🔴 Nur in einen Ordner, den es WIRKLICH gibt. Der Waechter legt
            # keine Ordner an, und eine Regel auf ein Ziel, das es nicht gibt,
            # wuerde bei jedem Lauf still scheitern.
            bekannt = (st("ablage.json", {}) or {}).get("ordner") or {}
            if ordner not in bekannt:
                return {"ok": False,
                        "text": "Den Ordner %s gibt es in deinem Postfach nicht." % ordner}
            ar[a] = ordner
            # 🔑 An DIESER einen Stelle laufen alle Wege zusammen, auf denen eine
            # Absenderregel entsteht: der Knopf neben der Mail, „Hierhin" in der
            # Heimatlosen-Karte und „Anlegen" (das ruft hier herein). Das
            # Nachziehen gehört deshalb hierher und nicht an die Knöpfe —
            # sonst vergisst der nächste Weg es wieder.
            nachziehen_an = (a, ordner)
        elif a:
            ar.pop(a, None)
        e["absender_regeln"] = ar
    schreibe("einstellungen.json", e)
    if nachziehen_an:
        return {"ok": True,
                "text": "Gespeichert." + nachziehen(*nachziehen_an)["text"]}
    return {"ok": True, "text": "Gespeichert."}


def notaus_datei(an: bool) -> dict:
    if an:
        with open(DISABLED, "w", encoding="utf-8") as fh:
            fh.write("von der Uebersichtsseite gesetzt %s\n"
                     % datetime.now().isoformat(timespec="seconds"))
        return {"ok": True, "text": "Notaus gesetzt — der Waechter ruehrt sich nicht mehr."}
    try:
        os.remove(DISABLED)
    except OSError:
        pass
    return {"ok": True, "text": "Notaus aufgehoben."}


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
    """Verschiebungen rueckgaengig machen. Der Weg ZURUECK muss immer offen sein —
    sonst waere „erst Lernlauf, dann scharf" eine Einbahnstrasse."""
    if not W:
        return {"ok": False, "text": "Waechter nicht ladbar."}
    zug = W.zugang() if W else {}
    if not zug.get("adresse"):
        return {"ok": False, "text": "Kein Postfach eingerichtet."}
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
        return {"ok": False, "text": "Postfach: %s" % str(e)[:160]}
    try:
        tmp = os.path.join(OUT, "journal.jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(json.dumps(j, ensure_ascii=False) for j in journal) + "\n")
        os.replace(tmp, os.path.join(OUT, "journal.jsonl"))
    except OSError:
        pass
    return {"ok": getan > 0 or not fehler,
            "text": "%d Mail(s) zurueck in den Posteingang%s."
                    % (getan, ", %d fehlgeschlagen" % fehler if fehler else "")}


def aufraeumen(d: dict) -> dict:
    """Den Posteingang EINMAL durchgehen — auch das, was schon dort liegt.

    🔑 Ohne das bleibt dessen eigentliches Aergernis unberuehrt. Der Waechter
    liest im Normalbetrieb nur NEUE Mails (`UID letzte+1:*`); alles, was vor
    seiner Einrichtung ankam oder was er unter alten Regeln liegen liess, ist
    fuer ihn fuer immer Vergangenheit. Genau die 22 Mails, die der Besitzer am
    12.09.2026 vor sich sah, lagen hinter dem Zeiger.

    🔴 Es wird NICHT gemeldet. Ein Aufraeumen ist kein Ereignis: 22 Weckrufe
    fuer Post, die seit Tagen daliegt, waeren Laerm und kein Dienst. Der
    UID-Zeiger wird ebenfalls NICHT angefasst — dieser Lauf ist ein Zusatz,
    keine Ersetzung des normalen Betriebs.

    Der Riegel ist derselbe wie im Waechter: nur dorthin, wo der Besitzer selbst
    schon abgelegt hat oder wo sein Ordner den Absender beim Namen nennt.
    """
    if not W:
        return {"ok": False, "text": "Waechter nicht ladbar."}
    einst = W.einstellungen()
    if not einst.get("scharf"):
        return {"ok": False,
                "text": "Er ist im Lernlauf. Erst „Wirklich sortieren\u201c einschalten."}
    if os.path.exists(DISABLED):
        return {"ok": False, "text": "Notaus ist gesetzt."}
    zug = W.zugang()
    if not zug.get("adresse"):
        return {"ok": False, "text": "Kein Postfach eingerichtet."}
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
        return {"ok": False, "text": "Aufr\u00e4umen fehlgeschlagen: %s" % str(e)[:160]}
    rest = ", ".join("%d\u00d7 %s" % (n, k) for k, n in
                     sorted(blieb.items(), key=lambda kv: -kv[1])[:4])
    W.chronik("aufgeraeumt", titel="Posteingang aufger\u00e4umt",
              detail="%d von %d Mails einsortiert. Liegen geblieben: %s."
                     % (bewegt, gesehen, rest or "nichts"))
    return {"ok": True,
            "text": "%d von %d Mails einsortiert. Liegen geblieben: %s."
                    % (bewegt, gesehen, rest or "nichts")}


def jetzt_pruefen() -> dict:
    try:
        r = subprocess.run(["/usr/bin/python3", os.path.join(BASE, "postwache.py")],
                           capture_output=True, text=True, timeout=180)
        letzte = (r.stdout or "").strip().splitlines()
        return {"ok": r.returncode == 0,
                "text": letzte[-1] if letzte else "Lauf beendet (rc=%s)." % r.returncode}
    except subprocess.TimeoutExpired:
        return {"ok": False, "text": "Lauf dauert zu lange — laeuft im Hintergrund weiter."}
    except Exception as e:
        return {"ok": False, "text": str(e)[:160]}


# ── Dokumente in der Post ────────────────────────────────────────────────────
def ds_kurz() -> dict:
    """Was von DocuSort auf die Seite darf. 🔴 Das Passwort NIE — nur, DASS
    eines hinterlegt ist. Dieselbe Regel wie beim Postfach."""
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


def ds_zugang_speichern(d: dict) -> dict:
    z = st("docusort.json", {})
    if not isinstance(z, dict):
        z = {}
    if "url" in d:
        url = str(d.get("url") or "").strip().rstrip("/")
        if url and not url.startswith("https://"):
            # 🔴 Nur HTTPS. Das Passwort dieses Zugangs geht ueber diese
            # Verbindung; DocuSort spricht ohnehin nur TLS (http:// gibt dort
            # gar keine Antwort).
            return {"ok": False, "text": "Die Adresse muss mit https:// beginnen."}
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
    schreibe("docusort.json", z, 0o600)      # 🔴 0600, wie der Postfach-Zugang
    return {"ok": True, "text": "Gespeichert."}


def ds_pruefen(_d=None) -> dict:
    """Einmal wirklich anmelden. Ein gespeicherter Zugang, der nicht geht, ist
    schlimmer als keiner — er sieht auf der Seite genauso aus."""
    if not W:
        return {"ok": False, "text": "Waechter nicht ladbar."}
    ds, grund = W.ds_bereit()
    if ds is None:
        return {"ok": False, "text": "DocuSort ist %s." % grund}
    v = ds.version()
    if not v:
        return {"ok": False, "text": "Nicht erreichbar: %s" % (ds.fehler or "?")}
    fehl = ds.anmelden()
    if fehl:
        return {"ok": False, "text": fehl}
    return {"ok": True, "text": "DocuSort %s erreichbar, Anmeldung als %s gilt."
                                % (v, ds.benutzer)}


def dokumente_suchen(d: dict) -> dict:
    if not W:
        return {"gesamt": 0, "treffer": [], "fehler": "Waechter nicht ladbar."}
    idx = W.anhang_index()
    erg = W.dokumente_suchen(idx, str(d.get("q") or ""),
                             str(d.get("art") or "dokument"),
                             str(d.get("von") or ""), str(d.get("bis") or ""),
                             grenze=int(d.get("grenze") or 200))
    erg["docusort_url"] = ds_kurz()["url"]
    return erg


def dokumente_stand(_d=None) -> dict:
    """Bei DocuSort nachfragen, wie weit es ist — JETZT, nicht erst beim
    naechsten Minutenlauf.

    🔑 Abgehakt wird nichts, weil der Upload geklappt hat, sondern erst, wenn
    DocuSort das Dokument bestaetigt. Genau dafuer fragt die Seite nach, solange
    noch etwas unterwegs ist."""
    if not W:
        return {"ok": False, "text": "Waechter nicht ladbar."}
    ds, grund = W.ds_bereit()
    if ds is None:
        return {"ok": False, "text": "DocuSort ist %s." % grund}
    idx = W.anhang_index()
    try:
        n = W.ds_stand_nachtragen(ds, idx, grenze=200)
    except Exception as e:
        return {"ok": False, "text": "Nachfrage fehlgeschlagen: %s" % str(e)[:160]}
    if n:
        W.anhang_index_sichern(idx)
    z = W.dokument_zaehlung(idx)
    # 🔴 Die Kurzfassung IMMER nachziehen, auch wenn sich nichts geaendert hat:
    # die Seite sieht nur sie. Hinkt sie hinterher, zeigt die Karte „nichts
    # unterwegs", obwohl etwas unterwegs ist — und fragt deshalb nie nach.
    idx["zahlen"] = z
    W.dokument_kurz_schreiben(idx)
    return {"ok": True, "geaendert": n,
            "unterwegs": z["unterwegs"], "uebergeben": z["uebergeben"],
            "text": ("%d Dokument(e) noch unterwegs." % z["unterwegs"])
                    if z["unterwegs"] else "DocuSort ist durch."}


def dokumente_nachtragen(d: dict) -> dict:
    """Den Rueckstand jetzt lesen statt beim naechsten Lauf. READONLY — ein
    Nachtrag fasst im Postfach nichts an."""
    if not W:
        return {"ok": False, "text": "Waechter nicht ladbar."}
    zug = W.zugang()
    if not zug.get("adresse"):
        return {"ok": False, "text": "Kein Postfach eingerichtet."}
    try:
        idx = W.anhang_index()
        with W.Postfach(zug, False) as pf:          # readonly
            erg = W.anhaenge_nachtragen(pf, idx, frist=int(d.get("frist") or 60))
        W.anhang_index_sichern(idx)
        return {"ok": True,
                "text": "%d Ordner, %d Mails angesehen, %d neu im Index"
                        "%s (%.0f s)."
                        % (erg["ordner"], erg["mails"], erg["neu"],
                           "" if not erg["offen"] else
                           ", %d Ordner noch offen" % erg["offen"],
                           erg["dauer"])}
    except Exception as e:
        return {"ok": False, "text": "Nachtrag fehlgeschlagen: %s" % str(e)[:160]}


# Grenzen der Sammel-Uebergabe. 🔴 Beide sind noetig und meinen Verschiedenes:
# die ZAHL schuetzt DocuSort (jedes Dokument kostet dort OCR und ein Urteil),
# die ZEIT schuetzt den Browser, der auf die Antwort wartet. Was nicht mehr
# reingeht, bleibt ausgewaehlt stehen und wird beim naechsten Druck geholt —
# es verschwindet nicht still.
DOK_SAMMEL_MAX = 60
DOK_SAMMEL_FRIST = 150

DS_KLARTEXT = {"zu_gross": "zu gross", "phishing": "Phishing-Verdacht",
               "kann_docusort_nicht": "Dateityp nimmt DocuSort nicht",
               "fehler": "fehlgeschlagen", "finanzen": "in die Finanzen",
               "doppelt": "hatte DocuSort schon", "abgelegt": "abgelegt",
               "pruefen": "wartet auf Pruefung"}


def dokumente_geben(d: dict) -> dict:
    """Ausgewaehlte Mails an DocuSort geben — einzeln oder gesammelt.

    Das ist der „im Nachgang"-Weg: der Index weiss, in welchem Ordner die Mail
    liegt und welcher Teil das Dokument ist — geholt wird erst jetzt und nur
    dieser eine Teil.

    🔑 EINE Verbindung, EINE Anmeldung, EIN Sichern des Index fuer den ganzen
    Stapel. Und nur EINE Umsetzung: der Knopf an der einzelnen Mail kommt hier
    mit einer Liste der Laenge 1 herein. Zwei Wege, die dasselbe tun sollen,
    laufen irgendwann auseinander."""
    if not W:
        return {"ok": False, "text": "Waechter nicht ladbar."}
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
    # Neue Uebergabe-Vermerke erkennt man am Zeitstempel: alles ab JETZT ist aus
    # diesem Aufruf. Ohne das zaehlt man die Ergebnisse frueherer Laeufe mit.
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
        W.anhang_index_sichern(idx)       # was schon drueben ist, bleibt vermerkt
        return {"ok": False, "text": "Uebergabe abgebrochen nach %d Dokument(en): %s"
                                     % (gegeben, str(ex)[:140])}
    W.anhang_index_sichern(idx)
    teile = []
    if gegeben:
        teile.append("%d Dokument(e) aus %d Mail(s) an DocuSort gegeben"
                     % (gegeben, mails))
    for stand, n in sorted(staende.items(), key=lambda kv: -kv[1]):
        if stand == "uebergeben":
            continue                      # steht schon in der ersten Zeile
        teile.append("%d × %s" % (n, DS_KLARTEXT.get(stand, stand)))
    if rest:
        teile.append("%d Mail(s) blieben uebrig (Grenze) — einfach noch einmal" % rest)
    if umgezogen:
        teile.append("%d gerade umgezogen, der naechste Nachtrag findet sie" % umgezogen)
    if unbekannt:
        teile.append("%d nicht mehr im Index" % unbekannt)
    if not teile:
        teile.append("Nichts zu uebergeben — alles war schon drueben")
    return {"ok": bool(gegeben), "text": ". ".join(teile) + "."}


# ── HTTP ──────────────────────────────────────────────────────────────────────
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
    "dokumente_nachtragen": dokumente_nachtragen,
    # Einzel- und Sammelknopf gehen durch DIESELBE Funktion.
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
    "vorschlag_uebernehmen": vorschlag_uebernehmen,
    "vorschlag_verwerfen": vorschlag_verwerfen,
    "konfig": konfig_speichern,
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _sende(self, code, koerper, typ="application/json; charset=utf-8"):
        if isinstance(koerper, (dict, list)):
            koerper = json.dumps(koerper, ensure_ascii=False).encode("utf-8")
        elif isinstance(koerper, str):
            koerper = koerper.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", typ)
        self.send_header("Content-Length", str(len(koerper)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(koerper)

    def do_GET(self):
        if self.path.startswith("/api/lage"):
            try:
                return self._sende(200, lage())
            except Exception as e:
                return self._sende(500, {"fehler": str(e)[:200]})
        if self.path in ("/", "/index.html"):
            try:
                with open(neben_dem_programm("post_web.html"), encoding="utf-8") as fh:
                    return self._sende(200, seite_mit_sprache(fh.read()),
                                       "text/html; charset=utf-8")
            except OSError as e:
                return self._sende(500, "Seite fehlt: %s" % e, "text/plain; charset=utf-8")
        self._sende(404, {"fehler": "nicht gefunden"})

    def do_POST(self):
        name = self.path.rsplit("/", 1)[-1]
        if name not in AKTIONEN:
            return self._sende(404, {"ok": False, "text": "unbekannte Aktion"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            d = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            d = {}
        try:
            d = d if isinstance(d, dict) else {}
            # 🔑 EINE Stelle waehlt das Postfach — vor jeder Aktion. Haette
            # jede Aktion das selbst getan, waere die eine, die es vergisst,
            # genau die, die in den falschen Ordner schreibt.
            gewaehlt = aktives_pf(d.get("pf") or "")
            if d.get("pf") and d["pf"] != (st("ansicht.json", {}) or {}).get("pf"):
                pf_merken(gewaehlt)
            pf_waehlen(gewaehlt)
            try:
                return self._sende(200, AKTIONEN[name](d))
            finally:
                pf_waehlen("")
        except Exception as e:
            return self._sende(200, {"ok": False, "text": str(e)[:200]})


def takt_faden(sekunden: int) -> None:
    """Den Waechter selbst takten, statt auf einen Cron zu warten.

    🔴 Nur, wenn ausdruecklich gewuenscht (`POSTWACHE_TAKT`). Auf einem Rechner
    mit Cron waere das ein ZWEITER Takt — zwei Laeufe gleichzeitig auf demselben
    Postfach, und der eine schriebe dem anderen den Stand unter den Fuessen weg.
    Im Container gibt es keinen Cron, dort ist dieser Faden der Takt.
    """
    import threading

    def schleife():
        time.sleep(5)                     # die Seite zuerst erreichbar machen
        while True:
            t0 = time.time()
            try:
                if W is not None:
                    W.main()
            except Exception as e:
                print("Waechterlauf fehlgeschlagen: %s" % str(e)[:200], flush=True)
            # Abstand vom ENDE des Laufs, nicht vom Anfang: ein Lauf, der laenger
            # dauert als der Takt, soll sich nicht selbst ueberholen.
            time.sleep(max(5.0, sekunden - (time.time() - t0)))

    f = threading.Thread(target=schleife, name="postwache-takt", daemon=True)
    f.start()
    print("Waechter laeuft intern alle %d s" % sekunden, flush=True)


def main():
    os.makedirs(OUT, exist_ok=True)
    takt = int(os.environ.get("POSTWACHE_TAKT") or 0)
    if takt > 0:
        takt_faden(takt)
    print("Postwache-Seite auf :%d" % PORT, flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
