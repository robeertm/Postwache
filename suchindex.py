#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Postwache — the search index (5.31.0).

The brief: „search, bang, result — no matter which folder, no matter which
mail", and „I also want to search for word fragments when I do not know exactly
what I am looking for".

🔴 MEASURED BEFORE (13.11.2026, real mailbox, 176 folders, 11 594 mails): one
search across all folders took 57–70 s. The server was asked folder by folder
(EXAMINE + SEARCH TEXT, 176 times), and every word cost the same again. A typo
found nothing at all, and „kündigung" did not find „Kuendigung".

🔑 The answer is a local index that already knows everything: one SQLite file
per mailbox (FTS5 with the trigram tokenizer, so ANY three letters in a row are
searchable — „rechn" finds „Stromrechnung"), built on its OWN read-only
connection (EXAMINE, BODY.PEEK — it cannot set \\Seen by construction), kept up
to date by comparing every folder's mark (UIDVALIDITY, UIDNEXT, MESSAGES,
HIGHESTMODSEQ) in ONE command, and asked by the page instead of the server.

🔴 The index is a COPY, never the truth. Every row it hands out names folder and
UID as they were at the last comparison; a mail moved in between is found again
by its Message-Id. While the first build runs, the page is told how far it got,
and the folders not finished yet are still searched on the server — never
silently fewer hits.

🔴 The file holds mail text, so it is created 0600 and lives in the mailbox's
own state folder, next to the records the watchman keeps. It is never sent
anywhere; the health check reports counts only.
"""
from __future__ import annotations

import imaplib
import json
import os
import re
import sqlite3
import threading
import time
import unicodedata
import zlib
from datetime import datetime

SCHEMA = 4                          # a different number = the file is rebuilt
                                    # (4 = 5.32.0: the conversation columns)
DATEI = "suchindex.sqlite"
STUECK = 40                         # mails per fetch round of the build
DROSSEL_S = 0.15                    # pause between two rounds — the NAS and the provider breathe
try:                                # a test bench can slow the build down to catch it half-way
    DROSSEL_S = float(os.environ.get("POSTWACHE_SUCHINDEX_DROSSEL") or DROSSEL_S)
except ValueError:
    pass
RUNDE_JE_ORDNER = 400               # mails per folder and pass during the first build
TAKT_AKTIV_S = 15.0                 # mark comparison while somebody uses the page
TAKT_RUHE_S = 300.0                 # and while nobody does
AKTIV_FENSTER_S = 900.0             # „somebody uses it" = a request in the last 15 min
LEERLAUF_S = 60.0                   # the connection closes when the next look is further away
ROH_MAX = 512 * 1024                # raw bytes of a body part fetched at most
TEXT_MAX = 100000                   # characters of body text kept per mail
ANHANG_TEXT_MAX = 200 * 1024        # a text or HTML attachment up to this size is read too
UNSCHARF_AB = 3                     # fewer exact hits than this → the tolerant stage
WORTE_JE_BEGRIFF = 12               # similar words tried per misspelled term at most
AUSSCHNITT = 70                     # characters of context around a hit
HEILEN_UM = (3, 40)                 # the nightly full comparison, local time
DB_FEHLER_MAX = 3                   # 5.32.1: this many index-file errors in a row → file put aside
DB_DEFEKT = ("no such column", "no such table", "has no column", "malformed",
             "not a database", "corrupt", "disk image", "no such module")
BEISEITE_PAUSE_S = 86400.0          # the file is put aside at most once a day
ZUSTELL = ("X-Envelope-To", "X-Original-To", "Envelope-To", "Delivered-To")

# ── Folding: one spelling for everything that means the same ─────────────
# 🔑 ä = ae = a, ß = ss, upper = lower, é = e. The index stores FOLDED text and
# the question is folded the same way, so „Müller", „Mueller" and „muller" are
# one word to it. The second spelling of every umlaut (ae/oe/ue) is folded to
# the bare vowel on BOTH sides — that also turns „Steuer" into „steur", but
# since the question goes through the same door, every match still matches.
_AOU_E = re.compile(r"[aou]e")


def _zeichen_falten(c: str) -> str:
    t = c.casefold()
    if t.isascii():
        return t
    t = unicodedata.normalize("NFKD", t)
    return "".join(z for z in t if not unicodedata.combining(z)).lower()


def falten(text: str) -> str:
    """Lower case, accents off, ß → ss, ae/oe/ue → a/o/u."""
    t = (text or "").casefold()
    if not t.isascii():
        t = unicodedata.normalize("NFKD", t)
        t = "".join(z for z in t if not unicodedata.combining(z)).lower()
    return _AOU_E.sub(lambda m: m.group(0)[0], t)


def falten_mit_karte(text: str) -> tuple:
    """(folded, [index in the original for every folded character]) — what a
    highlighted excerpt needs to find its way back from the folded text to the
    letters the reader sees. Produces exactly what `falten()` produces."""
    zeichen, karte = [], []
    for i, c in enumerate(text or ""):
        for z in _zeichen_falten(c):
            zeichen.append(z)
            karte.append(i)
    raus, rkarte, i = [], [], 0
    while i < len(zeichen):
        raus.append(zeichen[i])
        rkarte.append(karte[i])
        if zeichen[i] in "aou" and i + 1 < len(zeichen) and zeichen[i + 1] == "e":
            i += 2
        else:
            i += 1
    return "".join(raus), rkarte


# ── Where the file lives ─────────────────────────────────────────────────
def _w():
    import postwache as W
    return W


def _kl():
    import klient as KL
    return KL


def pfad(pf_id: str) -> str:
    W = _w()
    ordner = os.path.join(W.STATE, "pf", str(pf_id or "standard"))
    os.makedirs(ordner, mode=0o700, exist_ok=True)
    return os.path.join(ordner, DATEI)


def eingeschaltet() -> bool:
    """`POSTWACHE_SUCHINDEX=0` switches the whole thing off — the search then
    asks the server, as it did up to 5.30."""
    return str(os.environ.get("POSTWACHE_SUCHINDEX", "1")).strip().lower() not in (
        "0", "aus", "off", "nein", "false")


def _log(text: str) -> None:
    try:
        _w().log(text)
    except Exception:
        pass


TABELLEN = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS ordner(
    name TEXT PRIMARY KEY, uidvalidity INTEGER, uidnext INTEGER, messages INTEGER,
    modseq INTEGER, unseen INTEGER, rolle TEXT, zeige TEXT, kurz TEXT,
    fertig INTEGER DEFAULT 0, geprueft REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS mails(
    id INTEGER PRIMARY KEY, ordner TEXT NOT NULL, uid INTEGER NOT NULL,
    datum REAL, msgid TEXT, groesse INTEGER, flaggen TEXT, anhang INTEGER,
    kopf TEXT, satz TEXT, roh BLOB, weg INTEGER DEFAULT 0,
    bezug TEXT DEFAULT '', fbetreff TEXT DEFAULT '', praefix INTEGER DEFAULT 0,
    teilnehmer TEXT DEFAULT '', zustell INTEGER DEFAULT 0,
    UNIQUE(ordner, uid));
CREATE INDEX IF NOT EXISTS mails_msgid ON mails(msgid);
CREATE INDEX IF NOT EXISTS mails_datum ON mails(datum);
CREATE INDEX IF NOT EXISTS mails_fbetreff ON mails(fbetreff);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    betreff, von, an, anhang, text, tokenize='trigram');
CREATE TABLE IF NOT EXISTS worte(w TEXT PRIMARY KEY);
CREATE VIRTUAL TABLE IF NOT EXISTS worte_fts USING fts5(w, tokenize='trigram');
"""


def _datei_entfernen(datei: str) -> None:
    for endung in ("", "-wal", "-shm"):
        try:
            os.remove(datei + endung)
        except OSError:
            pass


def _frisch(datei: str) -> sqlite3.Connection:
    """A connection to `datei` — created empty, 0600 before the first byte."""
    if not os.path.exists(datei):
        fd = os.open(datei, os.O_RDWR | os.O_CREAT, 0o600)
        os.close(fd)
    try:
        os.chmod(datei, 0o600)
    except OSError:
        pass
    db = sqlite3.connect(datei, timeout=10, check_same_thread=False)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=NORMAL")
        db.execute("PRAGMA busy_timeout=10000")
    except Exception:
        db.close()
        raise
    return db


def _fremdes_layout(db) -> bool:
    """True when the file holds data of another layout: a schema number that is
    not ours, tables but no number at all, or no database at all."""
    try:
        db.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        stand = db.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
        if stand is not None:
            return stand[0] != str(SCHEMA)
        andere = db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name<>'meta' "
            "AND name NOT LIKE 'sqlite_%'").fetchone()[0]
        return andere > 0
    except sqlite3.DatabaseError as e:
        # not a database / malformed: throw it away — but a LOCKED file is only busy
        return not any(m in str(e).lower() for m in ("locked", "busy"))


def _oeffnen(datei: str) -> sqlite3.Connection:
    """Open (and if needed create) the file — 0600 before the first byte.

    🔴 5.32.1: the schema number is read BEFORE a single table is created. The
    old order (`CREATE … IF NOT EXISTS` first, compare afterwards) tripped over
    the first index on a column the old file did not have, so the rebuild branch
    was never reached for a file of the previous layout."""
    for _versuch in range(2):
        db = None
        try:
            db = _frisch(datei)
            fremd = _fremdes_layout(db)
        except sqlite3.DatabaseError as e:
            if any(m in str(e).lower() for m in ("locked", "busy")):
                raise
            fremd = True                       # „file is not a database" and the like
        if not fremd:
            break
        # 🔴 A file of another layout is not migrated, it is rebuilt — a copy
        # can always be made again, a half-migrated one lies.
        if db is not None:
            db.close()
        _leser_vergessen(datei)
        _datei_entfernen(datei)
    else:
        raise sqlite3.DatabaseError("index file could not be rebuilt")
    db.executescript(TABELLEN)
    db.execute("INSERT OR REPLACE INTO meta VALUES('schema', ?)", (str(SCHEMA),))
    db.commit()
    for endung in ("-wal", "-shm"):
        try:
            os.chmod(datei + endung, 0o600)
        except OSError:
            pass
    return db


# Reader connections — a small pool, one caller at a time per connection.
# 🔴 5.32.1: every connection remembers WHICH file it was opened on (device +
# inode). A file that was rebuilt or put aside is a new file; a pooled
# connection on the old one would read a deleted file forever.
_LESER = {}
_LESER_SCHLOSS = threading.Lock()


def _datei_marke(datei: str):
    try:
        st = os.stat(datei)
        return (st.st_dev, st.st_ino)
    except OSError:
        return None


class _Leser:
    def __init__(self, pf_id: str):
        self.pf_id = pf_id
        self.db = None
        self.marke = None

    def __enter__(self):
        datei = pfad(self.pf_id)
        self.datei = datei
        marke = _datei_marke(datei)
        while True:
            with _LESER_SCHLOSS:
                frei = _LESER.setdefault(datei, [])
                eintrag = frei.pop() if frei else None
            if eintrag is None:
                break
            if eintrag[1] == marke:
                self.db, self.marke = eintrag
                return self.db
            try:
                eintrag[0].close()                  # on the old file: let go
            except Exception:
                pass
        if marke is None:
            raise FileNotFoundError(datei)
        self.marke = marke                          # taken BEFORE the connect
        self.db = sqlite3.connect(datei, timeout=10, check_same_thread=False)
        self.db.execute("PRAGMA busy_timeout=10000")
        return self.db

    def __exit__(self, typ, wert, tb):
        if typ is None and self.db is not None:
            with _LESER_SCHLOSS:
                frei = _LESER.setdefault(self.datei, [])
                if len(frei) < 4:
                    frei.append((self.db, self.marke))
                    return False
        try:
            self.db.close()
        except Exception:
            pass
        return False


def _leser_vergessen(datei: str) -> None:
    with _LESER_SCHLOSS:
        for eintrag in _LESER.pop(datei, []):
            try:
                eintrag[0].close()
            except Exception:
                pass


# ── Reading a mail into a row ────────────────────────────────────────────
def _volltext(roh: bytes, kodierung: str, zeichensatz: str, ist_html: bool) -> str:
    """The readable text of a (possibly cut-off) body part."""
    KL, W = _kl(), _w()
    k = (kodierung or "").upper()
    if k == "BASE64":
        sauber = re.sub(rb"[^A-Za-z0-9+/=]", b"", roh or b"")
        roh = sauber[:len(sauber) - (len(sauber) % 4)]
    elif k == "QUOTED-PRINTABLE":
        roh = re.sub(rb"=[0-9A-Fa-f]?$", b"", roh or b"")
    try:
        gepackt = W.teil_entpacken(roh, k)
    except Exception:
        gepackt = roh or b""
    text = KL._text_dekodieren(gepackt, zeichensatz)
    if ist_html:
        text = re.sub(r"(?s)<!--.*?-->", " ", text)
        text = re.sub(r"(?is)<(script|style|head|title)\b.*?</\1\s*>", " ", text)
        text = re.sub(r"(?is)<br\s*/?>|</(p|div|tr|li|h\d)\s*>", "\n", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = _html_unescape(text)
    text = re.sub(r"[ \t\r\f\v ​‌͏]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()[:TEXT_MAX]


def _html_unescape(t: str) -> str:
    import html as _h
    return _h.unescape(t)


def _adressen_text(msg, koepfe) -> str:
    import email.utils as eu
    W = _w()
    teile = []
    for kopf in koepfe:
        for wert in msg.get_all(kopf) or []:
            for name, adresse in eu.getaddresses([W.dekodieren(str(wert))]):
                teile.append(("%s %s" % (name, adresse)).strip())
    return " ".join(t for t in teile if t)


def _vl():
    import verlauf
    return verlauf


def _faden_felder(satz: dict, msg) -> None:
    """5.32.0: what the conversation view needs of a header, kept in private
    keys (never in the row's public record): the reference chain, the subject
    without „Re:/AW:/Fwd:", whether it was a reply or a forward, the people
    (From/To/Cc) and whether the provider delivered it (an envelope header)."""
    V = _vl()
    satz["_bezug"] = " ".join(V.ids_aus(" ".join(
        str(msg.get(k) or "") for k in ("In-Reply-To", "References"))))
    satz["_fbetreff"], satz["_praefix"] = V.betreff_kern(satz.get("betreff") or "")
    satz["_teilnehmer"] = V.adressen_kopf(msg)
    satz["_zustell"] = 1 if any(msg.get(k) for k in ZUSTELL) else 0


def _faden_spalten(satz: dict, teilnehmer: str) -> tuple:
    return (satz.get("_bezug") or "", satz.get("_fbetreff") or "",
            int(satz.get("_praefix") or 0), teilnehmer or "",
            int(satz.get("_zustell") or 0))


def _mehr_flaggen(flaggen: str, satz: dict) -> str:
    """The flags beyond the five the list knows — `$Forwarded` above all."""
    mehr = [f for f in (satz.get("_flaggen") or []) if f.startswith("$")]
    return " ".join(sorted(set(flaggen.split()) | set(mehr))) if mehr else flaggen


# ── The build: its own connection, read-only by construction ────────────
class _Posten:
    def __init__(self, pf_id: str):
        self.pf_id = pf_id
        self.ereignis = threading.Event()
        self.faden = None
        self.halt = False
        self.aktiv = 0.0                # last request from a page
        self.lauf = 0                   # passes completed
        self.lauf_bedingung = threading.Condition()
        self.letzter = 0.0              # end of the last complete comparison
        self.fehler = ""
        self.fehler_seit = 0.0
        self.heilung = {"datum": "", "ok": None, "fehler": ""}
        self.arbeitet = False
        self.aufbau_s = None            # how long the first build took
        self.aufbau_start = 0.0
        self.worte = None               # the vocabulary, loaded once
        self.stand = (0.0, None)        # the health check's counts, briefly kept
        self.gen = 0                    # 5.32.0: bumped whenever a pass changed rows
        self.db_fehler = 0              # 5.32.1: index-file errors of the same kind in a row
        self.beiseite_um = 0.0          # 5.32.1: when the file was last put aside
        self.beiseite_n = 0


_POSTEN = {}
_P_SCHLOSS = threading.Lock()


def _posten(pf_id: str) -> _Posten:
    with _P_SCHLOSS:
        p = _POSTEN.get(pf_id)
        if p is None:
            p = _POSTEN[pf_id] = _Posten(pf_id)
        return p


def starten(pf_id: str) -> None:
    """Start the index keeper of one mailbox (idempotent)."""
    if not eingeschaltet():
        return
    p = _posten(pf_id)
    with _P_SCHLOSS:
        if p.faden is not None and p.faden.is_alive():
            return
        p.halt = False
        p.faden = threading.Thread(target=_faden, args=(p,), name="index-" + pf_id,
                                   daemon=True)
        p.faden.start()


def alle_starten() -> None:
    W = _w()
    for f in W.postfaecher() or []:
        if f.get("an", True):
            starten(f["id"])


def anhalten(pf_id: str = "") -> None:
    """Stop keepers (for the test bench) — they finish the round they are in."""
    with _P_SCHLOSS:
        posten = [p for k, p in _POSTEN.items() if not pf_id or k == pf_id]
    for p in posten:
        p.halt = True
        p.ereignis.set()
    for p in posten:
        if p.faden is not None:
            p.faden.join(timeout=15)


def anstossen(pf_id: str = "") -> None:
    """Something has changed (an action on the page, the watchman's run, the
    listening post) — compare now instead of at the next beat."""
    with _P_SCHLOSS:
        posten = [p for k, p in _POSTEN.items() if not pf_id or k == pf_id]
    for p in posten:
        p.ereignis.set()


def lebenszeichen(pf_id: str) -> None:
    """A page is in use: compare often. Starts the keeper if it is not running."""
    if not eingeschaltet():
        return
    p = _posten(pf_id)
    neu = time.time() - p.aktiv > AKTIV_FENSTER_S
    p.aktiv = time.time()
    if p.faden is None or not p.faden.is_alive():
        starten(pf_id)
    elif neu:
        p.ereignis.set()


def abgleich_abwarten(pf_id: str, frist: float = 8.0) -> bool:
    """Ask for a comparison and wait until one that began AFTER this call has
    finished — for a mail whose UID went stale. False after `frist`."""
    p = _posten(pf_id)
    if p.faden is None or not p.faden.is_alive():
        return False
    with p.lauf_bedingung:
        ziel = p.lauf + (2 if p.arbeitet else 1)
    p.ereignis.set()
    ende = time.time() + frist
    with p.lauf_bedingung:
        while p.lauf < ziel:
            rest = ende - time.time()
            if rest <= 0:
                return False
            p.lauf_bedingung.wait(rest)
    return True


def _faden(p: _Posten) -> None:
    KL = _kl()
    kasten = None
    warte = 0.0
    pause_fehler = 30.0
    while not p.halt:
        if warte > 0:
            p.ereignis.wait(warte)
        p.ereignis.clear()
        if p.halt:
            break
        jetzt = time.time()
        gruendlich = _heilen_faellig(p)
        try:
            zug = KL._zugang(p.pf_id)
            if not zug or not zug.get("adresse"):
                warte = 60.0
                continue
            if kasten is None:
                kasten = KL.Briefkasten(zug, schreiben=False)
                kasten.__enter__()
            p.arbeitet = True
            fertig = Abgleich(p, kasten).lauf(gruendlich=gruendlich)
            p.arbeitet = False
            p.letzter = time.time()
            if p.fehler:
                _log("Suchindex %s: wieder in Ordnung" % p.pf_id)
            p.fehler, p.fehler_seit, pause_fehler = "", 0.0, 30.0
            p.db_fehler = 0
            if gruendlich:
                p.heilung = {"datum": _heute(), "ok": True, "fehler": ""}
            with p.lauf_bedingung:
                p.lauf += 1
                p.lauf_bedingung.notify_all()
            if not fertig:
                warte = DROSSEL_S          # the first build goes on at once
                continue
        except Exception as e:                       # noqa: BLE001
            p.arbeitet = False
            with p.lauf_bedingung:
                p.lauf_bedingung.notify_all()
            kurz = "%s: %s" % (type(e).__name__, str(e)[:120])
            if not p.fehler:
                p.fehler_seit = jetzt
                _log("Suchindex %s: %s" % (p.pf_id, kurz))
            p.fehler = kurz
            if gruendlich:
                p.heilung = {"datum": _heute(), "ok": False, "fehler": kurz}
                _log("Suchindex %s: naechtlicher Abgleich gescheitert" % p.pf_id)
            if kasten is not None:
                KL._schliessen(kasten)
                kasten = None
            warte = pause_fehler
            pause_fehler = min(600.0, pause_fehler * 2)
            if _datei_fehler(e):
                p.db_fehler += 1
                if p.db_fehler >= DB_FEHLER_MAX and _beiseite(p):
                    p.db_fehler = 0
                    warte, pause_fehler = DROSSEL_S, 30.0   # build the new file at once
            else:
                p.db_fehler = 0              # the provider or the network, not the file
            continue
        aktiv = time.time() - p.aktiv < AKTIV_FENSTER_S
        warte = TAKT_AKTIV_S if aktiv else TAKT_RUHE_S
        if warte > LEERLAUF_S and kasten is not None:
            # 🔑 Nobody looks: let go of the provider's connection until the
            # next comparison — a mailbox allows only a handful at a time.
            KL._schliessen(kasten)
            kasten = None
    if kasten is not None:
        KL._schliessen(kasten)


def _datei_fehler(e: Exception) -> bool:
    """An error that says the index FILE is the problem (a column that is not
    there, a damaged page) — not a busy file, not the provider, not the network."""
    if not isinstance(e, sqlite3.DatabaseError):
        return False
    t = str(e).lower()
    return any(m in t for m in DB_DEFEKT)


def _beiseite(p: _Posten) -> bool:
    """🔑 5.32.1 self-healing: the keeper failed on the file several times in a
    row. Put it aside (one copy `.defekt`, 0600) and let the next pass build a
    new one. At most once a day — a file that is broken again right after a
    fresh build is a bug to see in the health check, not a loop to spin in."""
    if p.beiseite_um and time.time() - p.beiseite_um < BEISEITE_PAUSE_S:
        return False
    datei = pfad(p.pf_id)
    _leser_vergessen(datei)
    try:
        if os.path.exists(datei):
            os.replace(datei, datei + ".defekt")
            try:
                os.chmod(datei + ".defekt", 0o600)
            except OSError:
                pass
        for endung in ("-wal", "-shm"):
            try:
                os.remove(datei + endung)
            except OSError:
                pass
    except OSError as e:
        _log("Suchindex %s: Datei konnte nicht beiseitegelegt werden (%s)" %
             (p.pf_id, type(e).__name__))
        return False
    p.beiseite_um = time.time()
    p.beiseite_n += 1
    p.worte = None
    p.aufbau_start, p.aufbau_s = 0.0, None
    p.stand = (0.0, None)
    _log("Suchindex %s: Datei fehlerhaft, beiseitegelegt — wird neu aufgebaut" % p.pf_id)
    return True


def _heute() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _heilen_faellig(p: _Posten) -> bool:
    """Once a day, at night — and never while the first build runs. A
    container started in the afternoon after two nights without a comparison
    catches up at once."""
    if p.aufbau_start and p.aufbau_s is None:
        return False
    if p.heilung.get("datum") == _heute() and p.heilung.get("ok") is not None:
        return False
    try:
        with _Leser(p.pf_id) as db:
            r = db.execute("SELECT v FROM meta WHERE k='geheilt'").fetchone()
            fertig = db.execute("SELECT COUNT(*) FROM ordner WHERE fertig=0").fetchone()[0]
    except Exception:
        return False
    if fertig:
        return False
    zuletzt = r[0] if r else ""
    if zuletzt == _heute():
        return False
    jetzt = datetime.now()
    nachts = HEILEN_UM <= (jetzt.hour, jetzt.minute) < (6, 0)
    try:
        lange_her = (jetzt - datetime.strptime(zuletzt, "%Y-%m-%d")).days >= 2
    except ValueError:
        lange_her = not zuletzt
    return nachts or lange_her


class Abgleich:
    """One comparison of the index with the mailbox."""

    def __init__(self, posten: _Posten, kasten):
        self.p = posten
        self.k = kasten
        self.db = None

    # ── the marks of every folder in ONE command ────────────────────
    def marken(self) -> dict:
        """{name: {uidvalidity, uidnext, messages, modseq, unseen}}.

        🔑 LIST-STATUS (RFC 5819) answers for every folder at once — the same
        trick that took the tree from 13 s to 0.3 s in 5.27.0. A server without
        it is asked folder by folder (only then is a comparison expensive)."""
        k = self.k
        felder = "MESSAGES UIDNEXT UIDVALIDITY UNSEEN" + (
            " HIGHESTMODSEQ" if k.kann("CONDSTORE") else "")
        raus = {}
        if k.kann("LIST-STATUS"):
            typ, dat = k.m._simple_command("LIST", '""', '"*"', "RETURN",
                                           "(STATUS (%s))" % felder)
            typ, zeilen = k.m._untagged_response(typ, dat, "LIST")
            status = k.m.untagged_responses.pop("STATUS", [])
            if typ != "OK":
                raise RuntimeError("LIST-STATUS abgelehnt")
            for el in status or []:
                name, werte = self._status_zeile(el)
                if name is not None:
                    raus[name] = werte
            return raus
        for o in k.baum(True):
            if not o["waehlbar"]:
                continue
            typ, dat = k.m.status(k._zitat(o["name"]), "(%s)" % felder)
            if typ == "OK" and dat:
                _, werte = self._status_zeile(dat[0], name=o["name"])
                raus[o["name"]] = werte
        return raus

    def _marke_einzeln(self, name: str):
        """The mark of ONE folder via STATUS — or None when the server refuses."""
        k = self.k
        felder = "MESSAGES UIDNEXT UIDVALIDITY UNSEEN" + (
            " HIGHESTMODSEQ" if k.kann("CONDSTORE") else "")
        try:
            typ, dat = k.m.status(k._zitat(name), "(%s)" % felder)
        except imaplib.IMAP4.abort:
            raise
        except Exception:
            return None
        if typ != "OK" or not dat:
            return None
        _, werte = self._status_zeile(dat[0], name=name)
        if not werte or werte.get("uidvalidity") is None:
            return None
        return werte

    @staticmethod
    def _status_zeile(el, name=None):
        if not isinstance(el, bytes):
            return None, None
        s = el.decode("utf-8", "replace").strip()
        t = re.match(r'^("(?:[^"\\]|\\.)*"|\S+)\s+\((.*)\)$', s)
        if not t:
            return None, None
        if name is None:
            name = t.group(1)
            if name.startswith('"') and name.endswith('"') and len(name) > 1:
                name = name[1:-1]
        werte = {}
        for schl, feld in (("MESSAGES", "messages"), ("UIDNEXT", "uidnext"),
                           ("UIDVALIDITY", "uidvalidity"), ("UNSEEN", "unseen"),
                           ("HIGHESTMODSEQ", "modseq")):
            g = re.search(schl + r"\s+(\d+)", t.group(2), re.I)
            werte[feld] = int(g.group(1)) if g else None
        return name, werte

    # ── one pass ────────────────────────────────────────────────────
    def lauf(self, gruendlich: bool = False) -> bool:
        """True = everything is in step; False = the first build has more to do
        (the keeper comes back at once)."""
        datei = pfad(self.p.pf_id)
        self.db = _oeffnen(datei)
        try:
            return self._lauf(gruendlich)
        finally:
            try:
                self.db.close()
            except Exception:
                pass

    def _lauf(self, gruendlich: bool) -> bool:
        db, k = self.db, self.k
        marken = self.marken()               # 🔑 the marks BEFORE any data
        baum = {o["name"]: o for o in k.baum(True)}
        bekannt = {r[0]: r for r in db.execute(
            "SELECT name, uidvalidity, uidnext, messages, modseq, unseen, fertig "
            "FROM ordner")}
        # Folders that are gone (deleted, renamed): their rows go — but only at
        # the end of the pass, so a renamed folder takes its text along.
        # 🔴 A folder the tree lists but the mark answer left out (a server
        # that skips one in LIST-STATUS, a STATUS refused) must never vanish
        # from the coverage: it is asked once on its own, and if that fails
        # too, it stays „not finished" — searched on the server, not missing.
        ohne_marke = set()
        for name, o in baum.items():
            if o.get("waehlbar") and name not in marken:
                einzeln = self._marke_einzeln(name)
                if einzeln is not None:
                    marken[name] = einzeln
                else:
                    ohne_marke.add(name)
        for name in set(bekannt) - set(marken) - ohne_marke:
            db.execute("UPDATE mails SET weg=1 WHERE ordner=?", (name,))
            db.execute("DELETE FROM ordner WHERE name=?", (name,))
        for name in ohne_marke:
            self._ordner_eintragen(name, baum[name], {"messages": None}, bekannt.get(name))
            db.execute("UPDATE ordner SET fertig=0 WHERE name=?", (name,))
        # 🔴 Every folder gets its row NOW, before the first one is read: the
        # coverage („index 40 %, the rest is searched on the server") must know
        # the folders not reached yet, or they would silently be missing.
        for name, m in marken.items():
            self._ordner_eintragen(name, baum.get(name) or {}, m, bekannt.get(name))
        db.commit()
        if not self.p.aufbau_start and (not bekannt or any(not r[6] for r in bekannt.values())):
            self.p.aufbau_start = time.time()
        # Order: the inbox first, then the folders where something new arrived,
        # the rest by the tree's order. The trash and spam come last.
        rang = {"posteingang": 0, "gesendet": 1, "papierkorb": 8, "spam": 8}
        namen = sorted(marken, key=lambda n: (
            rang.get((baum.get(n) or {}).get("rolle", ""), 2),
            list(baum).index(n) if n in baum else 9999))
        alles_fertig = True
        veraendert = False
        for name in namen:
            if self.p.halt:
                return True
            m = marken[name]
            o = baum.get(name) or {}
            if o and not o.get("waehlbar", True):
                continue
            alt = bekannt.get(name)
            gleich = (alt is not None and alt[6] and alt[1] == m["uidvalidity"]
                      and alt[2] == m["uidnext"] and alt[3] == m["messages"]
                      and alt[4] == m["modseq"]
                      and (m["modseq"] is not None or alt[5] == m["unseen"]))
            if gleich and not gruendlich:
                continue
            fertig = self._ordner_abgleichen(name, m, alt, gruendlich)
            alles_fertig = alles_fertig and fertig
            veraendert = True
        # Rows that were moved away and found nowhere else: gone for good.
        weg = [r[0] for r in db.execute("SELECT id FROM mails WHERE weg=1")]
        veraendert = veraendert or bool(weg) or bool(set(bekannt) - set(marken) - ohne_marke)
        for i in range(0, len(weg), 500):
            stueck = weg[i:i + 500]
            platz = ",".join("?" * len(stueck))
            db.execute("DELETE FROM fts WHERE rowid IN (%s)" % platz, stueck)
            db.execute("DELETE FROM mails WHERE id IN (%s)" % platz, stueck)
        db.execute("INSERT OR REPLACE INTO meta VALUES('abgleich', ?)", (str(time.time()),))
        if alles_fertig and self.p.aufbau_start and self.p.aufbau_s is None:
            self.p.aufbau_s = round(time.time() - self.p.aufbau_start, 1)
            db.execute("INSERT OR REPLACE INTO meta VALUES('aufbau_s', ?)",
                       (str(self.p.aufbau_s),))
        db.commit()
        if veraendert:
            # 🔑 5.32.0: the conversation map (verlauf.py) is rebuilt lazily when
            # this number moves — never on a pass that changed nothing.
            self.p.gen += 1
        if gruendlich:
            self._pruefen()
            self.db.execute("INSERT OR REPLACE INTO meta VALUES('geheilt', ?)", (_heute(),))
            self.db.commit()
            self.p.gen += 1
        return alles_fertig

    def _ordner_eintragen(self, name, o, m, alt) -> None:
        if alt is None:
            self.db.execute(
                "INSERT OR IGNORE INTO ordner(name, rolle, zeige, kurz, fertig) "
                "VALUES(?,?,?,?,0)", (name, o.get("rolle", ""), o.get("pfad", name),
                                      o.get("zeige", name)))
        elif o:
            self.db.execute("UPDATE ordner SET rolle=?, zeige=?, kurz=? WHERE name=?",
                            (o.get("rolle", ""), o.get("pfad", name), o.get("zeige", name),
                             name))
        # 🔴 An unfinished folder's size is known at once — otherwise the
        # coverage would count only what is already in and say „100 %".
        # A finished folder keeps the mark of its last complete comparison.
        self.db.execute("UPDATE ordner SET messages=? WHERE name=? AND fertig=0",
                        (m.get("messages"), name))

    def _ordner_abgleichen(self, name: str, m: dict, alt, gruendlich: bool) -> bool:
        db, k = self.db, self.k
        # 🔴 Always select anew: a folder still selected from the last pass
        # would not be asked again, and a new UIDVALIDITY would go unseen.
        k.ordner = ""
        k.waehle(name)                      # EXAMINE — this connection cannot write
        uv = k.uidvalidity
        if uv is None or (m.get("uidvalidity") and uv != m["uidvalidity"]):
            # The folder changed between the mark and the selection — next pass.
            return False
        if alt is not None and alt[1] not in (None, uv):
            # 🔴 New UIDVALIDITY: every UID of the old one means nothing now.
            db.execute("UPDATE mails SET weg=1 WHERE ordner=?", (name,))
            db.execute("UPDATE ordner SET fertig=0, uidnext=NULL, messages=NULL, "
                       "modseq=NULL WHERE name=?", (name,))
            alt = None
        db.execute("UPDATE ordner SET uidvalidity=? WHERE name=?", (uv, name))
        db.commit()
        auf_server = set(self._alle_uids())
        im_index = {r[0]: r[1] for r in db.execute(
            "SELECT uid, flaggen FROM mails WHERE ordner=? AND weg=0", (name,))}
        fort = [u for u in im_index if u not in auf_server]
        for i in range(0, len(fort), 500):
            stueck = fort[i:i + 500]
            db.execute("UPDATE mails SET weg=1 WHERE ordner=? AND uid IN (%s)"
                       % ",".join("?" * len(stueck)), [name] + stueck)
        # Flags: with CONDSTORE only what changed since the last mark.
        bleibt = [u for u in im_index if u in auf_server]
        if bleibt:
            seit = alt[4] if (alt is not None and alt[4] and not gruendlich
                              and m.get("modseq") is not None) else None
            neu_flaggen = self._flaggen(bleibt, seit)
            for u, f in neu_flaggen.items():
                if u in im_index and im_index[u] != f:
                    db.execute("UPDATE mails SET flaggen=? WHERE ordner=? AND uid=?",
                               (f, name, u))
        db.commit()
        neu = sorted((u for u in auf_server if u not in im_index), reverse=True)
        fertig = True
        if not gruendlich and len(neu) > RUNDE_JE_ORDNER:
            neu, fertig = neu[:RUNDE_JE_ORDNER], False
        for i in range(0, len(neu), STUECK):
            if self.p.halt:
                return False
            self._aufnehmen(name, neu[i:i + STUECK])
            db.commit()
            time.sleep(DROSSEL_S)
        if gruendlich:
            self._nachholen(name)
        if fertig:
            # 🔑 The mark read BEFORE the data — older than the data at worst,
            # never newer. A mail that arrived meanwhile changes UIDNEXT, and
            # the next pass sees it.
            db.execute("UPDATE ordner SET uidnext=?, messages=?, modseq=?, unseen=?, "
                       "fertig=1, geprueft=? WHERE name=?",
                       (m.get("uidnext"), m.get("messages"), m.get("modseq"),
                        m.get("unseen"), time.time(), name))
        db.commit()
        return fertig

    def _alle_uids(self) -> list:
        typ, dat = self.k.m.uid("SEARCH", "ALL")
        if typ != "OK":
            raise RuntimeError("UID SEARCH abgelehnt")
        roh = b" ".join(x for x in (dat or []) if x)
        return [int(x) for x in roh.split()]

    def _flaggen(self, uids: list, seit) -> dict:
        """{uid: 'flags'} — everything, or with CONDSTORE only what changed."""
        k, raus = self.k, {}
        if seit:
            typ, daten = k.m.uid("FETCH", "1:*", "(UID FLAGS) (CHANGEDSINCE %d)" % seit)
            stuecke = [daten] if typ == "OK" else None
            if stuecke is None:
                return self._flaggen(uids, None)
        else:
            stuecke = []
            for st in k._uid_stuecke(uids):
                typ, daten = k.m.uid("FETCH", k._uid_satz(st), "(UID FLAGS)")
                if typ != "OK":
                    raise RuntimeError("Flaggen nicht lesbar")
                stuecke.append(daten)
        for daten in stuecke:
            for el in daten or []:
                if isinstance(el, tuple):
                    el = el[0]
                if not isinstance(el, bytes):
                    continue
                zl = el.decode("utf-8", "replace")
                u = re.search(r"UID\s+(\d+)", zl)
                f = re.search(r"FLAGS\s+\(([^)]*)\)", zl)
                if u and f:
                    raus[int(u.group(1))] = " ".join(sorted(x.lower() for x in f.group(1).split()))
        return raus

    # ── taking mails in ─────────────────────────────────────────────
    def _aufnehmen(self, ordner: str, uids: list) -> None:
        KL, W = _kl(), _w()
        k, db = self.k, self.db
        teil = ",".join(str(u) for u in uids)
        typ, daten = k.m.uid(
            "FETCH", teil,
            "(UID FLAGS INTERNALDATE RFC822.SIZE BODY.PEEK[HEADER.FIELDS "
            "(FROM TO CC SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES "
            "LIST-UNSUBSCRIBE REPLY-TO X-ENVELOPE-TO X-ORIGINAL-TO ENVELOPE-TO "
            "DELIVERED-TO)])")
        if typ != "OK":
            raise RuntimeError("Koepfe nicht lesbar")
        saetze = KL.Briefkasten._koepfe_lesen(daten)
        roh_kopf = dict(KL._stuecke_je_uid(daten))
        for u, satz in saetze.items():
            _faden_felder(satz, W.msg_aus_bytes(roh_kopf.get(u) or b""))
        intern = {}
        for el in daten or []:
            rand = (el[0] if isinstance(el, tuple) else el) or b""
            if isinstance(rand, bytes):
                s = rand.decode("utf-8", "replace")
                u = re.search(r"UID\s+(\d+)", s)
                d = re.search(r'INTERNALDATE\s+"([^"]+)"', s)
                if u and d:
                    intern[int(u.group(1))] = KL._intern_sekunden(d.group(1))
        # 🔑 A mail that was MOVED is the same mail: same Message-Id, same size.
        # Its text is taken from the row it left (or from its twin in another
        # folder) instead of being fetched again.
        neu_holen = []
        for u in uids:
            satz = saetze.get(u)
            if satz is None:
                continue
            mid = satz.get("message_id") or ""
            vorbild = None
            if mid:
                vorbild = db.execute(
                    "SELECT id, weg FROM mails WHERE msgid=? AND groesse=? "
                    "ORDER BY weg DESC LIMIT 1", (mid, satz.get("groesse") or 0)).fetchone()
            if vorbild is not None:
                self._uebernehmen(ordner, u, satz, intern.get(u), vorbild)
            else:
                neu_holen.append(u)
        if not neu_holen:
            return
        strukturen = k.strukturen(neu_holen)
        gruppen, auszug_gruppen, anhang_texte = {}, {}, {}
        for u in neu_holen:
            struct = strukturen.get(u)
            # 🔴 MEASURED 13.11.2026 on the real mailbox: of the mails the
            # server found and the index did not, almost all had the word in
            # the HTML part only — the plain alternative said less, or nothing
            # but „view in your browser". So the BODY for searching comes from
            # the HTML part where there is one (what the reader sees), and the
            # list excerpt stays where the list takes it from (plain first).
            t = self._koerper_teil(struct)
            if t:
                gruppen.setdefault(t, []).append(u)
            a = KL._text_teil(struct)
            if a and a != t:
                auszug_gruppen.setdefault(a, []).append(u)
            for a in self._text_anhaenge(struct):
                anhang_texte.setdefault(u, []).append(a)
        texte = self._texte(gruppen)
        for u, auszug in k._auszuege_nach_teil(auszug_gruppen).items():
            if u in texte:
                texte[u] = (texte[u][0], auszug)
            else:
                texte[u] = ("", auszug)
        anh_texte = self._anhang_texte(anhang_texte)
        for u in neu_holen:
            satz = saetze[u]
            struct = strukturen.get(u)
            anh, _ = KL.anhang_und_inline(struct)
            satz["anhang"] = len(anh)
            satz["anhang_gross"] = sum(a["b"] for a in anh)
            roh, auszug = texte.get(u, ("", ""))
            satz["auszug"] = auszug
            msg = W.msg_aus_bytes(roh_kopf.get(u) or b"")
            an = _adressen_text(msg, ("To", "Cc", "Reply-To") + ZUSTELL)
            namen = " ".join(a.get("n", "") for a in anh)
            text = roh
            if anh_texte.get(u):
                text = (text + "\n" + anh_texte[u])[:TEXT_MAX]
            self._schreiben(ordner, u, satz, intern.get(u), an, namen, text)

    def _platz_machen(self, ordner, uid, ausser=None) -> None:
        """Remove a row (and its text) that holds this folder+UID — a left-over
        of an old UIDVALIDITY. `INSERT OR REPLACE` alone would leave its text
        behind in the full-text table."""
        for (rid,) in self.db.execute("SELECT id FROM mails WHERE ordner=? AND uid=?",
                                      (ordner, uid)).fetchall():
            if rid != ausser:
                self.db.execute("DELETE FROM fts WHERE rowid=?", (rid,))
                self.db.execute("DELETE FROM mails WHERE id=?", (rid,))

    def _schreiben(self, ordner, uid, satz, intern, an, namen, text) -> None:
        db = self.db
        self._platz_machen(ordner, uid)
        flaggen = " ".join(sorted(f for f, an_ in (
            ("\\seen", satz.get("gelesen")), ("\\flagged", satz.get("markiert")),
            ("\\answered", satz.get("beantwortet")), ("\\draft", satz.get("entwurf")),
            ("\\deleted", satz.get("geloescht"))) if an_))
        flaggen = _mehr_flaggen(flaggen, satz)
        von = "%s %s" % (satz.get("von") or "", satz.get("adresse") or "")
        kopf = falten("%s %s" % (satz.get("betreff") or "", von))
        oeffentlich = {k: v for k, v in satz.items() if not k.startswith("_")}
        datum = intern or _iso_sekunden(satz.get("zeit"))
        teilnehmer = satz.get("_teilnehmer") or ""
        if satz.get("_praefix") == 2 and text:
            # 🔑 A forward names the original's sender only in its body („From:
            # …" of the quoted block). Those addresses join the participants, so
            # a forward without References can still be matched — by people,
            # not by the subject alone.
            teilnehmer = _vl().teilnehmer_mit_text(teilnehmer, text)
        cur = db.execute(
            "INSERT INTO mails(ordner, uid, datum, msgid, groesse, flaggen, "
            "anhang, kopf, satz, roh, weg, bezug, fbetreff, praefix, teilnehmer, zustell) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)",
            (ordner, uid, datum, satz.get("message_id") or "", satz.get("groesse") or 0,
             flaggen, int(satz.get("anhang") or 0), kopf,
             json.dumps(oeffentlich, ensure_ascii=False),
             zlib.compress((text or "").encode("utf-8"), 6))
            + _faden_spalten(satz, teilnehmer))
        rid = cur.lastrowid
        felder = (falten(satz.get("betreff") or ""), falten(von), falten(an),
                  falten(namen), falten(text or ""))
        db.execute("INSERT INTO fts(rowid, betreff, von, an, anhang, text) "
                   "VALUES(?,?,?,?,?,?)", (rid,) + felder)
        self._worte(" ".join(felder))

    def _uebernehmen(self, ordner, uid, satz, intern, vorbild) -> None:
        """A moved (or copied) mail: header from the server, text from the row."""
        db = self.db
        alt_id, weg = vorbild
        zeile = db.execute("SELECT satz, roh, teilnehmer FROM mails WHERE id=?",
                           (alt_id,)).fetchone()
        fts = db.execute("SELECT betreff, von, an, anhang, text FROM fts WHERE rowid=?",
                         (alt_id,)).fetchone()
        if zeile is None or fts is None:
            return self._schreiben(ordner, uid, satz, intern, "", "", "")
        alt = json.loads(zeile[0] or "{}")
        for feld in ("anhang", "anhang_gross", "auszug"):
            if feld in alt:
                satz[feld] = alt[feld]
        flaggen = " ".join(sorted(f for f, an_ in (
            ("\\seen", satz.get("gelesen")), ("\\flagged", satz.get("markiert")),
            ("\\answered", satz.get("beantwortet")), ("\\draft", satz.get("entwurf")),
            ("\\deleted", satz.get("geloescht"))) if an_))
        flaggen = _mehr_flaggen(flaggen, satz)
        oeffentlich = {k: v for k, v in satz.items() if not k.startswith("_")}
        datum = intern or _iso_sekunden(satz.get("zeit"))
        kopf = falten("%s %s %s" % (satz.get("betreff") or "", satz.get("von") or "",
                                    satz.get("adresse") or ""))
        # The row's participants already carry what its text added (a forward).
        spalten = _faden_spalten(satz, zeile[2] or satz.get("_teilnehmer") or "")
        if weg:
            # The very row that left: it moves along, the text stays where it is.
            self._platz_machen(ordner, uid, ausser=alt_id)
            db.execute("UPDATE mails SET ordner=?, uid=?, datum=?, flaggen=?, kopf=?, "
                       "satz=?, weg=0, bezug=?, fbetreff=?, praefix=?, teilnehmer=?, "
                       "zustell=? WHERE id=?",
                       (ordner, uid, datum, flaggen, kopf,
                        json.dumps(oeffentlich, ensure_ascii=False)) + spalten + (alt_id,))
            return
        self._platz_machen(ordner, uid)
        cur = db.execute(
            "INSERT INTO mails(ordner, uid, datum, msgid, groesse, flaggen, "
            "anhang, kopf, satz, roh, weg, bezug, fbetreff, praefix, teilnehmer, zustell) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)",
            (ordner, uid, datum, satz.get("message_id") or "", satz.get("groesse") or 0,
             flaggen, int(satz.get("anhang") or 0), kopf,
             json.dumps(oeffentlich, ensure_ascii=False), zeile[1]) + spalten)
        rid = cur.lastrowid
        db.execute("INSERT INTO fts(rowid, betreff, von, an, anhang, text) "
                   "VALUES(?,?,?,?,?,?)", (rid,) + tuple(fts))

    def _worte(self, gefaltet: str) -> None:
        """The vocabulary the tolerant stage looks for similar words in."""
        db = self.db
        if self.p.worte is None:
            self.p.worte = {r[0] for r in db.execute("SELECT w FROM worte")}
        for w in set(re.findall(r"[a-z0-9]{4,30}", gefaltet)) - self.p.worte:
            if w.isdigit():
                continue
            self.p.worte.add(w)
            cur = db.execute("INSERT OR IGNORE INTO worte(w) VALUES(?)", (w,))
            if cur.rowcount:
                db.execute("INSERT INTO worte_fts(rowid, w) VALUES(?, ?)",
                           (cur.lastrowid, w))

    def _texte(self, gruppen: dict) -> dict:
        """{uid: (full text, excerpt)} — still BODY.PEEK, still only the text
        part, and that only up to ROH_MAX bytes. An attachment is never fetched
        for the body."""
        KL = _kl()
        raus = {}
        stueck = max(1, STUECK // 2)       # up to ROH_MAX each: keep one answer small
        for (nr, kod, satz, sub), liste in gruppen.items():
            for i in range(0, len(liste), stueck):
                teil = ",".join(str(u) for u in sorted(liste[i:i + stueck]))
                typ, daten = self.k.m.uid(
                    "FETCH", teil, "(UID BODY.PEEK[%s]<0.%d>)" % (nr, ROH_MAX))
                if typ != "OK":
                    continue
                for uid, roh in KL._stuecke_je_uid(daten):
                    try:
                        text = _volltext(roh, kod, satz, sub == "html")
                        auszug = KL._auszug_aus(roh[:KL.AUSZUG_BYTES], kod, satz,
                                                sub == "html")
                    except Exception:
                        text, auszug = "", ""
                    raus[uid] = (text, auszug)
        return raus

    @staticmethod
    def _koerper_teil(struct):
        """(part, ENCODING, charset, subtype) the searchable body is read
        from: the HTML part if the letter has one, else the plain one."""
        KL, W = _kl(), _w()
        teile = W._teile(struct) if struct else []
        t = KL._erster_text(teile, "html") or KL._erster_text(teile, "plain")
        if not t:
            return None
        return (t["nr"], (t.get("kodierung") or "").upper(), t.get("zeichensatz") or "",
                t["subtyp"].lower())

    @staticmethod
    def _text_anhaenge(struct) -> list:
        """Attached TEXT files (txt, csv, ics, html …) small enough to read
        along — an HTML attachment is often the whole letter (measured)."""
        W = _w()
        raus = []
        for t in (W._teile(struct) if struct else []):
            if t["typ"].upper() != "TEXT":
                continue
            if not ((t.get("name") or "").strip() or t.get("verfuegung") == "attachment"):
                continue
            if int(t.get("groesse") or 0) > ANHANG_TEXT_MAX:
                continue
            raus.append(t)
        return raus[:3]

    def _anhang_texte(self, je_uid: dict) -> dict:
        raus = {}
        for uid, teile in je_uid.items():
            stuecke = []
            for t in teile:
                typ, daten = self.k.m.uid(
                    "FETCH", str(uid), "(UID BODY.PEEK[%s]<0.%d>)" % (t["nr"], ANHANG_TEXT_MAX))
                if typ != "OK":
                    continue
                for _, roh in _kl()._stuecke_je_uid(daten):
                    stuecke.append(_volltext(roh, t.get("kodierung") or "",
                                             t.get("zeichensatz") or "",
                                             t["subtyp"].lower() == "html"))
            if stuecke:
                raus[uid] = "\n".join(stuecke)
        return raus

    def _nachholen(self, ordner: str) -> None:
        """The nightly pass: rows without text although the mail has a text
        part (a failed fetch) are read once more."""
        db = self.db
        leer = [r[0] for r in db.execute(
            "SELECT m.uid FROM mails m JOIN fts f ON f.rowid=m.id "
            "WHERE m.ordner=? AND m.weg=0 AND f.text='' LIMIT 400", (ordner,))]
        if not leer:
            return
        for i in range(0, len(leer), STUECK):
            stueck = leer[i:i + STUECK]
            platz = ",".join("?" * len(stueck))
            ids = [r[0] for r in db.execute(
                "SELECT id FROM mails WHERE ordner=? AND uid IN (%s)" % platz,
                [ordner] + stueck)]
            if ids:
                db.execute("DELETE FROM fts WHERE rowid IN (%s)" % ",".join("?" * len(ids)), ids)
                db.execute("DELETE FROM mails WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)
            self._aufnehmen(ordner, stueck)
            db.commit()
            time.sleep(DROSSEL_S)

    def _pruefen(self) -> None:
        """Self-healing: a damaged file is thrown away and built again."""
        try:
            ok = self.db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            self.db.execute("INSERT INTO fts(fts) VALUES('integrity-check')")
        except sqlite3.DatabaseError:
            ok = False
        if not ok:
            _log("Suchindex %s: Datei beschaedigt, wird neu aufgebaut" % self.p.pf_id)
            datei = pfad(self.p.pf_id)
            self.db.close()
            _leser_vergessen(datei)
            _datei_entfernen(datei)
            self.db = _oeffnen(datei)


def _iso_sekunden(iso) -> float:
    try:
        return datetime.fromisoformat(str(iso)).timestamp()
    except (TypeError, ValueError):
        return 0.0


# ── Asking the index ─────────────────────────────────────────────────────
FELDER = {"von": "von", "from": "von",
          "betreff": "betreff", "subject": "betreff", "asunto": "betreff",
          "objet": "betreff", "oggetto": "betreff",
          "an": "an", "to": "an", "para": "an",
          "text": "text", "texto": "text", "testo": "text", "texte": "text",
          "anhang": "anhang", "attachment": "anhang", "datei": "anhang",
          "adjunto": "anhang", "piece": "anhang", "allegato": "anhang",
          "ordner": "ordner", "folder": "ordner", "carpeta": "ordner",
          "dossier": "ordner", "cartella": "ordner"}
SPALTEN = ("betreff", "von", "an", "anhang", "text")


def zerlegen(frage: str, feld: str = "alle") -> list:
    """[(column or '', folded term)] — free words, „quoted phrases" and simple
    prefixes (`von:`, `betreff:`, `an:`, `text:`, `anhang:`, `ordner:`). Without
    any of that, every word may stand in any field."""
    vorgabe = feld if feld in SPALTEN else ""
    raus = []
    for t in re.finditer(r'(?:(\w+):)?(?:"([^"]*)"|(\S+))', frage or ""):
        praefix, phrase, wort = t.group(1), t.group(2), t.group(3)
        spalte = vorgabe
        if praefix:
            schl = falten(praefix)
            if schl in FELDER:
                spalte = FELDER[schl]
            else:
                # not a known field: the colon was part of the word (12:30, a URL)
                wort = "%s:%s" % (praefix, phrase if phrase is not None else wort)
                phrase = None
        begriff = falten((phrase if phrase is not None else wort) or "").strip().strip('"').strip()
        if begriff:
            raus.append((spalte, begriff))
    return raus


def _fts_begriff(spalte: str, begriff: str) -> str:
    q = '"%s"' % begriff.replace('"', '""')
    return ("%s : %s" % (spalte, q)) if spalte else q


def _aehnlich(a: str, b: str, grenze: int) -> int:
    """Smallest edit distance of `a` to any piece of `b` (Sellers) — „rechnug"
    is one step from a piece of „stromrechnung". > grenze = not similar."""
    vorher = [0] * (len(b) + 1)
    for i, ca in enumerate(a, 1):
        jetzt = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            jetzt[j] = min(vorher[j] + 1, jetzt[j - 1] + 1,
                           vorher[j - 1] + (ca != cb))
        vorher = jetzt
    return min(vorher)


def _erlaubt(begriff: str) -> int:
    return 1 if len(begriff) <= 6 else 2


def aehnliche_worte(db, begriff: str) -> list:
    """Words of the vocabulary close to a (misspelled) term, best first."""
    if len(begriff) < 4 or not re.fullmatch(r"[a-z0-9]+", begriff):
        return []
    drei = {begriff[i:i + 3] for i in range(len(begriff) - 2)}
    if not drei:
        return []
    q = " OR ".join('"%s"' % d for d in sorted(drei))
    try:
        kandidaten = [r[0] for r in db.execute(
            "SELECT w FROM worte_fts WHERE worte_fts MATCH ? ORDER BY rank LIMIT 400", (q,))]
    except sqlite3.OperationalError:
        return []
    grenze = _erlaubt(begriff)
    bewertet = []
    for w in kandidaten:
        if begriff in w:
            continue
        if abs(len(w) - len(begriff)) > 40:
            continue
        d = _aehnlich(begriff, w, grenze)
        if d <= grenze:
            bewertet.append((d, abs(len(w) - len(begriff)), w))
    bewertet.sort()
    return [w for _, _, w in bewertet[:WORTE_JE_BEGRIFF]]


def bereit(pf_id: str) -> bool:
    """Is there an index that can answer at all?"""
    if not eingeschaltet():
        return False
    try:
        return os.path.exists(pfad(pf_id))
    except Exception:
        return False


def abdeckung(pf_id: str, mit_korb: bool = False, ordner: str = "") -> dict:
    """How far the index is: {anteil, fertig, offen: [folder names], mails}."""
    with _Leser(pf_id) as db:
        zeilen = db.execute("SELECT name, rolle, fertig, messages, uidvalidity FROM ordner").fetchall()
        gezaehlt = dict(db.execute(
            "SELECT ordner, COUNT(*) FROM mails WHERE weg=0 GROUP BY ordner").fetchall())
    offen, soll, ist = [], 0, 0
    for name, rolle, fertig, messages, uv in zeilen:
        if ordner and name != ordner:
            continue
        if not ordner and not mit_korb and rolle in ("papierkorb", "spam"):
            continue
        n = gezaehlt.get(name, 0)
        if fertig:
            soll += n
            ist += n
        else:
            offen.append(name)
            soll += max(int(messages or 0), n)
            ist += n
    if ordner and not zeilen:
        offen = [ordner]
    elif ordner and ordner not in {z[0] for z in zeilen}:
        offen = [ordner]
    anteil = 1.0 if soll == 0 and not offen else (ist / soll if soll else 0.0)
    if offen:
        anteil = min(anteil, 0.99)      # a folder of unknown size is never „100 %"
    return {"anteil": round(min(1.0, anteil), 4), "fertig": not offen,
            "offen": offen, "mails": ist}


def suchen(pf_id: str, frage: str, feld: str = "alle", sieb: str = "",
           ordner: str = "", mit_korb: bool = False) -> dict:
    """Every hit, best first: {treffer: [(datum, ordner, uid, id)], gesamt,
    unscharf: [folded words tried instead], begriffe: [folded terms], ms}.

    🔑 Several words: ALL must occur, each in any field. Ranking: a hit whose
    every word stands in subject or sender comes first, one with some of them
    next, a hit in the text only after that — and inside each group the newest
    first. When the exact words find (almost) nothing, misspelled words are
    swapped for similar ones from the index's own vocabulary."""
    t0 = time.time()
    begriffe = zerlegen(frage, feld)
    ordner_filter = [b for s, b in begriffe if s == "ordner"]
    begriffe = [(s, b) for s, b in begriffe if s != "ordner"]
    with _Leser(pf_id) as db:
        orte = {r[0]: (r[1] or "") for r in db.execute("SELECT name, rolle FROM ordner")}
        erlaubt = set()
        for name, rolle in orte.items():
            if ordner and name != ordner:
                continue
            if not ordner and not mit_korb and rolle in ("papierkorb", "spam"):
                continue
            if ordner_filter:
                pf_ = falten(_kl().utf7_dekodieren(name))
                if not all(f in pf_ for f in ordner_filter):
                    continue
            erlaubt.add(name)
        exakt = _abfragen(db, begriffe, sieb, erlaubt)
        unscharf = []
        if len(exakt) < UNSCHARF_AB and begriffe:
            ersatz, versucht = [], False
            for s, b in begriffe:
                worte = aehnliche_worte(db, b) if len(b) >= 4 else []
                if worte:
                    versucht = True
                    unscharf.extend(worte)
                    ersatz.append((s, [b] + worte))
                else:
                    ersatz.append((s, [b]))
            if versucht:
                gesehen = {r[3] for r in exakt}
                weitere = [r for r in _abfragen(db, ersatz, sieb, erlaubt, oder=True)
                           if r[3] not in gesehen]
                for r in weitere:
                    r[4] += 10            # after every exact hit
                exakt = exakt + weitere
    exakt.sort(key=lambda r: (r[4], -(r[0] or 0), -r[2]))
    return {"treffer": [(r[0] or 0.0, r[1], r[2], r[3]) for r in exakt],
            "gesamt": len(exakt), "unscharf": unscharf,
            "begriffe": [b for _, b in begriffe] + unscharf,
            "ms": round((time.time() - t0) * 1000, 1)}


def _abfragen(db, begriffe, sieb: str, erlaubt: set, oder: bool = False) -> list:
    """[[datum, ordner, uid, id, rang]] for the terms. `oder=True`: every
    entry of `begriffe` is (column, [alternatives]) — one of them must occur."""
    lang, kurz = [], []
    for s, b in begriffe:
        alternativen = b if oder else [b]
        if all(len(a) >= 3 for a in alternativen):
            teil = " OR ".join(_fts_begriff(s, a) for a in alternativen)
            lang.append("(%s)" % teil if len(alternativen) > 1 else teil)
        else:
            kurz.append((s, alternativen))
    sql = ("SELECT m.datum, m.ordner, m.uid, m.id, m.kopf, m.flaggen, m.anhang "
           "FROM mails m ")
    werte = []
    bedingungen = ["m.weg=0"]
    if lang:
        sql += "JOIN fts ON fts.rowid=m.id "
        bedingungen.append("fts MATCH ?")
        werte.append(" AND ".join(lang))
    elif kurz:
        sql += "JOIN fts ON fts.rowid=m.id "
    for s, alternativen in kurz:
        spalten = [s] if s else list(SPALTEN)
        oder_teile = []
        for a in alternativen:
            for sp in spalten:
                oder_teile.append("instr(fts.%s, ?) > 0" % sp)
                werte.append(a)
        bedingungen.append("(%s)" % " OR ".join(oder_teile))
    sql += "WHERE " + " AND ".join(bedingungen)
    try:
        zeilen = db.execute(sql, werte).fetchall()
    except sqlite3.OperationalError as e:
        _log("Suchindex: Anfrage abgelehnt: %s" % str(e)[:100])
        return []
    raus = []
    alle_begriffe = [(b if oder else [b]) for _, b in begriffe]
    for datum, ordner, uid, rid, kopf, flaggen, anhang in zeilen:
        if ordner not in erlaubt:
            continue
        flaggen = flaggen or ""
        if sieb == "ungelesen" and "\\seen" in flaggen:
            continue
        if sieb == "markiert" and "\\flagged" not in flaggen:
            continue
        if sieb == "unbeantwortet" and "\\answered" in flaggen:
            continue
        if sieb == "anhang" and not anhang:
            continue
        kopf = kopf or ""
        im_kopf = sum(1 for alt in alle_begriffe if any(a in kopf for a in alt))
        rang = 0 if alle_begriffe and im_kopf == len(alle_begriffe) else (1 if im_kopf else 2)
        raus.append([datum, ordner, uid, rid, rang])
    return raus


def saetze(pf_id: str, ids: list, begriffe: list) -> dict:
    """{id: row record} for a page of hits — straight from the index, no round
    trip to the server. Every record carries `fund`: a piece of the text around
    the first hit, as [[text, is_hit], …], so the page can mark it without ever
    putting text into HTML unescaped."""
    raus = {}
    if not ids:
        return raus
    with _Leser(pf_id) as db:
        orte = {r[0]: r for r in db.execute("SELECT name, zeige, kurz, rolle FROM ordner")}
        platz = ",".join("?" * len(ids))
        for rid, ordner, uid, flaggen, satz, roh in db.execute(
                "SELECT id, ordner, uid, flaggen, satz, roh FROM mails WHERE id IN (%s)"
                % platz, list(ids)):
            try:
                s = json.loads(satz or "{}")
            except ValueError:
                continue
            f = flaggen or ""
            s.update({"uid": uid, "gelesen": "\\seen" in f, "markiert": "\\flagged" in f,
                      "beantwortet": "\\answered" in f, "entwurf": "\\draft" in f,
                      "geloescht": "\\deleted" in f})
            ort = orte.get(ordner) or (ordner, ordner, ordner, "")
            s["ordner"] = ordner
            s["ordner_zeige"] = ort[1] or ordner
            s["ordner_kurz"] = ort[2] or ordner
            s["rolle"] = ort[3] or ""
            s["aus_index"] = True
            try:
                text = zlib.decompress(roh).decode("utf-8") if roh else ""
            except Exception:
                text = ""
            fund = ausschnitt(text, begriffe)
            if fund:
                s["fund"] = fund
            betreff = markieren(s.get("betreff") or "", begriffe)
            if betreff:
                s["betreff_fund"] = betreff
            raus[rid] = s
    return raus


def _stellen(text: str, begriffe: list) -> list:
    """[(start, end)] in the ORIGINAL text of every occurrence of a term."""
    if not text or not begriffe:
        return []
    gefaltet, karte = falten_mit_karte(text)
    stellen = []
    for b in sorted(set(begriffe), key=len, reverse=True):
        if not b:
            continue
        start = 0
        while True:
            i = gefaltet.find(b, start)
            if i < 0:
                break
            ende = i + len(b) - 1
            stellen.append((karte[i], karte[ende] + 1))
            start = i + max(1, len(b))
            if len(stellen) > 200:
                break
    stellen.sort()
    zusammen = []
    for a, e in stellen:
        if zusammen and a <= zusammen[-1][1]:
            zusammen[-1] = (zusammen[-1][0], max(e, zusammen[-1][1]))
        else:
            zusammen.append((a, e))
    return zusammen


def _segmente(text: str, stellen: list, von: int, bis: int) -> list:
    raus, pos = [], von
    for a, e in stellen:
        if e <= von or a >= bis:
            continue
        a, e = max(a, von), min(e, bis)
        if a > pos:
            raus.append([text[pos:a], False])
        raus.append([text[a:e], True])
        pos = e
    if pos < bis:
        raus.append([text[pos:bis], False])
    return raus


def markieren(text: str, begriffe: list) -> list:
    """The whole (short) text as segments — empty when nothing is in it."""
    stellen = _stellen(text, begriffe)
    return _segmente(text, stellen, 0, len(text)) if stellen else []


def ausschnitt(text: str, begriffe: list) -> list:
    """A piece of a letter around its first hit, as segments.

    🔑 Only the piece around the hit is mapped character by character: a page
    of fifty hits with up to 100 000 characters each cost 0.3 s that way
    (measured on the real mailbox). The hit is LOCATED with the fast fold over
    overlapping pieces, then the exact positions are worked out in a window."""
    if not text or not begriffe:
        return []
    rand = 64
    start = _fundstueck(text, begriffe, 0, len(text), 4000, rand)
    if start is not None:
        # and inside that piece the smaller one — the window stays a few
        # hundred characters, whatever the length of the letter
        start = _fundstueck(text, begriffe, start, start + 4000 + rand, 400, rand)
    if start is None:
        return []
    von = max(0, start - AUSSCHNITT - 20)
    bis = min(len(text), start + 400 + rand + AUSSCHNITT * 2 + 20)
    segmente = _ausschnitt_kern(text[von:bis], begriffe)
    if segmente and von > 0 and not segmente[0][0].startswith("… "):
        segmente[0][0] = "… " + segmente[0][0].lstrip()
    if segmente and bis < len(text) and not segmente[-1][0].endswith(" …"):
        segmente[-1][0] = segmente[-1][0].rstrip() + " …"
    return segmente


def _fundstueck(text, begriffe, von, bis, stueck, rand):
    """Start of the first piece of `text[von:bis]` whose folded form holds a
    term — or None."""
    for start in range(von, min(bis, len(text)), stueck):
        gefaltet = falten(text[start:start + stueck + rand])
        if any(b and b in gefaltet for b in begriffe):
            return start
    return None


def _ausschnitt_kern(text: str, begriffe: list) -> list:
    stellen = _stellen(text, begriffe)
    if not stellen:
        return []
    a = stellen[0][0]
    von = max(0, a - AUSSCHNITT)
    # start at a word boundary, end at one
    if von > 0:
        leer = text.find(" ", von, min(a, von + 20))   # the next word start after `von`
        von = leer + 1 if leer >= 0 else von
    bis = min(len(text), a + AUSSCHNITT * 2)
    if bis < len(text):
        leer = text.find(" ", bis, bis + 15)
        bis = leer if leer >= 0 else bis
    segmente = _segmente(text, stellen, von, bis)
    for s in segmente:
        s[0] = re.sub(r"\s+", " ", s[0])
    if von > 0 and segmente:
        segmente[0][0] = "… " + segmente[0][0].lstrip()
    if bis < len(text) and segmente:
        segmente[-1][0] = segmente[-1][0].rstrip() + " …"
    return segmente


def ort_fuer(pf_id: str, ordner: str, uid: int) -> dict:
    """{msgid, groesse} of a row — to find a mail again after it moved."""
    try:
        with _Leser(pf_id) as db:
            r = db.execute("SELECT msgid, groesse FROM mails WHERE ordner=? AND uid=?",
                           (ordner, int(uid))).fetchone()
    except Exception:
        return {}
    return {"msgid": r[0], "groesse": r[1]} if r else {}


def wo_jetzt(pf_id: str, msgid: str, groesse: int = 0) -> list:
    """[(folder, uid)] where a Message-Id stands now (after a comparison)."""
    if not msgid:
        return []
    try:
        with _Leser(pf_id) as db:
            zeilen = db.execute(
                "SELECT ordner, uid, groesse FROM mails WHERE msgid=? AND weg=0 "
                "ORDER BY datum DESC", (msgid,)).fetchall()
    except Exception:
        return []
    passend = [(o, u) for o, u, g in zeilen if not groesse or g == groesse]
    return passend or [(o, u) for o, u, _ in zeilen]


def msgids_fuer(pf_id: str, frage: str, seit: float = 0.0, mit_korb: bool = False) -> set:
    """The Message-Ids of every hit (for the read-only door)."""
    erg = suchen(pf_id, frage, mit_korb=mit_korb)
    ids = [r[3] for r in erg["treffer"] if (r[0] or 0) >= seit][:5000]
    raus = set()
    if not ids:
        return raus
    with _Leser(pf_id) as db:
        for i in range(0, len(ids), 500):
            st = ids[i:i + 500]
            for (mid,) in db.execute("SELECT msgid FROM mails WHERE id IN (%s)"
                                     % ",".join("?" * len(st)), st):
                if mid:
                    raus.add(mid)
    return raus


def stand(pf_id: str) -> dict:
    """Counts only — for the health check and the page. No content, no names.
    Kept for ten seconds: Docker asks every half minute."""
    p = _POSTEN.get(pf_id)
    if p is not None and time.time() - p.stand[0] < 10 and p.stand[1] is not None:
        return dict(p.stand[1])
    raus = _stand(pf_id, p)
    if p is not None:
        p.stand = (time.time(), raus)
    return dict(raus)


def _stand(pf_id: str, p) -> dict:
    raus = {"an": eingeschaltet(), "laeuft": bool(p and p.faden and p.faden.is_alive())}
    datei = None
    try:
        datei = pfad(pf_id)
    except Exception:
        pass
    if not datei or not os.path.exists(datei):
        raus.update({"mails": 0, "ordner": 0, "aufbau_prozent": 0})
        return raus
    try:
        groesse = sum(os.path.getsize(datei + e) for e in ("", "-wal", "-shm")
                      if os.path.exists(datei + e))
        a = abdeckung(pf_id, mit_korb=True)
        with _Leser(pf_id) as db:
            ordner_n = db.execute("SELECT COUNT(*) FROM ordner").fetchone()[0]
            fertig_n = db.execute("SELECT COUNT(*) FROM ordner WHERE fertig=1").fetchone()[0]
            letzter = db.execute("SELECT v FROM meta WHERE k='abgleich'").fetchone()
            aufbau = db.execute("SELECT v FROM meta WHERE k='aufbau_s'").fetchone()
        raus.update({
            "mails": a["mails"], "ordner": ordner_n, "ordner_fertig": fertig_n,
            "aufbau_prozent": int(a["anteil"] * 100) if not a["fertig"] else 100,
            "abgleich_alter_s": (round(time.time() - float(letzter[0]))
                                 if letzter else None),
            "aufbau_s": float(aufbau[0]) if aufbau else None,
            "groesse_mb": round(groesse / 1048576, 1),
        })
    except Exception as e:                           # noqa: BLE001
        raus["fehler"] = type(e).__name__
    if p is not None:
        if p.fehler:
            raus["fehler"] = p.fehler.split(":")[0]
            raus["fehler_seit_s"] = round(time.time() - p.fehler_seit)
        if p.beiseite_n:
            raus["beiseite"] = p.beiseite_n
        if p.heilung.get("ok") is not None:
            raus["heilung"] = {"datum": p.heilung["datum"], "ok": p.heilung["ok"]}
    return raus
