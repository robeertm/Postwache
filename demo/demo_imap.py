#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Ein winziger IMAP-Server mit ERFUNDENER Post — damit der Klient zeigbar ist.

    python3 demo/demo_imap.py [--port 8143] [--sprache de|en]

🔑 WARUM ES DAS GIBT (05.10.2026)
`demo_daten.py` baut den Zustand des WAECHTERS: Statistik, gelernte Ablage,
Chronik. Der KLIENT holt seine Mails dagegen ueber IMAP, und die Demo zeigt
bewusst auf einen erfundenen Server (`imap.examplehouse.example`). Die
Klient-Bilder im README konnten darum nur aus einem ECHTEN Postfach stammen —
fremde Post, die niemand veroeffentlichen darf und die sich nicht nachstellen
laesst, wenn sich die Oberflaeche aendert.

🔑 Der Korpus kommt aus `welten.py`, also aus DERSELBEN Quelle wie die
Waechter-Demo. Zwei Korpora waeren zwei Haushalte, und ein Absender, der in der
Liste anders heisst als in der Mail, faellt genau im Bild auf.

🔴 Dies ist KEIN vollstaendiger IMAP-Server und soll keiner werden. Er kann
genau das, was `klient.py` benutzt — gemessen, nicht geraten:

    CAPABILITY LOGIN LIST SELECT EXAMINE STATUS CREATE
    SEARCH / UID SEARCH / UID FETCH / UID STORE / UID COPY
    EXPUNGE NOOP CLOSE LOGOUT

   SORT, MOVE und IDLE werden NICHT angeboten. Das ist Absicht: der Klient
   fragt `kann("SORT")`, `kann("MOVE")`, `kann("IDLE")` und faellt sauber auf
   UID-Reihenfolge, COPY+STORE und Nachfragen alle 20 s zurueck. Drei Befehle
   weniger zu bauen ist drei Fehlerquellen weniger.

🔴 Nichts davon geht ins Netz: der Server hoert nur auf 127.0.0.1.
"""
from __future__ import annotations

import argparse
import email.utils
import os
import random
import re
import socketserver
import ssl
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from welten import WELTEN                                          # noqa: E402

# 🔑 JEDE Anmeldung wird angenommen. Sonst muesste in `postfaecher.json` als
#    Adresse „demo" stehen — und genau diese Zeile steht im README-Bild. Ein
#    Demo-Server, der nur auf 127.0.0.1 hoert und erfundene Post ausliefert,
#    hat nichts zu schuetzen.
ZUGANG = None
POSTEINGANG = "INBOX"


# ── Die Post ───────────────────────────────────────────────────────────────
def _crlf(s: str) -> bytes:
    """Zeilenenden EINMAL auf CRLF bringen.

    🔴 Mein erster Wurf setzte die Teile mit "\\r\\n" zusammen UND ersetzte
       danach jedes "\\n" — heraus kam "\\r\\r\\n". imaplib lieferte das
       klaglos aus, Python bekam From und Date noch heraus, aber der Betreff
       blieb leer: im Klienten stand bei JEDER Mail „(no subject)".
       Darum: ueberall "\\n" bauen und genau hier einmal umsetzen.
    """
    return s.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")


def _text(absender_name: str, betreff: str, sprache: str) -> str:
    if sprache == "en":
        return ("Dear Martin,\n\n"
                "this is a message from the Postwache demo. Every name, address\n"
                "and amount in it is invented — it exists so the mail client can\n"
                "be shown without anybody's real post.\n\n"
                "Kind regards\n%s\n" % absender_name)
    return ("Hallo Martin,\n\n"
            "dies ist eine Nachricht aus der Postwache-Demo. Jeder Name, jede\n"
            "Adresse und jeder Betrag darin ist erfunden — sie gibt es, damit\n"
            "der Mailklient ohne fremde Post gezeigt werden kann.\n\n"
            "Viele Gruesse\n%s\n" % absender_name)


def _baue_mail(uid: int, adresse: str, name: str, betreff: str, wann: datetime,
               sprache: str, mit_anhang: bool) -> bytes:
    """Eine Nachricht als rohe Bytes — einfach genug, dass jeder Teilabruf stimmt."""
    kopf = [
        "From: %s <%s>" % (email.utils.quote(name), adresse),
        "To: Martin <post@examplehouse.example>",
        "Subject: %s" % betreff,
        "Date: %s" % email.utils.format_datetime(wann),
        "Message-ID: <demo-%d@examplehouse.example>" % uid,
        "MIME-Version: 1.0",
    ]
    rumpf = _text(name, betreff, sprache)
    if not mit_anhang:
        kopf.append('Content-Type: text/plain; charset="utf-8"')
        return _crlf("\n".join(kopf) + "\n\n" + rumpf)
    grenze = "demo%d" % uid
    kopf.append('Content-Type: multipart/mixed; boundary="%s"' % grenze)
    anhang = ("%%PDF-1.4\n%% erfunden, kein echtes Dokument\n"
              "1 0 obj << /Type /Catalog >> endobj\n%%%%EOF\n")
    teile = [
        "--" + grenze,
        'Content-Type: text/plain; charset="utf-8"',
        "", rumpf,
        "--" + grenze,
        'Content-Type: application/pdf; name="%s.pdf"' % re.sub(r"\W+", "_", betreff)[:40],
        'Content-Disposition: attachment; filename="%s.pdf"' % re.sub(r"\W+", "_", betreff)[:40],
        "", anhang,
        "--" + grenze + "--", "",
    ]
    return _crlf("\n".join(kopf) + "\n\n" + "\n".join(teile))


class Nachricht:
    __slots__ = ("uid", "roh", "flaggen", "datum")

    def __init__(self, uid, roh, flaggen, datum):
        self.uid, self.roh, self.flaggen, self.datum = uid, roh, set(flaggen), datum


def baue_postfach(sprache: str = "de", tage: int = 21) -> dict:
    """Ordner -> Liste von Nachrichten, aus dem Korpus der Waechter-Demo."""
    W = WELTEN[sprache]
    # 🔑 `betreff` ist nach KLASSE geordnet (frist, amt, …), nicht eine flache
    #    Liste — und die Betreffs tragen `{m}` fuer den Monat aus `monate`.
    #    Wer hier `choice(W["betreff"])` schreibt, zieht einen Schluessel.
    BETREFF, MONATE = W["betreff"], W["monate"]
    rng = random.Random(20261005)
    jetzt = datetime.now().astimezone()
    faecher = {POSTEINGANG: []}
    uid = 1000
    for adresse, name, _klasse, ordner, anzahl in W["absender"]:
        ziel = ordner or POSTEINGANG
        faecher.setdefault(ziel, [])
        # Ein Teil bleibt im Eingang — sonst waere die Liste leer, die das
        # README zeigt.
        for i in range(max(2, min(anzahl, 6))):
            uid += 1
            vorlagen = BETREFF.get(_klasse) or next(iter(BETREFF.values()))
            betreff = rng.choice(vorlagen).format(m=rng.choice(MONATE),
                                                  n=rng.randint(1000, 9999))
            wann = jetzt - timedelta(days=rng.uniform(0, tage), hours=rng.uniform(0, 24))
            anhang = rng.random() < 0.35
            roh = _baue_mail(uid, adresse, name, betreff, wann, sprache, anhang)
            flaggen = set()
            if rng.random() < 0.72:
                flaggen.add("\\Seen")
            if rng.random() < 0.08:
                flaggen.add("\\Flagged")
            wohin = POSTEINGANG if (ordner is None or i < 2) else ziel
            faecher.setdefault(wohin, []).append(Nachricht(uid, roh, flaggen, wann))
    for liste in faecher.values():
        liste.sort(key=lambda n: n.uid)
    return faecher


# ── Der Server ─────────────────────────────────────────────────────────────
FAECHER: dict = {}
SCHLOSS = threading.Lock()


def _zitat(s: str) -> str:
    return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"')


def _teil(roh: bytes, was: str) -> bytes:
    """Den angeforderten Teil einer Nachricht herausschneiden.

    🔴 Nur die Formen, die `klient.py` wirklich schickt: "", HEADER,
       HEADER.FIELDS (…), TEXT und die Nummern 1/2. Alles andere kaeme als
       leerer Abschnitt zurueck — und das faellt im Bild auf, statt still zu
       sein.
    """
    was = (was or "").strip().upper()
    kopf, _, rumpf = roh.partition(b"\r\n\r\n")
    if was == "":
        return roh
    if was == "HEADER":
        return kopf + b"\r\n\r\n"
    if was == "TEXT":
        return rumpf
    if was.startswith("HEADER.FIELDS"):
        drin = re.search(r"\(([^)]*)\)", was)
        wunsch = {w.strip().upper() for w in (drin.group(1) if drin else "").split()}
        raus, nimm = [], False
        for zeile in kopf.split(b"\r\n"):
            if zeile[:1] in (b" ", b"\t"):
                if nimm:
                    raus.append(zeile)
                continue
            name = zeile.split(b":", 1)[0].decode("latin-1").upper()
            nimm = name in wunsch
            if nimm:
                raus.append(zeile)
        return b"\r\n".join(raus) + b"\r\n\r\n"
    if was in ("1", "2"):
        grenze = re.search(rb'boundary="([^"]+)"', kopf)
        if not grenze:
            return rumpf if was == "1" else b""
        stuecke = rumpf.split(b"--" + grenze.group(1))
        nutz = [s for s in stuecke[1:] if not s.startswith(b"--")]
        i = int(was) - 1
        if i >= len(nutz):
            return b""
        _, _, inhalt = nutz[i].partition(b"\r\n\r\n")
        return inhalt.rstrip(b"\r\n")
    return b""


def _passt(n: "Nachricht", kriterien: list) -> bool:
    """Die Suchkriterien, die der Klient schickt — und nur die."""
    i = 0
    while i < len(kriterien):
        k = kriterien[i].upper()
        if k in ("ALL", "CHARSET", "UTF-8"):
            i += 1; continue
        if k == "UNSEEN":
            if "\\Seen" in n.flaggen: return False
            i += 1; continue
        if k == "SEEN":
            if "\\Seen" not in n.flaggen: return False
            i += 1; continue
        if k == "FLAGGED":
            if "\\Flagged" not in n.flaggen: return False
            i += 1; continue
        if k == "UNANSWERED":
            if "\\Answered" in n.flaggen: return False
            i += 1; continue
        if k == "OR":
            # 🔑 Genau eine Form kommt vor: OR HEADER X "a" HEADER X "b".
            links = kriterien[i+1:i+4]; rechts = kriterien[i+4:i+7]
            if not (_passt(n, links) or _passt(n, rechts)): return False
            i += 7; continue
        if k == "HEADER" and i + 2 < len(kriterien):
            feld, wert = kriterien[i+1].lower(), kriterien[i+2].lower()
            kopf = n.roh.split(b"\r\n\r\n", 1)[0].decode("utf-8", "replace").lower()
            if not any(z.startswith(feld + ":") and wert in z
                       for z in kopf.split("\r\n")): return False
            i += 3; continue
        if k in ("FROM", "TO", "SUBJECT", "BODY", "TEXT") and i + 1 < len(kriterien):
            wert = kriterien[i+1].lower()
            if wert not in n.roh.decode("utf-8", "replace").lower(): return False
            i += 2; continue
        i += 1
    return True


def _zerlege(rest: str) -> list:
    """Befehlsargumente in Stuecke — Anfuehrungszeichen halten zusammen."""
    return [s for s in re.findall(r'"([^"]*)"|(\S+)', rest) for s in s if s]


class Sitzung(socketserver.StreamRequestHandler):
    timeout = 300

    def schick(self, zeile: str) -> None:
        self.wfile.write(zeile.encode("utf-8") + b"\r\n"); self.wfile.flush()

    def schick_roh(self, kopf: str, nutz: bytes, schwanz: str = ")") -> None:
        self.wfile.write(("%s {%d}\r\n" % (kopf, len(nutz))).encode("utf-8"))
        self.wfile.write(nutz)
        self.wfile.write((schwanz + "\r\n").encode("utf-8"))
        self.wfile.flush()

    # ---- Hilfen -----------------------------------------------------------
    def liste(self):
        return FAECHER.get(self.ordner, [])

    def handle(self) -> None:
        self.ordner = POSTEINGANG
        self.angemeldet = False
        # 🔴 SORT, MOVE und IDLE stehen hier mit Absicht NICHT.
        self.schick("* OK [CAPABILITY IMAP4rev1] Postwache-Demo bereit")
        while True:
            try:
                roh = self.rfile.readline()
            except Exception:
                return
            if not roh:
                return
            try:
                zeile = roh.decode("utf-8", "replace").rstrip("\r\n")
            except Exception:
                continue
            if not zeile.strip():
                continue
            marke, _, rest = zeile.partition(" ")
            befehl, _, arg = rest.partition(" ")
            befehl = befehl.upper()
            try:
                if not self.fuehr_aus(marke, befehl, arg):
                    return
            except Exception as e:                     # nie die Sitzung sprengen
                self.schick("%s NO %s: %s" % (marke, befehl, str(e)[:120]))

    def fuehr_aus(self, marke: str, befehl: str, arg: str) -> bool:
        if befehl == "CAPABILITY":
            self.schick("* CAPABILITY IMAP4rev1")
            self.schick("%s OK CAPABILITY" % marke); return True
        if befehl == "LOGIN":
            self.angemeldet = len(_zerlege(arg)) >= 2
            self.schick("%s %s LOGIN" % (marke, "OK" if self.angemeldet else "NO"))
            return True
        if befehl == "LOGOUT":
            self.schick("* BYE"); self.schick("%s OK LOGOUT" % marke); return False
        if befehl == "NOOP":
            self.schick("%s OK NOOP" % marke); return True
        if not self.angemeldet:
            self.schick("%s NO erst anmelden" % marke); return True

        if befehl == "LIST":
            for name in sorted(FAECHER):
                kennung = r"\HasNoChildren"
                self.schick(r'* LIST (%s) "/" %s' % (kennung, _zitat(name)))
            self.schick("%s OK LIST" % marke); return True

        if befehl in ("SELECT", "EXAMINE"):
            name = (_zerlege(arg) or [POSTEINGANG])[0]
            if name not in FAECHER:
                self.schick("%s NO kein solcher Ordner" % marke); return True
            self.ordner = name
            msgs = self.liste()
            self.schick("* %d EXISTS" % len(msgs))
            self.schick("* 0 RECENT")
            self.schick(r"* FLAGS (\Seen \Flagged \Answered \Deleted \Draft)")
            self.schick("* OK [UIDVALIDITY 1] uids")
            self.schick("* OK [UIDNEXT %d] naechste" % ((msgs[-1].uid + 1) if msgs else 1))
            self.schick("%s OK [%s] %s" % (marke, "READ-ONLY" if befehl == "EXAMINE"
                                           else "READ-WRITE", befehl)); return True

        if befehl == "STATUS":
            teile = _zerlege(arg)
            name = teile[0] if teile else POSTEINGANG
            msgs = FAECHER.get(name, [])
            werte = {"MESSAGES": len(msgs),
                     "UNSEEN": sum(1 for m in msgs if "\\Seen" not in m.flaggen),
                     "UIDNEXT": (msgs[-1].uid + 1) if msgs else 1,
                     "UIDVALIDITY": 1, "RECENT": 0}
            gefragt = re.search(r"\(([^)]*)\)", arg)
            namen = (gefragt.group(1).split() if gefragt else list(werte))
            paare = " ".join("%s %d" % (n.upper(), werte.get(n.upper(), 0)) for n in namen)
            self.schick("* STATUS %s (%s)" % (_zitat(name), paare))
            self.schick("%s OK STATUS" % marke); return True

        if befehl == "CREATE":
            name = (_zerlege(arg) or [""])[0]
            if name:
                FAECHER.setdefault(name, [])
            self.schick("%s OK CREATE" % marke); return True

        if befehl in ("SEARCH", "UID"):
            return self.such_oder_uid(marke, befehl, arg)

        if befehl == "EXPUNGE":
            with SCHLOSS:
                FAECHER[self.ordner] = [m for m in self.liste()
                                        if "\\Deleted" not in m.flaggen]
            self.schick("%s OK EXPUNGE" % marke); return True

        if befehl == "CLOSE":
            self.schick("%s OK CLOSE" % marke); return True

        self.schick("%s BAD unbekannt: %s" % (marke, befehl)); return True

    # ---- SEARCH / UID ----------------------------------------------------
    def such_oder_uid(self, marke: str, befehl: str, arg: str) -> bool:
        if befehl == "SEARCH":
            unter, rest = "SEARCH", arg
        else:
            unter, _, rest = arg.partition(" ")
            unter = unter.upper()

        if unter == "SEARCH":
            kriterien = _zerlege(rest)
            treffer = [m for m in self.liste() if _passt(m, kriterien)]
            nummern = ([m.uid for m in treffer] if befehl == "UID"
                       else [i + 1 for i, m in enumerate(self.liste()) if m in treffer])
            self.schick("* SEARCH " + " ".join(str(x) for x in nummern))
            self.schick("%s OK SEARCH" % marke); return True

        if unter == "FETCH":
            menge, _, posten = rest.partition(" ")
            return self.hol(marke, menge, posten)

        if unter == "STORE":
            menge, _, posten = rest.partition(" ")
            feld, _, werte = posten.partition(" ")
            flaggen = {f.strip() for f in werte.strip("()").split() if f.strip()}
            with SCHLOSS:
                for m in self._gewaehlt(menge):
                    if feld.startswith("+"):
                        m.flaggen |= flaggen
                    elif feld.startswith("-"):
                        m.flaggen -= flaggen
                    else:
                        m.flaggen = set(flaggen)
            self.schick("%s OK STORE" % marke); return True

        if unter == "COPY":
            menge, _, ziel = rest.partition(" ")
            name = (_zerlege(ziel) or [""])[0]
            with SCHLOSS:
                FAECHER.setdefault(name, [])
                hoechste = max([m.uid for liste in FAECHER.values() for m in liste] or [1000])
                for m in self._gewaehlt(menge):
                    hoechste += 1
                    FAECHER[name].append(Nachricht(hoechste, m.roh, m.flaggen, m.datum))
            self.schick("%s OK COPY" % marke); return True

        self.schick("%s BAD UID %s" % (marke, unter)); return True

    def _gewaehlt(self, menge: str) -> list:
        """„1001", „1001,1003", „1001:1005" und „1:*" — mehr schickt der Klient nicht."""
        msgs = self.liste()
        if not msgs:
            return []
        aus = []
        for stueck in menge.split(","):
            if ":" in stueck:
                a, _, b = stueck.partition(":")
                lo = int(a) if a != "*" else msgs[-1].uid
                hi = msgs[-1].uid if b == "*" else int(b)
                lo, hi = min(lo, hi), max(lo, hi)
                aus += [m for m in msgs if lo <= m.uid <= hi]
            elif stueck.strip().isdigit():
                aus += [m for m in msgs if m.uid == int(stueck)]
        return aus

    def hol(self, marke: str, menge: str, posten: str) -> bool:
        gewollt = posten.strip()
        for nr, m in enumerate(self.liste(), start=1):
            if m not in self._gewaehlt(menge):
                continue
            felder = []
            if "UID" in gewollt.upper():
                felder.append("UID %d" % m.uid)
            if "FLAGS" in gewollt.upper():
                felder.append("FLAGS (%s)" % " ".join(sorted(m.flaggen)))
            if "INTERNALDATE" in gewollt.upper():
                felder.append('INTERNALDATE "%s"'
                              % m.datum.strftime("%d-%b-%Y %H:%M:%S %z"))
            if "RFC822.SIZE" in gewollt.upper():
                felder.append("RFC822.SIZE %d" % len(m.roh))
            koerper = re.search(r"BODY(?:\.PEEK)?\[([^\]]*)\](?:<(\d+)\.(\d+)>)?",
                                gewollt, re.I)
            if not koerper:
                self.schick("* %d FETCH (%s)" % (nr, " ".join(felder)))
                continue
            was = koerper.group(1)
            nutz = _teil(m.roh, was)
            if koerper.group(2) is not None:
                ab, laenge = int(koerper.group(2)), int(koerper.group(3))
                nutz = nutz[ab:ab + laenge]
                etikett = "BODY[%s]<%d>" % (was, ab)
            else:
                etikett = "BODY[%s]" % was
            kopf = "* %d FETCH (%s%s" % (nr, (" ".join(felder) + " ") if felder else "",
                                         etikett)
            self.schick_roh(kopf, nutz)
        self.schick("%s OK FETCH" % marke); return True


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    ssl_kontext = None

    def get_request(self):
        verbindung, adresse = self.socket.accept()
        if self.ssl_kontext is not None:
            verbindung = self.ssl_kontext.wrap_socket(verbindung, server_side=True)
        return verbindung, adresse


def zeugnis(ordner: str) -> str:
    """Ein selbstsigniertes Zertifikat fuer `localhost` — einmal, im Demo-Ordner.

    🔴 WARUM UEBERHAUPT TLS: der Klient verbindet mit `imaplib.IMAP4_SSL` und
       OHNE eigenen Kontext. Python nimmt dann `create_default_context()`, und
       der PRUEFT. Ein Klartext-Server wird gar nicht erst angenommen.

    🔑 Und darum wird hier auch NICHTS abgeschaltet. Das Programm behaelt seine
       Pruefung; der Demo-Starter setzt lediglich `SSL_CERT_FILE` auf genau
       dieses Zertifikat. Das ist eine dokumentierte OpenSSL-Weiche, gilt NUR
       fuer den gestarteten Prozess und macht keine andere Verbindung unsicher.
    """
    os.makedirs(ordner, exist_ok=True)
    pem = os.path.join(ordner, "demo-imap.pem")
    if os.path.exists(pem):
        return pem
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
         "-keyout", pem, "-out", pem, "-days", "3650",
         "-subj", "/CN=localhost",
         "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
        check=True, capture_output=True)
    os.chmod(pem, 0o600)
    return pem


def starte(port: int = 8143, sprache: str = "de", ordner: str = None) -> tuple:
    global FAECHER
    FAECHER = baue_postfach(sprache)
    pem = zeugnis(ordner or os.path.join(os.path.expanduser("~"), ".postwache-demo"))
    kontext = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    kontext.load_cert_chain(pem)
    Server.ssl_kontext = kontext
    s = Server(("127.0.0.1", port), Sitzung)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, pem


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--port", type=int, default=8143)
    p.add_argument("--sprache", default="de", choices=("de", "en"))
    p.add_argument("--ordner", default=None,
                   help="wohin das Zertifikat gelegt wird (Vorgabe: ~/.postwache-demo)")
    a = p.parse_args()
    _, pem = starte(a.port, a.sprache, a.ordner)
    gesamt = sum(len(v) for v in FAECHER.values())
    print("Demo-IMAP auf localhost:%d (TLS) — %d Mails in %d Ordnern"
          % (a.port, gesamt, len(FAECHER)))
    print("Alles erfunden. Hoert NUR auf 127.0.0.1.")
    print()
    print("Die Postwache so starten, damit sie GENAU diesem Zertifikat traut:")
    print("  SSL_CERT_FILE=%s \\" % pem)
    print("  POSTWACHE_HOME=<demo-ordner> python3 post_web.py")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
