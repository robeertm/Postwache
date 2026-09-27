#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pruefstand fuer den Umbau (umbau.py).

🔑 Jeder Fall hier ist ein Fehler, der am 27.09.2026 WIRKLICH passiert ist —
   kein erfundener Test. Ein Prüfstand, der nur bestaetigt, was man sich
   gedacht hat, misst das eigene Gedaechtnis.

Aufruf:  python3 probe_umbau.py
"""
import ast
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import umbau  # noqa: E402

GRUEN, ROT = 0, 0


def pruef(name: str, ist, soll):
    global GRUEN, ROT
    if ist == soll:
        GRUEN += 1
        print("  ✓ %s" % name)
    else:
        ROT += 1
        print("  ✗ %s\n      ist:  %r\n      soll: %r" % (name, ist, soll))


def mail(von, betreff="", an="", datum="2020-01-01T00:00:00"):
    return {"von": von, "betreff": betreff, "an": an, "datum": datum,
            "uid": 1, "mid": "<x@y>", "name": "", "liste": False}


# ── Ein ERFUNDENER persoenlicher Katalog ────────────────────────────────────
# 🔴 27.09.2026: hier standen die echten Adressen von dessen Bankberaterin,
#    seinem Architekten und seiner Familie — und `probe_umbau.py` steht auf der
#    Veroeffentlichungsliste. Seit 4.5.1 liegt der persoenliche Teil in
#    `state/regeln_eigen.json`; der Pruefstand setzt sich einen EIGENEN,
#    vollstaendig ausgedachten Katalog und prueft damit dieselben Vorrangfragen.
#    🔑 Ein Pruefstand, der echte Personendaten braucht, um zu pruefen, ist
#    falsch gebaut.
def setze_katalog(eintraege, adressen=()):
    """Einen erfundenen persoenlichen Katalog setzen — mit POSITIONEN.

    🔴 Die Position gehoert dazu: ohne sie standen beim ersten Anlauf alle
       persoenlichen Regeln vorn, „Eigene Post Archiv" kam vor die
       Geraeteregeln, und 823 Geraetemeldungen von der eigenen Adresse landeten
       im Archiv. Gemessen an 17.966 Mails, behoben, hier festgehalten.
    """
    umbau._EIGEN_ZWISCHEN["katalog"] = {
        "eigene_adressen": list(adressen),
        "regeln": [{"nr": nr, "regel": r} for nr, r in eintraege],
    }
    umbau._EIGEN_ZWISCHEN.pop("katalog_voll", None)   # sonst gilt der alte


# Positionen so gewaehlt, dass sie die echten Vorrangfragen nachstellen:
# Geraete (0) VOR eigener Post (68) — und die Beraterin vor der Bank.
setze_katalog([
    (0,  ("Technik.Synology", (), (),
          r"diskstation|synology|\bDSM\b|beispielhaus|systemprotokoll")),
    (8,  ("Hausbau.Finanzierung", ("beraterin@sparkasse.example",), (),
          r"hausplanung|finanzierung|baufinanzierung|darlehen")),
    (6,  ("Hausbau.Architekt", (), ("architekt-berg.example",), r"")),
    (9,  ("Hausbau.Allgemein", (), (), r"musterdorfer|bauvorhaben")),
    (23, ("Banking.Sparkasse Musterstadt", (), ("sparkasse.example",), r"")),
    (44, ("Menschen.Uwe Winter", ("u.winter@beispiel.example",), (), r"")),
    # 🔑 Vor `Menschen.Weitere` (landet bei 56) und nach den Geraeteregeln —
    #    genau die Reihenfolge, die am 27.09. dessen 863 eigene Mails rettete.
    (54, ("Eigene Post Archiv", (), (), r"")),
], adressen=["besitzer@beispiel.example", "besitzer@zweitanbieter.example"])

print("\n── Die zwei Fehler vom 27.09., die die Messung gefunden hat ──")
# 🔴 „Menschen.Weitere" mit gmail.com nahm dessen 863 EIGENE gesendete Mails
#    mit: „Eigene Post Archiv" fiel von 963 auf 14.
pruef("eigene gesendete Post -> Eigene Post Archiv",
      umbau.ziel_fuer(mail("besitzer@beispiel.example", "Re: wetter",
                           "u.winter@beispiel.example"))[0],
      umbau.EIGENE_POST)
# 🔴 m8mit.de (120 KIA-Leistungsnachweise) war angekuendigt, aber nicht
#    eingetragen — 120 Mails landeten im Auffang.
pruef("m8mit.de -> Auto.KIA",
      umbau.ziel_fuer(mail("no-reply@m8mit.de",
                           "Leistungsnachweis SNE-23-013793"))[0],
      "Auto.KIA")

print("\n── Der Befund, der den Entwurf gekippt hat ──")
# Die DiskStation verschickte ueber das eigene Konto: 6.954 Archivmails tragen
# die EIGENE Adresse. Hier entscheidet der Betreff, nicht der Absender.
pruef("Geraetebericht von eigener Adresse -> Technik.Synology",
      umbau.ziel_fuer(mail("besitzer@beispiel.example",
                           "DISKSTATION_ Monatlicher Festplattenintegritätsbericht",
                           "besitzer@zweitanbieter.example"))[0],
      "Technik.Synology")
pruef("Systemprotokoll des Geraets -> Technik.Synology",
      umbau.ziel_fuer(mail("besitzer@beispiel.example",
                           "BEISPIELHAUS1Es gibt ein Systemprotokoll mit Schwere err"))[0],
      "Technik.Synology")
pruef("FRITZ!Powerline -> Technik.Netzwerk",
      umbau.ziel_fuer(mail("besitzer@beispiel.example",
                           "FRITZ!Powerline-Info: Nutzungs- und Verbindungsdaten vom 1.2."))[0],
      "Technik.Netzwerk")

print("\n── Vorrang: der persoenliche Katalog schlaegt den allgemeinen ──")
# 🔑 Die Bankberaterin schreibt zum Hausbau. Ihre Post ist Hausbau, nicht
#    Banking — und weil der persoenliche Katalog VOR dem allgemeinen gefragt
#    wird, gewinnt sie auch gegen eine allgemeine Bankregel.
pruef("Beraterin/Hausplanung -> Hausbau.Finanzierung",
      umbau.ziel_fuer(mail("beraterin@sparkasse.example",
                           "AW: Muster - Hausplanung - Finanzierung"))[0],
      "Hausbau.Finanzierung")
pruef("dieselbe Bank sonst -> Banking",
      umbau.ziel_fuer(mail("noreply@sparkasse.example",
                           "Ihr Online-Banking-Limit wurde geändert."))[0],
      "Banking.Sparkasse Musterstadt")
pruef("Architekt -> Hausbau.Architekt",
      umbau.ziel_fuer(mail("m.frei@architekt-berg.example",
                           "Ihr BV 1. Entwurfsplanung"))[0],
      "Hausbau.Architekt")
pruef("Ortsname im Betreff -> Hausbau.Allgemein",
      umbau.ziel_fuer(mail("info@fremde-firma.xy",
                           "Ummeldung der Gasversorgung Musterdorfer Straße 54"))[0],
      "Hausbau.Allgemein")
# 🔴 Und ohne persoenlichen Katalog faellt NICHTS davon um — es wird nur
#    allgemeiner. Eine frische Installation muss laufen.
_gemerkt = umbau._EIGEN_ZWISCHEN["katalog"]
umbau._EIGEN_ZWISCHEN["katalog"] = {"eigene_adressen": [], "regeln": []}
pruef("ohne persoenlichen Katalog kein Absturz",
      isinstance(umbau.ziel_fuer(mail("wer@fremd.xy", "irgendwas")), tuple), True)
pruef("ohne ihn greift die allgemeine Bankregel",
      umbau.ziel_fuer(mail("noreply@sparkasse.example", "Limit"))[0] != "Hausbau.Finanzierung",
      True)
umbau._EIGEN_ZWISCHEN["katalog"] = _gemerkt

print("\n── Konto vor Kaufbeleg (die enge Regel muss vorn stehen) ──")
pruef("Apple-Sicherheitscode -> Konten.Apple",
      umbau.ziel_fuer(mail("no_reply@email.apple.com",
                           "Ihr Apple-ID Sicherheitscode"))[0],
      "Konten.Apple")
pruef("Apple-Kaufbeleg -> Einkauf.Apple",
      umbau.ziel_fuer(mail("no_reply@email.apple.com",
                           "Ihre Rechnung von Apple"))[0],
      "Einkauf.Apple")

print("\n── Umschrift: das Muster darf den Umlaut nicht brauchen ──")
# Dieselbe Lehre wie am 11.09.: `normal()` macht ä→ae, deshalb muss beides
# gleich einsortiert werden.
a = umbau.ziel_fuer(mail("x@y.de", "Betreuungsrechnung für Juli"))[0]
b = umbau.ziel_fuer(mail("x@y.de", "Betreuungsrechnung fuer Juli"))[0]
pruef("Umlaut und Umschrift gleich behandelt", a, b)

print("\n── Reihenfolge-Zusagen im Katalog ──")
# 🔑 Gegen den VERZAHNTEN Katalog, nicht gegen den Quelltext allein — der
#    persoenliche Teil sitzt zwischen den allgemeinen Regeln.
ziele = [r[0] for r in umbau.katalog()]
technik = min(i for i, z in enumerate(ziele) if z.startswith("Technik."))
eigen = ziele.index(umbau.EIGENE_POST)
menschen = ziele.index("Menschen.Weitere")
pruef("Geraete VOR eigener Post VOR Menschen.Weitere",
      technik < eigen < menschen, True)
# 🔑 Der Vorrang „meine Beraterin vor irgendeiner Bank" haengt seit 4.5.1 nicht
#    mehr an der Reihenfolge IM QUELLTEXT, sondern daran, dass der persoenliche
#    Katalog VOR dem allgemeinen gefragt wird. Genau das prueft der Block oben
#    („Vorrang: der persoenliche Katalog schlaegt den allgemeinen"). Hier bleibt
#    die Zusage, die den Quelltext betrifft:
# (Die Gegenprobe gegen den persoenlichen Katalog steht gleich darunter — sie
#  braucht keinen Namen in dieser Datei.)
# 🔴 Und die Gegenprobe fuer den GANZEN Katalog — OHNE einen einzigen Namen in
#    dieser Datei. Der Pruefstand steht selbst auf der Veroeffentlichungsliste;
#    eine Verbotsliste mit echten Nachnamen darin waere genau derselbe Fehler,
#    den sie verhindern soll.
#
# 🔑 Geprueft wird gegen die ZUSTANDSDATEI: keine Adresse und keine Domain aus
#    `state/regeln_eigen.json` darf im Quelltext-Katalog auftauchen. Wo es die
#    Datei nicht gibt (frische Installation, fremder Rechner), entfaellt die
#    Frage — dort kann auch nichts ausgelaufen sein.
_eigen = umbau.eigen_laden()
_persoenlich = set(_eigen["eigene_adressen"])
for _r in _eigen["regeln"]:
    _persoenlich.update(_r["regel"][1])
    _persoenlich.update(_r["regel"][2])
_katalogtext = " ".join(
    r[0] + " " + " ".join(list(r[1]) + list(r[2])) + " " + (r[3] or "")
    for r in umbau.REGELN).lower()
if _persoenlich:
    _drin = sorted(w for w in _persoenlich if w and w in _katalogtext)
    pruef("nichts aus dem persoenlichen Katalog steht im Quelltext",
          _drin, [])
    # Auch der Kern einer Domain reicht als Fund („sparkasse-musterstadt" aus
    # „noreply@sparkasse-musterstadt.de").
    _kerne = {w.split("@")[-1].split(".")[0] for w in _persoenlich if len(w) > 4}
    _drin2 = sorted(k for k in _kerne if len(k) > 4 and k in _katalogtext)
    pruef("auch kein Domain-Kern daraus", _drin2, [])
    print("     (geprueft gegen %d persoenliche Adressen/Domains)" % len(_persoenlich))
else:
    print("  –   kein persoenlicher Katalog auf diesem Rechner — nichts zu pruefen")

pruef("Konten.Apple vor Einkauf.Apple",
      ziele.index("Konten.Apple") < ziele.index("Einkauf.Apple"), True)

print("\n── Tabu: was nie angefasst wird ──")
for t in ("Sent Items", "Drafts", "Trash", "Spam", "Sent Items.2019"):
    pruef("tabu: %s" % t, umbau.tabu(t), True)
for t in ("Haus", "Archiv Gmail", "Shopping.Paypal"):
    pruef("nicht tabu: %s" % t, umbau.tabu(t), False)

print("\n── Jede Mail bekommt ein Ziel, keine faellt durch ──")
pruef("voellig unbekannter Absender -> Auffang",
      umbau.ziel_fuer(mail("wer@voellig-unbekannt.xyz", "irgendwas"))[0],
      umbau.AUFFANG)
pruef("leerer Absender -> Auffang",
      umbau.ziel_fuer(mail("", ""))[0], umbau.AUFFANG)

print("\n── Neue Post selbst einsortieren (27.09., dessen Nachtrag) ──")
# der Besitzer: „wenn neue mails kommen muessen die immer analysiert werden und
# einsortiert werden und wenn es neue ordner braucht dann soll es die
# selbstaendig erstellen."
import tempfile
_tmp = tempfile.mkdtemp()
_echt = umbau.out_pfad
umbau.out_pfad = lambda n: os.path.join(_tmp, n)     # nichts ins Projekt schreiben
try:
    pruef("bekannte Regel schlaegt die Ableitung",
          umbau.ziel_fuer_neue(mail("service@paypal.de", "Neue Nachricht"))[:1],
          ("Banking.PayPal",))
    # 🔴 Erst ab der Schwelle ein EIGENER Ordner — sonst waechst das Postfach um
    #    einen Ordner pro einmaligem Newsletter.
    wege = [umbau.ziel_fuer_neue(mail("shop@neuerladen.de", "Ihre Bestellung %d" % i))[0]
            for i in range(1, 6)]
    pruef("erste drei -> Kategorie.Allgemein", wege[:3],
          ["Einkauf.Allgemein"] * 3)
    pruef("ab der vierten -> eigener Ordner", wege[3:],
          ["Einkauf.Neuerladen"] * 2)
    pruef("Entscheidung wird gemerkt (Datei entsteht)",
          os.path.exists(umbau.out_pfad("umbau_domains.json")), True)
    pruef("nichts erkennbar -> kein Ziel, Mail bleibt liegen",
          umbau.ziel_fuer_neue(mail("x@voellig.unklar", "hallo"))[0], "")
    pruef("Rundschreiben ohne Regel -> Newsletter",
          umbau.ziel_fuer(dict(mail("wer@fremd.xy", "Angebote"), liste=True))[0],
          "Newsletter")
finally:
    umbau.out_pfad = _echt

print("\n── Marke aus der Domain (nicht das erste Stueck) ──")
for dom, soll in (("news.miele.de", "Miele"), ("mail.anthropic.com", "Anthropic"),
                  ("txn-email03.playstation.com", "Playstation"),
                  ("meine.steuertipps.de", "Steuertipps"),
                  ("de.idealo.com", "Idealo"), ("something.co.uk", "Something")):
    pruef("%s -> %s" % (dom, soll), umbau.marke_aus_domain(dom), soll)

print("\n── Mehrheit entscheidet, nicht die erste Mail ──")
pruef("drei Bestellungen -> Einkauf",
      umbau.kategorie_aus_inhalt(["Ihre Bestellung", "Versandbestaetigung",
                                  "Rechnung 123"]), "Einkauf")
pruef("ein Treffer unter zehn -> nichts (Raten waere schlimmer)",
      umbau.kategorie_aus_inhalt(["Bestellung"] + ["blah"] * 9), "")

print("\n── Der Riegel vor dem Verschieben (27.09., Nachweis am lebenden Postfach) ──")
# 🔴 `anwenden` meldete „3 bewegt", obwohl es nichts bewegt hatte: nach einem
#    `zurueck` lagen die Mails unter NEUEN UIDs, die alte UID lieferte keinen
#    Kopf — und ein LEERES Ergebnis galt als bestaetigt.
pruef("gleiche Message-Id -> bewegen erlaubt",
      umbau.mid_bestaetigt("<a@b>", "<a@b>"), True)
pruef("Leerzeichen/Umbruch stoert nicht",
      umbau.mid_bestaetigt("<a@b>", " <a@b>\r\n"), True)
pruef("andere Message-Id -> NICHT bewegen",
      umbau.mid_bestaetigt("<a@b>", "<c@d>"), False)
pruef("nichts gefunden (UID weg) -> NICHT bewegen",
      umbau.mid_bestaetigt("<a@b>", ""), False)
pruef("keine Id im Plan -> NICHT bewegen",
      umbau.mid_bestaetigt("", "<a@b>"), False)
pruef("beides leer -> NICHT bewegen",
      umbau.mid_bestaetigt("", ""), False)

print("\n── Mehrdeutige Absender: die Domain darf nicht entscheiden ──")
# der Besitzer, 27.09.2026: „ich habe mails von check24 die bieten ja alles an, ich
# habe da eine reise gebucht, also sollte diese mail unter reisen auftauchen
# nicht unter versicherungen."
# 🔴 Das stand schon in Phase 1 (11.09.): „check24.de macht Hotels UND
#    Versicherungen — die Hauptdomain darf vorschlagen, nicht handeln." Ich
#    hatte sie trotzdem handeln lassen.
for von, betreff, soll in (
    ("info@hotel.check24.de", 'Eingangsbestätigung Ihrer Buchung "Wolin" (319991)', "Reisen"),
    ("no-reply@hotel.check24.de", 'Buchungsbestätigung "Wolin" (319992)', "Reisen"),
    ("e-scooter-versicherung@check24.de",
     "Ihr Versicherungsschein zur E-Scooter-Versicherung ist online",
     "Versicherungen.Allgemein"),
    # 🔴 Der Wortstamm zaehlt: „Kfz-Versicherer" enthaelt kein „Versicherung".
    ("kfz-serviceteam@check24.de",
     "Bewerten Sie uns und Ihren neuen Kfz-Versicherer für Ihren Kia Carens",
     "Versicherungen.Allgemein"),
    ("kundenkonto@check24.de", "Ihr CHECK24 Sicherheitscode", "Konten.Allgemein"),
    ("login@check24.de", "Ihr CHECK24 Kundenbereich", "Konten.Allgemein"),
    # Passt gar nichts, gilt der Rueckfall der Domain — geraten wird nicht.
    ("smily@check24.de", "Willkommen bei Smily", "Versicherungen.Allgemein"),
    # Dieselbe Regel, anderer Absender: die Post liefert Pakete UND Steuertipps.
    ("noreply@deutschepost.de", "Ihre Sendung wurde zugestellt", "Einkauf.Versand"),
    ("steuertipps@deutschepost.de", "Steuertipps zum Jahresende", "Steuer"),
):
    pruef("%-34s -> %s" % (von, soll), umbau.ziel_fuer(mail(von, betreff))[0], soll)

# Eine Regel auf die GENAUE Adresse bleibt unangetastet — sie ist schon so
# genau, wie es geht.
pruef("genaue Adresse schlaegt die Feinentscheidung",
      umbau.ziel_fuer(mail("beraterin@sparkasse.example",
                           "Hausplanung - Finanzierung"))[0],
      "Hausbau.Finanzierung")
pruef("eindeutiger Absender bleibt unberuehrt",
      umbau.ziel_fuer(mail("service@paypal.de", "Ihre Zahlung"))[0],
      "Banking.PayPal")

print("\n── Netzaussetzer: die Verbindung wird wiederholt ──")
# der Besitzer, 27.09.2026: „manchmal ist das postfach nicht erreichbar."
# Gemessen: der Anbieter nimmt 8 gleichzeitige Verbindungen — KEINE Abfragesperre.
# Im Pi-Protokoll stehen aber zwei echte DNS-Aussetzer. Ein Hickser von zwei
# Sekunden darf keinen Lauf von sechs Minuten abbrechen.
import postwache as _pw

class _Kaputt:
    versuche = 0
    scheitert_bis = 0

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        _Kaputt.versuche += 1
        if _Kaputt.versuche < _Kaputt.scheitert_bis:
            raise OSError("Temporary failure in name resolution")
        return "VERBUNDEN"

    def __exit__(self, *a):
        pass

_echt_pf, _echt_stand, _echt_schlaf = _pw.Postfach, umbau.stand_schreiben, umbau.time.sleep
umbau.stand_schreiben = lambda **k: None
umbau.time.sleep = lambda s: None          # Probe soll nicht 6 s warten
try:
    _pw.Postfach = _Kaputt
    _Kaputt.versuche, _Kaputt.scheitert_bis = 0, 3
    with umbau.verbindung({}, False) as pf:
        pruef("nach zwei Aussetzern verbunden", (pf, _Kaputt.versuche), ("VERBUNDEN", 3))
    _Kaputt.versuche, _Kaputt.scheitert_bis = 0, 99
    try:
        with umbau.verbindung({}, False, versuche=3):
            pruef("dauerhaft tot -> haette werfen muessen", True, False)
    except OSError:
        pruef("dauerhaft tot -> Fehler fliegt weiter (nicht verschluckt)",
              _Kaputt.versuche, 3)
finally:
    _pw.Postfach, umbau.stand_schreiben, umbau.time.sleep = _echt_pf, _echt_stand, _echt_schlaf

print("\n── Leere Ordner entfernen: was NIE angefasst wird ──")
# der Besitzer, 27.09.2026: „ordner die nicht mehr gebraucht werden entfernen aber
# nur wenn darin keine mails mehr sind."
# 🔴 Sonderordner erkennt man an der FLAGGE, nicht am Namen — ein englisches
#    Postfach nennt den Papierkorb anders, die Flagge ist dieselbe.
for flags, soll in (("\\HasNoChildren", False),
                    ("\\Trash \\HasNoChildren", True),
                    ("\\Junk", True),
                    ("\\Noselect \\HasChildren", True),
                    ("\\Sent", True),
                    ("\\Archive", True),
                    ("\\Drafts \\HasNoChildren", True),
                    ("\\HasChildren", False)):
    pruef("Flagge %-24s -> Sonderordner %s" % (flags, soll),
          umbau.sonderordner(flags), soll)

print("\n── Gegenprobe am Quelltext: geloescht wird nie ──")
baum = ast.parse(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "umbau.py"), encoding="utf-8").read())
# 🔴 Nicht per Wortsuche, sondern ueber den AST: in welchen Funktionen steht
#    ueberhaupt ein expunge/Deleted?
wo = {}
for fn in [n for n in ast.walk(baum) if isinstance(n, ast.FunctionDef)]:
    quelle = ast.unparse(fn)
    if "expunge" in quelle or "Deleted" in quelle:
        wo[fn.name] = (quelle.count("expunge("), quelle.count("Deleted"))
pruef("expunge/Deleted nur in anwenden und zurueck",
      sorted(wo), ["anwenden", "zurueck"])
# 🔴 Ordner entfernen ist die EINZIGE Ausnahme — und sie darf nur an EINER
#    Stelle stehen, damit die Bedingung „leer" nicht irgendwo umgangen wird.
loescht = sorted(fn.name for fn in ast.walk(baum)
                 if isinstance(fn, ast.FunctionDef)
                 and re.search(r"\.delete\(", ast.unparse(fn)))
pruef("m.delete() nur in ordner_raeumen", loescht, ["ordner_raeumen"])
q_ord = ast.unparse([n for n in ast.walk(baum)
                     if isinstance(n, ast.FunctionDef)
                     and n.name == "ordner_raeumen"][0])
pruef("in ordner_raeumen: Leer-Pruefung steht VOR dem Entfernen",
      q_ord.index("_ordner_leer") < q_ord.index(".delete("), True)
pruef("Vorgabe ist trocken (Entfernen nur mit --scharf)",
      umbau.ordner_raeumen.__defaults__[0], True)
# 🔴 Ein Umbau, der die gelernte Landkarte nicht mitnimmt, sortiert morgen in
#    Ordner, die es nicht mehr gibt (46 gelernt, 29 davon nach dem Lauf leer).
for fn_name in ("anwenden", "ordner_raeumen"):
    q = ast.unparse([n for n in ast.walk(baum) if isinstance(n, ast.FunctionDef)
                     and n.name == fn_name][0])
    pruef("%s erneuert die Landkarte" % fn_name, "ablage_erneuern" in q, True)
    # 🔴 Und zwar SOLANGE die Verbindung offen ist — sonst kann es nur den
    #    Merker loeschen, und der Waechter lernt erst beim naechsten Lauf,
    #    der ueberhaupt Post sieht.
    pruef("%s erneuert INNERHALB des with-Blocks" % fn_name,
          q.index("ablage_erneuern") > q.index("verbindung(")
          and "ablage_erneuern(pf)" in q, True)
# 🔴 Am 27.09. meldete `ablage_veralten()` False: von der Kommandozeile ist
#    `_PF_ID` leer, also las es die GLOBALE ablage.json statt der des Postfachs.
pruef("ablage_veralten waehlt das Postfach",
      "postfach_gewaehlt" in ast.unparse([n for n in ast.walk(baum)
                                          if isinstance(n, ast.FunctionDef)
                                          and n.name == "ablage_veralten"][0]), True)
pruef("postfach_gewaehlt stellt den alten Wert zurueck, statt zu leeren",
      "self.vorher" in ast.unparse([n for n in ast.walk(baum)
                                    if isinstance(n, ast.ClassDef)
                                    and n.name == "postfach_gewaehlt"][0]), True)
pruef("ablage_veralten fasst nur das Feld 'gelernt' an",
      "gelernt" in ast.unparse([n for n in ast.walk(baum)
                                if isinstance(n, ast.FunctionDef)
                                and n.name == "ablage_veralten"][0]), True)
pruef("in anwenden genau EIN expunge (ein Stapel je Ordner)",
      wo.get("anwenden", (0, 0))[0], 1)
# Kopie muss VOR dem Abhaken stehen — sonst kann Post verloren gehen.
fn = [n for n in ast.walk(baum)
      if isinstance(n, ast.FunctionDef) and n.name == "anwenden"][0]
q = ast.unparse(fn)
pruef("in anwenden: copy steht vor +FLAGS",
      q.index("'copy'") < q.index("+FLAGS"), True)
pruef("kein 'store' ohne vorherige Kopiepruefung (typ != OK -> continue)",
      "if typ != 'OK'" in q.replace('"', "'"), True)

print("\n── Folge des Umbaus fuer den Rest des Programms (27.09., dessen Befund) ──")
# 🔴 „ich habe bei neu anlegen auto.autohaus-nord angegeben … aber die mail dahinter
#    von j. haller nicht dorthin geschoben, jetzt finde ich sie garnicht mehr."
#    Die Mails lagen in „Unsortiert". `nachziehen()` sah NUR den Posteingang an —
#    bis 4.0.0 richtig, seit dem Umbau still falsch: unerkannte Post bleibt nicht
#    mehr im Posteingang liegen, sie wird in den Auffang geraeumt.
import post_web  # noqa: E402


class _FalschesPostfach:
    trenner = "."

    def ordner_liste(self):
        return ["INBOX", "Unsortiert", "Einkauf.Allgemein", "Auto.Allgemein",
                "Auto.KIA", "Auto.Autohaus-nord", "Banking.PayPal", "Newsletter"]


orte = post_web.auffangorte(_FalschesPostfach())
pruef("Auffangorte: Posteingang ist dabei", "INBOX" in orte, True)
pruef("Auffangorte: der Auffang ist dabei", umbau.AUFFANG in orte, True)
pruef("Auffangorte: jedes Allgemein ist dabei",
      sorted(o for o in orte if o.endswith("Allgemein")),
      ["Auto.Allgemein", "Einkauf.Allgemein"])
# 🔴 Das ist die Grenze, die das Ganze verteidigbar macht: aus einem GEPFLEGTEN
#    Ordner wird nichts geholt. Dort ist die Entscheidung schon gefallen.
pruef("Auffangorte: ein gepflegter Ordner ist NICHT dabei", "Auto.KIA" in orte, False)
pruef("Auffangorte: Newsletter ist NICHT dabei", "Newsletter" in orte, False)
pruef("Auffangorte: genau vier Orte (INBOX, Unsortiert, 2x Allgemein)",
      len(orte), 4)

baum_web = ast.parse(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       "post_web.py"), encoding="utf-8").read())
q_nach = ast.unparse([n for n in ast.walk(baum_web)
                      if isinstance(n, ast.FunctionDef) and n.name == "nachziehen"][0])
pruef("nachziehen fragt die Auffangorte", "auffangorte(pf)" in q_nach, True)
pruef("nachziehen holt nicht aus dem Ziel selbst", "if ort == voll" in q_nach, True)
pruef("nachziehen schreibt den ECHTEN Herkunftsordner ins Journal",
      "'von': ort" in q_nach, True)
pruef("nachziehen nennt die Herkunft in der Meldung", "orte=woher" in q_nach, True)
# 🔴 Ein `or`, dessen zweiter Teil den ersten verschluckt, prueft nichts —
#    deshalb hier die eine Frage, um die es geht: wird ueberhaupt
#    zurueckgestellt? `ast.unparse` schreibt einfache Anfuehrungszeichen.
pruef("nachziehen stellt am Ende auf INBOX zurueck",
      "select('INBOX'" in q_nach, True)

print("\n── Der gelernte Wortschatz (27.09.: warum wird Unsortiert nicht sortiert?) ──")
pruef("Fuellwoerter fallen weg",
      umbau.worte("Ihre Rechnung fuer die neue Bestellung"), ["rechnung", "bestellung"])
pruef("reine Zahlen sind keine Woerter",
      umbau.worte("Bestellung 4711 vom 12.09.2026"), ["bestellung"])
pruef("Umschrift wie bei den Mustern",
      umbau.worte("Rückfrage zur Prüfung"), ["rueckfrage", "pruefung"])
pruef("der Auffang lehrt NICHT", umbau.kategorie_des_ordners("Unsortiert"), "")
pruef("eigene gesendete Post lehrt NICHT",
      umbau.kategorie_des_ordners("Eigene Post Archiv"), "")
pruef("der Posteingang lehrt NICHT", umbau.kategorie_des_ordners("INBOX"), "")
pruef("Unterordner lehrt fuer die Kategorie",
      umbau.kategorie_des_ordners("Banking.PayPal"), "Banking")
pruef("auch dessen eigene Schubladen lehren",
      umbau.kategorie_des_ordners("Fahrrad"), "Fahrrad")

# 🔴 DER FEHLER, DEN DIE KREUZPROBE ERLEGT HAT. Erst liess ich das Vorwissen
#    ueber die Groesse weg — dann gewann `Gesundheit` (15 Mails) gegen `Technik`
#    (7.433): bei Add-1-Glaettung ist ein unbekanntes Wort in einer winzigen
#    Kategorie billig. 25,7 % Treffer. Dieser Fall haelt das fest.
KLEIN = {"kategorien": {
            "Technik": {"diskstation": 900, "systemprotokoll": 800, "festplatte": 700,
                        "bericht": 300, "rechnung": 40},
            "Gesundheit": {"rezept": 3, "praxis": 2}},
         "mails": {"Technik": 2000, "Gesundheit": 6}}
pruef("eine winzige Kategorie gewinnt NICHT gegen eine grosse",
      umbau.kategorie_aus_wortschatz(
          ["DISKSTATION Systemprotokoll Festplatte Bericht"], dict(KLEIN))[0], "Technik")
pruef("die winzige Kategorie gewinnt, wenn die Woerter IHR gehoeren",
      umbau.kategorie_aus_wortschatz(["Rezept von der Praxis"], dict(KLEIN))[0],
      "Gesundheit")
pruef("ein einziges bekanntes Wort reicht NICHT fuer ein Urteil",
      umbau.kategorie_aus_wortschatz(["Rechnung"], dict(KLEIN))[0], "")
pruef("unbekannte Woerter geben kein Urteil",
      umbau.kategorie_aus_wortschatz(["Quastelquiek Blubberfug"], dict(KLEIN))[0], "")
pruef("ohne Wortschatz kein Urteil",
      umbau.kategorie_aus_wortschatz(["Rezept Praxis"], {"kategorien": {}, "mails": {}})[0], "")
# 🔑 Die Kreuzprobe muss die eigene Mail abziehen, sonst beantwortet sich jede
#    Mail selbst und die Trefferquote ist eine Selbstauskunft.
EINZEL = {"kategorien": {"A": {"eins": 1, "zwei": 1}, "B": {"drei": 5, "vier": 5}},
          "mails": {"A": 1, "B": 5}}
pruef("mit Abzug beantwortet sich eine Mail nicht selbst",
      umbau.kategorie_aus_wortschatz(["eins zwei"], dict(EINZEL),
                                     ohne={"A": {"eins", "zwei"}})[0], "")
pruef("ohne Abzug wuerde sie es",
      umbau.kategorie_aus_wortschatz(["eins zwei"], dict(EINZEL))[0], "A")
pruef("die gemessene Schwelle steht im Code", umbau.WORT_ABSTAND, 2.5)

print("\n── Der Wortschatz darf keine Marken erfinden (nach dem ersten scharfen Lauf) ──")
# 🔴 Gemessen: der erste scharfe Lauf erzeugte `Reisen.Samsung`,
#    `Kinder.Endomondo`, `Versicherungen.Fastspring`, `Gaming.Highresaudio`.
#    Ein geratener NAME ist schlimmer als eine geratene Schublade.
q_str = ast.unparse([n for n in ast.walk(baum) if isinstance(n, ast.FunctionDef)
                     and n.name == "struktur_lernen"][0])
pruef("struktur_lernen prueft die Quelle vor einem eigenen Ordner",
      "darf_eigenen" in q_str and "wortschatz" in q_str, True)
q_neu = ast.unparse([n for n in ast.walk(baum) if isinstance(n, ast.FunctionDef)
                     and n.name == "ziel_fuer_neue"][0])
pruef("ziel_fuer_neue prueft dasselbe", "darf_eigenen" in q_neu, True)
pruef("die Reihenfolge Muster -> Wortschatz -> Modell steht so im Code",
      q_str.index("kategorie_aus_inhalt") < q_str.index("kategorie_aus_wortschatz")
      < q_str.index("ki_kategorien"), True)

print("\n── Die KI in der Werkstatt: der Rundlauf ──")
pruef("ki_kategorien gibt einen CODE zurueck, keine Prosa",
      len(umbau.ki_kategorien({"x.de": ["a"]}, {})), 3)
q_wf = ast.unparse([n for n in ast.walk(baum) if isinstance(n, ast.FunctionDef)
                    and n.name == "werkstatt_fragen"][0])
pruef("erst die Stausperre, dann der Auftrag",
      q_wf.index("auftrag_stau") < q_wf.index("escalate"), True)
pruef("dieselbe Frage wird nicht wiederholt", "WERK_WIEDERVORLAGE" in q_wf, True)
pruef("nur Domain und Betreffe werden uebergeben (kein Text, keine Menschen)",
      "'betreffe'" in q_wf and "text" not in q_wf.split("daten = ")[1][:200], True)
q_wa = ast.unparse([n for n in ast.walk(baum) if isinstance(n, ast.FunctionDef)
                    and n.name == "werkstatt_antwort_holen"][0])
# 🔴 Eine Antwort aus einem Auftragsbuch ist FREMDER TEXT.
pruef("jede Antwort wird gegen die erlaubten Kategorien geprueft",
      "erlaubt" in q_wa and "not in erlaubt" in q_wa, True)
pruef("die Antwortdatei wird umbenannt, nicht geloescht",
      "os.replace" in q_wa and ".erledigt-" in q_wa, True)
pruef("der Rundlauf sammelt EIN, bevor er fragt",
      q_str.index("werkstatt_antwort_holen") < q_str.index("werkstatt_fragen"), True)

print("\n── 100 % nachverfolgbar: was aus einem Weckruf wurde ──")
baum_w2 = ast.parse(quelle_pw := open(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "postwache.py"), encoding="utf-8").read())


def pw_fn(name):
    for n in ast.walk(baum_w2):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return ast.unparse(n)
    return ""


pruef("escalate traegt den Auftrag in die eigene Buchhaltung ein",
      "auftrag_merken" in pw_fn("escalate"), True)
q_pr = pw_fn("auftraege_pruefen")
pruef("ein laufender Auftrag gilt NICHT als Haenger",
      "not a.get('gestartet')" in q_pr.replace('"', "'"), True)
pruef("gemeldet wird EINMAL, nicht jede Minute", "'gemeldet'" in q_pr.replace('"', "'"), True)
pruef("eine Frist gibt es", "AUFTRAG_FRIST" in q_pr, True)
q_lauf = pw_fn("main")
pruef("der Lauf prueft die Auftraege", "auftraege_pruefen" in q_lauf, True)
pruef("der Lauf holt die Werkstatt-Antwort", "werkstatt_antwort_holen" in q_lauf, True)
q_fp = pw_fn("lauf_fuer_postfach")
pruef("kein neuer Weckruf bei Stau", "auftrag_stau" in q_fp, True)
# 🔴 Die erste Haelfte war tautologisch („x < x+1") und prueft nichts. Was
#    zaehlt, ist: bei Stau entfaellt der WECKRUF, nicht der BEFUND.
pruef("der Befund geht TROTZDEM in die Chronik",
      "chronik('auftrag_stau'" in q_fp.replace('"', "'"), True)
# 🔴 Ein leeres Ergebnis der Werkstatt darf nicht „alles erledigt" heissen.
q_zu = pw_fn("_werkstatt_zustaende")
pruef("faellt die Werkstatt aus, kommt ein LEERES Woerterbuch (kein Erfolg)",
      q_zu.rstrip().endswith("return {}"), True)
q_web = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "post_web.py"), encoding="utf-8").read()
pruef("die Karte erscheint nur mit Werkstatt",
      "konfig().get('werkstatt')" in q_web.replace('"', "'"), True)

print("\n── dessen eigene Regeln schlagen alles (27.09., teuer gelernt) ──")
# 🔴 Der Umbau entfernte `Auto.Autohaus-nord` — dessen EIGENEN Ordner mit seiner
#    EIGENEN Regel. Danach zeigte die Regel ins Nichts.
_echt_einst = umbau.pw.einstellungen
umbau._EIGEN_ZWISCHEN.clear()
umbau.pw.einstellungen = lambda: {"absender_regeln": {"j.haller@autohaus-nord.example": "Auto.Autohaus-nord"}}
try:
    pruef("eine eigene Regel gewinnt gegen jede Ableitung",
          umbau.ziel_fuer(mail("j.haller@autohaus-nord.example", "AW: smart #5 Probefahrt")),
          ("Auto.Autohaus-nord", "deine eigene Regel"))
    pruef("sie steht GANZ VORN, nicht nach den Mustern",
          umbau.ziel_fuer(mail("j.haller@autohaus-nord.example", "Ihre Rechnung Nr. 4711"))[0],
          "Auto.Autohaus-nord")
    pruef("fremde Adresse bleibt unberuehrt",
          umbau.ziel_fuer(mail("service@paypal.de", "Zahlung"))[0] != "Auto.Autohaus-nord", True)
    pruef("eigene Ziele werden gelesen",
          sorted(umbau.eigene_regeln().values()), ["Auto.Autohaus-nord"])
finally:
    umbau.pw.einstellungen = _echt_einst
    umbau._EIGEN_ZWISCHEN.clear()
q_ra = ast.unparse([n for n in ast.walk(baum) if isinstance(n, ast.FunctionDef)
                    and n.name == "ordner_raeumen"][0])
pruef("Aufraeumen schuetzt die Ziele eigener Regeln",
      "eigene_ziele" in q_ra and "name in eigene_ziele" in q_ra, True)
pruef("die eigenen Ziele werden VOR der Schleife gelesen",
      q_ra.index("eigene_ziele = ") < q_ra.index("for nr, name in"), True)

print("\n── Die Seite: eine Liste der Ansichten, nicht zwei ──")
# 🔴 4.5.0: für den Hash gab es eine ZWEITE, hartkodierte Aufzählung. Beim
#    vierten Reiter wurde nur die untere gepflegt — ein geteilter Link
#    `…/#umzug` landete still auf der Übersicht.
_html = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "post_web.html"), encoding="utf-8").read()
pruef("ANSICHTEN steht genau EINMAL im Quelltext",
      _html.count("const ANSICHTEN = ["), 1)
pruef("keine zweite hartkodierte Liste fuer den Hash",
      '["uebersicht", "umbau", "einstellungen"]' in _html, False)
pruef("der Hash wird gegen ANSICHTEN geprueft",
      "ANSICHTEN.includes(h)" in _html, True)
pruef("alle vier Reiter stehen in der einen Liste",
      all(('"%s"' % n) in _html.split("const ANSICHTEN = [")[1].split("]")[0]
          for n in ("uebersicht", "umbau", "umzug", "einstellungen")), True)

print("\n%d gruen, %d rot" % (GRUEN, ROT))
sys.exit(1 if ROT else 0)
