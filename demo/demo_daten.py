#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Baut eine Postwache mit ERFUNDENEN Daten — fuer Screenshots und zum Ausprobieren.

    python3 demo/demo_daten.py /tmp/postwache-demo
    POSTWACHE_HOME=/tmp/postwache-demo python3 post_web.py

Es wird kein Postfach angefasst und nichts abgerufen: die Datei schreibt genau
den Zustand, den ein paar Wochen Betrieb hinterlassen haetten. Alle Namen,
Adressen und Betreffzeilen sind ausgedacht.
"""
import json, os, random, sys
from datetime import datetime, timedelta

ZIEL = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "/tmp/postwache-demo")
STATE, OUT = os.path.join(ZIEL, "state"), os.path.join(ZIEL, "out")
PF = os.path.join(STATE, "pf", "privat")
for d in (STATE, OUT, PF, os.path.join(STATE, "pf", "buero")):
    os.makedirs(d, exist_ok=True)
random.seed(20260925)
JETZT = datetime(2026, 9, 25, 19, 40)


def schreib(pfad, daten, modus=0o644):
    tmp = pfad + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, modus)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(daten, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, pfad)


def zeilen(pfad, saetze):
    with open(pfad, "w", encoding="utf-8") as fh:
        for s in saetze:
            fh.write(json.dumps(s, ensure_ascii=False) + "\n")


# ── die erfundene Welt ───────────────────────────────────────────────────────
ABSENDER = [
    # (Adresse, Name, Klasse, Ordner, wie oft)
    ("rechnung@stadtwerke-elmbruck.de", "Stadtwerke Elmbruck", "frist", "Rechnungen", 24),
    ("service@nordlicht-versicherung.de", "Nordlicht Versicherung", "amt", "Versicherung", 11),
    ("post@kreissparkasse-elmbruck.de", "Kreissparkasse Elmbruck", "amt", "Bank", 18),
    ("buergerbuero@elmbruck.de", "Stadt Elmbruck", "amt", "Behoerden", 6),
    ("versand@paket24.example", "Paket24", "automatisch", "Shopping.Paket24", 63),
    ("newsletter@gartenhaus-weber.example", "Gartenhaus Weber", "newsletter", "Newsletter", 41),
    ("angebote@elektro-lindner.example", "Elektro Lindner", "werbung", "Newsletter", 37),
    ("sekretariat@schule-am-anger.example", "Schule Am Anger", "mensch", "Schule", 9),
    ("nas01@heimnetz.example", "NAS01", "automatisch", "Technik.Sicherungen", 88),
    ("nas02@heimnetz.example", "NAS02", "automatisch", "Technik.Sicherungen", 86),
    ("no-reply@quellwerk.example", "Quellwerk", "unklar", "", 14),
    ("hinweis@leitungsdienst.example", "Leitungsdienst", "unklar", "", 8),
    ("m.kessler@dachdecker-kessler.example", "Martin Kessler", "mensch", "Handwerker", 5),
    ("sicherheit@kontoschutz.example", "Kontoschutz", "sicherheit", "", 3),
]
BETREFF = {
    "frist": ["Ihre Abschlagsrechnung {m} 2026", "Jahresabrechnung Strom 2025",
              "Zahlungserinnerung — Vertrag 44-20871"],
    "amt": ["Beitragsanpassung zum 01.01.2027", "Ihr Kontoauszug {m} 2026",
            "Bescheid ueber Grundsteuer 2026", "Neue Nachricht im Postfach"],
    "automatisch": ["[heimnetz]Sicherung abgeschlossen", "Ihre Sendung ist unterwegs",
                    "Zustellung fuer heute angekuendigt", "[heimnetz]Pruefung ohne Befund"],
    "newsletter": ["Herbst im Garten — 12 Ideen", "Unser Newsletter {m}",
                   "Neu eingetroffen: Hochbeete"],
    "werbung": ["-20 % auf alles bis Sonntag", "Nur heute: Aktionswoche"],
    "mensch": ["Elternabend am 8. Oktober", "Kurze Rueckfrage zum Angebot",
               "Termin naechste Woche?"],
    "unklar": ["Ihre Anfrage 2026-{n}", "Wichtige Information", "Statusmeldung {n}"],
    "sicherheit": ["Ungewoehnliche Anmeldung bemerkt", "Bitte bestaetigen Sie Ihr Konto"],
}
MONATE = ["Januar", "Februar", "Maerz", "April", "Mai", "Juni", "Juli",
          "August", "September", "Oktober", "November", "Dezember"]
GRUND = {"frist": "Betreff nennt eine Zahlung", "amt": "Absender ist eine Behoerde oder Bank",
         "automatisch": "Maschinenpost — fester Absender, fester Betreff",
         "newsletter": "Kopfzeile List-Unsubscribe", "werbung": "Werbewortmuster im Betreff",
         "mensch": "Kein Rundschreiben, persoenliche Anrede",
         "unklar": "Kein Muster getroffen", "sicherheit": "Sicherheitswortmuster"}
ORDNER = {}
for _a, _n, _k, o, _c in ABSENDER:
    if o:
        ORDNER[o] = ORDNER.get(o, 0) + _c

# ── koepfe: was er zuletzt eingeordnet hat ───────────────────────────────────
koepfe, uid = [], 41200
for i in range(260):
    adresse, name, klasse, ordner, _ = random.choice(ABSENDER)
    v = BETREFF[klasse]
    betreff = random.choice(v).format(m=random.choice(MONATE), n=random.randint(1000, 9999))
    wann = JETZT - timedelta(minutes=random.randint(3, 60 * 24 * 12))
    uid += random.randint(1, 4)
    laerm = klasse in ("newsletter", "werbung", "automatisch")
    koepfe.append({
        "uid": uid, "klasse": klasse, "grund": GRUND[klasse],
        "phishing": "Absender gibt sich als Bank aus" if klasse == "sicherheit" and i % 3 == 0 else "",
        "kopf": {"betreff": betreff, "name": name, "adresse": adresse,
                 "datum": wann.isoformat(timespec="seconds"),
                 "bulk": laerm, "bulk_grund": "List-Unsubscribe" if laerm else "",
                 "message_id": "<demo%05d@beispielhaus.de>" % i},
        "ziel": ordner, "ziel_sicher": random.choice([88, 92, 96, 100]) if ordner else 0,
        "ziel_grund": ("So legst du Post von diesem Absender ab" if ordner
                       else "Kein Ordner gelernt — bleibt liegen"),
        "ziel_darf": bool(ordner), "gesehen": wann.isoformat(timespec="seconds"),
        "verschoben_nach": ordner if (ordner and laerm) else "",
    })
koepfe.sort(key=lambda e: e["gesehen"])
schreib(os.path.join(PF, "koepfe.json"), koepfe)

# ── Ablage: die gelernte Landkarte ───────────────────────────────────────────
# 🔴 `ordner` ist eine flache Zaehlung {Name: Zahl}, kein verschachteltes
# Woerterbuch — die Seite sortiert danach (`-kv[1]`) und waere an einem dict
# mit „bad operand type for unary -" gescheitert. Genau deshalb wird die Demo
# gegen die ECHTE Struktur gebaut und nicht gegen eine vermutete.
schreib(os.path.join(PF, "ablage.json"), {
    "gelernt": JETZT.isoformat(timespec="seconds"), "dauer": 6.7, "mails": 4820,
    "deckung": 91.4,
    "ordner": {o: n for o, n in sorted(ORDNER.items())},
    "namen": {o: o.replace(".", " / ") for o in ORDNER},
    "absender": {a: {"ordner": o, "treffer": max(2, c - 2), "gesamt": c}
                 for a, _n, _k, o, c in ABSENDER if o},
    "domain": {a.split("@")[1]: {"ordner": o, "treffer": max(2, c - 2), "gesamt": c}
               for a, _n, _k, o, c in ABSENDER if o},
    "haupt": {}, "v_absender": {}, "v_domain": {}, "v_haupt": {},
    "schwaechen": {},
})

# ── Absenderprofile ──────────────────────────────────────────────────────────
schreib(os.path.join(PF, "absender.json"), {
    a: {"n": c, "zuletzt": (JETZT - timedelta(hours=random.randint(1, 400))).isoformat(timespec="seconds"),
        "name": n, "klassen": {k: c}}
    for a, n, k, _o, c in ABSENDER})

# ── Statistik ────────────────────────────────────────────────────────────────
tage = [{"tag": (JETZT - timedelta(days=d)).strftime("%Y-%m-%d"),
         "n": max(0, int(random.gauss(11, 4)))} for d in range(89, -1, -1)]
schreib(os.path.join(PF, "statistik.json"), {
    "gebaut": JETZT.isoformat(timespec="seconds"), "dauer": 9.2, "mails": 4820,
    "eigene_ausgelassen": 612, "ordner": 9, "von": "2019-04-02", "bis": "2026-09-25",
    "vollstaendig_ab": "2024-01-01", "schnitt_pro_tag": 10.7, "tage_gemessen": 90,
    "je_tag": tage,
    "je_stunde": [1, 0, 0, 0, 1, 3, 12, 28, 46, 61, 58, 44, 39, 47, 52, 49, 41, 33, 26, 19, 12, 7, 4, 2],
    "je_wochentag": [128, 141, 136, 133, 119, 47, 29],
    "top_absender": [{"adresse": a, "name": n, "n": c,
                      "zuletzt": (JETZT - timedelta(days=random.randint(0, 9))).strftime("%Y-%m-%d")}
                     for a, n, _k, _o, c in sorted(ABSENDER, key=lambda x: -x[4])[:8]],
})

# ── Anhaenge und Dokumente ───────────────────────────────────────────────────
dok = [("Abschlagsrechnung-09-2026.pdf", "Stadtwerke Elmbruck", "Rechnungen", 148230),
       ("Beitragsanpassung-2027.pdf", "Nordlicht Versicherung", "Versicherung", 203440),
       ("Kontoauszug-2026-08.pdf", "Kreissparkasse Elmbruck", "Bank", 96110),
       ("Grundsteuerbescheid-2026.pdf", "Stadt Elmbruck", "Behoerden", 312880),
       ("Elternabend-Einladung.pdf", "Schule Am Anger", "Schule", 88420)]
eintraege = {}
for i, (datei, wer, ordner, groesse) in enumerate(dok):
    eintraege["m%02d" % i] = {
        "ordner": ordner, "uid": 900 + i,
        "betreff": datei.replace("-", " ").replace(".pdf", ""),
        "absender": wer.lower().replace(" ", ".") + "@beispiel.example",
        "name": wer, "datum": (JETZT - timedelta(days=i * 6)).isoformat(timespec="seconds"),
        "dateien": [{"n": datei, "art": "dokument", "b": groesse, "t": "application/pdf", "nr": "2"}],
        "ds": [{"n": datei, "stand": "abgelegt", "doc": str(510 + i), "text": ordner,
                "zeit": (JETZT - timedelta(days=i * 6)).isoformat(timespec="seconds")}] if i < 3 else [],
    }
schreib(os.path.join(PF, "anhaenge.json"), {
    "stand": JETZT.isoformat(timespec="seconds"), "eintraege": eintraege,
    "nachtrag": {"zeit": JETZT.isoformat(timespec="seconds"), "ordner": 9, "mails": 4820,
                 "neu": 0, "offen": 0, "dauer": 6.1},
    "gesichert": JETZT.isoformat(timespec="seconds"),
    "zahlen": {"mails": 412, "dokumente": 286, "uebergeben": 3, "unterwegs": 0}})
schreib(os.path.join(OUT, "dokumente.json"),
        {"mails": 412, "dokumente": 286, "uebergeben": 3, "unterwegs": 0,
         "ordner": 9, "ordner_offen": 0, "zeit": JETZT.isoformat(timespec="seconds"),
         "nachtrag": {"zeit": JETZT.isoformat(timespec="seconds"), "ordner": 9,
                      "mails": 4820, "neu": 0, "offen": 0, "dauer": 6.1}})

# ── Zaehler, Lauf, Status ────────────────────────────────────────────────────
heute = {"frist": 2, "amt": 3, "mensch": 1, "automatisch": 9, "newsletter": 5,
         "werbung": 4, "unklar": 2}
schreib(os.path.join(PF, "zaehler.json"), {"tag": JETZT.strftime("%Y-%m-%d"),
                                           "weckrufe": 0, "heute": heute, "dokumente": 3})
schreib(os.path.join(PF, "lauf.json"), {
    "uid": uid, "fehler": "", "grund": "", "dauer": 0.4,
    "zeit": JETZT.isoformat(timespec="seconds"),
    "historie": [(JETZT - timedelta(minutes=m)).isoformat(timespec="seconds")
                 for m in range(0, 90)]})
schreib(os.path.join(STATE, "pf", "buero", "lauf.json"), {"uid": 0, "fehler": "", "grund": ""})
schreib(os.path.join(OUT, "status.json"), {
    "aktiv": True, "eingerichtet": True, "scharf": True, "postfaecher_gesamt": 2,
    "neu": 26, "verschoben": 18, "gemeldet": 6, "unklar": 2, "dokumente": 3,
    "postfaecher": {"privat": {"aktiv": True, "eingerichtet": True, "scharf": True,
                               "neu": 26, "uid": uid,
                               "zeit": JETZT.isoformat(timespec="seconds"), "version": "3.0.0"}},
    "zeit": JETZT.isoformat(timespec="seconds"), "version": "3.0.0"})

# ── Journal und Chronik ──────────────────────────────────────────────────────
zeilen(os.path.join(OUT, "journal.jsonl"), [
    {"zeit": (JETZT - timedelta(minutes=i * 13)).isoformat(timespec="seconds"),
     "uid": 41000 + i, "von": "INBOX", "nach": "INBOX." + e["ziel"], "anzeige": e["ziel"],
     "klasse": e["klasse"], "betreff": e["kopf"]["betreff"],
     "absender": e["kopf"]["adresse"], "zurueck": False}
    for i, e in enumerate([k for k in koepfe if k["verschoben_nach"]][-40:])])
zeilen(os.path.join(OUT, "chronik.jsonl"), [
    {"zeit": (JETZT - timedelta(hours=h)).isoformat(timespec="seconds"), "art": art,
     "titel": t, "detail": d, "n": 1}
    for h, (art, t, d) in enumerate([
        ("sortiert", "Aussortiert", "18 Mail(s) aus dem Posteingang in Unterordner verschoben."),
        ("dokumente", "Dokumente an DocuSort", "3 Dokument(e) aus neuer Post uebergeben."),
        ("vorschlaege", "2 Regelvorschlaege", "Aus 9 unklaren Mails. Auf der Seite ansehen und einzeln uebernehmen."),
        ("sortiert", "Aussortiert", "23 Mail(s) aus dem Posteingang in Unterordner verschoben."),
        ("kaltstart", "Kaltstart", "300 Mails als Ausgangslage eingeordnet — nichts gemeldet, nichts verschoben."),
    ])])

# ── global: Postfaecher, Einstellungen, Urteilshilfe, Vorschlaege ────────────
schreib(os.path.join(STATE, "postfaecher.json"), {"liste": [
    {"id": "privat", "name": "Privat", "adresse": "post@beispielhaus.example",
     "passwort": "demo", "server": "imap.beispielhaus.example", "port": 993, "an": True},
    {"id": "buero", "name": "Buero", "adresse": "buero@beispielhaus.example",
     "passwort": "demo", "server": "imap.beispielhaus.example", "port": 993, "an": False},
]}, 0o600)
schreib(os.path.join(STATE, "einstellungen.json"), {
    "scharf": True, "telegram": True, "bericht_stunde": 7, "wichtiges_bleibt": False,
    "regeln": {k: {"melden": True} for k in ("frist", "amt", "sicherheit", "mensch")},
    "absender_regeln": {"versand@paket24.example": "Shopping.Paket24"}})
schreib(os.path.join(STATE, "ki.json"), {"anbieter": "ollama",
                                         "url": "http://127.0.0.1:11434",
                                         "modell": "llama3.1:8b"}, 0o600)
schreib(os.path.join(STATE, "konfig.json"), {"seite": "http://heimserver:8110",
                                             "ha": {"url": "", "token_datei": "", "schalter": ""},
                                             "werkstatt": "", "imap_server": ""})
schreib(os.path.join(STATE, "docusort.json"), {"url": "https://docusort.heimnetz.example:9876",
                                               "benutzer": "postwache", "passwort": "demo",
                                               "aktiv": True, "max_mb": 25.0}, 0o600)
schreib(os.path.join(OUT, "vorschlaege.json"), {
    "zeit": JETZT.isoformat(timespec="seconds"), "quelle": "ollama", "liste": [
        {"absender": "no-reply@quellwerk.example", "schublade": "automatisch",
         "warum": "Immer derselbe Absender, immer dieselbe Betreffform — Maschinenpost.",
         "sicher": 84},
        {"absender": "hinweis@leitungsdienst.example", "schublade": "newsletter",
         "warum": "Rundschreiben mit Abmeldelink, kein persoenlicher Bezug.", "sicher": 71}]})

# Die Versionsnummer steht neben dem Skript — die Demo braucht sie auch,
# sonst zeigt die Fusszeile ein Fragezeichen.
# Die Seite und die Versionsnummer liegen neben dem Waechter — die Demo
# braucht beides in IHREM Ordner, weil POSTWACHE_HOME dorthin zeigt.
import shutil
_quelle = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _datei in ("VERSION", "post_web.html"):
    try:
        shutil.copy(os.path.join(_quelle, _datei), os.path.join(ZIEL, _datei))
    except OSError:
        pass

print("Demo gebaut in", ZIEL)
print("  Postfaecher : Privat (an), Buero (ruht)")
print("  Mails       : %d eingeordnet, %d Ordner gelernt" % (len(koepfe), len(ORDNER)))
print("  Dokumente   : 286 von 412 Mails mit Anhang")
print("\nStarten:  POSTWACHE_HOME=%s python3 post_web.py" % ZIEL)
