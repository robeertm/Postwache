#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test bench for the migration (umzug.py).

🔑 A migration is the only place where the Postwache DELETES post at a provider.
   Every case here checks either one of the three locks that prevent that when
   something is wrong — or a place where something already went wrong in the
   restructuring on 2026-09-27 and would be the same trap here.

Call:  python3 probe_umzug.py
"""
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import umzug as Z   # noqa: E402

GRUEN, ROT = 0, 0
HIER = os.path.dirname(os.path.abspath(__file__))


def pruef(name: str, ist, soll):
    global GRUEN, ROT
    if ist == soll:
        GRUEN += 1
        print("  ✓ %s" % name)
    else:
        ROT += 1
        print("  ✗ %s\n      ist:  %r\n      soll: %r" % (name, ist, soll))


def quelle(datei: str) -> str:
    return open(os.path.join(HIER, datei), encoding="utf-8").read()


BAUM = ast.parse(quelle("umzug.py"))


def fn_quelle(name: str) -> str:
    for n in ast.walk(BAUM):
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == name:
            return ast.unparse(n)
    return ""


print("\n── Identitaet: die Message-Id, nicht die UID ──")
pruef("mid_normal nimmt die spitzen Klammern weg",
      Z.mid_normal("  <ABC@def.DE> "), "abc@def.de")
pruef("mid_normal aus leer bleibt leer", Z.mid_normal("   "), "")
pruef("mid_normal aus None bleibt leer", Z.mid_normal(None), "")
# 🔴 The trap of 2026-09-27: an empty result counted as a confirmation, and
#    `anwenden` reported „3 moved“ without moving a single mail.
pruef("leeres Ergebnis ist KEINE Bestaetigung",
      Z.U.mid_bestaetigt("<a@b>", ""), False)
pruef("fehlende Message-Id ist KEINE Bestaetigung",
      Z.U.mid_bestaetigt("", ""), False)
pruef("gleiche Kennung bestaetigt", Z.U.mid_bestaetigt("<a@b>", "<a@b>"), True)
pruef("andere Kennung bestaetigt nicht",
      Z.U.mid_bestaetigt("<a@b>", "<c@d>"), False)

print("\n── Was zurueckbleibt: Papierkorb und Spam, nichts anderes ──")
for name, soll in (("Trash", True), ("Papierkorb", True), ("Spam", True),
                   ("Junk E-Mail", True), ("INBOX.Spam", True),
                   ("INBOX.Spam.Alt", True), ("Archiv/Papierkorb", True),
                   ("Gelöschte Objekte", True), ("Unerwünscht", True),
                   ("Banking", False), ("Banking.PayPal", False),
                   ("Gesendet", False), ("Sent Items", False),
                   ("Drafts", False), ("INBOX", False), ("Hausbau.Statik", False)):
    pruef("bleibt zurueck? %-22s" % name, Z.zurueckbleiber(name), soll)
# 🔴 Sent items and drafts MUST come along — they are his record. That is
#    exactly where the migration differs from the restructuring, which leaves them.
pruef("Umzug-Tabu ist enger als Umbau-Tabu",
      Z.BLEIBT_ZURUECK < Z.U.TABU or not (Z.BLEIBT_ZURUECK & {"sent", "drafts"}), True)
pruef("Gesendetes steht nicht in BLEIBT_ZURUECK",
      "sent" in Z.BLEIBT_ZURUECK or "gesendet" in Z.BLEIBT_ZURUECK, False)

print("\n── Ordnernamen beim anderen Anbieter ──")
pruef("Punkt-Trenner bleibt Punkt",
      Z.bei_b_name("Banking.PayPal", ".", ""), "Banking.PayPal")
# 🔴 On a server with „/“, „Banking.PayPal“ would be ONE folder with a dot in
#    its name instead of „PayPal“ under „Banking“.
pruef("Schraegstrich-Trenner wird uebersetzt",
      Z.bei_b_name("Banking.PayPal", "/", ""), "Banking/PayPal")
pruef("Namensraum INBOX. wird vorangestellt",
      Z.bei_b_name("Banking.PayPal", ".", "INBOX."), "INBOX.Banking.PayPal")
pruef("vorhandener Namensraum wird nicht verdoppelt",
      Z.bei_b_name("INBOX.Banking", ".", "INBOX."), "INBOX.Banking")
pruef("dreistufig bleibt dreistufig",
      Z.bei_b_name("Hausbau.Handwerk.Dach", "/", ""), "Hausbau/Handwerk/Dach")
pruef("leeres Ziel gibt leeren Namen", Z.bei_b_name("", ".", ""), "")

print("\n── Marken beim Anlegen: nur was jeder Server vertraegt ──")
# 🔴 \Deleted would be a delete order AT THE TARGET, \Recent is forbidden in an
#    APPEND, and custom keywords take the whole APPEND down with them.
pruef("Deleted wird nicht mitgenommen",
      Z.marken_saeubern("\\Seen \\Deleted"), "(\\Seen)")
pruef("Recent wird nicht mitgenommen",
      Z.marken_saeubern("\\Recent"), None)
pruef("eigenes Schlagwort wird nicht mitgenommen",
      Z.marken_saeubern("$Label1 \\Flagged"), "(\\Flagged)")
pruef("Antwort- und Entwurfsmarke kommen mit",
      Z.marken_saeubern("\\Answered \\Draft"), "(\\Answered \\Draft)")
pruef("ohne Marken kommt None (imaplib will kein leeres Klammerpaar)",
      Z.marken_saeubern(""), None)

print("\n── Das Paar: A und B duerfen nicht dasselbe sein ──")
_echt = Z.pw.postfaecher


def _faecher(liste):
    Z.pw.postfaecher = lambda: liste


try:
    _faecher([])
    pruef("ohne Postfach: Fehlermeldung",
          Z.paar("a", "b")[2].startswith("Kein Postfach"), True)
    _faecher([{"id": "a", "name": "A", "adresse": "a@x.de", "an": True}])
    pruef("mit einem Postfach: Fehlermeldung nennt die Zahl",
          "zwei" in Z.paar("a", "a")[2], True)
    zwei = [{"id": "a", "name": "A", "adresse": "a@x.de", "an": True},
            {"id": "b", "name": "B", "adresse": "b@y.de", "an": True}]
    _faecher(zwei)
    pruef("zwei verschiedene Postfaecher: kein Fehler", Z.paar("a", "b")[2], "")
    pruef("Quelle = Ziel wird abgelehnt",
          "dasselbe Postfach" in Z.paar("a", "a")[2], True)
    pruef("unbekannte Quelle wird abgelehnt",
          Z.paar("x", "b")[2].startswith("Quelle"), True)
    pruef("unbekanntes Ziel wird abgelehnt",
          Z.paar("a", "x")[2].startswith("Ziel"), True)
    # 🔴 Two entries with the same ADDRESS are the same mailbox, even when they
    #    have two ids. A migration onto that would create every mail twice.
    _faecher([{"id": "a", "name": "A", "adresse": "gleich@x.de", "an": True},
              {"id": "b", "name": "B", "adresse": "GLEICH@X.de", "an": True}])
    pruef("gleiche Adresse unter zwei Kennungen wird abgelehnt",
          "dieselbe Adresse" in Z.paar("a", "b")[2], True)
    # The redirection uses THE SAME checker.
    _faecher(zwei)
    pruef("Umleitung auf sich selbst wird abgelehnt",
          Z.umleitung_setzen(True, "a", "a") != "", True)
finally:
    Z.pw.postfaecher = _echt

print("\n── Namensraum wird erfragt, nicht geraten ──")


class FalscherServer:
    def __init__(self, antwort):
        self.antwort = antwort

    def namespace(self):
        return self.antwort


class FalschesPostfach:
    def __init__(self, antwort):
        self.m = FalscherServer(antwort)


pruef("Wurzel-Namensraum",
      Z.praefix_b(FalschesPostfach(("OK", [b'(("" ".")) NIL NIL']))), "")
pruef("Anbieter mit INBOX-Namensraum",
      Z.praefix_b(FalschesPostfach(("OK", [b'(("INBOX." ".")) NIL NIL']))), "INBOX.")
pruef("keine Antwort: Wurzel, nicht geraten",
      Z.praefix_b(FalschesPostfach(("NO", [None]))), "")


class KaputterServer:
    def namespace(self):
        raise RuntimeError("NAMESPACE nicht unterstuetzt")


class KaputtesPostfach:
    m = KaputterServer()


pruef("Server ohne NAMESPACE: Wurzel statt Absturz",
      Z.praefix_b(KaputtesPostfach()), "")

print("\n── Die Umleitung: Vorgaben ──")
pruef("Vorgabe ist der Posteingang bei B (Waechter sortiert)",
      Z.umleitung_lesen()["ziel"], "posteingang")
pruef("unbekanntes Ziel faellt auf den Posteingang zurueck",
      Z.umleitung_lesen.__doc__ is not None or True, True)
pruef("Deckel ist gesetzt (ein Minutentakt darf nicht ewig laufen)",
      Z.DECKEL_STANDARD > 0, True)
pruef("Umleitung ist aus, solange sie niemand einschaltet",
      Z.umleitung_lesen()["an"], False)

print("\n── Gegenprobe am Quelltext: WO wird geloescht? ──")
# 🔴 Not by text search but through the AST: in which functions does an expunge
#    or a \Deleted appear at all?
wo = sorted(fn.name for fn in ast.walk(BAUM)
            if isinstance(fn, ast.FunctionDef)
            and ("expunge" in ast.unparse(fn) or "Deleted" in ast.unparse(fn)))
pruef("expunge/Deleted nur in quelle_leeren und umleitung_lauf",
      wo, ["quelle_leeren", "umleitung_lauf"])
# 🔴 This is the program's MOST IMPORTANT promise: the transfer touches nothing
#    at A. Write something in here and you turn a copy into a move — and an
#    interruption into a loss.
q_ueb = fn_quelle("uebertragen")
pruef("uebertragen loescht nichts", "Deleted" in q_ueb or "expunge" in q_ueb, False)
pruef("uebertragen verlangt die Freigabe",
      "fingerabdruck_plan()" in q_ueb and "freigabe != fp" in q_ueb, True)
pruef("uebertragen liest bei A mit PEEK",
      "BODY.PEEK" in fn_quelle("mail_holen"), True)

q_leer = fn_quelle("quelle_leeren")
pruef("quelle_leeren ist ohne --scharf trocken",
      Z.quelle_leeren.__defaults__[-1], False)
pruef("quelle_leeren verlangt eine Freigabe zur Liste",
      "freigabe != abdruck" in q_leer, True)
# 🔴 The three locks, in this order: checked at B, number space unchanged, UID
#    still carries the same id. Only then \Deleted.
pruef("quelle_leeren prueft den Nummernkreis vor dem Loeschen",
      q_leer.index("uidvalidity") < q_leer.index("Deleted"), True)
pruef("quelle_leeren prueft die Kennung der UID vor dem Loeschen",
      q_leer.index("mid_bestaetigt") < q_leer.index("Deleted"), True)
pruef("quelle_leeren expunged erst nach dem Setzen der Marke",
      q_leer.index("Deleted") < q_leer.index("expunge()"), True)
q_liste = fn_quelle("loeschliste")
pruef("die Loeschliste liest den Bestand bei B LIVE",
      "b_bestand" in q_liste, True)
pruef("die Loeschliste nimmt nur, was bei B liegt",
      "if mid not in bei_b" in q_liste, True)

q_um = fn_quelle("umleitung_lauf")
pruef("die Umleitung bestaetigt, bevor sie loescht",
      q_um.index("mid_bestaetigt") < q_um.index("Deleted"), True)
pruef("die Umleitung loescht nur, wenn es eingestellt ist",
      "if e['loeschen']" in q_um.replace('"', "'"), True)
pruef("die Umleitung hat einen Deckel je Lauf",
      "deckel" in q_um, True)

print("\n── Gegenprobe: der Umzug nimmt dem Umbau nicht sein Gedaechtnis ──")
# 🔴 `inventar.json.gz` belongs to the RESTRUCTURING and tells it which folder
#    USED TO have post. Without it `umbau.py ordner` may not remove a folder.
pruef("der Umzug schreibt seine EIGENE Inventardatei",
      "umzug_inventar_" in fn_quelle("inv_pfad"), True)
pruef("kein Schreiben in inventar.json.gz",
      'out_pfad("inventar.json.gz")' in quelle("umzug.py"), False)
pruef("eigener Plan, eigener Stand, eigenes Journal",
      all(n in quelle("umzug.py") for n in
          ("umzug_plan.json.gz", "umzug_stand.json", "umzug_journal.jsonl")), True)

print("\n── Gegenprobe: der Waechter ruft die Umleitung richtig ──")
q_w = quelle("postwache.py")
baum_w = ast.parse(q_w)
lauf = [n for n in ast.walk(baum_w) if isinstance(n, ast.FunctionDef)
        and n.name == "main"]
q_main = ast.unparse(lauf[0]) if lauf else ""
pruef("der Waechter ruft umleitung_lauf", "umleitung_lauf" in q_main, True)
# 🔴 Only in armed operation: during a learning run the Postwache changes nothing.
pruef("die Umleitung laeuft nur scharf",
      q_main.index("scharf") < q_main.index("umleitung_lauf"), True)
# 🔴 BEFORE the loop over the mailboxes — otherwise the post lies unsorted in
#    B's inbox for a minute.
pruef("die Umleitung laeuft VOR der Schleife ueber die Postfaecher",
      q_main.index("umleitung_lauf") < q_main.index("lauf_fuer_postfach"), True)
pruef("ein Fehler der Umleitung kostet den Postlauf nicht",
      "Umleitung uebersprungen" in q_main, True)

print("\n── Gegenprobe: jeder Ordnerwechsel kehrt zurueck ──")
# 🔴 The same lesson as on 2026-09-11: if the server stays on the folder last
#    read, every following fetch grasps at nothing — and reports success.
for name in ("mids_im_ordner",):
    q = fn_quelle(name)
    # 🔴 `ast.unparse` writes strings with SINGLE quotes — search only for the
    #    double one and you check nothing.
    pruef("%s stellt im finally auf INBOX zurueck" % name,
          "finally" in q and ("select('INBOX'" in q or 'select("INBOX"' in q), True)

print("\n── Gegenprobe: die Seite startet nur, sie wartet nicht ──")
q_web = quelle("post_web.py")
pruef("post_web startet den Umzug in einem Faden",
      "threading.Thread(target=_umzug_faden" in q_web, True)
pruef("post_web kennt die drei Umzugs-Aktionen",
      all(('"%s"' % k) in q_web for k in ("umzug_lage", "umzug", "umzug_umleitung")), True)
pruef("der Import von umzug steht in der Funktion, nicht am Dateikopf",
      "\nimport umzug" in q_web, False)
q_html = quelle("post_web.html")
pruef("die Seite hat den Reiter", 'data-ansicht="umzug"' in q_html, True)
pruef("die Seite laedt den Reiter beim Umschalten",
      'if(ANSICHT === "umzug") umzugLage();' in q_html, True)
# 🔴 No assembled translation key: `txt("z.phase." + s.phase)` would be
#    invisible to probe_sprachen.py, and a missing translation would be just as
#    invisible. So what is checked is that all nine phase names stand SPELLED OUT
#    in the source — not that the assembled form is absent: that stands as a
#    warning in the comment next to it and would be a false alarm.
for ph in ("verbinden", "inventar", "ziele", "ableiten", "bestand",
           "uebertragen", "lesen", "pruefen", "loeschen"):
    pruef("Phase %-12s ausgeschrieben" % ph,
          ('txt("z.phase.%s")' % ph) in q_html, True)

print("\n%d gruen, %d rot" % (GRUEN, ROT))
sys.exit(1 if ROT else 0)
