"""Offline-Probe fuer den Dokumenten-Teil — ohne Postfach, ohne Pi, ohne DocuSort.

Geprueft wird das, was man NICHT am lebenden System sehen kann, ohne zu warten:
der Bauplan-Leser (BODYSTRUCTURE), das Kriterium „Dokument oder Kram“, die
Suche und die Regeln der Uebergabe.

🔴 Die Beispiele sind echte IMAP-Antwortformen, keine erfundenen: mit Literal
mitten im Satz, mit Klammer im Dateinamen, mit =?UTF-8?Q?…?= und mit
filename*0*. Genau daran scheitert ein naiv gebauter Leser — und zwar still.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import postwache as W                                   # noqa: E402

TMP = tempfile.mkdtemp(prefix="postwache-probe-")
W.STATE = os.path.join(TMP, "state")
W.OUT = os.path.join(TMP, "out")
os.makedirs(W.STATE, exist_ok=True)
os.makedirs(W.OUT, exist_ok=True)

F = []


def pruefe(name, ist, soll):
    F.append((name, ist, soll))
    print(("  OK   " if ist == soll else "  FEHL ") + name
          + ("" if ist == soll else "   ist=%r soll=%r" % (ist, soll)))


def anhaenge(antwort):
    """So, wie imaplib es liefert -> Liste der Anhaenge der ersten Mail."""
    s = W._strukturen_lesen(antwort)
    uid = sorted(s)[0]
    return W.anhaenge_der_mail(s[uid])


print("\n── Der Bauplan-Leser ───────────────────────────────────────────────")

# 1) Eine gewoehnliche Textmail hat keinen Anhang.
EINFACH = [b'1 (UID 101 BODYSTRUCTURE ("TEXT" "PLAIN" ("CHARSET" "utf-8")'
           b' NIL NIL "7BIT" 231 5))']
pruefe("Textmail: kein Anhang", anhaenge(EINFACH), [])

# 2) Rechnung als PDF, der Normalfall.
RECHNUNG = [b'2 (UID 102 BODYSTRUCTURE (("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL'
            b' NIL "QUOTED-PRINTABLE" 842 21)("APPLICATION" "PDF" ("NAME"'
            b' "Rechnung_092026.pdf") NIL NIL "BASE64" 154320 NIL'
            b' ("ATTACHMENT" ("FILENAME" "Rechnung_092026.pdf")) NIL NIL)'
            b' "MIXED" ("BOUNDARY" "----=_Part_1") NIL NIL))']
a = anhaenge(RECHNUNG)
pruefe("PDF gefunden", [x["n"] for x in a], ["Rechnung_092026.pdf"])
pruefe("PDF ist ein Dokument", a[0]["art"], "dokument")
pruefe("Teilenummer stimmt", a[0]["t"], "2")
pruefe("Groesse mitgelesen", a[0]["b"], 154320)
pruefe("Kodierung mitgelesen", a[0]["k"], "BASE64")

# 3) Text+HTML+Anhang: der Anhang ist Teil 2, nicht Teil 1.2.
GESCHACHTELT = [b'3 (UID 103 BODYSTRUCTURE ((("TEXT" "PLAIN" ("CHARSET" "utf-8")'
                b' NIL NIL "7BIT" 10 1)("TEXT" "HTML" ("CHARSET" "utf-8") NIL NIL'
                b' "7BIT" 40 2) "ALTERNATIVE" ("BOUNDARY" "a") NIL NIL)'
                b'("APPLICATION" "OCTET-STREAM" ("NAME" "Vertrag.docx") NIL NIL'
                b' "BASE64" 20000 NIL ("ATTACHMENT" ("FILENAME" "Vertrag.docx"))'
                b' NIL NIL) "MIXED" ("BOUNDARY" "b") NIL NIL))']
a = anhaenge(GESCHACHTELT)
pruefe("Anhang neben alternative", [(x["n"], x["t"]) for x in a],
       [("Vertrag.docx", "2")])
pruefe("docx ist ein Dokument", a[0]["art"], "dokument")
pruefe("docx kann DocuSort NICHT", W.ds_verdaulich("Vertrag.docx"), False)

# 4) Logo in der Signatur + PDF, das sich als octet-stream ausgibt.
#    🔴 Genau hier entscheidet die ENDUNG, nicht der MIME-Typ.
LOGO = [b'4 (UID 104 BODYSTRUCTURE (("TEXT" "HTML" ("CHARSET" "utf-8") NIL NIL'
        b' "7BIT" 900 9)("IMAGE" "PNG" ("NAME" "logo.png") "<logo>" NIL "BASE64"'
        b' 4322 NIL ("INLINE" ("FILENAME" "logo.png")) NIL NIL)'
        b'("APPLICATION" "OCTET-STREAM" ("NAME" "Rechnung (Kopie).pdf") NIL NIL'
        b' "BASE64" 88000 NIL NIL NIL NIL) "MIXED" ("BOUNDARY" "c") NIL NIL))']
a = anhaenge(LOGO)
pruefe("Klammer im Dateinamen zerreisst nichts",
       [(x["n"], x["art"]) for x in a],
       [("logo.png", "bild"), ("Rechnung (Kopie).pdf", "dokument")])

# 5) Literal mitten im Satz — so liefert imaplib jeden Umlaut-Dateinamen.
LITERAL = [(b'5 (UID 105 BODYSTRUCTURE (("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL'
            b' NIL "7BIT" 10 1)("APPLICATION" "PDF" ("NAME" {29}',
            'Rechnung Brücke (2026).pdf'.encode("utf-8")),
           b') NIL NIL "BASE64" 9000 NIL NIL NIL NIL) "MIXED" ("BOUNDARY" "d")'
           b' NIL NIL))']
pruefe("Literal + Umlaut + Klammer",
       [x["n"] for x in anhaenge(LITERAL)], ["Rechnung Brücke (2026).pdf"])

# 6) RFC 2047 — der Dateiname steht kodiert im Parameter.
KODIERT = [b'6 (UID 106 BODYSTRUCTURE ("APPLICATION" "PDF" ("NAME"'
           b' "=?UTF-8?Q?Telekom=5FRechnung=5FM=C3=A4rz=2Epdf?=") NIL NIL'
           b' "BASE64" 5000 NIL NIL NIL NIL))']
pruefe("=?UTF-8?Q?…?= im Namen",
       [x["n"] for x in anhaenge(KODIERT)], ["Telekom_Rechnung_März.pdf"])

# 7) RFC 2231 — lange Namen kommen in Stuecken und prozentkodiert.
GESTUECKELT = [b'7 (UID 107 BODYSTRUCTURE ("APPLICATION" "PDF" NIL NIL NIL'
               b' "BASE64" 5000 NIL ("ATTACHMENT" ("FILENAME*0*"'
               b' "utf-8\'\'Stromabrechnung%20" "FILENAME*1*" "2026%2Epdf"))'
               b' NIL NIL))']
pruefe("filename*0* zusammengesetzt",
       [x["n"] for x in anhaenge(GESTUECKELT)], ["Stromabrechnung 2026.pdf"])

# 8) Weitergeleitete Mail — die Rechnung haengt INNEN.
WEITER = [b'8 (UID 108 BODYSTRUCTURE (("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL'
          b' NIL "7BIT" 30 2)("MESSAGE" "RFC822" NIL NIL NIL "7BIT" 40000'
          b' ("Mo, 1 Sep 2026" "Ihre Rechnung" NIL NIL NIL NIL NIL NIL NIL NIL)'
          b' (("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "7BIT" 20 1)'
          b'("APPLICATION" "PDF" ("NAME" "innen.pdf") NIL NIL "BASE64" 1000 NIL'
          b' NIL NIL NIL) "MIXED" ("BOUNDARY" "e") NIL NIL) 500 NIL NIL NIL NIL)'
          b' "MIXED" ("BOUNDARY" "f") NIL NIL))']
a = anhaenge(WEITER)
pruefe("Anhang in weitergeleiteter Mail",
       [(x["n"], x["t"]) for x in a], [("innen.pdf", "2.2")])

# 9) Ein ganzer Block auf einmal — so kommt es beim Nachtragen.
BLOCK = EINFACH + RECHNUNG + [b'9 (UID 109 BODYSTRUCTURE ("TEXT" "PLAIN"'
                              b' ("CHARSET" "utf-8") NIL NIL "7BIT" 5 1))']
pruefe("drei Mails in einer Antwort", sorted(W._strukturen_lesen(BLOCK)),
       [101, 102, 109])

print("\n── Das Kriterium ───────────────────────────────────────────────────")
for name, soll in [("rechnung.pdf", "dokument"), ("Kontoauszug.CSV", "dokument"),
                   ("Angebot.docx", "dokument"), ("tabelle.xlsx", "dokument"),
                   ("urlaub.jpg", "bild"), ("logo.PNG", "bild"),
                   ("scan.tiff", "bild"), ("termin.ics", "kram"),
                   ("smime.p7s", "kram"), ("bilder.zip", "kram"),
                   ("visitenkarte.vcf", "kram")]:
    pruefe("%-22s -> %s" % (name, soll),
           W.anhang_art("APPLICATION", "OCTET-STREAM", name), soll)
pruefe("ohne Endung entscheidet der Typ",
       W.anhang_art("APPLICATION", "PDF", "anhang"), "dokument")
pruefe("ohne Endung: Bild bleibt Bild",
       W.anhang_art("IMAGE", "JPEG", "anhang"), "bild")

print("\n── Die Suche ───────────────────────────────────────────────────────")
IDX = {"stand": {}, "eintraege": {}}
W.anhang_eintragen(IDX, {"message_id": "<a@x>", "adresse": "rechnung@telekom.de",
                         "name": "Telekom Deutschland GmbH",
                         "betreff": "Ihre Rechnung September",
                         "datum": "2026-09-03T08:00:00+02:00"},
                   "Shopping.Telekom", 12,
                   [{"n": "Rechnung.pdf", "art": "dokument", "b": 1, "t": "2",
                     "k": "BASE64", "m": "application/pdf"}])
W.anhang_eintragen(IDX, {"message_id": "<b@x>", "adresse": "urlaub@freund.de",
                         "name": "Jan", "betreff": "Bilder von der Tour",
                         "datum": "2026-08-30T20:00:00+02:00"},
                   "INBOX", 13,
                   [{"n": "strand.jpg", "art": "bild", "b": 1, "t": "2",
                     "k": "BASE64", "m": "image/jpeg"}])
W.anhang_eintragen(IDX, {"message_id": "<c@x>", "adresse": "service@telekom.de",
                         "name": "Telekom", "betreff": "Ihr Auftrag",
                         "datum": "2026-07-01T09:00:00+02:00"},
                   "Shopping.Telekom", 14,
                   [{"n": "Auftragsbestaetigung.pdf", "art": "dokument", "b": 1,
                     "t": "2", "k": "BASE64", "m": "application/pdf"}])

pruefe("„telekom pdf“ findet beide Rechnungen",
       W.dokumente_suchen(IDX, "telekom pdf")["gesamt"], 2)
pruefe("„pdf telekom“ findet dasselbe",
       W.dokumente_suchen(IDX, "pdf telekom")["gesamt"], 2)
pruefe("Bilder sind keine Dokumente",
       W.dokumente_suchen(IDX, "jan")["gesamt"], 0)
pruefe("…aber mit „alle“ findet man sie",
       W.dokumente_suchen(IDX, "jan", art="alle")["gesamt"], 1)
pruefe("Zeitraum grenzt ein",
       W.dokumente_suchen(IDX, "telekom", von="2026-08-01")["gesamt"], 1)
pruefe("neueste zuerst",
       W.dokumente_suchen(IDX, "telekom")["treffer"][0]["betreff"],
       "Ihre Rechnung September")
pruefe("Ordner ist mitdurchsuchbar",
       W.dokumente_suchen(IDX, "shopping.telekom")["gesamt"], 2)

print("\n── Die Regeln der Uebergabe ────────────────────────────────────────")


class KeinPostfach:
    """Ein Postfach, das nichts herausgibt — hier wird geprueft, WAS gar nicht
    erst geholt wird."""

    def __init__(self):
        self.geholt = []

    def teil_aus_ordner(self, ordner, uid, nr, kod):
        self.geholt.append((ordner, uid, nr))
        return b"%PDF-1.4 ..."


class KeinDocuSort:
    max_mb = 25.0

    def __init__(self):
        self.hoch = []

    def hochladen(self, name, inhalt):
        self.hoch.append(name)
        return {"stand": "uebergeben", "inbox": "20260925-1-abc.pdf", "text": ""}


def eintrag_mit(dateien):
    return {"ordner": "INBOX", "uid": 1, "dateien": dateien, "ds": []}


pf, ds = KeinPostfach(), KeinDocuSort()
e = eintrag_mit([{"n": "Rechnung.pdf", "art": "dokument", "b": 1000, "t": "2",
                  "k": "BASE64"}])
n = W.ds_uebergeben(ds, pf, e, "INBOX", 1)
pruefe("PDF geht durch", (n, ds.hoch), (1, ["Rechnung.pdf"]))
pruefe("Stand steht am Eintrag", e["ds"][0]["stand"], "uebergeben")
n = W.ds_uebergeben(ds, pf, e, "INBOX", 1)
pruefe("kein zweites Mal", (n, len(ds.hoch)), (0, 1))

pf, ds = KeinPostfach(), KeinDocuSort()
e = eintrag_mit([{"n": "Rechnung.pdf", "art": "dokument", "b": 1000, "t": "2",
                  "k": "BASE64"}])
W.ds_uebergeben(ds, pf, e, "INBOX", 1, phishing="Absender gibt sich als Bank aus")
pruefe("Phishing: nichts geholt, nichts geschickt",
       (pf.geholt, ds.hoch, e["ds"][0]["stand"]), ([], [], "phishing"))

pf, ds = KeinPostfach(), KeinDocuSort()
e = eintrag_mit([{"n": "urlaub.jpg", "art": "bild", "b": 100, "t": "2", "k": "BASE64"},
                 {"n": "termin.ics", "art": "kram", "b": 100, "t": "3", "k": "7BIT"}])
W.ds_uebergeben(ds, pf, e, "INBOX", 1)
pruefe("Foto und Kram bleiben, wo sie sind",
       (pf.geholt, ds.hoch, e["ds"]), ([], [], []))

pf, ds = KeinPostfach(), KeinDocuSort()
e = eintrag_mit([{"n": "gross.pdf", "art": "dokument", "b": 40 * 1024 * 1024,
                  "t": "2", "k": "BASE64"}])
W.ds_uebergeben(ds, pf, e, "INBOX", 1)
pruefe("zu grosses PDF wird NICHT geholt",
       (pf.geholt, e["ds"][0]["stand"]), ([], "zu_gross"))

pf, ds = KeinPostfach(), KeinDocuSort()
e = eintrag_mit([{"n": "Vertrag.docx", "art": "dokument", "b": 1000, "t": "2",
                  "k": "BASE64"}])
W.ds_uebergeben(ds, pf, e, "INBOX", 1)
pruefe("docx: gesagt statt verschluckt",
       (ds.hoch, e["ds"][0]["stand"]), ([], "kann_docusort_nicht"))

print("\n── Nicht zweimal dasselbe ──────────────────────────────────────────")
# Dieselbe Rechnung hängt an zwei Mails: im Themenordner UND im Archiv.
DATEI = {"n": "Rechnung_Telekom.pdf", "art": "dokument", "b": 148231,
         "t": "2", "k": "BASE64", "m": "application/pdf"}
Z = {"stand": {}, "eintraege": {}}
a = W.anhang_eintragen(Z, {"message_id": "<1@x>", "adresse": "rechnung@telekom.de",
                           "name": "Telekom", "betreff": "Ihre Rechnung",
                           "datum": "2026-09-03T08:00:00+02:00"},
                       "Shopping.Telekom", 12, [dict(DATEI)])
a["ds"] = [{"n": DATEI["n"], "stand": "abgelegt", "doc": "741",
            "text": "Rechnungen", "zeit": "2026-09-25T15:31:27"}]
W.anhang_eintragen(Z, {"message_id": "<2@x>", "adresse": "rechnung@telekom.de",
                       "name": "Telekom", "betreff": "Ihre Rechnung (Kopie)",
                       "datum": "2026-09-03T08:05:00+02:00"},
                   "Archiv Gmail", 99, [dict(DATEI)])
# Eine gleichnamige, aber ANDERE Datei — die muss weiter angeboten werden.
W.anhang_eintragen(Z, {"message_id": "<3@x>", "adresse": "rechnung@telekom.de",
                       "name": "Telekom", "betreff": "Rechnung Oktober",
                       "datum": "2026-10-03T08:00:00+02:00"},
                   "Shopping.Telekom", 13, [dict(DATEI, b=151999)])
t = {x["betreff"]: x for x in W.dokumente_suchen(Z, "telekom")["treffer"]}
pruefe("die übergebene Mail wird nicht noch einmal angeboten",
       t["Ihre Rechnung"]["gebbar"], False)
pruefe("dieselbe Datei an einer ANDEREN Mail auch nicht",
       t["Ihre Rechnung (Kopie)"]["gebbar"], False)
pruefe("…und sie sagt, wo sie schon liegt",
       (t["Ihre Rechnung (Kopie)"]["dateien_gezeigt"][0]["zwilling"] or {}).get("doc"),
       "741")
pruefe("gleicher Name, andere Größe = andere Datei",
       t["Rechnung Oktober"]["gebbar"], True)
pruefe("kein falscher Zwilling",
       t["Rechnung Oktober"]["dateien_gezeigt"][0]["zwilling"], None)

# Ein .docx ist ein Dokument, aber DocuSort nimmt es nicht — sagen, nicht anbieten.
W.anhang_eintragen(Z, {"message_id": "<4@x>", "adresse": "chef@firma.de",
                       "name": "Chef", "betreff": "Vertrag",
                       "datum": "2026-09-01T08:00:00+02:00"},
                   "INBOX", 14,
                   [{"n": "Vertrag.docx", "art": "dokument", "b": 2000,
                     "t": "2", "k": "BASE64", "m": "application/octet-stream"}])
v = [x for x in W.dokumente_suchen(Z, "vertrag")["treffer"]][0]
pruefe("docx wird gar nicht erst angeboten", v["gebbar"], False)
pruefe("…und der Grund steht dran",
       v["dateien_gezeigt"][0]["stand"], "kann_docusort_nicht")

# Eine Mail ohne bekannten Ort (gerade verschoben) kann man nicht holen.
W.anhang_eintragen(Z, {"message_id": "<5@x>", "adresse": "amt@stadt.de",
                       "name": "Amt", "betreff": "Bescheid",
                       "datum": "2026-09-02T08:00:00+02:00"},
                   "Shopping.Amt", 0,
                   [{"n": "Bescheid.pdf", "art": "dokument", "b": 5000,
                     "t": "2", "k": "BASE64", "m": "application/pdf"}])
b = W.dokumente_suchen(Z, "bescheid")["treffer"][0]
pruefe("umgezogene Mail wird nicht angeboten", b["gebbar"], False)

print("\n── Abgehakt wird erst, wenn DocuSort es sagt ───────────────────────")


class AntwortendesDocuSort:
    def __init__(self, antworten):
        self.antworten = list(antworten)
        self.gefragt = 0

    def stand(self, inbox):
        self.gefragt += 1
        return self.antworten.pop(0) if self.antworten else {}


def idx_mit(stand="uebergeben", **extra):
    e = {"ordner": "INBOX", "uid": 1,
         "dateien": [{"n": "R.pdf", "art": "dokument", "b": 1, "t": "2", "k": "BASE64"}],
         "ds": [dict({"n": "R.pdf", "stand": stand, "inbox": "x.pdf", "doc": "",
                      "text": "", "zeit": "2026-09-25T15:00:00"}, **extra)]}
    return {"stand": {}, "eintraege": {"a": e}}, e


i, e = idx_mit()
W.ds_stand_nachtragen(AntwortendesDocuSort([{"status": "processing"}]), i)
pruefe("während DocuSort arbeitet, bleibt es „unterwegs“", e["ds"][0]["stand"], "uebergeben")

i, e = idx_mit()
W.ds_stand_nachtragen(AntwortendesDocuSort(
    [{"status": "filed", "doc_id": 742, "category": "Rechnungen"}]), i)
pruefe("erst DocuSorts Zusage hakt ab",
       (e["ds"][0]["stand"], e["ds"][0]["doc"], e["ds"][0]["text"]),
       ("abgelegt", "742", "Rechnungen"))

i, e = idx_mit()
W.ds_stand_nachtragen(AntwortendesDocuSort([{"status": "unknown"}]), i)
pruefe("„kenne ich nicht“ ist direkt nach dem Hochladen normal",
       (e["ds"][0]["stand"], bool(e["ds"][0].get("unbekannt_seit"))),
       ("uebergeben", True))

alt_zeit = (datetime.now() - timedelta(seconds=W.DS_VERSCHOLLEN_S + 60)) \
    .isoformat(timespec="seconds")
i, e = idx_mit(unbekannt_seit=alt_zeit)
W.ds_stand_nachtragen(AntwortendesDocuSort([{"status": "unknown"}]), i)
pruefe("…aber nicht ewig: dann gilt sie als verschollen",
       e["ds"][0]["stand"], "verschollen")
pruefe("und verschollen darf man wiederholen", W.ds_offen(e["ds"][0]), True)
pruefe("abgelegt dagegen nicht", W.ds_offen({"stand": "abgelegt"}), False)
# 🔴 „Fehler" ist zweierlei — die Dokumentnummer unterscheidet sie.
pruefe("gescheiterte ÜBERGABE: noch einmal",
       W.ds_offen({"stand": "fehler", "doc": ""}), True)
pruefe("gescheiterte VERARBEITUNG in DocuSort: dort wiederholen, nicht hier",
       W.ds_offen({"stand": "fehler", "doc": "749"}), False)
i, e = idx_mit(stand="fehler", doc="749")
pruefe("…und sie gilt als Beleg für die gleiche Datei anderswo",
       bool(W.ds_zwillinge(i)), True)

print("\n── Entpacken ───────────────────────────────────────────────────────")
pruefe("base64", W.teil_entpacken(b"SGFsbG8gV2VsdA==", "BASE64"), b"Hallo Welt")
pruefe("quoted-printable", W.teil_entpacken(b"Gr=C3=BC=C3=9Fe", "QUOTED-PRINTABLE"),
       "Grüße".encode("utf-8"))
pruefe("7bit bleibt, wie es ist", W.teil_entpacken(b"roh", "7BIT"), b"roh")

schlecht = [n for n, i, s in F if i != s]
print("\nGESAMT: %d Proben, %d Fehlschlaege" % (len(F), len(schlecht)))
for n in schlecht:
    print("   FEHL:", n)
sys.exit(1 if schlecht else 0)
