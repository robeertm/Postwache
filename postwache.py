#!/usr/bin/env python3
"""Postwache — the watchman over an IMAP mailbox.

ARCHITECTURE (the reason this costs little) — inherited from the das Schwesterprojekt:
The watchman here is MUTE. No language model, no tokens. It runs every minute
from cron (since 2026-09-19; every 5 before that — a run without new mail
takes 0.2 s), looks at the NEW mails (only those since the last remembered UID)
and classifies them by fixed, readable rules. The AGENT in the workshop is only
woken when the watchman itself does not know what to do — that is, for mails that
fit no drawer. So the cost hangs on the number of UNCLEAR cases, not on the
number of mails.

🔑 THE MOST IMPORTANT PRINCIPLE: WHAT MATTERS STAYS IN THE INBOX.
Only the noise is sorted OUT (newsletters, advertising, automatic mail).
Deadlines, official post, security warnings and mail from real people stay where
he sees them anyway. A sorter that clears away what matters is more dangerous
than none at all.

🔴 READ STATUS: EVERY fetch uses BODY.PEEK. A bare BODY[] would mark the mail as
read — the watchman would thereby change his mailbox without anyone having
ordered it. During the learning run the mailbox is additionally opened with
readonly=True, which rules out accidental writing as well.

🔴 NOTHING IS EVER DELETED. The watchman knows not a single call that deletes a
mail or sets the \\Deleted flag. Mail is only moved into subfolders of the inbox,
and every move is recorded with source and target in the journal, so that it can
be brought back one at a time or all at once.

EMERGENCY STOP: input_boolean.postwache_aktiv (phone) OR the file DISABLED in the
state folder. Either one on its own is enough. It is checked BEFORE everything
else, even before connecting to the mailbox.

LEARNING RUN: as long as `einstellungen.json` does not carry "scharf": true,
NOTHING is moved. The watchman only writes down what it WOULD do. He arms it on
the page (port 8110) once the classification is right.
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
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

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
PAUSE = os.path.join(BASE, "PAUSE")
# 🔑 Since 3.0.0 nothing environment-specific is fixed in the source. What
# used to stand here as a constant now comes from `konfig.json` — and when that
# file is absent, the environment is DETECTED. That is the difference between a
# program that runs on exactly one machine and one you can download: on the
# owner's Pi the detection finds Home Assistant and the workshop and everything
# stays as it was; on a stranger's machine it finds neither and the Postwache
# simply runs without both, rather than pointing at paths that do not exist.
# What the detection looks for when `konfig.json` says nothing. Deliberately
# generic places: a path from one single household has no business in a program
# other people download.
ENVFILE_STANDARD = os.path.join(BASE, "ha.env")
HA_STANDARD = "http://127.0.0.1:8123"
SCHALTER_STANDARD = "input_boolean.postwache_aktiv"


def _version() -> str:
    """One source for the version number: the file VERSION next to the script.
    Watchman AND page read the same file — two constants would be two truths, and
    one of them would eventually be the wrong one."""
    try:
        with open(neben_dem_programm("VERSION"), encoding="utf-8") as fh:
            return fh.read().strip() or "?"
    except OSError:
        return "?"


VERSION = _version()

LOG_ZEILEN = 4000
CHRONIK_ZEILEN = 1200
JOURNAL_ZEILEN = 5000        # every move, so that it stays reversible

ZUGANG = "zugang.json"       # 0600, up to 2.x: ONE mailbox. Gets migrated.
POSTFAECHER = "postfaecher.json"  # 0600: since 3.0.0 the list of all mailboxes
KONFIG = "konfig.json"       # Umgebung: Seite, Home Assistant, Werkstatt
KI = "ki.json"               # 0600: provider for the judgement aid
ZETTEL = "einricht_zettel.json"   # 0600: one-time note for the Ollama helper
EINST = "einstellungen.json"
LAUF = "lauf.json"
KOEPFE = "koepfe.json"       # what the watchman kept from every mail
ABSENDER = "absender.json"   # Langzeitprofil je Absender

# How many mails a cold start looks at to learn the starting position.
KALTSTART_MAILS = 300
# At most this many new mails are handled per run. A mailbox that is delivering
# 4000 mails at once must not stretch a run beyond the cron interval.
MAX_PRO_LAUF = 120
# This many run timestamps are kept. 120 × 1 min = 2 hours — enough to measure
# the interval, too little to let the file grow.
LAUF_HISTORIE = 120
TAKT_MINDEST = 4                  # fewer gaps = no statement about the interval
# More immediate alerts than this in ONE run are folded into a single line.
MELDE_EINZELN_MAX = 12

TG_API = "https://api.telegram.org/bot%s/sendMessage"
TG_GRENZE = 3800
# The page's own address lives in konfig()["seite"] — see _erkenne_umgebung().

# The agent in the workshop. It is only woken when judgement is really needed.
# 🔴 Such an agent usually runs isolated and can NOT read the watchman's own
# directory. So the watchman puts its unclear cases into an agreed folder: the
# watchman writes, the agent reads, nobody else. The path is in `konfig.json`
# under "werkstatt"; without an entry this route is off.

MAX_WECKRUFE_PRO_TAG = 4
UNKLAR_SCHWELLE = 8          # this many unclear mails make a judgement worthwhile

# ── The owner's own filing is the teacher ────────────────────────────
# The task: find the best way by itself and create more categories, perhaps
# even from the mails themselves.
#
# 🔑 The categories already exist: 35 topic folders, filled by hand over
# years. They are his own decisions — any drawer we invented would be worse. The
# watchman reads the SENDERS in those folders and derives from them where new
# mail belongs.
#
# Measured 2026-09-11: 2909 mails from 35 folders, 308 senders. 174 of them
# write unambiguously into the same folder every time — that covers 94 % of the
# post.
ABLAGE = "ablage.json"            # the learned map
ABLAGE_FRISCH = 24 * 3600         # relearn once a day, not on every run
LERN_JE_ORDNER = 400              # more does not change the verdict, only the time
# 🔑 The COARSER the level, the more evidence it needs. Measured 2026-09-11
# against 670 held-back mails (learned from the older ones, checked against the
# newest):
#
#   thresholds                          right  wrong  rate
#   0.8 / 0.8 / 0.8  (first attempt)      380     40  90.5%
#   same, own addresses excluded          380     34  91.8%
#   0.8 / 0.9 / 0.95                      359     17  95.5%
#   without the main domain               351     10  97.2%   ← chosen
#
# The main domain brings 8 hits and 7 misses — a coin toss. `check24.de` does
# hotels AND insurance, `deutschepost.de` delivers parcels AND the tax
# newsletter. So it may SUGGEST, but not act.
LERN_SCHWELLEN = {              # Stufe -> (Mindestanteil, Mindestzahl, darf_handeln)
    "absender": (0.80, 2, True),
    "domain":   (0.90, 5, True),
    "haupt":    (0.90, 5, False),
}
# Below the acting threshold there is still a SUGGESTION. An example from the
# real mailbox: `info@account.netflix.com` sits 5× in Shopping.Netflix and once
# elsewhere — 83 %, just under the bar. Deciding alone would be too bold, but
# staying silent is useless: the page shows the suggestion and one tap turns it
# into a standing rule.
VORSCHLAG_ANTEIL = 0.5
VORSCHLAG_MINDEST = 2
# 🔑 THE NAME BRIDGE. The owner left a second, completely independent
# statement that the watchman ignored until 2026-09-12: **the name of the
# folder**. `Synology.NAS01` is named after `nas01@beispielhaus.example`,
# `Shopping.Ikea` after `ikea.de`. No statistics needed for that — the mapping
# is in the name.
#
# This is the way out of a dead end that counting cannot leave: a folder with
# nothing in it can teach nothing. NAS01 to NAS04 were exactly that — created
# but empty (0/0/0/1 mails). The watchman can only imitate, and here there was
# nothing to imitate.
#
# Measured 2026-09-12:
#   against 1814 hand-filed mails                     99.0 % right
#   as a fallback ONLY where counting is silent        24 of 30 right
# The 6 deviations are not slips but the owner's own ambiguity:
# `lidl-connect@vodafone.de` sits sometimes in Shopping.Vodafone, sometimes in
# Shopping.Diverses; a Cyberport order arrived via marketplace.amazon.de. None
# of them leaves the Shopping area.
NAMENSBRUECKE_MINDEST = 3         # „DHL“ and „KIA“ should be allowed to count

LERN_MINDEST = 2                  # a single hit is not a pattern
LERN_ANTEIL = 0.8                 # Rueckfallwert

# 🔴 These folders are NOT learned from. An archive is not a topic: „Archiv
# Gmail“ holds 12396 mails, and in the sample 235 of 400 came from the owner
# HIMSELF. Learn from that and you send future post into the archive instead of
# the right folder — 16 of 106 archive senders also sit in real topic folders.
# ── Statistics ───────────────────────────────────────────────
STATISTIK = "statistik.json"
STAT_FRISCH = 6 * 3600            # six times a day is enough; the numbers change slowly
STAT_JE_ORDNER = 1500             # Deckel je Ordner — siehe „vollstaendig_ab"
STAT_TAGE = 120                   # this far back the daily course is shown
# 🔴 What does NOT belong in a statistic about INCOMING mail: sent items (he
# wrote those himself), drafts and templates (never arrived). The trash stays
# IN — what lies there did arrive and was then thrown away; leaving it out
# would answer the question „how much post do I get?“ wrongly.
KEINE_STATISTIK = {"Sent Items", "Sent", "Drafts", "Templates"}

KEIN_LEHRMEISTER = {"INBOX", "Trash", "Spam", "Drafts", "Sent Items",
                    "Archiv Gmail", "Archive", "Junk", "Sent", "Templates",
                    # 🔴 Necessary since the restructuring (4.0.0,
                    # 2026-09-27): the exclusion of „Archiv Gmail“ hung on the
                    # NAME. Its 12,396 mails now sit in ordinary folders — and
                    # two of those are dangerous as teachers:
                    #   „Unsortiert“ would teach that an unknown sender
                    #   BELONGS there — after which the content rules never fire
                    #   again, because the filing map already knows a target.
                    #   „Eigene Post Archiv“ contains only the owner's own
                    #   addresses; nothing about other senders can be learned
                    #   from it.
                    "Unsortiert", "Eigene Post Archiv"}

# ── The drawers ────────────────────────────────────────────────
# Order = precedence. The first four are the owner's alarm classes (2026-09-11:
# real people, security warnings, authorities/insurance/bank, deadlines &
# invoices), the rest is noise.
# „Stoerung“ (fault) is the fifth alarm class. It does not RAISE the number of
# wake-ups, it lowers it: before, EVERY device report raised an alarm as
# „human“; now only the one where something went wrong.
ALARM = ("sicherheit", "frist", "amt", "mensch", "stoerung")
LAERM = ("werbung", "newsletter", "automatisch")

# Since the restructuring of 2026-09-11 the drawers answer only ONE question:
# does he need to know this immediately? Where a mail travels to is decided by
# his own filing — see `ablage_lernen()`.
# 🔑 The name stands here as German text and is at the same time the fallback:
# `schublade.<key>` in the language files beats it. That keeps the source
# readable even when no language file is at hand.
def schubladen_namen(sprachcode: str = "") -> dict:
    """The drawers with a translated name — ONE place that translates,
    instead of every display doing it."""
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
    # Added on 2026-09-12. Before that a FAILED backup had no drawer of its own
    # and slipped in among the humans as „a person writes directly (NAS02)“ — a
    # reason that was simply untrue.
    "stoerung":    {"name": "St\u00f6rungen",   "icon": "\U0001F6A8"},
    "werbung":     {"name": "Werbung",      "icon": "\U0001F3F7\uFE0F"},
    "newsletter":  {"name": "Newsletter",   "icon": "\U0001F4F0"},
    "automatisch": {"name": "Automatisch",  "icon": "\U0001F916"},
    "unklar":      {"name": "Unklar",       "icon": "\u2753"},
}

_RX = re.IGNORECASE


def normal(t: str) -> str:
    """Lower case AND umlauts into their transcription (ae/oe/ue/ss).

    🔴 This is not cosmetic, it is the lesson from the first measurement: the
    pattern `ger[aeae]t` did match „Gerät“ but NOT „Geraet“ — and senders write
    both. A security warning would have been filed away as „automatic“ because
    of it. Instead of writing every pattern twice (and forgetting it on the
    next one), normalisation happens ONCE; after that every pattern is plain
    ASCII and can no longer get the question wrong.
    """
    t = (t or "").lower()
    for a, b in (("\u00e4", "ae"), ("\u00f6", "oe"), ("\u00fc", "ue"),
                 ("\u00df", "ss"), ("\u00e9", "e"), ("\u00e8", "e")):
        t = t.replace(a, b)
    return t


# From here on every pattern is ASCII — it always runs against `normal(...)`.

# Security: anything that hints at access to an account. This class is NEVER
# moved and reports even when the mail looks like a circular — a genuine warning
# often arrives looking exactly like one.
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

# Deadline: anything with a date or an amount that can pass.
# 🔴 `\brechnung` with a word boundary: without it the pattern also matched
# inside „NebenkostenabRECHNUNG“ and pushed a service-charge statement from the
# property manager into the deadlines instead of the official post (which
# happened in the first measurement).
W_FRIST = re.compile(
    r"(\brechnung(en|s)?\b|\bmahnung(en)?\b|zahlungserinnerung|"
    r"zahlungsaufforderung|letzte mahnung|inkasso|faellig|zahlbar bis|"
    r"bis zum \d|\bfrist\b|kuendigungsfrist|widerrufsfrist|"
    r"verlaengert sich automatisch|beitragsanpassung|beitragserhoehung|"
    r"lastschrift|sepa[- ]?mandat|zahlungsverzug|offene forderung|"
    r"mahngebuehr|vertrag laeuft aus|termin am \d|"
    r"rueckmeldung bis|antwort bis|zahlungsziel)", _RX)

# Authority / bank / insurance — recognised by the sender OR by the text.
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

# Advertising — a circular WITH intent to sell.
W_WERBUNG = re.compile(
    r"(rabatt|gutschein|\bsale\b|angebot|nur heute|nur noch heute|jetzt sichern|"
    r"jetzt kaufen|schnaeppchen|\bdeal\b|prozent sparen|% ?(rabatt|off)|"
    r"-\d{2} ?%|\d{2} ?% ?rabatt|black friday|cyber monday|ausverkauf|"
    r"exklusiv fuer sie|\bgratis\b|kostenlos testen|neukunden|"
    r"letzte chance|nicht verpassen|unschlagbar|bestpreis)", _RX)

# Machine: a sender with no human behind it.
W_NOREPLY = re.compile(
    r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|noreply|nicht[-_.]?antworten|"
    r"mailer[-_.]?daemon|postmaster|bounce|automat|automailer|"
    r"notification[s]?|benachrichtigung|system|robot|\bbot)\b", _RX)

# 🔴 Role mailbox: there is a human behind it, but not one writing to him
# PERSONALLY. Without this list every ticket reply ended up as „human“ and would
# have woken him.
W_ROLLE = re.compile(
    r"^(info|kontakt|contact|service|support|hilfe|help|team|office|buero|"
    r"mail|email|admin|webmaster|hello|hallo|moin|shop|bestellung|order|"
    r"kundenservice|kundendienst|vertrieb|sales|marketing|presse|jobs|"
    r"newsletter|news|abo|billing|rechnungen|buchhaltung|zentrale)"
    r"([-_.]?\d+)?$", _RX)

# Company display names do not look like people.
W_FIRMA = re.compile(
    r"(gmbh|\bag\b|\bkg\b|\be\.?v\.?\b|ltd|inc\b|team|service|support|"
    r"shop|\binfo\b|kundenservice|kundendienst|newsletter|redaktion|"
    r"vertrieb|zentrale|hotline|noreply|no-reply)", _RX)

# Brands whose names phishing likes to carry in the display name. The value is
# the piece that has to occur in the REAL sender domain.
MARKEN = {
    "paypal": "paypal.", "amazon": "amazon.", "apple": "apple.",
    "microsoft": "microsoft.", "netflix": "netflix.", "hoster": "hoster.",
    "telekom": "telekom.", "vodafone": "vodafone.", "dhl": "dhl.",
    "hermes": "hermes", "postbank": "postbank.", "sparkasse": "sparkasse",
    "commerzbank": "commerzbank.", "volksbank": "volksbank",
    "deutsche bank": "deutsche-bank.", "ing": "ing.de", "dkb": "dkb.",
    "n26": "n26.", "klarna": "klarna.", "shopify": "shopify.",
    "google": "google.", "whatsapp": "whatsapp.", "disney": "disney",
}

# 🔑 Machine post. Measured on 2026-09-12: the four NAS boxes report as
# `"NAS02" <nas02@beispielhaus.example>` with the subject
# `[beispielhaus02.synology.me]Network backup - ... erfolgreich`. The
# human rule fired, because „NAS02“ is not a company name — and so EVERY backup
# run landed in an ALARM CLASS. He was woken for a backup that had succeeded.
#
# 🔴 The detection needs TWO features, not one. „display name equals mailbox
# name“ alone would declare `"anna" <anna@beispiel.example>` a machine — exactly
# the mistake that once broke the human rule (it demanded a space in the name
# and thereby discarded a first name on its own).
# 🔴 The bracket must contain a HOSTNAME, not just anything. The first version
# only checked for „[…]“ — and the counter-check promptly declared
# `"Mike" <mike@example.de>` with the subject „[Wichtig] Kannst du mal schauen?“
# a machine, which would have muted a friend. A hostname has a dot and no
# space; „Wichtig“ has neither.
BETREFF_HOST = re.compile(r"^\s*\[[^\]\s]*\.[^\]\s]*\]")  # „[host.synology.me] …"

# 🔴 A fault report is NOT noise. Mute machine post wholesale and you take
# away the message that a backup FAILED — which would be worse than the wake-ups
# this bolt prevents.
# 🔴 TWO patterns, not one. The first version looked for ALL fault words in the
# whole text — and promptly declared a successful Synology task a fault, because
# the report contains the line „Standardausgabe/Fehler:“ while two lines above it
# reads „Aktueller Status: 0 (normal)“. The word was a FIELD LABEL, not a
# result.
#
# Result words say how it ended — those may count anywhere.
W_STOERUNG_HART = re.compile(
    r"(fehlgeschlagen|fehlschlag|gescheitert|abgebrochen|nicht\s+erfolgreich|"
    r"ausgefallen|\bdefekt\b|degraded|\bfailed\b|\bfailure\b|"
    r"\baborted\b|\bunsuccessful\b|\boffline\b)", _RX)
# Label words appear in every report, including a successful one. They count
# ONLY in the subject — that is where a device writes its result, not a legend.
W_STOERUNG_WEICH = re.compile(
    r"(fehler|warnung|kritisch|\berror\b|\bwarning\b|\bcritical\b)", _RX)

BETRAG = re.compile(r"\d{1,3}(?:[.\s]\d{3})*,\d{2}\s*(?:€|EUR\b)", _RX)
DATUM = re.compile(r"\b\d{1,2}\.\s?\d{1,2}\.\s?\d{2,4}\b")

# Collects the history entries of THIS run (as in the das Schwesterprojekt, the report
# hangs off the log, not off a list of call sites).
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
    """Read from the 0600 file. Cron passes no environment, and a token does not
    belong in the source of a readable file."""
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


# 🔑 THE WHOLE MULTI-MAILBOX REBUILD HANGS ON THESE THREE LINES.
# The watchman has about a hundred places that read and write state — rewriting
# them all would have been a hundred chances to make a mistake. Instead the ONE
# bottleneck is rebuilt: `load`/`save` put a file that belongs to a particular
# mailbox under `state/pf/<id>/`. The rest of the program notices nothing.
PRO_POSTFACH = frozenset({
    "lauf.json", "koepfe.json", "absender.json", "ablage.json",
    "statistik.json", "anhaenge.json", "zaehler.json",
})
# Which mailbox is being worked on. Empty = the global files.
_PF_ID = ""


def pf_waehlen(pf_id: str) -> None:
    """From here on every piece of state belongs to this mailbox. Global are the
    things that apply to all: settings, credentials, journal, log, history."""
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
    # 🔴 Credentials must never sit on disk with 0644, not even briefly. So
    # the TEMPORARY file already gets the right mode, not just the target.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, modus)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, ziel)


def anhaengen(datei: str, satz: dict, grenze: int) -> None:
    """One line into a JSONL file, with a hard cap. Without a cap this would be a
    leak: the watchman runs 288 times a day."""
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
    """History: what the watchman did WHEN. As in the das Schwesterprojekt, the Telegram
    report hangs off this — anything that goes into the log can be reported. An
    immediate repetition is folded together instead of appended."""
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
    """Up to 2.x there was exactly ONE mailbox, and its state lay flat in
    `state/`. From 3.0.0 there is a list, and every piece of state belongs to a
    mailbox.

    🔴 The migration MOVES the existing files to `state/pf/standard/`, it does
    not copy them. Two copies of the same filing map would be worse than no
    migration at all: the watchman would read one and write the other, and
    nobody would see it. `zugang.json` is left untouched — that is the safety
    net in case something is missing after all.
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
    """All configured mailboxes, always complete and plausible.

    A mailbox without an address or without an id is skipped rather than guessed —
    a guessed id would be a second state folder, and that only shows up once the
    numbers stop adding up.
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
        satz = {
            "id": pid,
            "name": str(e.get("name") or "").strip() or adresse,
            "adresse": adresse,
            "passwort": str(e.get("passwort") or ""),
            "server": (str(e.get("server") or "").strip()
                       or konfig()["imap_server"] or _server_raten(adresse)),
            "port": int(e.get("port") or 993),
            "an": bool(e.get("an", True)),
        }
        # 🔴 THE WAY OUT BELONGS TO THE MAILBOX (5.0.0). This function does not
        # copy an entry, it REBUILDS it out of a fixed set of fields — everything
        # else is silently dropped. The client stored its outgoing server and
        # promptly kept sending to the guessed one: the file was right, the answer
        # was old. Found by the test bench, which asked for the EFFECT instead of
        # trusting the write — the same lesson as with the own address in 4.5.3.
        # Whoever adds a field to a mailbox has to enter it HERE as well.
        for feld, vorgabe in (("smtp_server", ""), ("smtp_port", 0),
                              ("smtp_art", ""), ("smtp_benutzer", ""),
                              ("smtp_passwort", ""), ("absender_name", "")):
            wert = e.get(feld, vorgabe)
            if feld == "smtp_port":
                try:
                    wert = int(wert or 0)
                except (TypeError, ValueError):
                    wert = 0
            else:
                wert = str(wert or "")
            satz[feld] = wert
        fertig.append(satz)
    return fertig


def _server_raten(adresse: str) -> str:
    """Only a suggestion for the form, never a silent assumption during a run:
    enter nothing and you get `imap.<domain>` — which is right at a great many
    providers and, where it is not, immediately visible in an error message."""
    dom = adresse.rsplit("@", 1)[-1].strip().lower()
    return ("imap." + dom) if dom and "." in dom else ""


# ═══ Languages ════════════════════════════════════════════════
# The texts live as flat key/value files in `locales/<code>.json` NEXT TO the
# program — not in the state folder: they belong to the code and ship with it.
#
# 🔴 The language is a setting of the INSTALLATION, not a property of the
# browser. The watchman writes history and Telegram messages when nobody is
# looking — a cookie cannot tell it which language to use. Anyone who needs two
# languages in one house needs two Postwachen.
SPRACHEN = ("de", "en", "es", "fr", "it")
# The names in their OWN spelling. „Francais“ instead of „Français“ is the first
# impression a French reader gets of this program's care — and it would be an
# accurate one.
SPRACHNAMEN = {"de": "Deutsch", "en": "English", "es": "Espa\u00f1ol",
               "fr": "Fran\u00e7ais", "it": "Italiano"}
RUECKFALL = "de"          # the language the Postwache grew up in
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
    """Fetch a text. If it is missing in the chosen language the fallback
    language applies; if it is missing there too, the KEY comes back.

    🔴 The key as the last fallback is deliberate: a missing text then stands
    out at once („kopf.titel“ in the middle of the page) instead of silently
    leaving an empty spot that nobody notices.
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
    """Fallback language and chosen language laid on top of each other — so that
    a lookup in the browser ALWAYS finds something and the page never shows a
    key just because one translation is missing."""
    code = code or sprache()
    zusammen = dict(_texte(RUECKFALL))
    zusammen.update(_texte(code))
    return zusammen


def _erkenne_umgebung() -> dict:
    """What can be found on THIS machine? Only asked when `konfig.json` says
    nothing.

    🔴 The reason for detection instead of empty defaults: on the machine where
    the Postwache grew, the emergency stop hangs on a Home Assistant switch and
    the judgement aid on the workshop. Had 3.0.0 simply said „nothing preset“,
    the update would have silenced both — the emergency stop first. Detection
    preserves what has grown without casting it in concrete.
    """
    ha_datei = ENVFILE_STANDARD if os.path.isfile(ENVFILE_STANDARD) else ""
    # The workshop is this household's own build and is NOT searched for —
    # whoever has one enters its path. Guessing here would only mean looking on
    # a stranger's machine for something that never exists there.
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
    """The environment this Postwache stands in. Order: whatever `konfig.json`
    says, otherwise whatever was detected. Each part on its own — enter just the
    page address and you do not lose the Home Assistant connection."""
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
        # Empty = no judgement aid through the workshop. For everyone outside
        # this house that is the normal case; there `ki.json` takes over.
        "werkstatt": str(k.get("werkstatt", erkannt["werkstatt"]) or ""),
        "imap_server": str(k.get("imap_server") or "").strip(),
        "sprache": sprache(),
    }
    return _KONFIG_ZWISCHEN


def ha_an() -> bool:
    """Home Assistant is connected when a token file is named. Without it there
    is no switch, no sensor — and no error messages about something missing that
    does not belong here in the first place."""
    k = konfig()["ha"]
    return bool(k["url"] and k["token_datei"])


def einstellungen() -> dict:
    """Always complete and plausible. If something is missing, the CAUTIOUS case
    applies: not armed. A missing file must never lead to the mailbox being
    rearranged unasked.

    🔑 Since the restructuring of 2026-09-11 the rules carry only the question
    „report immediately?“. WHERE a mail goes is no longer here but in the owner's
    own filing (`ablage.json`) — a setting can no longer move it, because it is
    no longer a setting.
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
        # With this switched on, everything from the four alarm classes stays
        # put, even when the filing map knows a target.
        "wichtiges_bleibt": bool(e.get("wichtiges_bleibt", False)),
        # New mail must always be analysed and filed, and where new folders
        # are needed it should create them on its own.
        # 🔴 That lifts the bolt from 2.0 („only where the owner has already
        # filed something himself“). So it stays a SWITCH: whoever wants the old
        # principle back turns it off. Phishing always stays put, and the alert
        # goes out regardless of where the mail travels.
        "selbst_sortieren": bool(e.get("selbst_sortieren", True)),
        "regeln": fertig,
        "absender_regeln": (e.get("absender_regeln")
                            if isinstance(e.get("absender_regeln"), dict) else {}),
    }


def zugang(pf_id: str = "") -> dict:
    """ONE mailbox — without an argument the one being worked on, otherwise the
    first.

    It survives the rebuild for several mailboxes, because the page needs
    exactly one mailbox in a dozen places (create a folder, check credentials,
    add attachments). If none is available an empty dictionary comes back — the
    watchman does not guess and does not ask anyone.
    """
    faecher = postfaecher()
    if not faecher:
        return {}
    ziel = str(pf_id or _PF_ID or "").strip()
    for f in faecher:
        if f["id"] == ziel:
            return f
    return faecher[0]


# ── Report to the owner (Telegram, the same bot as das Schwesterprojekt/DocuSort) ───────
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
    """A message to the owner. NEVER raises: a messenger must not stop the
    watchman."""
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
            # 🔴 The reason may go into the log, the token NEVER — it is in the URL.
            log("Telegram HTTP %s: %s" % (e.code, grund))
            return False
        except Exception as e:
            log("Telegram nicht erreichbar: %s" % str(e)[:160])
            return False
    return False


# ── Home Assistant ────────────────────────────────────────────────────────────
def api(path: str, tok: str, timeout: int = 20, data=None):
    """Without `data` a GET, with `data` a POST.

    🔴 Exactly this distinction was missing in the das Schwesterprojekt: `service()` passed
    `data=`, `api()` did not know the name, and EVERY measure failed silently
    with a TypeError — 506 times in four days. Built in here from the start.
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
    """Emergency stop and pause — checked FIRST OF ALL, before the mailbox. The
    file works even when HA does not answer (the switch is then unreachable);
    the switch works from a phone."""
    if os.path.exists(DISABLED):
        try:
            grund = open(DISABLED, encoding="utf-8").read().strip()
        except OSError:
            grund = ""
        return txt("w.aus.datei") + (" — " + grund[:120] if grund else "")
    if os.path.exists(PAUSE):
        try:
            p = json.load(open(PAUSE, encoding="utf-8"))
            bis = datetime.fromisoformat(p.get("bis"))
            if datetime.now() < bis:
                return txt("w.aus.pause", bis=bis.strftime("%H:%M"),
                           grund=p.get("grund") or txt("w.aus.ohne_grund"))
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
            return txt("w.aus.schalter", was=konfig()["ha"]["schalter"])
    except urllib.error.HTTPError as e:
        # 404 = the switch does not exist (yet). That is NOT an emergency stop —
        # otherwise a forgotten helper could silently shut the watchman up.
        if e.code != 404:
            log("Schalter nicht lesbar: HTTP %s" % e.code)
    except Exception as e:
        log("Schalter nicht lesbar: %s" % str(e)[:120])
    return ""


def push_status(tok: str, zustand: str, attrs: dict) -> None:
    if _PF_ID:
        return          # the sensor covers the whole Postwache, not one mailbox
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
def msg_aus_bytes(roh: bytes):
    """A header block as a message — EIGHT-BIT SAFE.

    🔴 `email.message_from_bytes()` reads header lines as ASCII and replaces every
    other byte with U+FFFD. The information is gone BEFORE anyone could decode it —
    no later repair can bring it back. And plenty of real mail sends its umlauts
    raw instead of as `=?UTF-8?B?…?=`.

    Found in the SCREENSHOT of the client, not in the source: the list showed
    „Ivo Sandstr\ufffd\ufffdm" and „Gr\ufffd\ufffde" where the mailbox has
    „Ivo Sandström" and „Grüße". So the bytes are decoded FIRST, with the charset
    that fits, and parsed afterwards.
    """
    for satz in ("utf-8", "cp1252", "iso-8859-15"):
        try:
            return email.message_from_string(roh.decode(satz))
        except (UnicodeDecodeError, LookupError):
            continue
    return email.message_from_bytes(roh)


def dekodieren(roh) -> str:
    """Make MIME-encoded header lines readable (=?UTF-8?B?…?=)."""
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
    """(display name, address) — both lower-cased except the name.

    🔴 2026-09-27, measured against 11 Booking.com mails in „Unsortiert“:
    `parseaddr` cannot cope with a MIME-encoded display name that is in quotes
    AND folded across two lines:

        From: "=?UTF-8?B?UHl0bG91bi4uLg==?=
          =?UTF-8?B?b20=?=" <noreply@booking.com>

    It returned `adr='=?UTF-8?B?UHl0bG91bi4uLg==?='` — half the NAME as the
    address. The mail therefore had no recognisable sender domain, no rule could
    fire, and 11 obvious travel mails sat in the catch folder.

    🔑 Decode first, then split — but ONLY as a fallback. The normal path stays
    untouched; the other way round is tried only when the result contains no
    "@". That way the repair cannot break anything that works today.
    """
    roh = von or ""
    name, adresse = email.utils.parseaddr(roh)
    if "@" not in (adresse or ""):
        name2, adresse2 = email.utils.parseaddr(dekodieren(roh))
        if "@" in (adresse2 or ""):
            name, adresse = name2, adresse2
    return dekodieren(name).strip(), (adresse or "").strip().lower()


# ── The classification (the core — fixed, readable, no language model) ─────
def phishing_verdacht(name: str, adresse: str) -> str:
    """The display name names a brand, the sender domain does not belong to it.

    Deliberately narrow: a warning is raised only when a KNOWN brand appears in
    the name. A broad suspicion would be a false-alarm generator, and a warning
    that keeps crying wolf stops being read.
    """
    n = (name or "").lower()
    dom = adresse.split("@")[-1] if "@" in adresse else ""
    for marke, muss in MARKEN.items():
        if marke in n and muss not in dom:
            return txt("w.grund.phishing", marke=marke, wo=dom or "?")
    return ""


def einordnen(kopf: dict, text: str) -> dict:
    """Put a mail into exactly one drawer and give the reason why.

    The reason is not decoration: it stands next to every mail on the page. A
    classification you cannot follow is one you cannot correct either — and
    being able to correct it is the whole point of the learning run.

    The order is the precedence. The four alarm classes come first: better once
    too often in the inbox than once too rarely.
    """
    name = kopf.get("name") or ""
    adresse = (kopf.get("adresse") or "").lower()
    # EVERYTHING runs against the normalised form — see `normal()`.
    heu = normal((kopf.get("betreff") or "") + " " + (text or ""))
    n_name = normal(name)
    bulk = bool(kopf.get("bulk"))
    lokal = adresse.split("@")[0] if "@" in adresse else adresse
    noreply = bool(W_NOREPLY.search(lokal))
    rolle = bool(W_ROLLE.search(lokal))

    verdacht = phishing_verdacht(name, adresse)

    # 1) Security — beats everything else, a circular and a no-reply sender
    #    included: genuine warnings almost always come from one.
    if W_SICHER.search(heu):
        g = txt("w.grund.sicherheit")
        if verdacht:
            g += " · \u26a0\ufe0f " + verdacht
        return {"klasse": "sicherheit", "grund": g, "phishing": verdacht}

    # 2) Deadline — something that can pass. For a circular only with a real
    #    amount or date; otherwise every shop advert containing the word
    #    „Angebot“ (offer) lands among the deadlines.
    if W_FRIST.search(heu):
        hat_zahl = bool(BETRAG.search(heu) or DATUM.search(heu))
        if not bulk or hat_zahl:
            g = txt("w.grund.frist_zahl") if hat_zahl else txt("w.grund.frist")
            return {"klasse": "frist", "grund": g, "phishing": verdacht}

    # 3) Authority, bank, insurance — by the sender OR by the text.
    if W_AMT_DOMAIN.search(adresse) or W_AMT_TEXT.search(heu):
        woran = (txt("w.grund.woran_domain") if W_AMT_DOMAIN.search(adresse)
                 else txt("w.grund.woran_text"))
        return {"klasse": "amt", "grund": txt("w.grund.amt", woran=woran),
                "phishing": verdacht}

    # 3b) Machine post. TWO features must come together, never one alone: the
    #     display name is the same as the mailbox name AND the subject starts
    #     with a HOSTNAME in square brackets.
    #
    #     🔴 The first version also accepted „name looks like a device id“
    #     (nas02) as the second feature. The counter-check settled that at once:
    #     `"anna2" <anna2@example.de>` with „Servus“ would have been a machine.
    #     A bolt that is only checked in ONE direction is no proof — the same
    #     lesson as with the page's typing bolt.
    maschine = bool(n_name and n_name == normal(lokal)
                    and BETREFF_HOST.search(kopf.get("betreff") or ""))
    if maschine:
        n_betreff = normal(kopf.get("betreff") or "")
        if W_STOERUNG_HART.search(heu) or W_STOERUNG_WEICH.search(n_betreff):
            return {"klasse": "stoerung",
                    "grund": txt("w.grund.stoerung", wer=name.strip()),
                    "phishing": verdacht}
        return {"klasse": "automatisch",
                "grund": txt("w.grund.vollzug", wer=name.strip()),
                "phishing": verdacht}

    # 4) Human — BEFORE the noise, so an acquaintance with a newsletter
    #    signature does not end up in the advertising folder. A human is: no
    #    circular, no machine, no role mailbox and no company name in the sender.
    if not bulk and not noreply and not rolle and not W_FIRMA.search(n_name):
        if name.strip():
            return {"klasse": "mensch",
                    "grund": txt("w.grund.person", wer=name.strip()),
                    "phishing": verdacht}
        # No display name, but a personal-looking address
        # (firstname.lastname@...) is still a human.
        if re.fullmatch(r"[a-z]{2,}[._-][a-z]{2,}\d{0,3}", lokal):
            return {"klasse": "mensch",
                    "grund": txt("w.grund.person_adresse", wer=lokal),
                    "phishing": verdacht}

    # 5) Noise — only what identifies itself as a circular.
    if bulk:
        grund_kopf = kopf.get("bulk_grund") or txt("w.grund.listenkopf")
        if W_WERBUNG.search(heu):
            return {"klasse": "werbung",
                    "grund": txt("w.grund.werbung", woran=grund_kopf),
                    "phishing": verdacht}
        return {"klasse": "newsletter",
                "grund": txt("w.grund.newsletter", woran=grund_kopf),
                "phishing": verdacht}

    # 6) A machine with no list header (order confirmation, system message).
    if noreply:
        return {"klasse": "automatisch",
                "grund": txt("w.grund.automat", wer=lokal),
                "phishing": verdacht}

    # 7) Role mailbox with no further feature: there is a human behind it, but
    #    the watchman cannot say whether it concerns him. These are exactly the
    #    cases the agent exists for.
    return {"klasse": "unklar",
            "grund": (txt("w.grund.rolle", wer=lokal) if rolle
                      else txt("w.grund.unklar")),
            "phishing": verdacht}


# ── IMAP ──────────────────────────────────────────────────────────────────────
class Postfach:
    """A thin shell around imaplib. Every fetch uses BODY.PEEK, every mailbox is
    opened read-only during the learning run."""

    def __init__(self, zug: dict, schreiben: bool):
        self.zug = zug
        self.schreiben = schreiben
        self.m = None
        self.trenner = "/"

    def __enter__(self):
        socket.setdefaulttimeout(45)
        self.m = imaplib.IMAP4_SSL(self.zug["server"], self.zug["port"])
        self.m.login(self.zug["adresse"], self.zug["passwort"])
        # ASK the server for the hierarchy separator instead of guessing it:
        # some providers use „.“, others „/“ — guess, and you create a folder with
        # a dot in its name instead of a subfolder.
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
        """All UIDs greater than `ab`. A UID range is the only way that does not
        need a full walk through the mailbox."""
        typ, daten = self.m.uid("search", None, "UID %d:*" % (ab + 1))
        if typ != "OK" or not daten or not daten[0]:
            return []
        # 🔴 With an empty remainder, „UID n:*“ returns the highest existing
        # UID as well — the server cannot answer the range with nothing. So the
        # result is filtered against `ab` here too, otherwise the same mail
        # counts as new on every run.
        return sorted(u for u in (int(x) for x in daten[0].split()) if u > ab)

    def letzte_uids(self, anzahl: int) -> list:
        typ, daten = self.m.uid("search", None, "ALL")
        if typ != "OK" or not daten or not daten[0]:
            return []
        alle = sorted(int(x) for x in daten[0].split())
        return alle[-anzahl:]

    def kopf_und_text(self, uid: int):
        """Headers and the start of the body — ALWAYS with PEEK, so the mail is
        not marked as read."""
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
        msg = msg_aus_bytes(roh_kopf)
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
        """A folder's number space. When it changes, every remembered UID is
        worthless."""
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
        """The BLUEPRINT of several mails (BODYSTRUCTURE) — without fetching a
        single byte of attachment. That is the whole trick of the document
        index: it costs so little that it can run backwards over years."""
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
        """Fetch ONE attachment — with PEEK, so the mail stays unread."""
        typ, daten = self.m.uid("fetch", str(uid), "(BODY.PEEK[%s])" % nr)
        if typ != "OK" or not daten:
            return b""
        roh = b""
        for st in daten:
            if isinstance(st, tuple) and len(st) >= 2:
                roh += st[1] or b""
        return teil_entpacken(roh, kodierung)

    def teil_aus_ordner(self, ordner: str, uid: int, nr: str, kodierung: str) -> bytes:
        """The same for a mail that has already been filed. With the same
        `finally` that returns to INBOX — a change of folder has to return to
        where it came from (measured 2026-09-11)."""
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
        """(header, UID, blueprint) of a folder's mails from a given UID on.

        Reads in blocks and stops when the time is up — the rest follows on the
        next run. Read-only and BODY.PEEK, as everywhere else."""
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
                                koepfe[u] = msg_aus_bytes(st[1] or b"")
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
            # 🔴 BACK TO INBOX — otherwise every following fetch grasps at nothing.
            try:
                self.m.select("INBOX", readonly=not self.schreiben)
            except Exception:
                pass
        return saetze, hoechste, fertig

    def ordner_liste(self) -> list:
        """All folders of the mailbox, exactly as the server names them."""
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
        """Who has written into this folder? ONLY the sender header — no subject,
        no body. And read-only, so that learning is guaranteed to change nothing
        in the mailbox."""
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
                    # 🔴 Through `absender_teile()`, not taken apart here —
                    # otherwise the encoded display name falls through at
                    # exactly this spot again (two versions of the same question
                    # are one too many).
                    _, adr = absender_teile(
                        msg_aus_bytes(st[1]).get("From", ""))
                    if adr and "@" in adr:
                        zaehler[adr] = zaehler.get(adr, 0) + 1
        except Exception as e:
            log("Ordner %s nicht lesbar: %s" % (name, str(e)[:100]))
        finally:
            # 🔴 BACK TO INBOX. Without it the server stays on the folder last
            # read, and every following `uid fetch` grasps at nothing — the UIDs
            # come from the inbox but do not apply there. Measured 2026-09-11:
            # after learning, 14 mails were fetched and NOT ONE was found, and
            # the run still reported success. A change of folder has to return
            # to where it came from.
            try:
                self.m.select("INBOX", readonly=not self.schreiben)
            except Exception:
                pass
        return zaehler

    def koepfe_im_ordner(self, name: str, grenze: int):
        """(sender, date) of the newest `grenze` mails of a folder.

        Built like `absender_im_ordner`: read-only, BODY.PEEK, in blocks — and
        the same `finally` that returns to INBOX. Without it the server stays on
        the folder last read and every following fetch grasps at nothing
        (measured 2026-09-11).

        Also returns whether the cap took effect — from that comes the boundary
        beyond which the daily course is COMPLETE. A chart that quietly droops
        at its left edge because data is missing there is lying.
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
                    msg = msg_aus_bytes(st[1])
                    _, adr = absender_teile(msg.get("From", ""))
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
        """The folder names come from `ordner_liste()` and are therefore already
        written the way the server knows them (e.g. „Shopping.Paypal“). So
        NOTHING is assembled here — insert the separator yourself and the next
        server quirk has you creating a folder with a dot in its name instead of
        writing into the existing one."""
        return name

    def ordner_sicherstellen(self, pfad: str) -> str:
        """Create a subfolder below INBOX if needed. Returns the server-correct
        full name."""
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
        """Copy, then tick off in the source folder. 🔴 EXPUNGE is NEVER called
        and a \\Deleted is never set without a successful copy beforehand — if
        the copy fails, the mail is left untouched."""
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
        """Undo a move: from the target folder back into the inbox. Called by the
        page."""
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


# ── Documents in the post ────────────────────────────────────────
# The task: mails carrying PDFs or similar documents with
# information in them (no photos, no PNGs and the like) should be handed over to
# DocuSort for filing; and he wants to search filed mails for such documents,
# also retroactively.
#
# 🔑 THESE ARE TWO THINGS, and they are kept apart — exactly as „does it report
# immediately?“ and „where does it belong?“ have been kept apart since the 2.0
# rebuild:
#
#   1. WHAT IS ATTACHED?  -> the INDEX. It is built for every mail from the
#      BODYSTRUCTURE, that is from the mail's BLUEPRINT. Not one byte of
#      attachment is fetched for it. That is why it can be pulled backwards over
#      years, and why it is complete even when DocuSort is not set up at all.
#
#   2. WHAT GOES ON?      -> the HANDOVER to DocuSort. Only documents, only what
#      DocuSort can actually digest, never on a phishing suspicion, and only
#      while the connection is configured and switched on.
#
# Throw the two together and you have an index that depends on a setting — and
# later you will fail to find exactly those mails for which the handover
# happened to be off.
ANHAENGE = "anhaenge.json"          # the index: what hangs on which mail
ANHANG_INDEX_MAX = 20000            # Deckel; aeltestes fliegt zuerst raus
# 🔴 NOT a work cap but an emergency brake. The first back-fill on
# 2026-09-25 ran with 4000 per folder — „Archiv Gmail“ has 12,396 mails, so the
# oldest 8400 were skipped. And because the back-fill remembers the HIGHEST UID
# it has read, they would never have been looked at again: a gap that covers
# itself up. The pace is set by TIME (`frist`), and time can continue on the
# next run. If the cap does take effect, it says so.
NACHTRAG_JE_ORDNER = 50000
NACHTRAG_FRIST = 45                 # seconds per run — cron comes every minute
NACHTRAG_FRISCH = 12 * 3600         # twice a day is enough for the back-fill

# 🔴 These folders stay out of the back-fill. The list is DELIBERATELY not the
# same as `KEIN_LEHRMEISTER`: that one is about LEARNING (an archive is not a
# topic, it spoils the mapping), this one is about FINDING. A 2019 phone bill
# sits in the archive — not indexing it there would mean being unable to answer
# exactly the question that was asked.
KEIN_NACHTRAG = {"Trash", "Spam", "Junk", "Papierkorb"}

# 🔑 THE EXTENSION DECIDES, NOT THE MIME TYPE. A great many senders declare
# their PDF invoice as `application/octet-stream` — go by the type and you miss
# them. The type is only consulted when there is no usable file name.
DOK_ENDUNGEN = {".pdf", ".csv", ".doc", ".docx", ".odt", ".rtf",
                ".xls", ".xlsx", ".ods", ".ppt", ".pptx", ".odp"}
# „no photos or PNGs or that kind of stuff“ — his words, and the right
# boundary: an image can be a document (a scan), but it is NOT distinguishable
# from the company logo under a signature.
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
# 🔴 What DocuSort REALLY accepts (measured against the running service 0.58.1:
# `ALLOWED_SUFFIXES` in web/app.py plus the CSV route into the finances). A
# .docx would come back from there as `rejected` — so it counts as a document
# here (and is findable), but is not handed over, and that is noted on the
# entry. Discarding it silently would be the worse mistake.
DS_ENDUNGEN = {".pdf", ".csv"}


def endung(name: str) -> str:
    n = (name or "").strip().lower()
    i = n.rfind(".")
    return n[i:] if 0 < i and len(n) - i <= 6 else ""


def anhang_art(typ: str, subtyp: str, name: str) -> str:
    """„dokument“ | „bild“ | „kram“. One word the rest hangs on."""
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


# ── Reading BODYSTRUCTURE ──────────────────────────────────────
# A mail's blueprint arrives as a nested IMAP list. That has to be taken apart,
# and taken apart properly: a file name may contain brackets
# („Rechnung (Kopie).pdf“), and long names or names with umlauts arrive as a
# LITERAL — imaplib passes those through as a piece of their own, mid-sentence.
def _imap_stuecke(roh: bytes):
    """Splits an IMAP response into (kind, value): „(“, „)“, „s“ (a quoted
    string) or „a“ (a bare word, e.g. NIL or a number).

    🔴 The s/a distinction is not decoration: a bracket INSIDE quotes is text,
    not a bracket. Treat both the same and you tear apart every file name with a
    bracket, and with it the whole blueprint."""
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
    """The response line as a nested list. NIL becomes None."""
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
    """Carry the bracket count forward. Brackets inside quotes do not count."""
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
    """Build ONE complete line per mail out of imaplib's response list.

    🔴 Where one piece ends and the next begins is decided NOT by how the line
    looks but by the BRACKET BALANCE. A response with a literal arrives as a
    tuple, the rest of the same line as a separate piece after it — check for
    „starts with a number“ and you cut apart exactly the mails with an umlaut in
    the file name, which are the interesting ones."""
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
    """{uid: blueprint} out of a FETCH response."""
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
    """Fetch one parameter from the flat pair list — RFC 2047 (=?utf-8?B?…?=) and
    RFC 2231 (filename*0*, in pieces, percent-encoded) included. Both forms occur
    in real post, and precisely with the long German invoice names."""
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
    """All simple parts of a mail with their IMAP part number.

    The numbers are what you later use to fetch a single attachment. With an
    embedded mail (forwarded post!) the inner parts continue counting under the
    number of the outer one — and that is often exactly where the invoice
    hangs."""
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
    # The disposition (attachment/inline) sits among the extensions, and where
    # they sit depends on the type. Instead of counting three special cases, the
    # SHAPE is searched for: a list whose first word is „attachment“ or
    # „inline“. That holds even when a server leaves a field out.
    verfuegung, dparams = "", []
    for el in struct[7:]:
        if (isinstance(el, list) and el and isinstance(el[0], str)
                and el[0].lower() in ("attachment", "inline")):
            verfuegung = el[0].lower()
            dparams = el[1] if len(el) > 1 and isinstance(el[1], list) else []
            break
    name = _param(dparams, "filename") or _param(params, "name")
    # 🔴 The character set and the Content-ID belong to the part, not to the
    # reader. The document index does not need either — the CLIENT needs both:
    # without the charset every text is decoded as UTF-8 and a Latin-1 mail turns
    # into rubble, and without the Content-ID an inline image cannot be assigned
    # to the „cid:" in the HTML. Two more fields in the SAME answer, instead of a
    # second reader asking the same question (5.0.0).
    eintrag = {"nr": nr, "typ": typ, "subtyp": sub, "name": name,
               "groesse": groesse, "kodierung": kod.upper(),
               "verfuegung": verfuegung,
               "zeichensatz": _param(params, "charset"),
               "id": (str(struct[3]).strip("<>")
                      if len(struct) > 3 and struct[3] else "")}
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
    """Only real attachments: everything with a file name. Body text has none."""
    raus = []
    for t in _teile(struct):
        name = (t.get("name") or "").strip()
        if not name:
            continue
        if t["typ"].upper() == "MESSAGE" and t["subtyp"].upper() == "RFC822":
            continue            # not the envelope itself, only what hangs inside it
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

# ── The index ────────────────────────────────────────────────
def anhang_schluessel(kopf: dict, ordner: str, uid: int) -> str:
    """🔴 The key is the Message-Id, NOT (folder, UID). A UID only applies inside
    its folder, and the watchman moves post — the same mail gets a new UID when
    it moves. Key on that and every filed mail is in the index twice and none of
    them findable."""
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
        # Oldest out first — by the DATE OF THE MAIL, not by when it was
        # indexed: otherwise a back-fill of old folders throws away exactly what
        # it has just found.
        nach_alter = sorted(e.items(), key=lambda kv: str(kv[1].get("datum") or ""))
        for k, _ in nach_alter[:len(e) - ANHANG_INDEX_MAX]:
            e.pop(k, None)
    idx["eintraege"] = e
    idx["gesichert"] = datetime.now().isoformat(timespec="seconds")
    idx["zahlen"] = dokument_zaehlung(idx)
    save(ANHAENGE, idx)
    dokument_kurz_schreiben(idx)


def dokument_kurz_schreiben(idx: dict) -> dict:
    """Put the short version next to the index.

    🔑 It lies SEPARATELY, because the page refreshes every 30 s and should not
    have to read a file with tens of thousands of entries for that.

    🔴 But it MUST always be brought up to date whenever the index CAN have
    changed — not only when saving. The page sees this file and nothing else: if
    it lags behind, the page shows a state that no longer exists, and the
    follow-up watch never even starts (measured 2026-09-25 with an artificial
    entry)."""
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
    """One mail into the index. Returns the entry (new or refreshed).

    An existing entry keeps its handover results — a second look at the same mail
    must not forget that its PDF has long been in DocuSort."""
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
    """The three numbers that belong on the page."""
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
    """„show me all mails with a pdf attached from the phone company“.

    The search runs through `normal()` — the same function that already freed the
    classification from „Gerät“/„Geraet“. One word has to occur somewhere: in the
    sender, the name, the subject, the file name or the folder. Several words
    must ALL occur, but not next to each other — „telekom pdf“ and „pdf telekom“
    find the same thing."""
    worte = [normal(w) for w in re.split(r"\s+", frage or "") if w.strip()]
    # Once for the whole search: which file — attached to WHICHEVER mail — is
    # already in DocuSort?
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
        # 🔑 Whether a file can still be handed over is decided by the SERVER —
        # at the same place that also decides the handover itself. The page only
        # draws what it finds here. Previously the same rule lived in the browser
        # as well, and two opinions about it are two chances to upload the same
        # document a second time.
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
                    stand = "kann_docusort_nicht"      # say it, do not offer it
                else:
                    zwilling = zwillinge.get((name.lower(), int(f.get("b") or 0)))
            gezeigt.append(dict(
                f, stand=stand, doc=str(satz.get("doc") or ""),
                text=str(satz.get("text") or ""), zwilling=zwilling,
                gebbar=bool(f.get("art") == "dokument" and ds_verdaulich(name)
                            and ds_offen(satz) and not zwilling and hat_ort)))
        # 🔴 The key MUST come out with it: it is the only handle the page has
        # later for saying „hand this to DocuSort“.
        treffer.append(dict(e, schluessel=schluessel, dateien_gezeigt=gezeigt,
                            gebbar=any(f["gebbar"] for f in gezeigt)))
    treffer.sort(key=lambda e: str(e.get("datum") or ""), reverse=True)
    return {"gesamt": len(treffer), "treffer": treffer[:grenze]}


# ── Handover to DocuSort ──────────────────────────────────────
# 🔑 The route is DocuSort's FRONT DOOR: `POST /upload`, the same one the
# browser uses. The rule set for DocuSort: there is only the upload button
# and nothing else, one central place to tip everything into, DocuSort does the
# rest, no different entry points. A second route (SSH into the VM's inbox
# folder) would have been quicker to build and would have broken exactly that
# rule — and would have needed a key nobody can withdraw again without knowing.
#
# 🔴 What this account CAN do: upload, ask for status — and, because DocuSort
# knows only two roles, also READ the library and the finances. A narrower rank
# of its own („may only put things in“) would be cleaner; that is a change to
# DocuSort and stands as the next step in the note. Until then: the account is a
# user of its own, not an admin, and it can be deactivated in DocuSort with one
# click — the session is then dead in the same moment (DocuSort deletes sessions
# when deactivating).
DS_ZUGANG = "docusort.json"          # 0600: URL + Benutzer + Passwort
DS_SITZUNG = "docusort_sitzung.json"  # 0600: the session marker
DS_MAX_MB = 25.0                     # anything bigger is not uploaded
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
    """🔴 Redirects are NOT followed, and both times that is the whole point:

    The login answers with a 303 and puts the session marker into EXACTLY that
    response — follow it and you get the start page and have lost the marker.

    And an expired session sends the upload to the login page. Follow it and you
    get HTTP 200 back with an HTML form: a „success“ in which nothing was
    uploaded. A 303 is an ANSWER here, not a mishap."""

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
                kopfzeilen = e.headers          # THIS is the success case
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
                # 🔴 0600. The marker IS the access, as long as it is valid.
                save(DS_SITZUNG, {"cookie": self.sitzung,
                                  "zeit": datetime.now().isoformat(timespec="seconds")},
                     0o600)
                return ""
        return "Anmeldung ohne Sitzungsmerkmal — DocuSort hat sie abgelehnt."

    def _mit_sitzung(self, weg, daten=None, typ=""):
        """Try once, log in again if the session has expired, retry once. No
        more — retry endlessly and you lock yourself out."""
        for versuch in (1, 2):
            if not self.sitzung:
                fehl = self.anmelden()
                if fehl:
                    return None, fehl
            try:
                return self._anfrage(weg, daten, typ), ""
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308, 401, 403) and versuch == 1:
                    self.sitzung = ""           # the session is dead
                    continue
                return None, "HTTP %d" % e.code
            except Exception as e:
                return None, str(e)[:140]
        return None, txt("a.ds.sitzung")

    # -- Was Postwache braucht ----------------------------------------------
    def hochladen(self, dateiname: str, inhalt: bytes) -> dict:
        """One file through the front door. Returns the status as it is written
        into the index."""
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
            # 🔴 No JSON means: that was not the upload but a page.
            return {"stand": "fehler", "text": txt("a.ds.kein_ergebnis")}
        if d.get("saved"):
            return {"stand": "uebergeben", "inbox": d["saved"][0].get("inbox_name") or "",
                    "text": ""}
        if d.get("imported"):
            erste = d["imported"][0] or {}
            if erste.get("error"):
                return {"stand": "fehler", "text": str(erste["error"])[:160]}
            return {"stand": "finanzen",
                    "text": txt("a.ds.buchungen", n=(erste.get("rows_inserted") or 0))}
        if d.get("rejected"):
            return {"stand": "kann_docusort_nicht",
                    "text": txt("a.ds.dateityp")}
        return {"stand": "fehler", "text": txt("a.ds.stumm")}

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
    """(connection, reason). If DocuSort is not configured or switched off, that
    is NOT an error — the index is still built completely, and the handover can be
    made up at any time."""
    z = ds_zugang()
    if not (z["url"] and z["benutzer"] and z["passwort"]):
        return None, "nicht eingerichtet"
    if not z["aktiv"]:
        return None, "ausgeschaltet"
    return DocuSort(z), ""


# 🔑 ONE rule about who may go again — and only this one. Everything that has
# a status is through; except what is explicitly repeatable. The button on the
# page, the checkbox and the handover itself all ask the same function. Three
# opinions about it would be three chances to upload the same document a second
# time.
# (Which states are repeatable is written in `ds_offen` — the ONE place.)
# How much a status says — „filed“ beats „in transit“ when the same file hangs
# on several mails.
DS_RANG = {"uebergeben": 1, "doppelt": 2, "finanzen": 3, "pruefen": 3,
           "abgelegt": 4}
# This long DocuSort may say „I do not know that file“ before the handover counts
# as lost. 🔴 Right after an upload, „unknown“ is NORMAL: the file has left the
# inbox, the database entry is not there yet. Turn that into an error at once and
# you report every healthy handover as broken.
DS_VERSCHOLLEN_S = 15 * 60

DS_UEBERSETZT = {"filed": "abgelegt", "review": "pruefen", "duplicate": "doppelt",
                 "failed": "fehler", "queued": "uebergeben",
                 "processing": "uebergeben"}


def ds_offen(satz) -> bool:
    """May this file (still) be given to DocuSort?

    🔴 „Error“ is TWO different things, and the difference decides:
      · the **handover** failed — then nothing is over there, and a second attempt
        is exactly right;
      · DocuSort has the file but failed while processing it (it then names a
        document number) — then it is already there. A second upload only makes a
        duplicate; the retry belongs THERE, on the document.
    The document number is what tells the two apart."""
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
    """Give the documents of ONE mail to DocuSort. Returns how many were really
    uploaded.

    🔴 This has to happen BEFORE the move. After the move the UID in the inbox
    no longer exists, and nobody knows the new one."""
    schon = ds_eintraege(eintrag)
    getan = 0
    for f in (eintrag.get("dateien") or []):
        if f.get("art") != "dokument":
            continue
        name = str(f.get("n") or "")
        alt = schon.get(name) or {}
        if not ds_offen(alt):
            continue                       # already through, not twice
        satz = {"n": name, "stand": "", "inbox": "", "doc": "", "text": "",
                "zeit": datetime.now().isoformat(timespec="seconds")}
        if phishing:
            satz["stand"] = "phishing"
            satz["text"] = txt("w.phishing_nichts")
        elif not ds_verdaulich(name):
            satz["stand"] = "kann_docusort_nicht"
            satz["text"] = txt("a.ds.nur_pdf")
        elif int(f.get("b") or 0) > ds.max_mb * 1024 * 1024:
            satz["stand"] = "zu_gross"
            satz["text"] = txt("a.ds.zu_gross",
                                mb="%.1f" % (int(f.get("b") or 0) / 1048576.0),
                                grenze="%.0f" % ds.max_mb)
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
                satz["text"] = txt("a.ds.kein_anhang")
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
    """What became of the handovers — nothing is ticked off until DocuSort
    CONFIRMS it.

    🔑 „uploaded“ is not „arrived“. DocuSort needs seconds to minutes for text
    recognition and filing; until then the file stands at „in transit“ and is
    asked about on every run (and from the page).

    🔴 And there is an end to it: if DocuSort says „I do not know that file“ for
    longer than `DS_VERSCHOLLEN_S` (15 minutes), the handover counts as **lost**
    and may be repeated. Without that deadline such a file would stand at „in
    transit“ for ever — a state that never ends is not information.

    (The number of minutes is spelled out here: a string with a `%` after it is
    no longer a documentation text but an expression — the function would then
    have no description at all.)
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
                continue               # queued/processing: DocuSort is still working
            d["stand"] = neu
            d["doc"] = str(antw.get("doc_id") or "")
            d["text"] = str(antw.get("category") or "")
            geaendert += 1
    return geaendert


def ds_zwillinge(idx: dict) -> dict:
    """(file name, size) -> the best known DocuSort status of THIS file, no
    matter which mail it hung on.

    🔑 The same invoice often hangs on several mails: once in the topic folder,
    once in the archive, once forwarded. Without this reconciliation the second
    search would show an empty button again — and hand it over a second time.

    🔴 Name AND size are compared. The name alone would be too coarse:
    „Rechnung.pdf“ is what twenty senders call it. The size is that of the
    encoded part from the blueprint — stable for the same file in the same mail
    form. That is strong evidence, not proof; DocuSort itself decides on the
    content (SHA256) and reports „already had that one“."""
    raus = {}
    for e in (idx.get("eintraege") or {}).values():
        groessen = {str(f.get("n") or ""): int(f.get("b") or 0)
                    for f in (e.get("dateien") or [])}
        for d in (e.get("ds") or []):
            if ds_offen(d):
                continue          # what is still open is no evidence
            st = str(d.get("stand") or "")
            name = str(d.get("n") or "")
            k = (name.lower(), groessen.get(name, -1))
            alt = raus.get(k)
            if alt is None or DS_RANG.get(st, 0) > DS_RANG.get(alt["stand"], 0):
                raus[k] = {"stand": st, "doc": str(d.get("doc") or ""),
                           "betreff": str(e.get("betreff") or "")[:90],
                           "datum": str(e.get("datum") or "")}
    return raus

# ── Retroactively: what hangs on post that was filed long ago? ──────────
def anhaenge_nachtragen(pf, idx: dict, frist: int = NACHTRAG_FRIST,
                        nur: str = "") -> dict:
    """Work through folder by folder — with a TIME BUDGET.

    🔑 The back-fill runs in the same one-minute rhythm as everything else and
    must not burst it. So the index remembers per folder up to which UID it has
    read: the next run continues there. After a few runs everything is in, and
    after that it costs nothing.

    🔴 The UID only applies with the same UIDVALIDITY. If the server changes it
    (the folder was recreated), every remembered number is worthless and the
    folder is read from the start. Without that check exactly the mails that
    arrived after a server rebuild are missing — and nobody notices."""
    t0 = time.time()
    ende = t0 + max(5.0, float(frist))
    alle = [o for o in pf.ordner_liste()
            if o.split(pf.trenner)[-1] not in KEIN_NACHTRAG and o not in KEIN_NACHTRAG]
    if nur:
        wunsch = {nur} if isinstance(nur, str) else set(nur)
        alle = [o for o in alle if o in wunsch]
    # INBOX first, then the ones not yet finished — whoever was interrupted
    # last is next in line.
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
            ab = 0                      # number space changed: everything anew
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
    """Is anything pending at all — WITHOUT reading the big index?

    🔴 The watchman runs every minute. Read and rewrite a file with tens of
    thousands of entries on every run and you have built a leak you only notice
    on the disk. The short version answers the question."""
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
    """The document part of a run — ask for status, catch up on the backlog, save
    the index.

    🔴 This DELIBERATELY does not hang off the „there is new post“ branch. A run
    without new mail turns back early, and that is the normal case: on a quiet
    day 5 mails arrive but 1440 runs happen. Hang the back-fill in there and the
    retroactive search stays empty for days — measured 2026-09-25, right on the
    first delivery."""
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
    """Catch up when needed: while a folder is still open, a piece on every run —
    after that only twice a day."""
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
    """netflix.com out of members.netflix.com.

    🔴 This is exactly where the first measurement failed: the folder
    `Shopping.Netflix` holds `info@account.netflix.com`, while the inbox received
    `info@members.netflix.com`. Same company, different subdomain — without this
    step something like that falls through.

    Deliberately kept simple: the last two parts, and for the known two-level
    endings (co.uk, com.au …) the last three. A complete list of public suffixes
    would be more upkeep here than use.
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
    """Record every run — and derive the interval from OBSERVATION.

    🔑 Nowhere could anybody see WHEN and HOW OFTEN the watchman runs. A cron
    entry is an
    INTENTION. What is built here is the reality: the last run timestamps, and
    from them the median gap. Exactly the difference that the homelab tab's
    scheduled-task monitoring hung on the very same day — a configured interval
    proves no execution.

    🔴 AND THE REAL REASON this has to be a funnel: until today the
    shut-down branch rewrote `lauf.json` COMPLETELY and lost the `uid` doing so.
    On the next start `letzte_uid = 0` would have meant a cold start, that is
    „learn and stay silent“. Every mail that arrived during the standstill would
    have been skipped mutely: no alarm, no sorting, no hint. So this ALWAYS
    builds on the previous state; a field can only disappear when somebody
    overwrites it deliberately.
    """
    alt = load(LAUF, {})
    if not isinstance(alt, dict):
        alt = {}
    jetzt = datetime.now()
    hist = [z for z in (alt.get("historie") or []) if isinstance(z, str)]
    hist.append(jetzt.isoformat(timespec="seconds"))
    hist = hist[-LAUF_HISTORIE:]

    # Median, not mean: a single outage or a manual start should not bend the
    # interval.
    abstaende = []
    for a, b in zip(hist, hist[1:]):
        try:
            d = (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
        except ValueError:
            continue
        if 0 < d < 86400:
            abstaende.append(d)
    # 🔴 Two runs make no interval. Right after this was built it said
    # „21 seconds“ there, because both data points were manual starts — the page
    # would have claimed „every 21 sec“. The same lesson as with the cron watch
    # on the same day: a short look says nothing about a rhythm.
    takt = (int(sorted(abstaende)[len(abstaende) // 2])
            if len(abstaende) >= TAKT_MINDEST else 0)

    tag = jetzt.strftime("%Y-%m-%d")
    heute = (int(alt.get("heute") or 0) + 1) if alt.get("tag") == tag else 1

    neu = dict(alt)                      # 🔴 build ON it, do not replace it
    neu.update(felder)
    neu.update({"zeit": jetzt.isoformat(timespec="seconds"),
                "historie": hist, "takt_s": takt, "tag": tag, "heute": heute})
    save(LAUF, neu)
    return neu


def ablage_lernen(pf, melden=None) -> dict:
    """Build the map: who writes into which folder.

    Three levels, from precise to coarse — when matching, the most precise one
    that applies always wins:
      1. the full sender address
      2. the full domain
      3. the main domain

    Along the way the WEAKNESSES of the filing are written down too: senders
    that were put sometimes here and sometimes there, and folders that are
    nearly empty. That is the raw material for the later restructuring — the
    owner's own folders are not perfect either, and sorting them afresh is
    what the agent is for.
    """
    t0 = time.time()
    # 🔴 2026-09-27: TWO private mail addresses stood here in the source — in
    #    two places. The own addresses now come from the credentials themselves
    #    and from `state/regeln_eigen.json`: the same place as the personal rule
    #    catalogue, 0600, on no rollout list.
    eigene = {(pf.zug.get("adresse") or "").lower()}
    try:
        import umbau as _U
        eigene.update(_U.eigen_laden()["eigene_adressen"])
    except Exception:
        pass
    eigene.discard("")
    ordner = [o for o in pf.ordner_liste() if o not in KEIN_LEHRMEISTER]
    je_absender, groessen = {}, {}
    for i, name in enumerate(ordner, 1):
        # 🔴 `melden` is the only way out while this is running. Since the
        # restructuring a learning run takes over 4 minutes (124 folders instead
        # of 37) — without feedback that looks like a hanging service, and the
        # button gets pressed a second time.
        if melden is not None:
            try:
                melden(i, len(ordner), name)
            except Exception:
                pass
        c = pf.absender_im_ordner(name, LERN_JE_ORDNER)
        groessen[name] = sum(c.values())
        for adr, n in c.items():
            # 🔴 His OWN addresses are no good as a rule: over the years he
            # forwarded mail to himself across every topic. In the Gmail archive
            # 235 of 400 samples came from himself.
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
        # 🔑 The name bridge is built HERE and not computed at matching time —
        # it hangs only on the folder names, not on the content, and therefore
        # also applies to FOLDERS WITH NOTHING IN THEM YET. Those were exactly
        # the blind spot: Synology.NAS01 to NAS04 exist but are empty, and could
        # never teach the counting anything.
        "namen": namens_marken(groessen),
        "gelernt": datetime.now().isoformat(timespec="seconds"),
        "dauer": round(time.time() - t0, 1),
        "mails": sum(groessen.values()),
    }
    # The weaknesses — for the later restructuring, not for the sorting.
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
    """Every calendar day from `ab` until today, including the ones without post."""
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
    """The numbers behind the mailbox — from the REAL headers.

    A small statistic in the Postwache: how many mails arrive per day, at
    what time, and who writes most often.

    🔑 `Date:` and `From:` of every mail in all incoming folders are read — not
    what the Postwache has seen since it was set up. Otherwise he would have had
    something to look at only weeks later, and the answer to „how much arrives
    per day“ would be an extrapolation from two days.

    🔴 The cap per folder makes the left edge of the daily course incomplete:
    where it was cut off, the OLDEST mails are missing. So `vollstaendig_ab` is
    supplied as well — the latest start of all capped folders. No daily bar may
    be shown before that, otherwise the curve drops at the edge without less post
    having arrived there.
    """
    t0 = time.time()
    # 🔴 2026-09-27: TWO private mail addresses stood here in the source — in
    #    two places. The own addresses now come from the credentials themselves
    #    and from `state/regeln_eigen.json`: the same place as the personal rule
    #    catalogue, 0600, on no rollout list.
    eigene = {(pf.zug.get("adresse") or "").lower()}
    try:
        import umbau as _U
        eigene.update(_U.eigen_laden()["eigene_adressen"])
    except Exception:
        pass
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
            # 🔴 His own addresses do not count as incoming post — in the Gmail
            # archive every second sample came from himself.
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
            # 🔴 With no display name, `absender_teile` returns the ADDRESS.
            # Carrying that as a „name“ looks like a bug on the page
            # (`service@paypal.de  service@paypal.de`).
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

    # Only the complete part goes into the daily course and the average.
    grenze = (datetime.now() - timedelta(days=STAT_TAGE)).strftime("%Y-%m-%d")
    ab = max(voll_ab, grenze) if voll_ab else grenze
    tage_voll = {t: n for t, n in je_tag.items() if t >= ab}
    # TODAY is not over yet — it would drag every average down.
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
        # 🔴 Fill the gaps. A bar chart that simply leaves out zero days
        # squeezes the time axis and makes a quiet week look like a busy one.
        # Measured: 2026-09-06 was missing entirely.
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
    """At most six hours old — numbers of this kind change slowly."""
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


def pfad_umschreiben(name: str, alt: str, neu: str, trenner: str = ".") -> str:
    """A stored folder name after a move — the folder itself AND everything under
    it. Anything else stays exactly as it was."""
    name = str(name or "")
    if name == alt:
        return neu
    if name.startswith(alt + trenner):
        return neu + name[len(alt):]
    return name


def ordner_abgleichen(vorhanden) -> dict:
    """The learned map against the LIVE list of folders: whatever is gone goes
    out, whatever is new comes in.

    When somebody deletes or creates a folder by hand, the Postwache has to
    LEARN that too.

    🔴 And „by hand" means anywhere — in the Postwache, in Apple Mail, in the
    provider's web page. Which is why this does not hang off a button but off the
    ONLY thing that is always true: the list the server gives. A rule that names a
    folder nobody has any more does not file into the void — the copy FAILS, the
    mail stays in the inbox, and it fails again five minutes later. Nobody sees
    that but the log.

    🔑 A NEW folder is worth just as much: the name bridge hangs only on folder
    NAMES, so an empty folder called „Steuer" can take post from `steuer@…` the
    minute it exists — but only if the map knows it is there.
    """
    da = set(vorhanden or ())
    bericht = {"fort": 0, "neu": 0, "regeln": 0}
    if not da:
        return bericht                     # an empty list is a failed LIST, not an empty mailbox
    karte = load(ABLAGE, None)
    if not isinstance(karte, dict):
        return bericht
    for stufe in ("absender", "domain", "haupt", "v_absender", "v_domain", "v_haupt"):
        topf = karte.get(stufe)
        if not isinstance(topf, dict):
            continue
        raus = {}
        for schl, eintrag in topf.items():
            if isinstance(eintrag, dict) and eintrag.get("ordner") not in da:
                bericht["regeln"] += 1
                continue
            raus[schl] = eintrag
        karte[stufe] = raus
    groessen = karte.get("ordner")
    if isinstance(groessen, dict):
        for name in list(groessen):
            if name not in da:
                groessen.pop(name, None)
                bericht["fort"] += 1
        for name in da:
            if name not in groessen and name not in KEIN_LEHRMEISTER:
                # 🔑 Zero, not absent: the name bridge is built from THIS list, so
                # a folder with nothing in it yet can already be a target.
                groessen[name] = 0
                bericht["neu"] += 1
        karte["ordner"] = groessen
        karte["namen"] = namens_marken(groessen)
    schwach = karte.get("schwaechen")
    if isinstance(schwach, dict):
        for feld in ("fast_leere_ordner", "leere_ordner"):
            if isinstance(schwach.get(feld), list):
                schwach[feld] = [o for o in schwach[feld] if o in da]
    if bericht["fort"] or bericht["neu"] or bericht["regeln"]:
        karte["abgeglichen"] = datetime.now().isoformat(timespec="seconds")
        save(ABLAGE, karte)
        log("Ordner abgeglichen: %d fort, %d neu, %d Regel(n) ohne Ziel"
            % (bericht["fort"], bericht["neu"], bericht["regeln"]))
    return bericht


def ordner_umgezogen(alt: str, neu: str, trenner: str = ".") -> dict:
    """A folder now hangs somewhere else. Carry the watchman's memory over.

    🔴 THIS is the work; renaming at the provider is one command. Everything that
    knows the folder BY NAME has to be told, and each piece for its own reason:

      * the learned filing map (`ablage.json`) decides where post goes. Leave the
        old name in there and the next run files into a folder that no longer
        exists — and the filing CREATES it, so the mail ends up split between the
        moved folder and a fresh empty one beside it.
      * the document index (`anhaenge.json`) reaches for an attachment by folder
        and number. Old name, no attachment.
      * the journal is the WAY BACK for every mail the watchman has moved. A way
        back that names a folder nobody has any more is not a way back.
      * what it kept of each mail (`koepfe.json`) says on the page where that
        mail was filed.

    🔑 Everywhere, names are stored the way the SERVER writes them (see
    `voller_name`) — one spelling, so one rewriting rule for all of them.
    """
    bericht = {"alt": alt, "neu": neu, "ablage": 0, "anhaenge": 0, "journal": 0,
               "koepfe": 0}
    if not alt or not neu or alt == neu:
        return bericht

    def um(name):
        return pfad_umschreiben(name, alt, neu, trenner)

    karte = load(ABLAGE, None)
    if isinstance(karte, dict):
        zahl = 0
        for stufe in ("absender", "domain", "haupt", "v_absender", "v_domain", "v_haupt"):
            for eintrag in (karte.get(stufe) or {}).values():
                if isinstance(eintrag, dict) and um(eintrag.get("ordner")) != eintrag.get("ordner"):
                    eintrag["ordner"] = um(eintrag["ordner"])
                    zahl += 1
        for feld in ("ordner",):          # folder -> how many mails
            if isinstance(karte.get(feld), dict):
                karte[feld] = {um(o): n for o, n in karte[feld].items()}
        if isinstance(karte.get("namen"), dict):    # mark -> folder
            karte["namen"] = {mk: um(o) for mk, o in karte["namen"].items()}
        schwach = karte.get("schwaechen")
        if isinstance(schwach, dict):
            for feld in ("fast_leere_ordner", "leere_ordner"):
                if isinstance(schwach.get(feld), list):
                    schwach[feld] = [um(o) for o in schwach[feld]]
        if zahl or True:
            save(ABLAGE, karte)
        bericht["ablage"] = zahl

    idx = load(ANHAENGE, None)
    if isinstance(idx, dict):
        zahl = 0
        for eintrag in (idx.get("eintraege") or {}).values():
            if isinstance(eintrag, dict) and um(eintrag.get("ordner")) != eintrag.get("ordner"):
                eintrag["ordner"] = um(eintrag["ordner"])
                zahl += 1
        if isinstance(idx.get("stand"), dict):
            idx["stand"] = {um(o): w for o, w in idx["stand"].items()}
        if zahl or idx.get("stand"):
            anhang_index_sichern(idx)
        bericht["anhaenge"] = zahl

    koepfe = load(KOEPFE, None)
    if isinstance(koepfe, list):
        zahl = 0
        for e in koepfe:
            if not isinstance(e, dict):
                continue
            for feld in ("verschoben_nach", "wuerde_nach", "ordner"):
                if e.get(feld) and um(e[feld]) != e[feld]:
                    e[feld] = um(e[feld])
                    zahl += 1
        if zahl:
            save(KOEPFE, koepfe)
        bericht["koepfe"] = zahl

    # 🔴 The journal is a FILE OF LINES, and it is rewritten as a whole — line by
    # line, in order. It is the way back; losing its order would mean handing a
    # mail back to the wrong folder.
    weg = os.path.join(OUT, "journal.jsonl")
    if os.path.isfile(weg):
        zeilen, zahl = [], 0
        try:
            with open(weg, encoding="utf-8") as fh:
                for zeile in fh:
                    zeile = zeile.strip()
                    if not zeile:
                        continue
                    try:
                        e = json.loads(zeile)
                    except ValueError:
                        zeilen.append(zeile)
                        continue
                    for feld in ("von", "nach", "anzeige"):
                        if e.get(feld) and um(e[feld]) != e[feld]:
                            e[feld] = um(e[feld])
                            zahl += 1
                    zeilen.append(json.dumps(e, ensure_ascii=False))
            if zahl:
                tmp = weg + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(zeilen) + "\n")
                os.replace(tmp, weg)
            bericht["journal"] = zahl
        except OSError as e:
            log("Journal nicht umgeschrieben: %s" % str(e)[:100])

    log("Ordner umgezogen: %s -> %s (%s)"
        % (alt, neu, ", ".join("%s %d" % (k, bericht[k])
                               for k in ("ablage", "anhaenge", "journal", "koepfe"))))
    return bericht


def ablage_frisch(pf) -> dict:
    """The map, at most a day old. Learning takes seconds to minutes — that does
    not belong in a run that comes every minute."""
    k = load(ABLAGE, None)
    if isinstance(k, dict) and k.get("gelernt"):
        try:
            alter = (datetime.now()
                     - datetime.fromisoformat(k["gelernt"])).total_seconds()
            if alter < ABLAGE_FRISCH:
                # 🔑 Folders come and go between two learning runs — created or
                # deleted in the Postwache, in another mail program, on the
                # provider's page. ONE `LIST` per run keeps the map honest, and
                # it costs a single command. Learning stays a once-a-day affair;
                # this is only the bookkeeping around it.
                try:
                    abgleich = ordner_abgleichen(pf.ordner_liste())
                    if abgleich["fort"] or abgleich["neu"]:
                        k = load(ABLAGE, k)
                except Exception as e:                  # noqa: BLE001
                    log("Ordnerabgleich nicht moeglich: %s" % str(e)[:140])
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
    """A folder name or a piece of an address, boiled down to its core.

    Runs through `normal()`, so that „Bücher“ and „Buecher“ come out the same,
    and then throws away everything that is not a letter or a digit.
    """
    return re.sub(r"[^a-z0-9]", "", normal(text))


def namens_marken(ordner: dict) -> dict:
    """Folder name -> mark. Only the LAST level counts: `Shopping.Ikea` is the
    Ikea folder, not the Shopping folder.

    Duplicate marks are dropped. If two folders had the same mark, every hit
    would be a coin toss — then the bridge would rather stay silent.
    """
    gezaehlt = {}
    for o in ordner:
        mk = marke(o.rsplit(".", 1)[-1])
        if len(mk) >= NAMENSBRUECKE_MINDEST:
            gezaehlt.setdefault(mk, []).append(o)
    return {mk: liste[0] for mk, liste in gezaehlt.items() if len(liste) == 1}


def adress_marken(adresse: str) -> set:
    """The parts of an address a folder name may be matched against: the WHOLE
    local part and EVERY domain level.

    Whole parts, not substrings — otherwise „Haus“ would find the sender
    `haushalt@...` and „KIA“ the word `kiabi`. Evidence resting on an accidental
    snippet of characters is no evidence.
    """
    lokal, _, dom = (adresse or "").lower().partition("@")
    teile = [marke(lokal)] + [marke(x) for x in dom.split(".")]
    return {t for t in teile if t}


def namens_ziel(karte: dict, adresse: str):
    """Does the folder name itself carry the answer? Returns (folder, reason).

    If TWO folders fit, the bridge stays silent. It does not guess.
    """
    marken = karte.get("namen") or {}
    if not marken:
        return None, ""
    e = adress_marken(adresse)
    treffer = sorted({o for mk, o in marken.items() if mk in e})
    if len(treffer) != 1:
        return None, ""
    return treffer[0], txt("w.ziel.namensordner", ordner=treffer[0])


def ziel_finden(karte: dict, adresse: str):
    """Where does this mail belong — going by the owner's own habit?

    Returns (folder, reason, confidence, may_act). The most precise level wins.
    The coarsest one (main domain) delivers a SUGGESTION but `may_act=False` —
    measured, it costs more mistakes than it brings hits (see LERN_SCHWELLEN).

    The order is measured, not guessed:
      1. sender       — where he really files this address
      2. domain       — the same one level coarser
      3. name bridge  — where the FOLDER NAME names the sender
      4. main domain and the suggestion levels — suggest only, do not act
    """
    adresse = (adresse or "").lower()
    if not adresse or "@" not in adresse:
        return None, txt("w.ziel.keine_adresse"), 0, False
    # Counting comes first: where he really filed things beats any name
    # comparison. Only when the counting is silent does the bridge get a turn.
    # 🔴 The WHOLE sentence belongs in ONE key. Translate „post from X you always
    #    file under“ and „ %s (%d of %d)“ separately and you force every language
    #    into German word order.
    for stufe, wert, schluessel in (
            ("absender", adresse, "w.ziel.immer"),
            ("domain", adresse.split("@")[-1], "w.ziel.immer")):
        e = (karte.get(stufe) or {}).get(wert)
        if not e:
            continue
        sicher = round(100.0 * e["treffer"] / max(e["gesamt"], 1))
        grund = txt(schluessel, wer=wert, ordner=e["ordner"],
                    treffer=e["treffer"], gesamt=e["gesamt"])
        return e["ordner"], grund, sicher, LERN_SCHWELLEN[stufe][2]

    # 🔑 The name bridge. It stands EXACTLY here — measured 2026-09-12: put in
    # front of the counting it costs accuracy (97.1 instead of 97.6 %), as a
    # fallback behind it, it brings 30 extra mails, 24 of them right.
    n_ziel, n_grund = namens_ziel(karte, adresse)
    if n_ziel:
        return n_ziel, n_grund, 90, True

    for stufe, wert, schluessel in (
            ("haupt", haupt_domain(adresse), "w.ziel.meist"),
            ("v_absender", adresse, "w.ziel.meistens"),
            ("v_domain", adresse.split("@")[-1], "w.ziel.meistens"),
            ("v_haupt", haupt_domain(adresse), "w.ziel.meistens")):
        e = (karte.get(stufe) or {}).get(wert)
        if not e:
            continue
        sicher = round(100.0 * e["treffer"] / max(e["gesamt"], 1))
        darf = LERN_SCHWELLEN.get(stufe, (0, 0, False))[2]
        grund = txt(schluessel, wer=wert, ordner=e["ordner"],
                    treffer=e["treffer"], gesamt=e["gesamt"])
        if not darf:
            grund += txt("w.ziel.unsicher")
        return e["ordner"], grund, sicher, darf
    return None, txt("w.ziel.unbekannt"), 0, False


# ── Ein Lauf ──────────────────────────────────────────────────────────────────
def kopf_lesen(msg, text: str) -> dict:
    """Keep from a mail exactly what is needed for classification and display —
    and nothing more. The message body is NOT stored."""
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
    """Long-term memory per sender: how often, which drawer, when last. The
    summary is fed from this — and later the question whether a sender always
    sends the same kind of thing."""
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
    """His own assignment beats everything. It is created on the page with one
    click next to the mail and, since the restructuring, names a FOLDER, not a
    drawer — the drawers only decide about reporting now."""
    return str((einst.get("absender_regeln") or {}).get((adresse or "").lower()) or "")


# ═══ Judgement aid: a language model, when one is configured ════════════════
# The watchman itself stays mute — it classifies by fixed rules and costs
# nothing. A model is only asked about what it cannot decide itself: mails that
# fit no drawer. It suggests a rule; a human arms it.
#
# 🔴 WHAT LEAVES THE HOUSE WHEN A SERVICE IS CONFIGURED: sender, name, subject
# and the reason for the classification. Never the message body, never an
# attachment. Exactly the same selection that already went to the workshop agent.
# Anyone for whom that is too much takes a local model (Ollama) — then nothing
# leaves the machine at all — or leaves the judgement aid off, which is the
# default.
KI_ANBIETER = ("aus", "ollama", "openai", "anthropic", "werkstatt")
KI_ZEIT = 90               # seconds; a local model on weak hardware
                           # braucht laenger als ein Dienst
KI_MAX_FAELLE = 25         # more examples do not make the suggestion better,
                           # only the request more expensive
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


# ── The setup note (5.12.0) ─────────────────────────────────────────────
# 🔴 WHY THIS EXISTS
# With the page lock on, a POST had to become impossible without a session —
# until 5.11.1 anybody who could reach the port could change the mailbox
# credentials. But the one-click Ollama helper runs on ANOTHER machine and has
# no session; it writes the address and the model when it is done.
#
# So it carries a note instead: the settings page asks for one, the helper gets
# it on its command line, and only `ki`, `ki_pruefen` and `ki_suchen` accept it.
# Not `zugang`, not `umbau` — a note that opens everything would be a password
# with a shorter life, not a smaller key.
#
# 🔑 Stored as SHA-256 only. A stolen state file does not yield a usable note.
ZETTEL_FRIST = 4 * 60 * 60        # seconds. Pulling a model takes its time.
ZETTEL_AKTIONEN = ("ki", "ki_pruefen", "ki_suchen")


def _zettel_lesen() -> list:
    d = load(ZETTEL, None)
    jetzt = time.time()
    return [z for z in (d if isinstance(d, list) else [])
            if isinstance(z, dict) and float(z.get("bis") or 0) > jetzt]


def zettel_neu() -> str:
    """A fresh note. The caller gets the plain text exactly once — afterwards
    only its fingerprint is here."""
    wert = secrets.token_urlsafe(24)
    offen = _zettel_lesen()[-4:]          # a handful at most
    offen.append({"fingerabdruck": hashlib.sha256(wert.encode()).hexdigest(),
                  "bis": time.time() + ZETTEL_FRIST})
    save(ZETTEL, offen, 0o600)
    return wert


def zettel_gueltig(wert: str) -> bool:
    """🔑 Compared in constant time — a note is a secret like any other."""
    w = (wert or "").strip()
    if not w:
        return False
    f = hashlib.sha256(w.encode()).hexdigest()
    return any(secrets.compare_digest(f, str(z.get("fingerabdruck") or ""))
               for z in _zettel_lesen())


def werkstatt_da() -> bool:
    """Is there a workshop at all? Without one the provider must not even be
    OFFERED — until 5.12.0 every installation in the world could pick it and
    then read that a directory nobody has ever heard of is missing."""
    p = konfig().get("werkstatt") or ""
    return bool(p) and os.path.isdir(p)


def ki_konfig() -> dict:
    """The configured provider. Unknown names count as `aus` (off) — a mistyped
    setting must not lead to something being asked somewhere."""
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
    """(yes/no, reason). The reason is for the page, not for the log — it should
    say what is MISSING, not that something is broken."""
    k = ki_konfig()
    if k["anbieter"] == "aus":
        return False, txt("ki.grund.aus")
    if k["anbieter"] == "werkstatt":
        pfad = konfig()["werkstatt"]
        if not (pfad and os.path.isdir(pfad)):
            return False, txt("ki.grund.werkstatt")
        return True, ""
    if not k["url"]:
        return False, txt("ki.grund.adresse")
    if k["anbieter"] in ("openai", "anthropic") and not k["schluessel"]:
        return False, txt("ki.grund.schluessel")
    if not k["modell"]:
        return False, txt("ki.grund.modell")
    return True, ""


# ── Find a local model instead of typing in an address ──────────────────
# 🔴 The search runs FROM THE WATCHMAN, never from the browser. The browser runs
# on the machine Ollama sits on — it would report „reachable“ while the watchman
# on its small machine cannot get there at all. The question is not whether YOU
# can reach it, but whether IT can ask.
OLLAMA_PORT = 11434
# Order = preference. A model the watchman cannot use (embeddings) would be the
# worst possible preselection.
OLLAMA_WUNSCH = ("llama3.1:8b", "llama3.2:3b", "qwen2.5:7b-instruct",
                 "qwen2.5:14b-instruct", "mistral:7b", "gemma2:9b")
OLLAMA_UNTAUGLICH = ("embed", "bge-", "minilm", "clip", "rerank", "nomic-")


def ollama_modelle(url: str, zeit: float = 2.0) -> list:
    """The model list of an Ollama at `url` — or an empty list.

    Never raises: a search across several addresses must not end at the first one
    nobody is serving."""
    try:
        req = urllib.request.Request(url.rstrip("/") + "/api/tags")
        with urllib.request.urlopen(req, timeout=zeit) as r:
            daten = json.loads(r.read().decode("utf-8", "replace") or "{}")
    except Exception:
        return []
    namen = [str((m or {}).get("name") or "") for m in (daten.get("models") or [])]
    return [n for n in namen if n]


def ollama_taugliches(modelle: list) -> str:
    """The model the watchman is most likely to get along with."""
    brauchbar = [m for m in modelle
                 if not any(s in m.lower() for s in OLLAMA_UNTAUGLICH)]
    for w in OLLAMA_WUNSCH:
        for m in brauchbar:
            if m == w or m.split(":")[0] == w.split(":")[0]:
                return m
    return brauchbar[0] if brauchbar else ""


def ollama_suchen(zusatz=()) -> list:
    """All addresses at which the watchman finds an Ollama.

    Only addresses that are known anyway are asked: the configured one, the
    machine itself, the container's host — and the machine that has just opened
    the page (`zusatz`). The last one is the most common case: the Postwache runs
    on a small machine, the model on the workstation in front of it. No network
    scan, no walking through address ranges.
    """
    kandidaten = []

    def dazu(u):
        u = (u or "").strip().rstrip("/")
        if u and u not in kandidaten:
            kandidaten.append(u)

    k = ki_konfig()
    if k["anbieter"] == "ollama":
        dazu(k["url"])
    dazu("http://127.0.0.1:%d" % OLLAMA_PORT)
    if os.path.exists("/.dockerenv") or os.environ.get("POSTWACHE_IM_CONTAINER"):
        # 🔑 The box next door. `docker-compose.yml` carries an optional `ollama`
        # service; started with `--profile ki` it is reachable under its service
        # name inside the Docker network. This is the one address that needs
        # nothing published on the host and no address typed by anybody — so it
        # is asked before the host.
        dazu("http://ollama:%d" % OLLAMA_PORT)
        # 🔴 A model on the machine the container sits on. This name does NOT
        # exist on Linux by itself — only Docker Desktop invents it. The shipped
        # compose file maps it with `extra_hosts: host.docker.internal:
        # host-gateway`; without that line this candidate measurably answers
        # „Name or service not known" on a NAS, a Pi and a VPS alike, and the
        # search comes up empty where a model was sitting right there.
        dazu("http://host.docker.internal:%d" % OLLAMA_PORT)
    for a in (zusatz or ()):
        a = str(a or "").strip()
        if not a or a.startswith("127.") or a == "::1":
            continue
        dazu("http://%s:%d" % (("[%s]" % a) if ":" in a else a, OLLAMA_PORT))

    gefunden, faeden, sperre = [], [], threading.Lock()

    def pruefe(u):
        m = ollama_modelle(u)
        if m:
            with sperre:
                gefunden.append({"url": u, "modelle": m})

    # Side by side, not one after another: four unreachable addresses would
    # otherwise be four waits in a row, and the page would stand still that long.
    for u in kandidaten:
        f = threading.Thread(target=pruefe, args=(u,), daemon=True)
        f.start()
        faeden.append(f)
    for f in faeden:
        f.join(timeout=3.0)
    gefunden.sort(key=lambda e: kandidaten.index(e["url"]))
    return gefunden


def _ki_http(url: str, kopf: dict, rumpf: dict) -> dict:
    roh = json.dumps(rumpf).encode()
    req = urllib.request.Request(url, data=roh,
                                 headers=dict(kopf, **{"Content-Type": "application/json"}))
    with urllib.request.urlopen(req, timeout=KI_ZEIT) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "{}")


def ki_fragen(system: str, frage: str) -> str:
    """One question to the configured model, one answer as text.

    Two protocols are enough for all four cases: Ollama and OpenAI speak the same
    one (`/v1/chat/completions`), Anthropic speaks `/v1/messages`. The workshop
    takes a different route and does not come through here.
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
    """Models like to put their JSON into a code block or write a sentence in
    front of it. So the outermost brace is searched for, not the whole answer."""
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
    """Turn unclear mails into rule suggestions. Returns a (possibly empty) list
    and never raises — a judgement aid must not cost the mail run."""
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
    system += " " + txt("w.ki.antwortsprache", sprache=SPRACHNAMEN.get(sprache(), "Deutsch"))
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
        # 🔴 A drawer that does not exist is discarded, not guessed at. A
        # model invents categories if you let it.
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
    """Suggestions end up on the page, not in the settings.

    🔑 The watchman NEVER arms anything itself. A model that is wrong would
    otherwise clear post away to a place nobody looks — and the mistake only
    shows up when something is missing.
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
    """Put the unclear cases in front of the agent. Only subject, sender and the
    reason — the message body is stored nowhere and must not surface here
    either."""
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


def escalate(title: str, body: str, art: str = "weckruf") -> int:
    """Create a task in the workshop — the only route on which this program spends
    tokens. If the workshop container fails, only the wake-up call fails here,
    not the watchman."""
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
            # 🔑 Into our own books at once. A task only the workshop knows
            #    about is nobody's task once the workshop has an outage.
            auftrag_merken(nr, title, art)
            return nr
        log("Weckruf FEHLGESCHLAGEN (%s): %s" % (r.returncode, (r.stderr or "")[:200]))
    except Exception as e:
        log("Weckruf FEHLGESCHLAGEN: %s" % str(e)[:200])
    return 0


# ── What became of a wake-up call (4.5.0) ────────────────────────────
# What has to be fixed: the workshop always has heaps of tasks sitting in it
# with the agent stuck in the queue and nothing happening — while the point is
# to follow 100 % of what goes on in the Postwache.
#
# 🔴 MEASURED on 2026-09-27: TEN tasks of the Postwache had been sitting at
#    `queued`, `attempts=0`, since 2026-09-11, WITHOUT an error. The cause lay
#    outside: the working tree for the agent had never been set up in the task
#    system, and for 16 days the broker wrote „waiting for provisioning“ into a
#    log nobody reads. The Postwache reported „wake-up call: task #N“ every time
#    and took that for success.
#
# 🔑 THE SAME LESSON AS WITH THE UPLOAD: nothing is ticked off until the target
#    system CONFIRMS, and a confirmation needs a DEADLINE. Creating a task is not
#    a result — it is a claim until somebody has started it.
AUFTRAEGE = "auftraege.json"        # in out/: what the Postwache has created
AUFTRAG_FRIST = 2 * 3600            # danach gilt „keiner arbeitet daran"
MAX_OFFENE_AUFTRAEGE = 3            # this many untouched, then no new one


def auftraege_lesen() -> list:
    v = load_out(AUFTRAEGE)
    return v if isinstance(v, list) else []


def load_out(name: str):
    try:
        with open(os.path.join(OUT, name), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def save_out(name: str, daten) -> None:
    os.makedirs(OUT, exist_ok=True)
    ziel = os.path.join(OUT, name)
    tmp = ziel + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(daten, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, ziel)
    except OSError:
        pass


def auftrag_merken(nr: int, titel: str, art: str) -> None:
    """Enter a created task into our own books.

    🔑 The Postwache keeps a list of its OWN. Relying on the workshop alone would
       mean: when it fails, nobody knows any more that something was open.
    """
    if not nr:
        return
    liste = [a for a in auftraege_lesen() if int(a.get("nr") or 0) != int(nr)]
    liste.append({"nr": int(nr), "titel": str(titel)[:120], "art": str(art),
                  "angelegt": datetime.now().isoformat(timespec="seconds"),
                  "zustand": "angelegt", "gestartet": "", "gemeldet": False})
    save_out(AUFTRAEGE, liste[-50:])


def _werkstatt_zustaende() -> dict:
    """{task number: state} — ASKED of the workshop, not guessed.

    If the container fails, an empty dictionary comes back; the last known state
    then stands. 🔴 An empty result must not be read as „all done“.
    """
    # 🔴 As a READABLE script, not as a glued-together one-liner. A one-liner
    #    with embedded newlines is exactly the place where one quote too many
    #    goes unnoticed — and the return value would then simply be empty, that
    #    is: „all done“.
    code = "\n".join([
        "import json, sys",
        "sys.path.insert(0, '/app')",
        "import config, local_engine as L",
        "p = [x for x in config.PROJECTS if x['id'] == 'postwache'][0]",
        "c = L._db()",
        "raus = {}",
        "for r in c.execute('SELECT th.number, t.status, t.started_at FROM tasks t '",
        "                   'JOIN threads th ON th.id = t.thread_id WHERE t.repo = ?',",
        "                   (p['repo'],)):",
        "    raus[str(r[0])] = {'zustand': r[1], 'gestartet': r[2] or ''}",
        "print(json.dumps(raus))",
    ])
    try:
        r = subprocess.run(["docker", "exec", "-i", "werkstatt", "python", "-c", code],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and r.stdout.strip().startswith("{"):
            return json.loads(r.stdout.strip())
    except Exception as e:
        log("Auftragszustand nicht abfragbar: %s" % str(e)[:120])
    return {}


WERKSTATT_WEG_STAND = "werkstatt_weg.json"


def _werkstatt_weg_melden(liste: list) -> None:
    """Say ONCE that unfinished tasks can no longer be asked about.

    🔑 Silence would be wrong and a line per minute would be worse. The workshop
    being absent is a fact of the installation, not a failure — but tasks that
    are still open freeze at their last known state, and that is worth knowing
    exactly once. Same shape as `arbeitsplatz_pruefen()`: report when the finding
    CHANGES, and report again when it is resolved.
    """
    offen = [a for a in (liste or [])
             if a.get("zustand") not in ("done", "cancelled")]
    alt = load_out(WERKSTATT_WEG_STAND) or {}
    vorher = int(alt.get("offen") or 0)
    if offen and not vorher:
        log("Werkstatt nicht erreichbar: %d Auftrag/Auftraege bleiben auf ihrem "
            "letzten bekannten Zustand stehen." % len(offen))
        save_out(WERKSTATT_WEG_STAND, {"offen": len(offen), "gemeldet": time.time()})
    elif not offen and vorher:
        save_out(WERKSTATT_WEG_STAND, {"offen": 0, "gemeldet": time.time()})


def auftraege_pruefen() -> dict:
    """What became of the wake-up calls? Returns a summary.

    Called on every run (one query, no network load) and reports ONCE per task
    when the deadline is missed — not every minute.
    """
    liste = auftraege_lesen()
    # 🔴 „A PATH IS CONFIGURED" IS NOT „THE WORKSHOP IS THERE".
    # This guard asked `konfig().get("werkstatt")` — whether a STRING is set.
    # Move an installation into a container and the old path is still in the
    # state while the directory is gone and so is `docker`, so
    # `_werkstatt_zustaende()` ran into its `except` on EVERY tick and wrote
    # „Auftragszustand nicht abfragbar: No such file or directory: 'docker'".
    # At a 60 s tick that is 1440 lines a day, in exactly the log where one looks
    # for real findings. `werkstatt_da()` asks the right question.
    if not liste or not werkstatt_da():
        _werkstatt_weg_melden(liste)
        return {"offen": 0, "haengen": 0, "gesamt": len(liste)}
    staende = _werkstatt_zustaende()
    jetzt = datetime.now()
    offen = haengen = 0
    neu_gemeldet = []
    for a in liste:
        nr = str(int(a.get("nr") or 0))
        st = staende.get(nr)
        if st:
            a["zustand"] = st.get("zustand") or a.get("zustand") or "?"
            a["gestartet"] = st.get("gestartet") or ""
        if a.get("zustand") in ("done", "cancelled"):
            continue
        offen += 1
        try:
            alter = (jetzt - datetime.fromisoformat(a["angelegt"])).total_seconds()
        except Exception:
            alter = 0
        # 🔴 „running“ is not stuck. Only what has NEVER started after the
        #    deadline is reported.
        if alter > AUFTRAG_FRIST and not a.get("gestartet"):
            haengen += 1
            if not a.get("gemeldet"):
                a["gemeldet"] = True
                neu_gemeldet.append((a, alter))
    save_out(AUFTRAEGE, liste)
    for a, alter in neu_gemeldet:
        chronik("auftrag_haengt",
                titel=txt("w.auftrag.titel", nr=a["nr"]),
                detail=txt("w.auftrag.detail", stunden=int(alter // 3600),
                           titel=a.get("titel") or "?"))
        log("Auftrag #%s liegt seit %d h unbearbeitet: %s"
            % (a["nr"], alter // 3600, (a.get("titel") or "")[:80]))
    return {"offen": offen, "haengen": haengen, "gesamt": len(liste)}


# --- The agent's workplace ----------------------------------------------------
# A workshop agent only judges well with two paths: the VAULT MIRROR
# (`vault-mirror`, what the owner knows, read-only) and the DROP FOLDER
# (`vault-inbox`, from which a collector carries its notes all the way into
# Obsidian). The workshop provisioner creates both. Build a workplace BY HAND and
# you forget them — which is exactly what happened to the Postwache on
# 2026-09-27: the agent classified 199 senders from domain and subject alone and
# wrote so in its note: „the vault mirror was not reachable“.
#
# 🔑 That report stood in a note which, in the same moment, went nowhere: the
#    drop folder was missing too. A workplace cannot report its own defect. So
#    the watchman checks it from ITS side — it sees the same paths, and its way
#    of reporting does not hang on what is missing.
ARBEITSPLATZ = ("vault-mirror", "vault-inbox")
ARBEITSPLATZ_STAND = "arbeitsplatz.json"
ARBEITSPLATZ_FRIST = 12 * 3600


def arbeitsplatz_pruefen() -> list:
    """Is a path missing for the agent? Returns the missing names.

    A report goes out when the finding CHANGES, after that at most every
    ARBEITSPLATZ_FRIST seconds — and once when it has been fixed. Without a
    workshop path there is no workplace to check.
    """
    pfad = str(konfig().get("werkstatt") or "").strip()
    if not pfad:
        return []
    heim = os.path.dirname(pfad.rstrip("/"))
    # 🔴 The watchman may not READ the agent's home, only traverse it (execute
    #    permission via ACL). `isdir` on a NAMED path works with that, `listdir`
    #    does not — so each path is asked for on its own.
    if not heim or not os.path.isdir(heim):
        return []
    fehlt = [n for n in ARBEITSPLATZ if not os.path.isdir(os.path.join(heim, n))]
    alt = load_out(ARBEITSPLATZ_STAND) or {}
    vorher = [str(n) for n in (alt.get("fehlt") or [])]
    try:
        seit = time.time() - float(alt.get("gemeldet") or 0)
    except (TypeError, ValueError):
        seit = ARBEITSPLATZ_FRIST + 1
    if fehlt and (fehlt != vorher or seit > ARBEITSPLATZ_FRIST):
        chronik("arbeitsplatz", titel=txt("w.arbeitsplatz.titel"),
                detail=txt("w.arbeitsplatz.detail",
                           wege=", ".join(fehlt), heim=heim))
        log("Arbeitsplatz des Agenten unvollstaendig: %s fehlt in %s"
            % (", ".join(fehlt), heim))
        save_out(ARBEITSPLATZ_STAND, {"fehlt": fehlt, "gemeldet": time.time()})
    elif not fehlt and vorher:
        chronik("arbeitsplatz_ok", titel=txt("w.arbeitsplatz.ok.titel"),
                detail=txt("w.arbeitsplatz.ok.detail", wege=", ".join(vorher)))
        save_out(ARBEITSPLATZ_STAND, {"fehlt": [], "gemeldet": time.time()})
    return fehlt


def auftrag_stau() -> int:
    """How many tasks are untouched? From MAX_OFFENE_AUFTRAEGE on, no new one.

    🔑 This is the answer to the heap: it helps nobody to add an eleventh task
       to a pile nobody is touching. The FINDING still goes into the history —
       that must never be lost.
    """
    nicht_fertig = [a for a in auftraege_lesen()
                    if a.get("zustand") not in ("done", "cancelled")]
    return len(nicht_fertig)


def melde_sofort(treffer: list, einst: dict) -> None:
    """The alarm classes straight to Telegram — bundled into ONE message. Nobody
    reads ten separate alerts within a minute."""
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
    """The daily overview. Short enough that it actually gets read."""
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
    # The three loudest senders of the day: that is the information a rule is
    # made of.
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


def nur_einmal():
    """Prevents two runs from working on the same mailbox at once.

    🔴 Became necessary through the restructuring (4.0.0): learning the filing
    map used to read 3,376 mails from 37 folders (8 s). Since the 12,396 archive
    mails sit in NORMAL folders it is 17,900 from 105 folders — about 10 minutes.
    But cron starts every minute. Without a lock ten processes would then run on
    the same mailbox at once, and two of them could copy the same mail before the
    other ticks it off.

    Returns the open file (which must stay alive, otherwise the lock falls) or
    `None` when somebody is already working.
    """
    import fcntl
    try:
        fh = open(os.path.join(OUT, "postwache.lock"), "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        return None
    except Exception:
        # Where `flock` is unavailable, a missing lock is better than a watchman
        # that no longer runs at all.
        return True


def main() -> int:
    t0 = time.time()
    os.makedirs(STATE, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)
    # 🔴 `sperre` looks unused and is not: the OPEN FILE IS the lock. Tidy the
    #    variable away and you release it immediately.
    sperre = nur_einmal()
    if sperre is None:
        # No log entry: at a one-minute rhythm that would be noise. The next run
        # in 60 s will still find the same post.
        return 0
    log_kappen()
    tok = token()

    grund = stopped(tok)
    if grund:
        vorher = load(LAUF, {})
        if vorher.get("grund") != grund:
            log("stillgelegt: %s" % grund)
            chronik("stillgelegt", titel=txt("w.still.titel"), detail=grund)
        # 🔴 lauf_buchen instead of save: the shut-down branch used to write the
        # `uid` away with everything else — the next start would have been a cold
        # start and would have skipped mutely everything that arrived during the
        # standstill.
        lauf_buchen({"grund": grund})
        status_schreiben({"aktiv": False, "grund": grund})
        melde_sammlung()
        return 0

    einst = einstellungen()
    faecher = [f for f in postfaecher() if f["an"]]
    if not faecher:
        status_schreiben({"aktiv": True, "eingerichtet": False,
                          "grund": txt("a.kein_postfach")})
        # No wake-up call, no alert: this is not a failure but the state before
        # anything has been set up.
        return 0

    # 🔑 One run PER mailbox, one after another. One after another and not in
    # parallel, because every run uses the same history, the same journal and the
    # same DocuSort session — three threads on that would be three chances for a
    # half-written file, and the gain would be one second.
    # 🔴 A mailbox that does not answer must not hold up the others: every run
    # stands on its own, its failure stays its own.
    # ── Permanent redirection (changing provider) ───────────────────────
    # Or set up a permanent redirection of the mail through
    # the Postwache, so that provider A keeps no mail but everything arrives at
    # provider B and is filed there.
    #
    # 🔑 BEFORE the loop, not inside it. The redirection collects the post at A
    #    and puts it into the inbox at B; it is SORTED by the run for B that comes
    #    right after. If the redirection ran inside A's run, B might already have
    #    had its run this minute depending on the order — and then the post would
    #    lie unsorted in the inbox for a minute. Here it reaches its place in the
    #    SAME pass.
    #
    # 🔴 Only in armed operation. During a learning run the Postwache changes
    #    nothing at all — and a redirection appends at B and deletes at A. „does
    #    nothing“ and „can do nothing“ are two different things.
    if einst.get("scharf"):
        try:
            import umzug as UZ
            um = UZ.umleitung_lauf()
            if um.get("bestaetigt") or um.get("geloescht"):
                chronik("umleitung", titel=txt("w.umleitung.titel"),
                        detail=txt("w.umleitung.detail",
                                   n=int(um.get("bestaetigt") or 0),
                                   weg=int(um.get("geloescht") or 0),
                                   von=um.get("von") or "?",
                                   nach=um.get("nach") or "?"))
            if um.get("fehler"):
                log("Umleitung: %s" % um["fehler"])
        except Exception as e:
            # A migration must not cost the mail run — the same separate bolt
            # as with the statistics.
            log("Umleitung uebersprungen: %s" % str(e)[:160])

    # 🔑 What became of the wake-up calls? ONE query per run. Without it nobody
    #    would have noticed that ten tasks had been sitting there for 16 days.
    try:
        auftraege_pruefen()
    except Exception as e:
        log("Auftragspruefung uebersprungen: %s" % str(e)[:140])

    # 🔑 And does the agent even have its two paths? Two `isdir` questions.
    #    Without them it keeps judging blindly and its report about it vanishes.
    try:
        arbeitsplatz_pruefen()
    except Exception as e:
        log("Arbeitsplatzpruefung uebersprungen: %s" % str(e)[:140])

    # 🔑 Has the workshop answered domain categories meanwhile? That is a file
    #    check and costs nothing — but without it a plan would have to be started
    #    by hand for the agent's answer to arrive at all.
    try:
        import umbau as UB
        UB.werkstatt_antwort_holen()
    except Exception as e:
        log("Werkstatt-Antwort uebersprungen: %s" % str(e)[:140])

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
    """The top level of `status.json` is the SUM over all mailboxes.

    The page should be able to say „3 new, 1 moved“ at a glance without adding up
    itself — and the house-switch sensor gets the same sum. What applies per
    mailbox stands below it in `postfaecher`.
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
    """One complete mail run for EXACTLY ONE mailbox.

    Everything this run reads and writes as state lies, thanks to `pf_waehlen()`,
    under `state/pf/<id>/` — the code below notices nothing of it. What stays
    global: history, journal, log, settings and the credentials for Telegram and
    DocuSort.

    Returns what the overall run needs for the sum; it does NOT report or write
    the sensor itself.
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

    # During a learning run the mailbox is opened READONLY — then not even a
    # programming mistake can change anything. That is the difference between
    # „does nothing“ and „can do nothing“.
    schreiben = bool(einst.get("scharf"))
    treffer, verschoben, unklar = [], 0, 0
    try:
        with Postfach(zug, schreiben) as pf:
            if kaltstart:
                uids = pf.letzte_uids(KALTSTART_MAILS)
            else:
                uids = pf.neue_uids(letzte_uid)[:MAX_PRO_LAUF]
            if not uids:
                # 🔴 Also WITHOUT new post: otherwise nothing is caught up on a
                # quiet day (see `dokumente_pflegen`).
                dokumente_pflegen(pf)
                lauf_buchen({"uid": letzte_uid, "grund": "", "fehler": "",
                             "dauer": round(time.time() - t0, 1)})
                status_schreiben({"aktiv": True, "eingerichtet": True,
                                  "scharf": schreiben, "neu": 0,
                                  "uid": letzte_uid})
                tagesbericht(zaehler, prof, einst)
                return {"rc": 0, "neu": 0}

            # 🔑 TWO SEPARATE QUESTIONS, and that is the whole rebuild of
            # 2026-09-11:
            #   1. Does he need to know it AT ONCE?  -> `einordnen()`, by content
            #   2. Where does it belong?             -> his own filing
            # Before that, the classification decided both. That was wrong: a
            # PayPal invoice belongs in Shopping.Paypal (as he has done it for
            # years) AND should still report at once. Kept apart, both work.
            karte = ablage_frisch(pf)
            # 🔴 The statistics are decoration. If they fail, that must NOT cost
            # the mail run — hence a bolt of their own. For the filing map that
            # would be wrong: without it nothing can be sorted at all.
            try:
                statistik_frisch(pf)
            except Exception as e:
                log("Statistik uebersprungen: %s" % str(e)[:120])

            # ── Documents in the post ───────────────────────────────
            # The blueprint of ALL new mails in one go — one fetch instead of 120,
            # and not a single byte of attachment.
            idx = anhang_index()
            strukturen = pf.strukturen(uids)
            ds, ds_grund = ds_bereit()
            # 🔴 During a cold start NOTHING is handed over. It looks at 300 old
            # mails to learn the situation — it would flood DocuSort with post
            # years old, and do so once and irreversibly.
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

                # Where to? His own assignment beats the learned one.
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

                # ── What is attached? ─────────────────────────────
                # 🔴 At THIS point, not further down: after the move the UID in
                # the inbox no longer exists, and nobody knows the new one. The
                # index is ALWAYS built, the handover only when DocuSort is
                # configured and switched on.
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

                # 🚨 THE BOLT, at the ONE place every move has to pass. It no
                # longer rests on a list of categories but on something stronger:
                # the watchman may put a mail ONLY where he has already put post
                # from this sender himself. It invents no target and never creates
                # a folder. Where it has learned nothing, the mail stays put.
                darf = (bool(ziel) and darf_stufe
                        and ziel in (karte.get("ordner") or {}))
                if eigen:
                    darf = bool(ziel)          # a rule of his own always applies
                # Phishing-Verdacht bleibt IMMER liegen, egal was gelernt wurde.
                if urteil.get("phishing"):
                    darf = False
                    eintrag["ziel_grund"] = "Phishing-Verdacht — bleibt liegen"
                # ── File it itself when the learned map knows nothing ───────
                # 🔑 ONE place, after the bolt and BEFORE „important stays put“:
                #    that way the phishing lock and his switch still apply.
                # 🔴 The import stands HERE, not at the top: `umbau` imports
                #    `postwache` — an import in the header would be a cycle.
                neuer_ordner = False
                if (not darf and not urteil.get("phishing")
                        and einst.get("selbst_sortieren")):
                    try:
                        import umbau as U
                        z2, g2, abgeleitet = U.ziel_fuer_neue({
                            "von": kopf["adresse"], "betreff": kopf["betreff"],
                            "an": kopf.get("an") or "",
                            # 🔴 `einordnen()` returns no „liste“ field — the
                            # circular feature sits in the CLASS. Write
                            # `urteil.get("liste")` here and you build dead
                            # code: always False, the newsletter fallback never
                            # active.
                            "liste": klasse in ("newsletter", "werbung")})
                        if z2:
                            ziel, warum = z2, g2
                            darf, neuer_ordner = True, abgeleitet
                            eintrag["ziel"], eintrag["ziel_grund"] = ziel, warum
                            eintrag["ziel_darf"] = True
                    except Exception as e:
                        log("Selbst einsortieren nicht moeglich: %s" % str(e)[:140])

                # On request: leave important mail in the inbox anyway.
                if darf and einst.get("wichtiges_bleibt") and klasse in ALARM:
                    darf = False
                    eintrag["ziel_grund"] = (
                        "%s — bleibt liegen, weil du „Wichtiges bleibt im "
                        "Posteingang\u201c eingeschaltet hast" % warum)

                if kaltstart:
                    pass                       # learn and stay silent
                elif darf and schreiben:
                    # 🔴 NOT `ordner_sicherstellen()`: that puts „INBOX.“ in
                    #    front. This server's namespace is the ROOT ("" with
                    #    "."), and an INBOX prefix would create a second tree.
                    #    `umbau` creates at the root and then checks that the
                    #    folder really appears under that name in LIST.
                    if neuer_ordner and ziel not in pf.ordner_liste():
                        try:
                            import umbau as U
                            log("neuer Ordner: %s" % U.ordner_anlegen(pf, ziel))
                        except Exception as e:
                            log("Ordner %s nicht anlegbar: %s" % (ziel, str(e)[:140]))
                            darf = False
                    voll = pf.voller_name(ziel) if not neuer_ordner else ziel
                    if darf and pf.verschieben(uid, voll):
                        eintrag["verschoben_nach"] = ziel
                        verschoben += 1
                        if ae is not None:
                            # The mail is somewhere else now and has a
                            # DIFFERENT UID there. Until the back-fill has seen
                            # it there, the old number is worthless — 0 says
                            # honestly „I do not know right now“.
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

            # Find mails that were just moved again at their new place, so the
            # index does not sit there saying „I do not know“ until the next
            # back-fill.
            if beruehrte_ordner:
                try:
                    anhaenge_nachtragen(pf, idx, frist=10, nur=beruehrte_ordner)
                except Exception as e:
                    log("Nachtrag der Zielordner: %s" % str(e)[:120])
            # Status of the handovers + backlog — the same route as in a run
            # without new post, so that only ONE place does this.
            dokumente_pflegen(pf, idx, ds, ds_bekannt=True)
            anhang_index_sichern(idx)
    except imaplib.IMAP4.error as e:
        # A wrong password looks exactly like an outage. Both are reported, but
        # only ONCE — otherwise the watchman radios every minute.
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
        # The cold start must not fill the daily counter — otherwise the first
        # daily report announces 300 mails that are all old.
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

    # A wake-up call only where judgement is really needed: too many mails the
    # watchman cannot classify. Everything else it can do itself.
    offen_unklar = [e for e in koepfe[-400:] if e["klasse"] == "unklar"]
    if (len(offen_unklar) >= UNKLAR_SCHWELLE
            and int(zaehler.get("weckrufe") or 0) < MAX_WECKRUFE_PRO_TAG
            and not lauf.get("unklar_gemeldet_am") == heute):
        beispiele = "\n".join(
            "- %s — von %s (%s)" % (e["kopf"]["betreff"][:120],
                                    e["kopf"]["adresse"], e["grund"])
            for e in offen_unklar[-15:])
        # 🔑 One route per configured judgement aid. The workshop route puts the
        # cases down and wakes an agent; every other provider is asked directly
        # and delivers suggestions onto the page. If nothing is configured, the
        # history is all there is — the finding is never lost.
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
            stau = auftrag_stau()
            if stau >= MAX_OFFENE_AUFTRAEGE:
                # 🔴 No eleventh task onto a pile nobody touches. The FINDING
                #    still goes into the history — it must never be lost, only
                #    the wake-up call is dropped.
                log("Weckruf unterdrueckt: %d Auftraege liegen unbearbeitet" % stau)
                chronik("auftrag_stau", titel=txt("w.stau.titel", n=stau),
                        detail=txt("w.stau.detail", n=stau))
                uebergeben(offen_unklar)
            else:
                uebergeben(offen_unklar)
                nr = escalate(
                    "Postwache: %d Mails passen in keine Schublade" % len(offen_unklar),
                    "Der Waechter ordnet nach festen Regeln ein (siehe `einordnen()` in "
                    "`postwache.py`). Diese Mails fielen durch:\n\n"
                    + beispiele +
                    "\n\nBitte pruefen: laesst sich daraus eine REGEL ableiten (Absender, "
                    "Kopfzeile, Wortmuster), oder ist das wirklich Einzelfall-Post? "
                    "Vorschlaege als Notiz nach den vereinbarten Ablageordner. Regeln NICHT selbst "
                    "scharfschalten — der Mensch entscheidet auf der Seite.",
                    art="unklar")
        # 🔴 The wake-up call MUST NOT be the only output. If the workshop
        # container fails or does not know the project, this would be a path that
        # always fails silently — exactly the mistake that swallowed 506 measures
        # unnoticed in the das Schwesterprojekt. So the finding is ALWAYS written into the
        # history (and thereby reported), and the wake-up call is only the
        # flourish.
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
    """`out/status.json` — what the page reads.

    While a mailbox is running, its state lands under `postfaecher.<id>`; the top
    level is the sum and is set at the end by `gesamt_schreiben()`. That keeps
    each of the roughly ten existing call sites valid without any of them having
    to know that there are several mailboxes.
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
    """Once a day — the only scheduled report. It goes out even when nothing
    happened: „no new post“ is a statement one should be able to read without
    having to go and look."""
    heute = datetime.now().strftime("%Y-%m-%d")
    # 🔴 The marker lies GLOBALLY. If it lay in the mailbox's counter, three
    # mailboxes would produce three daily reports — each with a third of the
    # truth.
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
    """What went into the history this round goes out as well — but only the
    events that mean something. Pure counters stay on the page.

    🔴 While a mailbox is being processed, nothing happens here. Otherwise three
    mailboxes would produce three messages instead of one, and the third would
    have lost the context of the first."""
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
            continue                      # that is already in the daily report
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
