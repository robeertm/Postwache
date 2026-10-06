#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Postwache — the mail client behind the watchman (since 5.0.0).

The brief: turn the Postwache into a real mail client — preview and all, and
everything adjustable.

This module is the engine: it reads folders, lists and mails, hands out
attachments, sets flags, moves, deletes, writes and sends. The page
(`post_klient.html`) only draws what comes from here.

🔴 THE LOCK IS PART OF THE FEATURE, not an extra. The overview page has always
shown subject and sender only — and the reason was written down: „not enough to
spread the inbox across a web page WITHOUT A LOGIN". A client shows the body, so
the client brings the login. Without an access word set, this module hands out
nothing but the fact that no word is set.

🔴 Everything that reads uses BODY.PEEK, exactly as in the watchman. Whether a
mail counts as read is decided by the reader, never by the act of displaying it —
and „mark as read" is a setting with an „only by hand" position.

🔴 Nothing is cached on disk. Headers live in memory for as long as a list is
shown, bodies only for the duration of one response. The principle from the
watchman („the body is stored nowhere") survives the client.
"""
from __future__ import annotations

import base64
import email
import email.policy
import email.utils
import hashlib
import hmac
import html as _html
import imaplib
import json
import mimetypes
import os
import re
import secrets
import select
import smtplib
import socket
import string
import sys
import threading
import time
import urllib.parse
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone

HOME = os.path.expanduser("~")
BASE = os.path.abspath(os.environ.get("POSTWACHE_HOME")
                       or os.path.join(HOME, "scripts", "postwache"))
sys.path.insert(0, BASE)
try:
    import postwache as W        # ONE source for drawers, rules and IMAP parsing
except Exception:                # the client must not take the page down with it
    W = None

KLIENT_STAND = "klient.json"        # access word + lock, 0600
KLIENT_EINST = "klient_einst.json"  # view settings, harmless
KEKS = "pw_sitzung"                 # name of the session cookie
RUNDEN = 240000                     # PBKDF2 rounds; ~0.15 s on the Pi
SITZUNG_MIN = 720                   # session lifetime in minutes, adjustable
SPERRZEIT = (30, 60, 120, 300, 900)  # seconds after 5, 6, 7, 8, 9+ failures
VERSUCHE_FREI = 4                   # up to this many failures without waiting
LEERLAUF = 300                      # close an unused IMAP connection after 5 min
# ── The listening post ───────────────────────────────────────────────────
# 🔑 A mail program that ASKS every minute is a minute late. IMAP has IDLE for
# exactly this: the server speaks up on its own when something happens. So the
# page no longer polls — it leaves ONE request waiting, and that request comes
# back the moment the provider says a word.
HORCH_FRIST = 90.0                  # seconds without a waiting reader, then it closes
HORCH_WARTE = 25.0                  # how long one waiting request is held at most
HORCH_FENSTER = 300.0               # one IDLE lasts this long, then it is renewed
HORCH_TAKT = 20.0                   # a server without IDLE gets asked this often
HORCH_ORDNER = 6                    # how many folders one reader may have watched
MAX_ANHANG = 25 * 1024 * 1024       # per mail, adjustable up to this hard ceiling
TEXT_GRENZE = 900 * 1024            # never render more body than this
AUSZUG_BYTES = 900                  # how much of a body a list excerpt costs
AUSZUG_ZEICHEN = 180                # and how much of it a row shows
NEU_GRENZE = 200                    # rows in the „New" overview at most
NEU_KOEPFE = 500                    # and header records fetched for it at most

# Special folders are recognised by their FLAG, not by their name — a mailbox in
# French calls its bin „Corbeille", and a name-based guess misses every mailbox
# that is not German. Same lesson as in the migration (4.4.0).
SONDER_FLAGGEN = {
    "\\inbox": "posteingang", "\\sent": "gesendet", "\\drafts": "entwuerfe",
    "\\trash": "papierkorb", "\\junk": "spam", "\\archive": "archiv",
    "\\all": "alle", "\\flagged": "markiert", "\\important": "wichtig",
}
# Fallback for servers without SPECIAL-USE: the usual names, lowercased.
SONDER_NAMEN = {
    "inbox": "posteingang",
    "sent": "gesendet", "sent items": "gesendet", "sent messages": "gesendet",
    "gesendet": "gesendet", "gesendete objekte": "gesendet",
    "drafts": "entwuerfe", "entwürfe": "entwuerfe", "entwuerfe": "entwuerfe",
    "trash": "papierkorb", "deleted items": "papierkorb",
    "papierkorb": "papierkorb", "gelöschte objekte": "papierkorb",
    "junk": "spam", "junk e-mail": "spam", "spam": "spam", "junk-e-mail": "spam",
    "archive": "archiv", "archiv": "archiv",
}
SONDER_ICON = {"posteingang": "📥", "gesendet": "📤", "entwuerfe": "📝",
               "papierkorb": "🗑️", "spam": "🚫", "archiv": "📦",
               "alle": "🗂️", "markiert": "⭐", "wichtig": "❗"}


# ── State, always through the watchman ───────────────────────────────────
def _load(name, default):
    if W is not None:
        return W.load(name, default)
    try:
        with open(os.path.join(BASE, "state", name), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _save(name, daten, modus=0o644):
    if W is not None:
        W.save(name, daten, modus)
        return
    ordner = os.path.join(BASE, "state")
    os.makedirs(ordner, exist_ok=True)
    tmp = os.path.join(ordner, name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, modus)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(daten, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(ordner, name))


def txt(schluessel: str, **werte) -> str:
    """One text table for watchman, page and client — the client does not carry a
    second one. A key that is not in it comes back as itself, which is ugly but
    never a crash."""
    if W is not None:
        return W.txt(schluessel, **werte)
    return schluessel


def log(nachricht: str) -> None:
    if W is not None:
        W.log(nachricht)


# ── Settings ────────────────────────────────────────────────────────────
# 🔑 Every one of these is on the page. „alles einstellbar" means: whoever
# disagrees with a decision made here can move it, instead of having to live with
# it. The defaults are the cautious case throughout — remote images off, marking
# as read by hand, a confirmation before deleting.
# 🔴 The permitted positions of every choice stand in `AUSWAHL` below and
# NOWHERE else. They used to be repeated as a comment on each line — two places
# saying the same thing, and the comment is the one that rots.
VORGABEN = {
    "vorschau": "rechts",
    "dichte": "bequem",
    "pro_seite": 50,
    "sortierung": "datum",
    "richtung": "ab",
    "strang": False,               # group a conversation
    "nur_ungelesen": False,
    # The „New" overview above the inbox: what counts as new, and whether the
    # junk folder is part of it.
    "neu_zeitraum": "ungelesen",
    "neu_spam": False,
    "vorschautext": True,          # two lines of the body in each list row
    "absender_zeigen": "name",
    "gelesen_nach": -1,            # -1 by hand, 0 at once, otherwise seconds
    "bilder": "nie",
    "html_zuerst": True,
    "wache_grund": True,           # show the watchman's reason in the mail
    "kopfzeilen": False,           # all headers expanded
    "schrift": 100,                # percent
    "tasten": True,                # keyboard shortcuts
    "zeitform": "24",
    "startseite": "waechter",
    "papierkorb": "ordner",
    "sicherheitsfrage": True,
    "ordner_papierkorb": "",       # empty = recognise automatically
    "ordner_archiv": "",
    "ordner_spam": "",
    "ordner_entwuerfe": "",
    "ordner_gesendet": "",
    "signatur": "",
    "zitat": "unten",
    "zitat_kopf": True,
    "kopie_gesendet": True,
    "blind_kopie_selbst": False,
    "antwort_an": "",
    "anhang_grenze": 25,           # MB per mail
    "sperre_seite": False,         # ask for the word on the watchman page too
    "frist_min": SITZUNG_MIN,
}
# Values that may only take one of a few positions. A free text would go through,
# and then the page draws a layout nobody built.
AUSWAHL = {
    "vorschau": ("rechts", "unten", "aus"),
    "dichte": ("bequem", "eng"),
    "sortierung": ("datum", "von", "betreff", "groesse"),
    "richtung": ("ab", "auf"),
    "absender_zeigen": ("name", "adresse", "beides"),
    "neu_zeitraum": ("ungelesen", "t1", "t3", "t7"),
    "bilder": ("nie", "bekannte", "immer"),
    "zeitform": ("24", "12"),
    "startseite": ("waechter", "postfach"),
    "papierkorb": ("ordner", "flagge"),
    "zitat": ("unten", "oben"),
}
ZAHLEN = {"pro_seite": (10, 200), "gelesen_nach": (-1, 600), "schrift": (80, 140),
          "anhang_grenze": (1, 25), "frist_min": (5, 10080)}


# ── What the settings look like — ONE description for every surface ──────
# 🔑 There are two pages now (wide and phone). A settings table written in each of
# them is two tables, and the second one is always the one missing the newest
# switch. So the description lives HERE, next to `VORGABEN` and `AUSWAHL` that it
# is built from, and both pages only draw what they are handed.
WAHL_TEXTE = {
    "vorschau": {"rechts": "k.o_rechts", "unten": "k.o_unten", "aus": "k.o_aus"},
    "dichte": {"bequem": "k.o_bequem", "eng": "k.o_eng"},
    "sortierung": {"datum": "k.sort_datum", "von": "k.sort_von",
                   "betreff": "k.sort_betreff", "groesse": "k.sort_groesse"},
    "richtung": {"ab": "k.o_neuste", "auf": "k.o_aelteste"},
    "absender_zeigen": {"name": "k.o_name", "adresse": "k.o_adresse",
                        "beides": "k.o_beides"},
    # 🔴 „t1" is NOT „the last 24 hours". IMAP compares SINCE against the
    # internal DATE, without a time — so the honest name is „today and
    # yesterday", and that is what the labels say.
    "neu_zeitraum": {"ungelesen": "k.o_ungelesen", "t1": "k.o_seit_gestern",
                     "t3": "k.o_drei_tage", "t7": "k.o_sieben_tage"},
    "bilder": {"nie": "k.o_nie", "bekannte": "k.o_bekannte", "immer": "k.o_immer"},
    "zeitform": {"24": "k.o_24", "12": "k.o_12"},
    "startseite": {"waechter": "k.o_waechter", "postfach": "k.o_postfach"},
    "papierkorb": {"ordner": "k.o_in_korb", "flagge": "k.o_flagge"},
    "zitat": {"unten": "k.o_zitat_unten", "oben": "k.o_zitat_oben"},
}
# Numbers offered as a list rather than a slider — „every 3 seconds" is a decision,
# not a dial.
ZAHL_WAHL = {
    "pro_seite": [[25, "25"], [50, "50"], [100, "100"], [200, "200"]],
    "gelesen_nach": [[-1, "k.o_von_hand"], [0, "k.o_sofort"], [3, "k.o_3s"],
                     [10, "k.o_10s"]],
    "frist_min": [[60, "k.o_1h"], [720, "k.o_12h"], [10080, "k.o_7t"]],
}
SCHIEBER = {"schrift": (80, 140, 5, "%"), "anhang_grenze": (1, 25, 1, "MB")}
# 🔴 The help key is written out, not assembled from the title. Assembled
# („k.g_ansicht" + „_hilfe") it has no literal anywhere, and the language test
# bench can neither find a typo in it nor tell that it is in use.
GRUPPEN = [
    ("k.g_ansicht", "k.g_ansicht_hilfe",
     ("vorschau", "dichte", "pro_seite", "absender_zeigen", "vorschautext",
      "strang", "schrift", "zeitform", "startseite")),
    ("k.g_neu", "k.g_neu_hilfe", ("neu_zeitraum", "neu_spam")),
    ("k.g_lesen", "k.g_lesen_hilfe",
     ("gelesen_nach", "html_zuerst", "kopfzeilen", "wache_grund", "sortierung",
      "richtung", "nur_ungelesen")),
    ("k.g_sicher", "k.g_sicher_hilfe",
     ("bilder", "sperre_seite", "frist_min", "sicherheitsfrage", "papierkorb")),
    ("k.g_schreiben", "k.g_schreiben_hilfe",
     ("signatur", "zitat", "zitat_kopf", "kopie_gesendet", "blind_kopie_selbst",
      "antwort_an", "anhang_grenze")),
    ("k.g_ordner", "k.g_ordner_hilfe",
     ("ordner_gesendet", "ordner_entwuerfe", "ordner_papierkorb", "ordner_archiv",
      "ordner_spam")),
]
# Settings the phone version leaves out — not because they are unimportant but
# because they describe a layout that does not exist there.
NUR_BREIT = ("vorschau", "dichte", "strang", "kopfzeilen", "absender_zeigen")


def oberflaeche() -> list:
    """The settings as groups of fields — the description both pages draw from."""
    raus = []
    for titel, hilfe, felder in GRUPPEN:
        gruppe = {"titel": titel, "hilfe": hilfe, "felder": []}
        for name in felder:
            vor = VORGABEN[name]
            eintrag = {"s": name, "titel": "k.e." + name, "hilfe": "k.h." + name,
                       "nur_breit": name in NUR_BREIT}
            if name in AUSWAHL:
                texte = WAHL_TEXTE.get(name, {})
                eintrag["art"] = "wahl"
                # Out of AUSWAHL, so a value can never exist without being offered.
                eintrag["optionen"] = [[w, texte.get(w, w)] for w in AUSWAHL[name]]
            elif name in ZAHL_WAHL:
                eintrag["art"] = "wahl"
                eintrag["optionen"] = ZAHL_WAHL[name]
            elif name in SCHIEBER:
                tief, hoch, schritt, einheit = SCHIEBER[name]
                eintrag.update({"art": "zahl", "tief": tief, "hoch": hoch,
                                "schritt": schritt, "einheit": einheit})
            elif isinstance(vor, bool):
                eintrag["art"] = "knebel"
            elif name.startswith("ordner_"):
                eintrag["art"] = "ordner"
            elif name == "signatur":
                eintrag["art"] = "text"
                eintrag["mehrzeilig"] = True
            else:
                eintrag["art"] = "text"
            gruppe["felder"].append(eintrag)
        raus.append(gruppe)
    return raus


def einstellungen() -> dict:
    """Complete and plausible, whatever is in the file. A missing value is the
    default, an impossible one is the default as well — the page is drawn from
    this, and a layout nobody built is worse than a setting that did not stick."""
    e = _load(KLIENT_EINST, None)
    e = e if isinstance(e, dict) else {}
    fertig = {}
    for k, vor in VORGABEN.items():
        wert = e.get(k, vor)
        if isinstance(vor, bool):
            fertig[k] = bool(wert)
        elif isinstance(vor, int):
            try:
                wert = int(wert)
            except (TypeError, ValueError):
                wert = vor
            tief, hoch = ZAHLEN.get(k, (None, None))
            if tief is not None:
                wert = max(tief, min(hoch, wert))
            fertig[k] = wert
        else:
            wert = str(wert or "")
            if k in AUSWAHL and wert not in AUSWAHL[k]:
                wert = vor
            fertig[k] = wert
    return fertig


def einstellung_setzen(d: dict) -> dict:
    """Write one or several settings. Unknown keys are dropped instead of stored:
    a typo would otherwise sit in the file for ever and look like a feature."""
    e = _load(KLIENT_EINST, None)
    e = e if isinstance(e, dict) else {}
    genommen = []
    for k, v in (d or {}).items():
        if k not in VORGABEN:
            continue
        e[k] = v
        genommen.append(k)
    _save(KLIENT_EINST, e)
    fertig = einstellungen()
    return {"ok": True, "text": txt("a.gespeichert"), "einst": fertig,
            "genommen": genommen}


# ── The lock ────────────────────────────────────────────────────────────
# 🔴 Why there is one at all: the watchman page shows subject and sender, and the
# note in the vault says WHY — „not enough to spread the inbox across a web page
# without a login". The client shows the body. So it brings the login, and it is
# not optional: without a word set, no mail leaves this module.
_SITZUNGEN = {}          # token -> [expires, address, last seen]
_VERSUCHE = {}           # address -> [count, blocked until]
_SCHLOSS = threading.Lock()


def zugang_stand() -> dict:
    d = _load(KLIENT_STAND, None)
    return d if isinstance(d, dict) else {}


def wort_gesetzt() -> bool:
    s = zugang_stand()
    return bool(s.get("hash") and s.get("salz"))


def _haschen(wort: str, salz: bytes, runden: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", wort.encode("utf-8"), salz, runden)


def wort_setzen(neu: str, alt: str = "") -> dict:
    """Set or change the access word.

    🔴 Whoever already has a word has to name it. Otherwise anyone who reaches the
    page could set a new one and the lock would be a doorbell.
    """
    neu = str(neu or "")
    if len(neu) < 8:
        return {"ok": False, "text": txt("k.wort_kurz")}
    # 🔴 Whoever came in with a one-time word cannot name the old one — that
    #    is exactly what was forgotten. Otherwise they would be logged in and
    #    caught all the same: the very dead end a DocuSort user stood in.
    if wort_gesetzt() and not muss_wechseln():
        pruef = wort_pruefen(alt, "")
        if not pruef.get("ok"):
            return {"ok": False, "text": txt("k.wort_alt_falsch")}
    salz = os.urandom(16)
    # 🔑 The file is written from scratch, so `muss_wechseln` is gone
    #    afterwards — the obligation ends with the word that lifted it.
    _save(KLIENT_STAND, {
        "salz": salz.hex(), "hash": _haschen(neu, salz, RUNDEN).hex(),
        "runden": RUNDEN, "gesetzt": datetime.now().astimezone().isoformat(timespec="seconds"),
    }, 0o600)
    _not_schreiben({})
    _not_datei_weg()
    with _SCHLOSS:
        _SITZUNGEN.clear()          # a new word ends every old session
    if W is not None:
        W.chronik("klient_wort", text="Zugangswort gesetzt")
    return {"ok": True, "text": txt("k.wort_gesetzt")}



# ── Getting back in when the word no longer works ────────────────────────
# 🔴 WHY THIS EXISTS (06.10.2026)
#
# The same hole as in DocuSort, one door further along: `wort_setzen` asks for
# the old word, and whoever has forgotten it cannot reach their own mailbox any
# more. The mail sits at the provider and is gone all the same. A lock that
# shuts out ONLY the rightful owner protects nobody.
#
# 🔑 THE SCHEME IS DOCUSORT'S, piece by piece:
#   * The word is rolled, put down once and stored nowhere — only its hash
#     goes into the state file.
#   * ASKING FOR ONE CHANGES NOTHING. Whoever presses the button locks nobody
#     out; the existing word stays valid until somebody really uses the code.
#   * Two ways, and the second is the more important one: a mail to the
#     mailbox itself (the channel that demonstrably belongs to the owner) and,
#     if that fails, a file in the state folder — which anybody running
#     Postwache reaches through the file manager of their NAS.
#   * Valid 15 minutes, exactly once, at most five wrong tries, and a new one
#     no sooner than every two minutes.
#   * After getting in, a new word is demanded AT ONCE — and the old one is
#     not asked for, because that is precisely what was forgotten.
NOT_STAND = "klient_notzugang.json"            # Hash + Frist, 0600
NOT_DATEI = "zugangswort-zuruecksetzen.txt"    # the second way
NOT_GUELTIG_S = 15 * 60
NOT_SPERRE_S = 120
NOT_MAX = 5
# No characters that get mixed up while being typed over (0/O, 1/l/I) — the
# same alphabet as in DocuSort's `notzugang.py`.
NOT_ALPHABET = "".join(c for c in (string.ascii_letters + string.digits)
                       if c not in "0O1lI")


def _not_lesen() -> dict:
    d = _load(NOT_STAND, None)
    return d if isinstance(d, dict) else {}


def _not_schreiben(d) -> None:
    _save(NOT_STAND, d or {}, 0o600)


def _not_ordner() -> str:
    if W is not None:
        return os.path.dirname(W.state_pfad(NOT_STAND))
    return os.path.join(BASE, "state")


def _not_datei_weg() -> None:
    try:
        os.remove(os.path.join(_not_ordner(), NOT_DATEI))
    except OSError:
        pass


def _not_datei_legen(wort: str, bis: float) -> str:
    """Put the one-time word into the state folder — the second way.

    🔴 Not a weakening: the mailbox credentials live in that same folder.
    Whoever can read this file could read everything anyway.
    """
    ordner = _not_ordner()
    os.makedirs(ordner, exist_ok=True)
    ziel = os.path.join(ordner, NOT_DATEI)
    text = (
        "Postwache \u2014 Einmalwort\n"
        "==========================\n\n"
        "  Zugangswort:  %s\n\n"
        "Gueltig bis %s, genau einmal benutzbar.\n"
        "Nach dem Anmelden fragt die Postwache sofort nach einem neuen Wort,\n"
        "und diese Datei verschwindet von selbst.\n\n"
        "Hat das niemand angefordert? Dann loesche die Datei einfach \u2014\n"
        "es wurde nichts geaendert, das bisherige Wort gilt weiter.\n"
        % (wort, datetime.fromtimestamp(bis).strftime("%d.%m.%Y %H:%M")))
    fd = os.open(ziel, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return ziel


def _not_mailen(wort: str) -> str:
    """Send the one-time word to the mailbox itself.

    🔑 The channel that demonstrably belongs to the owner: their own address.
    Whoever is locked out of this page still reaches their mail — on the
    phone, on the provider's website, in any other program.

    Returns the address it went to, or "".
    """
    if W is None:
        return ""
    for f in (W.postfaecher() or []):
        pf_id = str(f.get("id") or "")
        s = smtp_zugang(pf_id)
        adresse = str(f.get("adresse") or "")
        if not s or not s.get("server") or not adresse:
            continue
        try:
            nachricht = EmailMessage()
            nachricht["From"] = adresse
            nachricht["To"] = adresse
            nachricht["Subject"] = "Postwache \u2014 Einmalwort"
            nachricht.set_content(
                "Jemand hat an der Postwache ein Einmalwort angefordert.\n\n"
                "  Zugangswort:  %s\n\n"
                "Gueltig 15 Minuten, genau einmal benutzbar. Nach dem Anmelden "
                "fragt die Postwache sofort nach einem neuen Wort.\n\n"
                "Warst du das nicht? Dann ignoriere diese Mail \u2014 es wurde "
                "nichts geaendert, dein bisheriges Wort gilt weiter." % wort)
            verbindung = _smtp_verbinden(s)
            try:
                verbindung.send_message(nachricht)
            finally:
                try:
                    verbindung.quit()
                except Exception:
                    pass
            return adresse
        except Exception as e:                       # noqa: BLE001
            log("Einmalwort per Mail gescheitert: %s" % str(e)[:140])
    return ""


def notwort_anfordern() -> dict:
    """Roll a one-time word, put it where the owner finds it — change NOTHING."""
    if not wort_gesetzt():
        return {"ok": False, "text": txt("k.kein_wort"), "kein_wort": True}
    jetzt = time.time()
    alt = _not_lesen()
    if alt and jetzt - float(alt.get("erzeugt") or 0) < NOT_SPERRE_S:
        warte = int(NOT_SPERRE_S - (jetzt - float(alt["erzeugt"])))
        return {"ok": False, "text": txt("k.not_zu_frueh", s=warte), "warte": warte}
    wort = "".join(secrets.choice(NOT_ALPHABET) for _ in range(10))
    bis = jetzt + NOT_GUELTIG_S
    salz = os.urandom(16)
    _not_schreiben({"salz": salz.hex(), "hash": _haschen(wort, salz, RUNDEN).hex(),
                    "runden": RUNDEN, "erzeugt": jetzt, "ablauf": bis,
                    "versuche": 0})
    adresse = _not_mailen(wort)
    if adresse:
        _not_datei_weg()
        if W is not None:
            W.chronik("klient_notwort", text="Einmalwort per Mail")
        return {"ok": True, "weg": "mail", "adresse": adresse,
                "gueltig_min": NOT_GUELTIG_S // 60,
                "text": txt("k.not_per_mail", a=adresse)}
    try:
        ort = _not_datei_legen(wort, bis)
    except OSError as e:
        _not_schreiben({})
        return {"ok": False, "text": txt("k.not_fehler", fehler=str(e)[:160])}
    if W is not None:
        W.chronik("klient_notwort", text="Einmalwort als Datei")
    return {"ok": True, "weg": "datei", "datei": ort, "dateiname": NOT_DATEI,
            "gueltig_min": NOT_GUELTIG_S // 60,
            "text": txt("k.not_per_datei", d=NOT_DATEI)}


def notwort_einloesen(wort: str) -> bool:
    """Does this word match the current one-time word? Then spend it — once.

    🔴 The page's access word is NOT touched here, and that is deliberate: a
    one-time word that becomes the new permanent one is no longer valid once,
    but until the next change. The dead end (in, forced to change, old word
    forgotten) is solved on the other side: `wort_setzen` does not ask for the
    old word while `muss_wechseln` stands.
    """
    stand = _not_lesen()
    if not stand or not stand.get("hash"):
        return False
    if time.time() > float(stand.get("ablauf") or 0):
        _not_schreiben({})
        _not_datei_weg()
        return False
    try:
        salz = bytes.fromhex(str(stand.get("salz") or ""))
        soll = bytes.fromhex(str(stand.get("hash") or ""))
        runden = int(stand.get("runden") or RUNDEN)
    except ValueError:
        return False
    if not hmac.compare_digest(_haschen(str(wort or ""), salz, runden), soll):
        stand["versuche"] = int(stand.get("versuche") or 0) + 1
        if stand["versuche"] >= NOT_MAX:
            _not_schreiben({})
            _not_datei_weg()
        else:
            _not_schreiben(stand)
        return False
    _not_schreiben({})                       # exactly once
    _not_datei_weg()
    s = zugang_stand()
    s["muss_wechseln"] = True
    _save(KLIENT_STAND, s, 0o600)
    if W is not None:
        W.chronik("klient_notwort", text="Einmalwort eingeloest")
    return True


def muss_wechseln() -> bool:
    """Did somebody come in with a one-time word and still owe a new one?"""
    return bool(zugang_stand().get("muss_wechseln"))


def _bremse(adresse: str) -> int:
    """How many seconds this address still has to wait. The counter stands
    against the ADDRESS, not against the word — otherwise trying a second word
    would be free."""
    with _SCHLOSS:
        eintrag = _VERSUCHE.get(adresse)
        if not eintrag:
            return 0
        rest = int(eintrag[1] - time.time())
        return max(0, rest)


def _fehlversuch(adresse: str) -> None:
    with _SCHLOSS:
        eintrag = _VERSUCHE.setdefault(adresse, [0, 0.0])
        eintrag[0] += 1
        if eintrag[0] > VERSUCHE_FREI:
            i = min(eintrag[0] - VERSUCHE_FREI, len(SPERRZEIT)) - 1
            eintrag[1] = time.time() + SPERRZEIT[i]


def wort_pruefen(wort: str, adresse: str) -> dict:
    """Check the word and, on success, hand out a session.

    The comparison is `compare_digest`, not `==`: whoever can measure the answer
    time can read a normal comparison letter by letter.
    """
    if not wort_gesetzt():
        return {"ok": False, "text": txt("k.kein_wort"), "kein_wort": True}
    warte = _bremse(adresse) if adresse else 0
    if warte:
        return {"ok": False, "text": txt("k.gesperrt", s=warte), "warte": warte}
    s = zugang_stand()
    try:
        salz = bytes.fromhex(str(s.get("salz") or ""))
        soll = bytes.fromhex(str(s.get("hash") or ""))
        runden = int(s.get("runden") or RUNDEN)
    except ValueError:
        return {"ok": False, "text": txt("k.wort_falsch")}
    if not hmac.compare_digest(_haschen(str(wort or ""), salz, runden), soll):
        # 🔑 The second key: a one-time word somebody asked for. Only after
        #    the real word did not match — otherwise a stale code could push
        #    the valid word aside.
        if notwort_einloesen(wort):
            if adresse:
                with _SCHLOSS:
                    _VERSUCHE.pop(adresse, None)
            return {"ok": True, "text": txt("k.angemeldet"), "wechseln": True}
        if adresse:
            _fehlversuch(adresse)
        warte = _bremse(adresse) if adresse else 0
        return {"ok": False, "text": txt("k.wort_falsch"), "warte": warte}
    if adresse:
        with _SCHLOSS:
            _VERSUCHE.pop(adresse, None)
    return {"ok": True, "text": txt("k.angemeldet")}


def sitzung_neu(adresse: str) -> str:
    frist = int(einstellungen()["frist_min"]) * 60
    marke = base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("=")
    with _SCHLOSS:
        _SITZUNGEN[marke] = [time.time() + frist, adresse, time.time()]
    return marke


def sitzung_gueltig(marke: str) -> bool:
    """Valid, and it keeps itself alive while being used — an expiry in the middle
    of writing a mail would be the worst possible moment."""
    if not marke:
        return False
    frist = int(einstellungen()["frist_min"]) * 60
    with _SCHLOSS:
        eintrag = _SITZUNGEN.get(marke)
        if not eintrag:
            return False
        if time.time() > eintrag[0]:
            _SITZUNGEN.pop(marke, None)
            return False
        eintrag[0] = time.time() + frist
        eintrag[2] = time.time()
        return True


def sitzung_beenden(marke: str) -> dict:
    with _SCHLOSS:
        _SITZUNGEN.pop(marke or "", None)
    return {"ok": True, "text": txt("k.abgemeldet")}


def keks_lesen(kopfzeile: str) -> str:
    """Pick our cookie out of the Cookie header — without `http.cookies`, because
    one malformed cookie from another page on the same host would make the whole
    parse fail there."""
    for stueck in (kopfzeile or "").split(";"):
        name, _, wert = stueck.strip().partition("=")
        if name.strip() == KEKS:
            return wert.strip()
    return ""


def keks_setzen(marke: str, sicher: bool) -> str:
    frist = int(einstellungen()["frist_min"]) * 60
    teile = ["%s=%s" % (KEKS, marke), "Path=/", "HttpOnly", "SameSite=Strict",
             "Max-Age=%d" % (frist if marke else 0)]
    # Only behind TLS — with `Secure` on plain HTTP the browser would drop the
    # cookie and the login would silently never work.
    if sicher:
        teile.append("Secure")
    return "; ".join(teile)


def lage_schloss(marke: str) -> dict:
    """What the page may know before anyone is logged in: whether a word exists at
    all, and nothing else."""
    return {"wort": wort_gesetzt(), "an": sitzung_gueltig(marke),
            "sperre_seite": bool(einstellungen()["sperre_seite"]),
            # 🔑 So the page can offer the way back at all — and afterwards
            #    knows that a new word is due now.
            "wechseln": muss_wechseln()}


# ── Folder names: modified UTF-7 ─────────────────────────────────────────
# 🔑 A folder is called what the SERVER calls it — that name goes into every
# command and must never be touched. But „Gel&APY-schte Objekte" is not a name a
# human reads. So: the raw name stays the key, the decoded one is only ever
# displayed. Mixing those two up creates a second folder next to the existing one.
def utf7_dekodieren(name: str) -> str:
    raus, i, n = [], 0, len(name or "")
    while i < n:
        if name[i] != "&":
            raus.append(name[i])
            i += 1
            continue
        ende = name.find("-", i)
        if ende < 0:
            raus.append(name[i:])
            break
        stueck = name[i + 1:ende]
        if not stueck:
            raus.append("&")
        else:
            roh = stueck.replace(",", "/")
            roh += "=" * (-len(roh) % 4)
            try:
                raus.append(base64.b64decode(roh).decode("utf-16-be"))
            except Exception:
                raus.append(name[i:ende + 1])
        i = ende + 1
    return "".join(raus)


def utf7_kodieren(name: str) -> str:
    raus, puffer = [], []

    def leeren():
        if puffer:
            roh = base64.b64encode("".join(puffer).encode("utf-16-be")).decode()
            raus.append("&" + roh.rstrip("=").replace("/", ",") + "-")
            puffer.clear()

    for z in name or "":
        if z == "&":
            leeren()
            raus.append("&-")
        elif 0x20 <= ord(z) <= 0x7E:
            leeren()
            raus.append(z)
        else:
            puffer.append(z)
    leeren()
    return "".join(raus)


# ── One connection per mailbox, kept warm ────────────────────────────────
# 🔴 A client that opens a new IMAP connection for every click is slow AND rude:
# most providers allow only a handful at a time, and the watchman needs one of
# them every minute. So: exactly ONE per mailbox, guarded by a lock, closed after
# five idle minutes — and re-established when the server has hung up in between.
_VERBINDUNGEN = {}
_V_SCHLOSS = threading.Lock()


class Briefkasten(W.Postfach if W is not None else object):
    """The watchman's mailbox, taught what a client needs on top: the tree with
    flags and counts, pages of a folder, single mails, flags, moving, appending.

    Everything that reads uses BODY.PEEK, and a folder is opened read-only unless
    an action actually has to write. Both are inherited, and both are checked by
    the test bench against the real command text."""

    def __init__(self, zug: dict, schreiben: bool = True):
        # 🔑 `schreiben=False` makes a connection that CANNOT write — the folder
        # is opened read-only from the login on. The listening post uses it: not
        # „it does not write" out of discipline, but out of construction.
        super().__init__(zug, schreiben)
        self.ordner = ""            # which folder is selected
        self.schreibend = False     # and in which mode
        self.faehig = set()
        self.baum_stand = (0.0, [])

    def __enter__(self):
        super().__enter__()
        self.ordner, self.schreibend = "INBOX", bool(self.schreiben)
        try:
            typ, dat = self.m.capability()
            if typ == "OK" and dat:
                self.faehig = {w.decode().upper() if isinstance(w, bytes) else str(w).upper()
                               for w in (dat[0] or b"").split()}
        except Exception:
            self.faehig = set()
        return self

    def kann(self, was: str) -> bool:
        return was.upper() in self.faehig

    def lebt(self) -> bool:
        try:
            return self.m.noop()[0] == "OK"
        except Exception:
            return False

    # ── Choosing a folder ───────────────────────────────────────────
    def waehle(self, ordner: str, schreiben: bool = False) -> int:
        """Select a folder — and only really do it when it is a different one or a
        different mode.

        🔴 Read-only is the default. Whoever browses changes nothing; only an
        action that has to write opens the folder for writing. That way a bug in
        the drawing code cannot set a flag.
        """
        ordner = ordner or "INBOX"
        if self.ordner == ordner and self.schreibend == bool(schreiben):
            return -1
        typ, dat = self.m.select(self._zitat(ordner), readonly=not schreiben)
        if typ != "OK":
            raise RuntimeError("Ordner nicht wählbar: %s" % ordner)
        self.ordner, self.schreibend = ordner, bool(schreiben)
        try:
            return int((dat[0] or b"0").decode() if isinstance(dat[0], bytes) else dat[0])
        except (TypeError, ValueError, IndexError):
            return 0

    # ── The tree ────────────────────────────────────────────────────
    def baum(self, frisch: bool = False) -> list:
        """All folders with flags, depth, counts and role.

        The counts come from STATUS, one command per folder — with 160 folders
        that is a second, so the answer is kept for half a minute. `frisch=True`
        asks again."""
        if not frisch and self.baum_stand[1] and time.time() - self.baum_stand[0] < 30:
            return self.baum_stand[1]
        try:
            typ, zeilen = self.m.list()
        except Exception as e:
            log("Ordnerbaum nicht lesbar: %s" % str(e)[:120])
            return []
        if typ != "OK":
            return []
        roh = []
        for zl in zeilen or []:
            s = zl.decode("utf-8", "replace") if isinstance(zl, bytes) else str(zl)
            t = re.match(r'^\((?P<f>[^)]*)\)\s+(?P<tr>"[^"]*"|NIL)\s+(?P<n>.*)$', s.strip())
            if not t:
                continue
            flaggen = {f.lower() for f in t.group("f").split()}
            trenner = t.group("tr").strip('"') or self.trenner
            name = t.group("n").strip()
            if name.startswith('"') and name.endswith('"') and len(name) > 1:
                name = name[1:-1]
            if not name:
                continue
            roh.append((name, flaggen, trenner))
        raus = []
        for name, flaggen, trenner in roh:
            rolle = ""
            for flagge, r in SONDER_FLAGGEN.items():
                if flagge in flaggen:
                    rolle = r
                    break
            if not rolle:
                letzte = name.split(trenner)[-1] if trenner else name
                rolle = SONDER_NAMEN.get(utf7_dekodieren(letzte).lower(), "")
                if name.upper() == "INBOX":
                    rolle = "posteingang"
            waehlbar = "\\noselect" not in flaggen
            gesamt, ungelesen = (0, 0)
            if waehlbar:
                gesamt, ungelesen = self.zaehlen(name)
            teile = name.split(trenner) if trenner else [name]
            raus.append({
                "name": name,
                "zeige": utf7_dekodieren(teile[-1]),
                "pfad": utf7_dekodieren(name),
                "tiefe": max(0, len(teile) - 1),
                "rolle": rolle,
                "icon": SONDER_ICON.get(rolle, ""),
                "waehlbar": waehlbar,
                "kinder": "\\haschildren" in flaggen,
                "gesamt": gesamt,
                "ungelesen": ungelesen,
            })
        # Inbox first, then special folders in a fixed order, then alphabetically —
        # the same order every mail program has trained its users on.
        rang = {"posteingang": 0, "entwuerfe": 1, "gesendet": 2, "archiv": 3,
                "spam": 8, "papierkorb": 9}
        raus.sort(key=lambda o: (rang.get(o["rolle"], 5),
                                 o["pfad"].lower() if o["rolle"] not in rang else ""))
        self.baum_stand = (time.time(), raus)
        return raus

    def zaehlen(self, name: str) -> tuple:
        """(total, unread) — via STATUS, so without selecting the folder. Selecting
        would throw away the list currently being shown."""
        try:
            typ, dat = self.m.status(self._zitat(name), "(MESSAGES UNSEEN)")
        except Exception:
            return 0, 0
        if typ != "OK" or not dat:
            return 0, 0
        s = dat[0].decode("utf-8", "replace") if isinstance(dat[0], bytes) else str(dat[0])
        g = re.search(r"MESSAGES\s+(\d+)", s, re.I)
        u = re.search(r"UNSEEN\s+(\d+)", s, re.I)
        return (int(g.group(1)) if g else 0), (int(u.group(1)) if u else 0)

    def stand(self, name: str) -> tuple:
        """(total, unread, next number) — one STATUS, without selecting.

        🔑 The third number is the one that says „mail has ARRIVED". Total and
        unread both move when something is read, deleted or moved away; UIDNEXT
        only ever grows, and only when something comes in. The listening post
        needs both kinds: what changed at all, and whether it is new post.
        """
        try:
            typ, dat = self.m.status(self._zitat(name), "(MESSAGES UNSEEN UIDNEXT)")
        except Exception:
            return (0, 0, 0)
        if typ != "OK" or not dat:
            return (0, 0, 0)
        s = dat[0].decode("utf-8", "replace") if isinstance(dat[0], bytes) else str(dat[0])
        zahl = {}
        for feld in ("MESSAGES", "UNSEEN", "UIDNEXT"):
            t = re.search(feld + r"\s+(\d+)", s, re.I)
            zahl[feld] = int(t.group(1)) if t else 0
        return (zahl["MESSAGES"], zahl["UNSEEN"], zahl["UIDNEXT"])

    def lauschen(self, sekunden: float, weiter=None) -> bool:
        """Wait for the server to say something. True = it did.

        🔴 Exactly ONE line is read by hand here — the „+ idling" that the server
        sends at once. Everything that follows is read by `imaplib` itself, after
        DONE. That is the whole trick: `imaplib` is not built for untagged lines
        arriving unasked, and a connection read half by hand and half by the
        library is a connection out of step with itself. This way it cannot
        happen — whatever the server said in between, the library picks it up
        with the tagged answer and the state is its own again.

        🔑 And the waiting itself does not read at all: `select` only asks the
        socket whether something is there. A timeout can therefore never cut a
        line in half.
        """
        m = self.m
        marke = m._new_tag()                        # registers the tag as pending
        m.send(marke + b" IDLE\r\n")
        zeile = m.readline()
        if not zeile.startswith(b"+"):
            raise imaplib.IMAP4.abort("IDLE abgelehnt: %r" % zeile[:80])
        # 🔴 In slices, not in one long wait: `weiter` is how the post learns
        # that nobody is listening any more. A single `select` over five minutes
        # would keep a connection open for five minutes after the last tab
        # closed.
        ende = time.monotonic() + max(1.0, sekunden)
        bereit = False
        try:
            while True:
                rest = min(15.0, ende - time.monotonic())
                if rest <= 0:
                    break
                if select.select([m.socket()], [], [], rest)[0]:
                    bereit = True
                    break
                if weiter is not None and not weiter():
                    break
        finally:
            m.send(b"DONE\r\n")
            m._get_tagged_response(marke)
        return bereit

    # ── Which mails, in which order ─────────────────────────────────
    # 🔴 NO `UTF8=ACCEPT`. The server would then name its folders in plain UTF-8
    # instead of modified UTF-7 — and the watchman has learned them in UTF-7 and
    # has them in `ablage.json` that way. Two spellings of the same folder means
    # the client files into a NEW folder next to the existing one. So the raw
    # form stays everywhere, and only the display is decoded.
    def _such_bytes(self, feld: str, wort: str) -> bytes:
        """One search criterion as bytes.

        A non-ASCII search word cannot go into the command line as a str:
        imaplib encodes str as ASCII and fails. As bytes with `CHARSET UTF-8` it
        works on every server that was tried — and if it does not, `suche()`
        falls back."""
        wort = (wort or "").replace("\\", "").replace('"', "")
        roh = wort.encode("utf-8")
        if feld == "alle":
            return (b'OR OR SUBJECT "' + roh + b'" FROM "' + roh
                    + b'" TEXT "' + roh + b'"')
        schluessel = {"von": b"FROM", "betreff": b"SUBJECT", "an": b"TO",
                      "text": b"TEXT"}.get(feld, b"SUBJECT")
        return schluessel + b' "' + roh + b'"'

    def uids(self, ordner: str, sieb: str = "", suche: str = "", feld: str = "alle",
             sortierung: str = "datum", richtung: str = "ab") -> tuple:
        """All matching UIDs of a folder, already in display order.

        Returns (uids, sortiert_vom_server). Without the SORT extension only the
        UID order is available — that is the order of ARRIVAL, which for a mailbox
        is almost always the date order, but not guaranteed. The page is told, so
        it can say so instead of quietly showing something else.
        """
        self.waehle(ordner)
        kriterien = []
        if sieb == "ungelesen":
            kriterien.append(b"UNSEEN")
        elif sieb == "markiert":
            kriterien.append(b"FLAGGED")
        elif sieb == "unbeantwortet":
            kriterien.append(b"UNANSWERED")
        elif sieb == "anhang":
            # There is no IMAP criterion for „has an attachment". What there is:
            # the mail's own Content-Type. Everything with a real attachment is
            # `multipart/mixed`, so this finds it — plus the occasional newsletter
            # that is built that way without one. Named honestly on the page.
            kriterien.append(b'OR HEADER Content-Type "multipart/mixed"'
                             b' HEADER Content-Type "multipart/related"')
        if suche:
            kriterien.append(self._such_bytes(feld, suche))
        if not kriterien:
            kriterien = [b"ALL"]
        umgekehrt = richtung != "auf"
        schluessel = {"datum": b"DATE", "von": b"FROM", "betreff": b"SUBJECT",
                      "groesse": b"SIZE"}.get(sortierung, b"DATE")
        if self.kann("SORT"):
            folge = b"(" + (b"REVERSE " if umgekehrt else b"") + schluessel + b")"
            try:
                typ, dat = self.m.uid("SORT", folge, b"UTF-8", *kriterien)
                if typ == "OK":
                    roh = b" ".join(x for x in (dat or []) if x)
                    return [int(x) for x in roh.split()], True
            except Exception as e:
                log("SORT nicht nutzbar: %s" % str(e)[:100])
        alle = self._suchen(kriterien)
        alle.sort(reverse=umgekehrt)
        # Without SORT only the date can be ordered honestly, and only as UID
        # order. Everything else would be a claim.
        return alle, sortierung == "datum"

    def _suchen(self, kriterien: list) -> list:
        """UID SEARCH with three attempts: with charset, without, and ASCII-only.
        A server that cannot do one of them must not leave the page empty."""
        versuche = ([b"CHARSET", b"UTF-8"] + kriterien, kriterien)
        for args in versuche:
            try:
                typ, dat = self.m.uid("SEARCH", *args)
            except Exception:
                continue
            if typ == "OK":
                roh = b" ".join(x for x in (dat or []) if x)
                try:
                    return [int(x) for x in roh.split()]
                except ValueError:
                    return []
        return []

    def koepfe(self, uids: list, auszug: bool = False) -> dict:
        """{uid: header record} for a page of the list — two FETCHes, no more.

        🔴 The metadata and the header block are fetched TOGETHER, the blueprint
        separately through the watchman's proven `strukturen()`. Not out of
        laziness: `_fetch_zeilen()` turns a literal into a quoted string with the
        line breaks replaced by spaces — which is right for a blueprint and wrong
        for a header block, because then `From:` and `Subject:` end up on one
        line. Two questions, two answers.
        """
        raus = {}
        if not uids:
            return raus
        for i in range(0, len(uids), 100):
            teil = ",".join(str(u) for u in uids[i:i + 100])
            try:
                typ, daten = self.m.uid(
                    "FETCH", teil,
                    "(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER.FIELDS "
                    "(FROM TO CC SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES "
                    "LIST-UNSUBSCRIBE)])")
            except Exception as e:
                log("Kopfzeilen nicht lesbar: %s" % str(e)[:120])
                continue
            if typ != "OK":
                continue
            raus.update(self._koepfe_lesen(daten))
        strukturen = self.strukturen(list(raus))
        for uid, satz in raus.items():
            anh, _ = anhang_und_inline(strukturen.get(uid))
            satz["anhang"] = len(anh)
            satz["anhang_gross"] = sum(a["b"] for a in anh)
        if auszug:
            for uid, text in self.auszuege(strukturen).items():
                if uid in raus:
                    satz = raus.get(uid)
                    satz["auszug"] = text
        return raus

    def auszuege(self, strukturen: dict) -> dict:
        """The first two lines of every letter in the list.

        A list that shows only who wrote and what the subject says makes the
        reader open a mail to find out whether it is worth opening. Two lines of
        the text answer that in the list.

        🔴 Still `BODY.PEEK`, and still only a PIECE of the part: the fetch asks
        for the first AUSZUG_BYTES bytes of the body, never for the mail. A page
        of fifty costs a few kilobytes that way, and a letter with a
        ten-megabyte picture in it costs exactly as much as one without.

        🔑 And it is ONE question per shape, not one per mail: the mails are
        grouped by part number and encoding, so a page usually needs two or three
        FETCHes — most letters carry their text in the same place.
        """
        raus, gruppen = {}, {}
        for uid, struct in (strukturen or {}).items():
            teile = W._teile(struct) if struct else []
            t = _erster_text(teile, "plain") or _erster_text(teile, "html")
            if not t:
                continue
            schluessel = (t["nr"], (t.get("kodierung") or "").upper(),
                          t.get("zeichensatz") or "", t["subtyp"].lower())
            gruppen.setdefault(schluessel, []).append(uid)
        for (nr, kod, satz, sub), liste in gruppen.items():
            for i in range(0, len(liste), 100):
                teil = ",".join(str(u) for u in sorted(liste[i:i + 100]))
                try:
                    typ, daten = self.m.uid(
                        "FETCH", teil,
                        "(UID BODY.PEEK[%s]<0.%d>)" % (nr, AUSZUG_BYTES))
                except Exception as e:
                    log("Auszug nicht lesbar: %s" % str(e)[:120])
                    continue
                if typ != "OK":
                    continue
                for uid, roh in _stuecke_je_uid(daten):
                    raus[uid] = _auszug_aus(roh, kod, satz, sub == "html")
        return raus

    @staticmethod
    def _koepfe_lesen(daten) -> dict:
        """Pick the header records out of imaplib's answer.

        🔴 The metadata (UID, FLAGS, …) stands in the piece BEFORE the literal —
        unless the server answers in a different order, then it stands in the one
        after it. Both are looked at, which costs two lines and saves a class of
        mails that are otherwise simply missing from the list.
        """
        raus = {}
        stuecke = list(daten or [])
        for i, el in enumerate(stuecke):
            if not (isinstance(el, tuple) and len(el) >= 2):
                continue
            vor = el[0] or b""
            nach = stuecke[i + 1] if i + 1 < len(stuecke) and isinstance(stuecke[i + 1], bytes) else b""
            rand = (vor + b" " + nach).decode("utf-8", "replace")
            t = re.search(r"UID\s+(\d+)", rand)
            if not t:
                continue
            uid = int(t.group(1))
            flaggen = re.search(r"FLAGS\s+\(([^)]*)\)", rand)
            flaggen = [f.lower() for f in (flaggen.group(1).split() if flaggen else [])]
            groesse = re.search(r"RFC822\.SIZE\s+(\d+)", rand)
            intern = re.search(r'INTERNALDATE\s+"([^"]+)"', rand)
            msg = W.msg_aus_bytes(el[1] or b"")
            name, adresse = W.absender_teile(msg.get("From", ""))
            wann = _zeitpunkt(msg.get("Date", "")) or _intern_zeit(intern.group(1) if intern else "")
            raus[uid] = {
                "uid": uid,
                "von": name or adresse,
                "adresse": (adresse or "").lower(),
                "an": _adressen_kurz(msg.get_all("To") or []),
                "kopie": _adressen_kurz(msg.get_all("Cc") or []),
                "betreff": W.dekodieren(msg.get("Subject", "")) or "",
                "zeit": wann,
                "groesse": int(groesse.group(1)) if groesse else 0,
                "gelesen": "\\seen" in flaggen,
                "markiert": "\\flagged" in flaggen,
                "beantwortet": "\\answered" in flaggen,
                "entwurf": "\\draft" in flaggen,
                "geloescht": "\\deleted" in flaggen,
                "message_id": (msg.get("Message-Id") or "").strip(),
                "strang": _strang_schluessel(msg),
                "abmelden": bool((msg.get("List-Unsubscribe") or "").strip()),
            }
        return raus

    # ── One mail, completely ────────────────────────────────────────
    def neue(self, zeitraum: str = "ungelesen", mit_spam: bool = False,
             grenze: int = NEU_GRENZE, auszug: bool = False) -> dict:
        """Everything new, wherever it has ended up.

        🔑 This is the one view the watchman makes NECESSARY. It moves new mail
        out of the inbox into its folder, and does it well — and exactly because
        of that, „what came in" is no longer one folder but twelve. So the
        question is asked of all of them at once and answered in one list, with
        the folder written next to every line.

        Cheap by construction: `baum()` already knows the unread count of every
        folder from STATUS, so for the „unread" setting only folders that have
        any are opened at all — usually two or three, not twenty. Still
        `EXAMINE`, still `BODY.PEEK`: looking at this list changes nothing.
        """
        tage = {"t1": 1, "t3": 3, "t7": 7}.get(zeitraum, 0)
        ordner_liste, gesamt, treffer = [], 0, []
        for o in self.baum():
            if not o["waehlbar"] or o["rolle"] in ("papierkorb", "entwuerfe"):
                continue
            if o["rolle"] == "spam" and not mit_spam:
                continue
            if not tage:
                if not o["ungelesen"]:
                    continue
                kriterien = [b"UNSEEN"]
            else:
                seit = (datetime.now() - timedelta(days=tage)).strftime("%d-%b-%Y")
                kriterien = [("SINCE " + seit).encode()]
            try:
                self.waehle(o["name"])
                uids = self._suchen(kriterien)
            except Exception as e:
                log("Ordner %s nicht durchsuchbar: %s" % (o["name"], str(e)[:100]))
                continue
            if not uids:
                continue
            gesamt += len(uids)
            uids.sort(reverse=True)
            ordner_liste.append((o, uids))
        # 🔴 A single folder of newsletters can hold two thousand unread mails.
        # Every folder gets the SAME share of the budget, the newest first —
        # otherwise the first folder eats it and the rest is silently missing.
        anteil = max(10, NEU_KOEPFE // max(1, len(ordner_liste)))
        gekuerzt = False
        for o, uids in ordner_liste:
            teil = uids[:min(anteil, grenze)]
            gekuerzt = gekuerzt or len(teil) < len(uids)
            try:
                self.waehle(o["name"])
                koepfe = self.koepfe(teil, auszug=auszug)
            except Exception as e:
                log("Koepfe aus %s nicht lesbar: %s" % (o["name"], str(e)[:100]))
                continue
            for uid in teil:
                satz = koepfe.get(uid)
                if not satz:
                    continue
                satz["ordner"] = o["name"]
                satz["ordner_zeige"] = o["pfad"]
                satz["ordner_kurz"] = o["zeige"]
                satz["rolle"] = o["rolle"]
                treffer.append(satz)
        # The only order that means anything across folders is the date — a UID
        # is only comparable inside its own folder.
        treffer.sort(key=lambda m: m.get("zeit") or "", reverse=True)
        return {"mails": treffer[:grenze], "gesamt": gesamt,
                "gekuerzt": gekuerzt or len(treffer) > grenze,
                "ordner": len(ordner_liste)}

    def mail(self, ordner: str, uid: int, bilder: bool = False,
             roh_teile: bool = True) -> dict:
        """Everything needed to display ONE mail — and nothing beyond it.

        🔴 Still BODY.PEEK. Displaying a mail does not make it read; that happens
        only through `flagge()`, and when it happens is a setting with an „only by
        hand" position.
        """
        self.waehle(ordner)
        struct = self.strukturen([uid]).get(uid)
        typ, daten = self.m.uid("FETCH", str(uid),
                                "(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER])")
        if typ != "OK" or not daten:
            return {}
        kopf_roh, rand = b"", ""
        stuecke = list(daten)
        for i, el in enumerate(stuecke):
            if isinstance(el, tuple) and len(el) >= 2:
                kopf_roh = el[1] or b""
                nach = stuecke[i + 1] if i + 1 < len(stuecke) and isinstance(stuecke[i + 1], bytes) else b""
                rand = ((el[0] or b"") + b" " + nach).decode("utf-8", "replace")
                break
        if not kopf_roh:
            return {}
        msg = W.msg_aus_bytes(kopf_roh)
        flaggen = re.search(r"FLAGS\s+\(([^)]*)\)", rand)
        flaggen = [f.lower() for f in (flaggen.group(1).split() if flaggen else [])]
        name, adresse = W.absender_teile(msg.get("From", ""))

        teile = W._teile(struct) if struct else []
        text_teil = _erster_text(teile, "plain")
        html_teil = _erster_text(teile, "html")
        text, roh_html = "", ""
        if roh_teile and text_teil:
            text = _text_dekodieren(self.teil_holen(uid, text_teil["nr"],
                                                    text_teil["kodierung"]),
                                    text_teil.get("zeichensatz"))
        if roh_teile and html_teil:
            roh_html = _text_dekodieren(self.teil_holen(uid, html_teil["nr"],
                                                        html_teil["kodierung"]),
                                        html_teil.get("zeichensatz"))
        if roh_teile and not text and not roh_html:
            # A mail without a recognisable structure (or a server that answers a
            # blueprint we cannot read) still has a body. Fetching BODY.PEEK[TEXT]
            # is the last resort — never nothing at all.
            text = _text_dekodieren(self.teil_holen(uid, "TEXT", ""), "")
        anhaenge, inline = anhang_und_inline(struct, teile)
        html, blockiert, links = ("", 0, [])
        if roh_html:
            html, blockiert, links = html_saeubern(roh_html, bilder, inline, ordner, uid)
        # The filter handed back nothing readable. Then the letter is shown as
        # text — made out of the HTML if there is no plain part. A reading pane
        # that stays empty tells the reader the mail is empty, and that is a lie.
        if roh_html and not html and not text:
            text = _html.unescape(re.sub(
                r"<[^>]+>", " ",
                re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", roh_html)))
            text = re.sub(r"[ \t]{2,}", " ", re.sub(r"\n{3,}", "\n\n", text))
        kopf = W.kopf_lesen(msg, text[:3000])
        urteil = W.einordnen(kopf, text[:3000]) if kopf.get("adresse") else {}
        return {
            "uid": uid, "ordner": ordner,
            "von": name or adresse, "adresse": (adresse or "").lower(),
            "an": _adressen_lang(msg.get_all("To") or []),
            "kopie": _adressen_lang(msg.get_all("Cc") or []),
            "antwort_an": _adressen_lang(msg.get_all("Reply-To") or []),
            "betreff": W.dekodieren(msg.get("Subject", "")) or "",
            "zeit": _zeitpunkt(msg.get("Date", "")),
            "message_id": (msg.get("Message-Id") or "").strip(),
            "in_antwort_auf": (msg.get("In-Reply-To") or "").strip(),
            "abmelden": _abmeldeweg(msg.get("List-Unsubscribe") or ""),
            "gelesen": "\\seen" in flaggen, "markiert": "\\flagged" in flaggen,
            "beantwortet": "\\answered" in flaggen, "entwurf": "\\draft" in flaggen,
            "groesse": int((re.search(r"RFC822\.SIZE\s+(\d+)", rand) or [0, 0])[1] or 0)
                       if re.search(r"RFC822\.SIZE\s+(\d+)", rand) else 0,
            "kopfzeilen": _kopfzeilen_liste(msg),
            "text": text[:TEXT_GRENZE],
            "html": html[:TEXT_GRENZE * 2],
            # 🔑 „There is an HTML view" means one that shows something. Where
            # the filter kept nothing, the switch to it would lead to a blank page.
            "hat_text": bool(text), "hat_html": bool(roh_html and html),
            "fern_blockiert": blockiert,
            "links_verdacht": [l for l in links if l.get("warnung")][:12],
            "links_gesamt": len(links),
            "anhaenge": anhaenge,
            "phishing": urteil.get("phishing") or "",
            "klasse": urteil.get("klasse") or "",
            "grund": urteil.get("grund") or "",
        }

    def roh(self, ordner: str, uid: int) -> bytes:
        """The whole mail as it lies on the server — for „show source"."""
        self.waehle(ordner)
        typ, daten = self.m.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if typ != "OK":
            return b""
        for el in daten or []:
            if isinstance(el, tuple) and len(el) >= 2:
                return el[1] or b""
        return b""

    # ── Acting ──────────────────────────────────────────────────────
    # 🔴 An IMAP command is ONE line, and servers cut it off at a few kilobytes.
    #    Measured: 12.000 mails written out one by one are **60 KB** — far over
    #    any limit, so „mark the whole folder" would simply have failed. Written
    #    as RANGES the same folder is **7 bytes** (`1:12000`).
    #
    #    But ranges alone are not enough: a scattered selection (a search, a
    #    folder with holes) still came to 15–16 KB. So the set is also cut into
    #    pieces — and the criterion is the LENGTH of the line, not a number of
    #    mails, because that is what the server actually limits.
    _SATZ_MAX = 900

    @staticmethod
    def _uid_satz(uids) -> str:
        """`[1,2,3,7,9,10]` → `1:3,7,9:10`."""
        zahlen = sorted({int(u) for u in uids})
        teile, i = [], 0
        while i < len(zahlen):
            j = i
            while j + 1 < len(zahlen) and zahlen[j + 1] == zahlen[j] + 1:
                j += 1
            teile.append(str(zahlen[i]) if i == j
                         else "%d:%d" % (zahlen[i], zahlen[j]))
            i = j + 1
        return ",".join(teile)

    @classmethod
    def _uid_stuecke(cls, uids):
        """The set in pieces, each one short enough for a single command."""
        zahlen = sorted({int(u) for u in uids})
        stueck, raus = [], []
        for u in zahlen:
            stueck.append(u)
            if len(cls._uid_satz(stueck)) > cls._SATZ_MAX:
                raus.append(stueck[:-1] or stueck)
                stueck = [] if stueck[:-1] else []
                if not raus[-1] or raus[-1][-1] != u:
                    stueck = [u]
        if stueck:
            raus.append(stueck)
        return [x for x in raus if x]

    def flagge(self, ordner: str, uids: list, flagge: str, an: bool) -> int:
        """Set or clear a flag. This is the ONLY place that makes a mail read —
        and it is reached only by a deliberate action or by the setting that says
        so."""
        if not uids:
            return 0
        self.waehle(ordner, schreiben=True)
        getan = 0
        for stueck in self._uid_stuecke(uids):
            typ, _ = self.m.uid("STORE", self._uid_satz(stueck),
                                "+FLAGS" if an else "-FLAGS", "(%s)" % flagge)
            if typ != "OK":
                # 🔑 What is already set stays set. The number says how far it
                #    got, instead of reporting a partial success as a failure.
                log("STORE refused after %d mail(s)" % getan)
                break
            getan += len(stueck)
        return getan

    def verschieben_viele(self, ordner: str, uids: list, ziel: str) -> dict:
        """Move — with MOVE where the server can do it, otherwise copy, tick off,
        expunge.

        🔴 The watchman's rule holds unchanged: `\\Deleted` is set only AFTER a
        confirmed copy. If the copy fails, the mail stays where it is. And
        `EXPUNGE` runs only in the source folder of this very move.
        """
        if not uids or not ziel:
            return {"ok": False, "n": 0, "text": txt("k.kein_ziel")}
        self.waehle(ordner, schreiben=True)
        getan, kopiert = 0, False
        for stueck in self._uid_stuecke(uids):
            satz = self._uid_satz(stueck)
            if self.kann("MOVE"):
                typ, _ = self.m.uid("MOVE", satz, self._zitat(ziel))
                if typ == "OK":
                    getan += len(stueck)
                    continue
                log("MOVE abgelehnt, es geht per Kopie weiter")
            typ, _ = self.m.uid("COPY", satz, self._zitat(ziel))
            if typ != "OK":
                # 🔴 The watchman's rule, unchanged: `\\Deleted` is set ONLY
                #    after a confirmed copy. If the copy fails, this piece stays
                #    where it is — and whatever already went across stays across.
                if not getan:
                    return {"ok": False, "n": 0, "text": txt("k.kopie_fehl")}
                break
            self.m.uid("STORE", satz, "+FLAGS", "(\\Deleted)")
            kopiert = True
            getan += len(stueck)
        if kopiert:
            self.m.expunge()
        return {"ok": bool(getan), "n": getan}

    def anhaengen(self, ordner: str, roh: bytes, flaggen: str = "") -> bool:
        """Put a mail INTO a folder — the sent copy, a draft. The folder is created
        if it is missing, because a provider without a drafts folder must not cost
        the text that was written."""
        try:
            self.m.create(self._zitat(ordner))
        except Exception:
            pass
        try:
            typ, _ = self.m.append(self._zitat(ordner), flaggen or None,
                                   imaplib.Time2Internaldate(time.time()), roh)
            return typ == "OK"
        except Exception as e:
            log("Anhängen in %s fehlgeschlagen: %s" % (ordner, str(e)[:140]))
            return False

    def ordner_ziehen(self, alt: str, ziel: str) -> dict:
        """Move a folder — with its mail and with everything hanging under it.

        🔑 IMAP has ONE command for this, and it is not „copy every mail":
        `RENAME` moves the folder, its messages and its subfolders in a single
        step, and the numbers stay what they were. Copying would mean thousands of
        mails over the wire and a window in which the same post lies twice.

        🔴 A special folder stays where it is. Sent, Drafts, Trash and Junk are
        not folders somebody sorted into, they are POSITIONS — every mail program
        on this mailbox looks for them where they are, and the watchman files
        against them too.
        """
        alle = self.ordner_liste()
        if alt not in alle or alt == "INBOX":
            return {"ok": False, "text": txt("k.ordner_fort")}
        rollen = {o["name"]: o["rolle"] for o in self.baum()}
        if rollen.get(alt):
            return {"ok": False, "text": txt("k.ordner_fest")}
        t = self.trenner
        if ziel and ziel not in alle:
            return {"ok": False, "text": txt("k.ordner_fort")}
        if ziel == alt or (ziel and ziel.startswith(alt + t)):
            # 🔴 Into itself, or into one of its own children: the server would
            # refuse it — but not before it had done half of it.
            return {"ok": False, "text": txt("k.ordner_in_sich")}
        kurz = alt.rsplit(t, 1)[-1]
        if ziel:
            neu = ziel + t + kurz
        else:
            # The top level of the own folders: whatever prefix the shallowest of
            # them carries. Asked, not assumed — on one server that is „INBOX.",
            # on the next it is nothing at all.
            eigen = [o for o in alle if o != "INBOX" and not rollen.get(o)]
            flach = min((o.count(t) for o in eigen), default=0)
            muster = next((o for o in eigen if o.count(t) == flach), "")
            wurzel = muster.rsplit(t, 1)[0] if t in muster else ""
            neu = (wurzel + t + kurz) if wurzel else kurz
        if neu == alt:
            return {"ok": False, "text": txt("k.ordner_schon_da")}
        if neu in alle:
            return {"ok": False, "text": txt("k.ordner_name_belegt")}
        mails = self.zaehlen(alt)[0]
        kinder = [o for o in alle if o.startswith(alt + t)]
        typ, _ = self.m.rename(self._zitat(alt), self._zitat(neu))
        if typ != "OK":
            return {"ok": False, "text": txt("k.ordner_ging_nicht")}
        # A subscription does not travel with every server — so it is renewed,
        # for the folder and for every child that came along.
        for name in [alt] + kinder:
            try:
                self.m.unsubscribe(self._zitat(name))
            except Exception:
                pass
        for name in [neu] + [neu + o[len(alt):] for o in kinder]:
            try:
                self.m.subscribe(self._zitat(name))
            except Exception:
                pass
        self.baum_stand = (0.0, [])          # the tree is a different one now
        if self.ordner == alt or self.ordner.startswith(alt + t):
            self.ordner = ""                 # whatever was selected is not there
        return {"ok": True, "alt": alt, "neu": neu, "mails": mails,
                "kinder": len(kinder), "trenner": t}

    def ordner_neu(self, pfad: str) -> dict:
        """Create a folder — under the inbox, with the server's separator, and the
        name in modified UTF-7 so an umlaut arrives as an umlaut."""
        pfad = (pfad or "").strip().strip("/")
        if not pfad:
            return {"ok": False, "text": txt("k.kein_name")}
        voll = "INBOX" + self.trenner + utf7_kodieren(
            pfad.replace("/", self.trenner))
        try:
            self.m.create(self._zitat(voll))
            self.m.subscribe(self._zitat(voll))
        except Exception as e:
            return {"ok": False, "text": str(e)[:160]}
        self.baum_stand = (0.0, [])
        return {"ok": True, "name": voll, "text": txt("k.ordner_da", o=pfad)}

    def ordner_fort(self, name: str, mit_inhalt: bool = False) -> dict:
        """Delete a folder, and say beforehand what that costs.

        🔴 This is the one action in the whole client that cannot be taken back.
        `DELETE` takes the mail with it; there is no trash for a folder. So:
        children are refused (delete them yourself, one at a time, and see every
        count), a folder that still holds mail is refused until the caller has
        been told the number and says yes, and a special folder is never deleted
        at all — every mail program on this mailbox expects it to be there.
        """
        alle = self.ordner_liste()
        if name not in alle or name == "INBOX":
            return {"ok": False, "text": txt("k.ordner_fort")}
        rollen = {o["name"]: o["rolle"] for o in self.baum()}
        if rollen.get(name):
            return {"ok": False, "text": txt("k.ordner_fest")}
        t = self.trenner
        kinder = [o for o in alle if o.startswith(name + t)]
        if kinder:
            return {"ok": False, "text": txt("k.ordner_hat_kinder", n=len(kinder)),
                    "kinder": len(kinder)}
        mails = self.zaehlen(name)[0]
        if mails and not mit_inhalt:
            # Not a refusal — a QUESTION with the number in it. The page asks it
            # and comes back with the answer.
            return {"ok": False, "frage": True, "mails": mails,
                    "text": txt("k.ordner_nicht_leer", n=mails)}
        try:
            self.m.unsubscribe(self._zitat(name))
        except Exception:
            pass
        typ, _ = self.m.delete(self._zitat(name))
        if typ != "OK":
            return {"ok": False, "text": txt("k.ordner_fort_ging_nicht")}
        self.baum_stand = (0.0, [])
        if self.ordner == name:
            self.ordner = ""
        return {"ok": True, "name": name, "mails": mails,
                "text": txt("k.ordner_ist_fort",
                            o=utf7_dekodieren(name.rsplit(t, 1)[-1]), n=mails)}


# ── Small helpers ───────────────────────────────────────────────────────
def _zeitpunkt(datum: str) -> str:
    try:
        return email.utils.parsedate_to_datetime(datum).astimezone().isoformat(
            timespec="seconds")
    except Exception:
        return ""


def _intern_zeit(s: str) -> str:
    """The server's own delivery date — the fallback when a mail brings no `Date`
    or an unreadable one. Better the wrong kind of date than an empty column."""
    try:
        teile = imaplib.Internaldate2tuple(('INTERNALDATE "%s"' % s).encode())
        if not teile:
            return ""
        return datetime.fromtimestamp(time.mktime(teile)).astimezone().isoformat(
            timespec="seconds")
    except Exception:
        return ""


def _adressen_kurz(felder: list) -> str:
    namen = []
    for f in felder or []:
        for name, adresse in email.utils.getaddresses([W.dekodieren(str(f))]):
            namen.append(name or adresse)
    return ", ".join(n for n in namen if n)[:200]


def _adressen_lang(felder: list) -> list:
    raus = []
    for f in felder or []:
        for name, adresse in email.utils.getaddresses([W.dekodieren(str(f))]):
            if adresse:
                raus.append({"name": name or "", "adresse": adresse})
    return raus[:60]


def _strang_schluessel(msg) -> str:
    """Which conversation a mail belongs to: the FIRST Message-Id of its
    references chain, otherwise its own. Cheap, and correct for every mail
    program that keeps the chain — which is all of them."""
    kette = (msg.get("References") or "").split()
    if kette:
        return kette[0].strip("<>")[:120]
    antwort = (msg.get("In-Reply-To") or "").strip()
    if antwort:
        return antwort.strip("<>")[:120]
    return (msg.get("Message-Id") or "").strip().strip("<>")[:120]


# Headers that say something to a human. The rest is transport noise and stands
# in the source view, which is one click away.
KOPF_ZEIGEN = ("From", "To", "Cc", "Reply-To", "Date", "Subject", "Message-Id",
               "In-Reply-To", "References", "Return-Path", "Sender",
               "Delivered-To", "List-Id", "List-Unsubscribe", "Precedence",
               "Auto-Submitted", "X-Mailer", "User-Agent", "Organization",
               "Content-Type", "Authentication-Results", "Received-SPF",
               "DKIM-Signature", "X-Spam-Status", "X-Spam-Score", "Importance",
               "X-Priority")


def _kopfzeilen_liste(msg) -> list:
    raus = []
    for name in KOPF_ZEIGEN:
        for wert in msg.get_all(name) or []:
            wert = W.dekodieren(str(wert)).replace("\r", " ").replace("\n", " ")
            # A DKIM signature is 400 characters of base64 — it says „there is
            # one" and nothing more, so that is what is shown.
            if name.lower() == "dkim-signature":
                wert = re.sub(r"b=[^;]+", "b=…", wert)
            raus.append([name, wert[:400]])
    return raus


def anhang_und_inline(struct, teile=None) -> tuple:
    """(attachments, inline images) of one mail — ONE place decides which is which.

    🔴 A LOGO IS NOT AN ATTACHMENT. `anhaenge_der_mail()` takes everything with a
    file name, which is right for the document index: it wants to find every file.
    For the reader it is wrong — a newsletter builds its layout out of a dozen
    inline images, and a mail that announces „12 attachments" and then hands out
    spacer graphics is a mail program nobody trusts.

    🔴 And the list has to count it the same way as the reading pane. It did not
    for a while: the pane showed one attachment, the row showed two. Hence this
    function, called from BOTH — the same lesson as with the mailbox choice in the
    page (one place, not four).
    """
    if teile is None:
        teile = W._teile(struct) if struct else []
    inline = [{"id": t["id"], "nr": t["nr"], "k": t["kodierung"],
               "m": ("%s/%s" % (t["typ"], t["subtyp"])).lower()}
              for t in teile
              if t.get("id") and t["typ"].upper() == "IMAGE"]
    drin = {t["nr"] for t in teile
            if t.get("id") and t.get("verfuegung") == "inline"}
    anhaenge = [a for a in (W.anhaenge_der_mail(struct) if struct else [])
                if a["t"] not in drin]
    return anhaenge, inline


def _erster_text(teile: list, art: str) -> dict:
    """The first body part of a kind — and body means: no file name and not
    declared an attachment. A text/plain WITH a file name is a text file someone
    sent, not the letter."""
    for t in teile or []:
        if t["typ"].upper() != "TEXT" or t["subtyp"].lower() != art:
            continue
        if (t.get("name") or "").strip() or t.get("verfuegung") == "attachment":
            continue
        return t
    return {}


def _stuecke_je_uid(daten) -> list:
    """(UID, bytes) out of an answer that carries several mails at once.

    🔴 The UID stands in the piece BEFORE the literal — and with some servers in
    the one after it. Both are read; the same two lines that `_koepfe_lesen()`
    needs, for the same reason."""
    raus, stuecke = [], list(daten or [])
    for i, el in enumerate(stuecke):
        if not (isinstance(el, tuple) and len(el) >= 2):
            continue
        nach = (stuecke[i + 1]
                if i + 1 < len(stuecke) and isinstance(stuecke[i + 1], bytes) else b"")
        rand = ((el[0] or b"") + b" " + nach).decode("utf-8", "replace")
        t = re.search(r"UID\s+(\d+)", rand)
        if t:
            raus.append((int(t.group(1)), el[1] or b""))
    return raus


def _auszug_aus(roh: bytes, kodierung: str, zeichensatz: str, ist_html: bool) -> str:
    """A readable line or two out of the first bytes of a body part.

    🔴 The piece is CUT OFF by design, and that breaks both transfer encodings in
    its own way: base64 needs a length divisible by four, quoted-printable must
    not end in the middle of an `=XX`. Trimming the tail costs two lines and is
    the difference between an excerpt and an empty row."""
    k = (kodierung or "").upper()
    if k == "BASE64":
        sauber = re.sub(rb"[^A-Za-z0-9+/=]", b"", roh or b"")
        roh = sauber[:len(sauber) - (len(sauber) % 4)]
    elif k == "QUOTED-PRINTABLE":
        roh = re.sub(rb"=[0-9A-Fa-f]?$", b"", roh or b"")
    gepackt = W.teil_entpacken(roh, k)
    # 🔴 And the same again one level up: a piece cut at byte 900 can end in the
    # middle of a CHARACTER. Decoded as it stands, an umlaut turns into two
    # question marks at the end of every excerpt — so the tail is shortened
    # until what is left decodes.
    for _ in range(4):
        try:
            gepackt.decode(zeichensatz or "utf-8")
            break
        except UnicodeDecodeError:
            gepackt = gepackt[:-1]
        except LookupError:
            break
    text = _text_dekodieren(gepackt, zeichensatz)
    if ist_html:
        text = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", text)
        text = _html.unescape(re.sub(r"<[^>]+>", " ", text))
    zeilen = []
    for zeile in text.splitlines():
        zeile = zeile.strip()
        # Quoted passages and the signature are the part of a letter that is
        # least worth two lines in a list.
        if not zeile or zeile.startswith(">") or zeile in ("--", "-- "):
            continue
        zeilen.append(zeile)
        if sum(len(z) for z in zeilen) > AUSZUG_ZEICHEN * 2:
            break
    ganz = re.sub(r"\s+", " ", " ".join(zeilen)).strip()
    return ganz[:AUSZUG_ZEICHEN]


def _text_dekodieren(roh: bytes, zeichensatz: str) -> str:
    """Bytes to text — with the charset the mail itself names.

    🔴 Decoding everything as UTF-8 is the classic: a Latin-1 mail then arrives
    with „Gr��e" and looks like a broken program. The mail says what it is; if it
    lies or says nothing, the usual suspects are tried in order.
    """
    if not roh:
        return ""
    kandidaten = [zeichensatz] if zeichensatz else []
    kandidaten += ["utf-8", "cp1252", "iso-8859-15", "iso-8859-1"]
    for satz in kandidaten:
        if not satz:
            continue
        try:
            return roh.decode(satz)
        except (LookupError, UnicodeDecodeError):
            continue
    return roh.decode("utf-8", "replace")


def _abmeldeweg(kopf: str) -> str:
    """The unsubscribe link out of `List-Unsubscribe` — only the http form, never
    the mailto one. Sending is an action, and an action stays with the reader."""
    for teil in re.findall(r"<([^>]+)>", kopf or ""):
        if teil.lower().startswith(("http://", "https://")):
            return teil[:400]
    return ""


def groesse_kurz(b: int) -> str:
    b = int(b or 0)
    for grenze, name in ((1024 ** 3, "GB"), (1024 ** 2, "MB"), (1024, "kB")):
        if b >= grenze:
            return ("%.1f %s" % (b / grenze, name)).replace(".0 ", " ")
    return "%d B" % b


# ── HTML mail: three walls, not one ──────────────────────────────────────
# 🔑 Foreign HTML is displayed here, so the question is not „is my filter good"
# but „what holds when it is not". Three walls, independent of each other:
#   1. this cleaner, an ALLOW-list — what is not named does not get through.
#      A block list would have to know `onerror`, `onanimationstart`, `srcset`
#      and whatever comes next; an allow list does not care.
#   2. a `Content-Security-Policy` in the frame: `script-src 'none'`, and images
#      only from where they are allowed to come.
#   3. the `sandbox` attribute on the frame, without `allow-scripts`. The browser
#      then executes nothing, whatever ends up in there.
# Wall 1 can have a hole. All three at once is a different question.
from html.parser import HTMLParser

ERLAUBTE_TAGS = {
    "a", "abbr", "address", "b", "bdi", "bdo", "big", "blockquote", "br",
    "caption", "center", "cite", "code", "col", "colgroup", "dd", "del", "dfn",
    "div", "dl", "dt", "em", "figcaption", "figure", "font", "h1", "h2", "h3",
    "h4", "h5", "h6", "hr", "i", "img", "ins", "kbd", "li", "mark", "nobr",
    "ol", "p", "pre", "q", "s", "samp", "small", "span", "strike", "strong",
    "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "time", "tr",
    "tt", "u", "ul", "var", "wbr",
}
# 🔴 EVERY void element of HTML, not just the ones a letter uses for layout.
# This set carries the whole weight of the rule below: a void element never has
# an end tag, so it must never open a region that waits for one. `meta` was
# missing here and stood in the silent list at the same time — and with that one
# `<meta http-equiv="Content-Type">` at the top of nearly every newsletter the
# counter went to 1 and never came back. Everything after it was dropped; only
# the style block survived, because it is read before the counter is asked. The
# letter arrived complete and the reader saw an empty page (5.0.1).
LEERE_TAGS = {"area", "base", "basefont", "br", "col", "embed", "frame", "hr",
              "img", "input", "isindex", "keygen", "link", "meta", "param",
              "source", "track", "wbr"}
# Tags whose CONTENT has to go as well — text inside <script> is code, text
# inside <title> is not part of the letter.
# 🔑 `style` is NOT in here. A newsletter carries its layout in a style block,
# and throwing it away makes every mail look broken. So the block stays and its
# CONTENT is cleaned — see `_css_saeubern()`.
# 🔑 And only tags whose content is really not for reading belong in here. `head`,
# `body`, `form`, `button` and `option` were in it and are not: their content IS
# the letter in a great many mails. A tag that is simply not allowed disappears
# on its own — what is inside it stays readable.
STILLE_TAGS = {"script", "title", "noscript", "template", "svg", "math",
               "object", "applet", "iframe", "frameset", "audio", "video",
               "textarea", "select"}
ERLAUBTE_ATTRIBUTE = {
    "*": {"style", "class", "title", "dir", "lang", "align", "valign",
          "bgcolor", "color", "width", "height"},
    "a": {"href", "name"},
    "img": {"src", "alt", "border", "hspace", "vspace"},
    "table": {"border", "cellpadding", "cellspacing", "summary"},
    "td": {"colspan", "rowspan", "nowrap"},
    "th": {"colspan", "rowspan", "nowrap", "scope"},
    "col": {"span"}, "colgroup": {"span"},
    "ol": {"start", "type"}, "ul": {"type"}, "li": {"value"},
    "font": {"face", "size"},
    "blockquote": {"cite"}, "time": {"datetime"},
}
LEERES_BILD = ("data:image/gif;base64,"
               "R0lGODlhAQABAIAAAP///wAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==")
BOESE_SCHEMA = re.compile(r"^\s*(javascript|vbscript|data:text|data:application|"
                          r"file|about|blob)", re.I)
CSS_BOESE = re.compile(r"(@import|expression\s*\(|behavior\s*:|-moz-binding|"
                       r"javascript:)", re.I)
CSS_URL = re.compile(r"url\(\s*['\"]?(?P<u>[^)'\"]+)['\"]?\s*\)", re.I)


class _Saeuberer(HTMLParser):
    """Rebuilds the mail's HTML out of what is allowed — it does not repair the
    original. Whatever is not in the allow list simply does not appear in the
    output, and its text does."""

    def __init__(self, bilder: bool, inline: list, ordner: str, uid: int):
        super().__init__(convert_charrefs=True)
        self.bilder = bool(bilder)
        self.cid = {str(t.get("id") or "").strip("<>"): t for t in inline or []}
        self.ordner, self.uid = ordner, uid
        self.raus = []
        self.blockiert = 0
        self.links = []
        self.still = 0          # depth inside a tag whose content is dropped
        self.stille_namen = []  # which tags hold that silence, innermost last
        self.stapel = []
        self.stil_puffer = None  # not None while inside a <style> block

    # — text —
    def handle_data(self, daten):
        if self.stil_puffer is not None:
            self.stil_puffer.append(daten)
            return
        if self.still:
            return
        self.raus.append(_html.escape(daten, quote=False))

    # — tags —
    def handle_starttag(self, tag, attrs, leer=False):
        tag = (tag or "").lower()
        if tag == "style":
            self.stil_puffer = []
            return
        # 🔴 A void element and a self-closed tag NEVER open a region. Written
        # the other way round, a single `<meta …/>` silences the whole rest of
        # the letter, because the end tag it waits for cannot exist.
        if leer or tag in LEERE_TAGS:
            if tag in STILLE_TAGS or tag not in ERLAUBTE_TAGS:
                return
        elif tag in STILLE_TAGS:
            self.still += 1
            self.stille_namen.append(tag)
            return
        if self.still or tag not in ERLAUBTE_TAGS:
            return
        stuecke = []
        if tag == "img":
            quelle, zaehlt = self._bildquelle(dict(attrs))
            if zaehlt:
                self.blockiert += 1
            stuecke.append('src="%s"' % _html.escape(quelle, quote=True))
            if zaehlt:
                stuecke.append('class="pw-fern" title="%s"'
                               % _html.escape(txt("k.bild_blockiert"), quote=True))
        for name, wert in attrs:
            name = (name or "").lower()
            wert = wert if wert is not None else ""
            if name.startswith("on") or name in ("src", "srcset", "background",
                                                 "poster", "formaction",
                                                 "lowsrc", "dynsrc", "usemap"):
                continue
            if name not in ERLAUBTE_ATTRIBUTE.get("*", set()) and \
                    name not in ERLAUBTE_ATTRIBUTE.get(tag, set()):
                continue
            if name == "style":
                wert = self._stil(wert)
                if not wert:
                    continue
            if name == "href":
                wert = self._verweis(wert, attrs)
                if not wert:
                    continue
            if name == "class":
                wert = re.sub(r"[^\w\s-]", "", wert)[:200]
            stuecke.append('%s="%s"' % (name, _html.escape(str(wert), quote=True)))
        if tag == "a":
            # 🔴 The frame is sandboxed. Without `target` a click would try to
            # navigate the frame itself and — correctly — be blocked: the link
            # would look broken. With it, the reader's browser opens the page, and
            # `noopener` keeps the opened page from reaching back.
            stuecke.append('target="_blank" rel="noopener noreferrer nofollow"')
        schluss = " /" if (leer or tag in LEERE_TAGS) else ""
        self.raus.append("<%s%s%s>" % (tag, (" " + " ".join(stuecke)) if stuecke else "",
                                       schluss))
        if not leer and tag not in LEERE_TAGS:
            self.stapel.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs, leer=True)

    def handle_endtag(self, tag):
        tag = (tag or "").lower()
        if tag == "style":
            css = self._css("".join(self.stil_puffer or ()))
            self.stil_puffer = None
            if css:
                self.raus.append("<style>%s</style>" % css)
            return
        # Closed by NAME, not by counting: a mail that forgets a `</script>` must
        # not take the rest of the letter with it, and a stray `</iframe>` must
        # not lift a silence that was never set.
        if tag in STILLE_TAGS:
            if tag in self.stille_namen:
                while self.stille_namen:
                    if self.stille_namen.pop() == tag:
                        break
                    self.still -= 1
                self.still -= 1
            return
        if self.still or tag not in ERLAUBTE_TAGS or tag in LEERE_TAGS:
            return
        if tag in self.stapel:
            # Close everything that was opened inside — mail HTML is full of tags
            # nobody closed, and a stray </div> must not tear the page apart.
            while self.stapel:
                offen = self.stapel.pop()
                self.raus.append("</%s>" % offen)
                if offen == tag:
                    break

    def handle_comment(self, daten):
        pass                    # a comment carries nothing a reader needs

    def handle_decl(self, daten):
        pass

    def handle_pi(self, daten):
        pass

    # — the three interesting attributes —
    def _bildquelle(self, karte: dict) -> tuple:
        """Returns (source, was it blocked). `cid:` goes to our own endpoint, a
        `data:` image through, everything remote only when it is allowed."""
        roh = str(karte.get("src") or "").strip()
        if roh.lower().startswith("cid:"):
            kennung = roh[4:].strip().strip("<>")
            if kennung in self.cid:
                return ("/api/klient/bild?ordner=%s&uid=%d&cid=%s"
                        % (urllib.parse.quote(self.ordner), self.uid,
                           urllib.parse.quote(kennung)), False)
            return LEERES_BILD, False
        if roh.lower().startswith("data:image/"):
            return roh[:200000], False
        if BOESE_SCHEMA.match(roh):
            return LEERES_BILD, False
        if roh.lower().startswith(("http://", "https://", "//")):
            if self.bilder:
                return roh[:2000], False
            return LEERES_BILD, True
        return LEERES_BILD, False

    def _css(self, roh: str) -> str:
        """A style block, cleaned.

        Removed: `@import` (fetches from outside), `expression(` and `behavior:`
        (old Internet Explorer could run code in them), `-moz-binding`. And every
        `url(...)` that points outward, as long as remote content is off — a
        background image is a tracking pixel just as much as an `<img>`.
        """
        roh = str(roh or "")[:40000]
        roh = re.sub(r"@import[^;}]*;?", "", roh, flags=re.I)
        roh = re.sub(r"(expression\s*\(|behavior\s*:|-moz-binding\s*:)[^;}]*",
                     "", roh, flags=re.I)
        if not self.bilder:
            def fern(t):
                if t.group("u").lower().startswith("data:"):
                    return t.group(0)
                self.blockiert += 1
                return "none"
            roh = CSS_URL.sub(fern, roh)
        # A style block cannot contain „</style>" — the parser ended it there. What
        # is left over is the one character that could open a tag again.
        return roh.replace("<", "")

    def _stil(self, wert: str) -> str:
        wert = str(wert or "")
        if CSS_BOESE.search(wert):
            return ""
        if not self.bilder:
            treffer = CSS_URL.search(wert)
            if treffer and not treffer.group("u").lower().startswith("data:"):
                self.blockiert += 1
                wert = CSS_URL.sub("none", wert)
        return wert[:1200]

    def _verweis(self, wert: str, attrs: list) -> str:
        wert = str(wert or "").strip()
        if BOESE_SCHEMA.match(wert):
            return ""
        if not re.match(r"^(https?:|mailto:|#)", wert, re.I):
            return ""
        self.links.append({"ziel": wert[:500], "warnung": ""})
        return wert[:2000]

    def ergebnis(self) -> str:
        while self.stapel:
            self.raus.append("</%s>" % self.stapel.pop())
        return "".join(self.raus)


def _wirt(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def links_pruefen(links: list, text_je_link: dict) -> list:
    """Which link says something other than where it goes.

    🔑 Exactly the check the Linkwache was built for, only inside the mail: if the
    visible text is itself a domain and it is not the one behind the link, that is
    the oldest trick there is. And a punycode host („xn--") can look like any
    brand at all.
    """
    raus = []
    for l in links:
        ziel = l.get("ziel") or ""
        wirt = _wirt(ziel)
        sichtbar = (text_je_link.get(ziel) or "").strip()
        warnung = ""
        t = re.search(r"\b((?:[\w-]+\.)+[a-z]{2,})\b", sichtbar, re.I)
        if t and wirt:
            gezeigt = t.group(1).lower()
            if not (wirt == gezeigt or wirt.endswith("." + gezeigt)
                    or gezeigt.endswith("." + wirt)):
                warnung = "text"
        if wirt.startswith("xn--") or ".xn--" in wirt:
            warnung = warnung or "punycode"
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", wirt):
            warnung = warnung or "ip"
        raus.append({"ziel": ziel, "wirt": wirt, "text": sichtbar[:120],
                     "warnung": warnung})
    return raus


def _sichtbar(html: str) -> bool:
    """Is there anything in there a reader would see — text or a picture?"""
    if re.search(r"(?i)<img\b", html or ""):
        return True
    ohne = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html or "")
    return bool(_html.unescape(re.sub(r"<[^>]+>", " ", ohne)).strip())


def html_saeubern(roh: str, bilder: bool, inline: list, ordner: str,
                  uid: int) -> tuple:
    """(clean HTML, number of blocked remote references, links)."""
    s = _Saeuberer(bilder, inline, ordner, uid)
    try:
        s.feed(roh[:TEXT_GRENZE * 4])
        s.close()
    except Exception as e:
        log("HTML nicht säuberbar: %s" % str(e)[:120])
        return _html.escape(roh[:4000]), 0, []
    saubere = s.ergebnis()
    # 🔴 The wall behind the rule. A filter that rebuilds a letter out of the
    # allowed can, if it is wrong about one tag, hand back a page with nothing on
    # it — and a blank reading pane looks exactly like a mail with no content.
    # So the result is ASKED whether anything readable is in it, and where the
    # answer is no and the original did have text, the letter is shown as text
    # rather than not at all. Measured, not assumed (5.0.1).
    if not _sichtbar(saubere) and _sichtbar(re.sub(r"(?is)<(script|style)\b.*?</\1>",
                                                  " ", roh)):
        log("HTML-Säuberung ergab nichts Sichtbares — Textfassung gezeigt")
        return "", 0, []
    # The visible text per link — for the comparison „says A, goes to B".
    text_je_link = {}
    for treffer in re.finditer(r'<a\b[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
                               saubere, re.S | re.I):
        ziel = _html.unescape(treffer.group(1))
        sichtbar = re.sub(r"<[^>]+>", " ", treffer.group(2))
        text_je_link.setdefault(ziel, _html.unescape(sichtbar).strip())
    return saubere, s.blockiert, links_pruefen(s.links, text_je_link)


def text_zu_html(text: str) -> str:
    """Plain text as a readable letter: links clickable, quoted passages as
    quotes, the signature set apart. Nothing invented, only marked up."""
    raus, zitat = [], 0
    for zeile in (text or "").splitlines():
        tiefe = 0
        rest = zeile
        while rest.startswith((">", " >")):
            rest = rest.lstrip(" ")[1:]
            tiefe += 1
        while zitat > tiefe:
            raus.append("</blockquote>")
            zitat -= 1
        while zitat < tiefe:
            raus.append('<blockquote class="pw-zitat">')
            zitat += 1
        raus.append(_verlinken(_html.escape(rest, quote=False)) + "\n")
    raus.append("</blockquote>" * zitat)
    ganz = "".join(raus)
    # „-- " on its own line is the signature separator since the first mail
    # programs. Setting it apart makes a long letter shorter to read.
    return re.sub(r"(?m)^(--\s*)$", r'<span class="pw-sig">\1</span>', ganz, count=1)


def _verlinken(s: str) -> str:
    def ersetze(t):
        url = t.group(0)
        schwanz = ""
        while url and url[-1] in ".,;:!?)]}'\"":
            schwanz, url = url[-1] + schwanz, url[:-1]
        ziel = url if url.lower().startswith(("http://", "https://")) else "https://" + url
        return ('<a href="%s" target="_blank" rel="noopener noreferrer nofollow">%s</a>%s'
                % (_html.escape(ziel, quote=True), url, schwanz))
    s = re.sub(r"(https?://[^\s<>\"]+|www\.[^\s<>\"]+)", ersetze, s)
    return re.sub(r"\b([\w.+-]+@[\w-]+\.[\w.-]+)\b",
                  lambda t: '<a href="mailto:%s">%s</a>' % (t.group(1), t.group(1)), s)


def rahmen(inhalt: str, bilder: bool, dunkel: bool = True) -> str:
    """The document that goes into the frame — with its own policy.

    🔴 The policy is the second wall: `script-src 'none'` means the browser
    refuses every script, however it got in there. And `img-src` decides for the
    browser, not for my filter, whether a tracking pixel may be fetched.
    """
    bild_quellen = "data: 'self'" + (" https: http:" if bilder else "")
    politik = ("default-src 'none'; img-src %s; style-src 'unsafe-inline'; "
               "font-src data:; script-src 'none'; object-src 'none'; "
               "frame-src 'none'; form-action 'none'; base-uri 'none'"
               % bild_quellen)
    # 🔑 The same four colours the page itself uses in that mode — the letter is
    # the only part of the view the browser paints from a stylesheet of OUR
    # making, and a letter on white inside a page on paper shows the seam.
    grund, schrift, leise, akzent = (("#161410", "#f4efe6", "#a99c88", "#e0a458")
                                     if dunkel else
                                     ("#fffdf8", "#2b2015", "#6e6353", "#9a5a17"))
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta http-equiv="Content-Security-Policy" content="%s">'
        '<style>'
        'html,body{margin:0;padding:0}'
        'body{background:%s;color:%s;font:15px/1.55 ui-rounded,"SF Pro Rounded",'
        'system-ui,"Segoe UI",sans-serif;padding:.9rem 1rem 1.4rem;'
        'word-break:break-word;overflow-wrap:anywhere}'
        'a{color:%s}'
        'img{max-width:100%%;height:auto}'
        'img.pw-fern{min-width:18px;min-height:18px;border:1px dashed %s55;'
        'border-radius:4px;background:%s22}'
        'table{max-width:100%%;border-collapse:collapse}'
        'pre{white-space:pre-wrap;font:13px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}'
        # 🔴 The measure applies to PLAIN TEXT only. On a wide screen a line of
        # 200 characters is harder to read, not easier. HTML mail keeps its own
        # width: a newsletter is built for 600 px, and a max-width across it
        # breaks the layout it came with.
        '.pw-text{white-space:pre-wrap;max-width:74ch}'
        '.pw-zitat{margin:.5rem 0;padding:.1rem 0 .1rem .8rem;border-left:2px solid %s55;'
        'color:%s}'
        '.pw-sig{color:%s}'
        'blockquote{margin:.5rem 0;padding:.1rem 0 .1rem .8rem;'
        'border-left:2px solid %s55;color:%s}'
        '</style></head><body>%s</body></html>'
        % (politik, grund, schrift, akzent, akzent, akzent, akzent, leise,
           leise, akzent, leise, inhalt))


# ── One warm connection per mailbox ──────────────────────────────────────
import contextlib


def _zugang(pf_id: str) -> dict:
    for f in (W.postfaecher() if W is not None else []):
        if f["id"] == str(pf_id or ""):
            return f
    return {}


def _schliessen(pf) -> None:
    try:
        pf.__exit__(None, None, None)
    except Exception:
        pass


@contextlib.contextmanager
def briefkasten(pf_id: str):
    """The connection of this mailbox — exactly one, and only one caller at a
    time.

    🔴 An IMAP connection is not thread-safe, and the answer to a command belongs
    to whoever asked. Two requests on one connection at the same time and both
    read each other's answer. The lock is per mailbox, so two mailboxes do not
    wait for each other.
    """
    with _V_SCHLOSS:
        eintrag = _VERBINDUNGEN.setdefault(
            pf_id, {"schloss": threading.Lock(), "pf": None, "zeit": 0.0})
        # Along the way: close every OTHER connection that has been idle too long.
        # A client left open in a browser tab must not hold five sessions on the
        # provider for a week.
        for kennung, e in list(_VERBINDUNGEN.items()):
            if (kennung != pf_id and e["pf"] is not None
                    and time.time() - e["zeit"] > LEERLAUF
                    and not e["schloss"].locked()):
                _schliessen(e["pf"])
                e["pf"] = None
    eintrag["schloss"].acquire()
    try:
        pf = eintrag["pf"]
        if pf is not None and (time.time() - eintrag["zeit"] > LEERLAUF or not pf.lebt()):
            _schliessen(pf)
            pf = None
        if pf is None:
            zug = _zugang(pf_id)
            if not zug:
                raise RuntimeError(txt("a.kein_postfach"))
            pf = Briefkasten(zug)
            pf.__enter__()
            eintrag["pf"] = pf
        eintrag["zeit"] = time.time()
        yield pf
    except (imaplib.IMAP4.abort, imaplib.IMAP4.error, OSError, EOFError):
        _schliessen(eintrag.get("pf"))
        eintrag["pf"] = None
        raise
    finally:
        eintrag["zeit"] = time.time()
        eintrag["schloss"].release()


def tu(pf_id: str, aufgabe):
    """Run one job on the mailbox — and once again if the connection had gone
    stale. A provider hangs up on an idle connection without telling anybody, and
    the reader must not be the one to find out."""
    letzter = None
    for versuch in (1, 2):
        try:
            with briefkasten(pf_id) as kasten:
                return aufgabe(kasten)
        except (imaplib.IMAP4.abort, OSError, EOFError) as e:
            letzter = e
            log("Verbindung erneuern (%d. Versuch): %s" % (versuch, str(e)[:100]))
    raise letzter if letzter else RuntimeError("keine Verbindung")


def verbindungen_schliessen() -> None:
    """Hang up everywhere — for the end of the process and for the test bench."""
    horch_halt()
    with _V_SCHLOSS:
        for e in _VERBINDUNGEN.values():
            if e["pf"] is not None:
                _schliessen(e["pf"])
            e["pf"] = None


# ── The listening post: hearing instead of asking ───────────────────────
# 🔑 It has its OWN connection, and that is not a detail: an IDLE sits on its
# connection for minutes, and the warm one is guarded by a lock that every click
# goes through. Put the listening post on that lock and reading a mail would wait
# for the next new mail. So: one connection, read-only (EXAMINE), no command that
# writes anything — it listens, nothing else.
#
# 🔑 And it belongs to the MAILBOX, not to the browser tab. Three tabs open ask
# ONE post, and that post holds ONE connection at the provider. Nobody waits
# alone.
_HORCH = {}
_H_SCHLOSS = threading.Lock()


def _horch_posten(pf_id: str) -> dict:
    with _H_SCHLOSS:
        posten = _HORCH.get(pf_id)
        if posten is None:
            posten = {"wache": threading.Condition(), "marken": {}, "stand": 0,
                      "wunsch": 0.0, "ordner": {}, "faden": None, "idle": None,
                      "fehler": "", "runden": 0}
            _HORCH[pf_id] = posten
        return posten


def _horch_orte(posten: dict) -> list:
    """Which folders to keep an ear on: the inbox, plus what readers are looking
    at. Interest expires — otherwise an hour of browsing would leave the post
    watching forty folders."""
    jetzt = time.time()
    with posten["wache"]:
        for name, wann in list(posten["ordner"].items()):
            if jetzt - wann > HORCH_FRIST:
                posten["ordner"].pop(name, None)
        wunsch = sorted(posten["ordner"], key=lambda n: -posten["ordner"][n])
    # 🔑 „INBOX" is the one folder name IMAP prescribes, so it needs no lookup —
    # and it has to be in here whatever anybody is looking at: new mail arrives
    # THERE, and the watchman carries it out of there into its folders. Both
    # events are visible in this one folder.
    return (["INBOX"] + [n for n in wunsch if n != "INBOX"])[:HORCH_ORDNER]


def _horch_messen(pf, posten: dict, orte: list) -> bool:
    """One STATUS per folder. Changed anything? Then wake everybody who waits."""
    neu = {}
    for name in orte:
        neu[name] = pf.stand(name)
    with posten["wache"]:
        if all(posten["marken"].get(n) == w for n, w in neu.items()):
            return False
        posten["marken"].update(neu)
        posten["stand"] += 1
        posten["wache"].notify_all()
    return True


def _horch_faden(pf_id: str) -> None:
    """The post itself. It lives as long as somebody is listening — and not a
    minute longer: a browser tab closed at night must not hold a connection at
    the provider until morning."""
    posten = _horch_posten(pf_id)
    pf = None
    try:
        while time.time() - posten["wunsch"] < HORCH_FRIST:
            try:
                if pf is None:
                    zug = _zugang(pf_id)
                    if not zug:
                        posten["fehler"] = txt("a.kein_postfach")
                        return
                    pf = Briefkasten(zug, False)       # cannot write, by build
                    pf.__enter__()
                    posten["idle"] = pf.kann("IDLE")
                    posten["fehler"] = ""
                orte = _horch_orte(posten)
                _horch_messen(pf, posten, orte)
                posten["runden"] += 1
                if posten["idle"]:
                    # Read-only, always. The post watches, it does not touch.
                    pf.waehle(orte[0], False)
                    pf.lauschen(HORCH_FENSTER,
                                lambda: time.time() - posten["wunsch"] < HORCH_FRIST)
                else:
                    # 🔴 A server without IDLE is not a reason to give up — it is
                    # a reason to ask politely. Twenty seconds is still twenty
                    # times closer than the minute the page used to wait.
                    time.sleep(HORCH_TAKT)
            except Exception as e:                      # noqa: BLE001
                posten["fehler"] = str(e)[:140]
                log("Horchposten: %s" % str(e)[:140])
                _schliessen(pf)
                pf = None
                if time.time() - posten["wunsch"] >= HORCH_FRIST:
                    break
                time.sleep(5.0)
    finally:
        _schliessen(pf)
        with _H_SCHLOSS:
            posten["faden"] = None


def _horch_faden_start(posten: dict, pf_id: str) -> None:
    with _H_SCHLOSS:
        faden = posten["faden"]
        if faden is not None and faden.is_alive():
            return
        faden = threading.Thread(target=_horch_faden, args=(pf_id,), daemon=True,
                                 name="horch-%s" % (pf_id or "-"))
        posten["faden"] = faden
    faden.start()


def horch(d: dict) -> dict:
    """Hold this request until something happens — or until the wait is up.

    🔑 The answer has the SAME shape whether it waited twenty milliseconds or
    twenty-five seconds: a counter and the marks of the folders asked about. The
    page compares them itself and decides what is worth refetching. Nothing is
    pushed through a pipe that could break, and a page that cannot reach the post
    at all simply asks again — the old rhythm, only slower and as a fallback.
    """
    pf_id = str(d.get("pf") or "")
    ordner = [str(o) for o in (d.get("ordner") or []) if o][:HORCH_ORDNER]
    try:
        stand = int(d.get("stand") or 0)
    except (TypeError, ValueError):
        stand = 0
    try:
        warte = max(1.0, min(HORCH_WARTE, float(d.get("warte") or HORCH_WARTE)))
    except (TypeError, ValueError):
        warte = HORCH_WARTE
    posten = _horch_posten(pf_id)
    jetzt = time.time()
    with posten["wache"]:
        posten["wunsch"] = jetzt
        for name in ordner:
            posten["ordner"][name] = jetzt
        _horch_faden_start(posten, pf_id)
        # 🔴 Only wait when the reader is up to date. Whoever is behind gets the
        # answer AT ONCE — otherwise a page that missed one round would wait
        # twenty-five seconds for news that is already lying here.
        if posten["stand"] == stand:
            posten["wache"].wait(warte)
        return {"ok": True, "stand": posten["stand"], "horcht": bool(posten["idle"]),
                "marken": {n: list(posten["marken"][n]) for n in ordner
                           if n in posten["marken"]},
                "fehler": posten["fehler"]}


def horch_halt() -> None:
    """Close every listening post — end of process, and the test bench."""
    for posten in list(_HORCH.values()):
        with posten["wache"]:
            posten["wunsch"] = 0.0
            posten["ordner"].clear()
            posten["wache"].notify_all()
    for posten in list(_HORCH.values()):
        faden = posten.get("faden")
        if faden is not None:
            faden.join(timeout=2.0)


# ── Writing and sending ─────────────────────────────────────────────────
def smtp_zugang(pf_id: str) -> dict:
    """The sending route of a mailbox — guessed where it is not set.

    🔴 Guessed, not assumed: the guess is shown on the page and can be overwritten
    there. An outgoing server that silently does not fit is the kind of error that
    only shows up when a mail matters.
    """
    zug = _zugang(pf_id)
    if not zug:
        return {}
    server = str(zug.get("smtp_server") or "").strip()
    if not server:
        imap = str(zug.get("server") or "")
        server = re.sub(r"^imap[.-]?", "smtp.", imap) if imap.startswith("imap") else imap
    try:
        port = int(zug.get("smtp_port") or 0)
    except (TypeError, ValueError):
        port = 0
    art = str(zug.get("smtp_art") or "").strip().lower()
    if not art:
        art = "ssl" if port == 465 else "starttls"
    if not port:
        port = 465 if art == "ssl" else 587
    return {
        "server": server, "port": port, "art": art,
        "benutzer": str(zug.get("smtp_benutzer") or zug.get("adresse") or ""),
        "passwort": str(zug.get("smtp_passwort") or zug.get("passwort") or ""),
        "adresse": str(zug.get("adresse") or ""),
        "absender_name": str(zug.get("absender_name") or ""),
        "geraten": not str(zug.get("smtp_server") or "").strip(),
    }


def smtp_speichern(d: dict) -> dict:
    """Store the sending route — in the same file as the mailbox, with the same
    0600. A second file with a second password would be a second place to forget."""
    pf_id = str(d.get("pf") or "")
    liste = list(W.postfaecher() if W is not None else [])
    eintrag = next((f for f in liste if f["id"] == pf_id), None)
    if not eintrag:
        return {"ok": False, "text": txt("a.kein_postfach")}
    art = str(d.get("art") or "").lower()
    eintrag["smtp_server"] = str(d.get("server") or "").strip()
    try:
        eintrag["smtp_port"] = int(d.get("port") or 0) or 0
    except (TypeError, ValueError):
        eintrag["smtp_port"] = 0
    eintrag["smtp_art"] = art if art in ("starttls", "ssl", "klar") else ""
    eintrag["smtp_benutzer"] = str(d.get("benutzer") or "").strip()
    if str(d.get("passwort") or ""):
        eintrag["smtp_passwort"] = str(d.get("passwort"))
    eintrag["absender_name"] = str(d.get("absender_name") or "").strip()[:120]
    W.pf_waehlen("")
    W.save("postfaecher.json", {"liste": liste}, 0o600)
    return {"ok": True, "text": txt("a.gespeichert"), "smtp": _smtp_kurz(pf_id)}


def _smtp_kurz(pf_id: str) -> dict:
    """What the page may know about the sending route — everything except the
    password. Same rule as for the mailbox: taken in, never handed out."""
    s = smtp_zugang(pf_id)
    if not s:
        return {}
    return {"server": s["server"], "port": s["port"], "art": s["art"],
            "benutzer": s["benutzer"], "absender_name": s["absender_name"],
            "passwort_da": bool(s["passwort"]), "geraten": s["geraten"]}


def _smtp_verbinden(s: dict):
    socket.setdefaulttimeout(30)
    if s["art"] == "ssl":
        verbindung = smtplib.SMTP_SSL(s["server"], s["port"], timeout=30)
    else:
        verbindung = smtplib.SMTP(s["server"], s["port"], timeout=30)
        verbindung.ehlo()
        if s["art"] == "starttls":
            verbindung.starttls()
            verbindung.ehlo()
    if s["passwort"]:
        verbindung.login(s["benutzer"], s["passwort"])
    return verbindung


def smtp_pruefen(d: dict) -> dict:
    """Check the sending route WITHOUT sending anything.

    🔑 Log in and hang up again. A test that sends a mail to prove it works leaves
    a real mail behind in a real mailbox — and on a running system that is exactly
    what must not happen.
    """
    pf_id = str(d.get("pf") or "")
    s = smtp_zugang(pf_id)
    if not s or not s["server"]:
        return {"ok": False, "text": txt("k.smtp_kein_server")}
    for feld in ("server", "port", "art", "benutzer"):
        if d.get(feld):
            s[feld] = int(d[feld]) if feld == "port" else str(d[feld])
    if d.get("passwort"):
        s["passwort"] = str(d["passwort"])
    try:
        verbindung = _smtp_verbinden(s)
        try:
            verbindung.quit()
        except Exception:
            pass
        return {"ok": True, "text": txt("k.smtp_ok", server=s["server"], port=s["port"])}
    except smtplib.SMTPAuthenticationError:
        return {"ok": False, "text": txt("k.smtp_anmeldung")}
    except Exception as e:
        return {"ok": False, "text": txt("k.smtp_fehler", fehler=str(e)[:160])}


def eigene_adressen(pf_id: str) -> set:
    """Every address that is me — the mailboxes plus the personal catalogue.

    Needed for „reply to all": whoever puts himself in the Cc gets his own answer,
    and on the third round the thread has three copies of everything.
    """
    raus = {str((_zugang(pf_id) or {}).get("adresse") or "").lower()}
    for f in (W.postfaecher() if W is not None else []):
        raus.add(str(f.get("adresse") or "").lower())
    try:
        import umbau as _U
        raus.update(_U.eigen_laden()["eigene_adressen"])
    except Exception:
        pass
    return {a for a in raus if a and "@" in a}


def _betreff_praefix(betreff: str, praefix: str) -> str:
    """„Re: Re: Re:" is what happens when everyone adds one. Present already —
    whatever the language — and nothing is added."""
    b = (betreff or "").strip()
    if re.match(r"^\s*(re|aw|antw|antwort|rif|res|réf|fwd|fw|wg|weitergeleitet|tr|i)\s*:",
                b, re.I):
        return b
    return "%s %s" % (praefix, b) if b else praefix


def vorlage(d: dict) -> dict:
    """The prefilled form for reply, reply-to-all and forward.

    Built on the server, because everything it needs is here: the original, the
    own addresses, the signature, the setting for where the quote goes.
    """
    pf_id = str(d.get("pf") or "")
    art = str(d.get("art") or "antwort")
    ordner, uid = str(d.get("ordner") or "INBOX"), int(d.get("uid") or 0)
    einst = einstellungen()
    if art == "neu" or not uid:
        return {"ok": True, "an": "", "kopie": "", "betreff": "",
                "text": _signatur(einst), "art": "neu"}
    m = tu(pf_id, lambda k: k.mail(ordner, uid, bilder=False))
    if not m:
        return {"ok": False, "text": txt("k.mail_weg")}
    meine = eigene_adressen(pf_id)
    antwort_an = [a["adresse"] for a in m.get("antwort_an") or []]
    absender = antwort_an or ([m["adresse"]] if m.get("adresse") else [])
    an, kopie = "", ""
    if art == "weiter":
        betreff = _betreff_praefix(m["betreff"], txt("k.fwd"))
    else:
        betreff = _betreff_praefix(m["betreff"], txt("k.re"))
        an = ", ".join(absender)
        if art == "antwort_alle":
            weitere = []
            for eintrag in (m.get("an") or []) + (m.get("kopie") or []):
                adr = (eintrag.get("adresse") or "").lower()
                if adr and adr not in meine and adr not in [a.lower() for a in absender]:
                    if adr not in [w.lower() for w in weitere]:
                        weitere.append(eintrag["adresse"])
            kopie = ", ".join(weitere)
    zitat = _zitat_bauen(m, art, einst)
    text = ((zitat + "\n" + _signatur(einst)) if einst["zitat"] == "oben"
            else (_signatur(einst) + "\n" + zitat))
    return {
        "ok": True, "art": art, "an": an, "kopie": kopie, "betreff": betreff,
        "text": text,
        "in_antwort_auf": m.get("message_id") or "",
        "quelle": {"ordner": ordner, "uid": uid},
        # Forwarding without the attachment is the classic complaint. They are
        # offered, ticked on, and fetched from the server only when sending.
        "anhaenge": (m.get("anhaenge") or []) if art == "weiter" else [],
    }


def _signatur(einst: dict) -> str:
    sig = str(einst.get("signatur") or "").strip("\n")
    return ("\n-- \n" + sig + "\n") if sig else "\n"


def _zitat_bauen(m: dict, art: str, einst: dict) -> str:
    text = m.get("text") or ""
    if not text and m.get("html"):
        # A mail with HTML only still has to be quotable. The tags come out, the
        # sentences stay.
        text = _html.unescape(re.sub(r"<[^>]+>", "", m["html"]))
        text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip("\n")
    wann = _datum_lesbar(m.get("zeit") or "", einst)
    if art == "weiter":
        kopf = [txt("k.weiter_kopf"),
                "%s: %s <%s>" % (txt("k.von"), m.get("von") or "", m.get("adresse") or ""),
                "%s: %s" % (txt("k.datum"), wann),
                "%s: %s" % (txt("k.betreff"), m.get("betreff") or "")]
        empfaenger = ", ".join(a["adresse"] for a in m.get("an") or [])
        if empfaenger:
            kopf.append("%s: %s" % (txt("k.an"), empfaenger))
        return "\n" + "\n".join(kopf) + "\n\n" + text + "\n"
    kopf = (txt("k.zitat_kopf", datum=wann, wer=m.get("von") or m.get("adresse") or "")
            if einst.get("zitat_kopf") else "")
    zitiert = "\n".join("> " + z for z in text.splitlines())
    return ("\n" + (kopf + "\n" if kopf else "") + zitiert + "\n")


def _datum_lesbar(iso: str, einst: dict) -> str:
    try:
        d = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return iso or ""
    form = "%d.%m.%Y, %H:%M" if einst.get("zeitform") == "24" else "%d.%m.%Y, %I:%M %p"
    return d.strftime(form)


def _adressen_aus(feld: str) -> list:
    """„Name <a@b>, c@d" as a list — through `getaddresses`, not through
    `split(",")`.

    A QUOTED display name may contain a comma (`"Meier, Anna" <a@b>`), and cutting
    on the comma would make two broken recipients out of it. Unquoted, the comma
    really is a separator — which is why `formataddr()` puts the quotes in when a
    name needs them, instead of hoping it does not.
    """
    raus = []
    for name, adresse in email.utils.getaddresses([str(feld or "")]):
        adresse = (adresse or "").strip()
        if adresse and "@" in adresse:
            raus.append(email.utils.formataddr((name.strip(), adresse)))
    return raus


def _nur_adressen(felder: list) -> list:
    return [a for _, a in email.utils.getaddresses(felder) if a and "@" in a]


def _brief_bauen(d: dict, pf_id: str, einst: dict) -> tuple:
    """Assemble the mail. Returns (message, recipients, error)."""
    s = smtp_zugang(pf_id)
    an = _adressen_aus(d.get("an"))
    kopie = _adressen_aus(d.get("kopie"))
    blind = _adressen_aus(d.get("blind"))
    if einst.get("blind_kopie_selbst") and s.get("adresse"):
        blind.append(s["adresse"])
    if not (an or kopie or blind):
        return None, [], txt("k.kein_empfaenger")
    msg = EmailMessage()
    msg["From"] = email.utils.formataddr(
        (s.get("absender_name") or "", s.get("adresse") or ""))
    if an:
        msg["To"] = ", ".join(an)
    if kopie:
        msg["Cc"] = ", ".join(kopie)
    if blind:
        msg["Bcc"] = ", ".join(blind)
    if einst.get("antwort_an"):
        msg["Reply-To"] = str(einst["antwort_an"])
    msg["Subject"] = str(d.get("betreff") or "")
    msg["Date"] = email.utils.formatdate(localtime=True)
    bereich = (s.get("adresse") or "@localhost").split("@")[-1]
    msg["Message-Id"] = email.utils.make_msgid(domain=bereich)
    bezug = str(d.get("in_antwort_auf") or "").strip()
    if bezug:
        msg["In-Reply-To"] = bezug
        msg["References"] = bezug
    # 🔑 A mail program names itself. Not vanity: when something arrives
    # malformed somewhere, this line is the first clue as to who built it.
    msg["X-Mailer"] = "Postwache %s" % (W._version() if W is not None else "")
    msg.set_content(str(d.get("text") or ""), subtype="plain", charset="utf-8")
    grenze = min(int(einst.get("anhang_grenze") or 25) * 1024 * 1024, MAX_ANHANG)
    summe = 0
    for a in (d.get("anhaenge") or [])[:40]:
        try:
            roh = base64.b64decode(str(a.get("b64") or ""), validate=False)
        except Exception:
            return None, [], txt("k.anhang_kaputt", n=str(a.get("name") or "?"))
        summe += len(roh)
        if summe > grenze:
            return None, [], txt("k.anhang_gross", mb=grenze // (1024 * 1024))
        typ, _, unter = _mime_raten(str(a.get("name") or "datei"),
                                    str(a.get("typ") or ""))
        msg.add_attachment(roh, maintype=typ, subtype=unter,
                           filename=str(a.get("name") or "datei")[:200])
    # Forwarded attachments come from the server, not from the browser — nobody
    # has to download and re-upload them.
    for u in (d.get("uebernahme") or [])[:40]:
        roh = tu(pf_id, lambda k, u=u: (k.waehle(str(u.get("ordner") or "INBOX")),
                                        k.teil_holen(int(u.get("uid") or 0),
                                                     str(u.get("t") or "1"),
                                                     str(u.get("k") or "")))[1])
        if not roh:
            continue
        summe += len(roh)
        if summe > grenze:
            return None, [], txt("k.anhang_gross", mb=grenze // (1024 * 1024))
        typ, _, unter = _mime_raten(str(u.get("n") or "datei"), str(u.get("m") or ""))
        msg.add_attachment(roh, maintype=typ, subtype=unter,
                           filename=str(u.get("n") or "datei")[:200])
    return msg, _nur_adressen(an + kopie + blind), ""


def _mime_raten(name: str, angabe: str) -> tuple:
    """(maintype, full, subtype) — the declared type if it is usable, otherwise
    guessed from the extension, otherwise the neutral one that every mail program
    understands."""
    voll = (angabe or "").split(";")[0].strip().lower()
    if "/" not in voll:
        voll = mimetypes.guess_type(name)[0] or "application/octet-stream"
    haupt, _, unter = voll.partition("/")
    haupt = re.sub(r"[^a-z0-9.+-]", "", haupt) or "application"
    unter = re.sub(r"[^a-z0-9.+-]", "", unter) or "octet-stream"
    return haupt, voll, unter


def senden(d: dict) -> dict:
    """Send — and only then everything that goes with it: the copy into the sent
    folder, the „answered" mark on the original, the draft removed.

    🔴 In that order. Whoever files the copy first and then fails to send has a
    mail in the sent folder that was never sent — and that is the one error nobody
    checks for.
    """
    pf_id = str(d.get("pf") or "")
    einst = einstellungen()
    s = smtp_zugang(pf_id)
    if not s or not s["server"]:
        return {"ok": False, "text": txt("k.smtp_kein_server")}
    msg, empfaenger, fehler = _brief_bauen(d, pf_id, einst)
    if fehler:
        return {"ok": False, "text": fehler}
    fuer_ablage = msg.as_bytes()
    del msg["Bcc"]                  # a blind copy stays blind on the wire
    try:
        verbindung = _smtp_verbinden(s)
    except smtplib.SMTPAuthenticationError:
        return {"ok": False, "text": txt("k.smtp_anmeldung")}
    except Exception as e:
        return {"ok": False, "text": txt("k.smtp_fehler", fehler=str(e)[:160])}
    try:
        verwehrt = verbindung.send_message(msg, from_addr=s["adresse"],
                                           to_addrs=empfaenger)
    except Exception as e:
        try:
            verbindung.quit()
        except Exception:
            pass
        return {"ok": False, "text": txt("k.smtp_fehler", fehler=str(e)[:160])}
    try:
        verbindung.quit()
    except Exception:
        pass
    nachwort = []
    if verwehrt:
        # Partly delivered is not „sent". Naming who was refused is the whole
        # difference between a report and a green tick.
        nachwort.append(txt("k.teils_verwehrt", wer=", ".join(sorted(verwehrt))[:200]))
    if einst.get("kopie_gesendet"):
        ziel = _rollen_ordner(pf_id, "gesendet")
        if ziel and tu(pf_id, lambda k: k.anhaengen(ziel, fuer_ablage, "(\\Seen)")):
            nachwort.append(txt("k.kopie_gelegt", o=utf7_dekodieren(ziel)))
        else:
            nachwort.append(txt("k.kopie_fehl_ordner"))
    quelle = d.get("quelle") or {}
    if quelle.get("uid"):
        try:
            tu(pf_id, lambda k: k.flagge(str(quelle.get("ordner") or "INBOX"),
                                         [int(quelle["uid"])], "\\Answered", True))
        except Exception as e:
            log("Antwort-Marke nicht gesetzt: %s" % str(e)[:100])
    if d.get("entwurf_uid"):
        entwuerfe = _rollen_ordner(pf_id, "entwuerfe")
        if entwuerfe:
            try:
                tu(pf_id, lambda k: (k.flagge(entwuerfe, [int(d["entwurf_uid"])],
                                              "\\Deleted", True),
                                     k.m.expunge()))
            except Exception as e:
                log("Entwurf nicht entfernt: %s" % str(e)[:100])
    if W is not None:
        # 🔑 The chronicle carries WHO was written to, never WHAT. The body is
        # stored nowhere — that holds for the outgoing direction too.
        W.chronik("klient_gesendet", n=len(empfaenger),
                  text="%d Empfänger" % len(empfaenger))
    return {"ok": True, "text": " ".join([txt("k.gesendet")] + nachwort)}


def entwurf_speichern(d: dict) -> dict:
    """Put the draft into the drafts folder — the only place it is safe. A text in
    a browser tab is gone with the tab."""
    pf_id = str(d.get("pf") or "")
    einst = einstellungen()
    msg, _, fehler = _brief_bauen(dict(d, an=d.get("an") or "niemand@invalid"),
                                  pf_id, einst)
    if fehler and not msg:
        return {"ok": False, "text": fehler}
    ziel = _rollen_ordner(pf_id, "entwuerfe")
    if not ziel:
        return {"ok": False, "text": txt("k.kein_entwurfsordner")}
    if d.get("entwurf_uid"):
        try:
            tu(pf_id, lambda k: (k.flagge(ziel, [int(d["entwurf_uid"])],
                                          "\\Deleted", True), k.m.expunge()))
        except Exception:
            pass
    ok = tu(pf_id, lambda k: k.anhaengen(ziel, msg.as_bytes(), "(\\Draft)"))
    return ({"ok": True, "text": txt("k.entwurf_gelegt", o=utf7_dekodieren(ziel))}
            if ok else {"ok": False, "text": txt("k.entwurf_fehl")})


def _rollen_ordner(pf_id: str, rolle: str) -> str:
    """Which folder plays a role — set by hand, otherwise recognised, otherwise
    created for the two that a client cannot do without."""
    einst = einstellungen()
    gesetzt = str(einst.get("ordner_" + rolle) or "").strip()
    if gesetzt:
        return gesetzt
    baum = tu(pf_id, lambda k: k.baum())
    for o in baum:
        if o["rolle"] == rolle:
            return o["name"]
    if rolle in ("gesendet", "entwuerfe"):
        name = {"gesendet": "Sent", "entwuerfe": "Drafts"}[rolle]
        neu = tu(pf_id, lambda k: k.ordner_neu(name))
        return neu.get("name") or ""
    return ""


# ── What the page asks for ──────────────────────────────────────────────
def lage(pf_id: str) -> dict:
    """Everything the client needs to draw itself once."""
    zug = _zugang(pf_id)
    faehig = {}
    ordner = []
    try:
        ordner = tu(pf_id, lambda k: k.baum())
        faehig = tu(pf_id, lambda k: {"sort": k.kann("SORT"), "move": k.kann("MOVE"),
                                      "trenner": k.trenner})
    except Exception as e:
        return {"ok": False, "text": str(e)[:200], "einst": einstellungen(),
                "ordner": [], "faehig": {}}
    return {
        "ok": True, "einst": einstellungen(), "ordner": ordner, "faehig": faehig,
        "oberflaeche": oberflaeche(),
        "adresse": zug.get("adresse") or "", "name": zug.get("name") or "",
        "smtp": _smtp_kurz(pf_id),
        "rollen": {r: next((o["name"] for o in ordner if o["rolle"] == r), "")
                   for r in ("posteingang", "gesendet", "entwuerfe", "papierkorb",
                             "spam", "archiv")},
    }


def liste(d: dict) -> dict:
    """One page of a folder.

    🔑 Paging happens on the UID list, not on the fetched mails: the list of
    numbers costs one command even with six thousand mails, the headers only for
    the fifty being shown. That is the difference between a client that opens and
    one you wait for.
    """
    pf_id = str(d.get("pf") or "")
    ordner = str(d.get("ordner") or "INBOX")
    einst = einstellungen()
    sieb = str(d.get("sieb") or "")
    suche = str(d.get("suche") or "").strip()[:200]
    feld = str(d.get("feld") or "alle")
    sortierung = str(d.get("sortierung") or einst["sortierung"])
    richtung = str(d.get("richtung") or einst["richtung"])
    try:
        pro = max(10, min(200, int(d.get("pro_seite") or einst["pro_seite"])))
        seite = max(1, int(d.get("seite") or 1))
    except (TypeError, ValueError):
        pro, seite = einst["pro_seite"], 1

    # 🔑 „Select everything" needs the numbers of everything — and they are
    #    ALREADY here: `uids` is the full list, the page is only a slice of it.
    #    So the answer carries them when they are asked for, and nothing has to
    #    be searched a second time. They belong to what is on screen: after a
    #    search or with a filter on, „everything" means every HIT, not the whole
    #    folder.
    alle_uids = bool(d.get("alle_uids"))

    def arbeit(k):
        uids, echt = k.uids(ordner, sieb, suche, feld, sortierung, richtung)
        gesamt = len(uids)
        seiten = max(1, (gesamt + pro - 1) // pro)
        nr = min(seite, seiten)
        teil = uids[(nr - 1) * pro:nr * pro]
        koepfe = k.koepfe(teil, auszug=bool(einst["vorschautext"]))
        gesamt_o, ungelesen_o = k.zaehlen(ordner)
        return {
            "ok": True, "ordner": ordner, "gesamt": gesamt, "seite": nr,
            "seiten": seiten, "pro_seite": pro, "sortiert": echt,
            "ordner_gesamt": gesamt_o, "ordner_ungelesen": ungelesen_o,
            "alle_uids": [int(u) for u in uids] if alle_uids else None,
            # In the order the server gave them — a dictionary has no order and
            # would show the newest mail somewhere in the middle.
            "mails": [koepfe[u] for u in teil if u in koepfe],
        }
    return tu(pf_id, arbeit)


def neu_liste(d: dict) -> dict:
    """The „New" overview: one list out of every folder that has something new."""
    pf_id = str(d.get("pf") or "")
    einst = einstellungen()
    zeitraum = str(d.get("zeitraum") or einst["neu_zeitraum"])
    if zeitraum not in AUSWAHL["neu_zeitraum"]:
        zeitraum = VORGABEN["neu_zeitraum"]
    mit_spam = bool(d.get("spam", einst["neu_spam"]))
    antwort = tu(pf_id, lambda k: k.neue(zeitraum, mit_spam,
                                         auszug=bool(einst["vorschautext"])))
    antwort["ok"] = True
    antwort["zeitraum"] = zeitraum
    return antwort


def _bilder_erlaubt(pf_id: str, adresse: str, wunsch) -> bool:
    """Remote images: off by default, because a remote image is a receipt that the
    mail was opened, at the exact second it was opened.

    „bekannte" means: someone this mailbox has already received several mails
    from — the watchman's long-term memory answers that, it has been keeping it
    for weeks."""
    if wunsch is not None:
        return bool(wunsch)
    regel = einstellungen()["bilder"]
    if regel == "immer":
        return True
    if regel == "nie":
        return False
    W.pf_waehlen(pf_id)
    prof = W.load(W.ABSENDER, {}) or {}
    eintrag = prof.get((adresse or "").lower()) if isinstance(prof, dict) else None
    return bool(isinstance(eintrag, dict) and int(eintrag.get("n") or 0) >= 2)


def mail_zeigen(d: dict) -> dict:
    """One mail, ready to display — including the finished frame document.

    🔴 THE one place that turns „displayed" into „read", and only when the setting
    says „at once". Everything else (after N seconds, by hand) is a deliberate call
    from the page. One place decides, not four.
    """
    pf_id = str(d.get("pf") or "")
    ordner = str(d.get("ordner") or "INBOX")
    uid = int(d.get("uid") or 0)
    einst = einstellungen()
    wunsch = d.get("bilder")
    # 🔑 Where the answer does not depend on the sender, it is known BEFORE the
    # mail is fetched — and then the mail is fetched once instead of twice. Only
    # „known senders" has to see the address first; that alone costs a second
    # pass, and with the setting on „always" it was paid on every single mail.
    vorab = (None if wunsch is None and einstellungen()["bilder"] == "bekannte"
             else _bilder_erlaubt(pf_id, "", wunsch))
    m = tu(pf_id, lambda k: k.mail(ordner, uid, bilder=bool(vorab)))
    if not m:
        return {"ok": False, "text": txt("k.mail_weg")}
    bilder = vorab if vorab is not None else _bilder_erlaubt(
        pf_id, m.get("adresse") or "", wunsch)
    if bilder and vorab is None and m.get("hat_html"):
        zweit = tu(pf_id, lambda k: k.mail(ordner, uid, bilder=True))
        # 🔴 And never let the second pass empty the letter: a connection that
        # goes stale between the two answers must not turn a readable mail into
        # a blank page.
        if zweit:
            m = zweit
        else:
            bilder = False
    ansicht = str(d.get("ansicht") or "")
    if not ansicht:
        ansicht = "html" if (m.get("hat_html") and einst["html_zuerst"]) else "text"
    if ansicht == "html" and not m.get("hat_html"):
        ansicht = "text"
    if ansicht == "html":
        inhalt = m["html"]
    else:
        text = m.get("text") or ""
        if not text and m.get("hat_html"):
            text = _html.unescape(re.sub(r"<[^>]+>", " ", m.get("html") or ""))
            text = re.sub(r"[ \t]{2,}", " ", re.sub(r"\n{3,}", "\n\n", text))
        inhalt = '<div class="pw-text">%s</div>' % text_zu_html(text)
    m["ansicht"] = ansicht
    m["bilder"] = bilder
    # 🔑 The letter is a document of its own inside the frame, with its own
    # stylesheet — so the day mode has to reach IN THERE too, or the page turns
    # to paper and the letter stays night. Which mode is in force is something
    # only the browser knows (the setting may say „follow the device"), so the
    # page says so with the request.
    m["rahmen"] = rahmen(inhalt, bilder, not bool(d.get("hell")))
    m["ok"] = True
    if einst["gelesen_nach"] == 0 and not m.get("gelesen"):
        try:
            tu(pf_id, lambda k: k.flagge(ordner, [uid], "\\Seen", True))
            m["gelesen"] = True
        except Exception as e:
            log("Gelesen-Marke nicht gesetzt: %s" % str(e)[:100])
    # The watchman's opinion — where it would file this sender, and why. That is
    # the one thing no other mail program can show.
    if einst["wache_grund"]:
        m["wache"] = wache_urteil(pf_id, m.get("adresse") or "")
    return m


def wache_urteil(pf_id: str, adresse: str) -> dict:
    """Where the watchman would file this sender — out of ITS learned map, not out
    of a second opinion of my own."""
    if not adresse or W is None:
        return {}
    W.pf_waehlen(pf_id)
    karte = W.load(W.ABLAGE, {}) or {}
    try:
        ordner, grund, sicher, darf = W.ziel_finden(karte, adresse.lower())
    except Exception:
        return {}
    prof = W.load(W.ABSENDER, {}) or {}
    eintrag = prof.get(adresse.lower()) if isinstance(prof, dict) else {}
    klassen = (eintrag or {}).get("klassen") or {}
    return {"ordner": ordner or "", "zeige": utf7_dekodieren(ordner or ""),
            "grund": grund, "sicher": int(sicher or 0), "darf": bool(darf),
            "gesehen": int((eintrag or {}).get("n") or 0),
            "klasse": max(klassen, key=klassen.get) if klassen else ""}


def flaggen(d: dict) -> dict:
    """Set or clear a flag on one or several mails."""
    pf_id = str(d.get("pf") or "")
    ordner = str(d.get("ordner") or "INBOX")
    uids = [int(u) for u in (d.get("uids") or []) if str(u).isdigit()]
    welche = {"gelesen": "\\Seen", "markiert": "\\Flagged",
              "beantwortet": "\\Answered", "geloescht": "\\Deleted"}
    flagge = welche.get(str(d.get("was") or ""))
    if not flagge or not uids:
        return {"ok": False, "text": txt("k.nichts_gewaehlt")}
    n = tu(pf_id, lambda k: k.flagge(ordner, uids, flagge, bool(d.get("an"))))
    return {"ok": bool(n), "n": n}


def verschieben(d: dict) -> dict:
    pf_id = str(d.get("pf") or "")
    ordner = str(d.get("ordner") or "INBOX")
    ziel = str(d.get("ziel") or "")
    uids = [int(u) for u in (d.get("uids") or []) if str(u).isdigit()]
    if not uids:
        return {"ok": False, "text": txt("k.nichts_gewaehlt")}
    if ziel == ordner:
        return {"ok": False, "text": txt("k.selbes_ziel")}
    erg = tu(pf_id, lambda k: k.verschieben_viele(ordner, uids, ziel))
    if erg.get("ok") and W is not None:
        W.chronik("klient_verschoben", n=erg["n"],
                  text="%d nach %s" % (erg["n"], utf7_dekodieren(ziel)))
    if erg.get("ok"):
        erg["text"] = txt("k.verschoben", n=erg["n"], o=utf7_dekodieren(ziel))
        # For the way back — the same principle as the watchman's journal: whoever
        # can move must be able to undo it.
        erg["zurueck"] = {"ordner": ziel, "ziel": ordner, "uids": []}
    return erg


def loeschen(d: dict) -> dict:
    """Delete — and „delete" means two different things.

    🔴 Normally into the bin: reversible, and that is the point. Only for a mail
    that is ALREADY in the bin, or when the setting says so, is `\\Deleted` set —
    and `EXPUNGE` runs only when it was explicitly asked for. The watchman has
    never deleted anything and still does not; this is the reader's hand.
    """
    pf_id = str(d.get("pf") or "")
    ordner = str(d.get("ordner") or "INBOX")
    uids = [int(u) for u in (d.get("uids") or []) if str(u).isdigit()]
    if not uids:
        return {"ok": False, "text": txt("k.nichts_gewaehlt")}
    einst = einstellungen()
    korb = _rollen_ordner(pf_id, "papierkorb")
    endgueltig = bool(d.get("endgueltig")) or einst["papierkorb"] == "flagge" \
        or not korb or korb == ordner
    if not endgueltig:
        erg = tu(pf_id, lambda k: k.verschieben_viele(ordner, uids, korb))
        if erg.get("ok"):
            # 🔑 One mail is not „1 mails". The message is meant to read as a
            #    sentence, and for that the one needs its own form — the same
            #    rule as the confirmation question before deleting.
            erg["text"] = (txt("k.in_korb_eine", o=utf7_dekodieren(korb))
                           if erg["n"] == 1 else
                           txt("k.in_korb", n=erg["n"], o=utf7_dekodieren(korb)))
        return erg

    def arbeit(k):
        n = k.flagge(ordner, uids, "\\Deleted", True)
        if d.get("endgueltig") or einst["papierkorb"] != "flagge":
            k.m.expunge()
        return n
    n = tu(pf_id, arbeit)
    if W is not None and n:
        W.chronik("klient_geloescht", n=n, text="%d endgültig" % n)
    return {"ok": bool(n), "n": n,
            "text": txt("k.geloescht_eine") if n == 1
            else txt("k.geloescht", n=n)}


def _wache_abgleichen(pf_id: str) -> dict:
    """Tell the watchman which folders exist NOW.

    🔑 One function for all three cases — created, deleted, moved. It does not
    ask what happened; it asks the server what IS. That is also the only answer
    that covers a folder somebody deleted in a completely different mail program.
    """
    if W is None:
        return {}
    try:
        W.pf_waehlen(pf_id)
        return W.ordner_abgleichen(tu(pf_id, lambda k: k.ordner_liste()))
    except Exception as e:                              # noqa: BLE001
        log("Ordnerabgleich nicht moeglich: %s" % str(e)[:140])
        return {"fehler": str(e)[:140]}


def ordner_neu(d: dict) -> dict:
    antwort = tu(str(d.get("pf") or ""),
                 lambda k: k.ordner_neu(str(d.get("name") or "")))
    if antwort.get("ok"):
        # 🔑 At once, not at the next learning run: an empty folder called
        # „Steuer" can take post from `steuer@…` the minute it exists — but only
        # if the map knows that it is there.
        antwort["wache"] = _wache_abgleichen(str(d.get("pf") or ""))
    return antwort


def ordner_loeschen(d: dict) -> dict:
    """Delete a folder, and let the watchman forget it in the same breath."""
    pf_id = str(d.get("pf") or "")
    antwort = tu(pf_id, lambda k: k.ordner_fort(str(d.get("ordner") or ""),
                                                bool(d.get("mit_inhalt"))))
    if antwort.get("ok"):
        antwort["wache"] = _wache_abgleichen(pf_id)
        if W is not None:
            try:
                W.chronik("klient_ordner_fort", text=str(d.get("ordner") or ""))
            except Exception:
                pass
    return antwort


def _einst_ordner_umschreiben(alt: str, neu: str, trenner: str) -> int:
    """The five folders a reader may have PINNED by hand in the settings. If one
    of them was the folder that moved, it has to follow — otherwise the client
    looks for the archive where nothing is any more."""
    if W is None:
        return 0
    e = _load(KLIENT_EINST, None)
    if not isinstance(e, dict):
        return 0
    zahl = 0
    for feld in ("ordner_papierkorb", "ordner_archiv", "ordner_spam",
                 "ordner_entwuerfe", "ordner_gesendet"):
        wert = str(e.get(feld) or "")
        neuer = W.pfad_umschreiben(wert, alt, neu, trenner) if wert else ""
        if neuer and neuer != wert:
            e[feld] = neuer
            zahl += 1
    if zahl:
        _save(KLIENT_EINST, e)
    return zahl


def ordner_ziehen(d: dict) -> dict:
    """Move a folder — and tell everything that knew it by name.

    A whole folder travels by drag and drop, mail and all — and the watchman
    has to notice, for the sake of what it has learned.

    🔑 The second half of that sentence is the bigger half. The provider does the
    move in one command; the watchman has LEARNED that folder — who writes into
    it, what hangs in it, where each mail came from. That memory is carried over
    in the same breath, not at the next run: between the two the watchman would
    file into a folder that is not there, and filing CREATES what is missing.
    """
    pf_id = str(d.get("pf") or "")
    alt = str(d.get("ordner") or "")
    ziel = str(d.get("ziel") or "")
    antwort = tu(pf_id, lambda k: k.ordner_ziehen(alt, ziel))
    if not antwort.get("ok"):
        return antwort
    trenner = antwort.get("trenner") or "."
    antwort["einstellungen"] = _einst_ordner_umschreiben(antwort["alt"],
                                                         antwort["neu"], trenner)
    if W is not None:
        try:
            W.pf_waehlen(pf_id)
            antwort["wache"] = W.ordner_umgezogen(antwort["alt"], antwort["neu"],
                                                  trenner)
            W.chronik("klient_ordner_gezogen", text="%s → %s"
                      % (antwort["alt"], antwort["neu"]))
        except Exception as e:                      # noqa: BLE001
            # 🔴 The move HAPPENED. Saying „did not work" now would be a lie, and
            # the reader would press again — so it says what is true: the folder
            # has moved, the watchman has not understood it yet.
            log("Wache nicht nachgezogen: %s" % str(e)[:140])
            antwort["wache_fehler"] = str(e)[:140]
    # The tree has a new shape: one path gone, one path new. Same question,
    # same answer — ask the server what IS.
    _wache_abgleichen(pf_id)
    zeige = utf7_dekodieren(antwort["neu"].rsplit(trenner, 1)[-1])
    unter = antwort["neu"].rsplit(trenner, 1)[0] if trenner in antwort["neu"] else ""
    antwort["text"] = txt("k.ordner_gezogen", o=zeige,
                          ziel=utf7_dekodieren(unter.rsplit(trenner, 1)[-1])
                          or txt("k.ordner_oben"),
                          n=antwort.get("mails") or 0)
    return antwort


def adressbuch(d: dict) -> dict:
    """Who has written here before — the address book nobody had to maintain.

    🔑 The watchman has been keeping `absender.json` for weeks: address, name, how
    often, when last. That IS the address book, and it is more accurate than one
    kept by hand.
    """
    frage = str(d.get("q") or "").strip().lower()
    W.pf_waehlen(str(d.get("pf") or ""))
    prof = W.load(W.ABSENDER, {}) or {}
    treffer = []
    for adresse, e in (prof.items() if isinstance(prof, dict) else ()):
        if not isinstance(e, dict) or "@" not in str(adresse):
            continue
        name = str(e.get("name") or "")
        if frage and frage not in str(adresse).lower() and frage not in name.lower():
            continue
        treffer.append({"adresse": adresse, "name": name,
                        "n": int(e.get("n") or 0), "zuletzt": e.get("zuletzt") or ""})
    treffer.sort(key=lambda t: (-t["n"], t["adresse"]))
    return {"ok": True, "treffer": treffer[:12]}


def anhang(pf_id: str, ordner: str, uid: int, nr: str, kodierung: str,
           name: str) -> tuple:
    """One attachment as (file name, type, bytes)."""
    roh = tu(pf_id, lambda k: (k.waehle(ordner), k.teil_holen(uid, nr, kodierung))[1])
    typ = mimetypes.guess_type(name or "datei")[0] or "application/octet-stream"
    return (name or "anhang"), typ, roh


def bild(pf_id: str, ordner: str, uid: int, cid: str) -> tuple:
    """An inline image out of the mail — found by its Content-ID, which is what the
    HTML refers to."""
    def arbeit(k):
        k.waehle(ordner)
        struct = k.strukturen([uid]).get(uid)
        for t in (W._teile(struct) if struct else []):
            if str(t.get("id") or "").strip("<>") == cid and t["typ"].upper() == "IMAGE":
                return (("%s/%s" % (t["typ"], t["subtyp"])).lower(),
                        k.teil_holen(uid, t["nr"], t["kodierung"]))
        return "", b""
    return tu(pf_id, arbeit)


def roh_text(pf_id: str, ordner: str, uid: int) -> bytes:
    return tu(pf_id, lambda k: k.roh(ordner, uid))
