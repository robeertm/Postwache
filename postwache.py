#!/usr/bin/env python3
"""Postwache — der Waechter ueber dessen IMAP-Postfach.

ARCHITEKTUR (der Grund, warum das wenig kostet) — uebernommen von einem Schwesterprojekt:
Der Waechter hier ist STUMM. Kein Sprachmodell, keine Tokens. Er laeuft jede
Minute aus dem Cron (der Besitzer, 19.09.2026; vorher alle 5 — ein Lauf ohne neue Mail
dauert 0,2 s), sieht sich die NEUEN Mails an (nur die seit der letzten
gemerkten UID) und ordnet sie nach festen, lesbaren Regeln ein. Der AGENT in der
Werkstatt wird nur geweckt, wenn der Waechter selbst nicht weiterweiss — also bei
Mails, die in keine Schublade passen. Damit haengen die Kosten an der Zahl der
UNKLAREN Faelle, nicht an der Zahl der Mails.

🔑 DER WICHTIGSTE GRUNDSATZ: WICHTIGES BLEIBT IM POSTEINGANG.
Sortiert wird nur der Laerm HINAUS (Newsletter, Werbung, Automatisches). Fristen,
Amtliches, Sicherheitswarnungen und Mails von echten Menschen bleiben liegen, wo
Der Besitzer sie sowieso sieht. Ein Sortierer, der Wichtiges wegraeumt, ist gefaehrlicher
als gar keiner.

🔴 GELESEN-STATUS: JEDER Abruf benutzt BODY.PEEK. Ein blankes BODY[] wuerde die
Mail als gelesen markieren — der Waechter wuerde damit dessen Postfach veraendern,
ohne dass jemand es angeordnet hat. Im Lernlauf wird die Mailbox zusaetzlich mit
readonly=True geoeffnet, dann ist auch versehentliches Schreiben ausgeschlossen.

🔴 GELOESCHT WIRD NIE. Der Waechter kennt keinen einzigen Aufruf, der eine Mail
loescht oder das \\Deleted-Flag setzt. Verschoben wird nur in Unterordner des
Posteingangs, und jede Verschiebung steht mit Quelle und Ziel im Journal, damit
sie einzeln oder komplett zurueckgeholt werden kann.

NOTAUS: input_boolean.postwache_aktiv (Handy) ODER die Datei DISABLED im
Zustandsordner. Beides einzeln genuegt. Wird VOR allem anderen geprueft, auch vor
dem Verbinden mit dem Postfach.

LERNLAUF: Solange `einstellungen.json` kein "scharf": true traegt, wird NICHTS
verschoben. Der Waechter schreibt nur auf, was er tun WUERDE. Der Besitzer schaltet auf
der Seite (Port 8110) scharf, wenn die Einordnung stimmt.
"""
from __future__ import annotations

import base64
import email
import email.header
import email.utils
import hashlib
import imaplib
import json
import os
import quopri
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

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
PAUSE = os.path.join(BASE, "PAUSE")
# 🔑 Ab 3.0.0 steht nichts Umgebungsabhaengiges mehr fest im Quelltext. Was
# frueher hier als Konstante stand, kommt jetzt aus `konfig.json` — und fehlt
# die Datei, wird die Umgebung ERKANNT. Das ist der Unterschied zwischen einem
# Programm, das auf genau einem Rechner laeuft, und einem, das man herunterlaedt:
# auf dessen Pi findet die Erkennung Home Assistant und die Werkstatt und alles
# bleibt wie es war; auf einem fremden Rechner findet sie nichts und die
# Postwache laeuft eben ohne beides, statt auf Pfade zu zeigen, die es nicht gibt.
# Wonach die Erkennung sucht, wenn `konfig.json` nichts sagt. Bewusst
# allgemeine Orte: ein Pfad aus genau einem Haushalt hat in einem Programm,
# das andere herunterladen, nichts verloren.
ENVFILE_STANDARD = os.path.join(BASE, "ha.env")
HA_STANDARD = "http://127.0.0.1:8123"
SCHALTER_STANDARD = "input_boolean.postwache_aktiv"


def _version() -> str:
    """Eine Quelle fuer die Versionsnummer: die Datei VERSION neben dem Skript.
    Waechter UND Seite lesen dieselbe Datei — zwei Konstanten waeren zwei
    Wahrheiten, und eine davon waere irgendwann die falsche."""
    try:
        with open(neben_dem_programm("VERSION"), encoding="utf-8") as fh:
            return fh.read().strip() or "?"
    except OSError:
        return "?"


VERSION = _version()

LOG_ZEILEN = 4000
CHRONIK_ZEILEN = 1200
JOURNAL_ZEILEN = 5000        # jede Verschiebung, damit sie umkehrbar bleibt

ZUGANG = "zugang.json"       # 0600, bis 2.x: EIN Postfach. Wird migriert.
POSTFAECHER = "postfaecher.json"  # 0600: seit 3.0.0 die Liste aller Postfaecher
KONFIG = "konfig.json"       # Umgebung: Seite, Home Assistant, Werkstatt
KI = "ki.json"               # 0600: Anbieter fuer die Urteilshilfe
EINST = "einstellungen.json"
LAUF = "lauf.json"
KOEPFE = "koepfe.json"       # was der Waechter von jeder Mail behalten hat
ABSENDER = "absender.json"   # Langzeitprofil je Absender

# Wie viele Mails ein Kaltstart anschaut, um die Ausgangslage zu lernen.
KALTSTART_MAILS = 300
# So viele neue Mails werden pro Lauf hoechstens verarbeitet. Ein Postfach, das
# gerade 4000 Mails nachliefert, darf den Lauf nicht ueber den Cron-Takt ziehen.
MAX_PRO_LAUF = 120
# So viele Laufzeitpunkte bleiben stehen. 120 × 1 min = 2 Stunden — genug,
# um den Takt zu messen, zu wenig, um die Datei wachsen zu lassen.
LAUF_HISTORIE = 120
TAKT_MINDEST = 4                  # weniger Abstaende = keine Aussage ueber den Takt
# Mehr Sofortmeldungen als das in EINEM Lauf werden zu einer Zeile zusammengefasst.
MELDE_EINZELN_MAX = 12

TG_API = "https://api.telegram.org/bot%s/sendMessage"
TG_GRENZE = 3800
# Die eigene Seitenadresse steht in konfig()["seite"] — siehe _erkenne_umgebung().

# Der Agent in der Werkstatt. Geweckt wird er NUR bei echtem Urteilsbedarf.
# 🔴 Ein solcher Agent laeuft ueblicherweise isoliert und kann das Verzeichnis
# des Waechters NICHT lesen. Deshalb legt der Waechter seine unklaren Faelle in
# einen vereinbarten Ordner: der Waechter schreibt, der Agent liest, sonst
# niemand. Der Pfad steht in `konfig.json` unter "werkstatt"; ohne Eintrag ist
# dieser Weg aus.

MAX_WECKRUFE_PRO_TAG = 4
UNKLAR_SCHWELLE = 8          # so viele unklare Mails, dann lohnt sich ein Urteil

# ── dessen eigene Ablage ist der Lehrmeister ────────────────────────────────
# der Besitzer am 11.09.2026: „soll automatisch den besten weg finden und auch mehr
# kategorien erstellen, vieleicht auch aus mails kategorien erstellen."
#
# 🔑 Die Kategorien gibt es schon: 35 Themenordner, ueber Jahre von Hand
# gefuellt. Sie sind seine eigenen Entscheidungen — jede erfundene Schublade
# waere schlechter. Der Waechter liest die ABSENDER in diesen Ordnern und leitet
# daraus ab, wohin neue Post gehoert.
#
# Gemessen am 11.09.2026: 2909 Mails aus 35 Ordnern, 308 Absender. 174 davon
# schreiben eindeutig immer in denselben Ordner — das deckt 94 % der Post ab.
ABLAGE = "ablage.json"            # die gelernte Landkarte
ABLAGE_FRISCH = 24 * 3600         # einmal am Tag neu lernen, nicht in jedem Lauf
LERN_JE_ORDNER = 400              # mehr aendert das Urteil nicht, kostet nur Zeit
# 🔑 Je GROEBER die Stufe, desto mehr Beweis. Am 11.09.2026 an 670 zurueck-
# gehaltenen Mails gemessen (gelernt aus den aelteren, geprueft an den neuesten):
#
#   Staffelung                          richtig  falsch  Quote
#   0.8 / 0.8 / 0.8  (erster Wurf)          380      40  90.5%
#   dieselbe, eigene Adressen raus          380      34  91.8%
#   0.8 / 0.9 / 0.95                        359      17  95.5%
#   ohne Hauptdomain                        351      10  97.2%   ← gewaehlt
#
# Die Hauptdomain bringt 8 Treffer und 7 Fehler — ein Muenzwurf. `check24.de`
# macht Hotels UND Versicherungen, `deutschepost.de` liefert Pakete UND den
# Steuer-Newsletter. Sie darf deshalb VORSCHLAGEN, aber nicht handeln.
LERN_SCHWELLEN = {              # Stufe -> (Mindestanteil, Mindestzahl, darf_handeln)
    "absender": (0.80, 2, True),
    "domain":   (0.90, 5, True),
    "haupt":    (0.90, 5, False),
}
# Unterhalb der Handlungsschwelle wird noch VORGESCHLAGEN. Beispiel aus dessen
# Postfach: `info@account.netflix.com` liegt 5x in Shopping.Netflix und 1x
# woanders — 83 %, knapp unter der Latte. Selbst entscheiden waere zu forsch,
# aber schweigen ist unnuetz: die Seite zeigt den Vorschlag, ein Tipp macht
# daraus eine Dauerregel.
VORSCHLAG_ANTEIL = 0.5
VORSCHLAG_MINDEST = 2
# 🔑 DIE NAMENSBRUECKE. Der Besitzer hat eine zweite, voellig unabhaengige Aussage
# hinterlassen, die der Waechter bis zum 12.09.2026 ignoriert hat: **den Namen
# des Ordners**. `Synology.NAS01` ist nach `nas01@beispielhaus.example` benannt,
# `Shopping.Ikea` nach `ikea.de`. Dafuer braucht es keine Statistik — die
# Zuordnung steht im Namen.
#
# Das ist der Ausweg aus einer Sackgasse, die das Zaehlen nicht verlassen kann:
# ein Ordner, in dem noch nichts liegt, kann nichts lehren. Genau so lagen
# NAS01 bis NAS04 — angelegt, aber leer (0/0/0/1 Mails). Der Waechter kann nur
# nachahmen, und hier gab es nichts nachzuahmen.
#
# Gemessen am 12.09.2026:
#   an 1814 von Hand einsortierten Mails             99.0 % richtig
#   als Rueckfall NUR dort, wo das Zaehlen schweigt   24 von 30 richtig
# Die 6 Abweichungen sind keine Ausrutscher, sondern dessen eigene Uneindeutig-
# keit: `lidl-connect@vodafone.de` liegt mal in Shopping.Vodafone, mal in
# Shopping.Diverses; eine Cyberport-Bestellung kam ueber marketplace.amazon.de.
# Keine davon verlaesst den Shopping-Bereich.
NAMENSBRUECKE_MINDEST = 3         # „DHL" und „KIA" sollen mitzaehlen duerfen

LERN_MINDEST = 2                  # ein einziger Treffer ist kein Muster
LERN_ANTEIL = 0.8                 # Rueckfallwert

# 🔴 Diese Ordner werden NICHT gelernt. Ein Archiv ist kein Thema: „Archiv
# Gmail" hat 12396 Mails, davon kamen in der Stichprobe 235 von 400 von der Besitzer
# SELBST. Wer das mitlernt, schickt kuenftige Post ins Archiv statt in den
# richtigen Ordner — 16 von 106 Archiv-Absendern liegen auch in echten
# Themenordnern.
# ── Statistik ─────────────────────────────────────────────────────────────────
STATISTIK = "statistik.json"
STAT_FRISCH = 6 * 3600            # sechsmal am Tag reicht; Zahlen aendern sich langsam
STAT_JE_ORDNER = 1500             # Deckel je Ordner — siehe „vollstaendig_ab"
STAT_TAGE = 120                   # so weit zurueck wird der Tagesverlauf gezeigt
# 🔴 Was NICHT in eine Eingangsstatistik gehoert: Gesendetes (das hat der Besitzer
# geschrieben), Entwuerfe und Vorlagen (nie angekommen). Der Papierkorb bleibt
# DRIN — was dort liegt, ist angekommen und dann weggeworfen worden; es
# wegzulassen wuerde die Frage „wie viel Post bekomme ich?" falsch beantworten.
KEINE_STATISTIK = {"Sent Items", "Sent", "Drafts", "Templates"}

KEIN_LEHRMEISTER = {"INBOX", "Trash", "Spam", "Drafts", "Sent Items",
                    "Archiv Gmail", "Archive", "Junk", "Sent", "Templates"}

# ── Die Schubladen ────────────────────────────────────────────────────────────
# Reihenfolge = Vorrang. Die ersten vier sind dessen Alarmklassen (11.09.2026:
# „Echte Menschen, Sicherheitswarnungen, Behoerden/Versicherung/Bank,
# Fristen & Rechnungen"), die restlichen sind der Laerm.
# „stoerung" ist die fuenfte Alarmklasse. Sie ERHOEHT die Weckrufe nicht,
# sie senkt sie: vorher schlug JEDE Geraetemeldung als „mensch" Alarm,
# jetzt nur noch die, in der etwas schiefging.
ALARM = ("sicherheit", "frist", "amt", "mensch", "stoerung")
LAERM = ("werbung", "newsletter", "automatisch")

# Die Schubladen beantworten seit dem Umbau vom 11.09.2026 nur noch EINE Frage:
# muss der Besitzer das sofort wissen? Wohin eine Mail wandert, steht in seiner
# eigenen Ablage — siehe `ablage_lernen()`.
# 🔑 Der Name steht hier als deutscher Text und ist zugleich der Rueckfall:
# `schublade.<schluessel>` in den Sprachdateien schlaegt ihn. So bleibt der
# Quelltext lesbar, auch wenn keine Sprachdatei zur Hand ist.
def schubladen_namen(sprachcode: str = "") -> dict:
    """Die Schubladen mit uebersetztem Namen — EINE Stelle, an der uebersetzt
    wird, statt an jeder Anzeige."""
    raus = {}
    for k, v in SCHUBLADEN.items():
        name = _texte(sprachcode or sprache()).get("schublade." + k)
        raus[k] = {"name": name or v["name"], "icon": v["icon"]}
    return raus


SCHUBLADEN = {
    "sicherheit":  {"name": "Sicherheit",   "icon": "\U0001F510"},
    "frist":       {"name": "Fristen",      "icon": "\u23F3"},
    "amt":         {"name": "Amtliches",    "icon": "\U0001F3DB\uFE0F"},
    "mensch":      {"name": "Menschen",     "icon": "\U0001F4AC"},
    # Am 12.09.2026 dazugekommen. Vorher hatte eine fehlgeschlagene Sicherung
    # keine eigene Schublade und rutschte als „Person schreibt direkt (NAS02)"
    # in die Menschen — eine Begruendung, die schlicht nicht stimmte.
    "stoerung":    {"name": "St\u00f6rungen",   "icon": "\U0001F6A8"},
    "werbung":     {"name": "Werbung",      "icon": "\U0001F3F7\uFE0F"},
    "newsletter":  {"name": "Newsletter",   "icon": "\U0001F4F0"},
    "automatisch": {"name": "Automatisch",  "icon": "\U0001F916"},
    "unklar":      {"name": "Unklar",       "icon": "\u2753"},
}

_RX = re.IGNORECASE


def normal(t: str) -> str:
    """Kleinschreibung UND Umlaute in die Umschrift (ae/oe/ue/ss).

    🔴 Das ist kein Schoenheitspflaster, sondern die Lehre aus der ersten
    Messung: das Muster `ger[aeae]t` traf zwar „Gerät", aber NICHT „Geraet" —
    und Absender schreiben beides. Eine Sicherheitswarnung waere dadurch als
    „Automatisch" aussortiert worden. Statt jedes Muster zweimal zu schreiben
    (und beim naechsten zu vergessen), wird EINMAL normalisiert; danach sind
    alle Muster reines ASCII und koennen die Frage gar nicht mehr falsch
    beantworten.
    """
    t = (t or "").lower()
    for a, b in (("\u00e4", "ae"), ("\u00f6", "oe"), ("\u00fc", "ue"),
                 ("\u00df", "ss"), ("\u00e9", "e"), ("\u00e8", "e")):
        t = t.replace(a, b)
    return t


# Ab hier ist jedes Muster ASCII — es laeuft immer gegen `normal(...)`.

# Sicherheit: alles, was auf einen Zugriff auf ein Konto hindeutet. Diese Klasse
# wird NIE verschoben und meldet auch dann, wenn die Mail wie ein Rundschreiben
# aussieht — eine echte Warnung kommt oft genau so daher.
W_SICHER = re.compile(
    r"(sicherheitswarnung|security alert|sicherheitshinweis zu ihrem konto|"
    r"neue[rs]? anmeldung|angemeldet von|anmeldung von einem|login from|"
    r"new sign[- ]?in|new device|neues geraet|unbekanntes geraet|"
    r"passwort.{0,25}geaendert|password.{0,15}changed|kennwort.{0,25}geaendert|"
    r"konto.{0,15}gesperrt|account.{0,15}(locked|suspended)|zugang gesperrt|"
    r"verdaechtige aktivit|suspicious (activity|login)|"
    r"bestaetigungscode|verification code|sicherheitscode|security code|"
    r"zwei[- ]?faktor|two[- ]?factor|einmalpasswort|one[- ]?time password|"
    r"passwort zuruecksetzen|zuruecksetzen des passworts|password reset|"
    r"ungewoehnliche anmeldung|unusual sign)", _RX)

# Frist: irgendetwas mit einem Datum oder Betrag, das verstreichen kann.
# 🔴 `\brechnung` mit Wortgrenze: ohne sie traf das Muster auch in
# „NebenkostenabRECHNUNG" und schob eine Abrechnung der Hausverwaltung in die
# Fristen statt zum Amtlichen (in der ersten Messung passiert).
W_FRIST = re.compile(
    r"(\brechnung(en|s)?\b|\bmahnung(en)?\b|zahlungserinnerung|"
    r"zahlungsaufforderung|letzte mahnung|inkasso|faellig|zahlbar bis|"
    r"bis zum \d|\bfrist\b|kuendigungsfrist|widerrufsfrist|"
    r"verlaengert sich automatisch|beitragsanpassung|beitragserhoehung|"
    r"lastschrift|sepa[- ]?mandat|zahlungsverzug|offene forderung|"
    r"mahngebuehr|vertrag laeuft aus|termin am \d|"
    r"rueckmeldung bis|antwort bis|zahlungsziel)", _RX)

# Amt / Bank / Versicherung — am Absender ODER am Text erkannt.
W_AMT_TEXT = re.compile(
    r"(finanzamt|steuerbescheid|steuererklaerung|elster|umsatzsteuer|"
    r"bundesagentur|jobcenter|agentur fuer arbeit|"
    r"stadtverwaltung|gemeindeverwaltung|landratsamt|buergeramt|behoerde|"
    r"krankenkasse|krankenversicherung|rentenversicherung|"
    r"versicherung|\bpolice\b|schadenmeldung|schadensnummer|schadennummer|"
    r"kontoauszug|konto[- ]?nr|\biban\b|bankverbindung|\bdepot\b|"
    r"vermieter|hausverwaltung|nebenkostenabrechnung|betriebskostenabrechnung|"
    r"mietvertrag|notar|rechtsanwalt|kanzlei|amtsgericht|bussgeld|mahnbescheid)",
    _RX)
W_AMT_DOMAIN = re.compile(
    r"(\.bund\.de|\.gv\.de|[.-]amt\.|finanzamt|elster|"
    r"sparkasse|volksbank|raiffeisen|commerzbank|deutsche-bank|postbank|"
    r"\bing\.de|dkb\.de|comdirect|\bn26\.|consorsbank|targobank|santander|"
    r"allianz|\baxa\.|\bergo\.|\bhuk\.|debeka|signal-iduna|generali|gothaer|"
    r"provinzial|barmenia|wuerttembergische|hansemerkur|\bdevk\.|"
    r"\baok\.|barmer|\btk\.de|dak\.de|\bikk|knappschaft|"
    r"deutsche-rentenversicherung|arbeitsagentur)", _RX)

# Werbung — Rundschreiben MIT Verkaufsabsicht.
W_WERBUNG = re.compile(
    r"(rabatt|gutschein|\bsale\b|angebot|nur heute|nur noch heute|jetzt sichern|"
    r"jetzt kaufen|schnaeppchen|\bdeal\b|prozent sparen|% ?(rabatt|off)|"
    r"-\d{2} ?%|\d{2} ?% ?rabatt|black friday|cyber monday|ausverkauf|"
    r"exklusiv fuer sie|\bgratis\b|kostenlos testen|neukunden|"
    r"letzte chance|nicht verpassen|unschlagbar|bestpreis)", _RX)

# Automat: Absender, hinter dem kein Mensch sitzt.
W_NOREPLY = re.compile(
    r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|noreply|nicht[-_.]?antworten|"
    r"mailer[-_.]?daemon|postmaster|bounce|automat|automailer|"
    r"notification[s]?|benachrichtigung|system|robot|\bbot)\b", _RX)

# 🔴 Rollen-Postfach: dahinter steckt zwar ein Mensch, aber keiner, der der Besitzer
# PERSOENLICH schreibt. Ohne diese Liste landete jede Ticket-Antwort bei
# „Mensch" und haette ihn geweckt.
W_ROLLE = re.compile(
    r"^(info|kontakt|contact|service|support|hilfe|help|team|office|buero|"
    r"mail|email|admin|webmaster|hello|hallo|moin|shop|bestellung|order|"
    r"kundenservice|kundendienst|vertrieb|sales|marketing|presse|jobs|"
    r"newsletter|news|abo|billing|rechnungen|buchhaltung|zentrale)"
    r"([-_.]?\d+)?$", _RX)

# Firmen-Anzeigenamen sehen nicht aus wie Menschen.
W_FIRMA = re.compile(
    r"(gmbh|\bag\b|\bkg\b|\be\.?v\.?\b|ltd|inc\b|team|service|support|"
    r"shop|\binfo\b|kundenservice|kundendienst|newsletter|redaktion|"
    r"vertrieb|zentrale|hotline|noreply|no-reply)", _RX)

# Marken, deren Namen Phishing gern im Anzeigenamen traegt. Der Wert ist das
# Stueck, das in der ECHTEN Absenderdomain vorkommen muss.
MARKEN = {
    "paypal": "paypal.", "amazon": "amazon.", "apple": "apple.",
    "microsoft": "microsoft.", "netflix": "netflix.", "strato": "strato.",
    "telekom": "telekom.", "vodafone": "vodafone.", "dhl": "dhl.",
    "hermes": "hermes", "postbank": "postbank.", "sparkasse": "sparkasse",
    "commerzbank": "commerzbank.", "volksbank": "volksbank",
    "deutsche bank": "deutsche-bank.", "ing": "ing.de", "dkb": "dkb.",
    "n26": "n26.", "klarna": "klarna.", "shopify": "shopify.",
    "google": "google.", "whatsapp": "whatsapp.", "disney": "disney",
}

# 🔑 Maschinenpost. Am 12.09.2026 gemessen: dessen vier NAS melden als
# `"NAS02" <nas02@beispielhaus.example>` mit dem Betreff
# `[beispielhaus.example]Network backup - ... erfolgreich`. Die
# Menschen-Regel griff, weil „NAS02" kein Firmenname ist — und damit landete
# JEDER Sicherungslauf in einer ALARMKLASSE. Der Besitzer wurde fuer eine geglueckte
# Sicherung geweckt.
#
# 🔴 Die Erkennung braucht ZWEI Merkmale, nicht eines. „Anzeigename gleich
# Postfachname" allein wuerde `"anna" <anna@web.de>` zur Maschine erklaeren
# — genau der Fehler, der die Menschen-Regel schon einmal zerlegt hat (sie
# verlangte ein Leerzeichen im Namen und verwarf damit „Anna").
# 🔴 Die Klammer muss einen HOSTNAMEN enthalten, nicht irgendetwas. Erste
# Fassung pruefte nur auf „[…]" — die Gegenprobe erklaerte damit prompt
# `"Mike" <mike@example.de>` mit dem Betreff „[Wichtig] Kannst du mal schauen?"
# zur Maschine und haette einen Freund stummgeschaltet. Ein Hostname hat einen
# Punkt und kein Leerzeichen; „Wichtig" hat beides nicht.
BETREFF_HOST = re.compile(r"^\s*\[[^\]\s]*\.[^\]\s]*\]")  # „[host.synology.me] …"

# 🔴 Eine Stoerungsmeldung ist KEIN Laerm. Wer Maschinenpost pauschal
# stummschaltet, nimmt der Besitzer die Nachricht, dass eine Sicherung FEHLSCHLUG —
# und das waere schlimmer als die Weckrufe, die dieser Riegel abstellt.
# 🔴 ZWEI Muster, nicht eines. Die erste Fassung suchte ALLE Stoerungsworte im
# ganzen Text — und erklaerte prompt eine geglueckte Synology-Aufgabe zur
# Stoerung, weil im Bericht die Zeile „Standardausgabe/Fehler:" steht, waehrend
# zwei Zeilen darueber „Aktueller Status: 0 (normal)" zu lesen ist. Das Wort war
# eine FELDBESCHRIFTUNG, kein Ergebnis.
#
# Ergebnisworte sagen, wie es ausging — die duerfen ueberall zaehlen.
W_STOERUNG_HART = re.compile(
    r"(fehlgeschlagen|fehlschlag|gescheitert|abgebrochen|nicht\s+erfolgreich|"
    r"ausgefallen|\bdefekt\b|degraded|\bfailed\b|\bfailure\b|"
    r"\baborted\b|\bunsuccessful\b|\boffline\b)", _RX)
# Beschriftungsworte stehen in jedem Bericht, auch im geglueckten. Sie zaehlen
# NUR im Betreff — dort schreibt ein Geraet sein Ergebnis hin, keine Legende.
W_STOERUNG_WEICH = re.compile(
    r"(fehler|warnung|kritisch|\berror\b|\bwarning\b|\bcritical\b)", _RX)

BETRAG = re.compile(r"\d{1,3}(?:[.\s]\d{3})*,\d{2}\s*(?:€|EUR\b)", _RX)
DATUM = re.compile(r"\b\d{1,2}\.\s?\d{1,2}\.\s?\d{2,4}\b")

# Sammelt die Chronikeintraege DIESES Laufs (wie bei der das Schwesterprojekt haengt der
# Bericht am Protokoll, nicht an einer Liste von Aufrufstellen).
_ZU_MELDEN: list = []


# ── Grundlagen ────────────────────────────────────────────────────────────────
def log_kappen() -> None:
    pfad = os.path.join(OUT, "postwache.log")
    try:
        if os.path.getsize(pfad) < 400_000:
            return
        zeilen = open(pfad, encoding="utf-8", errors="replace").read().splitlines()
        with open(pfad + ".tmp", "w", encoding="utf-8") as fh:
            fh.write("\n".join(zeilen[-LOG_ZEILEN:]) + "\n")
        os.replace(pfad + ".tmp", pfad)
    except OSError:
        pass


def log(msg: str) -> None:
    os.makedirs(OUT, exist_ok=True)
    zeile = "[%s] %s" % (datetime.now().isoformat(timespec="seconds"), msg)
    try:
        with open(os.path.join(OUT, "postwache.log"), "a", encoding="utf-8") as fh:
            fh.write(zeile + "\n")
    except OSError:
        pass
    print(zeile)


def token(key: str = "HA_TOKEN") -> str:
    """Aus der 0600-Datei lesen. Cron uebergibt keine Umgebung, und ein Token
    gehoert nicht in den Quelltext einer lesbaren Datei."""
    v = os.environ.get(key)
    if v:
        return v.strip()
    try:
        with open(konfig()["ha"]["token_datei"] or ENVFILE_STANDARD, encoding="utf-8") as fh:
            for ln in fh:
                ln = ln.strip()
                if ln.startswith(key + "="):
                    return ln.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


# 🔑 DER GANZE MEHR-POSTFACH-UMBAU HAENGT AN DIESEN DREI ZEILEN.
# Der Waechter hat rund hundert Stellen, die Zustand lesen und schreiben — sie
# alle umzuschreiben waere hundert Gelegenheiten fuer einen Fehler gewesen.
# Stattdessen wird die EINZIGE Engstelle umgebaut: `load`/`save` legen eine
# Datei, die zu einem bestimmten Postfach gehoert, unter `state/pf/<id>/` ab.
# Der Rest des Programms merkt davon nichts.
PRO_POSTFACH = frozenset({
    "lauf.json", "koepfe.json", "absender.json", "ablage.json",
    "statistik.json", "anhaenge.json", "zaehler.json",
})
# Welches Postfach gerade bearbeitet wird. Leer = die globalen Dateien.
_PF_ID = ""


def pf_waehlen(pf_id: str) -> None:
    """Ab hier gehoert jeder Zustand diesem Postfach. Global bleibt, was fuer
    alle gilt: Einstellungen, Zugaenge, Chronik, Journal, Protokoll."""
    global _PF_ID
    _PF_ID = str(pf_id or "").strip()


def state_pfad(name: str) -> str:
    if _PF_ID and name in PRO_POSTFACH:
        return os.path.join(STATE, "pf", _PF_ID, name)
    return os.path.join(STATE, name)


def load(name: str, default):
    try:
        with open(state_pfad(name), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def save(name: str, data, modus: int = 0o644) -> None:
    ziel = state_pfad(name)
    os.makedirs(os.path.dirname(ziel), exist_ok=True)
    tmp = ziel + ".tmp"
    # 🔴 Zugangsdaten duerfen nie kurz mit 0644 auf der Platte liegen. Deshalb
    # bekommt schon die TEMPORAERE Datei die richtigen Rechte, nicht erst das Ziel.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, modus)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, ziel)


def anhaengen(datei: str, satz: dict, grenze: int) -> None:
    """Eine Zeile an eine JSONL-Datei, mit hartem Deckel. Ohne Deckel waere das
    ein Leck: der Waechter laeuft 288 Mal am Tag."""
    os.makedirs(OUT, exist_ok=True)
    pfad = os.path.join(OUT, datei)
    try:
        alt = open(pfad, encoding="utf-8").read().splitlines()
    except OSError:
        alt = []
    alt.append(json.dumps(satz, ensure_ascii=False))
    try:
        tmp = pfad + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(alt[-grenze:]) + "\n")
        os.replace(tmp, pfad)
    except OSError:
        pass


def chronik(art: str, **felder) -> None:
    """Verlauf: was der Waechter WANN getan hat. Wie bei der das Schwesterprojekt haengt der
    Telegram-Bericht hier dran — alles, was ins Protokoll geht, kann gemeldet
    werden. Eine unmittelbare Wiederholung wird zusammengefasst statt angehaengt."""
    os.makedirs(OUT, exist_ok=True)
    z = dict(felder)
    z["zeit"] = datetime.now().isoformat(timespec="seconds")
    z["art"] = art
    pfad = os.path.join(OUT, "chronik.jsonl")
    try:
        alt = open(pfad, encoding="utf-8").read().splitlines()
    except OSError:
        alt = []
    wiederholung = False
    if alt:
        try:
            letzte = json.loads(alt[-1])
            if (letzte.get("art") == art
                    and str(letzte.get("titel") or "") == str(z.get("titel") or "")):
                letzte["anzahl"] = int(letzte.get("anzahl") or 1) + 1
                letzte["zeit"] = z["zeit"]
                alt[-1] = json.dumps(letzte, ensure_ascii=False)
                wiederholung = True
        except ValueError:
            pass
    if not wiederholung:
        alt.append(json.dumps(z, ensure_ascii=False))
    try:
        tmp = pfad + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(alt[-CHRONIK_ZEILEN:]) + "\n")
        os.replace(tmp, pfad)
    except OSError:
        pass
    if not wiederholung:
        _ZU_MELDEN.append(z)


# ── Einstellungen ─────────────────────────────────────────────────────────────
def _postfach_migrieren() -> list:
    """Bis 2.x gab es genau EIN Postfach, und sein Zustand lag flach in
    `state/`. Ab 3.0.0 gibt es eine Liste, und jeder Zustand gehoert einem
    Postfach.

    🔴 Die Migration VERSCHIEBT die vorhandenen Dateien nach
    `state/pf/standard/`, sie kopiert sie nicht. Zwei Staende derselben
    Ablage waeren schlimmer als gar keine Migration: der Waechter laese den
    einen und schriebe den anderen, und niemand saehe es. `zugang.json`
    bleibt unberuehrt liegen — das ist die Sicherung, falls doch etwas fehlt.
    """
    z = load(ZUGANG, None)
    if not isinstance(z, dict) or not str(z.get("adresse") or "").strip():
        return []
    adresse = str(z["adresse"]).strip()
    eintrag = {
        "id": "standard",
        "name": adresse,
        "adresse": adresse,
        "passwort": str(z.get("passwort") or ""),
        "server": str(z.get("server") or "").strip(),
        "port": int(z.get("port") or 993),
        "an": True,
    }
    ziel = os.path.join(STATE, "pf", "standard")
    os.makedirs(ziel, exist_ok=True)
    umgezogen = []
    for name in sorted(PRO_POSTFACH):
        quelle = os.path.join(STATE, name)
        if os.path.isfile(quelle) and not os.path.exists(os.path.join(ziel, name)):
            try:
                os.replace(quelle, os.path.join(ziel, name))
                umgezogen.append(name)
            except OSError as e:
                log("Migration: %s blieb liegen (%s)" % (name, e))
    save(POSTFAECHER, {"liste": [eintrag]}, 0o600)
    log("Migration auf 3.0.0: Postfach als \u201estandard\u201c uebernommen, "
        "%d Zustandsdatei(en) umgezogen" % len(umgezogen))
    chronik("migration", titel=txt("w.migration.titel"),
            detail=txt("w.migration.detail", n=len(umgezogen)))
    return [eintrag]


def postfaecher() -> list:
    """Alle eingerichteten Postfaecher, immer vollstaendig und plausibel.

    Ein Postfach ohne Adresse oder ohne Kennung wird uebergangen statt geraten —
    ein geratener Bezeichner waere ein zweiter Zustandsordner, und der faellt
    erst auf, wenn die Zahlen nicht mehr stimmen.
    """
    v = load(POSTFAECHER, None)
    if isinstance(v, dict) and isinstance(v.get("liste"), list):
        roh = v["liste"]
    elif isinstance(v, list):
        roh = v
    else:
        roh = _postfach_migrieren()
    fertig, gesehen = [], set()
    for i, e in enumerate(roh):
        if not isinstance(e, dict):
            continue
        adresse = str(e.get("adresse") or "").strip()
        pid = re.sub(r"[^a-zA-Z0-9_-]", "", str(e.get("id") or "")) or ("pf%d" % (i + 1))
        if not adresse or pid in gesehen:
            continue
        gesehen.add(pid)
        fertig.append({
            "id": pid,
            "name": str(e.get("name") or "").strip() or adresse,
            "adresse": adresse,
            "passwort": str(e.get("passwort") or ""),
            "server": (str(e.get("server") or "").strip()
                       or konfig()["imap_server"] or _server_raten(adresse)),
            "port": int(e.get("port") or 993),
            "an": bool(e.get("an", True)),
        })
    return fertig


def _server_raten(adresse: str) -> str:
    """Nur ein Vorschlag fuer das Formular, nie eine stille Annahme im Lauf:
    wer nichts eintraegt, bekommt `imap.<domaene>` — das stimmt bei sehr vielen
    Anbietern und ist an einer Fehlermeldung sofort erkennbar, wenn nicht."""
    dom = adresse.rsplit("@", 1)[-1].strip().lower()
    return ("imap." + dom) if dom and "." in dom else ""


# ═══ Sprachen ════════════════════════════════════════════════════════════════
# Die Texte liegen als flache Schluessel/Wert-Dateien in `locales/<code>.json`
# NEBEN dem Programm — nicht im Zustandsordner: sie gehoeren zum Code und
# werden mit ihm ausgeliefert.
#
# 🔴 Die Sprache ist eine Einstellung der INSTALLATION, kein Merkmal des
# Browsers. Der Waechter schreibt Chronik und Telegram-Nachrichten, wenn
# niemand hinsieht — ein Cookie kann ihm nicht sagen, in welcher Sprache. Wer
# zwei Sprachen im Haus braucht, braucht zwei Postwachen.
SPRACHEN = ("de", "en", "es", "fr", "it")
# 🔴 Die Namen in ihrer EIGENEN Schreibweise. „Francais" statt „Français" ist
# der erste Eindruck, den ein franzoesischer Leser von der Sorgfalt des
# Programms bekommt — und er ist zutreffend.
SPRACHNAMEN = {"de": "Deutsch", "en": "English", "es": "Espa\u00f1ol",
               "fr": "Fran\u00e7ais", "it": "Italiano"}
RUECKFALL = "de"          # in dieser Sprache ist die Postwache gewachsen
_TEXTE: dict = {}


def _texte(code: str) -> dict:
    if code in _TEXTE:
        return _TEXTE[code]
    pfad = os.path.join(PROG, "locales", "%s.json" % code)
    try:
        with open(pfad, encoding="utf-8") as fh:
            _TEXTE[code] = json.load(fh)
    except (OSError, ValueError):
        _TEXTE[code] = {}
    return _TEXTE[code]


def sprache() -> str:
    code = str((load(KONFIG, None) or {}).get("sprache") or "").strip().lower()
    return code if code in SPRACHEN else RUECKFALL


def txt(schluessel: str, **werte) -> str:
    """Einen Text holen. Fehlt er in der gewaehlten Sprache, gilt die
    Rueckfallsprache; fehlt er auch dort, kommt der SCHLUESSEL zurueck.

    🔴 Der Schluessel als letzter Rueckfall ist Absicht: ein fehlender Text
    faellt dann sofort auf („kopf.titel" mitten auf der Seite), statt still eine
    leere Stelle zu hinterlassen, die niemand bemerkt.
    """
    wert = _texte(sprache()).get(schluessel)
    if wert is None and sprache() != RUECKFALL:
        wert = _texte(RUECKFALL).get(schluessel)
    if wert is None:
        return schluessel
    if werte:
        try:
            return wert.format(**werte)
        except (KeyError, IndexError, ValueError):
            return wert
    return wert


def alle_texte(code: str = "") -> dict:
    """Rueckfallsprache und gewaehlte Sprache uebereinandergelegt — damit ein
    Nachschlagen im Browser IMMER etwas findet und die Seite nie einen
    Schluessel anzeigt, nur weil eine Uebersetzung fehlt."""
    code = code or sprache()
    zusammen = dict(_texte(RUECKFALL))
    zusammen.update(_texte(code))
    return zusammen


def _erkenne_umgebung() -> dict:
    """Was findet sich auf DIESEM Rechner? Wird nur gefragt, wenn `konfig.json`
    nichts sagt.

    🔴 Der Grund fuer die Erkennung statt leerer Vorgaben: auf dem Rechner, auf
    dem die Postwache gewachsen ist, haengt der Notaus an einem Home-Assistant-
    Schalter und die Urteilshilfe an der Werkstatt. Haette 3.0.0 einfach „nichts
    voreingestellt" gesagt, waere mit dem Update beides stumm ausgefallen — der
    Notaus zuerst. Erkennung bewahrt das Gewachsene, ohne es einzubetonieren.
    """
    ha_datei = ENVFILE_STANDARD if os.path.isfile(ENVFILE_STANDARD) else ""
    # Die Werkstatt ist ein Eigenbau dieses Hauses und wird NICHT gesucht —
    # wer sie hat, traegt ihren Pfad ein. Erraten wuerde hier nur heissen, auf
    # einem fremden Rechner nach etwas zu suchen, das es dort nie gibt.
    werkstatt = ""
    try:
        rechner = socket.gethostname() or "localhost"
    except OSError:
        rechner = "localhost"
    return {
        "seite": "http://%s:8110" % rechner,
        "ha_url": HA_STANDARD if ha_datei else "",
        "ha_token_datei": ha_datei,
        "ha_schalter": SCHALTER_STANDARD if ha_datei else "",
        "werkstatt": werkstatt,
    }


_KONFIG_ZWISCHEN = None


def konfig() -> dict:
    """Die Umgebung, in der diese Postwache steht. Reihenfolge: was in
    `konfig.json` steht, sonst was erkannt wurde. Jeder Teil einzeln — wer nur
    die Seitenadresse eintraegt, verliert nicht die Home-Assistant-Anbindung."""
    global _KONFIG_ZWISCHEN
    if _KONFIG_ZWISCHEN is not None:
        return _KONFIG_ZWISCHEN
    k = load(KONFIG, None)
    k = k if isinstance(k, dict) else {}
    erkannt = _erkenne_umgebung()
    ha = k.get("ha") if isinstance(k.get("ha"), dict) else {}
    _KONFIG_ZWISCHEN = {
        "seite": str(k.get("seite") or erkannt["seite"]).rstrip("/"),
        "ha": {
            "url": str(ha.get("url", erkannt["ha_url"]) or "").rstrip("/"),
            "token_datei": str(ha.get("token_datei", erkannt["ha_token_datei"]) or ""),
            "schalter": str(ha.get("schalter", erkannt["ha_schalter"]) or "").strip(),
        },
        # Leer = keine Urteilshilfe ueber die Werkstatt. Fuer alle ausserhalb
        # dieses Hauses ist das der Normalfall; dort uebernimmt `ki.json`.
        "werkstatt": str(k.get("werkstatt", erkannt["werkstatt"]) or ""),
        "imap_server": str(k.get("imap_server") or "").strip(),
        "sprache": sprache(),
    }
    return _KONFIG_ZWISCHEN


def ha_an() -> bool:
    """Home Assistant ist angebunden, wenn eine Token-Datei benannt ist. Ohne
    sie gibt es keinen Schalter, keinen Sensor — und keine Fehlermeldungen
    darueber, dass etwas fehlt, das hier gar nicht hingehoert."""
    k = konfig()["ha"]
    return bool(k["url"] and k["token_datei"])


def einstellungen() -> dict:
    """Immer vollstaendig und plausibel. Fehlt etwas, gilt der VORSICHTIGE Fall:
    nicht scharf. Eine fehlende Datei darf niemals dazu fuehren, dass ungefragt
    im Postfach umgeraeumt wird.

    🔑 Seit dem Umbau vom 11.09.2026 tragen die Regeln nur noch die Frage
    „meldet sofort?". WOHIN eine Mail geht, steht nicht mehr hier, sondern in
    dessen eigener Ablage (`ablage.json`) — eine Einstellung kann das nicht
    mehr verstellen, weil es keine Einstellung mehr ist.
    """
    e = load(EINST, None)
    if not isinstance(e, dict):
        e = {}
    regeln = e.get("regeln") if isinstance(e.get("regeln"), dict) else {}
    fertig = {}
    for k in SCHUBLADEN:
        r = regeln.get(k) if isinstance(regeln.get(k), dict) else {}
        fertig[k] = {"melden": bool(r.get("melden", k in ALARM))}
    return {
        "scharf": bool(e.get("scharf", False)),
        "telegram": bool(e.get("telegram", True)),
        "bericht_stunde": int(e.get("bericht_stunde", 7)),
        # Wenn der Besitzer das einschaltet, bleibt alles aus den vier Alarmklassen
        # liegen, auch wenn die Ablage ein Ziel kennt.
        "wichtiges_bleibt": bool(e.get("wichtiges_bleibt", False)),
        "regeln": fertig,
        "absender_regeln": (e.get("absender_regeln")
                            if isinstance(e.get("absender_regeln"), dict) else {}),
    }


def zugang(pf_id: str = "") -> dict:
    """EIN Postfach — ohne Angabe das gerade bearbeitete, sonst das erste.

    Bleibt nach dem Umbau auf mehrere Postfaecher bestehen, weil die Seite an
    einem Dutzend Stellen genau ein Postfach braucht (Ordner anlegen, Zugang
    pruefen, Anhaenge nachtragen). Steht nichts bereit, kommt ein leeres
    Woerterbuch zurueck — der Waechter raet nicht und fragt niemanden.
    """
    faecher = postfaecher()
    if not faecher:
        return {}
    ziel = str(pf_id or _PF_ID or "").strip()
    for f in faecher:
        if f["id"] == ziel:
            return f
    return faecher[0]


# ── Bericht an der Besitzer (Telegram, derselbe Bot wie das Schwesterprojekt/DocuSort) ─────────
def tg_zugang(key: str) -> str:
    v = load("telegram_zugang.json", {}) or {}
    if isinstance(v, dict) and v.get(key):
        return str(v[key]).strip()
    return token(key)


def _tg_md(t: str) -> str:
    for z in "_*`[":
        t = t.replace(z, "\\" + z)
    return t


def telegram(text: str) -> bool:
    """Eine Nachricht an der Besitzer. Schlaegt NIE durch: ein Botenweg darf den
    Waechter nicht anhalten."""
    tok, chat = tg_zugang("TELEGRAM_BOT_TOKEN"), tg_zugang("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        return False
    if len(text) > TG_GRENZE:
        text = text[:TG_GRENZE] + "\n…"
    for modus in ("Markdown", None):
        last = {"chat_id": chat, "text": text, "disable_web_page_preview": True}
        if modus:
            last["parse_mode"] = modus
        req = urllib.request.Request(
            TG_API % tok, data=json.dumps(last).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return 200 <= r.status < 300
        except urllib.error.HTTPError as e:
            grund = ""
            try:
                grund = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            if modus and "can't parse entities" in grund.lower():
                text = text.replace("*", "").replace("_", "").replace("`", "")
                continue
            # 🔴 Der Grund darf ins Protokoll, der Token NIE — er steht in der URL.
            log("Telegram HTTP %s: %s" % (e.code, grund))
            return False
        except Exception as e:
            log("Telegram nicht erreichbar: %s" % str(e)[:160])
            return False
    return False


# ── Home Assistant ────────────────────────────────────────────────────────────
def api(path: str, tok: str, timeout: int = 20, data=None):
    """Ohne `data` ein GET, mit `data` ein POST.

    🔴 Genau diese Unterscheidung hat in der das Schwesterprojekt gefehlt: `service()` gab
    `data=` mit, `api()` kannte den Namen nicht, und JEDE Massnahme scheiterte
    still mit einem TypeError — 506 Mal in vier Tagen. Hier von Anfang an drin.
    """
    kopf = {"Authorization": "Bearer " + tok}
    roh = None
    if data is not None:
        roh = json.dumps(data).encode("utf-8")
        kopf["Content-Type"] = "application/json"
    req = urllib.request.Request(konfig()["ha"]["url"] + path, data=roh, headers=kopf)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def stopped(tok: str) -> str:
    """Notaus und Pause — als ALLERERSTES geprueft, noch vor dem Postfach.
    Die Datei wirkt auch dann, wenn HA nicht antwortet (dann ist der Schalter
    unerreichbar); der Schalter wirkt vom Handy aus."""
    if os.path.exists(DISABLED):
        try:
            grund = open(DISABLED, encoding="utf-8").read().strip()
        except OSError:
            grund = ""
        return "Datei DISABLED" + (" — " + grund[:120] if grund else "")
    if os.path.exists(PAUSE):
        try:
            p = json.load(open(PAUSE, encoding="utf-8"))
            bis = datetime.fromisoformat(p.get("bis"))
            if datetime.now() < bis:
                return "Pause bis %s — %s" % (bis.strftime("%H:%M"),
                                              p.get("grund") or "ohne Grund")
            os.remove(PAUSE)
            chronik("pause_ende", titel=txt("w.pause.titel"), detail=txt("w.pause.detail"))
        except (OSError, ValueError, TypeError):
            try:
                os.remove(PAUSE)
            except OSError:
                pass
    if not tok:
        return ""
    try:
        st = api("/api/states/" + konfig()["ha"]["schalter"], tok, timeout=10)
        if isinstance(st, dict) and st.get("state") == "off":
            return "Schalter %s ist aus" % konfig()["ha"]["schalter"]
    except urllib.error.HTTPError as e:
        # 404 = der Schalter existiert (noch) nicht. Das ist KEIN Notaus — sonst
        # koennte ein vergessener Helfer den Waechter stumm stilllegen.
        if e.code != 404:
            log("Schalter nicht lesbar: HTTP %s" % e.code)
    except Exception as e:
        log("Schalter nicht lesbar: %s" % str(e)[:120])
    return ""


def push_status(tok: str, zustand: str, attrs: dict) -> None:
    if _PF_ID:
        return          # der Sensor gilt fuer die ganze Postwache, nicht je Postfach
    """Den eigenen Zustand als sensor.postwache nach HA schreiben.

    🔴 Ein per API gesetzter Zustand ueberlebt keinen HA-Neustart — nach einem
    Neustart ist der Sensor bis zum naechsten Lauf weg. Bekannter Preis dafuer,
    dass hier keine Template-Akrobatik noetig ist.
    """
    if not tok:
        return
    try:
        api("/api/states/sensor.postwache", tok, timeout=15,
            data={"state": str(zustand)[:255], "attributes": attrs})
    except Exception as e:
        log("Statusanzeige fehlgeschlagen: %s" % str(e)[:120])


# ── Kopfzeilen lesen ──────────────────────────────────────────────────────────
def dekodieren(roh) -> str:
    """MIME-kodierte Kopfzeilen lesbar machen (=?UTF-8?B?…?=)."""
    if not roh:
        return ""
    try:
        teile = email.header.decode_header(str(roh))
    except Exception:
        return str(roh)
    raus = []
    for text, kodierung in teile:
        if isinstance(text, bytes):
            try:
                raus.append(text.decode(kodierung or "utf-8", "replace"))
            except (LookupError, TypeError):
                raus.append(text.decode("utf-8", "replace"))
        else:
            raus.append(text)
    return "".join(raus).replace("\r", " ").replace("\n", " ").strip()


def absender_teile(von: str):
    """(Anzeigename, Adresse) — beide klein geschrieben ausser dem Namen."""
    name, adresse = email.utils.parseaddr(von or "")
    return dekodieren(name).strip(), (adresse or "").strip().lower()


# ── Die Einordnung (das Herzstueck — fest, lesbar, ohne Sprachmodell) ─────────
def phishing_verdacht(name: str, adresse: str) -> str:
    """Der Anzeigename nennt eine Marke, die Absenderdomain gehoert ihr nicht.

    Das ist bewusst eng gefasst: gemeldet wird nur, wenn eine BEKANNTE Marke im
    Namen steht. Ein breiter Verdacht waere ein Fehlalarm-Generator, und ein
    Warnhinweis, der staendig faelschlich kommt, wird nicht mehr gelesen.
    """
    n = (name or "").lower()
    dom = adresse.split("@")[-1] if "@" in adresse else ""
    for marke, muss in MARKEN.items():
        if marke in n and muss not in dom:
            return "Anzeigename nennt %s, Absender ist aber %s" % (marke, dom or "?")
    return ""


def einordnen(kopf: dict, text: str) -> dict:
    """Eine Mail in genau eine Schublade legen und begruenden, warum.

    Die Begruendung ist kein Beiwerk: sie steht auf der Seite neben jeder Mail.
    Eine Einordnung, die man nicht nachvollziehen kann, kann man auch nicht
    korrigieren — und korrigieren koennen ist der ganze Sinn des Lernlaufs.

    Die Reihenfolge ist der Vorrang. Die vier Alarmklassen stehen vorn: lieber
    einmal zu viel im Posteingang als einmal zu wenig.
    """
    name = kopf.get("name") or ""
    adresse = (kopf.get("adresse") or "").lower()
    # ALLES laeuft gegen die normalisierte Fassung — siehe `normal()`.
    heu = normal((kopf.get("betreff") or "") + " " + (text or ""))
    n_name = normal(name)
    bulk = bool(kopf.get("bulk"))
    lokal = adresse.split("@")[0] if "@" in adresse else adresse
    noreply = bool(W_NOREPLY.search(lokal))
    rolle = bool(W_ROLLE.search(lokal))

    verdacht = phishing_verdacht(name, adresse)

    # 1) Sicherheit — schlaegt alles andere, auch ein Rundschreiben und auch
    #    einen no-reply-Absender: echte Warnungen kommen fast immer von einem.
    if W_SICHER.search(heu):
        g = "Wort aus dem Sicherheitsbereich in Betreff oder Text"
        if verdacht:
            g += " · \u26a0\ufe0f " + verdacht
        return {"klasse": "sicherheit", "grund": g, "phishing": verdacht}

    # 2) Frist — etwas, das verstreichen kann. Bei einem Rundschreiben nur mit
    #    echtem Betrag oder Datum; sonst landet jede Shop-Werbung mit dem Wort
    #    „Angebot" in den Fristen.
    if W_FRIST.search(heu):
        hat_zahl = bool(BETRAG.search(heu) or DATUM.search(heu))
        if not bulk or hat_zahl:
            g = "Frist- oder Zahlungsbegriff" + (" mit Betrag/Datum" if hat_zahl else "")
            return {"klasse": "frist", "grund": g, "phishing": verdacht}

    # 3) Amt, Bank, Versicherung — am Absender ODER am Text.
    if W_AMT_DOMAIN.search(adresse) or W_AMT_TEXT.search(heu):
        woran = "Absenderdomain" if W_AMT_DOMAIN.search(adresse) else "Betreff/Text"
        return {"klasse": "amt", "grund": "Amt/Bank/Versicherung erkannt am " + woran,
                "phishing": verdacht}

    # 3b) Maschinenpost. ZWEI Merkmale muessen zusammenkommen, nie eines
    #     allein: der Anzeigename ist derselbe wie der Postfachname UND der
    #     Betreff beginnt mit einem HOSTNAMEN in eckigen Klammern.
    #
    #     🔴 Die erste Fassung liess auch „Name sieht aus wie eine Geraete-
    #     kennung" (maro02) als zweites Merkmal gelten. Die Gegenprobe erledigte
    #     das sofort: `"robert2" <robert2@example.de>` mit „Servus" waere eine
    #     Maschine gewesen. Ein Riegel, der nur in EINE Richtung geprueft wird,
    #     ist kein Beweis — dieselbe Lehre wie beim Tippriegel der Seite.
    maschine = bool(n_name and n_name == normal(lokal)
                    and BETREFF_HOST.search(kopf.get("betreff") or ""))
    if maschine:
        n_betreff = normal(kopf.get("betreff") or "")
        if W_STOERUNG_HART.search(heu) or W_STOERUNG_WEICH.search(n_betreff):
            return {"klasse": "stoerung",
                    "grund": "Ger\u00e4t %s meldet eine St\u00f6rung" % name.strip(),
                    "phishing": verdacht}
        return {"klasse": "automatisch",
                "grund": "Ger\u00e4t %s meldet Vollzug \u2014 nichts zu tun" % name.strip(),
                "phishing": verdacht}

    # 4) Mensch — VOR dem Laerm, damit ein Bekannter mit Newsletter-Signatur
    #    nicht im Werbeordner landet. Ein Mensch ist: kein Rundschreiben, kein
    #    Automat, kein Rollen-Postfach und kein Firmenname im Absender.
    if not bulk and not noreply and not rolle and not W_FIRMA.search(n_name):
        if name.strip():
            return {"klasse": "mensch",
                    "grund": "Person schreibt direkt (%s)" % name.strip(),
                    "phishing": verdacht}
        # Kein Anzeigename, aber eine persoenlich aussehende Adresse
        # (vorname.nachname@...) ist immer noch ein Mensch.
        if re.fullmatch(r"[a-z]{2,}[._-][a-z]{2,}\d{0,3}", lokal):
            return {"klasse": "mensch",
                    "grund": "Persoenliche Adresse ohne Anzeigenamen (%s)" % lokal,
                    "phishing": verdacht}

    # 5) Laerm — nur was sich selbst als Rundschreiben ausweist.
    if bulk:
        grund_kopf = kopf.get("bulk_grund") or "Listenkopf"
        if W_WERBUNG.search(heu):
            return {"klasse": "werbung",
                    "grund": "Rundschreiben (%s) mit Verkaufsabsicht" % grund_kopf,
                    "phishing": verdacht}
        return {"klasse": "newsletter",
                "grund": "Rundschreiben — %s" % grund_kopf,
                "phishing": verdacht}

    # 6) Automat ohne Listenkopf (Bestellbestaetigung, Systemmeldung).
    if noreply:
        return {"klasse": "automatisch",
                "grund": "Absender %s antwortet nicht (Automat)" % lokal,
                "phishing": verdacht}

    # 7) Rollen-Postfach ohne weiteres Merkmal: dahinter sitzt ein Mensch, aber
    #    der Waechter kann nicht sagen, ob es der Besitzer betrifft. Genau das sind die
    #    Faelle, fuer die es den Agenten gibt.
    return {"klasse": "unklar",
            "grund": ("Rollen-Postfach %s, sonst kein Merkmal" % lokal) if rolle
                     else ("Passt in keine Schublade — weder Rundschreiben noch "
                           "Automat noch erkennbare Person"),
            "phishing": verdacht}


# ── IMAP ──────────────────────────────────────────────────────────────────────
class Postfach:
    """Duenne Huelle um imaplib. Jeder Abruf benutzt BODY.PEEK, jede Mailbox
    wird im Lernlauf readonly geoeffnet."""

    def __init__(self, zug: dict, schreiben: bool):
        self.zug = zug
        self.schreiben = schreiben
        self.m = None
        self.trenner = "/"

    def __enter__(self):
        socket.setdefaulttimeout(45)
        self.m = imaplib.IMAP4_SSL(self.zug["server"], self.zug["port"])
        self.m.login(self.zug["adresse"], self.zug["passwort"])
        # Den Hierarchie-Trenner beim Server ERFRAGEN statt ihn zu raten:
        # dem Anbieter benutzt „.", andere „/" — wer raet, legt Ordner mit Punkt im
        # Namen an statt Unterordner.
        try:
            typ, zeilen = self.m.list()
            if typ == "OK" and zeilen:
                t = re.search(rb'\(.*?\)\s+"([^"]*)"', zeilen[0])
                if t:
                    self.trenner = t.group(1).decode() or "/"
        except Exception:
            pass
        self.m.select("INBOX", readonly=not self.schreiben)
        return self

    def __exit__(self, *a):
        try:
            self.m.close()
        except Exception:
            pass
        try:
            self.m.logout()
        except Exception:
            pass

    def neue_uids(self, ab: int) -> list:
        """Alle UIDs groesser als `ab`. Ein UID-Bereich ist der einzige Weg, der
        ohne vollstaendigen Postfach-Durchlauf auskommt."""
        typ, daten = self.m.uid("search", None, "UID %d:*" % (ab + 1))
        if typ != "OK" or not daten or not daten[0]:
            return []
        # 🔴 „UID n:*" liefert bei leerem Rest die hoechste vorhandene UID mit
        # zurueck — der Server kann den Bereich nicht leer beantworten. Deshalb
        # wird hier zusaetzlich gegen `ab` gefiltert, sonst gilt dieselbe Mail
        # bei jedem Lauf erneut als neu.
        return sorted(u for u in (int(x) for x in daten[0].split()) if u > ab)

    def letzte_uids(self, anzahl: int) -> list:
        typ, daten = self.m.uid("search", None, "ALL")
        if typ != "OK" or not daten or not daten[0]:
            return []
        alle = sorted(int(x) for x in daten[0].split())
        return alle[-anzahl:]

    def kopf_und_text(self, uid: int):
        """Kopfzeilen und Textanfang — IMMER mit PEEK, damit die Mail nicht als
        gelesen markiert wird."""
        typ, daten = self.m.uid(
            "fetch", str(uid),
            "(BODY.PEEK[HEADER] BODY.PEEK[TEXT]<0.4000>)")
        if typ != "OK" or not daten:
            return None, ""
        roh_kopf, roh_text = b"", b""
        for teil in daten:
            if not isinstance(teil, tuple) or len(teil) < 2:
                continue
            marke = teil[0] or b""
            if b"HEADER" in marke:
                roh_kopf = teil[1]
            elif b"TEXT" in marke:
                roh_text = teil[1]
        if not roh_kopf:
            return None, ""
        msg = email.message_from_bytes(roh_kopf)
        text = ""
        if roh_text:
            try:
                text = roh_text.decode("utf-8", "replace")
                text = re.sub(r"<[^>]+>", " ", text)
                text = re.sub(r"\s+", " ", text)[:3000]
            except Exception:
                text = ""
        return msg, text


    def uidvalidity(self, name: str) -> int:
        """Der Nummernkreis eines Ordners. Aendert er sich, sind alle gemerkten
        UIDs wertlos."""
        try:
            typ, dat = self.m.status(self._zitat(name), "(UIDVALIDITY)")
            if typ == "OK" and dat:
                t = re.search(rb"UIDVALIDITY\s+(\d+)",
                              dat[0] if isinstance(dat[0], bytes) else str(dat[0]).encode())
                if t:
                    return int(t.group(1))
        except Exception:
            pass
        return 0

    def strukturen(self, uids: list) -> dict:
        """Der BAUPLAN mehrerer Mails (BODYSTRUCTURE) — ohne ein einziges Byte
        Anhang zu holen. Das ist der ganze Trick am Dokumenten-Index: er kostet
        so wenig, dass er auch rueckwirkend ueber Jahre laufen kann."""
        raus = {}
        for i in range(0, len(uids or []), 100):
            teil = ",".join(str(u) for u in uids[i:i + 100])
            try:
                typ, daten = self.m.uid("fetch", teil, "(BODYSTRUCTURE)")
            except Exception as e:
                log("Bauplan nicht lesbar: %s" % str(e)[:120])
                continue
            if typ == "OK" and daten:
                raus.update(_strukturen_lesen(daten))
        return raus

    def teil_holen(self, uid: int, nr: str, kodierung: str) -> bytes:
        """EINEN Anhang holen — mit PEEK, damit die Mail ungelesen bleibt."""
        typ, daten = self.m.uid("fetch", str(uid), "(BODY.PEEK[%s])" % nr)
        if typ != "OK" or not daten:
            return b""
        roh = b""
        for st in daten:
            if isinstance(st, tuple) and len(st) >= 2:
                roh += st[1] or b""
        return teil_entpacken(roh, kodierung)

    def teil_aus_ordner(self, ordner: str, uid: int, nr: str, kodierung: str) -> bytes:
        """Dasselbe fuer eine Mail, die schon einsortiert ist. Mit demselben
        `finally`, das auf INBOX zurueckstellt — ein Wechsel muss dahin zurueck,
        wo er herkam (gemessen am 11.09.2026)."""
        if not ordner or ordner == "INBOX":
            return self.teil_holen(uid, nr, kodierung)
        try:
            self.m.select(self._zitat(ordner), readonly=True)
            return self.teil_holen(uid, nr, kodierung)
        finally:
            try:
                self.m.select("INBOX", readonly=not self.schreiben)
            except Exception:
                pass

    def ordner_anhaenge(self, name: str, ab_uid: int, deckel: int, ende: float):
        """(Kopf, UID, Bauplan) der Mails eines Ordners ab einer UID.

        Liest in Bloecken und hoert auf, wenn die Zeit um ist — der Rest kommt
        beim naechsten Lauf. readonly und BODY.PEEK, wie ueberall sonst."""
        saetze, hoechste, fertig = [], int(ab_uid or 0), True
        try:
            self.m.select(self._zitat(name), readonly=True)
            typ, daten = self.m.uid("search", None, "UID %d:*" % (int(ab_uid or 0) + 1))
            if typ != "OK" or not daten or not daten[0]:
                return saetze, hoechste, True
            uids = sorted(u for u in (int(x) for x in daten[0].split()) if u > int(ab_uid or 0))
            if len(uids) > deckel:
                log("Nachtrag %s: %d Mails, es werden nur die neuesten %d "
                    "gelesen — die aelteren bleiben ungesehen."
                    % (name, len(uids), deckel))
                uids = uids[-deckel:]
            for i in range(0, len(uids), 100):
                if time.time() >= ende:
                    fertig = False
                    break
                teil = uids[i:i + 100]
                liste = ",".join(str(u) for u in teil)
                typ, roh = self.m.uid(
                    "fetch", liste,
                    "(BODY.PEEK[HEADER.FIELDS (FROM DATE SUBJECT MESSAGE-ID)])")
                koepfe = {}
                if typ == "OK":
                    letzte_uid = 0
                    for st in roh or []:
                        if isinstance(st, bytes):
                            t = re.search(rb"UID\s+(\d+)", st)
                            if t:
                                letzte_uid = int(t.group(1))
                            continue
                        if isinstance(st, tuple) and len(st) >= 2:
                            t = re.search(rb"UID\s+(\d+)", st[0] or b"")
                            u = int(t.group(1)) if t else letzte_uid
                            if u:
                                koepfe[u] = email.message_from_bytes(st[1] or b"")
                typ, roh = self.m.uid("fetch", liste, "(BODYSTRUCTURE)")
                bauplaene = _strukturen_lesen(roh) if typ == "OK" else {}
                for u in teil:
                    msg = koepfe.get(u)
                    struct = bauplaene.get(u)
                    if msg is None or struct is None:
                        continue
                    saetze.append((kopf_lesen(msg, ""), u, struct))
                    hoechste = max(hoechste, u)
        finally:
            # 🔴 ZURUECK AUF INBOX — sonst greift jeder folgende Abruf ins Leere.
            try:
                self.m.select("INBOX", readonly=not self.schreiben)
            except Exception:
                pass
        return saetze, hoechste, fertig

    def ordner_liste(self) -> list:
        """Alle Ordner des Postfachs, so wie der Server sie nennt."""
        try:
            typ, zeilen = self.m.list()
        except Exception:
            return []
        if typ != "OK":
            return []
        raus = []
        for zl in zeilen or []:
            t = re.search(r'"[^"]*" "?([^"]+?)"?\s*$',
                          zl.decode("utf-8", "replace") if isinstance(zl, bytes) else str(zl))
            if t:
                raus.append(t.group(1))
        return raus

    def absender_im_ordner(self, name: str, grenze: int) -> dict:
        """Wer hat in diesen Ordner geschrieben? NUR die Absender-Kopfzeile —
        kein Betreff, kein Text. Und readonly, damit das Lernen im Postfach
        garantiert nichts veraendert."""
        zaehler = {}
        try:
            self.m.select(self._zitat(name), readonly=True)
            typ, daten = self.m.uid("search", None, "ALL")
            if typ != "OK" or not daten or not daten[0]:
                return zaehler
            uids = daten[0].split()[-grenze:]
            # In Bloecken holen — 400 Einzelabrufe dauern Minuten, 4 Bloecke
            # Sekunden.
            for i in range(0, len(uids), 100):
                teil = b",".join(uids[i:i + 100]).decode()
                typ, antw = self.m.uid("fetch", teil,
                                       "(BODY.PEEK[HEADER.FIELDS (FROM)])")
                if typ != "OK":
                    continue
                for st in antw:
                    if not isinstance(st, tuple) or len(st) < 2:
                        continue
                    _, adr = email.utils.parseaddr(
                        email.message_from_bytes(st[1]).get("From", ""))
                    adr = (adr or "").strip().lower()
                    if adr and "@" in adr:
                        zaehler[adr] = zaehler.get(adr, 0) + 1
        except Exception as e:
            log("Ordner %s nicht lesbar: %s" % (name, str(e)[:100]))
        finally:
            # 🔴 ZURUECK AUF INBOX. Ohne das bleibt der Server auf dem zuletzt
            # gelesenen Ordner stehen, und jeder folgende `uid fetch` greift
            # ins Leere — die UIDs stammen aus dem Posteingang, gelten dort
            # aber nicht. Gemessen am 11.09.2026: nach dem Lernen wurden 14
            # Mails abgerufen und KEINE einzige gefunden, der Lauf meldete
            # trotzdem Erfolg. Ein Wechsel muss dahin zurueck, wo er herkam.
            try:
                self.m.select("INBOX", readonly=not self.schreiben)
            except Exception:
                pass
        return zaehler

    def koepfe_im_ordner(self, name: str, grenze: int):
        """(Absender, Datum) der neuesten `grenze` Mails eines Ordners.

        Gleiche Bauart wie `absender_im_ordner`: readonly, BODY.PEEK, in
        Bloecken — und dasselbe `finally`, das auf INBOX zurueckstellt. Ohne das
        bleibt der Server auf dem zuletzt gelesenen Ordner stehen und jeder
        folgende Abruf greift ins Leere (gemessen am 11.09.2026).

        Gibt zusaetzlich zurueck, ob der Deckel gegriffen hat — daraus entsteht
        die Grenze, ab der der Tagesverlauf VOLLSTAENDIG ist. Ein Diagramm, das
        an seinem linken Rand still abfaellt, weil dort Daten fehlen, luegt.
        """
        raus, gedeckelt = [], False
        try:
            self.m.select(self._zitat(name), readonly=True)
            typ, daten = self.m.uid("search", None, "ALL")
            if typ != "OK" or not daten or not daten[0]:
                return raus, False
            alle = daten[0].split()
            gedeckelt = len(alle) > grenze
            uids = alle[-grenze:]
            for i in range(0, len(uids), 200):
                teil = b",".join(uids[i:i + 200]).decode()
                typ, antw = self.m.uid("fetch", teil,
                                       "(BODY.PEEK[HEADER.FIELDS (FROM DATE)])")
                if typ != "OK":
                    continue
                for st in antw:
                    if not isinstance(st, tuple) or len(st) < 2:
                        continue
                    msg = email.message_from_bytes(st[1])
                    _, adr = email.utils.parseaddr(msg.get("From", ""))
                    adr = (adr or "").strip().lower()
                    try:
                        ts = email.utils.parsedate_to_datetime(msg.get("Date", ""))
                        ts = ts.astimezone()
                    except Exception:
                        ts = None
                    if adr and "@" in adr and ts is not None:
                        raus.append((adr, ts, dekodieren(msg.get("From", ""))))
        except Exception as e:
            log("Statistik: Ordner %s nicht lesbar: %s" % (name, str(e)[:100]))
        finally:
            try:
                self.m.select("INBOX", readonly=not self.schreiben)
            except Exception:
                pass
        return raus, gedeckelt

    def voller_name(self, name: str) -> str:
        """Die Ordnernamen kommen aus `ordner_liste()` und sind damit bereits
        so geschrieben, wie der Server sie kennt (bei dem Anbieter „Shopping.Paypal").
        Hier wird deshalb NICHTS zusammengebaut — wer den Trenner selbst
        einsetzt, legt bei der naechsten Server-Eigenart einen Ordner mit einem
        Punkt im Namen an, statt in den vorhandenen zu schreiben."""
        return name

    def ordner_sicherstellen(self, pfad: str) -> str:
        """Unterordner unterhalb von INBOX anlegen, falls noetig. Gibt den
        serverrichtigen vollen Namen zurueck."""
        voll = "INBOX" + self.trenner + pfad.replace("/", self.trenner)
        try:
            typ, _ = self.m.create(self._zitat(voll))
        except Exception:
            typ = "NO"
        try:
            self.m.subscribe(self._zitat(voll))
        except Exception:
            pass
        return voll

    @staticmethod
    def _zitat(name: str) -> str:
        return '"%s"' % name.replace('"', '')

    def verschieben(self, uid: int, ziel_voll: str) -> bool:
        """Kopieren, dann im Quellordner abhaken. 🔴 Es wird NIE EXPUNGE
        aufgerufen und nie ein \\Deleted ohne vorherige erfolgreiche Kopie
        gesetzt — schlaegt die Kopie fehl, bleibt die Mail unberuehrt liegen."""
        try:
            typ, _ = self.m.uid("copy", str(uid), self._zitat(ziel_voll))
            if typ != "OK":
                return False
            self.m.uid("store", str(uid), "+FLAGS", "(\\Deleted)")
            self.m.expunge()
            return True
        except Exception as e:
            log("Verschieben von UID %s fehlgeschlagen: %s" % (uid, str(e)[:140]))
            return False

    def zurueck(self, uid: int, quell_voll: str) -> bool:
        """Eine Verschiebung rueckgaengig machen: aus dem Zielordner zurueck in
        den Posteingang. Wird von der Seite aufgerufen."""
        try:
            self.m.select(self._zitat(quell_voll), readonly=False)
            typ, _ = self.m.uid("copy", str(uid), "INBOX")
            if typ != "OK":
                return False
            self.m.uid("store", str(uid), "+FLAGS", "(\\Deleted)")
            self.m.expunge()
            return True
        except Exception as e:
            log("Zuruecksortieren UID %s fehlgeschlagen: %s" % (uid, str(e)[:140]))
            return False
        finally:
            try:
                self.m.select("INBOX", readonly=not self.schreiben)
            except Exception:
                pass


# ── Dokumente in der Post ─────────────────────────────────────────────────────
# der Besitzer, 25.09.2026: „wenn emails mit anhaengen kommen die aus einer pdf
# bestehen oder aehnlichen dokumenten wo infos drin sind, keine fotos oder png
# oder so ein kram, dann sollen diese anhaenge nach docusort gegeben werden
# damit sie dort einsortiert werden. ich moechte auch gezielt in einsortierten
# mails suchen koennen nach eben solchen dokumenten, als auch im nachgang."
#
# 🔑 DAS SIND ZWEI DINGE, und sie werden getrennt gehalten — genauso wie
# „meldet sofort?" und „wohin gehoert es?" seit dem Umbau 2.0 getrennt sind:
#
#   1. WAS HAENGT DRAN?  -> der INDEX. Er entsteht fuer jede Mail aus dem
#      BODYSTRUCTURE, also aus dem BAUPLAN der Mail. Dafuer wird kein einziges
#      Byte Anhang geholt. Deshalb kann er auch rueckwirkend ueber Jahre
#      gezogen werden, und deshalb ist er vollstaendig, selbst wenn DocuSort
#      gar nicht eingerichtet ist.
#
#   2. WAS GEHT WEITER?  -> die UEBERGABE an DocuSort. Nur Dokumente, nur was
#      DocuSort auch verdauen kann, nie bei Phishing-Verdacht, und nur solange
#      der Zugang eingerichtet und eingeschaltet ist.
#
# Wer beides zusammenwirft, hat einen Index, der von einer Einstellung abhaengt
# — und findet spaeter genau die Mails nicht, bei denen die Uebergabe gerade aus
# war.
ANHAENGE = "anhaenge.json"          # der Index: was haengt an welcher Mail
ANHANG_INDEX_MAX = 20000            # Deckel; aeltestes fliegt zuerst raus
# 🔴 KEIN Arbeitsdeckel, sondern eine Notbremse. Der erste Nachtrag am
# 25.09.2026 lief mit 4000 je Ordner — „Archiv Gmail" hat 12 396 Mails, also
# wurden die aeltesten 8400 uebersprungen. Und weil der Nachtrag sich die
# HOECHSTE gelesene UID merkt, waeren sie nie wieder angesehen worden: eine
# Luecke, die sich selbst zudeckt. Getaktet wird ueber die ZEIT (`frist`), und
# die kann beim naechsten Lauf weitermachen. Greift der Deckel doch, sagt er es.
NACHTRAG_JE_ORDNER = 50000
NACHTRAG_FRIST = 45                 # Sekunden je Lauf — der Cron kommt jede Minute
NACHTRAG_FRISCH = 12 * 3600         # zweimal am Tag reicht fuer den Nachtrag

# 🔴 Diese Ordner bleiben beim Nachtragen draussen. Die Liste ist ABSICHTLICH
# eine andere als `KEIN_LEHRMEISTER`: dort geht es ums LERNEN (ein Archiv ist
# kein Thema, es verdirbt die Zuordnung), hier ums FINDEN. Eine Telekom-Rechnung
# von 2019 liegt im Archiv — sie dort nicht zu indizieren hiesse, genau die
# Frage nicht beantworten zu koennen, die der Besitzer gestellt hat.
KEIN_NACHTRAG = {"Trash", "Spam", "Junk", "Papierkorb"}

# 🔑 DIE ENDUNG ENTSCHEIDET, NICHT DER MIME-TYP. Sehr viele Absender deklarieren
# ihre PDF-Rechnung als `application/octet-stream` — wer nach dem Typ geht,
# uebersieht sie. Der Typ wird nur befragt, wenn es keinen brauchbaren
# Dateinamen gibt.
DOK_ENDUNGEN = {".pdf", ".csv", ".doc", ".docx", ".odt", ".rtf",
                ".xls", ".xlsx", ".ods", ".ppt", ".pptx", ".odp"}
# „keine fotos oder png oder so ein kram" — dessen Wort, und es ist die
# richtige Grenze: ein Bild kann ein Dokument sein (ein Scan), aber es ist
# NICHT zu unterscheiden von dem Firmenlogo unter der Signatur.
BILD_ENDUNGEN = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff",
                 ".webp", ".heic", ".heif", ".svg", ".ico", ".avif"}
DOK_MIME = {"application/pdf", "text/csv", "application/csv",
            "application/msword", "application/rtf", "text/rtf",
            "application/vnd.ms-excel", "application/vnd.ms-powerpoint",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "application/vnd.oasis.opendocument.text",
            "application/vnd.oasis.opendocument.spreadsheet",
            "application/vnd.oasis.opendocument.presentation"}
# 🔴 Was DocuSort WIRKLICH annimmt (gemessen am lebenden Dienst 0.58.1:
# `ALLOWED_SUFFIXES` in web/app.py plus der CSV-Weg in die Finanzen). Ein
# .docx wuerde dort als `rejected` zurueckkommen — es gilt hier deshalb als
# Dokument (und ist findbar), wird aber nicht uebergeben, und das steht am
# Eintrag. Stilles Verwerfen waere der schlimmere Fehler.
DS_ENDUNGEN = {".pdf", ".csv"}


def endung(name: str) -> str:
    n = (name or "").strip().lower()
    i = n.rfind(".")
    return n[i:] if 0 < i and len(n) - i <= 6 else ""


def anhang_art(typ: str, subtyp: str, name: str) -> str:
    """„dokument" | „bild" | „kram". Ein Wort, an dem der Rest haengt."""
    e = endung(name)
    if e in DOK_ENDUNGEN:
        return "dokument"
    if e in BILD_ENDUNGEN:
        return "bild"
    if e:
        return "kram"          # .ics, .p7s, .zip, .eml, .vcf …
    m = ("%s/%s" % (typ or "", subtyp or "")).lower()
    if m in DOK_MIME:
        return "dokument"
    if (typ or "").lower() == "image":
        return "bild"
    return "kram"


def ds_verdaulich(name: str) -> bool:
    return endung(name) in DS_ENDUNGEN


# ── BODYSTRUCTURE lesen ──────────────────────────────────────────────────────
# Der Bauplan einer Mail kommt als verschachtelte IMAP-Liste. Die muss man
# zerlegen, und zwar richtig: ein Dateiname darf Klammern enthalten
# („Rechnung (Kopie).pdf"), und lange oder umlauthaltige Namen kommen als
# LITERAL — imaplib reicht die als eigenes Stueck durch, mitten im Satz.
def _imap_stuecke(roh: bytes):
    """Zerlegt eine IMAP-Antwort in (art, wert): „(", „)", „s" (Zeichenkette in
    Anfuehrungszeichen) oder „a" (blankes Wort, z. B. NIL oder eine Zahl).

    🔴 Die Unterscheidung s/a ist kein Zierrat: eine Klammer INNERHALB von
    Anfuehrungszeichen ist Text, keine Klammer. Wer beides gleich behandelt,
    zerreisst jeden Dateinamen mit Klammer und damit den ganzen Bauplan."""
    i, n = 0, len(roh)
    while i < n:
        c = roh[i:i + 1]
        if c in b" \t\r\n":
            i += 1
            continue
        if c in b"()":
            yield (c.decode(), None)
            i += 1
            continue
        if c == b'"':
            j, buf = i + 1, bytearray()
            while j < n:
                d = roh[j:j + 1]
                if d == b"\\" and j + 1 < n:
                    buf += roh[j + 1:j + 2]
                    j += 2
                    continue
                if d == b'"':
                    break
                buf += d
                j += 1
            yield ("s", buf.decode("utf-8", "replace"))
            i = j + 1
            continue
        j = i
        while j < n and roh[j:j + 1] not in b' \t\r\n()"':
            j += 1
        yield ("a", roh[i:j].decode("utf-8", "replace"))
        i = j


def _imap_baum(roh: bytes) -> list:
    """Die Antwortzeile als verschachtelte Liste. NIL wird zu None."""
    stapel = [[]]
    for art, wert in _imap_stuecke(roh):
        if art == "(":
            neu = []
            stapel[-1].append(neu)
            stapel.append(neu)
        elif art == ")":
            if len(stapel) > 1:
                stapel.pop()
        elif art == "a":
            stapel[-1].append(None if wert.upper() == "NIL" else wert)
        else:
            stapel[-1].append(wert)
    return stapel[0]


def _klammerstand(roh: bytes, stand: int) -> int:
    """Klammerstand fortschreiben. Klammern in Anfuehrungszeichen zaehlen nicht."""
    i, n, drin = 0, len(roh), False
    while i < n:
        c = roh[i:i + 1]
        if drin:
            if c == b"\\":
                i += 2
                continue
            if c == b'"':
                drin = False
        elif c == b'"':
            drin = True
        elif c == b"(":
            stand += 1
        elif c == b")":
            stand -= 1
        i += 1
    return stand


def _fetch_zeilen(daten) -> list:
    """Aus imaplibs Antwortliste je Mail EINE vollstaendige Zeile bauen.

    🔴 Wo ein Stueck aufhoert und das naechste anfaengt, entscheidet NICHT das
    Aussehen der Zeile, sondern die KLAMMERBILANZ. Eine Antwort mit Literal
    kommt als Tupel, der Rest derselben Zeile als eigenes Stueck danach — wer
    auf „faengt mit einer Zahl an" prueft, zerschneidet genau die Mails mit
    umlauthaltigem Dateinamen, also die interessanten."""
    zeilen, akt, stand = [], b"", 0
    for el in daten:
        if isinstance(el, tuple) and len(el) >= 2:
            kopf = re.sub(rb"\s*\{\d+\}\s*$", b"", el[0] or b"")
            lit = (el[1] or b"").replace(b"\\", b"\\\\").replace(b'"', b'\\"')
            stueck = kopf + b'"' + lit.replace(b"\r", b" ").replace(b"\n", b" ") + b'"'
        elif isinstance(el, bytes):
            stueck = el
        else:
            continue
        akt = (akt + b" " + stueck) if akt else stueck
        stand = _klammerstand(stueck, stand)
        if akt and stand <= 0:
            zeilen.append(akt)
            akt, stand = b"", 0
    if akt:
        zeilen.append(akt)
    return zeilen


def _strukturen_lesen(daten) -> dict:
    """{uid: Bauplan} aus einer FETCH-Antwort."""
    raus = {}
    for zeile in _fetch_zeilen(daten):
        try:
            baum = _imap_baum(zeile)
        except Exception:
            continue
        liste = next((x for x in baum if isinstance(x, list)), None)
        if not isinstance(liste, list):
            continue
        uid, struct = 0, None
        for i, el in enumerate(liste):
            if not isinstance(el, str) or i + 1 >= len(liste):
                continue
            if el.upper() == "UID":
                try:
                    uid = int(str(liste[i + 1]))
                except (TypeError, ValueError):
                    uid = 0
            elif el.upper() == "BODYSTRUCTURE":
                struct = liste[i + 1]
        if uid and isinstance(struct, list):
            raus[uid] = struct
    return raus


def _param(paare, schluessel: str) -> str:
    """Einen Parameter aus der flachen Paarliste holen — RFC 2047 (=?utf-8?B?…?=)
    und RFC 2231 (filename*0*, in Stuecken, prozentkodiert) inbegriffen. Beide
    Formen kommen in echter Post vor, und zwar genau bei den langen deutschen
    Rechnungsnamen."""
    if not isinstance(paare, list):
        return ""
    d = {}
    for i in range(0, len(paare) - 1, 2):
        k = paare[i]
        if isinstance(k, str):
            d[k.lower()] = paare[i + 1]
    s = schluessel.lower()
    if d.get(s):
        return dekodieren(str(d[s]))
    teile = sorted((k for k in d if k.startswith(s + "*")),
                   key=lambda k: int(re.sub(r"\D", "", k) or 0))
    if not teile:
        return ""
    roh = "".join(str(d[k] or "") for k in teile)
    if any(k.endswith("*") for k in teile):
        zeichensatz, _, rest = roh.partition("'")
        _, _, wert = rest.partition("'")
        try:
            return urllib.parse.unquote(wert or roh,
                                        encoding=zeichensatz or "utf-8",
                                        errors="replace").strip()
        except (LookupError, ValueError):
            return urllib.parse.unquote(wert or roh).strip()
    return dekodieren(roh)


def _teile(struct, praefix: str = "") -> list:
    """Alle einfachen Teile einer Mail mit ihrer IMAP-Teilenummer.

    Die Nummern sind das, womit man den Anhang spaeter einzeln holt. Bei einer
    eingebetteten Mail (weitergeleitete Post!) zaehlen die inneren Teile unter
    der Nummer der aeusseren weiter — genau dort haengt oft die Rechnung."""
    if not isinstance(struct, list) or not struct:
        return []
    if isinstance(struct[0], list):
        raus, i = [], 0
        for kind in struct:
            if not isinstance(kind, list):
                break
            i += 1
            raus += _teile(kind, "%s%d" % (praefix + "." if praefix else "", i))
        return raus
    nr = praefix or "1"
    typ = str(struct[0] or "")
    sub = str(struct[1] if len(struct) > 1 and struct[1] else "")
    params = struct[2] if len(struct) > 2 and isinstance(struct[2], list) else []
    kod = str(struct[5] or "") if len(struct) > 5 else ""
    try:
        groesse = int(str(struct[6])) if len(struct) > 6 else 0
    except (TypeError, ValueError):
        groesse = 0
    # Die Verfuegung (attachment/inline) steht in den Erweiterungen, und deren
    # Platz haengt vom Typ ab. Statt drei Sonderfaelle zu zaehlen wird die
    # FORM gesucht: eine Liste, deren erstes Wort „attachment" oder „inline"
    # ist. Das haelt auch, wenn ein Server ein Feld weglaesst.
    verfuegung, dparams = "", []
    for el in struct[7:]:
        if (isinstance(el, list) and el and isinstance(el[0], str)
                and el[0].lower() in ("attachment", "inline")):
            verfuegung = el[0].lower()
            dparams = el[1] if len(el) > 1 and isinstance(el[1], list) else []
            break
    name = _param(dparams, "filename") or _param(params, "name")
    eintrag = {"nr": nr, "typ": typ, "subtyp": sub, "name": name,
               "groesse": groesse, "kodierung": kod.upper(),
               "verfuegung": verfuegung}
    raus = [eintrag]
    if typ.upper() == "MESSAGE" and sub.upper() == "RFC822" and len(struct) > 8:
        innen = struct[8]
        if isinstance(innen, list) and innen:
            if isinstance(innen[0], list):
                raus += _teile(innen, nr)
            else:
                raus += _teile(innen, nr + ".1")
    return raus


def anhaenge_der_mail(struct) -> list:
    """Nur echte Anhaenge: alles mit Dateinamen. Der Fliesstext hat keinen."""
    raus = []
    for t in _teile(struct):
        name = (t.get("name") or "").strip()
        if not name:
            continue
        if t["typ"].upper() == "MESSAGE" and t["subtyp"].upper() == "RFC822":
            continue            # die Huelle selbst nicht, nur was drin haengt
        raus.append({
            "n": name[:200],
            "art": anhang_art(t["typ"], t["subtyp"], name),
            "b": int(t.get("groesse") or 0),
            "t": t["nr"],
            "k": t["kodierung"],
            "m": ("%s/%s" % (t["typ"], t["subtyp"])).lower()[:80],
        })
    return raus


def teil_entpacken(roh: bytes, kodierung: str) -> bytes:
    k = (kodierung or "").upper()
    try:
        if k == "BASE64":
            return base64.b64decode(re.sub(rb"[^A-Za-z0-9+/=]", b"", roh or b""))
        if k == "QUOTED-PRINTABLE":
            return quopri.decodestring(roh or b"")
    except Exception as e:
        log("Anhang nicht entpackbar (%s): %s" % (k, str(e)[:100]))
        return b""
    return roh or b""

# ── Der Index ────────────────────────────────────────────────────────────────
def anhang_schluessel(kopf: dict, ordner: str, uid: int) -> str:
    """🔴 Der Schluessel ist die Message-Id, NICHT (Ordner, UID). Eine UID gilt
    nur in ihrem Ordner, und der Waechter verschiebt Post — dieselbe Mail
    bekommt beim Verschieben eine neue UID. Wer darueber schluesselt, hat jede
    einsortierte Mail zweimal im Index und keine davon auffindbar."""
    mid = str(kopf.get("message_id") or "").strip()
    if mid:
        return "m" + hashlib.sha1(mid.encode("utf-8", "replace")).hexdigest()[:18]
    return "o%s|%d" % (ordner, uid)


def anhang_index() -> dict:
    d = load(ANHAENGE, None)
    if not isinstance(d, dict):
        d = {}
    if not isinstance(d.get("stand"), dict):
        d["stand"] = {}
    if not isinstance(d.get("eintraege"), dict):
        d["eintraege"] = {}
    return d


def anhang_index_sichern(idx: dict) -> None:
    e = idx.get("eintraege") or {}
    if len(e) > ANHANG_INDEX_MAX:
        # Aeltestes zuerst raus — nach dem Datum der Mail, nicht nach dem
        # Zeitpunkt des Eintragens: sonst wirft ein Nachtrag alter Ordner
        # genau das weg, was er gerade gefunden hat.
        nach_alter = sorted(e.items(), key=lambda kv: str(kv[1].get("datum") or ""))
        for k, _ in nach_alter[:len(e) - ANHANG_INDEX_MAX]:
            e.pop(k, None)
    idx["eintraege"] = e
    idx["gesichert"] = datetime.now().isoformat(timespec="seconds")
    idx["zahlen"] = dokument_zaehlung(idx)
    save(ANHAENGE, idx)
    dokument_kurz_schreiben(idx)


def dokument_kurz_schreiben(idx: dict) -> dict:
    """Die Kurzfassung neben den Index legen.

    🔑 Sie liegt SEPARAT, weil die Seite alle 30 s auffrischt und dafuer nicht
    eine Datei mit Zehntausenden Eintraegen einlesen soll.

    🔴 Sie muss aber IMMER nachgezogen werden, wenn sich der Index geaendert
    haben KANN — nicht nur beim Sichern. Die Seite sieht ausschliesslich diese
    Datei: hinkt sie hinterher, zeigt die Seite einen Zustand, den es nicht mehr
    gibt, und die Nachfrage-Wache springt gar nicht erst an (gemessen am
    25.09.2026 mit einem kuenstlichen Eintrag)."""
    kurz = dict(idx.get("zahlen") or dokument_zaehlung(idx))
    kurz["nachtrag"] = idx.get("nachtrag") or {}
    kurz["ordner_offen"] = sum(1 for s in (idx.get("stand") or {}).values()
                               if not s.get("fertig"))
    kurz["ordner"] = len(idx.get("stand") or {})
    kurz["zeit"] = idx.get("gesichert") or datetime.now().isoformat(timespec="seconds")
    try:
        os.makedirs(OUT, exist_ok=True)
        tmp = os.path.join(OUT, "dokumente.json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(kurz, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(OUT, "dokumente.json"))
    except OSError:
        pass
    return kurz


def anhang_eintragen(idx: dict, kopf: dict, ordner: str, uid: int,
                     dateien: list) -> dict:
    """Eine Mail in den Index. Gibt den Eintrag zurueck (neu oder aufgefrischt).

    Ein vorhandener Eintrag behaelt seine Uebergabe-Ergebnisse — ein zweiter
    Blick auf dieselbe Mail darf nicht vergessen, dass ihr PDF laengst in
    DocuSort liegt."""
    s = anhang_schluessel(kopf, ordner, uid)
    alt = idx["eintraege"].get(s) if isinstance(idx["eintraege"].get(s), dict) else {}
    neu = {
        "ordner": ordner,
        "uid": int(uid or 0),
        "datum": str(kopf.get("datum") or ""),
        "adresse": str(kopf.get("adresse") or ""),
        "name": str(kopf.get("name") or ""),
        "betreff": str(kopf.get("betreff") or ""),
        "dateien": dateien,
        "ds": alt.get("ds") if isinstance(alt.get("ds"), list) else [],
        "gesehen": alt.get("gesehen") or datetime.now().isoformat(timespec="seconds"),
    }
    idx["eintraege"][s] = neu
    return neu


def dokument_zaehlung(idx: dict) -> dict:
    """Die drei Zahlen, die auf die Seite gehoeren."""
    mails, dok, uebergeben, offen = 0, 0, 0, 0
    for e in (idx.get("eintraege") or {}).values():
        mails += 1
        hat = [f for f in (e.get("dateien") or []) if f.get("art") == "dokument"]
        dok += len(hat)
        for d in (e.get("ds") or []):
            if d.get("stand") in ("abgelegt", "pruefen", "doppelt", "finanzen"):
                uebergeben += 1
            elif d.get("stand") == "uebergeben":
                offen += 1
    return {"mails": mails, "dokumente": dok,
            "uebergeben": uebergeben, "unterwegs": offen}


def dokumente_suchen(idx: dict, frage: str = "", art: str = "dokument",
                     von: str = "", bis: str = "", grenze: int = 200) -> dict:
    """„zeige mir alle mails die eine pdf dran haben von der telekom".

    Gesucht wird ueber `normal()` — dieselbe Funktion, die schon die Einordnung
    von „Gerät"/„Geraet" befreit hat. Ein Wort muss irgendwo vorkommen: im
    Absender, im Namen, im Betreff, im Dateinamen oder im Ordner. Mehrere
    Woerter muessen ALLE vorkommen, aber nicht nebeneinander — „telekom pdf"
    und „pdf telekom" finden dasselbe."""
    worte = [normal(w) for w in re.split(r"\s+", frage or "") if w.strip()]
    # Einmal fuer die ganze Suche: welche Datei liegt — an WELCHER Mail auch
    # immer — schon in DocuSort?
    zwillinge = ds_zwillinge(idx)
    treffer = []
    for schluessel, e in (idx.get("eintraege") or {}).items():
        dateien = e.get("dateien") or []
        if art and art != "alle":
            dateien = [f for f in dateien if f.get("art") == art]
            if not dateien:
                continue
        d = str(e.get("datum") or "")[:10]
        if von and d and d < von:
            continue
        if bis and d and d > bis:
            continue
        heuhaufen = normal(" ".join([
            str(e.get("adresse") or ""), str(e.get("name") or ""),
            str(e.get("betreff") or ""), str(e.get("ordner") or ""),
            " ".join(str(f.get("n") or "") for f in (e.get("dateien") or [])),
            " ".join(endung(str(f.get("n") or "")).lstrip(".")
                     for f in (e.get("dateien") or [])),
        ]))
        if worte and not all(w in heuhaufen for w in worte):
            continue
        # 🔑 Ob eine Datei noch uebergeben werden kann, entscheidet der SERVER —
        # an derselben Stelle, an der es auch die Uebergabe entscheidet. Die
        # Seite zeichnet nur noch, was hier steht. Vorher lag dieselbe Regel
        # zusaetzlich im Browser, und zwei Meinungen darueber sind zwei
        # Gelegenheiten, dasselbe Dokument ein zweites Mal hochzuladen.
        eigen = ds_eintraege(e)
        hat_ort = bool(int(e.get("uid") or 0))
        gezeigt = []
        for f in dateien:
            name = str(f.get("n") or "")
            satz = eigen.get(name) or {}
            stand = str(satz.get("stand") or "")
            zwilling = None
            if f.get("art") == "dokument" and ds_offen(satz):
                if not ds_verdaulich(name):
                    stand = "kann_docusort_nicht"      # sagen, nicht anbieten
                else:
                    zwilling = zwillinge.get((name.lower(), int(f.get("b") or 0)))
            gezeigt.append(dict(
                f, stand=stand, doc=str(satz.get("doc") or ""),
                text=str(satz.get("text") or ""), zwilling=zwilling,
                gebbar=bool(f.get("art") == "dokument" and ds_verdaulich(name)
                            and ds_offen(satz) and not zwilling and hat_ort)))
        # 🔴 Der Schluessel MUSS mit heraus: er ist die einzige Handhabe, mit
        # der die Seite spaeter „gib das an DocuSort" sagen kann.
        treffer.append(dict(e, schluessel=schluessel, dateien_gezeigt=gezeigt,
                            gebbar=any(f["gebbar"] for f in gezeigt)))
    treffer.sort(key=lambda e: str(e.get("datum") or ""), reverse=True)
    return {"gesamt": len(treffer), "treffer": treffer[:grenze]}


# ── Uebergabe an DocuSort ────────────────────────────────────────────────────
# 🔑 Der Weg ist DocuSorts VORDERTUER: `POST /upload`, dieselbe, die auch der
# Browser benutzt. Der Besitzer am 20.09.2026 zu DocuSort: „es gibt nur den upload
# button mehr nicht, zentrale anlaufstelle, dort alles reinkippen und docusort
# macht den rest, keine unterschiedlichen stellen." Ein zweiter Weg (SSH in den
# Eingangsordner der VM) waere schneller gebaut und haette genau diese Regel
# gebrochen — und einen Schluessel gebraucht, den niemand mehr zurueckziehen
# kann, ohne es zu wissen.
#
# 🔴 Was dieser Zugang KANN: hochladen, den Stand abfragen — und, weil DocuSort
# nur zwei Rollen kennt, auch die Bibliothek und die Finanzen LESEN. Ein
# eigener, engerer Rang („darf nur hereinlegen") waere sauberer; das ist eine
# Aenderung an DocuSort und steht als naechster Schritt in der Notiz. Bis dahin
# gilt: der Zugang ist ein eigener Benutzer, kein Admin, und der Besitzer kann ihn in
# DocuSort mit einem Klick deaktivieren — dann ist die Sitzung im selben
# Moment tot (DocuSort loescht beim Deaktivieren die Sitzungen).
DS_ZUGANG = "docusort.json"          # 0600: URL + Benutzer + Passwort
DS_SITZUNG = "docusort_sitzung.json"  # 0600: das Sitzungsmerkmal
DS_MAX_MB = 25.0                     # groesseres wird nicht hochgeladen
DS_JE_LAUF = 12                      # so viele Uebergaben hoechstens pro Lauf


def ds_zugang() -> dict:
    z = load(DS_ZUGANG, None)
    if not isinstance(z, dict):
        z = {}
    return {
        "url": str(z.get("url") or "").strip().rstrip("/"),
        "benutzer": str(z.get("benutzer") or "").strip(),
        "passwort": str(z.get("passwort") or ""),
        "aktiv": bool(z.get("aktiv", True)),
        "max_mb": float(z.get("max_mb") or DS_MAX_MB),
    }


class _OhneUmleitung(urllib.request.HTTPRedirectHandler):
    """🔴 Umleitungen werden NICHT verfolgt, und das ist beides Mal der Kern:

    Die Anmeldung antwortet mit 303 und legt das Sitzungsmerkmal in GENAU diese
    Antwort — wer folgt, bekommt die Startseite und hat das Merkmal verloren.

    Und eine abgelaufene Sitzung schickt den Upload auf die Anmeldeseite. Wer
    folgt, bekommt HTTP 200 mit einem HTML-Formular zurueck: ein „Erfolg", bei
    dem nichts hochgeladen wurde. Ein 303 ist hier eine ANTWORT, keine Panne."""

    def redirect_request(self, *a, **k):
        return None


class DocuSort:
    def __init__(self, zug: dict):
        self.basis = zug["url"]
        self.benutzer = zug["benutzer"]
        self.passwort = zug["passwort"]
        self.max_mb = float(zug.get("max_mb") or DS_MAX_MB)
        self.sitzung = str((load(DS_SITZUNG, {}) or {}).get("cookie") or "")
        self.oeffner = urllib.request.build_opener(_OhneUmleitung)
        self.fehler = ""

    # -- Grundlagen ----------------------------------------------------------
    def _anfrage(self, weg: str, daten=None, typ: str = "", sitzung: bool = True):
        req = urllib.request.Request(self.basis + weg, data=daten,
                                     method="POST" if daten is not None else "GET")
        if typ:
            req.add_header("Content-Type", typ)
        if sitzung and self.sitzung:
            req.add_header("Cookie", "ds_session=" + self.sitzung)
        req.add_header("User-Agent", "Postwache/%s" % VERSION)
        return self.oeffner.open(req, timeout=90)

    def anmelden(self) -> str:
        """Leerer Rueckgabewert heisst: angemeldet."""
        if not (self.basis and self.benutzer and self.passwort):
            return "Kein DocuSort-Zugang hinterlegt."
        koerper = urllib.parse.urlencode(
            {"username": self.benutzer, "password": self.passwort,
             "next": "/"}).encode("utf-8")
        kopfzeilen = None
        try:
            antw = self._anfrage("/login", koerper,
                                 "application/x-www-form-urlencoded", sitzung=False)
            kopfzeilen = antw.headers
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                kopfzeilen = e.headers          # DAS ist der Erfolgsfall
            elif e.code == 429:
                return "DocuSort bremst die Anmeldung (zu viele Versuche)."
            elif e.code == 401:
                return "DocuSort weist Benutzer oder Passwort zurueck."
            else:
                return "Anmeldung fehlgeschlagen (HTTP %d)." % e.code
        except Exception as e:
            return "DocuSort nicht erreichbar: %s" % str(e)[:120]
        for roh in (kopfzeilen.get_all("Set-Cookie") if kopfzeilen else None) or []:
            t = re.match(r"\s*ds_session=([^;]+)", roh)
            if t:
                self.sitzung = t.group(1)
                # 🔴 0600. Das Merkmal IST der Zugang, solange es gilt.
                save(DS_SITZUNG, {"cookie": self.sitzung,
                                  "zeit": datetime.now().isoformat(timespec="seconds")},
                     0o600)
                return ""
        return "Anmeldung ohne Sitzungsmerkmal — DocuSort hat sie abgelehnt."

    def _mit_sitzung(self, weg, daten=None, typ=""):
        """Einmal versuchen, bei abgelaufener Sitzung neu anmelden, einmal
        wiederholen. Mehr nicht — wer endlos wiederholt, sperrt sich aus."""
        for versuch in (1, 2):
            if not self.sitzung:
                fehl = self.anmelden()
                if fehl:
                    return None, fehl
            try:
                return self._anfrage(weg, daten, typ), ""
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308, 401, 403) and versuch == 1:
                    self.sitzung = ""           # Sitzung ist tot
                    continue
                return None, "HTTP %d" % e.code
            except Exception as e:
                return None, str(e)[:140]
        return None, "Sitzung liess sich nicht erneuern."

    # -- Was Postwache braucht ----------------------------------------------
    def hochladen(self, dateiname: str, inhalt: bytes) -> dict:
        """Eine Datei durch die Vordertuer. Gibt den Stand zurueck, wie er in
        den Index geschrieben wird."""
        grenze = "----postwache%s" % hashlib.sha1(
            (dateiname + str(len(inhalt))).encode("utf-8", "replace")).hexdigest()[:20]
        sicher = re.sub(r'[\r\n"\\]', "_", dateiname)[:120] or "anhang"
        koerper = b"".join([
            ("--%s\r\nContent-Disposition: form-data; name=\"files\"; "
             "filename=\"%s\"\r\nContent-Type: application/octet-stream\r\n\r\n"
             % (grenze, sicher)).encode("utf-8"),
            inhalt, b"\r\n",
            ("--%s--\r\n" % grenze).encode("utf-8"),
        ])
        antw, fehl = self._mit_sitzung(
            "/upload", koerper, "multipart/form-data; boundary=%s" % grenze)
        if antw is None:
            return {"stand": "fehler", "text": fehl}
        try:
            d = json.loads(antw.read().decode("utf-8", "replace"))
        except Exception:
            # 🔴 Kein JSON heisst: das war nicht der Upload, sondern eine Seite.
            return {"stand": "fehler", "text": "Antwort war kein Upload-Ergebnis."}
        if d.get("saved"):
            return {"stand": "uebergeben", "inbox": d["saved"][0].get("inbox_name") or "",
                    "text": ""}
        if d.get("imported"):
            erste = d["imported"][0] or {}
            if erste.get("error"):
                return {"stand": "fehler", "text": str(erste["error"])[:160]}
            return {"stand": "finanzen",
                    "text": "%s Buchungen" % (erste.get("rows_inserted") or 0)}
        if d.get("rejected"):
            return {"stand": "kann_docusort_nicht",
                    "text": "DocuSort nimmt diesen Dateityp nicht."}
        return {"stand": "fehler", "text": "DocuSort hat nichts gemeldet."}

    def stand(self, inbox_name: str) -> dict:
        antw, fehl = self._mit_sitzung(
            "/api/status/" + urllib.parse.quote(inbox_name))
        if antw is None:
            return {}
        try:
            return json.loads(antw.read().decode("utf-8", "replace")) or {}
        except Exception:
            return {}

    def version(self) -> str:
        try:
            antw = self._anfrage("/api/version", sitzung=False)
            return str(json.loads(antw.read().decode("utf-8", "replace")).get("current") or "")
        except Exception as e:
            self.fehler = str(e)[:140]
            return ""


def ds_bereit():
    """(Verbindung, Grund). Ist DocuSort nicht eingerichtet oder ausgeschaltet,
    ist das KEIN Fehler — der Index entsteht trotzdem vollstaendig, und die
    Uebergabe laesst sich jederzeit nachholen."""
    z = ds_zugang()
    if not (z["url"] and z["benutzer"] and z["passwort"]):
        return None, "nicht eingerichtet"
    if not z["aktiv"]:
        return None, "ausgeschaltet"
    return DocuSort(z), ""


# 🔑 EINE Regel, wer noch einmal darf — und nur diese eine. Alles, was einen
# Stand hat, ist durch; ausgenommen das, was ausdruecklich wiederholbar ist.
# Der Knopf auf der Seite, das Kaestchen und die Uebergabe selbst fragen
# dieselbe Funktion. Drei Meinungen darueber waeren drei Gelegenheiten, dasselbe
# Dokument ein zweites Mal hochzuladen.
# (Welche Staende wiederholbar sind, steht in `ds_offen` — die EINE Stelle.)
# Wie aussagekraeftig ein Stand ist — „abgelegt" schlaegt „unterwegs", wenn
# dieselbe Datei an mehreren Mails haengt.
DS_RANG = {"uebergeben": 1, "doppelt": 2, "finanzen": 3, "pruefen": 3,
           "abgelegt": 4}
# So lange darf DocuSort „die Datei kenne ich nicht" sagen, bevor die Uebergabe
# als verschollen gilt. 🔴 Direkt nach dem Hochladen ist „unknown" NORMAL: die
# Datei ist aus dem Eingang verschwunden, der Datenbankeintrag noch nicht da.
# Wer daraus sofort einen Fehler macht, meldet jede gesunde Uebergabe als kaputt.
DS_VERSCHOLLEN_S = 15 * 60

DS_UEBERSETZT = {"filed": "abgelegt", "review": "pruefen", "duplicate": "doppelt",
                 "failed": "fehler", "queued": "uebergeben",
                 "processing": "uebergeben"}


def ds_offen(satz) -> bool:
    """Darf diese Datei (noch) an DocuSort gegeben werden?

    🔴 „Fehler" ist ZWEIERLEI, und der Unterschied entscheidet:
      · die **Uebergabe** ist gescheitert — dann liegt drueben nichts, und ein
        zweiter Versuch ist genau richtig;
      · DocuSort hat die Datei, ist aber beim Verarbeiten gescheitert (es nennt
        dann eine Dokumentnummer) — dann liegt sie dort schon. Ein zweiter
        Upload macht nur ein Duplikat; wiederholt wird DORT, am Dokument.
    An der Dokumentnummer sind die beiden auseinanderzuhalten."""
    satz = satz or {}
    st = str(satz.get("stand") or "")
    if not st or st == "verschollen":
        return True
    if st == "fehler":
        return not str(satz.get("doc") or "")
    return False


def ds_eintraege(eintrag: dict) -> dict:
    return {str(d.get("n") or ""): d for d in (eintrag.get("ds") or [])
            if isinstance(d, dict)}


def ds_uebergeben(ds, pf, eintrag: dict, ordner: str, uid: int,
                  phishing: str = "", offen: int = DS_JE_LAUF) -> int:
    """Die Dokumente EINER Mail an DocuSort geben. Gibt zurueck, wie viele
    wirklich hochgeladen wurden.

    🔴 Das muss VOR dem Verschieben passieren. Nach dem Verschieben gibt es die
    UID im Posteingang nicht mehr, und die neue kennt niemand."""
    schon = ds_eintraege(eintrag)
    getan = 0
    for f in (eintrag.get("dateien") or []):
        if f.get("art") != "dokument":
            continue
        name = str(f.get("n") or "")
        alt = schon.get(name) or {}
        if not ds_offen(alt):
            continue                       # schon durch, nicht zweimal
        satz = {"n": name, "stand": "", "inbox": "", "doc": "", "text": "",
                "zeit": datetime.now().isoformat(timespec="seconds")}
        if phishing:
            satz["stand"] = "phishing"
            satz["text"] = "Phishing-Verdacht — nichts weitergereicht."
        elif not ds_verdaulich(name):
            satz["stand"] = "kann_docusort_nicht"
            satz["text"] = "DocuSort nimmt nur PDF und CSV."
        elif int(f.get("b") or 0) > ds.max_mb * 1024 * 1024:
            satz["stand"] = "zu_gross"
            satz["text"] = "%.1f MB — ueber der Grenze von %.0f MB." % (
                int(f.get("b") or 0) / 1048576.0, ds.max_mb)
        elif getan >= offen:
            continue                       # naechster Lauf, Eintrag bleibt offen
        else:
            try:
                roh = pf.teil_aus_ordner(ordner, uid, str(f.get("t") or "1"),
                                         str(f.get("k") or ""))
            except Exception as e:
                roh = b""
                log("Anhang %s (UID %s) nicht holbar: %s" % (name, uid, str(e)[:110]))
            if not roh:
                satz["stand"] = "fehler"
                satz["text"] = "Anhang liess sich nicht holen."
            else:
                erg = ds.hochladen(name, roh)
                satz.update({k: v for k, v in erg.items() if k in ("stand", "inbox", "text")})
                if satz["stand"] == "uebergeben":
                    getan += 1
        eintrag.setdefault("ds", [])
        eintrag["ds"] = [d for d in eintrag["ds"] if str(d.get("n") or "") != name]
        eintrag["ds"].append(satz)
    return getan


def ds_stand_nachtragen(ds, idx: dict, grenze: int = 60) -> int:
    """Was aus den Uebergaben geworden ist — abgehakt wird erst, wenn DocuSort
    es BESTAETIGT.

    🔑 „Hochgeladen" ist nicht „angekommen". DocuSort braucht fuer Texterkennung
    und Einordnung Sekunden bis Minuten; bis dahin steht die Datei auf
    „unterwegs" und wird bei jedem Lauf (und von der Seite aus) nachgefragt.

    🔴 Und es gibt ein Ende: sagt DocuSort laenger als `DS_VERSCHOLLEN_S`
    (15 Minuten) „die Datei kenne ich nicht", gilt die Uebergabe als
    **verschollen** und darf wiederholt werden. Ohne diese Frist stuende so eine
    Datei fuer immer auf „unterwegs" — ein Zustand, der nie endet, ist keine
    Auskunft.

    (Die Minutenzahl steht hier ausgeschrieben: eine Zeichenkette mit `%`
    dahinter ist KEIN Dokumentationstext mehr, sondern ein Ausdruck — die
    Funktion haette danach gar keine Beschreibung.)
    """
    geaendert, jetzt = 0, datetime.now()
    for e in (idx.get("eintraege") or {}).values():
        for d in (e.get("ds") or []):
            if d.get("stand") != "uebergeben" or not d.get("inbox"):
                continue
            if geaendert >= grenze:
                return geaendert
            antw = ds.stand(str(d["inbox"]))
            roh = str(antw.get("status") or "")
            d["gefragt"] = int(d.get("gefragt") or 0) + 1
            if roh == "unknown":
                seit = str(d.get("unbekannt_seit") or "")
                if not seit:
                    d["unbekannt_seit"] = jetzt.isoformat(timespec="seconds")
                    continue
                try:
                    alt = (jetzt - datetime.fromisoformat(seit)).total_seconds()
                except (TypeError, ValueError):
                    alt = 0
                if alt >= DS_VERSCHOLLEN_S:
                    d["stand"] = "verschollen"
                    d["text"] = ("DocuSort kennt die Datei nach %.0f Minuten "
                                 "nicht — noch einmal uebergeben." % (alt / 60))
                    geaendert += 1
                continue
            d.pop("unbekannt_seit", None)
            neu = DS_UEBERSETZT.get(roh, "")
            if not neu or neu == "uebergeben":
                continue               # queued/processing: DocuSort arbeitet noch
            d["stand"] = neu
            d["doc"] = str(antw.get("doc_id") or "")
            d["text"] = str(antw.get("category") or "")
            geaendert += 1
    return geaendert


def ds_zwillinge(idx: dict) -> dict:
    """(Dateiname, Groesse) -> der beste bekannte DocuSort-Stand DIESER Datei,
    gleich an welcher Mail sie hing.

    🔑 Dieselbe Rechnung haengt oft an mehreren Mails: einmal im Themenordner,
    einmal im Archiv, einmal weitergeleitet. Ohne diesen Abgleich saehe man beim
    zweiten Suchen wieder einen leeren Knopf — und gaebe sie ein zweites Mal.

    🔴 Verglichen wird Name UND Groesse. Der Name allein waere zu grob:
    „Rechnung.pdf" heisst bei zwanzig Absendern so. Die Groesse ist die des
    kodierten Teils aus dem Bauplan — fuer dieselbe Datei in derselben Mailform
    stabil. Das ist ein starkes Indiz, kein Beweis; DocuSort selbst entscheidet
    am Inhalt (SHA256) und meldet „hatte ich schon"."""
    raus = {}
    for e in (idx.get("eintraege") or {}).values():
        groessen = {str(f.get("n") or ""): int(f.get("b") or 0)
                    for f in (e.get("dateien") or [])}
        for d in (e.get("ds") or []):
            if ds_offen(d):
                continue          # was noch offen ist, taugt nicht als Beleg
            st = str(d.get("stand") or "")
            name = str(d.get("n") or "")
            k = (name.lower(), groessen.get(name, -1))
            alt = raus.get(k)
            if alt is None or DS_RANG.get(st, 0) > DS_RANG.get(alt["stand"], 0):
                raus[k] = {"stand": st, "doc": str(d.get("doc") or ""),
                           "betreff": str(e.get("betreff") or "")[:90],
                           "datum": str(e.get("datum") or "")}
    return raus

# ── Rueckwirkend: was haengt an der Post, die laengst einsortiert ist? ────────
def anhaenge_nachtragen(pf, idx: dict, frist: int = NACHTRAG_FRIST,
                        nur: str = "") -> dict:
    """Ordner fuer Ordner durchsehen — mit ZEITBUDGET.

    🔑 Der Nachtrag laeuft im selben Minutentakt wie alles andere und darf ihn
    nicht sprengen. Deshalb merkt sich der Index je Ordner, bis zu welcher UID
    er gelesen hat: der naechste Lauf macht dort weiter. Nach ein paar Laeufen
    ist alles drin, danach kostet es nichts mehr.

    🔴 Die UID gilt nur bei gleicher UIDVALIDITY. Aendert der Server sie (der
    Ordner wurde neu angelegt), ist jede gemerkte Nummer wertlos und der Ordner
    wird von vorn gelesen. Ohne diese Pruefung fehlen genau die Mails, die nach
    einem Serverumbau kamen — und niemand merkt es."""
    t0 = time.time()
    ende = t0 + max(5.0, float(frist))
    alle = [o for o in pf.ordner_liste()
            if o.split(pf.trenner)[-1] not in KEIN_NACHTRAG and o not in KEIN_NACHTRAG]
    if nur:
        wunsch = {nur} if isinstance(nur, str) else set(nur)
        alle = [o for o in alle if o in wunsch]
    # INBOX zuerst, danach die noch nicht fertigen — wer zuletzt abgebrochen
    # hat, kommt als Naechster dran.
    def rang(o):
        st = (idx.get("stand") or {}).get(o) or {}
        return (0 if o == "INBOX" else 1, 0 if not st.get("fertig") else 1,
                str(st.get("zeit") or ""))
    alle.sort(key=rang)

    gesehen, neue, mails, offen = 0, 0, 0, []
    for o in alle:
        if time.time() >= ende:
            offen.append(o)
            continue
        st = (idx.get("stand") or {}).get(o) or {}
        try:
            uidv = pf.uidvalidity(o)
        except Exception:
            uidv = 0
        ab = int(st.get("bis") or 0)
        if uidv and int(st.get("uidvalidity") or 0) != uidv:
            ab = 0                      # Nummernkreis gewechselt: alles neu
        try:
            saetze, hoechste, fertig = pf.ordner_anhaenge(
                o, ab, NACHTRAG_JE_ORDNER, ende)
        except Exception as e:
            log("Nachtrag %s: %s" % (o, str(e)[:120]))
            continue
        gesehen += 1
        mails += len(saetze)
        for kopf, uid, struct in saetze:
            anh = anhaenge_der_mail(struct)
            if not anh:
                continue
            vorher = anhang_schluessel(kopf, o, uid) in idx["eintraege"]
            anhang_eintragen(idx, kopf, o, uid, anh)
            if not vorher:
                neue += 1
        idx["stand"][o] = {
            "uidvalidity": uidv,
            "bis": max(int(st.get("bis") or 0) if ab else 0, hoechste),
            "zeit": datetime.now().isoformat(timespec="seconds"),
            "fertig": bool(fertig),
        }
        if not fertig:
            offen.append(o)
    idx["nachtrag"] = {
        "zeit": datetime.now().isoformat(timespec="seconds"),
        "ordner": gesehen, "mails": mails, "neu": neue,
        "offen": len(offen) + sum(1 for o in alle
                                  if not ((idx.get("stand") or {}).get(o) or {}).get("fertig")
                                  and o not in offen),
        "dauer": round(time.time() - t0, 1),
    }
    return idx["nachtrag"]


def dokumente_faellig() -> bool:
    """Steht ueberhaupt etwas an — OHNE den grossen Index zu lesen?

    🔴 Der Waechter laeuft jede Minute. Wer bei jedem Lauf eine Datei mit
    Zehntausenden Eintraegen liest und wieder schreibt, hat ein Leck gebaut,
    das man erst an der Platte merkt. Die Kurzfassung reicht fuer die Frage."""
    try:
        with open(os.path.join(OUT, "dokumente.json"), encoding="utf-8") as fh:
            k = json.load(fh)
    except (OSError, ValueError):
        return True
    if int(k.get("unterwegs") or 0) > 0 or int(k.get("ordner_offen") or 0) > 0:
        return True
    n = k.get("nachtrag") if isinstance(k.get("nachtrag"), dict) else {}
    try:
        return ((datetime.now() - datetime.fromisoformat(n["zeit"])).total_seconds()
                >= NACHTRAG_FRISCH)
    except (KeyError, TypeError, ValueError):
        return True


def dokumente_pflegen(pf, idx=None, ds=None, ds_bekannt: bool = False) -> None:
    """Der Dokumenten-Teil eines Laufs — Stand nachfragen, Rueckstand nachtragen,
    Index sichern.

    🔴 Das haengt ABSICHTLICH nicht am Zweig „es gibt neue Post". Der Lauf ohne
    neue Mail kehrt frueh um, und das ist der Normalfall: an einem ruhigen Tag
    kommen 5 Mails, aber 1440 Laeufe. Haenge den Nachtrag dort hinein, und die
    rueckwirkende Suche bleibt tagelang leer — gemessen am 25.09.2026 gleich
    beim ersten Ausliefern."""
    if idx is None:
        if not dokumente_faellig():
            return
        idx = anhang_index()
    if not ds_bekannt:
        ds, _grund = ds_bereit()
    geaendert = False
    if ds is not None:
        try:
            geaendert = bool(ds_stand_nachtragen(ds, idx))
        except Exception as e:
            log("DocuSort-Stand nicht abfragbar: %s" % str(e)[:120])
    try:
        geaendert = anhaenge_frisch(pf, idx) or geaendert
    except Exception as e:
        log("Nachtrag uebersprungen: %s" % str(e)[:140])
    if geaendert:
        anhang_index_sichern(idx)


def anhaenge_frisch(pf, idx: dict) -> bool:
    """Nachtragen, wenn es noetig ist: solange noch ein Ordner offen ist, in
    jedem Lauf ein Stueck — danach nur noch zweimal am Tag."""
    n = idx.get("nachtrag") if isinstance(idx.get("nachtrag"), dict) else {}
    if int(n.get("offen") or 0) <= 0 and n.get("zeit"):
        try:
            alt = (datetime.now() - datetime.fromisoformat(n["zeit"])).total_seconds()
            if alt < NACHTRAG_FRISCH:
                return False
        except (ValueError, TypeError):
            pass
    erg = anhaenge_nachtragen(pf, idx)
    if erg.get("neu"):
        log("Nachtrag: %d Mails angesehen, %d neu im Dokumenten-Index, "
            "%d Ordner offen." % (erg["mails"], erg["neu"], erg["offen"]))
    return True


# ── Aus dessen eigener Ablage lernen ────────────────────────────────────────
def haupt_domain(adresse: str) -> str:
    """netflix.com aus members.netflix.com.

    🔴 Genau daran ist die erste Messung gescheitert: im Ordner
    `Shopping.Netflix` liegt `info@account.netflix.com`, im Posteingang kam
    `info@members.netflix.com`. Gleiche Firma, andere Unterdomain — ohne diese
    Stufe faellt so etwas durch.

    Bewusst einfach gehalten: die letzten zwei Teile, bei den bekannten
    zweistufigen Endungen (co.uk, com.au …) die letzten drei. Eine vollstaendige
    Liste oeffentlicher Endungen waere hier mehr Pflege als Nutzen.
    """
    dom = (adresse.split("@")[-1] if "@" in adresse else adresse).lower().strip(".")
    teile = dom.split(".")
    if len(teile) < 2:
        return dom
    zweistufig = {"co.uk", "org.uk", "ac.uk", "com.au", "co.nz", "co.jp",
                  "com.br", "co.za", "com.tr"}
    if len(teile) >= 3 and ".".join(teile[-2:]) in zweistufig:
        return ".".join(teile[-3:])
    return ".".join(teile[-2:])


def lauf_buchen(felder: dict) -> dict:
    """Jeden Lauf festhalten — und den Takt aus der BEOBACHTUNG ableiten.

    🔑 der Besitzer am 12.09.2026: „kann nirgends sehen wann und wie oft die läuft."
    Ein Cron-Eintrag ist eine ABSICHT. Was hier entsteht, ist die Wirklichkeit:
    die letzten Laufzeitpunkte, daraus der Median-Abstand. Genau der Unterschied,
    an dem an diesem Tag schon die Zeitauftrags-Überwachung des Homelab-Tabs
    hing — ein eingetragener Takt beweist keine Ausführung.

    🔴 UND DER EIGENTLICHE GRUND, warum das ein Trichter sein muss: bis heute
    schrieb der Stillgelegt-Zweig `lauf.json` KOMPLETT NEU und verlor dabei die
    `uid`. Beim nächsten Start wäre `letzte_uid = 0` gewesen — also `kaltstart`,
    also „lernen und schweigen". Jede Mail, die während des Stillstands ankam,
    wäre stumm übersprungen worden: kein Alarm, kein Sortieren, kein Hinweis.
    Hier wird deshalb IMMER auf den vorherigen Stand aufgesetzt; ein Feld kann
    nur verschwinden, wenn es jemand ausdrücklich überschreibt.
    """
    alt = load(LAUF, {})
    if not isinstance(alt, dict):
        alt = {}
    jetzt = datetime.now()
    hist = [z for z in (alt.get("historie") or []) if isinstance(z, str)]
    hist.append(jetzt.isoformat(timespec="seconds"))
    hist = hist[-LAUF_HISTORIE:]

    # Median, nicht Mittelwert: ein einzelner Ausfall oder ein Handstart soll
    # den Takt nicht verbiegen.
    abstaende = []
    for a, b in zip(hist, hist[1:]):
        try:
            d = (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
        except ValueError:
            continue
        if 0 < d < 86400:
            abstaende.append(d)
    # 🔴 Aus zwei Laeufen folgt kein Takt. Direkt nach dem Einbau standen dort
    # „21 Sekunden", weil beide Messpunkte Handstarts waren — die Seite haette
    # „alle 21 Sek" behauptet. Dieselbe Lehre wie beim Cron-Waechter am selben
    # Tag: wer kurz hinsieht, darf ueber den Rhythmus nichts sagen.
    takt = (int(sorted(abstaende)[len(abstaende) // 2])
            if len(abstaende) >= TAKT_MINDEST else 0)

    tag = jetzt.strftime("%Y-%m-%d")
    heute = (int(alt.get("heute") or 0) + 1) if alt.get("tag") == tag else 1

    neu = dict(alt)                      # 🔴 aufsetzen, nicht ersetzen
    neu.update(felder)
    neu.update({"zeit": jetzt.isoformat(timespec="seconds"),
                "historie": hist, "takt_s": takt, "tag": tag, "heute": heute})
    save(LAUF, neu)
    return neu


def ablage_lernen(pf) -> dict:
    """Die Landkarte bauen: wer schreibt in welchen Ordner.

    Drei Stufen, von genau nach grob — beim Zuordnen gewinnt immer die
    genaueste, die zutrifft:
      1. die volle Absenderadresse
      2. die volle Domain
      3. die Hauptdomain

    Nebenbei werden die SCHWAECHEN der Ablage mitgeschrieben: Absender, die
    der Besitzer mal hierhin, mal dorthin gelegt hat, und Ordner, die fast leer sind.
    Das ist das Rohmaterial fuer den spaeteren Umbau der Struktur — er sagte am
    11.09.2026 selbst: „meine ordner sind auch nicht perfekt, aber das soll ja
    der agent spaeter fuer mich neu sortieren."
    """
    t0 = time.time()
    eigene = {(pf.zug.get("adresse") or "").lower(),
              "der Dienstbenutzer@gmail.com", "der Dienstbenutzer@googlemail.com"}
    eigene.discard("")
    ordner = [o for o in pf.ordner_liste() if o not in KEIN_LEHRMEISTER]
    je_absender, groessen = {}, {}
    for name in ordner:
        c = pf.absender_im_ordner(name, LERN_JE_ORDNER)
        groessen[name] = sum(c.values())
        for adr, n in c.items():
            # 🔴 dessen EIGENE Adressen taugen nicht als Regel: er hat sich
            # ueber Jahre Mails selbst weitergeleitet, quer durch alle Themen.
            # Im Gmail-Archiv kamen 235 von 400 Stichproben von ihm selbst.
            if adr in eigene:
                continue
            je_absender.setdefault(adr, {})
            je_absender[adr][name] = je_absender[adr].get(name, 0) + n

    def eindeutig(topf: dict, stufe: str) -> dict:
        anteil, mindest, _ = LERN_SCHWELLEN[stufe]
        raus = {}
        for schl, wo in topf.items():
            gesamt = sum(wo.values())
            ziel, treffer = max(wo.items(), key=lambda kv: kv[1])
            if gesamt >= mindest and treffer / gesamt >= anteil:
                raus[schl] = {"ordner": ziel, "treffer": treffer, "gesamt": gesamt}
        return raus

    je_domain, je_haupt = {}, {}
    for adr, wo in je_absender.items():
        for stufe, schl in ((je_domain, adr.split("@")[-1]),
                            (je_haupt, haupt_domain(adr))):
            stufe.setdefault(schl, {})
            for o, n in wo.items():
                stufe[schl][o] = stufe[schl].get(o, 0) + n

    def locker(topf: dict) -> dict:
        raus = {}
        for schl, wo in topf.items():
            gesamt = sum(wo.values())
            ziel, treffer = max(wo.items(), key=lambda kv: kv[1])
            if gesamt >= VORSCHLAG_MINDEST and treffer / gesamt >= VORSCHLAG_ANTEIL:
                raus[schl] = {"ordner": ziel, "treffer": treffer, "gesamt": gesamt}
        return raus

    karte = {
        "absender": eindeutig(je_absender, "absender"),
        "domain": eindeutig(je_domain, "domain"),
        "haupt": eindeutig(je_haupt, "haupt"),
        "v_absender": locker(je_absender),
        "v_domain": locker(je_domain),
        "v_haupt": locker(je_haupt),
        "ordner": groessen,
        # 🔑 Die Namensbruecke wird hier MITGEBAUT und nicht erst beim Zuordnen
        # berechnet — sie haengt nur an den Ordnernamen, nicht am Inhalt, und
        # gilt deshalb auch fuer ORDNER, IN DENEN NOCH NICHTS LIEGT. Genau die
        # waren der blinde Fleck: Synology.NAS01 bis NAS04 sind angelegt, aber
        # leer, und konnten dem Zaehlen nie etwas beibringen.
        "namen": namens_marken(groessen),
        "gelernt": datetime.now().isoformat(timespec="seconds"),
        "dauer": round(time.time() - t0, 1),
        "mails": sum(groessen.values()),
    }
    # Die Schwaechen — fuer den spaeteren Umbau, nicht fuer das Sortieren.
    uneindeutig = []
    for adr, wo in je_absender.items():
        gesamt = sum(wo.values())
        ziel, treffer = max(wo.items(), key=lambda kv: kv[1])
        if gesamt >= 3 and treffer / gesamt < LERN_ANTEIL:
            uneindeutig.append({"absender": adr, "gesamt": gesamt,
                                "verteilt_auf": sorted(wo.items(),
                                                       key=lambda kv: -kv[1])[:4]})
    karte["schwaechen"] = {
        "uneindeutige_absender": sorted(uneindeutig,
                                        key=lambda e: -e["gesamt"])[:40],
        "fast_leere_ordner": sorted([o for o, n in groessen.items() if 0 < n <= 5]),
        "leere_ordner": sorted([o for o, n in groessen.items() if n == 0]),
    }
    abgedeckt = sum(v["gesamt"] for v in karte["absender"].values())
    karte["deckung"] = round(100.0 * abgedeckt / max(karte["mails"], 1), 1)
    log("Ablage gelernt: %d Mails aus %d Ordnern, %d eindeutige Absender, "
        "%.0f%% Deckung (%.1fs)"
        % (karte["mails"], len(ordner), len(karte["absender"]),
           karte["deckung"], karte["dauer"]))
    return karte


def _tagesreihe(tage: dict, ab: str) -> list:
    """Jeden Kalendertag von `ab` bis heute, auch die ohne Post."""
    try:
        d = datetime.strptime(ab, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return [{"tag": t, "n": n} for t, n in sorted(tage.items())]
    ende = datetime.now().date()
    raus = []
    while d <= ende:
        t = d.strftime("%Y-%m-%d")
        raus.append({"tag": t, "n": int(tage.get(t) or 0)})
        d += timedelta(days=1)
    return raus


def statistik_lernen(pf) -> dict:
    """Die Zahlen hinter dem Postfach — aus den ECHTEN Kopfzeilen.

    Der Besitzer, 12.09.2026: „bau mal in die postwache eine kleine statistik ein,
    wieviel mails pro tag kommen, zu welcher zeit wer am haeufigsten schreibt".

    🔑 Gelesen wird `Date:` und `From:` jeder Mail in allen Eingangsordnern —
    nicht das, was die Postwache seit ihrer Einrichtung gesehen hat. Sonst
    haette der Besitzer erst in Wochen etwas zu sehen, und die Antwort auf „wie viel
    kommt pro Tag" waere eine Hochrechnung aus zwei Tagen.

    🔴 Der Deckel je Ordner macht den linken Rand des Tagesverlaufs unvollstaendig:
    wo abgeschnitten wurde, fehlen die AELTESTEN Mails. Deshalb wird
    `vollstaendig_ab` mitgeliefert — der spaeteste Anfang aller gedeckelten
    Ordner. Davor darf kein Tagesbalken gezeigt werden, sonst faellt die Kurve
    am Rand ab, ohne dass dort weniger Post kam.
    """
    t0 = time.time()
    eigene = {(pf.zug.get("adresse") or "").lower(),
              "der Dienstbenutzer@gmail.com", "der Dienstbenutzer@googlemail.com"}
    eigene.discard("")
    ordner = [o for o in pf.ordner_liste() if o not in KEINE_STATISTIK]

    je_tag, je_ordner = {}, {}
    je_stunde = [0] * 24
    je_wochentag = [0] * 7
    absender = {}
    gedeckelt_ab = None
    eigene_n = 0
    frueheste, spaeteste = None, None

    for name in ordner:
        koepfe, gedeckelt = pf.koepfe_im_ordner(name, STAT_JE_ORDNER)
        if not koepfe:
            continue
        aeltest = min(t for _, t, _ in koepfe)
        if gedeckelt and (gedeckelt_ab is None or aeltest > gedeckelt_ab):
            gedeckelt_ab = aeltest
        for adr, ts, anzeige in koepfe:
            # 🔴 dessen eigene Adressen zaehlen nicht als eingegangene Post —
            # im Gmail-Archiv kam jede zweite Stichprobe von ihm selbst.
            if adr in eigene:
                eigene_n += 1
                continue
            tag = ts.strftime("%Y-%m-%d")
            je_tag[tag] = je_tag.get(tag, 0) + 1
            je_stunde[ts.hour] += 1
            je_wochentag[ts.weekday()] += 1
            je_ordner[name] = je_ordner.get(name, 0) + 1
            e = absender.setdefault(adr, {"n": 0, "name": "", "zuletzt": ""})
            e["n"] += 1
            nm = absender_teile(anzeige)[0]
            # 🔴 `absender_teile` gibt bei fehlendem Anzeigenamen die ADRESSE
            # zurueck. Die dann als „Name" zu fuehren sieht auf der Seite aus
            # wie ein Fehler (`service@paypal.de  service@paypal.de`).
            if nm and nm.lower() != adr and not e["name"]:
                e["name"] = nm[:60]
            iso = ts.isoformat(timespec="seconds")
            if iso > e["zuletzt"]:
                e["zuletzt"] = iso
            if frueheste is None or ts < frueheste:
                frueheste = ts
            if spaeteste is None or ts > spaeteste:
                spaeteste = ts

    gesamt = sum(je_tag.values())
    voll_ab = gedeckelt_ab.strftime("%Y-%m-%d") if gedeckelt_ab else (
        frueheste.strftime("%Y-%m-%d") if frueheste else "")

    # Nur der vollstaendige Teil geht in den Tagesverlauf und in den Schnitt.
    grenze = (datetime.now() - timedelta(days=STAT_TAGE)).strftime("%Y-%m-%d")
    ab = max(voll_ab, grenze) if voll_ab else grenze
    tage_voll = {t: n for t, n in je_tag.items() if t >= ab}
    # Der HEUTIGE Tag ist noch nicht zu Ende — er wuerde jeden Schnitt druecken.
    heute = datetime.now().strftime("%Y-%m-%d")
    fuer_schnitt = {t: n for t, n in tage_voll.items() if t != heute}
    spanne = len(fuer_schnitt) or 1
    schnitt = round(sum(fuer_schnitt.values()) / spanne, 1)

    top = sorted(((a, e) for a, e in absender.items()), key=lambda kv: -kv[1]["n"])[:15]
    k = {
        "gebaut": datetime.now().isoformat(timespec="seconds"),
        "dauer": round(time.time() - t0, 1),
        "mails": gesamt,
        "eigene_ausgelassen": eigene_n,
        "ordner": len(je_ordner),
        "von": frueheste.strftime("%Y-%m-%d") if frueheste else "",
        "bis": spaeteste.strftime("%Y-%m-%d") if spaeteste else "",
        "vollstaendig_ab": ab,
        "schnitt_pro_tag": schnitt,
        "tage_gemessen": len(fuer_schnitt),
        # 🔴 Luecken auffuellen. Ein Balkendiagramm, das Null-Tage einfach
        # weglaesst, staucht die Zeitachse und laesst eine ruhige Woche wie eine
        # dichte aussehen. Gemessen: der 06.09. fehlte komplett.
        "je_tag": _tagesreihe(tage_voll, ab),
        "je_stunde": je_stunde,
        "je_wochentag": je_wochentag,
        "top_absender": [{"adresse": a, "name": e["name"], "n": e["n"],
                          "zuletzt": e["zuletzt"]} for a, e in top],
        "top_ordner": sorted(({"ordner": o, "n": n} for o, n in je_ordner.items()),
                             key=lambda e: -e["n"])[:12],
        "absender_gesamt": len(absender),
    }
    log("Statistik gebaut: %d Mails aus %d Ordnern, %s bis %s, Schnitt %.1f/Tag (%.1fs)"
        % (gesamt, len(je_ordner), k["von"], k["bis"], schnitt, k["dauer"]))
    return k


def statistik_frisch(pf) -> dict:
    """Hoechstens sechs Stunden alt — Zahlen dieser Art aendern sich langsam."""
    k = load(STATISTIK, None)
    if isinstance(k, dict) and k.get("gebaut"):
        try:
            alt = (datetime.now() - datetime.fromisoformat(k["gebaut"])).total_seconds()
            if alt < STAT_FRISCH:
                return k
        except (ValueError, TypeError):
            pass
    k = statistik_lernen(pf)
    save(STATISTIK, k)
    return k


def ablage_frisch(pf) -> dict:
    """Die Landkarte, hoechstens einen Tag alt. Lernen dauert Sekunden bis
    Minuten — das gehoert nicht in einen Lauf, der jede Minute kommt."""
    k = load(ABLAGE, None)
    if isinstance(k, dict) and k.get("gelernt"):
        try:
            alter = (datetime.now()
                     - datetime.fromisoformat(k["gelernt"])).total_seconds()
            if alter < ABLAGE_FRISCH:
                return k
        except (ValueError, TypeError):
            pass
    k = ablage_lernen(pf)
    save(ABLAGE, k)
    chronik("gelernt", titel=txt("w.gelernt.titel"),
            detail="%d Mails aus %d Ordnern angesehen, %d Absender eindeutig "
                   "zuzuordnen (%.0f%% Deckung)."
                   % (k["mails"], len(k["ordner"]), len(k["absender"]),
                      k["deckung"]))
    return k


def marke(text: str) -> str:
    """Ein Ordnername oder ein Adressteil, auf seinen Kern eingedampft.

    Laeuft ueber `normal()`, damit „Bücher" und „Buecher" dasselbe ergeben, und
    wirft dann alles weg, was kein Buchstabe und keine Ziffer ist.
    """
    return re.sub(r"[^a-z0-9]", "", normal(text))


def namens_marken(ordner: dict) -> dict:
    """Ordnername -> Marke. Nur die LETZTE Stufe zaehlt: `Shopping.Ikea` ist
    der Ikea-Ordner, nicht der Shopping-Ordner.

    Doppelte Marken fliegen raus. Haetten zwei Ordner dieselbe Marke, waere
    jeder Treffer ein Muenzwurf — dann schweigt die Bruecke lieber.
    """
    gezaehlt = {}
    for o in ordner:
        mk = marke(o.rsplit(".", 1)[-1])
        if len(mk) >= NAMENSBRUECKE_MINDEST:
            gezaehlt.setdefault(mk, []).append(o)
    return {mk: liste[0] for mk, liste in gezaehlt.items() if len(liste) == 1}


def adress_marken(adresse: str) -> set:
    """Die Bestandteile einer Adresse, gegen die ein Ordnername stehen darf:
    der GANZE lokale Teil und JEDE Domainstufe.

    Ganze Teile, keine Teilzeichenketten — sonst faende „Haus" den Absender
    `haushalt@...` und „KIA" das Wort `kiabi`. Ein Beweis, der auf einem
    zufaelligen Zeichenschnipsel beruht, ist keiner.
    """
    lokal, _, dom = (adresse or "").lower().partition("@")
    teile = [marke(lokal)] + [marke(x) for x in dom.split(".")]
    return {t for t in teile if t}


def namens_ziel(karte: dict, adresse: str):
    """Traegt der Ordnername selbst die Antwort? Gibt (ordner, grund) zurueck.

    Passen ZWEI Ordner, schweigt die Bruecke. Sie raet nicht.
    """
    marken = karte.get("namen") or {}
    if not marken:
        return None, ""
    e = adress_marken(adresse)
    treffer = sorted({o for mk, o in marken.items() if mk in e})
    if len(treffer) != 1:
        return None, ""
    return treffer[0], ("Dein Ordner %s ist nach diesem Absender benannt"
                        % treffer[0])


def ziel_finden(karte: dict, adresse: str):
    """Wohin gehoert diese Mail — nach dessen eigener Gewohnheit?

    Gibt (ordner, grund, sicherheit, darf_handeln) zurueck. Die genaueste Stufe
    gewinnt. Die groebste (Hauptdomain) liefert einen VORSCHLAG, aber
    `darf_handeln=False` — gemessen kostet sie mehr Fehler als sie Treffer
    bringt (siehe LERN_SCHWELLEN).

    Die Reihenfolge ist gemessen, nicht geraten:
      1. Absender      — wo der Besitzer diese Adresse wirklich ablegt
      2. Domain        — dasselbe eine Stufe groeber
      3. Namensbruecke — wo der ORDNERNAME den Absender nennt
      4. Hauptdomain und die Vorschlagsstufen — nur vorschlagen, nicht handeln
    """
    adresse = (adresse or "").lower()
    if not adresse or "@" not in adresse:
        return None, "keine Absenderadresse", 0, False
    # Gezaehlt wird zuerst: wo der Besitzer wirklich abgelegt hat, schlaegt jeden
    # Namensvergleich. Erst wenn das Zaehlen schweigt, kommt die Bruecke.
    for stufe, wert, wie in (
            ("absender", adresse, "Post von %s legst du immer nach"),
            ("domain", adresse.split("@")[-1], "Post von %s legst du immer nach")):
        e = (karte.get(stufe) or {}).get(wert)
        if not e:
            continue
        sicher = round(100.0 * e["treffer"] / max(e["gesamt"], 1))
        grund = (wie % wert) + " %s (%d von %d)" % (e["ordner"], e["treffer"],
                                                    e["gesamt"])
        return e["ordner"], grund, sicher, LERN_SCHWELLEN[stufe][2]

    # 🔑 Die Namensbruecke. Sie steht GENAU hier — gemessen am 12.09.2026:
    # vor das Zaehlen gestellt kostet sie Genauigkeit (97.1 statt 97.6 %), als
    # Rueckfall dahinter bringt sie 30 zusaetzliche Mails, 24 davon richtig.
    n_ziel, n_grund = namens_ziel(karte, adresse)
    if n_ziel:
        return n_ziel, n_grund, 90, True

    for stufe, wert, wie in (
            ("haupt", haupt_domain(adresse), "Post von %s legst du meist nach"),
            ("v_absender", adresse, "Post von %s landet meistens in"),
            ("v_domain", adresse.split("@")[-1], "Post von %s landet meistens in"),
            ("v_haupt", haupt_domain(adresse), "Post von %s landet meistens in")):
        e = (karte.get(stufe) or {}).get(wert)
        if not e:
            continue
        sicher = round(100.0 * e["treffer"] / max(e["gesamt"], 1))
        darf = LERN_SCHWELLEN.get(stufe, (0, 0, False))[2]
        grund = (wie % wert) + " %s (%d von %d)" % (e["ordner"], e["treffer"],
                                                    e["gesamt"])
        if not darf:
            grund += " — zu unsicher zum Selbstentscheiden, sag einmal Ja"
        return e["ordner"], grund, sicher, darf
    return None, "Absender kommt in keinem deiner Ordner vor", 0, False


# ── Ein Lauf ──────────────────────────────────────────────────────────────────
def kopf_lesen(msg, text: str) -> dict:
    """Aus einer Mail genau das behalten, was fuer Einordnung und Anzeige noetig
    ist — und nichts weiter. Der Nachrichtentext wird NICHT gespeichert."""
    von = msg.get("From", "")
    name, adresse = absender_teile(von)
    listen = [k for k in ("List-Id", "List-Unsubscribe", "List-Post")
              if msg.get(k)]
    prec = (msg.get("Precedence") or "").lower()
    auto = (msg.get("Auto-Submitted") or "").lower()
    bulk_grund = ""
    if listen:
        bulk_grund = listen[0]
    elif prec in ("bulk", "list", "junk"):
        bulk_grund = "Precedence: " + prec
    elif auto and auto != "no":
        bulk_grund = "Auto-Submitted: " + auto
    datum = msg.get("Date", "")
    try:
        ts = email.utils.parsedate_to_datetime(datum)
        iso = ts.astimezone().isoformat(timespec="seconds")
    except Exception:
        iso = ""
    return {
        "betreff": dekodieren(msg.get("Subject", ""))[:300],
        "name": name[:120],
        "adresse": adresse[:160],
        "datum": iso,
        "bulk": bool(bulk_grund),
        "bulk_grund": bulk_grund[:80],
        "message_id": (msg.get("Message-Id") or "")[:200],
    }


def zaehlen(topf: dict, schluessel: str) -> None:
    topf[schluessel] = int(topf.get(schluessel) or 0) + 1


def absender_pflegen(prof: dict, kopf: dict, klasse: str) -> None:
    """Langzeitgedaechtnis je Absender: wie oft, welche Schublade, wann zuletzt.
    Daraus wird die Zusammenfassung gespeist — und spaeter die Frage, ob ein
    Absender immer dasselbe schickt."""
    a = kopf.get("adresse") or "?"
    e = prof.get(a) if isinstance(prof.get(a), dict) else {}
    e["n"] = int(e.get("n") or 0) + 1
    e["zuletzt"] = kopf.get("datum") or datetime.now().isoformat(timespec="seconds")
    e["name"] = kopf.get("name") or e.get("name") or ""
    klassen = e.get("klassen") if isinstance(e.get("klassen"), dict) else {}
    zaehlen(klassen, klasse)
    e["klassen"] = klassen
    prof[a] = e


def absender_regel(einst: dict, adresse: str) -> str:
    """dessen eigene Zuordnung schlaegt alles. Sie entsteht auf der Seite mit
    einem Klick neben der Mail und nennt seit dem Umbau einen ORDNER, keine
    Schublade — die Schubladen entscheiden nur noch ueber das Melden."""
    return str((einst.get("absender_regeln") or {}).get((adresse or "").lower()) or "")


# ═══ Urteilshilfe: ein Sprachmodell, wenn eines eingerichtet ist ═════════════
# Der Waechter selbst bleibt stumm — er ordnet nach festen Regeln ein und kostet
# nichts. Gefragt wird ein Modell nur bei dem, was er selbst NICHT entscheiden
# kann: Mails, die in keine Schublade passen. Es schlaegt eine Regel vor,
# scharfschalten tut sie ein Mensch.
#
# 🔴 WAS DAS HAUS VERLAESST, WENN EIN DIENST EINGESTELLT IST: Absender, Name,
# Betreff und die Begruendung der Einordnung. Niemals der Nachrichtentext,
# niemals ein Anhang. Genau dieselbe Auswahl, die schon an den Werkstatt-Agenten
# ging. Wem das zu viel ist, nimmt ein lokales Modell (Ollama) — dann verlaesst
# gar nichts den Rechner — oder laesst die Urteilshilfe aus, was die Voreinstellung
# ist.
KI_ANBIETER = ("aus", "ollama", "openai", "anthropic", "werkstatt")
KI_ZEIT = 90               # Sekunden; ein lokales Modell auf schwacher Hardware
                           # braucht laenger als ein Dienst
KI_MAX_FAELLE = 25         # mehr Beispiele machen den Vorschlag nicht besser,
                           # nur die Anfrage teurer
VORSCHLAEGE = "vorschlaege.json"   # in out/: was zuletzt vorgeschlagen wurde

KI_STANDARD_MODELL = {
    "ollama": "llama3.1:8b",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5-20251001",
}
KI_STANDARD_URL = {
    "ollama": "http://127.0.0.1:11434",
    "openai": "https://api.openai.com",
    "anthropic": "https://api.anthropic.com",
}


def ki_konfig() -> dict:
    """Der eingestellte Anbieter. Unbekannte Namen gelten als `aus` — eine
    vertippte Einstellung darf nicht dazu fuehren, dass irgendwohin gefragt wird."""
    k = load(KI, None)
    k = k if isinstance(k, dict) else {}
    anbieter = str(k.get("anbieter") or "aus").strip().lower()
    if anbieter not in KI_ANBIETER:
        anbieter = "aus"
    return {
        "anbieter": anbieter,
        "url": str(k.get("url") or KI_STANDARD_URL.get(anbieter, "")).rstrip("/"),
        "modell": str(k.get("modell") or KI_STANDARD_MODELL.get(anbieter, "")).strip(),
        "schluessel": str(k.get("schluessel") or ""),
    }


def ki_bereit() -> tuple:
    """(ja/nein, Grund). Der Grund ist fuer die Seite, nicht fuer das Protokoll —
    er soll sagen, was FEHLT, nicht dass etwas kaputt ist."""
    k = ki_konfig()
    if k["anbieter"] == "aus":
        return False, "Keine Urteilshilfe eingerichtet"
    if k["anbieter"] == "werkstatt":
        pfad = konfig()["werkstatt"]
        if not (pfad and os.path.isdir(pfad)):
            return False, "Werkstatt-Ordner nicht erreichbar"
        return True, ""
    if not k["url"]:
        return False, "Keine Adresse eingetragen"
    if k["anbieter"] in ("openai", "anthropic") and not k["schluessel"]:
        return False, "Kein Schluessel hinterlegt"
    if not k["modell"]:
        return False, "Kein Modell eingetragen"
    return True, ""


def _ki_http(url: str, kopf: dict, rumpf: dict) -> dict:
    roh = json.dumps(rumpf).encode()
    req = urllib.request.Request(url, data=roh,
                                 headers=dict(kopf, **{"Content-Type": "application/json"}))
    with urllib.request.urlopen(req, timeout=KI_ZEIT) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "{}")


def ki_fragen(system: str, frage: str) -> str:
    """Eine Frage an das eingestellte Modell, eine Antwort als Text.

    Zwei Protokolle genuegen fuer alle vier Faelle: Ollama und OpenAI sprechen
    dasselbe (`/v1/chat/completions`), Anthropic spricht `/v1/messages`. Die
    Werkstatt geht einen anderen Weg und kommt hier nicht vorbei.
    """
    k = ki_konfig()
    if k["anbieter"] == "anthropic":
        antw = _ki_http(
            k["url"] + "/v1/messages",
            {"x-api-key": k["schluessel"], "anthropic-version": "2023-06-01"},
            {"model": k["modell"], "max_tokens": 1500, "system": system,
             "messages": [{"role": "user", "content": frage}]})
        teile = antw.get("content") or []
        return "".join(str(t.get("text") or "") for t in teile if isinstance(t, dict))
    kopf = {}
    if k["schluessel"]:
        kopf["Authorization"] = "Bearer " + k["schluessel"]
    antw = _ki_http(
        k["url"] + "/v1/chat/completions", kopf,
        {"model": k["modell"], "temperature": 0, "max_tokens": 1500,
         "messages": [{"role": "system", "content": system},
                      {"role": "user", "content": frage}]})
    wahl = (antw.get("choices") or [{}])[0]
    return str((wahl.get("message") or {}).get("content") or "")


def _json_aus_text(t: str):
    """Modelle legen ihr JSON gern in einen Codeblock oder schreiben einen Satz
    davor. Gesucht wird deshalb die aeusserste Klammer, nicht die ganze Antwort."""
    t = (t or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"```\s*$", "", t).strip()
    for auf, zu in (("[", "]"), ("{", "}")):
        a, b = t.find(auf), t.rfind(zu)
        if a >= 0 and b > a:
            try:
                return json.loads(t[a:b + 1])
            except ValueError:
                continue
    return None


def ki_regeln_vorschlagen(faelle: list) -> list:
    """Aus unklaren Mails Regelvorschlaege machen. Gibt eine (moeglicherweise
    leere) Liste zurueck und wirft nie — eine Urteilshilfe darf den Postlauf
    nicht kosten."""
    ok, _grund = ki_bereit()
    if not ok or ki_konfig()["anbieter"] == "werkstatt" or not faelle:
        return []
    zeilen = []
    for e in faelle[:KI_MAX_FAELLE]:
        kopf = e.get("kopf") or {}
        zeilen.append("- Absender: %s | Name: %s | Betreff: %s | unklar weil: %s"
                      % (kopf.get("adresse", ""), kopf.get("name", ""),
                         str(kopf.get("betreff", ""))[:140], e.get("grund", "")))
    schubladen = ", ".join("%s (%s)" % (k, v["name"]) for k, v in SCHUBLADEN.items())
    system = (
        "Du hilfst einem E-Mail-Sortierer. Er ordnet Post in feste Schubladen ein "
        "und kommt bei den folgenden Mails nicht weiter. Schlage REGELN vor, keine "
        "Einzelentscheidungen: eine Regel gilt fuer einen Absender oder eine "
        "Absender-Domaene. Antworte AUSSCHLIESSLICH mit einem JSON-Array. Jeder "
        "Eintrag: {\"absender\": \"adresse oder @domaene\", \"schublade\": "
        "\"schluessel\", \"warum\": \"ein kurzer Satz\", \"sicher\": 0-100}. "
        "Nur Schubladen aus der vorgegebenen Liste. Wenn du dir bei einem Fall "
        "nicht sicher bist, lass ihn weg — ein fehlender Vorschlag kostet nichts, "
        "ein falscher raeumt Post an den falschen Ort."
    )
    frage = "Schubladen: %s\n\nUnklare Mails:\n%s" % (schubladen, "\n".join(zeilen))
    try:
        roh = _json_aus_text(ki_fragen(system, frage))
    except Exception as e:
        log("Urteilshilfe nicht erreichbar: %s" % str(e)[:160])
        return []
    if not isinstance(roh, list):
        return []
    fertig = []
    for v in roh:
        if not isinstance(v, dict):
            continue
        absender = str(v.get("absender") or "").strip().lower()
        schublade = str(v.get("schublade") or "").strip()
        # 🔴 Eine Schublade, die es nicht gibt, wird verworfen statt geraten.
        # Ein Modell erfindet Kategorien, wenn man es laesst.
        if not absender or schublade not in SCHUBLADEN:
            continue
        fertig.append({
            "absender": absender,
            "schublade": schublade,
            "warum": str(v.get("warum") or "")[:200],
            "sicher": max(0, min(100, int(v.get("sicher") or 0))),
        })
    return fertig


def vorschlaege_schreiben(vorschlaege: list, quelle: str) -> None:
    """Vorschlaege landen auf der Seite, nicht in den Einstellungen.

    🔑 Der Waechter schaltet NIE selbst scharf. Ein Modell, das sich irrt, wuerde
    sonst Post an einen Ort raeumen, an dem sie niemand sucht — und der Irrtum
    faellt erst auf, wenn etwas fehlt.
    """
    try:
        os.makedirs(OUT, exist_ok=True)
        tmp = os.path.join(OUT, VORSCHLAEGE + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"zeit": datetime.now().isoformat(timespec="seconds"),
                       "quelle": quelle, "liste": vorschlaege}, fh,
                      ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(OUT, VORSCHLAEGE))
    except OSError:
        pass


def uebergeben(unklar: list) -> bool:
    """Die unklaren Faelle dem Agenten hinlegen. Nur Betreff, Absender und die
    Begruendung — der Nachrichtentext wird nirgends gespeichert und soll auch
    hier nicht auftauchen."""
    if not (konfig()["werkstatt"] and os.path.isdir(konfig()["werkstatt"])):
        return False
    daten = [{"betreff": e["kopf"].get("betreff", ""),
              "absender": e["kopf"].get("adresse", ""),
              "name": e["kopf"].get("name", ""),
              "rundschreiben": bool(e["kopf"].get("bulk")),
              "kopfgrund": e["kopf"].get("bulk_grund", ""),
              "warum_unklar": e.get("grund", ""),
              "gesehen": e.get("gesehen", "")} for e in unklar]
    try:
        pfad = os.path.join(konfig()["werkstatt"], "unklar.json")
        tmp = pfad + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"stand": datetime.now().isoformat(timespec="seconds"),
                       "schubladen": sorted(SCHUBLADEN),
                       "bleibt_im_posteingang": list(ALARM),
                       "wird_aussortiert": list(LAERM),
                       "faelle": daten}, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, pfad)
        os.chmod(pfad, 0o640)
        return True
    except OSError as e:
        log("Uebergabe an den Agenten fehlgeschlagen: %s" % str(e)[:140])
        return False


def escalate(title: str, body: str) -> int:
    """Einen Auftrag in der Werkstatt anlegen — der einzige Weg, auf dem dieses
    Programm Tokens ausgibt. Faellt der Werkstatt-Container aus, faellt hier nur
    der Weckruf aus, nicht der Waechter."""
    code = (
        "import json,sys;sys.path.insert(0,'/app');"
        "import config,local_engine;"
        "p=[x for x in config.PROJECTS if x['id']=='postwache'][0];"
        "t=json.loads(sys.stdin.read());"
        "r=local_engine.create_thread(p,t['title'],t['body'],'Postwache',[]);"
        "print(r['number'])"
    )
    try:
        r = subprocess.run(["docker", "exec", "-i", "werkstatt", "python", "-c", code],
                           input=json.dumps({"title": title, "body": body}),
                           capture_output=True, text=True, timeout=60)
        if r.returncode == 0 and r.stdout.strip().isdigit():
            nr = int(r.stdout.strip())
            log("Weckruf: Auftrag #%d — %s" % (nr, title))
            return nr
        log("Weckruf FEHLGESCHLAGEN (%s): %s" % (r.returncode, (r.stderr or "")[:200]))
    except Exception as e:
        log("Weckruf FEHLGESCHLAGEN: %s" % str(e)[:200])
    return 0


def melde_sofort(treffer: list, einst: dict) -> None:
    """Die Alarmklassen sofort nach Telegram — gebuendelt zu EINER Nachricht.
    Zehn einzelne Meldungen in einer Minute liest niemand."""
    if not treffer or not einst.get("telegram"):
        return
    namen = schubladen_namen()
    L = ["📬 *Postwache* — " + txt("w.sofort.kopf", n=len(treffer)), ""]
    if len(treffer) > MELDE_EINZELN_MAX:
        nach_klasse = {}
        for e in treffer:
            zaehlen(nach_klasse, e["klasse"])
        for k, n in sorted(nach_klasse.items(), key=lambda kv: -kv[1]):
            L.append("%s *%s* — %d" % (namen[k]["icon"], namen[k]["name"], n))
        L.append("")
        L.append(txt("w.sofort.alle") + " " + konfig()["seite"])
    else:
        for e in treffer:
            s = namen[e["klasse"]]
            zeile = "%s *%s* — %s" % (s["icon"], _tg_md(s["name"]),
                                      _tg_md(e["kopf"]["betreff"] or txt("w.ohne_betreff")))
            von = e["kopf"]["name"] or e["kopf"]["adresse"]
            zeile += "\n   " + txt("w.sofort.von", wer=_tg_md(von))
            if e.get("verschoben_nach"):
                zeile += "\n   \U0001F5C2 " + txt("w.sofort.abgelegt",
                                                  ordner=_tg_md(e["verschoben_nach"]))
            elif e.get("ziel"):
                zeile += "\n   \U0001F5C2 " + txt("w.sofort.gehoert",
                                                  ordner=_tg_md(e["ziel"]))
            if e.get("phishing"):
                zeile += "\n   \u26A0\uFE0F *%s* %s" % (txt("w.sofort.vorsicht"),
                                                        _tg_md(e["phishing"]))
            L.append(zeile)
        L.append("")
        L.append(konfig()["seite"])
    telegram("\n".join(L))


def zusammenfassung(zaehler: dict, prof: dict, einst: dict) -> str:
    """Die Tagesuebersicht. Kurz genug, dass man sie wirklich liest."""
    heute = datetime.now()
    tag = zaehler.get("heute") if isinstance(zaehler.get("heute"), dict) else {}
    gesamt = sum(int(v) for v in tag.values())
    L = ["📬 *Postwache* — %s" % txt("w.bericht.titel", datum=heute.strftime("%d.%m.%Y")), ""]
    if not gesamt:
        L.append(txt("w.bericht.leer"))
        return "\n".join(L)
    L.append("*%s*" % txt("w.bericht.neu", n=gesamt))
    L.append("")
    namen = schubladen_namen()
    for k in list(ALARM) + list(LAERM) + ["unklar"]:
        n = int(tag.get(k) or 0)
        if n:
            s = namen[k]
            L.append("%s %s — %d" % (s["icon"], _tg_md(s["name"]), n))
    wichtig = sum(int(tag.get(k) or 0) for k in ALARM)
    laerm = sum(int(tag.get(k) or 0) for k in LAERM)
    L.append("")
    if einst.get("scharf"):
        L.append(txt("w.bericht.bilanz", laerm="*%d*" % laerm, wichtig="*%d*" % wichtig))
    else:
        L.append(txt("w.bericht.lernlauf", laerm="*%d*" % laerm))
    dok = int(zaehler.get("dokumente") or 0)
    if dok:
        L.append("📎 " + txt("w.bericht.dokumente", n="*%d*" % dok))
    # Die drei lautesten Absender des Tages: das ist die Information, aus der
    # eine Regel wird.
    laut = sorted(((a, e) for a, e in prof.items() if isinstance(e, dict)),
                  key=lambda kv: -int(kv[1].get("n") or 0))[:3]
    if laut:
        L.append("")
        L.append(txt("w.bericht.laut"))
        for a, e in laut:
            L.append("• %s — %d" % (_tg_md(e.get("name") or a), int(e.get("n") or 0)))
    L.append("")
    L.append(konfig()["seite"])
    return "\n".join(L)


def main() -> int:
    t0 = time.time()
    os.makedirs(STATE, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)
    log_kappen()
    tok = token()

    grund = stopped(tok)
    if grund:
        vorher = load(LAUF, {})
        if vorher.get("grund") != grund:
            log("stillgelegt: %s" % grund)
            chronik("stillgelegt", titel=txt("w.still.titel"), detail=grund)
        # 🔴 lauf_buchen statt save: der Stillgelegt-Zweig hat frueher die
        # `uid` mit weggeschrieben — der naechste Start waere ein Kaltstart
        # gewesen und haette alles stumm uebersprungen, was waehrend des
        # Stillstands ankam.
        lauf_buchen({"grund": grund})
        status_schreiben({"aktiv": False, "grund": grund})
        melde_sammlung()
        return 0

    einst = einstellungen()
    faecher = [f for f in postfaecher() if f["an"]]
    if not faecher:
        status_schreiben({"aktiv": True, "eingerichtet": False,
                          "grund": "Kein Postfach eingerichtet"})
        # Kein Weckruf, keine Meldung: das ist kein Fehler, sondern der Zustand
        # vor der Einrichtung.
        return 0

    # 🔑 Ein Lauf JE Postfach, nacheinander. Nacheinander und nicht nebenlaeufig,
    # weil jeder Lauf dieselbe Chronik, dasselbe Journal und dieselbe
    # DocuSort-Sitzung benutzt — drei Faeden darauf waeren drei Gelegenheiten
    # fuer eine halb geschriebene Datei, und gewonnen waere eine Sekunde.
    # 🔴 Ein Postfach, das nicht antwortet, darf die anderen nicht aufhalten:
    # jeder Lauf steht fuer sich, sein Fehler bleibt seiner.
    rc, stand = 0, {}
    for zug in faecher:
        pf_waehlen(zug["id"])
        try:
            ergebnis = lauf_fuer_postfach(zug, einst, tok, t0)
        except Exception as e:
            log("[%s] Lauf abgebrochen: %s" % (zug["id"], str(e)[:220]))
            ergebnis = {"rc": 1, "fehler": str(e)[:200]}
        stand[zug["id"]] = dict(ergebnis, name=zug["name"], adresse=zug["adresse"])
        rc = max(rc, int(ergebnis.get("rc") or 0))
    pf_waehlen("")

    gesamt_schreiben(stand, einst, tok)
    melde_sammlung()
    return rc


def gesamt_schreiben(stand: dict, einst: dict, tok: str) -> None:
    """Die oberste Ebene von `status.json` ist die SUMME ueber alle Postfaecher.

    Die Seite soll auf einen Blick sagen koennen „3 neu, 1 verschoben", ohne
    selbst zu addieren — und der Hausschalter-Sensor bekommt dieselbe Summe.
    Was je Postfach gilt, steht darunter in `postfaecher`.
    """
    summe = {"aktiv": True, "eingerichtet": True,
             "scharf": bool(einst.get("scharf")),
             "postfaecher_gesamt": len(stand)}
    for feld in ("neu", "verschoben", "gemeldet", "unklar", "dokumente"):
        summe[feld] = sum(int((e.get(feld) or 0)) for e in stand.values())
    fehler = [("%s: %s" % (e.get("name") or k, e["fehler"]))
              for k, e in sorted(stand.items()) if e.get("fehler")]
    if fehler:
        summe["fehler"] = " · ".join(fehler)[:400]
    if any(e.get("kaltstart") for e in stand.values()):
        summe["kaltstart"] = True
    vorher = _status_lesen()
    summe["postfaecher"] = vorher.get("postfaecher") or {}
    status_schreiben(summe)
    zaehler_gesamt = {"heute": {}}
    for pid in stand:
        pf_waehlen(pid)
        z = load("zaehler.json", {}) or {}
        for k, v in (z.get("heute") or {}).items():
            zaehler_gesamt["heute"][k] = zaehler_gesamt["heute"].get(k, 0) + int(v)
    pf_waehlen("")
    push_status(tok, "%d neu" % summe["neu"],
                statusfelder(einst, zaehler_gesamt, 0))


def lauf_fuer_postfach(zug: dict, einst: dict, tok: str, t0: float) -> dict:
    """Ein vollstaendiger Postlauf fuer GENAU EIN Postfach.

    Alles, was dieser Lauf an Zustand liest und schreibt, liegt dank
    `pf_waehlen()` unter `state/pf/<id>/` — der Code darunter merkt davon
    nichts. Was global bleibt: Chronik, Journal, Protokoll, Einstellungen und
    die Zugaenge zu Telegram und DocuSort.

    Gibt zurueck, was der Gesamtlauf fuer die Summe braucht; er meldet und
    schreibt den Sensor NICHT selbst.
    """
    lauf = load(LAUF, {})
    letzte_uid = int(lauf.get("uid") or 0)
    kaltstart = letzte_uid <= 0
    zaehler = load("zaehler.json", {})
    heute = datetime.now().strftime("%Y-%m-%d")
    if zaehler.get("tag") != heute:
        zaehler = {"tag": heute, "weckrufe": 0, "heute": {}}
    prof = load(ABSENDER, {})
    koepfe = load(KOEPFE, [])
    if not isinstance(koepfe, list):
        koepfe = []

    # Im Lernlauf wird die Mailbox READONLY geoeffnet — dann kann selbst ein
    # Programmierfehler nichts veraendern. Das ist der Unterschied zwischen
    # „tut nichts" und „kann nichts tun".
    schreiben = bool(einst.get("scharf"))
    treffer, verschoben, unklar = [], 0, 0
    try:
        with Postfach(zug, schreiben) as pf:
            if kaltstart:
                uids = pf.letzte_uids(KALTSTART_MAILS)
            else:
                uids = pf.neue_uids(letzte_uid)[:MAX_PRO_LAUF]
            if not uids:
                # 🔴 Auch OHNE neue Post: sonst wird an einem ruhigen Tag nichts
                # nachgetragen (siehe `dokumente_pflegen`).
                dokumente_pflegen(pf)
                lauf_buchen({"uid": letzte_uid, "grund": "", "fehler": "",
                             "dauer": round(time.time() - t0, 1)})
                status_schreiben({"aktiv": True, "eingerichtet": True,
                                  "scharf": schreiben, "neu": 0,
                                  "uid": letzte_uid})
                tagesbericht(zaehler, prof, einst)
                return {"rc": 0, "neu": 0}

            # 🔑 ZWEI GETRENNTE FRAGEN, und das ist der ganze Umbau vom
            # 11.09.2026:
            #   1. Muss der Besitzer es SOFORT wissen?  -> `einordnen()`, am Inhalt
            #   2. Wohin gehoert es?              -> seine eigene Ablage
            # Vorher entschied die Einordnung beides. Das war falsch: eine
            # Rechnung von PayPal gehoert nach Shopping.Paypal (so macht er es
            # seit Jahren) UND soll trotzdem sofort melden. Getrennt geht beides.
            karte = ablage_frisch(pf)
            # 🔴 Die Statistik ist Beiwerk. Faellt sie aus, darf das den
            # Postlauf NICHT kosten — deshalb ein eigener Riegel. Bei der
            # Ablage waere das falsch: ohne sie kann gar nicht sortiert werden.
            try:
                statistik_frisch(pf)
            except Exception as e:
                log("Statistik uebersprungen: %s" % str(e)[:120])

            # ── Dokumente in der Post ────────────────────────────────────────
            # Der Bauplan ALLER neuen Mails in einem Zug — ein Abruf statt 120,
            # und kein einziges Byte Anhang.
            idx = anhang_index()
            strukturen = pf.strukturen(uids)
            ds, ds_grund = ds_bereit()
            # 🔴 Im Kaltstart wird NICHTS uebergeben. Er sieht sich 300 alte
            # Mails an, um die Lage zu lernen — er wuerde DocuSort mit Jahren
            # alter Post fluten, und zwar einmalig und unwiderruflich.
            if kaltstart:
                ds = None
            ds_offen = DS_JE_LAUF
            dokumente, beruehrte_ordner = 0, set()

            for uid in uids:
                try:
                    msg, text = pf.kopf_und_text(uid)
                except Exception as e:
                    log("UID %s nicht lesbar: %s" % (uid, str(e)[:120]))
                    continue
                if msg is None:
                    continue
                kopf = kopf_lesen(msg, text)
                urteil = einordnen(kopf, text)
                klasse = urteil["klasse"]
                zaehlen(zaehler.setdefault("heute", {}), klasse)
                absender_pflegen(prof, kopf, klasse)
                if klasse == "unklar":
                    unklar += 1

                # Wohin? dessen eigene Zuordnung schlaegt die gelernte.
                eigen = absender_regel(einst, kopf["adresse"])
                if eigen:
                    ziel, warum, sicher, darf_stufe = eigen, "Deine eigene Regel", 100, True
                else:
                    ziel, warum, sicher, darf_stufe = ziel_finden(karte, kopf["adresse"])

                eintrag = {"uid": uid, "klasse": klasse, "grund": urteil["grund"],
                           "phishing": urteil.get("phishing") or "",
                           "kopf": kopf, "ziel": ziel or "", "ziel_grund": warum,
                           "ziel_sicher": sicher, "ziel_darf": bool(darf_stufe),
                           "gesehen": datetime.now().isoformat(timespec="seconds"),
                           "verschoben_nach": ""}

                # ── Was haengt dran? ─────────────────────────────────────────
                # 🔴 An DIESER Stelle, nicht weiter unten: nach dem Verschieben
                # gibt es die UID im Posteingang nicht mehr, und die neue kennt
                # niemand. Der Index entsteht IMMER, die Uebergabe nur, wenn
                # DocuSort eingerichtet und eingeschaltet ist.
                ae = None
                anh = anhaenge_der_mail(strukturen.get(uid))
                if anh:
                    ae = anhang_eintragen(idx, kopf, "INBOX", uid, anh)
                    eintrag["anhaenge"] = [{"n": f["n"], "art": f["art"]}
                                           for f in anh]
                    if ds is not None and ds_offen > 0:
                        n = ds_uebergeben(ds, pf, ae, "INBOX", uid,
                                          urteil.get("phishing") or "", ds_offen)
                        ds_offen -= n
                        dokumente += n

                # 🚨 DER RIEGEL, an der EINEN Stelle, durch die jede Verschiebung
                # muss. Er baut nicht mehr auf einer Kategorienliste, sondern auf
                # etwas Staerkerem: der Waechter darf eine Mail NUR dorthin
                # legen, wo der Besitzer selbst schon Post von diesem Absender
                # hingelegt hat. Er erfindet kein Ziel und legt nie einen Ordner
                # an. Wo er nichts gelernt hat, bleibt die Mail liegen.
                darf = (bool(ziel) and darf_stufe
                        and ziel in (karte.get("ordner") or {}))
                if eigen:
                    darf = bool(ziel)          # eine eigene Regel gilt immer
                # Phishing-Verdacht bleibt IMMER liegen, egal was gelernt wurde.
                if urteil.get("phishing"):
                    darf = False
                    eintrag["ziel_grund"] = "Phishing-Verdacht — bleibt liegen"
                # Wunsch: Wichtiges trotzdem im Posteingang lassen.
                if darf and einst.get("wichtiges_bleibt") and klasse in ALARM:
                    darf = False
                    eintrag["ziel_grund"] = (
                        "%s — bleibt liegen, weil du „Wichtiges bleibt im "
                        "Posteingang\u201c eingeschaltet hast" % warum)

                if kaltstart:
                    pass                       # lernen und schweigen
                elif darf and schreiben:
                    voll = pf.voller_name(ziel)
                    if pf.verschieben(uid, voll):
                        eintrag["verschoben_nach"] = ziel
                        verschoben += 1
                        if ae is not None:
                            # Die Mail liegt jetzt woanders und hat dort eine
                            # ANDERE UID. Bis der Nachtrag sie dort gesehen
                            # hat, ist die alte Nummer wertlos — 0 heisst
                            # ehrlich „weiss ich gerade nicht".
                            ae["ordner"], ae["uid"] = ziel, 0
                            beruehrte_ordner.add(ziel)
                        anhaengen("journal.jsonl", {
                            "zeit": eintrag["gesehen"], "uid": uid,
                            "von": "INBOX", "nach": voll, "anzeige": ziel,
                            "klasse": klasse, "betreff": kopf["betreff"],
                            "absender": kopf["adresse"], "zurueck": False},
                            JOURNAL_ZEILEN)
                elif darf:
                    eintrag["wuerde_nach"] = ziel

                if not kaltstart and klasse in ALARM:
                    regel = einst["regeln"].get(klasse) or {}
                    if regel.get("melden", True):
                        treffer.append(eintrag)
                koepfe.append(eintrag)
                letzte_uid = max(letzte_uid, uid)

            # Gerade verschobene Mails sofort am neuen Ort wiederfinden, damit
            # der Index nicht bis zum naechsten Nachtrag mit „weiss ich nicht"
            # dasteht.
            if beruehrte_ordner:
                try:
                    anhaenge_nachtragen(pf, idx, frist=10, nur=beruehrte_ordner)
                except Exception as e:
                    log("Nachtrag der Zielordner: %s" % str(e)[:120])
            # Stand der Uebergaben + Rueckstand — derselbe Weg wie im Lauf ohne
            # neue Post, damit es nur EINE Stelle gibt, die das tut.
            dokumente_pflegen(pf, idx, ds, ds_bekannt=True)
            anhang_index_sichern(idx)
    except imaplib.IMAP4.error as e:
        # Ein falsches Passwort sieht genauso aus wie eine Stoerung. Beides wird
        # gemeldet, aber nur EINMAL — sonst funkt der Waechter jede Minute.
        fehler = str(e)[:200]
        vorher = load(LAUF, {})
        if vorher.get("fehler") != fehler:
            log("Postfach-Fehler: %s" % fehler)
            chronik("postfach_fehler", titel=txt("w.fehler.titel"), detail=fehler)
        lauf_buchen({"uid": letzte_uid, "fehler": fehler, "grund": ""})
        status_schreiben({"aktiv": True, "eingerichtet": True, "fehler": fehler})
        return {"rc": 1, "fehler": fehler}
    except Exception as e:
        log("Lauf abgebrochen: %s" % str(e)[:220])
        status_schreiben({"aktiv": True, "eingerichtet": True, "fehler": str(e)[:200]})
        return {"rc": 1, "fehler": str(e)[:200]}

    if dokumente:
        zaehler["dokumente"] = int(zaehler.get("dokumente") or 0) + dokumente
        chronik("dokumente", titel=txt("w.dok.titel"),
                detail=txt("w.dok.detail", n=dokumente))

    koepfe = koepfe[-2000:]
    save(KOEPFE, koepfe)
    save(ABSENDER, prof)
    save("zaehler.json", zaehler)
    lauf_buchen({"uid": letzte_uid, "fehler": "", "grund": "",
                 "dauer": round(time.time() - t0, 1)})

    if kaltstart:
        verteilung = {}
        for e in koepfe:
            zaehlen(verteilung, e["klasse"])
        # Der Kaltstart darf den Tageszaehler nicht fuellen — sonst meldet der
        # erste Tagesbericht 300 Mails, die alle alt sind.
        zaehler["heute"] = {}
        save("zaehler.json", zaehler)
        _namen = schubladen_namen()
        verteilungstext = ", ".join("%d %s" % (n, _namen[k]["name"])
                                    for k, n in sorted(verteilung.items(), key=lambda kv: -kv[1]))
        chronik("kaltstart", titel=txt("w.kalt.titel"),
                detail=txt("w.kalt.detail", n=len(uids), verteilung=verteilungstext))
        log("Kaltstart: %d Mails gelernt (%s)" % (len(uids), verteilungstext))
        status_schreiben({"aktiv": True, "eingerichtet": True, "kaltstart": True,
                          "gelernt": len(uids), "verteilung": verteilung,
                          "scharf": schreiben, "uid": letzte_uid})
        return {"rc": 0, "kaltstart": True, "neu": len(uids)}

    if treffer:
        melde_sofort(treffer, einst)
    if verschoben:
        chronik("sortiert", titel=txt("w.sortiert.titel"),
                detail=txt("w.sortiert.detail", n=verschoben))

    # Weckruf nur bei echtem Urteilsbedarf: zu viele Mails, die der Waechter
    # nicht einordnen kann. Alles andere kann er selbst.
    offen_unklar = [e for e in koepfe[-400:] if e["klasse"] == "unklar"]
    if (len(offen_unklar) >= UNKLAR_SCHWELLE
            and int(zaehler.get("weckrufe") or 0) < MAX_WECKRUFE_PRO_TAG
            and not lauf.get("unklar_gemeldet_am") == heute):
        beispiele = "\n".join(
            "- %s — von %s (%s)" % (e["kopf"]["betreff"][:120],
                                    e["kopf"]["adresse"], e["grund"])
            for e in offen_unklar[-15:])
        # 🔑 Ein Weg je eingestellter Urteilshilfe. Der Werkstatt-Weg legt die
        # Faelle hin und weckt einen Agenten; jeder andere Anbieter wird direkt
        # gefragt und liefert Vorschlaege auf die Seite. Ist nichts eingerichtet,
        # bleibt es bei der Chronik — der Befund geht nie verloren.
        ki_a = ki_konfig()["anbieter"]
        nr = 0
        if ki_a not in ("aus", "werkstatt"):
            vorschlaege = ki_regeln_vorschlagen(offen_unklar)
            vorschlaege_schreiben(vorschlaege, ki_a)
            if vorschlaege:
                chronik("vorschlaege",
                        titel=txt("w.vorschlag.titel", n=len(vorschlaege)),
                        detail=txt("w.vorschlag.detail", n=len(offen_unklar)))
        elif ki_a == "werkstatt":
            uebergeben(offen_unklar)
            nr = escalate(
                "Postwache: %d Mails passen in keine Schublade" % len(offen_unklar),
                "Der Waechter ordnet nach festen Regeln ein (siehe `einordnen()` in "
                "`postwache.py`). Diese Mails fielen durch:\n\n"
                + beispiele +
                "\n\nBitte pruefen: laesst sich daraus eine REGEL ableiten (Absender, "
                "Kopfzeile, Wortmuster), oder ist das wirklich Einzelfall-Post? "
                "Vorschlaege als Notiz nach den vereinbarten Ablageordner. Regeln NICHT selbst "
                "scharfschalten — der Mensch entscheidet auf der Seite.")
        # 🔴 Der Weckruf DARF NICHT die einzige Ausgabe sein. Faellt der
        # Werkstatt-Container aus oder kennt er das Projekt nicht, waere das
        # sonst ein Pfad, der immer still scheitert — genau der Fehler, der in
        # der das Schwesterprojekt 506 Massnahmen unbemerkt verschluckt hat. Deshalb wird
        # der Befund IMMER in die Chronik geschrieben (und damit gemeldet), und
        # der Weckruf ist nur die Kuer.
        zaehler["weckrufe"] = int(zaehler.get("weckrufe") or 0) + 1
        save("zaehler.json", zaehler)
        lauf["unklar_gemeldet_am"] = heute
        lauf_buchen({"uid": letzte_uid, "fehler": "", "grund": "",
                     "unklar_gemeldet_am": heute})
        if nr:
            chronik("weckruf", titel=txt("w.weckruf.titel"),
                    detail=txt("w.weckruf.detail", nr=nr, n=len(offen_unklar)))
        else:
            chronik("unklar", titel=txt("w.unklar.titel", n=len(offen_unklar)),
                    detail=txt("w.unklar.detail", seite=konfig()["seite"]))

    tagesbericht(zaehler, prof, einst)
    status_schreiben({"aktiv": True, "eingerichtet": True, "scharf": schreiben,
                      "neu": len(uids), "verschoben": verschoben,
                      "gemeldet": len(treffer), "unklar": unklar,
                      "uid": letzte_uid})
    log("[%s] %d neue Mail(s): %d gemeldet, %d verschoben, %d unklar (%.1fs)"
        % (zug["id"], len(uids), len(treffer), verschoben, unklar, time.time() - t0))
    return {"rc": 0, "neu": len(uids), "verschoben": verschoben,
            "gemeldet": len(treffer), "unklar": unklar, "dokumente": dokumente}


def statusfelder(einst: dict, zaehler: dict, uid: int) -> dict:
    tag = zaehler.get("heute") if isinstance(zaehler.get("heute"), dict) else {}
    return {
        "friendly_name": "Postwache",
        "icon": "mdi:email-search-outline",
        "version": VERSION,
        "scharf": bool(einst.get("scharf")),
        "modus": "scharf" if einst.get("scharf") else "Lernlauf",
        "heute": tag,
        "heute_gesamt": sum(int(v) for v in tag.values()),
        "wichtig_heute": sum(int(tag.get(k) or 0) for k in ALARM),
        "letzte_uid": uid,
        "stand": datetime.now().isoformat(timespec="seconds"),
    }


def _status_lesen() -> dict:
    try:
        with open(os.path.join(OUT, "status.json"), encoding="utf-8") as fh:
            v = json.load(fh)
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def status_schreiben(d: dict) -> None:
    """`out/status.json` — das, was die Seite liest.

    Waehrend ein Postfach laeuft, landet sein Stand unter `postfaecher.<id>`;
    die oberste Ebene ist die Summe und wird am Ende von `gesamt_schreiben()`
    gesetzt. So bleibt jede der rund zehn bestehenden Aufrufstellen gueltig,
    ohne dass eine davon wissen muesste, dass es mehrere Postfaecher gibt.
    """
    d = dict(d)
    d["zeit"] = datetime.now().isoformat(timespec="seconds")
    d["version"] = VERSION
    if _PF_ID:
        ganz = _status_lesen()
        ganz.setdefault("postfaecher", {})[_PF_ID] = d
    else:
        ganz = d
    try:
        os.makedirs(OUT, exist_ok=True)
        tmp = os.path.join(OUT, "status.json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(ganz, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(OUT, "status.json"))
    except OSError:
        pass


def tagesbericht(zaehler: dict, prof: dict, einst: dict) -> None:
    """Einmal am Tag — der einzige planmaessige Bericht. Er geht auch dann raus,
    wenn nichts los war: „keine neue Post" ist eine Aussage, die man lesen
    koennen soll, ohne nachzusehen."""
    heute = datetime.now().strftime("%Y-%m-%d")
    # 🔴 Die Marke liegt GLOBAL. Lag sie im Zaehler des Postfachs, bekaeme man
    # bei drei Postfaechern drei Tagesberichte — jeder mit einem Drittel der
    # Wahrheit.
    marke = load("bericht.json", {}) or {}
    if marke.get("tag") == heute:
        return
    if datetime.now().hour != int(einst.get("bericht_stunde") or 7):
        return
    if einst.get("telegram"):
        telegram(zusammenfassung(zaehler, prof, einst))
    frueher = _PF_ID
    pf_waehlen("")
    save("bericht.json", {"tag": heute})
    pf_waehlen(frueher)


def melde_sammlung() -> None:
    """Was in dieser Runde in die Chronik ging, geht auch hinaus — aber nur die
    Ereignisse, die etwas bedeuten. Reine Zaehlstaende bleiben auf der Seite.

    🔴 Waehrend ein Postfach bearbeitet wird, passiert hier nichts. Sonst
    bekaeme man bei drei Postfaechern drei Nachrichten statt einer, und die
    dritte haette den Zusammenhang der ersten verloren."""
    if _PF_ID or not _ZU_MELDEN:
        return
    einst = einstellungen()
    if not einst.get("telegram"):
        _ZU_MELDEN.clear()
        return
    icon = {"stillgelegt": "🛑", "pause_ende": "▶️", "kaltstart": "🧊",
            "postfach_fehler": "⚠️", "weckruf": "📣", "sortiert": "🗂️",
            "dokumente": "📎"}
    zeilen = []
    for z in _ZU_MELDEN:
        if z["art"] == "sortiert":
            continue                      # das steht schon im Tagesbericht
        zeilen.append("%s *%s* — %s" % (icon.get(z["art"], "•"),
                                        _tg_md(str(z.get("titel") or z["art"])),
                                        _tg_md(str(z.get("detail") or ""))))
    _ZU_MELDEN.clear()
    if zeilen:
        telegram("📬 *Postwache*\n\n" + "\n".join(zeilen))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
