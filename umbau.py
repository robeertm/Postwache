#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Postwache — Umbau: das ganze Postfach neu ordnen.

Der Besitzer, 27.09.2026: „ich moechte das du die so baust das mein gesamtes postfach
komplett neu strukturiert wird, es soll jede mail analysieren und in einer
sinnvollen postfach/ordnerstrucktur sortiert werden, niemals mails loeschen nur
verschieben innerhalb des postfaches, aktuelle strucktur ist doof … auch
archivierte mails sortieren und aus dem archiv holen."

Das ist die **Phase 2**, die seit 11.09. im Bauplan stand („meine ordner sind
auch nicht perfekt, aber das soll ja der agent spaeter fuer mich neu sortieren").
Phase 1 hat dafuer bereits die Schwaechen der Ablage gemessen.

🔑 DREI STUFEN, GETRENNT UND IN DIESER REIHENFOLGE
   1. `inventar`  — liest JEDE Mail-Kopfzeile. Nur lesend, `BODY.PEEK`.
   2. `plan`      — rechnet aus dem Inventar einen Vorschlag. Beruehrt kein Postfach.
   3. `anwenden`  — verschiebt, aber nur was im freigegebenen Plan steht.

   Wer die Stufen vermischt, verschiebt auf Verdacht. Der Plan ist eine Datei,
   die man lesen kann, BEVOR eine Mail wandert.

🔴 GELOESCHT WIRD NIE. Es gibt in dieser Datei keinen Aufruf, der eine Mail
   entfernt: `bewegen()` kopiert zuerst, hakt erst nach BESTAETIGTER Kopie ab
   und schreibt jede Bewegung ins Journal. Schlaegt die Kopie fehl, bleibt die
   Mail unberuehrt liegen.

🔴 DER SCHLUESSEL IST DIE MESSAGE-ID, NICHT DIE UID. Eine UID gilt nur zusammen
   mit Ordner UND `UIDVALIDITY`. Vor jeder Verschiebung wird die Message-Id der
   UID gegen den Plan geprueft — sonst wandert nach einer Umnummerierung die
   falsche Mail (dieselbe Lehre wie bei den Dokumenten, 2.6.0).
"""

import contextlib
import email
import email.utils
import gzip
import json
import math
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import postwache as pw  # noqa: E402  — bewaehrtes IMAP, dekodieren(), normal()

# ── Was der Umbau NIE anfasst ────────────────────────────────────────────────
# Gesendetes ist dessen eigener Nachweis, Entwuerfe sind unfertig, Papierkorb
# und Spam sind Absicht. Ein Sortierer, der hier hineingreift, zerstoert
# Belege statt Ordnung zu schaffen.
TABU = {
    "sent items", "sent", "gesendet", "gesendete objekte",
    "drafts", "entwuerfe", "entwürfe",
    "trash", "papierkorb", "spam", "junk",
}

KOPFZEILEN = ("FROM TO SUBJECT DATE MESSAGE-ID LIST-ID LIST-UNSUBSCRIBE "
              "X-MAILER REPLY-TO")

# 🔴 Die Fassung des LESERS, nicht der Datei. Das Inventar uebernimmt
#    unveraenderte Ordner aus dem letzten Lauf — der Fingerabdruck aus
#    UIDVALIDITY/MESSAGES/UIDNEXT sagt aber nur, dass sich die POST nicht
#    geaendert hat, nicht dass wir sie gleich LESEN. Am 27.09.2026 wurde die
#    Absender-Zerlegung repariert (11 Booking-Mails hatten keine erkennbare
#    Adresse); ohne diese Zahl haette das Inventar die alten, falschen Werte
#    weiterbenutzt und die Reparatur waere unsichtbar geblieben — ein Lauf, der
#    Erfolg meldet und das Alte behaelt. Wer `kopf_saetze()` aendert, erhoeht sie.
LESER_FASSUNG = 2


def tabu(name: str) -> bool:
    """Tabu gilt fuer den Ordner UND alles darunter."""
    n = name.lower()
    return any(n == t or n.startswith(t + ".") for t in TABU)


def out_pfad(name: str) -> str:
    """Wo der Umbau seine Dateien ablegt.

    🔴 27.09.2026: hier stand ein fester Pfad mit Rueckfall auf den
    Programmordner — `POSTWACHE_HOME` wurde ignoriert. In der Demo blieben damit
    alle neuen Karten leer, obwohl die Dateien geschrieben waren: der Waechter
    schrieb in `$POSTWACHE_HOME/out`, der Umbau las im Programmordner. Zwei
    Stellen, die denselben Ort anders bestimmen, sind eine zu viel — es gilt
    `pw.OUT`, und das kennt die Umgebungsvariable.
    """
    return os.path.join(pw.OUT, name)


# ── Stand fuer die Seite ─────────────────────────────────────────────────────
# 🔑 Ein Lauf ueber 17.600 Mails dauert Minuten. Ohne Stand sitzt der Besitzer vor
#    einer Seite, die nichts sagt — und drueckt noch einmal. Deshalb schreibt
#    jede Stufe mit, wo sie steht, und die Seite liest nur diese Datei.
def stand_schreiben(**felder) -> None:
    d = stand_lesen()
    # 🔑 Beginn merken: ohne Laufzeit sieht jede laengere Stufe aus wie ein
    #    haengender Dienst. Mit „laeuft seit 3:20" ist Warten Warten.
    if felder.get("laeuft") and not d.get("laeuft"):
        felder.setdefault("begonnen", time.time())
    d.update(felder)
    d["zeit"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        with open(out_pfad("umbau_stand.json"), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def stand_lesen() -> dict:
    try:
        return json.load(open(out_pfad("umbau_stand.json"), encoding="utf-8"))
    except Exception:
        return {}


# ── Stufe 1: Inventar ────────────────────────────────────────────────────────
def kopf_saetze(pf, ordner: str, block: int = 300):
    """Alle Kopfzeilen eines Ordners. readonly, BODY.PEEK, in Bloecken.

    🔴 Das `finally` stellt auf INBOX zurueck. Ohne das bleibt der Server auf
    dem zuletzt gelesenen Ordner stehen und jeder folgende Abruf greift ins
    Leere — genau dieser Fehler meldete am 11.09. Erfolg und las nichts.
    """
    raus = []
    uidval = 0
    try:
        typ, _ = pf.m.select(pf._zitat(ordner), readonly=True)
        if typ != "OK":
            return raus, 0
        uidval = pf.uidvalidity(ordner)
        typ, daten = pf.m.uid("search", None, "ALL")
        if typ != "OK" or not daten or not daten[0]:
            return raus, uidval
        uids = daten[0].split()
        for i in range(0, len(uids), block):
            teil = b",".join(uids[i:i + block]).decode()
            typ, antw = pf.m.uid("fetch", teil,
                                 "(BODY.PEEK[HEADER.FIELDS (%s)])" % KOPFZEILEN)
            if typ != "OK":
                continue
            uid_jetzt = 0
            for st in antw:
                if isinstance(st, bytes):
                    t = re.search(rb"UID (\d+)", st)
                    if t:
                        uid_jetzt = int(t.group(1))
                    continue
                if not isinstance(st, tuple) or len(st) < 2:
                    continue
                t = re.search(rb"UID (\d+)", st[0] or b"")
                uid = int(t.group(1)) if t else uid_jetzt
                msg = email.message_from_bytes(st[1])
                # 🔴 Ueber `pw.absender_teile()`: es kennt den Rueckfall fuer
                # kodierte Anzeigenamen (11 Booking-Mails, 27.09.).
                name, adr = pw.absender_teile(msg.get("From", ""))
                ts = ""
                try:
                    d = email.utils.parsedate_to_datetime(msg.get("Date", ""))
                    ts = d.astimezone().isoformat()
                except Exception:
                    pass
                raus.append({
                    "uid": uid,
                    "von": (adr or "").strip().lower(),
                    # `absender_teile()` dekodiert den Namen schon.
                    "name": name,
                    "betreff": pw.dekodieren(msg.get("Subject", "")),
                    "an": pw.dekodieren(msg.get("To", ""))[:200],
                    "datum": ts,
                    "mid": (msg.get("Message-Id", "") or "").strip(),
                    "liste": bool(msg.get("List-Id") or msg.get("List-Unsubscribe")),
                })
    except Exception as e:
        print("   ! %s: %s" % (ordner, str(e)[:120]))
    finally:
        try:
            pf.m.select("INBOX", readonly=True)
        except Exception:
            pass
    return raus, uidval


def ordner_signatur(pf, name: str):
    """(uidvalidity, anzahl, uidnext) in EINER Abfrage.

    🔑 Aendert sich keiner der drei Werte, ist der Ordner unveraeandert: UIDNEXT
    waechst bei jeder neuen Mail und schrumpft nie. Wurde eine geloescht und
    eine neue gelegt, bleibt die ANZAHL gleich — aber UIDNEXT ist hoeher. Die
    drei zusammen sind deshalb ein verlaesslicher Fingerabdruck, und er kostet
    einen einzigen Hin- und Rueckweg statt SELECT + SEARCH + FETCH.
    """
    try:
        typ, d = pf.m.status(pf._zitat(name), "(UIDVALIDITY MESSAGES UIDNEXT)")
        if typ != "OK" or not d or not d[0]:
            return None
        roh = d[0] if isinstance(d[0], bytes) else str(d[0]).encode()
        werte = []
        for feld in (b"UIDVALIDITY", b"MESSAGES", b"UIDNEXT"):
            m = re.search(feld + rb"\s+(\d+)", roh)
            if not m:
                return None            # unvollstaendig = kein Fingerabdruck
            werte.append(int(m.group(1)))
        return tuple(werte)
    except Exception:
        return None


def inventar(nur: str = "", voll: bool = False) -> int:
    zug = pw.zugang()
    t0 = time.time()
    # Was beim letzten Mal drinstand — daraus wird uebernommen, was sich nicht
    # geaendert hat. 🔴 der Besitzer, 27.09.: das Postfach hat nach dem Umbau 132
    # statt 53 Ordner, und ein volles Inventar dauerte dadurch 7 Minuten statt
    # 27 Sekunden. Dieselben Mails, nur feiner verteilt — die Kosten stecken im
    # Ordner, nicht in der Post.
    alt = {}
    if not voll:
        try:
            with gzip.open(out_pfad("inventar.json.gz"), "rt", encoding="utf-8") as f:
                alt = json.load(f).get("ordner") or {}
        except Exception:
            alt = {}
    stand_schreiben(schritt="inventar", laeuft=True, fortschritt=0, gesamt=0,
                    phase="lesen", text="Postfach wird gelesen", fehler="")
    daten = {"gemessen": time.strftime("%Y-%m-%dT%H:%M:%S"), "ordner": {}}
    with verbindung(zug, schreiben=False) as pf:
        ordner = pf.ordner_liste()
        print("%d Ordner, Trenner %r" % (len(ordner), pf.trenner))
        for o in sorted(ordner):
            if nur and nur.lower() not in o.lower():
                continue
            if o.upper() == "INBOX":
                pass  # Posteingang wird mitgezaehlt, aber nie geraeumt
            sig = ordner_signatur(pf, o)
            frueher = alt.get(o) or {}
            uebernommen = (sig is not None and frueher.get("signatur")
                           and list(sig) == list(frueher["signatur"])
                           and int(frueher.get("leser") or 0) == LESER_FASSUNG)
            if uebernommen:
                daten["ordner"][o] = dict(frueher, tabu=tabu(o))
                saetze, uidval = frueher.get("mails") or [], frueher.get("uidvalidity")
            else:
                saetze, uidval = kopf_saetze(pf, o)
                daten["ordner"][o] = {
                    "uidvalidity": uidval,
                    "signatur": list(sig) if sig else None,
                    "leser": LESER_FASSUNG,
                    "tabu": tabu(o),
                    "mails": saetze,
                }
            print("   %-28s %6d  uidvalidity=%s%s%s"
                  % (o, len(saetze), uidval, "  [tabu]" if tabu(o) else "",
                     "  [unveraendert]" if uebernommen else ""))
            stand_schreiben(phase="lesen", fortschritt=len(daten["ordner"]),
                            gesamt=len(ordner), text="gelesen: %s" % o)
    ziel = out_pfad("inventar.json.gz")
    with gzip.open(ziel, "wt", encoding="utf-8") as f:
        json.dump(daten, f, ensure_ascii=False)
    gesamt = sum(len(v["mails"]) for v in daten["ordner"].values())
    stand_schreiben(schritt="inventar", laeuft=False, mails=gesamt,
                    ordner=len(daten["ordner"]),
                    text="%d Mails aus %d Ordnern gelesen" % (gesamt, len(daten["ordner"])))
    print("\n%d Mails aus %d Ordnern in %.1f s  ->  %s (%.1f MB)"
          % (gesamt, len(daten["ordner"]), time.time() - t0, ziel,
             os.path.getsize(ziel) / 1e6))
    return 0


# ── Stufe 2: der Zielbaum ────────────────────────────────────────────────────
# 🔑 Jede Regel steht hier mit der GEMESSENEN Zahl (Inventar 27.09.2026).
#    Erfundene Ordner gibt es nicht — wo keine Post liegt, entsteht kein Ordner.
#
# 🔑 DIE REIHENFOLGE IST DER VORRANG. Die erste passende Regel gewinnt.
#
# 🔴 SEMANTIK, damit sie nicht geraten werden muss:
#    Sind Absender/Domains UND ein Betreffmuster gesetzt, muessen BEIDE passen.
#    Ist nur eins gesetzt, entscheidet das allein. Wer „oder" braucht, schreibt
#    ZWEI Regeln — ein breites Muster wie `konto` mit „oder" verbunden saugt
#    sonst fremde Post ein, die drei Regeln weiter unten ihr Zuhause hat.
#
# 🔴 BEIM ARCHIV ENTSCHEIDET DER BETREFF, NICHT DER ABSENDER. 6.954 Archivmails
#    tragen dessen eigene Adresse im `From`, weil die DiskStation ueber sein
#    Gmail-Konto verschickt hat. Deshalb stehen die Geraeteregeln GANZ VORN.

# ── Absender, die ALLES anbieten ────────────────────────────────────────────
# 🔴 Das stand schon in Phase 1 (11.09.2026) und ich habe es trotzdem gebaut:
#    „Die Hauptdomain bringt 8 Treffer und 7 Fehler — ein Muenzwurf.
#     `check24.de` macht Hotels UND Versicherungen, `deutschepost.de` liefert
#     Pakete UND den Steuer-Newsletter." Die Hauptdomain durfte seither
#     VORSCHLAGEN, aber nicht HANDELN — meine Regel liess sie handeln.
#
# 🔑 Die Information ist laengst da, nur eine Ebene tiefer: bei der Besitzer kam die
#    Reise von `info@hotel.check24.de` („Buchungsbestaetigung Wolin"), die
#    Versicherung von `e-scooter-versicherung@check24.de`, der Sicherheitscode
#    von `kundenkonto@check24.de`. UNTERDOMAIN und der Teil VOR dem @ sagen es.
#
# Je Domain: welche Kategorie fuehrt wohin, und wohin, wenn nichts passt.
MEHRDEUTIG = {
    "check24.de": {"_rueckfall": "Versicherungen.Allgemein",
                   "Reisen": "Reisen",
                   "Versicherungen": "Versicherungen.Allgemein",
                   "Konten": "Konten.Allgemein",
                   "Auto": "Auto.Werkstatt"},
    "deutschepost.de": {"_rueckfall": "Einkauf.Versand",
                        "Einkauf": "Einkauf.Versand",
                        "Steuer": "Steuer",
                        "Konten": "Konten.Allgemein"},
    "verti.de": {"_rueckfall": "Versicherungen.Allgemein",
                 "Versicherungen": "Versicherungen.Allgemein",
                 "Auto": "Auto.Werkstatt"},
}

# Womit der feine Blick entscheidet. Gelesen wird die GANZE Adresse (Teil vor
# dem @, Unterdomain) UND der Betreff — die Reihenfolge ist der Vorrang.
# 🔴 Versicherung VOR Auto: „Kfz-Versicherung" ist beides, gemeint ist das
#    erste. Und Reisen VOR Einkauf: eine „Buchungsbestaetigung" ist keine
#    Bestellung.
FEIN_MUSTER = [
    ("Konten", r"kundenkonto|kundenbereich|\blogin\b|passwort|sicherheitscode|"
               r"anmeldung|zugangsdaten|zwei-faktor"),
    ("Reisen", r"hotel|reise|\bflug|unterkunft|ferienwohnung|urlaub|"
               r"buchungsbestaetigung|eingangsbestaetigung ihrer buchung"),
    # 🔴 Der STAMM, nicht das Wort: „Kfz-Versicherer" enthaelt kein
    #    „Versicherung" — zwei Mails landeten dadurch in `Auto.Werkstatt`.
    ("Versicherungen", r"versicher|\bpolice\b|\bevb\b|haftpflicht|"
                       r"\btarif\b|antragsnummer"),
    ("Auto", r"\bkfz\b|fahrzeug|zulassung|kennzeichen|werkstatt"),
    ("Steuer", r"steuer|elster|finanzamt"),
    ("Einkauf", r"bestellung|lieferung|versand|paket|sendung|rechnung"),
]


def fein_entscheiden(von: str, betreff: str):
    """Bei einem mehrdeutigen Absender entscheidet nicht die Domain.

    Gelesen wird die GANZE Adresse plus Betreff. Passt nichts, gilt der
    Rueckfall der Domain — raten wird hier nicht.
    """
    von = (von or "").lower()
    dom = von.split("@")[-1]
    karte = None
    for d, k in MEHRDEUTIG.items():
        if dom == d or dom.endswith("." + d):
            karte = k
            break
    if karte is None:
        return "", ""
    # Der Teil vor dem @ und die Unterdomain gehoeren zur Beweislage.
    heu = pw.normal(von.replace("@", " ").replace(".", " ") + " " + (betreff or ""))
    for kat, rx in FEIN_MUSTER:
        if kat in karte and _passt_rx(rx, heu):
            return karte[kat], "mehrdeutiger Absender — Adresse und Betreff entscheiden"
    return karte.get("_rueckfall", ""), "mehrdeutiger Absender — Rueckfall"


# 🔴 LEER im Quelltext. Die eigenen Adressen des Besitzers stehen in
#    `state/regeln_eigen.json` unter „eigene_adressen" — hier standen sie bis
#    zum 27.09.2026, und damit in jedem Abbild und jedem Repo.
EIGEN = ()

# 🔴 dessen EIGENE alte Post (863 Mails 2007-2022, an andere gerichtet) gehoert
#    nicht in Themenordner verteilt — sie ist ein Block und bleibt einer. Sie
#    wird ABSICHTLICH nicht nach `Sent Items` gelegt: das ist sein aktueller
#    Nachweis und tabu.
EIGENE_POST = "Eigene Post Archiv"

# Rundschreiben, die keine Regel getroffen hat. Erkannt am `List-Id` /
# `List-Unsubscribe` — dasselbe Merkmal, an dem `einordnen()` seit 1.0
# Newsletter erkennt. Ohne diese Stufe liegen 600 Einzelstuecke im Auffang,
# die alle dasselbe sind.
NEWSLETTER = "Newsletter"

# Was keine Regel trifft. Der Besitzer soll SEHEN, was nicht erkannt wurde — ein
# stiller Rest ist schlimmer als ein sichtbarer.
AUFFANG = "Unsortiert"

# (Ziel, Absender exakt, Domain-Endungen, Betreffmuster)
REGELN = [
    # 🔴 HIER STAND DER BESITZER PERSOENLICHER TEIL — 20 Regeln mit den echten
    #    Adressen von Familie, Bankberaterin, Architekt, Handwerkern und
    #    Arbeitgeber. Am 27.09.2026 standen genau solche Daten im OEFFENTLICHEN
    #    Repo (Name und Mailadresse einer echten Person, in allen drei Commits).
    #
    # 🔑 Sie liegen jetzt in `state/regeln_eigen.json` (0600, in KEINER
    #    Ausrollliste) und werden von `eigen_laden()` VOR diesen Regeln
    #    gefragt. Was niemand veroeffentlichen kann, muss niemand herausfiltern.
    #    Aufbau der Datei steht bei `eigen_laden()`.
    #
    #    Was hier steht, gilt fuer JEDEN: PayPal, Amazon, Telekom, Behoerden.
    #    Wer eine Regel mit einem echten Namen hier eintraegt, macht denselben
    #    Fehler noch einmal.

    # ── Geraetemeldungen: der Schluessel zum Archiv (~6.900 Mails) ───────────
    ("Technik.Synology", (), ("synology.com", "synologynotification.com"), r""),
    ("Technik.Netzwerk", (), (), r"fritz!?|powerline|heimnetz|portfreigaben|"
     r"wlan-gastzugang|archer c\d|änderungsnotiz|internet-adresse|"
     r"communication (establish|lost)|self test (start|end)"),
    # `sponionpi` ist kein Anbieter, sondern der HOSTNAME eines Pi — solche
    # Absender haben keine Domain, nur einen Rechnernamen.
    ("Technik.Netzwerk", (), ("avm.de", "no-ip.com", "sponionpi"), r""),

    # ── Hausbau: das Bauvorhaben des Besitzers ─────────────
    ("Hausbau.Statik", (), ("heinze-statik.de",), r""),
    # 🔴 Die Beraterin der Sparkasse VOR der Bank-Regel: ihre Post ist Hausbau,
    #    nicht Banking. Deshalb Adresse UND Muster.

    # ── Banking ─────────────────────────────────────────────────────────────
    ("Banking.DKB", (), ("dkb.de",), r""),
    ("Banking.PayPal", (), ("paypal.de", "paypal.com"), r""),
    ("Banking.Krypto", (), ("bitvavo.com", "kraken.com"), r""),

    # ── Versicherung ────────────────────────────────────────────────────────
    ("Versicherungen.PlusCard", (), ("pluscard.de",), r""),
    ("Versicherungen.Allgemein", (), ("check24.de", "verti.de", "valuenet.de"), r""),

    # ── Auto ────────────────────────────────────────────────────────────────
    ("Auto.KIA", (), ("kia.com", "kia.de", "m8mit.de"), r""),

    # ── Konten (Sicherheit/Anmeldung) VOR Einkauf (Belege) ───────────────────
    # 🔴 Apple zweimal: Sicherheitspost ist etwas anderes als ein Kaufbeleg. Die
    #    ENGE Regel muss vorn stehen, sonst frisst sie alle 761 Belege mit.
    ("Konten.Apple", (), ("apple.com", "itunes.com", "icloud.com", "me.com"),
     r"apple[- ]?id|anmeldung|sicherheitscode|passwort|zwei-faktor|verifizierung"),
    ("Konten.Google", (), ("google.com", "googleplay.com", "accounts.google.com"), r""),
    ("Konten.Facebook", (), ("facebookmail.com", "facebook.com", "instagram.com"), r""),

    # ── Technik/Software ────────────────────────────────────────────────────
    ("Technik.Ubiquiti", (), ("ui.com", "ubnt.com", "ubiquiti.com"), r""),
    ("Technik.Software", (), ("microsoft.com", "github.com", "ubuntu.com",
                              "trendmicro.com", "amd.com", "asknet.com",
                              "torproject.org", "hpconnected.com", "hpsmart.com",
                              "nvidia.com", "anthropic.com"), r""),

    # ── Einkauf & Versand ───────────────────────────────────────────────────
    ("Einkauf.Amazon", (), ("amazon.de", "amazon.com"), r""),
    ("Einkauf.Apple", (), ("apple.com", "itunes.com"), r""),
    ("Einkauf.Ebay", (), ("ebay.de", "ebay.com", "ebay-kleinanzeigen.de",
                          "kleinanzeigen.de"), r""),
    ("Einkauf.Versand", (), ("dhl.de", "dhl.com", "deutschepost.de", "dpd.de", "hermes.de",
                             "ups.com", "gls-group.eu"), r""),
    ("Einkauf.Cyberport", (), ("cyberport.de",), r""),
    ("Einkauf.Alternate", (), ("alternate.de",), r""),
    ("Einkauf.Pearl", (), ("pearl.de",), r""),
    ("Einkauf.Ikea", (), ("ikea.com", "ikea.de"), r""),
    ("Einkauf.Tylko", (), ("tylko.com",), r""),
    ("Einkauf.Foto", (), ("fotoparadies.de", "500px.com"), r""),
    ("Einkauf.Payback", (), ("payback.de",), r""),

    # ── Telekommunikation ───────────────────────────────────────────────────
    ("Telekommunikation.Vodafone", (), ("vodafone.com", "vodafone.de"), r""),
    ("Telekommunikation.Telekom", (), ("telekom.de", "t-online.de"), r""),

    # ── Abos, Gaming, Freizeit ──────────────────────────────────────────────
    ("Abos.Netflix", (), ("netflix.com",), r""),
    ("Abos.Allgemein", (), ("aboalarm.de", "twitch.tv", "spotify.com"), r""),
    ("Gaming", (), ("badlion.net", "mojang.com", "steampowered.com",
                    "epicgames.com", "blizzard.com", "riotgames.com"), r""),
    ("Freizeit.Essen", (), ("freddy-fresh.de", "mcdonalds.de", "mcdonalds.com",
                            "lieferando.de",
                            "ewhisky.de"), r""),
    ("Freizeit.Gluecksspiel", (), ("tipp24.com", "lottoland.com"), r""),

    # ── Arbeit ──────────────────────────────────────────────────────────────

    # ── Menschen: nur wer wirklich oft schreibt ─────────────────────────────

    # ── Mobilitaet & Auto (zweiter Regelsatz, aus dem Auffang gelernt) ──────
    # `m8mit.de` (120 Mails) sind die KIA-Leistungsnachweise — sie lagen alle
    # in `Shopping.KIA`, der Name verrät es aber nicht.
    ("Auto.Laden", (), ("enbw.com", "kiacharge.com", "maingau-energie.de",
                        "chargemap.com", "plugsurfing.com", "ionity.eu"), r""),
    ("Fahrrad", (), ("collos.de", "bikeleasing.de", "fantic26.de",
                     "bike-discount.de", "rosebikes.de", "canyon.com"), r""),
    ("Mobilitaet", (), ("tier.app", "nextbike.de", "voi.com"), r""),

    # ── Reisen ──────────────────────────────────────────────────────────────
    ("Reisen", (), ("booking.com", "reisebuero-finsterbusch.de",
                    "tropical-islands.de", "dertouristik.com", "kolumbus24.com",
                    "pytloun-hotels.cz", "airbnb.com", "ryanair.com",
                    "lufthansa.com", "bahn.de", "flixbus.de", "trivago.de",
                    "leipzig-halle-airport.de", "hotsplots.de"), r""),

    # ── Kinder ──────────────────────────────────────────────────────────────
    # 33 von 34 lagen in `Shopping.Rechnungen Hort` — Betreuungsrechnungen.
    ("Kinder.Hort", (), ("lernsax.de", "hgr-web.lernsax.de"), r""),
    ("Kinder.Schule", (), (), r"elternabend|elterninformation|klassenstufe|"
     r"schulanmeldung|zeugnis"),

    # ── Haushalt & SmartHome ────────────────────────────────────────────────
    ("Haushalt.Miele", (), ("miele.de", "news.miele.de", "mailservice.miele.de"), r""),
    ("Technik.SmartHome", (), ("evehome.com", "ifttt.com", "dyndns.com",
                               "smrtguard.com", "teamviewer.com", "shelly.cloud",
                               "home-assistant.io"), r""),
    ("Technik.Drohne", (), ("dji.com", "e.dji.com"), r""),

    # ── Gesundheit ──────────────────────────────────────────────────────────
    ("Gesundheit", (), ("drksachsen.de", "doctolib.de", "brillenplatz.de"), r""),

    # ── Medien & Unterhaltung ───────────────────────────────────────────────
    ("Medien", (), ("youtube.com", "flickr.com", "audials.com",
                    "uci-kinowelt.info", "spotify.com"), r""),
    ("Freizeit.Allgemein", (), ("hifi-forum.de", "ravelry.com",
                                "tubeampdoctor.com", "play-dresden.de",
                                "heavyweather.de"), r""),

    # ── Einkauf, was oben keinen eigenen Ordner hat ──────────────────────────
    ("Einkauf.Allgemein", (), ("lidl.de", "mediamarkt.de", "conrad.de",
                               "idealo.com", "de.idealo.com", "asgoodasnew.com",
                               "nike.com", "official.nike.com", "skatepro.de",
                               "elektrovorteil.de", "akademische.de", "aubu.de",
                               "digitalriver.com", "mous.co", "saturn.de",
                               "otto.de", "zalando.de"), r""),
    ("Gaming", (), ("playstation.com", "txn-email03.playstation.com", "ubi.com",
                    "xsolla.com", "gog.com", "nintendo.com"), r""),
    ("Steuer", (), ("wolterskluwer.com", "elster.de", "buhl.de"), r""),
    # Wohnungs-/Haussuche gehoert zum Haus — dessen Regel „alles was mit dem
    # haus zu tun hat kommt unter Hausbau".
    ("Hausbau.Suche", (), ("immobilienscout24.de", "immowelt.de"), r""),
    ("Telekommunikation.Kabel", (), ("kabeldeutschland.de", "unitymedia.de"), r""),

    # 🔴 dessen EIGENE Adressen muessen HIER stehen — vor `Menschen.Weitere`.
    #    Beim ersten Messen fiel „Eigene Post Archiv" von 963 auf 14, weil
    #    `gmail.com` in der Menschen-Regel seine 863 eigenen gesendeten Mails
    #    mitnahm. Und sie duerfen NICHT weiter oben stehen: 6.954 Geraeteberichte
    #    tragen dieselbe Adresse und gehoeren nach `Technik.*`.
    # 🔑 Bleibt als PLATZHALTER stehen: die eigenen Adressen kommen zur Laufzeit
    #    aus `eigen_laden()` (siehe `ziel_fuer`), hier steht nur der Zielordner.

    # ── Menschen ohne eigenen Ordner: private Postfachanbieter ──────────────
    # 🔴 Absichtlich SPAET: erst wenn keine Firma, kein Dienst und kein Geraet
    #    gepasst hat, ist ein gmail/gmx/web.de-Absender wahrscheinlich ein
    #    Mensch. Weiter oben würde diese Regel Firmenpost mitnehmen, die bei
    #    einem Freemailer sitzt.
    ("Menschen.Weitere", (), ("gmail.com", "googlemail.com", "gmx.de", "gmx.net",
                              "posteo.de",
                              "web.de", "hotmail.com", "hotmail.de", "freenet.de",
                              "me.com", "icloud.com", "t-online.de", "arcor.de",
                              "yahoo.de", "aol.com", "mail.ru"), r""),

    # ── Spaete Muster: greifen nur, wenn oben nichts passte ─────────────────
    ("Steuer", (), (), r"steuererklärung|lohnsteuer|elster|finanzamt|"
     r"einkommensteuer|steuerbescheid"),
    ("Behoerden", (), (), r"standesamt|meldebehörde|bürgerbüro|landratsamt"),
]

_RX = {}


def _passt_rx(rx: str, text: str) -> bool:
    if rx not in _RX:
        _RX[rx] = re.compile(rx, re.I)
    return bool(_RX[rx].search(text))


def mid_bestaetigt(soll: str, ist: str) -> bool:
    """Darf diese UID bewegt werden?

    🔴 NUR wenn die Message-Id am Ort GEFUNDEN wurde UND passt. Ein LEERES
    Ergebnis ist keine Bestaetigung, sondern der Normalfall fuer „UID gibt es
    nicht mehr" — beim Nachweis am 27.09. meldete `anwenden` „3 bewegt",
    obwohl es nichts bewegt hatte: die Mails waren nach einem `zurueck` unter
    NEUEN UIDs wieder da, die alte UID lieferte keinen Kopf, und die Pruefung
    liess das durch.
    """
    soll = (soll or "").strip()
    ist = (ist or "").strip()
    if not ist:
        return False          # nichts gefunden = nicht bestaetigt
    if not soll:
        return False          # ohne Plan-Id gibt es nichts zu bestaetigen
    return soll == ist


def eigene_regeln() -> dict:
    """dessen EIGENE Absenderregeln aus den Einstellungen. {adresse: ordner}

    🔴 27.09.2026, teuer gelernt: der Umbau entfernte `Auto.Autohaus-nord`, weil er
    ihn fuer leer und entbehrlich hielt — dabei hatte der Besitzer ihn selbst angelegt
    und eine Regel `j.haller@autohaus-nord.example -> Auto.Autohaus-nord` gesetzt. Der Plan
    schob die zwei Mails nach `Auto.Allgemein`, der Ordner wurde leer, und das
    Aufraeumen nahm ihn mit. Danach zeigte seine Regel auf einen Ordner, den es
    nicht mehr gab.

    🔑 DER BESITZER EIGENE ZUORDNUNG SCHLAEGT ALLES. Das gilt im Waechter seit 2.3.0
       (`absender_regel()` vor der gelernten Ablage) — der Umbau wusste es nur
       nicht. Eine zweite Stelle, die dieselbe Frage anders beantwortet, ist eine
       zu viel.
    """
    try:
        e = pw.einstellungen()
    except Exception:
        return {}
    ar = e.get("absender_regeln")
    if not isinstance(ar, dict):
        return {}
    return {str(k).strip().lower(): str(v).strip()
            for k, v in ar.items() if str(v).strip()}


# ── Der persoenliche Teil des Katalogs gehoert NICHT in den Quelltext ────────
# 🔴 27.09.2026, der teuerste Fund des Tages: im OEFFENTLICHEN Repo standen in
#    allen drei Commits der Name und die Mailadresse einer echten Person
#    (`k.ivanov@autogruppe.example`, aus einem Beispiel in einer
#    Dokumentationszeile), dazu Geraetenamen und der Mailanbieter. Das Repo
#    wurde geloescht und neu aufgebaut.
#
#    Der Schrubber beim Veroeffentlichen war die falsche Antwort: er ist ein
#    NETZ, keine Mauer. 20 von 74 Regeln trugen echte Adressen — Familie,
#    Bankberaterin, Architekt, Handwerker, Arbeitgeber. Solange die im Quelltext
#    stehen, entscheidet eine Regex darueber, ob sie oeffentlich werden.
#
# 🔑 DIE MAUER: der persoenliche Teil liegt in `state/regeln_eigen.json` —
#    dieselbe Schublade wie die Zugangsdaten, 0600, in KEINER Ausrollliste.
#    Im Quelltext stehen nur noch Regeln, die fuer jeden gelten (PayPal, Amazon,
#    Telekom). Was niemand veroeffentlichen kann, muss niemand herausfiltern.
EIGEN_DATEI = "regeln_eigen.json"


def eigen_laden() -> dict:
    """Der persoenliche Katalog dieser Installation. Fehlt er, ist er leer.

    Aufbau:
      {"eigene_adressen": ["..."],
       "regeln": [["Ziel", ["adresse", ...], ["domain", ...], "muster"], ...]}

    🔴 Leer ist ein gueltiger Zustand, kein Fehler: eine frische Installation
       hat keinen persoenlichen Teil, und der Waechter muss trotzdem laufen.
    """
    if "katalog" in _EIGEN_ZWISCHEN:
        return _EIGEN_ZWISCHEN["katalog"]
    v = pw.load(EIGEN_DATEI, None)
    raus = {"eigene_adressen": [], "regeln": []}
    if isinstance(v, dict):
        adr = v.get("eigene_adressen")
        if isinstance(adr, list):
            raus["eigene_adressen"] = [str(a).strip().lower() for a in adr if str(a).strip()]
        for r in v.get("regeln") or []:
            # 🔴 Jeder Eintrag traegt seine urspruengliche POSITION im Katalog.
            #    Ohne sie standen beim ersten Anlauf alle persoenlichen Regeln
            #    vorn — damit kam „Eigene Post Archiv" VOR den Geraeteregeln,
            #    und 823 FRITZ!-Meldungen von der eigenen Adresse landeten im
            #    Archiv statt unter Technik. Die Reihenfolge IST die Logik.
            if not isinstance(r, dict) or not str(r.get("ziel") or "").strip():
                continue
            raus["regeln"].append({
                "nr": int(r.get("nr") or 0),
                "regel": (str(r["ziel"]).strip(),
                          tuple(str(x).strip().lower() for x in (r.get("adressen") or [])),
                          tuple(str(x).strip().lower() for x in (r.get("domains") or [])),
                          str(r.get("muster") or "")),
            })
        raus["regeln"].sort(key=lambda e: e["nr"])
    _EIGEN_ZWISCHEN["katalog"] = raus
    return raus


def katalog():
    """Persoenlicher und allgemeiner Teil in der URSPRUENGLICHEN Reihenfolge.

    🔑 Die persoenlichen Regeln kommen an ihre gemerkte Position zurueck —
       zwischen die allgemeinen, nicht davor. „Meine Bankberaterin" muss vor
       „irgendeine Bank" stehen, die Geraeteregeln aber vor „meine eigene Post".
       Beides gleichzeitig geht nur ueber die Position.
    """
    if "katalog_voll" in _EIGEN_ZWISCHEN:
        return _EIGEN_ZWISCHEN["katalog_voll"]
    eigen = eigen_laden()["regeln"]
    nach_nr = {e["nr"]: e["regel"] for e in eigen}
    rest = list(REGELN)
    raus = []
    # 🔴 Der Bereich muss die HOECHSTE Position fassen, nicht nur die Summe der
    #    Laengen. Sonst fiel eine Regel mit hoher Nummer still heraus — genau so
    #    verschwand im Pruefstand „Eigene Post Archiv" (Position 68) aus einem
    #    Katalog mit 60 Eintraegen, und niemand haette es gemerkt.
    hoechste = max(nach_nr) if nach_nr else -1
    for i in range(max(hoechste + 1, len(eigen) + len(REGELN))):
        if i in nach_nr:
            raus.append(nach_nr[i])
        elif rest:
            raus.append(rest.pop(0))
    raus.extend(rest)          # was uebrig ist, geht nie verloren
    # 🔴 Gegenprobe im Betrieb: keine Regel darf beim Verzahnen abhandenkommen.
    if len(raus) != len(eigen) + len(REGELN):
        pw.log("Katalog unvollstaendig: %d statt %d Regeln"
               % (len(raus), len(eigen) + len(REGELN)))
    _EIGEN_ZWISCHEN["katalog_voll"] = raus
    return raus


_EIGEN_ZWISCHEN = {}


def ziel_fuer(m: dict):
    """Erste passende Regel gewinnt. Gibt (Ziel, Begruendung) zurueck.

    🔑 GANZ VORN stehen dessen eigene Absenderregeln. Sie sind seine
       Entscheidung, nicht meine Ableitung — nichts darf sie ueberstimmen.
    """
    von = (m.get("von") or "").lower()
    if "absender" not in _EIGEN_ZWISCHEN:
        _EIGEN_ZWISCHEN["absender"] = eigene_regeln()
    eigen = _EIGEN_ZWISCHEN["absender"].get(von)
    if eigen:
        return eigen, "deine eigene Regel"
    dom = von.split("@")[-1]
    # Der Betreff wird normalisiert (klein, Umlaut+Umschrift gleich) — dieselbe
    # Funktion, die 11.09. den Umlaut-Fehler an der Wurzel behoben hat.
    heu = pw.normal(m.get("betreff") or "")
    roh = (m.get("betreff") or "").lower()
    # 🔑 Der PERSOENLICHE Katalog zuerst, dann der allgemeine. Er ist genauer:
    #    „meine Bankberaterin" schlaegt „irgendeine Bank". Und er liegt in
    #    `state/`, nicht im Quelltext — siehe `eigen_laden()`.
    for ziel, adressen, domains, rx in katalog():
        # Der Platzhalter fuer die eigene Post bekommt seine Adressen zur
        # Laufzeit — im Quelltext steht dort ein leeres Tupel.
        if ziel == EIGENE_POST and not adressen:
            adressen = tuple(eigen_laden()["eigene_adressen"])
        adr_tr = bool(adressen) and von in adressen
        dom_tr = bool(domains) and any(dom == d or dom.endswith("." + d)
                                       for d in domains)
        rx_tr = bool(rx) and (_passt_rx(rx, heu) or _passt_rx(rx, roh))
        hat_wer = bool(adressen) or bool(domains)
        if hat_wer and rx:
            if (adr_tr or dom_tr) and rx_tr:
                return ziel, "Absender+Betreff"
        elif hat_wer:
            # 🔴 Eine Regel auf die genaue ADRESSE bleibt unangetastet — sie ist
            #    schon so genau, wie es geht. Nur wo die DOMAIN zieht, wird
            #    nachgesehen, ob dieser Absender alles Moegliche verschickt.
            if adr_tr:
                return ziel, "Absender %s" % von
            if dom_tr:
                fein, warum = fein_entscheiden(von, m.get("betreff") or "")
                if fein:
                    return fein, warum
                return ziel, "Absender %s" % dom
        elif rx and rx_tr:
            return ziel, "Betreff"
    # 🔴 Erst NACH allen Regeln: ein Absender mit eigenem Ordner soll dort
    #    landen, auch wenn er seine Post als Rundschreiben verschickt.
    if m.get("liste"):
        return NEWSLETTER, "List-Id"
    return AUFFANG, ""


# ── Stufe 2b: Struktur AUS DEN INHALTEN ableiten ─────────────────────────────
# der Besitzer, 27.09.2026: „er soll alle mails analysieren und sortieren und die neue
# ordnerstrucktur basierent auf die mailinhalte bauen".
#
# 🔑 Die Regeln oben decken das ab, was der Besitzer AUSDRUECKLICH genannt hat
#    (Banking, Hausbau) und was gemessen gross ist. Alles andere soll der Umbau
#    SELBST herausfinden — sonst muss jede neue Firma von Hand eingetragen
#    werden und der Auffang waechst still mit.
#
# 🔴 Abgeleitet wird NUR ein Ordnername, nie eine Verschiebung ohne Ordner. Wo
#    die Ableitung nichts erkennt, bleibt es beim Auffang — ein falsch benannter
#    Ordner ist schlimmer als ein sichtbarer Rest.

# Wie viele Mails ein Absender braucht, damit ein eigener Ordner entsteht.
# Darunter lohnt der Ordner nicht: er kostet einen Klick und spart keinen.
SCHWELLE_EIGENER_ORDNER = 4

# Was der Betreff ueber die Art der Post sagt. Reihenfolge = Vorrang.
# Die Muster sind ASCII — `normal()` macht vorher ae/oe/ue daraus.
INHALT_MUSTER = [
    ("Konten", r"anmeldung|passwort|kennwort|sicherheitscode|verifizier|"
               r"zwei-faktor|bestaetige (deine|ihre) e-?mail|konto (gesperrt|"
               r"erstellt)|willkommen bei"),
    ("Einkauf", r"bestellung|bestellbestaetigung|lieferung|versand|paket|"
                r"sendung|rechnung|zahlungsbestaetigung|widerruf|retoure|"
                r"auftragsbestaetigung|quittung|beleg"),
    ("Versicherungen", r"versicherung|police|beitragsrechnung|schadensmeldung|"
                       r"tarifwechsel"),
    ("Reisen", r"buchung|reservierung|hotel|flug|check-?in|reise|unterkunft|"
               r"ticket fuer"),
    ("Gesundheit", r"arzt|praxis|termin.*(arzt|zahn)|rezept|befund|blutspende"),
    ("Kinder", r"elternabend|betreuung|kita|hort|schule|zeugnis|klassenstufe"),
    ("Technik", r"firmware|update verfuegbar|systemprotokoll|backup|"
                r"zertifikat|server|nas\b|router|log\b"),
    ("Freizeit", r"verein|training|konzert|kino|restaurant|tischreservierung"),
    ("Abos", r"abonnement|abo\b|mitgliedschaft|verlaengert sich|kuendigungs"),
]


def marke_aus_domain(dom: str) -> str:
    """Aus `meine.steuertipps.de` wird `Steuertipps`.

    🔴 Die MARKE steht nicht vorn. `news.miele.de`, `mail.anthropic.com`,
    `txn-email03.playstation.com` — wer das erste Stueck nimmt, legt Ordner
    namens „News", „Mail" und „Txn-email03" an. Genommen wird das Stueck VOR
    der oeffentlichen Endung, und mehrteilige Endungen (`co.uk`) zaehlen als
    eine.
    """
    mehrteilig = ("co.uk", "com.au", "co.jp", "com.br", "co.nz")
    teile = [x for x in (dom or "").lower().split(".") if x]
    if not teile:
        return ""
    if len(teile) >= 3 and ".".join(teile[-2:]) in mehrteilig:
        kern = teile[-3]
    elif len(teile) >= 2:
        kern = teile[-2]
    else:
        kern = teile[0]
    # Technische Vorsilben, die keine Marke sind
    for weg in ("mail", "email", "e", "news", "newsletter", "info", "no-reply",
                "noreply", "smtp", "mx", "web", "my", "meine", "mein"):
        if kern == weg and len(teile) >= 3:
            kern = teile[-3]
    kern = re.sub(r"[^a-z0-9äöüß-]", "", kern)
    if not kern or len(kern) < 2:
        return ""
    return kern[:1].upper() + kern[1:]


def kategorie_aus_inhalt(betreffe: list) -> str:
    """Welche oberste Schublade passt zu DIESEN Betreffen?

    Gezaehlt wird, nicht beim ersten Treffer abgebrochen: ein Absender schickt
    Bestellungen UND Newsletter, und dann entscheidet die Mehrheit, nicht die
    erste Mail, die man zufaellig zuerst liest.
    """
    punkte = {}
    for b in betreffe:
        h = pw.normal(b or "")
        for kat, rx in INHALT_MUSTER:
            if _passt_rx(rx, h):
                punkte[kat] = punkte.get(kat, 0) + 1
                break
    if not punkte:
        return ""
    best = max(punkte, key=lambda k: punkte[k])
    # Eine Mehrheit muss eine sein: bei einem einzigen Treffer unter vielen
    # Mails ist das Raten, nicht Erkennen.
    if punkte[best] * 3 < len(betreffe):
        return ""
    return best


# ── Der Wortschatz: aus dessen EIGENEN Ordnern lernen ───────────────────────
# der Besitzer, 27.09.2026: „warum wird unsortiert eigentlich nicht sortiert? sind doch
# klare mails mit klaren inhalten die sich super einsortieren lassen, baue die app
# so das auch diese sauber erkannt werden, nicht du sollst das machen sondern
# immer die postwache, die nutzt doch ki oder?"
#
# Gemessen an den 489 Mails im Auffang: 234 verschiedene Absender-Domains, davon
# 202 mit weniger als 4 Mails. Eine Regel je Domain waere eine Liste, die ich
# schreibe — genau das, was der Besitzer NICHT will, und sie waere morgen wieder zu
# kurz. Die Muster (`INHALT_MUSTER`) trafen bei 14 von 15 der groessten Absender
# NICHT, weil sie auf Rechnungs- und Vertragswoerter gebaut sind.
#
# 🔑 Die Postwache hat aber etwas viel Besseres als jede Liste: 17.500 Mails, die
#    der Besitzer SELBST in 132 Ordner einsortiert hat. Das ist ein beschrifteter
#    Lehrstoff. Daraus laesst sich lernen, welche WOERTER zu welcher Kategorie
#    gehoeren — und eine unbekannte Booking-Mail landet dann unter „Reisen",
#    weil „hotel", „buchung" und „check" dort gehaeuft vorkommen. Das ist genau
#    der Grundsatz der Postwache, nur auf den Betreff angewandt statt auf den
#    Absender: SIE KANN NUR NACHAHMEN.
WORTSCHATZ = "umbau_wortschatz.json"
WORT_MIN_TREFFER = 2      # so viele bekannte Woerter muss ein Betreff haben
# 🔑 GEMESSEN am 27.09.2026 am ganzen Bestand (16.000 Mails, Kreuzprobe):
#      Abstand 1,6 -> 93,9 % Treffer bei 78,0 % Abdeckung, 770 Fehler
#      Abstand 2,5 -> 98,3 % Treffer bei 69,4 % Abdeckung, 190 Fehler
#      Abstand 3,5 -> 99,0 % Treffer bei 65,4 % Abdeckung, 103 Fehler
#    2,5 ist der Knick: drei Viertel der Fehler weg fuer neun Punkte Abdeckung.
#    Darueber wird es teuer und bringt kaum noch etwas. 🔴 Eine falsche
#    Einsortierung ist teurer als eine offene: im Auffang SIEHT der Besitzer, dass
#    etwas aussteht — in der falschen Schublade sieht er nichts.
WORT_ABSTAND = 2.5        # so weit muss der Erste vor dem Zweiten liegen
# Diese Ordner lehren NICHT: der Auffang ist die Frage selbst, und dessen eigene
# gesendete Post traegt die Betreffe ALLER Kategorien (63 % des Archivs) — sie
# wuerde jede Kategorie mit jedem Wort verbinden.
KEIN_WORTLEHRER = ("Unsortiert", "Eigene Post Archiv")

# Fuellwoerter. 🔴 Eine Stoppwortliste ist SPRACHE, nicht Logik — sie gehoert
# neben die Muster und nicht in eine Sprachdatei: sie wird nie angezeigt.
STOPP = set("""
und der die das den dem des ein eine einen einem eines fuer mit von vom bei
ist sind war waren wird werden wurde wurden hat haben hatte sie ihr ihre ihren
ihrem sich nicht auch noch nur aus auf zur zum als wie was wer wenn dann aber
oder dass sehr mehr sehr neue neuer neues ihnen uns wir mein meine unser
the and for you your with from this that have has was are our not but all any
new now can will one two more info mail email message please dear hello hallo
guten tag herr frau sehr geehrte geehrter geehrtes team support service
""".split())


def worte(text: str) -> list:
    """Einen Betreff in vergleichbare Woerter zerlegen.

    `pw.normal()` schreibt klein und loest Umlaute auf — dieselbe Umschrift, die
    die Muster benutzen, damit „Rueckfrage" und „Rückfrage" dasselbe Wort sind.
    Reine Zahlen fallen weg: eine Bestellnummer ist kein Wort, sie kommt genau
    einmal vor und waere fuer jede Kategorie gleich wertlos.
    """
    h = pw.normal(text or "")
    return [w for w in re.findall(r"[a-z][a-z0-9]{2,}", h) if w not in STOPP]


def kategorie_des_ordners(ordner: str) -> str:
    """Die oberste Stufe — `Banking.PayPal` lehrt fuer `Banking`.

    🔑 Gelernt wird ueber ALLE seine Ordner, nicht nur ueber die Kategorien aus
       `REGELN`: `Fahrrad`, `Hausbau`, `Menschen` sind genauso seine Schubladen.
       Wer nur die eigenen Kategorien lernt, bringt dem Programm bei, was es
       schon weiss.
    """
    erste = (ordner or "").split(".")[0].strip()
    if not erste or erste.upper() == "INBOX":
        return ""
    if any(erste == k or ordner == k for k in KEIN_WORTLEHRER):
        return ""
    return erste


def wortschatz_lernen(inv: dict, sichern: bool = True) -> dict:
    """Welche Woerter gehoeren zu welcher Kategorie? Aus dem Inventar gezaehlt.

    Gezaehlt wird je Mail die MENGE ihrer Woerter, nicht jedes Vorkommen: ein
    Betreff, der „rechnung" dreimal sagt, ist nicht dreimal so aussagekraeftig.
    """
    kats, mails = {}, {}
    for ordner, v in (inv.get("ordner") or {}).items():
        if v.get("tabu"):
            continue
        kat = kategorie_des_ordners(ordner)
        if not kat:
            continue
        for m in v.get("mails") or []:
            menge = set(worte(m.get("betreff")))
            if not menge:
                continue
            mails[kat] = mails.get(kat, 0) + 1
            d = kats.setdefault(kat, {})
            for w in menge:
                d[w] = d.get(w, 0) + 1
    # Woerter, die nur EINMAL in ihrer Kategorie vorkommen, sind Rauschen und
    # blaehen die Datei auf (gemessen: 60 % der Eintraege, kaum Wirkung).
    for kat, d in list(kats.items()):
        kats[kat] = {w: n for w, n in d.items() if n >= 2}
    ws = {"gelernt": time.strftime("%Y-%m-%dT%H:%M:%S"),
          "kategorien": kats, "mails": mails}
    if sichern:
        with open(out_pfad(WORTSCHATZ), "w", encoding="utf-8") as f:
            json.dump(ws, f, ensure_ascii=False)
    return ws


_WS_ZWISCHEN = {}


def wortschatz_laden() -> dict:
    if "ws" not in _WS_ZWISCHEN:
        try:
            with open(out_pfad(WORTSCHATZ), encoding="utf-8") as f:
                _WS_ZWISCHEN["ws"] = json.load(f)
        except Exception:
            _WS_ZWISCHEN["ws"] = {}
    return _WS_ZWISCHEN["ws"]


def _wort_gesamt(ws: dict) -> dict:
    """{wort: in wie vielen Mails insgesamt} und die Gesamtzahl der Mails.

    Einmal je Wortschatz berechnet und am Wortschatz gemerkt — die Kreuzprobe
    ruft die Einordnung 16.000-mal auf.
    """
    if "_gesamt" not in ws:
        c = {}
        for d in (ws.get("kategorien") or {}).values():
            for w, n in d.items():
                c[w] = c.get(w, 0) + n
        ws["_gesamt"] = c
        ws["_mails"] = sum((ws.get("mails") or {}).values()) or 1
    return ws["_gesamt"]


def kategorie_aus_wortschatz(betreffe: list, ws: dict = None,
                             ohne: dict = None, modus: str = "pmi"):
    """(Kategorie, Abstand) aus den Woertern — oder ("", Abstand) bei Unsicherheit.

    🔴 ERSTER VERSUCH WAR FALSCH, und die Kreuzprobe hat ihn erlegt. Ich hatte
       das Vorwissen ueber die Groesse der Kategorien absichtlich weggelassen
       („die Frage ist, WESSEN Wortschatz das ist, nicht welche Kategorie die
       haeufigste ist"). Gemessen: **25,7 % Treffer** — und die
       Verwechslungstabelle zeigte, warum: 2.519 Technik-Mails wanderten nach
       `Gesundheit` (15 Mails, 10 Woerter). Bei Add-1-Glaettung ist ein
       UNBEKANNTES Wort in einer winzigen Kategorie billig, weil dort durch eine
       winzige Summe geteilt wird. Also gewinnt die kleinste Kategorie fast
       jede Abstimmung.

    🔑 Richtig ist, nur POSITIVE Belege zu summieren und sie gegen den
       Gesamtbestand zu normieren — je Wort
       `log( p(Wort|Kategorie) / p(Wort|alle) )`. Ein Wort, das ueberall
       vorkommt („rechnung"), traegt fast nichts; ein Wort, das fast nur in
       einer Kategorie steht („statik", „diskstation"), traegt viel. Woerter,
       die eine Kategorie NICHT kennt, zaehlen nicht mit — sonst entscheidet
       wieder die Groesse des Nenners statt der Inhalt.

    `ohne` zieht die Woerter EINER Mail von ihrer eigenen Kategorie ab — nur
    fuer die Kreuzprobe, damit sich dort niemand selbst beantwortet.
    """
    ws = ws if ws is not None else wortschatz_laden()
    kats = ws.get("kategorien") or {}
    if not kats:
        return "", 0.0
    menge = set()
    for b in betreffe or []:
        menge.update(worte(b))
    if not menge:
        return "", 0.0
    gesamt = _wort_gesamt(ws)
    n_alle = ws.get("_mails") or 1
    mails = ws.get("mails") or {}
    bekannt = sorted(w for w in menge if w in gesamt)
    if len(bekannt) < WORT_MIN_TREFFER:
        return "", 0.0
    punkte, belege = {}, {}
    for kat, d in kats.items():
        n_kat = mails.get(kat, 0)
        weg = ohne.get(kat) if isinstance(ohne, dict) else None
        if weg:
            n_kat -= 1                      # die eigene Mail zaehlt nicht mit
        if n_kat < 1:
            continue
        wert, treffer = 0.0, 0
        for w in bekannt:
            c = d.get(w, 0)
            c_alle = gesamt.get(w, 0)
            if weg and w in weg:
                c -= 1
                c_alle -= 1
            if c < 1 or c_alle < 1:
                continue                    # kennt die Kategorie nicht -> kein Beleg
            treffer += 1
            wert += math.log((c / float(n_kat)) /
                             (c_alle / float(max(1, n_alle - (1 if weg else 0)))))
        if treffer:
            punkte[kat] = wert
            belege[kat] = treffer
    if not punkte:
        return "", 0.0
    rang = sorted(punkte.items(), key=lambda x: -x[1])
    erster = rang[0][0]
    # 🔴 Der Sieger muss die Woerter auch KENNEN. Ein Treffer aus einem
    #    einzigen Wort ist ein Zufall, nicht ein Urteil.
    if belege.get(erster, 0) < WORT_MIN_TREFFER:
        return "", 0.0
    if len(rang) < 2:
        return erster, 99.0
    abstand = rang[0][1] - rang[1][1]
    if abstand < WORT_ABSTAND:
        return "", abstand
    return erster, abstand

def wortschatz_pruefen(inv: dict, ws: dict = None) -> dict:
    """Kreuzprobe am eigenen Bestand: wie oft trifft der Wortschatz?

    🔑 Jede Mail wird beurteilt, NACHDEM ihre eigenen Woerter aus ihrer
       Kategorie abgezogen wurden. Ohne das beantwortet sich jede Mail selbst,
       und die Trefferquote waere eine Selbstauskunft. Das ist derselbe Fehler
       wie ein Pruefstand, der das eigene Gedaechtnis misst.
    """
    ws = ws if ws is not None else wortschatz_laden()
    richtig = falsch = offen = 0
    daneben = {}
    for ordner, v in (inv.get("ordner") or {}).items():
        if v.get("tabu"):
            continue
        soll = kategorie_des_ordners(ordner)
        if not soll:
            continue
        for m in v.get("mails") or []:
            menge = set(worte(m.get("betreff")))
            if not menge:
                continue
            kat, _abstand = kategorie_aus_wortschatz(
                [m.get("betreff")], ws, ohne={soll: menge})
            if not kat:
                offen += 1
            elif kat == soll:
                richtig += 1
            else:
                falsch += 1
                daneben[(soll, kat)] = daneben.get((soll, kat), 0) + 1
    geurteilt = richtig + falsch
    return {"richtig": richtig, "falsch": falsch, "offen": offen,
            "geurteilt": geurteilt,
            "treffer": (richtig / geurteilt) if geurteilt else 0.0,
            "abdeckung": (geurteilt / (geurteilt + offen)) if (geurteilt + offen) else 0.0,
            "daneben": sorted(daneben.items(), key=lambda x: -x[1])[:15]}


def domain_karte_laden() -> dict:
    p = out_pfad("umbau_domains.json")
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}


def domain_karte_sichern(k: dict) -> None:
    with open(out_pfad("umbau_domains.json"), "w", encoding="utf-8") as f:
        json.dump(k, f, ensure_ascii=False, indent=1, sort_keys=True)


def ki_kategorien(offen: dict, karte: dict):
    """Das eingestellte Modell einordnen lassen, was die Muster nicht erkannten.

    🔴 Faellt aus, ohne den Umbau zu kosten: kein Modell, kein Netz, kaputte
    Antwort — dann bleibt es bei der Ableitung aus den Mustern. Eine
    Urteilshilfe darf den Postlauf nie aufhalten (dieselbe Regel wie bei
    `ki_regeln_vorschlagen`).
    """
    # 🔴 Gibt (Anzahl, Grund) zurueck. Vorher war es eine blanke 0 — und genau
    #    deshalb stand am 27.09. KEINE der 234 Domains aus dem Auffang in der
    #    Karte, obwohl die Seite „KI bereit" meldete: der Anbieter ist
    #    „werkstatt", und die arbeitet ueber Weckrufe, also ASYNCHRON. Hier
    #    braucht es eine Antwort im selben Augenblick. Ein stilles 0 sieht aus
    #    wie „das Modell wusste nichts" und ist „das Modell wurde nie gefragt".
    # 🔴 Zurueck kommt (Anzahl, CODE, Text). Der Code ist fuer die Verzweigung,
    #    der Text fuer den Menschen. Wer an einem deutschen Satz verzweigt
    #    („'Werkstatt' in grund"), baut einen Fehler in die naechste
    #    Umformulierung ein — und in jede Uebersetzung.
    try:
        ok, grund = pw.ki_bereit()
        if not ok:
            return 0, "kein_modell", grund or "kein Modell eingerichtet"
        if pw.ki_konfig().get("anbieter") == "werkstatt":
            return 0, "werkstatt", (
                "Anbieter \u201eWerkstatt\u201c arbeitet ueber Auftraege "
                "(asynchron) und kann hier nicht im selben Augenblick antworten")
    except Exception as e:
        return 0, "fehler", "Modell nicht erreichbar: %s" % str(e)[:80]
    erlaubt = sorted({z.split(".")[0] for z, _, _, _ in REGELN})
    gefragt = 0
    namen = sorted(offen, key=lambda d: -len(offen[d]))[:60]
    for i in range(0, len(namen), 15):
        teil = namen[i:i + 15]
        zeilen = []
        for d in teil:
            bs = [b for b in offen[d][:4] if b]
            zeilen.append("%s | %s" % (d, " ; ".join(b[:70] for b in bs)))
        system = ("Du ordnest E-Mail-Absender in Postfach-Ordner ein. Antworte "
                  "NUR mit JSON: [{\"domain\":\"...\",\"kategorie\":\"...\"}]. "
                  "Erlaubte Kategorien: " + ", ".join(erlaubt) +
                  ". Passt keine, schreibe \"\" als kategorie.")
        frage = ("Ordne jede Zeile (Domain | Beispiel-Betreffe) einer Kategorie "
                 "zu:\n" + "\n".join(zeilen))
        try:
            antw = pw._json_aus_text(pw.ki_fragen(system, frage))
        except Exception:
            antw = None
        if not isinstance(antw, list):
            continue
        for e in antw:
            if not isinstance(e, dict):
                continue
            d = str(e.get("domain") or "").strip().lower()
            kat = str(e.get("kategorie") or "").strip()
            if d in offen and kat in erlaubt:
                karte[d] = {"kategorie": kat, "quelle": "ki"}
                gefragt += 1
    return gefragt, "", ""


# ── Die KI in der Werkstatt nutzen, dort wo sie laeuft ───────────────────────
# der Besitzer, 27.09.2026: „die postwache soll die ki in der werkstatt nutzen dort wo
# sie laeuft."
#
# 🔴 Die Werkstatt ist ein AUFTRAGSBUCH, kein Modell-Endpunkt. `local_engine`
#    kennt `create_thread` und eine Warteschlange — ein Agent nimmt den Auftrag
#    spaeter an. Es gibt dort keine Stelle, die eine Frage im selben Augenblick
#    beantwortet. Wer das ignoriert, baut einen Aufruf, der 60 s wartet und dann
#    nichts hat.
#
# 🔑 Also ein RUNDLAUF ueber zwei Laeufe, genau wie die Regelvorschlaege es seit
#    2.x machen: Frage hinlegen -> Auftrag anlegen -> beim naechsten Mal die
#    Antwort einsammeln. Die Postwache wartet nie, und die Antwort geht nicht
#    verloren, wenn der Container gerade aus ist.
WERK_FRAGE = "postwache_domains.json"
WERK_ANTWORT = "postwache_domains_antwort.json"
WERK_DECKEL = 120          # so viele Domains je Auftrag, damit er lesbar bleibt
# 🔴 So lange wird dieselbe Frage NICHT wiederholt. Am 27.09. legte der zweite
#    `plan`-Lauf innerhalb einer Minute Auftrag #11 mit genau denselben 120
#    Domains an wie #10 — ein Plan darf oefter laufen, ein Auftragsbuch mit
#    demselben Auftrag dreimal drin ist unbrauchbar. Ein Agent braucht Zeit.
WERK_WIEDERVORLAGE = 12 * 3600


def _werk_ordner() -> str:
    try:
        pfad = pw.konfig().get("werkstatt") or ""
    except Exception:
        return ""
    return pfad if pfad and os.path.isdir(pfad) else ""


def erlaubte_kategorien() -> list:
    """Was als Kategorie zurueckkommen DARF.

    🔑 Die Kategorien aus `REGELN` UND die, die der Besitzer sich selbst angelegt hat
       (aus dem gelernten Wortschatz). Wer nur die eigenen erlaubt, laesst das
       Modell an `Fahrrad` und `Hausbau` vorbeireden.
    """
    aus_regeln = {z.split(".")[0] for z, _, _, _ in REGELN}
    aus_ordnern = set((wortschatz_laden().get("kategorien") or {}).keys())
    return sorted((aus_regeln | aus_ordnern) - {AUFFANG, EIGENE_POST})


def werkstatt_fragen(offen: dict) -> int:
    """Die offenen Domains der Werkstatt hinlegen und einen Auftrag anlegen.

    Gibt die Auftragsnummer zurueck, 0 wenn nichts ging. Uebergeben werden nur
    Domain und Beispiel-BETREFFE — kein Nachrichtentext, keine Adressen von
    Menschen. Derselbe Datenschutz wie bei `pw.uebergeben()`.
    """
    ordner = _werk_ordner()
    if not ordner or not offen:
        return 0
    erlaubt = erlaubte_kategorien()
    namen = sorted(offen, key=lambda d: -len(offen[d]))[:WERK_DECKEL]
    # 🔴 Schon gefragt und noch keine Antwort? Dann warten, nicht noch einmal
    #    fragen. Verglichen wird die MENGE der Domains, nicht die Reihenfolge.
    vorher = os.path.join(ordner, WERK_FRAGE)
    try:
        with open(vorher, encoding="utf-8") as f:
            alt = json.load(f)
        alt_namen = {str(e.get("domain") or "") for e in (alt.get("domains") or [])}
        alter = time.time() - os.path.getmtime(vorher)
        if alt_namen == set(namen) and alter < WERK_WIEDERVORLAGE:
            print("schon gefragt (vor %.0f min) \u2014 es wird auf die Antwort "
                  "gewartet, kein neuer Auftrag" % (alter / 60.0))
            return 0
    except (OSError, ValueError):
        pass
    daten = [{"domain": d,
              "anzahl": len(offen[d]),
              "betreffe": [b[:120] for b in offen[d][:5] if b]} for d in namen]
    pfad = os.path.join(ordner, WERK_FRAGE)
    try:
        tmp = pfad + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"stand": time.strftime("%Y-%m-%dT%H:%M:%S"),
                       "erlaubte_kategorien": erlaubt,
                       "antwort_datei": WERK_ANTWORT,
                       "domains": daten}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, pfad)
        os.chmod(pfad, 0o640)
    except OSError as e:
        print("Werkstatt-Frage nicht schreibbar: %s" % str(e)[:120])
        return 0
    # 🔴 Dieselbe Stausperre wie beim Weckruf fuer unklare Mails: liegen schon
    #    zu viele unbearbeitet, wird die FRAGE hingelegt, aber kein neuer
    #    Auftrag angelegt. Der Agent findet sie, sobald er den offenen abarbeitet.
    stau = pw.auftrag_stau()
    if stau >= pw.MAX_OFFENE_AUFTRAEGE:
        print("Frage hinterlegt, aber KEIN neuer Auftrag: %d Auftraege liegen "
              "unbearbeitet in der Werkstatt" % stau)
        return 0
    nr = pw.escalate(
        "Postwache: %d Absender ohne Kategorie einordnen" % len(daten),
        "In `%s` liegen %d Absender-Domains mit Beispiel-Betreffen.\n\n"
        "Bitte ordne JEDER Domain genau eine Kategorie zu und schreibe das "
        "Ergebnis nach `%s` im selben Ordner:\n\n"
        "```json\n[{\"domain\": \"beispiel.de\", \"kategorie\": \"Reisen\"}]\n```\n\n"
        "ERLAUBTE Kategorien (nur diese, sonst wird die Zeile verworfen):\n%s\n\n"
        "Passt keine, schreibe \"\" als kategorie — dann bleibt die Domain im "
        "Auffang, und das ist besser als eine falsche Schublade.\n\n"
        "Nichts am Postfach tun, nichts scharfschalten: die Postwache holt die "
        "Datei beim naechsten Lauf selbst ab und traegt sie in ihre Landkarte ein."
        % (os.path.join(ordner, WERK_FRAGE), len(daten), WERK_ANTWORT,
           ", ".join(erlaubt)), art="domains")
    print("an die Werkstatt gegeben: %d Domains%s"
          % (len(daten), (" (Auftrag #%d)" % nr) if nr else " (kein Auftrag angelegt)"))
    return nr


def werkstatt_antwort_holen(karte: dict = None) -> int:
    """Die Antwort der Werkstatt einsammeln und in die Landkarte uebernehmen.

    🔴 Jede Zeile wird gegen `erlaubte_kategorien()` geprueft. Eine Antwort aus
       einem Auftragsbuch ist FREMDER TEXT — sie darf keine Kategorie erfinden
       und keinen Ordner bestimmen, den es nicht geben soll.

    Die Datei wird nach dem Einlesen umbenannt, nicht geloescht: wer sie
    loescht, kann hinterher nicht mehr nachsehen, was der Agent gesagt hat.
    """
    ordner = _werk_ordner()
    if not ordner:
        return 0
    pfad = os.path.join(ordner, WERK_ANTWORT)
    if not os.path.exists(pfad):
        return 0
    try:
        with open(pfad, encoding="utf-8") as f:
            roh = json.load(f)
    except (OSError, ValueError) as e:
        print("Werkstatt-Antwort unlesbar: %s" % str(e)[:120])
        return 0
    liste = roh.get("domains") if isinstance(roh, dict) else roh
    if not isinstance(liste, list):
        return 0
    eigene = karte is None
    karte = domain_karte_laden() if eigene else karte
    erlaubt = set(erlaubte_kategorien())
    genommen = verworfen = 0
    for e in liste:
        if not isinstance(e, dict):
            continue
        d = str(e.get("domain") or "").strip().lower()
        kat = str(e.get("kategorie") or "").strip()
        if not d or "." not in d:
            continue
        if not kat:
            continue                     # „passt keine" ist eine gueltige Antwort
        if kat not in erlaubt:
            verworfen += 1
            continue
        karte[d] = {"kategorie": kat, "quelle": "werkstatt"}
        genommen += 1
    if eigene and genommen:
        domain_karte_sichern(karte)
    try:
        os.replace(pfad, pfad + ".erledigt-" + time.strftime("%Y%m%d-%H%M%S"))
    except OSError:
        pass
    print("aus der Werkstatt uebernommen: %d Domains%s"
          % (genommen, (", %d verworfen (unerlaubte Kategorie)" % verworfen)
             if verworfen else ""))
    if genommen:
        pw.log("Umbau: %d Domain-Kategorien aus der Werkstatt uebernommen" % genommen)
    return genommen


def struktur_lernen(inv: dict, mit_ki: bool = True) -> dict:
    """Aus dem Inventar Ordner fuer die Absender ableiten, die keine Regel trifft.

    Gibt {domain: "Kategorie.Marke"} zurueck. Das Ergebnis wird gemerkt, damit
    dieselbe Domain beim naechsten Plan nicht neu beurteilt wird — und damit
    der Besitzer nachlesen kann, WARUM ein Ordner so heisst.
    """
    offen = {}
    for ordner, v in inv["ordner"].items():
        if v["tabu"] or ordner.upper() == "INBOX":
            continue
        for m in v["mails"]:
            ziel, _ = ziel_fuer(m)
            if ziel != AUFFANG:
                continue
            d = (m.get("von") or "").split("@")[-1].lower()
            if not d or "." not in d:
                continue
            offen.setdefault(d, []).append(m.get("betreff") or "")

    karte = domain_karte_laden()
    # 🔴 DIE REIHENFOLGE IST NICHT VERTAUSCHBAR, und zwar aus einem Grund:
    #    Genauigkeit vor Abdeckung.
    #    1. `kategorie_aus_inhalt` — von Hand geschriebene Muster, eng gefasst
    #       und am Bestand gegengemessen („Kfz-Versicherer" -> Versicherungen).
    #    2. `kategorie_aus_wortschatz` — aus dessen eigenen Ordnern GELERNT,
    #       breit, aber statistisch: ein einzelnes haeufiges Wort kann ziehen.
    #    3. das Modell — nur, wenn ein Anbieter synchron antwortet.
    #    Stuende der Wortschatz vorn, wuerde er die geprueften Muster
    #    ueberstimmen; stuende er hinten, kaeme er nie zum Zug, weil die KI
    #    (Anbieter „werkstatt") nie antwortet.
    ws = wortschatz_laden()
    # 1) aus den Betreffen ableiten
    for d, betreffe in offen.items():
        if d in karte:
            continue
        kat = kategorie_aus_inhalt(betreffe)
        if kat:
            karte[d] = {"kategorie": kat, "quelle": "muster"}
    # 2) was uebrig ist: der gelernte Wortschatz
    aus_wort = 0
    for d, betreffe in offen.items():
        if d in karte:
            continue
        kat, abstand = kategorie_aus_wortschatz(betreffe, ws)
        if kat:
            karte[d] = {"kategorie": kat, "quelle": "wortschatz",
                        "abstand": round(abstand, 2)}
            aus_wort += 1
    if aus_wort:
        print("aus dem gelernten Wortschatz: %d Domains" % aus_wort)
    # 3) was DANN noch uebrig ist, dem Modell zeigen
    #
    # 🔑 Genau hier liegt die Grenze des Nachahmens, gemessen am 27.09.: der
    #    Wortschatz holte 45 der 234 offenen Domains, weil er nur Woerter kennt,
    #    die in dessen Ordnern VORKOMMEN. „Please rejoin Test4Theory" oder
    #    „Mafia Wars jetzt auch auf Deutsch" hat er nie einsortiert — dafuer
    #    braucht es Weltwissen, und das hat nur ein Modell.
    rest = {d: b for d, b in offen.items() if d not in karte}
    if mit_ki and rest:
        gefragt, code, grund = ki_kategorien(rest, karte)
        if code == "werkstatt":
            # Der Rundlauf: ERST einsammeln, was beim letzten Mal gefragt wurde,
            # DANN das Uebrige fragen. Umgekehrt fragt man dasselbe zweimal.
            genommen = werkstatt_antwort_holen(karte)
            if genommen:
                rest = {d: b for d, b in offen.items() if d not in karte}
            if rest:
                werkstatt_fragen(rest)
        elif code:
            print("Modell nicht gefragt: %s" % grund)
        elif gefragt:
            print("vom Modell eingeordnet: %d Domains" % gefragt)

    # 3) Ordnernamen bauen — nur ab der Schwelle ein EIGENER Ordner
    #
    # 🔴 UND NIE aus einer Wortschatz-Entscheidung. Gemessen am 27.09. nach dem
    #    ersten scharfen Lauf: der Wortschatz erzeugte `Reisen.Samsung`,
    #    `Kinder.Endomondo`, `Versicherungen.Fastspring`, `Gaming.Highresaudio`.
    #    Der Wortschatz SCHAETZT die Kategorie aus Woertern — er kennt die Marke
    #    nicht. `Reisen.Allgemein` ist eine falsche Schublade und faellt beim
    #    naechsten Verfeinern auf; `Reisen.Samsung` ist ein falsch BENANNTER
    #    Ordner und bleibt fuer immer stehen. Ein geratener Name ist schlimmer
    #    als eine geratene Schublade.
    ziele = {}
    for d, betreffe in offen.items():
        eintrag = karte.get(d)
        if not eintrag:
            continue
        kat = eintrag.get("kategorie") or ""
        if not kat:
            continue
        marke = marke_aus_domain(d)
        darf_eigenen = (eintrag.get("quelle") or "") not in ("wortschatz", "wortschatz-neu")
        if len(betreffe) >= SCHWELLE_EIGENER_ORDNER and marke and darf_eigenen:
            ziele[d] = "%s.%s" % (kat, marke)
        else:
            ziele[d] = "%s.Allgemein" % kat
    domain_karte_sichern(karte)
    return ziele


def ziel_fuer_neue(m: dict, merken: bool = True):
    """Wohin gehoert DIESE eine neue Mail? Fuer den Waechter, nicht fuer den Plan.

    Der Besitzer, 27.09.2026: „wenn neue mails kommen muessen die immer analysiert
    werden und einsortiert werden und wenn es neue ordner braucht dann soll es
    die selbstaendig erstellen."

    🔑 Die Entscheidung wird GEMERKT. `kategorie_aus_inhalt()` sieht bei einer
       einzelnen Mail nur einen Betreff — morgen kaeme derselbe Absender
       vielleicht anders heraus, und dann laege seine Post in zwei Ordnern.
       Einmal entschieden, immer gleich.

    🔴 Ein eigener Ordner je Absender entsteht erst ab `SCHWELLE_EIGENER_ORDNER`
       Mails. Sonst waechst das Postfach um einen Ordner pro Newsletter, den
       der Besitzer einmal bekommt. Bis dahin: `Kategorie.Allgemein`.
       Zusammengelegt wird beim naechsten `plan`/`anwenden`.
    """
    ziel, grund = ziel_fuer(m)
    if ziel != AUFFANG:
        return ziel, grund, False
    dom = (m.get("von") or "").split("@")[-1].lower()
    if not dom or "." not in dom:
        return "", "", False

    karte = domain_karte_laden()
    eintrag = karte.get(dom) or {}
    kat = eintrag.get("kategorie") or ""
    if not kat:
        quelle = "muster-neu"
        kat = kategorie_aus_inhalt([m.get("betreff") or ""])
        if not kat:
            # 🔑 Dieselbe Mittelstufe wie im Plan: was die Muster nicht kennen,
            #    kann der aus dessen Ordnern gelernte Wortschatz oft trotzdem.
            kat, _abstand = kategorie_aus_wortschatz([m.get("betreff") or ""])
            quelle = "wortschatz-neu"
        if not kat:
            return "", "", False
        eintrag = {"kategorie": kat, "quelle": quelle, "anzahl": 0}
    eintrag["anzahl"] = int(eintrag.get("anzahl") or 0) + 1
    marke = marke_aus_domain(dom)
    # 🔴 Dieselbe Grenze wie im Plan: aus dem Wortschatz nie ein Markenordner.
    darf_eigenen = (eintrag.get("quelle") or "") not in ("wortschatz", "wortschatz-neu")
    if marke and darf_eigenen and eintrag["anzahl"] >= SCHWELLE_EIGENER_ORDNER:
        ziel = "%s.%s" % (kat, marke)
    else:
        ziel = "%s.Allgemein" % kat
    if merken:
        karte[dom] = eintrag
        domain_karte_sichern(karte)
    return ziel, "aus dem Inhalt abgeleitet (%s)" % eintrag.get("quelle", "muster"), True


def _zaehle_domains(mails: list) -> dict:
    c = {}
    for m in mails:
        d = (m.get("von") or "").split("@")[-1].lower()
        if d:
            c[d] = c.get(d, 0) + 1
    return c


def plan(grenze: int = 0) -> int:
    quelle = out_pfad("inventar.json.gz")
    if not os.path.exists(quelle):
        print("Kein Inventar. Erst:  umbau.py inventar")
        return 1
    with gzip.open(quelle, "rt", encoding="utf-8") as f:
        inv = json.load(f)

    # Stufe 2a: den Wortschatz aus dessen EIGENEN Ordnern lernen. 🔑 Muss VOR
    # `struktur_lernen` stehen — das fragt ihn.
    stand_schreiben(schritt="plan", laeuft=True, phase="wortschatz",
                    text="Wortschatz wird gelernt")
    ws = wortschatz_lernen(inv)
    _WS_ZWISCHEN["ws"] = ws          # der Zwischenspeicher muss den neuen kennen
    pr = wortschatz_pruefen(inv, ws)
    print("Wortschatz: %d Kategorien, %d Woerter, Kreuzprobe %.0f%% Treffer "
          "bei %.0f%% Abdeckung"
          % (len(ws["kategorien"]),
             sum(len(d) for d in ws["kategorien"].values()),
             100 * pr["treffer"], 100 * pr["abdeckung"]))

    # Stufe 2b: was keine Regel trifft, aus den Inhalten ableiten
    stand_schreiben(schritt="plan", laeuft=True, phase="ableiten",
                    text="Struktur wird abgeleitet")
    abgeleitet = struktur_lernen(inv)
    print("aus den Inhalten abgeleitet: %d Absender-Domains" % len(abgeleitet))

    bewegungen, bleibt, nach_ziel, unsortiert = [], 0, {}, []
    for ordner, v in sorted(inv["ordner"].items()):
        if v["tabu"] or ordner.upper() == "INBOX":
            continue          # Tabu und Posteingang werden nie geraeumt
        for m in v["mails"]:
            ziel, grund = ziel_fuer(m)
            if ziel == AUFFANG:
                d = (m.get("von") or "").split("@")[-1].lower()
                if d in abgeleitet:
                    ziel, grund = abgeleitet[d], "aus dem Inhalt abgeleitet"
            nach_ziel[ziel] = nach_ziel.get(ziel, 0) + 1
            if ziel == AUFFANG:
                unsortiert.append(m)
            if ziel == ordner:
                bleibt += 1
                continue
            bewegungen.append({
                "quelle": ordner, "uid": m["uid"], "uidvalidity": v["uidvalidity"],
                "ziel": ziel, "mid": m["mid"], "von": m["von"],
                "betreff": (m["betreff"] or "")[:120], "datum": m["datum"],
                "grund": grund,
            })
    if grenze:
        bewegungen = bewegungen[:grenze]

    p = {"erstellt": time.strftime("%Y-%m-%dT%H:%M:%S"),
         "inventar": inv["gemessen"],
         "ordner_neu": sorted(nach_ziel),
         "bewegungen": bewegungen}
    ziel_datei = out_pfad("umbau_plan.json.gz")
    with gzip.open(ziel_datei, "wt", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False)
    fp = fingerabdruck(ziel_datei)

    print("=== PLAN ===  %d Bewegungen, %d bleiben liegen, %d Zielordner"
          % (len(bewegungen), bleibt, len(nach_ziel)))
    print("\n%-32s %7s" % ("Zielordner", "Mails"))
    for z in sorted(nach_ziel, key=lambda x: (-nach_ziel[x], x)):
        print("%-32s %7d" % (z, nach_ziel[z]))
    if unsortiert:
        c = {}
        for m in unsortiert:
            d = m["von"].split("@")[-1]
            c[d] = c.get(d, 0) + 1
        print("\n--- groesste unerkannte Absender (Auffang '%s') ---" % AUFFANG)
        for d, n in sorted(c.items(), key=lambda x: -x[1])[:25]:
            print("   %5d  %s" % (n, d))
    stand_schreiben(schritt="plan", laeuft=False, bewegungen=len(bewegungen),
                    bleibt=bleibt, abdruck=fp,
                    wortschatz={"kategorien": len(ws["kategorien"]),
                                "woerter": sum(len(d) for d in ws["kategorien"].values()),
                                "treffer": round(100 * pr["treffer"]),
                                "abdeckung": round(100 * pr["abdeckung"])},
                    baum=sorted(nach_ziel.items(), key=lambda x: (-x[1], x[0])),
                    unerkannt=sorted(
                        _zaehle_domains(unsortiert).items(),
                        key=lambda x: -x[1])[:40],
                    text="%d Bewegungen geplant" % len(bewegungen))
    print("\nPlan: %s\nFingerabdruck: %s" % (ziel_datei, fp))
    print("Anwenden mit:  umbau.py anwenden --freigabe=%s [--grenze=N]" % fp)
    return 0


# ── Stufe 3: anwenden ────────────────────────────────────────────────────────
def fingerabdruck(pfad: str) -> str:
    """Kurzer Abdruck des Plans. Anwenden verlangt ihn als Freigabe — damit
    niemand einen Plan ausfuehrt, den er nicht gelesen hat, und kein alter Plan
    nach einem neuen Inventar noch losgeht."""
    import hashlib
    h = hashlib.sha256(open(pfad, "rb").read()).hexdigest()
    return h[:12]


def journal_schreiben(satz: dict) -> None:
    with open(out_pfad("umbau_journal.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(satz, ensure_ascii=False) + "\n")


def ordner_anlegen(pf, pfad: str) -> str:
    """Ordner an der WURZEL anlegen (Namespace ist ""), danach PRUEFEN, dass er
    in LIST steht. Ein Ordner, den der Server anders benannt hat als gedacht,
    ist die Vorstufe zu Post an einem Ort, den niemand findet."""
    voll = pfad.replace("/", pf.trenner)
    teile = voll.split(pf.trenner)
    for i in range(1, len(teile) + 1):
        zwischen = pf.trenner.join(teile[:i])
        try:
            pf.m.create(pf._zitat(zwischen))
        except Exception:
            pass
        try:
            pf.m.subscribe(pf._zitat(zwischen))
        except Exception:
            pass
    if voll not in pf.ordner_liste():
        raise RuntimeError("Ordner %r entstand nicht unter diesem Namen" % voll)
    return voll


def anwenden(freigabe: str, grenze: int = 0, trocken: bool = False) -> int:
    pfad = out_pfad("umbau_plan.json.gz")
    if not os.path.exists(pfad):
        print("Kein Plan. Erst:  umbau.py plan")
        return 1
    fp = fingerabdruck(pfad)
    if freigabe != fp:
        print("Freigabe passt nicht zum Plan.\n  Plan hat:  %s\n  angegeben: %s"
              % (fp, freigabe or "(nichts)"))
        return 1
    with gzip.open(pfad, "rt", encoding="utf-8") as f:
        p = json.load(f)
    bew = p["bewegungen"]
    if grenze:
        bew = bew[:grenze]
    stand_schreiben(schritt="anwenden", laeuft=True, fortschritt=0,
                    gesamt=len(bew), trocken=bool(trocken), fehler="",
                    phase="verschieben",
                    text="%d Bewegungen%s" % (len(bew), " (trocken)" if trocken else ""))
    print("%d Bewegungen%s%s" % (len(bew), "  [TROCKEN]" if trocken else "",
                                 "  (von %d)" % len(p["bewegungen"]) if grenze else ""))

    nach_quelle = {}
    for b in bew:
        nach_quelle.setdefault(b["quelle"], []).append(b)

    zug = pw.zugang()
    getan = uebersprungen = 0
    with verbindung(zug, schreiben=True) as pf:
        vorhanden = set(pf.ordner_liste())
        gebraucht = sorted({b["ziel"] for b in bew})
        for z in gebraucht:
            if z.replace("/", pf.trenner) not in vorhanden:
                if trocken:
                    print("   wuerde anlegen: %s" % z)
                else:
                    print("   angelegt: %s" % ordner_anlegen(pf, z))
        for quelle, liste in sorted(nach_quelle.items()):
            try:
                typ, _ = pf.m.select(pf._zitat(quelle), readonly=False)
                if typ != "OK":
                    print("   ! %s nicht oeffenbar, %d uebersprungen"
                          % (quelle, len(liste)))
                    uebersprungen += len(liste)
                    continue
                # 🔴 Nummernkreis pruefen: aendert sich UIDVALIDITY, sind alle
                #    gemerkten UIDs wertlos — dann wird NICHT geraten.
                jetzt = pf.uidvalidity(quelle)
                soll = liste[0].get("uidvalidity") or 0
                if soll and jetzt and jetzt != soll:
                    print("   ! %s: UIDVALIDITY %s != %s im Plan — uebersprungen"
                          % (quelle, jetzt, soll))
                    uebersprungen += len(liste)
                    continue

                # 1) Message-Ids der geplanten UIDs holen und abgleichen.
                # 🔴 Der Schluessel ist die Message-Id, nicht die UID. Stimmt sie
                #    nicht, liegt dort eine ANDERE Mail als im Plan — die wird
                #    nicht angefasst.
                nach_mid = {}
                uids = [str(b["uid"]) for b in liste]
                for i in range(0, len(uids), 300):
                    stueck = ",".join(uids[i:i + 300])
                    typ, d = pf.m.uid("fetch", stueck,
                                      "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
                    if typ != "OK":
                        continue
                    letzte = 0
                    for st in (d or []):
                        if isinstance(st, bytes):
                            m2 = re.search(rb"UID (\d+)", st)
                            if m2:
                                letzte = int(m2.group(1))
                            continue
                        if not isinstance(st, tuple) or len(st) < 2:
                            continue
                        m2 = re.search(rb"UID (\d+)", st[0] or b"")
                        u = int(m2.group(1)) if m2 else letzte
                        m3 = re.search(rb"<[^>]+>", st[1] or b"")
                        nach_mid[u] = m3.group(0).decode() if m3 else ""

                # 2) nach Ziel gruppieren, nur was die Message-Id bestaetigt
                je_ziel = {}
                for b in liste:
                    if not mid_bestaetigt(b.get("mid"), nach_mid.get(b["uid"], "")):
                        uebersprungen += 1
                        continue
                    je_ziel.setdefault(b["ziel"], []).append(b)

                # 3) je Ziel als UID-Satz kopieren. Ein IMAP-COPY ueber eine
                #    Menge ist EINE Anweisung: kommt "OK", ist ALLES kopiert —
                #    erst danach wird abgehakt. 17.600 Einzelkopien mit je einem
                #    expunge haetten ueber eine Stunde gebraucht.
                etwas_abgehakt = False
                for ziel, gruppe in sorted(je_ziel.items()):
                    ziel_voll = ziel.replace("/", pf.trenner)
                    for i in range(0, len(gruppe), 200):
                        teil = gruppe[i:i + 200]
                        satz = ",".join(str(b["uid"]) for b in teil)
                        if trocken:
                            getan += len(teil)
                            continue
                        typ, _ = pf.m.uid("copy", satz, pf._zitat(ziel_voll))
                        if typ != "OK":
                            print("   ! Kopie %s -> %s fehlgeschlagen (%d Mails)"
                                  % (quelle, ziel, len(teil)))
                            uebersprungen += len(teil)
                            continue
                        pf.m.uid("store", satz, "+FLAGS", "(\\Deleted)")
                        etwas_abgehakt = True
                        for b in teil:
                            journal_schreiben({
                                "zeit": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                "quelle": quelle, "ziel": ziel_voll,
                                "uid": b["uid"], "mid": b["mid"],
                                "von": b["von"], "betreff": b["betreff"]})
                        getan += len(teil)
                # 4) EIN expunge je Ordner, erst nachdem alle Kopien bestaetigt
                #    sind. Vorher ist keine Mail entfernt — nur markiert.
                if etwas_abgehakt:
                    pf.m.expunge()
                print("   %-28s %5d bewegt" % (quelle, sum(len(g) for g in je_ziel.values())))
                stand_schreiben(fortschritt=getan, text="fertig: %s" % quelle)
            finally:
                try:
                    pf.m.select("INBOX", readonly=False)
                except Exception:
                    pass
        # 🔴 Noch INNERHALB der offenen Verbindung: nach dem Umbau zeigt die
        #    gelernte Ablage auf Ordner, die gerade leer geworden sind.
        if getan and not trocken:
            print(ablage_erneuern(pf))
    stand_schreiben(schritt="anwenden", laeuft=False, fortschritt=getan,
                    uebersprungen=uebersprungen,
                    text="%s: %d bewegt, %d uebersprungen"
                         % ("Trockenlauf" if trocken else "fertig", getan, uebersprungen))
    print("\n%s: %d bewegt, %d uebersprungen"
          % ("TROCKEN" if trocken else "FERTIG", getan, uebersprungen))
    return 0


def zurueck(grenze: int = 0) -> int:
    """Alles aus dem Journal zurueck an seinen Herkunftsort. Das ist der Grund,
    warum jede Bewegung mit Quelle UND Message-Id protokolliert wird."""
    jp = out_pfad("umbau_journal.jsonl")
    if not os.path.exists(jp):
        print("Kein Journal.")
        return 1
    saetze = [json.loads(z) for z in open(jp, encoding="utf-8") if z.strip()]
    saetze.reverse()
    if grenze:
        saetze = saetze[:grenze]
    stand_schreiben(schritt="zurueck", laeuft=True, fortschritt=0,
                    gesamt=len(saetze), text="%d Bewegungen zuruecknehmen" % len(saetze))
    print("%d Bewegungen zuruecknehmen" % len(saetze))
    zug = pw.zugang()
    getan = fehl = 0
    with verbindung(zug, schreiben=True) as pf:
        for s in saetze:
            try:
                pf.m.select(pf._zitat(s["ziel"]), readonly=False)
                typ, d = pf.m.uid("search", None, "HEADER", "Message-Id", s["mid"])
                if typ != "OK" or not d or not d[0]:
                    fehl += 1
                    continue
                for uid in d[0].split():
                    typ, _ = pf.m.uid("copy", uid.decode(), pf._zitat(s["quelle"]))
                    if typ == "OK":
                        pf.m.uid("store", uid.decode(), "+FLAGS", "(\\Deleted)")
                        pf.m.expunge()
                        getan += 1
                    else:
                        fehl += 1
            except Exception as e:
                print("   ! %s: %s" % (s.get("mid", "?")[:40], str(e)[:80]))
                fehl += 1
    # 🔴 Erledigtes Journal beiseitelegen, nicht loeschen. Bliebe es stehen,
    #    zeigte die Seite weiter „N Bewegungen" und ein zweites „Alles
    #    zurueckholen" suchte Mails, die laengst zurueck sind — das sieht wie
    #    ein Fehler aus und ist keiner.
    if getan and not grenze:
        try:
            os.rename(jp, out_pfad("umbau_journal.%s.jsonl"
                                   % time.strftime("%Y%m%d-%H%M%S")))
        except Exception:
            pass
    stand_schreiben(schritt="zurueck", laeuft=False, fortschritt=getan,
                    text="%d zurueckgeholt, %d nicht gefunden" % (getan, fehl))
    print("%d zurueck, %d nicht gefunden" % (getan, fehl))
    return 0


@contextlib.contextmanager
def verbindung(zug: dict, schreiben: bool, versuche: int = 3):
    """Eine offene Verbindung — mit Wiederholung bei einem Netzaussetzer.

    🔴 der Besitzer, 27.09.2026: „manchmal ist das postfach nicht erreichbar."
    Gemessen: der Anbieter nimmt 8 gleichzeitige Verbindungen ohne Murren, es gibt
    also KEINE Abfragesperre. Im Protokoll des Pi stehen aber zwei echte
    Aussetzer („Temporary failure in name resolution", 21. und 23.09.) — ein
    DNS-Hickser von zwei Sekunden darf keinen Lauf von sechs Minuten abbrechen.

    Wartezeit verdoppelt sich (2 s, 4 s). Beim letzten Versuch fliegt der
    Fehler weiter — er gehoert sichtbar in den Stand, nicht verschluckt.
    """
    huelle = offen = None
    for i in range(1, versuche + 1):
        try:
            huelle = pw.Postfach(zug, schreiben=schreiben)
            # 🔴 Das Ergebnis von `__enter__()` weitergeben, nicht die Huelle.
            #    Bei `Postfach` ist beides dasselbe — darauf verlassen sollte
            #    sich ein Kontextmanager trotzdem nicht.
            offen = huelle.__enter__()
            break
        except Exception as e:
            huelle = None
            if i == versuche:
                raise
            wartezeit = 2 ** i
            print("Verbindung fehlgeschlagen (%d/%d): %s — noch einmal in %ds"
                  % (i, versuche, str(e)[:100], wartezeit))
            stand_schreiben(text="Postfach nicht erreichbar, Versuch %d/%d"
                                 % (i, versuche))
            time.sleep(wartezeit)
    try:
        yield offen
    finally:
        try:
            huelle.__exit__(None, None, None)
        except Exception:
            pass


class postfach_gewaehlt:
    """Fuer die Dauer des Blocks gehoert jeder Zustand DIESEM Postfach.

    🔴 `pw.state_pfad()` haengt an der globalen `_PF_ID`, und die setzt allein
    `pw.pf_waehlen()`. Von der Kommandozeile ist sie LEER — dann liest
    `load(ABLAGE)` die globale Datei statt `state/pf/<id>/ablage.json`, findet
    nichts und meldet brav Erfolg fuer nichts. Genau so schlug `ablage_veralten()`
    am 27.09. fehl (Rueckgabe `False`, Landkarte unveraendert).

    🔑 Das ist dieselbe Regel, die `do_POST` fuer die Seite durchsetzt: EINE
    Stelle waehlt das Postfach, vor jeder Aktion. Die eine Aktion, die es
    vergisst, ist genau die, die am falschen Ort arbeitet.
    """

    def __init__(self, pf_id: str = ""):
        self.wunsch = pf_id
        self.vorher = ""

    def __enter__(self):
        self.vorher = getattr(pw, "_PF_ID", "") or ""
        ziel = self.wunsch or self.vorher
        if not ziel:
            faecher = pw.postfaecher()
            ziel = faecher[0]["id"] if faecher else ""
        pw.pf_waehlen(ziel)
        return ziel

    def __exit__(self, *a):
        # Zurueckstellen, nicht leeren: ein Wechsel muss dahin zurueck, wo er
        # herkam (dieselbe Lehre wie beim Ordnerwechsel im IMAP).
        pw.pf_waehlen(self.vorher)


def ablage_erneuern(pf=None) -> str:
    """Die Landkarte des Waechters SOFORT neu lernen.

    🔴 Sie nur fuer ungueltig zu erklaeren reicht nicht: `ablage_frisch()` lernt
    erst im naechsten Lauf, der ueberhaupt Post sieht. Bis dahin stuende die
    alte Karte da — am 27.09. mit 46 Ordnern, von denen 29 nach dem Umbau leer
    waren. Wenn die Verbindung ohnehin offen ist, wird JETZT gelernt.

    Klappt das nicht, wird wenigstens der Merker geloescht (Rueckfall).
    """
    with postfach_gewaehlt():
        if pf is not None:
            try:
                def melden(i, n, name):
                    stand_schreiben(phase="lernen", fortschritt=i, gesamt=n,
                                    text="Landkarte wird neu gelernt: %d/%d (%s)"
                                         % (i, n, name))
                k = pw.ablage_lernen(pf, melden)
                pw.save(pw.ABLAGE, k)
                return "Landkarte neu gelernt: %d Ordner, %d Absender" % (
                    len(k.get("ordner") or {}), len(k.get("absender") or {}))
            except Exception as e:
                print("Neu lernen fehlgeschlagen: %s" % str(e)[:120])
    return "Landkarte fuer ungueltig erklaert" if ablage_veralten() else ""


def ablage_veralten() -> bool:
    """Die gelernte Landkarte des Waechters fuer ungueltig erklaeren.

    🔴 Ohne das sortiert der Waechter nach dem Umbau in Ordner, die es nicht
    mehr gibt: `ablage.json` kannte 46 Ordner, 29 davon waren nach dem Lauf
    leer und werden entfernt. `ablage_frisch()` lernt sonst erst nach 24 h neu
    — bis dahin schlaegt jede Verschiebung fehl. Der Umbau muss die Landkarte
    mitnehmen.
    """
    try:
        with postfach_gewaehlt():
            k = pw.load(pw.ABLAGE, None)
            if not isinstance(k, dict):
                return False
            k["gelernt"] = ""      # naechster Waechterlauf lernt neu
            pw.save(pw.ABLAGE, k)
        return True
    except Exception as e:
        print("Landkarte nicht fuer ungueltig erklaerbar: %s" % str(e)[:120])
        return False


# ── Stufe 4: leere Ordner entfernen ──────────────────────────────────────────
# der Besitzer, 27.09.2026: „was die postwache darf ist ordner die nicht mehr
# gebraucht werden entfernen aber nur wenn darin keine mails mehr sind."
#
# 🔑 „NICHT MEHR GEBRAUCHT" IST NICHT DASSELBE WIE „LEER". Ein Ordner, den
#    der Besitzer selbst angelegt hat und der auf Post wartet (`Github`, `Linkedin`,
#    `Traderepublic`), ist leer — aber gebraucht. Entbehrlich ist ein Ordner,
#    den DER UMBAU geleert hat. Deshalb zwei Klassen, und nur die erste wird
#    ohne Nachfrage entfernt.
#
# 🔴 Das ist die EINZIGE Ausnahme vom Grundsatz „geloescht wird nie" — und sie
#    gilt ausdruecklich nur fuer den ORDNER, nie fuer eine Mail. Die Zahl wird
#    unmittelbar VOR dem Entfernen am Server geholt, nicht aus dem Inventar:
#    zwischen Messung und Entfernen koennte Post angekommen sein.

# Sonderordner erkennt man an ihren FLAGS, nicht am Namen. Ein Postfach auf
# Englisch nennt den Papierkorb anders — die Flagge ist dieselbe.
SONDER_FLAGS = ("Noselect", "Trash", "Junk", "Drafts", "Sent",
                "Archive", "All", "Flagged", "Important")


def _list_mit_flags(pf) -> dict:
    """{Ordnername: Flags} — die Flags brauchen wir, `ordner_liste()` wirft sie weg."""
    raus = {}
    try:
        typ, zeilen = pf.m.list()
    except Exception:
        return raus
    if typ != "OK":
        return raus
    for zl in zeilen or []:
        s = zl.decode("utf-8", "replace") if isinstance(zl, bytes) else str(zl)
        t = re.match(r'\((?P<f>[^)]*)\)\s+"[^"]*"\s+"?(?P<n>.+?)"?\s*$', s)
        if t:
            raus[t.group("n")] = t.group("f")
    return raus


def sonderordner(flags: str) -> bool:
    """Traegt dieser Ordner eine Sonderflagge? Dann nie anfassen."""
    f = (flags or "").lower()
    return any(("\\" + s.lower()) in f for s in SONDER_FLAGS)


def _ordner_leer(pf, name: str):
    """(leer?, Anzahl) — LIVE am Server, nicht aus dem Inventar.

    🔴 Keine Antwort heisst NICHT leer. Wer eine ausbleibende Antwort als
    „nichts drin" liest, entfernt einen Ordner, den er nie gezaehlt hat.
    """
    try:
        typ, d = pf.m.status(pf._zitat(name), "(MESSAGES)")
        if typ != "OK" or not d or not d[0]:
            return False, -1
        roh = d[0] if isinstance(d[0], bytes) else str(d[0]).encode()
        m = re.search(rb"MESSAGES\s+(\d+)", roh)
        if not m:
            return False, -1
        n = int(m.group(1))
        return n == 0, n
    except Exception:
        return False, -1


def ordner_raeumen(trocken: bool = True, auch_vorher_leere: bool = False) -> int:
    """Leere, entbehrliche Ordner entfernen. Ohne `--scharf` passiert nichts.

    🔴 Ein Ordner, auf den eine EIGENE Regel zeigt, bleibt — auch leer. Genau so
    verschwand am 27.09. `Auto.Autohaus-nord`, den der Besitzer selbst angelegt hatte;
    danach zeigte seine Regel ins Nichts, und der Waechter haette neue Post
    dorthin nicht mehr legen koennen. Leer heisst nicht entbehrlich, wenn jemand
    darauf wartet.
    """
    inv = {}
    try:
        with gzip.open(out_pfad("inventar.json.gz"), "rt", encoding="utf-8") as f:
            inv = json.load(f).get("ordner") or {}
    except Exception:
        print("Kein Inventar — erst `umbau.py inventar`. Ohne das weiss niemand, "
              "welcher Ordner frueher Post hatte.")
        return 1
    hatte_post = {o: len(v.get("mails") or []) > 0 for o, v in inv.items()}
    # 🔴 Ein reiner Sammelordner wie `Shopping` hatte NIE eigene Post — nur
    #    seine 24 Kinder. Nach deren Abraeumen ist er genauso entbehrlich.
    #    Deshalb zaehlt auch, ob ein NACHFAHRE Post hatte.
    for o in list(hatte_post):
        if hatte_post[o]:
            for eltern in [o.rsplit(".", i)[0] for i in range(1, o.count(".") + 1)]:
                hatte_post[eltern] = True

    geplant = set()
    try:
        with gzip.open(out_pfad("umbau_plan.json.gz"), "rt", encoding="utf-8") as f:
            for b in json.load(f).get("bewegungen") or []:
                geplant.add(b["ziel"])
    except Exception:
        pass

    zug = pw.zugang()
    weg, behalten = [], []
    stand_schreiben(schritt="ordner", laeuft=True, fortschritt=0, gesamt=0,
                    phase="pruefen", weg=[], behalten=[],
                    text="leere Ordner werden gesucht", fehler="")
    # dessen eigene Ziele, EINMAL gelesen — sie sind tabu wie der Papierkorb.
    eigene_ziele = set(eigene_regeln().values())
    with verbindung(zug, schreiben=not trocken) as pf:
        flaggen = _list_mit_flags(pf)
        kinder = {}
        for n in flaggen:
            eltern = n.rsplit(pf.trenner, 1)[0] if pf.trenner in n else ""
            if eltern:
                kinder.setdefault(eltern, set()).add(n)

        # Tiefste zuerst: erst wenn die Kinder weg sind, ist der Vater kinderlos.
        reihe = sorted(flaggen, key=lambda x: (-x.count(pf.trenner), x))
        for nr, name in enumerate(reihe, 1):
            # 🔴 Der Fortschritt muss beim PRUEFEN laufen, nicht erst beim
            #    Entfernen: die Suche fragt jeden der 168 Ordner einzeln nach
            #    seiner Anzahl. Wer erst beim ersten Treffer meldet, zeigt
            #    minutenlang „0".
            stand_schreiben(phase="pruefen", fortschritt=nr, gesamt=len(reihe),
                            text="geprueft: %d/%d (%s)" % (nr, len(reihe), name))
            grund = ""
            if name.upper() == "INBOX" or tabu(name):
                grund = "tabu"
            elif name in eigene_ziele:
                # 🔴 Darauf zeigt eine EIGENE Regel. Leer heisst nicht
                #    entbehrlich, wenn jemand darauf wartet.
                grund = "Ziel einer eigenen Absenderregel"
            elif sonderordner(flaggen.get(name, "")):
                grund = "Sonderordner (%s)" % (flaggen.get(name) or "").strip()
            elif name in geplant:
                grund = "Ziel im aktuellen Plan"
            elif kinder.get(name):
                grund = "hat noch %d Unterordner" % len(kinder[name])
            elif not hatte_post.get(name, False) and not auch_vorher_leere:
                # 🔴 Ein Ordner in einem ZWEIG, den der Umbau abgeraeumt hat,
                #    darf mit. `Shopping.Amazon` war schon vorher leer — aber
                #    seine 23 Geschwister sind weg, und ein einzelner Rest
                #    haelt den ganzen alten Baum am Leben. Ein Ordner OHNE
                #    Vater (`Github`, `Linkedin`) bleibt: den hat der Besitzer
                #    angelegt und wartet vielleicht auf Post.
                eltern = (name.rsplit(pf.trenner, 1)[0]
                          if pf.trenner in name else "")
                if not (eltern and hatte_post.get(eltern)):
                    grund = ("war schon vorher leer — von dir angelegt, "
                             "nicht vom Umbau")
            else:
                leer, anzahl = _ordner_leer(pf, name)
                if not leer:
                    grund = "nicht leer (%s Mails)" % (anzahl if anzahl >= 0 else "?")
            if grund:
                behalten.append((name, grund))
                continue
            if trocken:
                weg.append(name)
                # 🔴 Auch in der Vorschau mitfuehren, sonst sieht der
                #    Trockenlauf anders aus als der scharfe Lauf: ein Vater
                #    haette dort „noch Unterordner", die in Wahrheit mit
                #    verschwinden.
                eltern = name.rsplit(pf.trenner, 1)[0] if pf.trenner in name else ""
                if eltern and eltern in kinder:
                    kinder[eltern].discard(name)
                continue
            try:
                pf.m.unsubscribe(pf._zitat(name))
            except Exception:
                pass
            typ, _ = pf.m.delete(pf._zitat(name))
            if typ != "OK":
                behalten.append((name, "Server verweigerte das Entfernen"))
                continue
            weg.append(name)
            with open(out_pfad("umbau_ordner_weg.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"zeit": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                     "ordner": name}, ensure_ascii=False) + "\n")
            eltern = name.rsplit(pf.trenner, 1)[0] if pf.trenner in name else ""
            if eltern and eltern in kinder:
                kinder[eltern].discard(name)
            stand_schreiben(phase="entfernen", entfernt=len(weg),
                            text="entfernt: %s (%d)" % (name, len(weg)))

        if weg and not trocken:
            stand_schreiben(phase="lernen", fortschritt=0, gesamt=0,
                            text="Landkarte wird neu gelernt — das dauert Minuten")
            print(ablage_erneuern(pf))

    print("%s: %d Ordner %s" % ("TROCKEN" if trocken else "FERTIG", len(weg),
                                "waeren weg" if trocken else "entfernt"))
    for n in weg:
        print("   - %s" % n)
    # 🔴 Erst zaehlen, dann aufzaehlen — und die erwartbaren Gruende ZULETZT.
    #    Die erste Fassung zeigte 40 Zeilen, alle „Ziel im aktuellen Plan":
    #    genau die Faelle, die man ohnehin erwartet. Warum `Github` bleibt, fiel
    #    hinten raus. Eine Aufstellung, die nur das Erwartete zeigt, ist keine.
    zaehlung = {}
    for _n, g in behalten:
        zaehlung[g] = zaehlung.get(g, 0) + 1
    print("\n%d Ordner bleiben:" % len(behalten))
    for g, n in sorted(zaehlung.items(), key=lambda x: -x[1]):
        print("   %4d x  %s" % (n, g))
    spannend = [(n, g) for n, g in behalten if "Ziel im aktuellen Plan" not in g]
    if spannend:
        print("\n   davon nicht-offensichtlich:")
        for n, g in spannend[:30]:
            print("   %-28s %s" % (n, g))
    stand_schreiben(schritt="ordner", laeuft=False, fortschritt=len(weg),
                    weg=weg, behalten=behalten[:40],
                    text="%s: %d Ordner %s" % ("Trockenlauf" if trocken else "fertig",
                                               len(weg),
                                               "waeren weg" if trocken else "entfernt"))
    return 0


def wortschatz_zeigen() -> int:
    """Lernen, gegenmessen, zeigen — ohne etwas am Postfach zu tun.

    🔑 Die Kreuzprobe ist der Punkt. Ein Einordner, der seine Trefferquote nicht
       nennt, ist eine Behauptung.
    """
    quelle = out_pfad("inventar.json.gz")
    if not os.path.exists(quelle):
        print("Kein Inventar. Erst:  umbau.py inventar")
        return 1
    with gzip.open(quelle, "rt", encoding="utf-8") as f:
        inv = json.load(f)
    t0 = time.time()
    ws = wortschatz_lernen(inv)
    _WS_ZWISCHEN["ws"] = ws
    pr = wortschatz_pruefen(inv, ws)
    print("=== WORTSCHATZ ===  gelernt aus %d Ordnern in %.1fs"
          % (len(inv.get("ordner") or {}), time.time() - t0))
    print("%d Kategorien, %d Woerter" % (len(ws["kategorien"]),
                                         sum(len(d) for d in ws["kategorien"].values())))
    print("Kreuzprobe: %d richtig, %d falsch, %d ohne Urteil "
          "\u2192 %.1f%% Treffer bei %.1f%% Abdeckung"
          % (pr["richtig"], pr["falsch"], pr["offen"],
             100 * pr["treffer"], 100 * pr["abdeckung"]))
    print("\n%-16s %7s %7s   kennzeichnende Woerter" % ("Kategorie", "Mails", "Woerter"))
    for kat in sorted(ws["kategorien"], key=lambda k: -ws["mails"].get(k, 0)):
        d = ws["kategorien"][kat]
        summe = sum(d.values()) or 1
        # Kennzeichnend ist nicht das HAEUFIGSTE Wort, sondern das, das in
        # DIESER Kategorie ueberproportional oft steht.
        andere = {}
        for k2, d2 in ws["kategorien"].items():
            if k2 == kat:
                continue
            for w, n in d2.items():
                andere[w] = andere.get(w, 0) + n
        gesamt_andere = sum(andere.values()) or 1
        punkte = sorted(d.items(),
                        key=lambda x: -((x[1] / summe) / ((andere.get(x[0], 0) / gesamt_andere)
                                                          + 1e-9)) * (x[1] >= 3))
        print("%-16s %7d %7d   %s"
              % (kat[:16], ws["mails"].get(kat, 0), len(d),
                 ", ".join(w for w, _ in punkte[:8])))
    if pr["daneben"]:
        print("\n--- haeufigste Verwechslungen (soll \u2192 gesagt) ---")
        for (soll, sagt), n in pr["daneben"]:
            print("   %5d  %s \u2192 %s" % (n, soll, sagt))
    stand_schreiben(schritt="wortschatz", laeuft=False,
                    wortschatz={"kategorien": len(ws["kategorien"]),
                                "woerter": sum(len(d) for d in ws["kategorien"].values()),
                                "treffer": round(100 * pr["treffer"]),
                                "abdeckung": round(100 * pr["abdeckung"])},
                    text="Wortschatz: %.0f%% Treffer bei %.0f%% Abdeckung"
                         % (100 * pr["treffer"], 100 * pr["abdeckung"]))
    return 0


def main(argv) -> int:
    befehle = ("inventar", "plan", "anwenden", "zurueck", "ordner", "wortschatz")
    if len(argv) < 2 or argv[1] not in befehle:
        print(__doc__)
        print("Aufruf:")
        print("  umbau.py inventar [Ordnerfilter] [--voll]")
        print("  umbau.py plan [--grenze=N]")
        print("  umbau.py anwenden --freigabe=<abdruck> [--grenze=N] [--trocken]")
        print("  umbau.py zurueck [--grenze=N]")
        print("  umbau.py ordner [--scharf] [--auch-vorher-leere]")
        print("  umbau.py wortschatz")
        return 2
    args = {}
    for a in argv[2:]:
        if a.startswith("--"):
            k, _, v = a[2:].partition("=")
            args[k] = v or "1"
    grenze = int(args.get("grenze", 0) or 0)
    if argv[1] == "inventar":
        return inventar(argv[2] if len(argv) > 2 and not argv[2].startswith("--") else "",
                        voll="voll" in args)
    if argv[1] == "plan":
        return plan(grenze)
    if argv[1] == "anwenden":
        return anwenden(args.get("freigabe", ""), grenze, "trocken" in args)
    if argv[1] == "zurueck":
        return zurueck(grenze)
    if argv[1] == "wortschatz":
        return wortschatz_zeigen()
    if argv[1] == "ordner":
        # 🔴 Vorgabe ist TROCKEN. Wer Ordner entfernen will, sagt es ausdruecklich.
        return ordner_raeumen(trocken="scharf" not in args,
                              auch_vorher_leere="auch-vorher-leere" in args)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
