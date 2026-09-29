#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Postwache — restructuring: reorder the whole mailbox.

The task: restructure an entire mailbox
completely; it should analyse every mail and sort it into a sensible folder
structure, never delete mail but only move it within the mailbox; the current
structure is silly, and archived mail should be sorted too and taken out of the
archive.

This is **phase 2**, which has been in the plan since 2026-09-11: the existing
folders are not perfect either, and sorting them afresh is what the agent is
for. Phase 1 has already measured the weaknesses of the filing.

🔑 THREE STAGES, SEPARATE AND IN THIS ORDER
   1. `inventar`  — reads EVERY mail header. Read-only, `BODY.PEEK`.
   2. `plan`      — computes a proposal from the inventory. Touches no mailbox.
   3. `anwenden`  — moves, but only what stands in the approved plan.

   Mix the stages and you move on suspicion. The plan is a file you can read
   BEFORE a single mail travels.

🔴 NOTHING IS EVER DELETED. There is not one call in this file that removes a
   mail: `bewegen()` copies first, ticks off only after a CONFIRMED copy and
   writes every move into the journal. If the copy fails, the mail is left
   untouched.

🔴 THE KEY IS THE MESSAGE-ID, NOT THE UID. A UID is only valid together with
   its folder AND `UIDVALIDITY`. Before every move the Message-Id of the UID is
   checked against the plan — otherwise, after a renumbering, the wrong mail
   travels (the same lesson as with the documents, 2.6.0).
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

# ── What the restructuring NEVER touches ──────────────────────────
# Sent items are his own record, drafts are unfinished, trash and spam are
# deliberate. A sorter that reaches in here destroys evidence instead of creating
# order.
TABU = {
    "sent items", "sent", "gesendet", "gesendete objekte",
    "drafts", "entwuerfe", "entwürfe",
    "trash", "papierkorb", "spam", "junk",
}

KOPFZEILEN = ("FROM TO SUBJECT DATE MESSAGE-ID LIST-ID LIST-UNSUBSCRIBE "
              "X-MAILER REPLY-TO")

# 🔴 The version of the READER, not of the file. The inventory carries
#    unchanged folders over from the last run — but the fingerprint from
#    UIDVALIDITY/MESSAGES/UIDNEXT only says that the POST has not changed, not
#    that we READ it the same way. On 2026-09-27 the sender split was repaired
#    (11 Booking mails had no recognisable address); without this number the
#    inventory would have kept using the old, wrong values and the repair would
#    have stayed invisible — a run that reports success and keeps the old state.
#    Whoever changes `kopf_saetze()` raises it.
LESER_FASSUNG = 2


def tabu(name: str) -> bool:
    """Taboo applies to the folder AND everything below it."""
    n = name.lower()
    return any(n == t or n.startswith(t + ".") for t in TABU)


def out_pfad(name: str) -> str:
    """Where the restructuring puts its files.

    🔴 2026-09-27: a fixed path with a fallback to the program folder stood here —
    `POSTWACHE_HOME` was ignored. In the demo all the new cards therefore stayed
    empty although the files had been written: the watchman wrote into
    `$POSTWACHE_HOME/out`, the restructuring read in the program folder. Two
    places that determine the same location differently are one too many —
    `pw.OUT` applies, and that one knows the environment variable.
    """
    return os.path.join(pw.OUT, name)


# ── Status for the page ────────────────────────────────────────
# 🔑 A run over 17,600 mails takes minutes. Without a status he sits in front
#    of a page that says nothing — and presses again. So every stage writes down
#    where it stands, and the page reads only this file.
def stand_schreiben(**felder) -> None:
    d = stand_lesen()
    # 🔑 Remember the start: without a runtime, every longer stage looks like a
    #    hanging service. With „running for 3:20“, waiting is just waiting.
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
    """All headers of a folder. Read-only, BODY.PEEK, in blocks.

    🔴 The `finally` returns to INBOX. Without it the server stays on the folder
    last read and every following fetch grasps at nothing — exactly that mistake
    reported success on 2026-09-11 and read nothing.
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
                # 🔴 Through `pw.absender_teile()`: it knows the fallback for
                # encoded display names (11 Booking mails, 2026-09-27).
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
                    # `absender_teile()` already decodes the name.
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
    """(uidvalidity, count, uidnext) in ONE query.

    🔑 If none of the three values changes, the folder is unchanged: UIDNEXT
    grows with every new mail and never shrinks. If one was deleted and a new one
    added, the COUNT stays the same — but UIDNEXT is higher. The three together
    are therefore a reliable fingerprint, and it costs a single round trip instead
    of SELECT + SEARCH + FETCH.
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
                return None            # incomplete = no fingerprint
            werte.append(int(m.group(1)))
        return tuple(werte)
    except Exception:
        return None


def inventar(nur: str = "", voll: bool = False) -> int:
    zug = pw.zugang()
    t0 = time.time()
    # What was in it last time — from that, everything that has not changed is
    # carried over. 🔴 Measured: after the restructuring the mailbox has
    # 132 folders instead of 53, and a full inventory therefore took 7 minutes
    # instead of 27 seconds. The same mails, only spread more finely — the cost
    # sits in the folder, not in the post.
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
                pass  # the inbox is counted, but never cleared
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


# ── Stage 2: the target tree ────────────────────────────────────
# 🔑 Every rule stands here with its MEASURED number (inventory 2026-09-27).
#    There are no invented folders — where no post lies, no folder appears.
#
# 🔑 THE ORDER IS THE PRECEDENCE. The first matching rule wins.
#
# 🔴 SEMANTICS, so they do not have to be guessed:
#    If senders/domains AND a subject pattern are set, BOTH must match. If only
#    one is set, that one decides alone. Whoever needs „or“ writes TWO rules — a
#    broad pattern like `konto` joined by „or“ would otherwise suck in foreign
#    post that has its home three rules further down.
#
# 🔴 FOR THE ARCHIVE THE SUBJECT DECIDES, NOT THE SENDER. 6,954 archive mails
#    carry his own address in `From`, because the DiskStation sent through his
#    Gmail account. That is why the device rules stand RIGHT AT THE FRONT.

# ── Senders that offer EVERYTHING ──────────────────────────────
# 🔴 This already stood in phase 1 (2026-09-11) and I built it anyway:
#    „The main domain brings 8 hits and 7 misses — a coin toss. `check24.de` does
#     hotels AND insurance, `deutschepost.de` delivers parcels AND the tax
#     newsletter.“ The main domain has been allowed to SUGGEST ever since, but not
#     to ACT — my rule let it act.
#
# 🔑 The information has been there all along, just one level deeper: the trip
#    came from `info@hotel.check24.de` („Buchungsbestaetigung Wolin“), the
#    insurance from `e-scooter-versicherung@check24.de`, the security code from
#    `kundenkonto@check24.de`. The SUBDOMAIN and the part BEFORE the @ say it.
#
# Per domain: which category leads where, and where to when nothing matches.
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

# What the fine-grained view decides by. The WHOLE address is read (part before
# the @, subdomain) AND the subject — the order is the precedence.
# 🔴 Insurance BEFORE car: „Kfz-Versicherung“ is both, and the first is meant.
#    And travel BEFORE shopping: a „Buchungsbestaetigung“ is not an order.
FEIN_MUSTER = [
    ("Konten", r"kundenkonto|kundenbereich|\blogin\b|passwort|sicherheitscode|"
               r"anmeldung|zugangsdaten|zwei-faktor"),
    ("Reisen", r"hotel|reise|\bflug|unterkunft|ferienwohnung|urlaub|"
               r"buchungsbestaetigung|eingangsbestaetigung ihrer buchung"),
    # 🔴 The STEM, not the word: „Kfz-Versicherer“ contains no
    #    „Versicherung“ — two mails landed in `Auto.Werkstatt` because of that.
    ("Versicherungen", r"versicher|\bpolice\b|\bevb\b|haftpflicht|"
                       r"\btarif\b|antragsnummer"),
    ("Auto", r"\bkfz\b|fahrzeug|zulassung|kennzeichen|werkstatt"),
    ("Steuer", r"steuer|elster|finanzamt"),
    ("Einkauf", r"bestellung|lieferung|versand|paket|sendung|rechnung"),
]


def fein_entscheiden(von: str, betreff: str):
    """With an ambiguous sender the domain does not decide.

    The WHOLE address plus the subject is read. If nothing matches, the domain's
    fallback applies — there is no guessing here.
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
    # The part before the @ and the subdomain are part of the evidence.
    heu = pw.normal(von.replace("@", " ").replace(".", " ") + " " + (betreff or ""))
    for kat, rx in FEIN_MUSTER:
        if kat in karte and _passt_rx(rx, heu):
            return karte[kat], "mehrdeutiger Absender — Adresse und Betreff entscheiden"
    return karte.get("_rueckfall", ""), "mehrdeutiger Absender — Rueckfall"


# 🔴 EMPTY in the source. The owner's own addresses live in
#    `state/regeln_eigen.json` under „eigene_adressen“ — they stood here until
#    2026-09-27, and therefore in every image and every repository.
EIGEN = ()

# 🔴 His OWN old post (863 mails 2007-2022, addressed to others) does not
#    belong spread across topic folders — it is one block and stays one. It is
#    DELIBERATELY not put into `Sent Items`: that is his current record and
#    taboo.
EIGENE_POST = "Eigene Post Archiv"

# Circulars that no rule has matched. Recognised by `List-Id` /
# `List-Unsubscribe` — the same feature by which `einordnen()` has recognised
# newsletters since 1.0. Without this stage 600 individual pieces lie in the
# catch folder that are all the same thing.
NEWSLETTER = "Newsletter"

# What no rule matches. He should SEE what was not recognised — a silent
# remainder is worse than a visible one.
AUFFANG = "Unsortiert"

# (Ziel, Absender exakt, Domain-Endungen, Betreffmuster)
REGELN = [
    # 🔴 HIS PERSONAL PART STOOD HERE — 20 rules with the real addresses of
    #    family, bank adviser, architect, tradespeople and employer. On
    #    2026-09-27 exactly that kind of data stood in the PUBLIC repository (the
    #    name and mail address of a real person, in all three commits). The
    #    repository was deleted and rebuilt.
    #
    # 🔑 They now live in `state/regeln_eigen.json` (0600, on NO rollout list)
    #    and are asked by `eigen_laden()` BEFORE these rules. What nobody can
    #    publish, nobody has to filter out. The file layout is documented at
    #    `eigen_laden()`.
    #
    #    What stands here applies to EVERYONE: PayPal, Amazon, Telekom,
    #    authorities. Whoever enters a rule with a real name here makes the same
    #    mistake all over again.

    # ── Device reports: the key to the archive (~6,900 mails) ───────────
    ("Technik.Synology", (), ("synology.com", "synologynotification.com"), r""),
    ("Technik.Netzwerk", (), (), r"fritz!?|powerline|heimnetz|portfreigaben|"
     r"wlan-gastzugang|archer c\d|änderungsnotiz|internet-adresse|"
     r"communication (establish|lost)|self test (start|end)"),
    # `sponionpi` is not a provider but the HOSTNAME of a Pi — senders like that
    # have no domain, only a machine name.
    ("Technik.Netzwerk", (), ("avm.de", "no-ip.com", "sponionpi"), r""),

    # ── House building: the owner's building project ──────
    ("Hausbau.Statik", (), ("heinze-statik.de",), r""),
    # 🔴 The bank adviser BEFORE the banking rule: her post is house building,
    #    not banking. Hence address AND pattern.

    # ── Banking ─────────────────────────────────────────────────────────────
    ("Banking.DKB", (), ("dkb.de",), r""),
    ("Banking.PayPal", (), ("paypal.de", "paypal.com"), r""),
    ("Banking.Krypto", (), ("bitvavo.com", "kraken.com"), r""),

    # ── Versicherung ────────────────────────────────────────────────────────
    ("Versicherungen.PlusCard", (), ("pluscard.de",), r""),
    ("Versicherungen.Allgemein", (), ("check24.de", "verti.de", "valuenet.de"), r""),

    # ── Auto ────────────────────────────────────────────────────────────────
    ("Auto.KIA", (), ("kia.com", "kia.de", "m8mit.de"), r""),

    # ── Accounts (security/login) BEFORE shopping (receipts) ─────────────
    # 🔴 Apple twice: security post is something different from a purchase
    #    receipt. The NARROW rule has to stand first, otherwise it eats all 761
    #    receipts with it.
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

    # ── People: only those who really write often ────────────────────

    # ── Mobility & car (second rule set, learned from the catch folder) ───
    # `m8mit.de` (120 mails) are the KIA service records — they all lay in
    # `Shopping.KIA`, but the name does not give it away.
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

    # ── Children ───────────────────────────────────────────────
    # 33 of 34 lay in `Shopping.Rechnungen Hort` — childcare invoices.
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

    # ── Shopping, whatever has no folder of its own above ──────────────
    ("Einkauf.Allgemein", (), ("lidl.de", "mediamarkt.de", "conrad.de",
                               "idealo.com", "de.idealo.com", "asgoodasnew.com",
                               "nike.com", "official.nike.com", "skatepro.de",
                               "elektrovorteil.de", "akademische.de", "aubu.de",
                               "digitalriver.com", "mous.co", "saturn.de",
                               "otto.de", "zalando.de"), r""),
    ("Gaming", (), ("playstation.com", "txn-email03.playstation.com", "ubi.com",
                    "xsolla.com", "gog.com", "nintendo.com"), r""),
    ("Steuer", (), ("wolterskluwer.com", "elster.de", "buhl.de"), r""),
    # Looking for a flat or a house belongs to the house — his rule: everything
    # to do with the house goes under Hausbau.
    ("Hausbau.Suche", (), ("immobilienscout24.de", "immowelt.de"), r""),
    ("Telekommunikation.Kabel", (), ("kabeldeutschland.de", "unitymedia.de"), r""),

    # 🔴 His OWN addresses have to stand HERE — before `Menschen.Weitere`. At
    #    the first measurement „Eigene Post Archiv“ fell from 963 to 14, because
    #    `gmail.com` in the people rule took his 863 own sent mails with it. And
    #    they must NOT stand further up: 6,954 device reports carry the same
    #    address and belong under `Technik.*`.
    # 🔑 Stays here as a PLACEHOLDER: the own addresses come at runtime from
    #    `eigen_laden()` (see `ziel_fuer`), only the target folder stands here.

    # ── People without a folder of their own: consumer mail providers ─────
    # 🔴 Deliberately LATE: only when no company, no service and no device has
    #    matched is a gmail/gmx/web.de sender likely to be a human. Further up,
    #    this rule would take company post with it that happens to sit at a free
    #    mail provider.
    ("Menschen.Weitere", (), ("gmail.com", "googlemail.com", "gmx.de", "gmx.net",
                              "posteo.de",
                              "web.de", "hotmail.com", "hotmail.de", "freenet.de",
                              "me.com", "icloud.com", "t-online.de", "arcor.de",
                              "yahoo.de", "aol.com", "mail.ru"), r""),

    # ── Late patterns: they only fire when nothing above matched ────────
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
    """May this UID be moved?

    🔴 ONLY if the Message-Id was FOUND at the location AND matches. An EMPTY
    result is not a confirmation but the normal case for „the UID no longer
    exists“ — during the proof on 2026-09-27 `anwenden` reported „3 moved“
    although it had moved nothing: after a `zurueck` the mails were back under
    NEW UIDs, the old UID returned no header, and the check let that pass.
    """
    soll = (soll or "").strip()
    ist = (ist or "").strip()
    if not ist:
        return False          # nothing found = not confirmed
    if not soll:
        return False          # without a plan id there is nothing to confirm
    return soll == ist


def eigene_regeln() -> dict:
    """His OWN sender rules from the settings. {address: folder}

    🔴 2026-09-27, learned the expensive way: the restructuring removed
    `Auto.Autohaus-nord` because it considered it empty and dispensable — when in
    fact he had created it himself and set a rule
    `j.haller@autohaus-nord.example -> Auto.Autohaus-nord`. The plan pushed its
    two mails to `Auto.Allgemein`, the folder became empty, and the cleanup took
    it with it. After that his rule pointed at a folder that no longer existed.

    🔑 HIS OWN ASSIGNMENT BEATS EVERYTHING. That has held in the watchman since
       2.3.0 (`absender_regel()` before the learned filing) — the restructuring
       simply did not know it. A second place that answers the same question
       differently is one too many.
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


# ── The personal part of the catalogue does NOT belong in the source ──────
# 🔴 2026-09-27, the most expensive finding of the day: the PUBLIC repository
#    carried, in all three commits, the name and mail address of a real person
#    (`k.ivanov@autogruppe.example`, from an example in a documentation line),
#    plus device names and the mail provider. The repository was deleted and
#    rebuilt.
#
#    Scrubbing at publish time was the wrong answer: it is a NET, not a wall.
#    20 of 74 rules carried real addresses — family, bank adviser, architect,
#    tradespeople, employer. As long as those stand in the source, a regular
#    expression decides whether they become public.
#
# 🔑 THE WALL: the personal part lives in `state/regeln_eigen.json` — the same
#    drawer as the credentials, 0600, on NO rollout list. Only rules that apply
#    to everyone remain in the source (PayPal, Amazon, Telekom). What nobody can
#    publish, nobody has to filter out.
EIGEN_DATEI = "regeln_eigen.json"


def eigen_laden() -> dict:
    """The personal catalogue of this installation. If it is absent, it is empty.

    Layout:
      {"eigene_adressen": ["..."],
       "regeln": [["Target", ["address", ...], ["domain", ...], "pattern"], ...]}

    🔴 Empty is a valid state, not an error: a fresh installation has no
       personal part, and the watchman still has to run.
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
            # 🔴 Every entry carries its original POSITION in the catalogue.
            #    Without it, all personal rules stood at the front on the first
            #    attempt — which put „Eigene Post Archiv“ BEFORE the device rules,
            #    and 823 FRITZ! reports from his own address landed in the archive
            #    instead of under Technik. The order IS the logic.
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
    """Personal and general part in their ORIGINAL order.

    🔑 The personal rules return to their remembered position — between the
       general ones, not in front of them. „My bank adviser“ has to stand before
       „some bank“, but the device rules before „my own post“. Both at once is
       only possible via the position.
    """
    if "katalog_voll" in _EIGEN_ZWISCHEN:
        return _EIGEN_ZWISCHEN["katalog_voll"]
    eigen = eigen_laden()["regeln"]
    nach_nr = {e["nr"]: e["regel"] for e in eigen}
    rest = list(REGELN)
    raus = []
    # 🔴 The range has to hold the HIGHEST position, not just the sum of the
    #    lengths. Otherwise a rule with a high number silently dropped out — that
    #    is exactly how „Eigene Post Archiv“ (position 68) vanished from a
    #    catalogue of 60 entries in the test bench, and nobody would have noticed.
    hoechste = max(nach_nr) if nach_nr else -1
    for i in range(max(hoechste + 1, len(eigen) + len(REGELN))):
        if i in nach_nr:
            raus.append(nach_nr[i])
        elif rest:
            raus.append(rest.pop(0))
    raus.extend(rest)          # whatever is left over is never lost
    # 🔴 A counter-check in operation: no rule may go missing while interleaving.
    if len(raus) != len(eigen) + len(REGELN):
        pw.log("Katalog unvollstaendig: %d statt %d Regeln"
               % (len(raus), len(eigen) + len(REGELN)))
    _EIGEN_ZWISCHEN["katalog_voll"] = raus
    return raus


_EIGEN_ZWISCHEN = {}


def ziel_fuer(m: dict):
    """The first matching rule wins. Returns (target, reason).

    🔑 RIGHT AT THE FRONT stand his own sender rules. They are his decision, not
       my derivation — nothing may override them.
    """
    von = (m.get("von") or "").lower()
    if "absender" not in _EIGEN_ZWISCHEN:
        _EIGEN_ZWISCHEN["absender"] = eigene_regeln()
    eigen = _EIGEN_ZWISCHEN["absender"].get(von)
    if eigen:
        return eigen, "deine eigene Regel"
    dom = von.split("@")[-1]
    # The subject is normalised (lower case, umlaut and transcription alike) —
    # the same function that fixed the umlaut bug at its root on 2026-09-11.
    heu = pw.normal(m.get("betreff") or "")
    roh = (m.get("betreff") or "").lower()
    # 🔑 The PERSONAL catalogue first, then the general one. It is more precise:
    #    „my bank adviser“ beats „some bank“. And it lives in `state/`, not in the
    #    source — see `eigen_laden()`.
    for ziel, adressen, domains, rx in katalog():
        # The placeholder for his own post gets its addresses at runtime — in
        # the source there is an empty tuple.
        #
        # 🔴 2026-09-27: the state file carried the SAME list twice — as
        #    `eigene_adressen` AND inside the rule itself. Routing went by the
        #    rule, so a newly entered own address had no effect: no error, no
        #    trace, just one mail in the catch folder. So now the UNION of both
        #    lists applies — a second list can no longer override the first.
        #    `sorted`, so that the order does not depend on the set.
        if ziel == EIGENE_POST:
            adressen = tuple(sorted(set(adressen or ())
                                    | set(eigen_laden()["eigene_adressen"])))
        adr_tr = bool(adressen) and von in adressen
        dom_tr = bool(domains) and any(dom == d or dom.endswith("." + d)
                                       for d in domains)
        rx_tr = bool(rx) and (_passt_rx(rx, heu) or _passt_rx(rx, roh))
        hat_wer = bool(adressen) or bool(domains)
        if hat_wer and rx:
            if (adr_tr or dom_tr) and rx_tr:
                return ziel, "Absender+Betreff"
        elif hat_wer:
            # 🔴 A rule on the exact ADDRESS stays untouched — it is already as
            #    precise as it gets. Only where the DOMAIN pulls is it worth
            #    looking at whether this sender sends all sorts of things.
            if adr_tr:
                return ziel, "Absender %s" % von
            if dom_tr:
                fein, warum = fein_entscheiden(von, m.get("betreff") or "")
                if fein:
                    return fein, warum
                return ziel, "Absender %s" % dom
        elif rx and rx_tr:
            return ziel, "Betreff"
    # 🔴 Only AFTER all rules: a sender with a folder of their own should land
    #    there, even when they send their post as a circular.
    if m.get("liste"):
        return NEWSLETTER, "List-Id"
    return AUFFANG, ""


# ── Stage 2b: derive structure FROM THE CONTENT ────────────────────
# The task: analyse and sort all mails and build the new
# folder structure based on the mail contents.
#
# 🔑 The rules above cover what he named EXPLICITLY (banking, house building)
#    and what is measurably large. Everything else the restructuring should work
#    out BY ITSELF — otherwise every new company has to be entered by hand and
#    the catch folder quietly grows along.
#
# 🔴 Only a folder NAME is derived, never a move without a folder. Where the
#    derivation recognises nothing, the catch folder stands — a wrongly named
#    folder is worse than a visible remainder.

# How many mails a sender needs before a folder of their own appears. Below that
# the folder is not worth it: it costs a click and saves none.
SCHWELLE_EIGENER_ORDNER = 4

# What the subject says about the kind of post. Order = precedence.
# The patterns are ASCII — `normal()` turns umlauts into ae/oe/ue beforehand.
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
    """`meine.steuertipps.de` becomes `Steuertipps`.

    🔴 The BRAND is not at the front. `news.miele.de`, `mail.anthropic.com`,
    `txn-email03.playstation.com` — take the first piece and you create folders
    called „News“, „Mail“ and „Txn-email03“. What is taken is the piece BEFORE
    the public suffix, and multi-part suffixes (`co.uk`) count as one.
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
    # Technical prefixes that are not a brand
    for weg in ("mail", "email", "e", "news", "newsletter", "info", "no-reply",
                "noreply", "smtp", "mx", "web", "my", "meine", "mein"):
        if kern == weg and len(teile) >= 3:
            kern = teile[-3]
    kern = re.sub(r"[^a-z0-9äöüß-]", "", kern)
    if not kern or len(kern) < 2:
        return ""
    return kern[:1].upper() + kern[1:]


def kategorie_aus_inhalt(betreffe: list) -> str:
    """Which top-level drawer fits THESE subjects?

    Counting happens; it does not stop at the first hit: a sender sends orders AND
    newsletters, and then the majority decides, not whichever mail you happen to
    read first.
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
    # A majority has to be one: with a single hit among many mails that is
    # guessing, not recognising.
    if punkte[best] * 3 < len(betreffe):
        return ""
    return best


# ── The vocabulary: learning from his OWN folders ───────────────────
# Why is the catch folder not being sorted? These are clear mails with clear
# content that could be filed beautifully; the app has to recognise these
# properly too — and not by a person but always by the Postwache,
# which uses AI, doesn't it?
#
# Measured against the 489 mails in the catch folder: 234 different sender
# domains, 202 of them with fewer than 4 mails. One rule per domain would be a
# list written by me — exactly what he does NOT want, and it would be too short
# again tomorrow. The patterns (`INHALT_MUSTER`) did NOT match for 14 of the 15
# biggest senders, because they are built on invoice and contract words.
#
# 🔑 But the Postwache has something far better than any list: 17,500 mails that
#    he sorted into 132 folders HIMSELF. That is labelled teaching material. From
#    it one can learn which WORDS belong to which category — and an unknown
#    Booking mail then lands under „Reisen“, because „hotel“, „buchung“ and
#    „check“ cluster there. That is exactly the Postwache's principle, only
#    applied to the subject instead of the sender: IT CAN ONLY IMITATE.
WORTSCHATZ = "umbau_wortschatz.json"
WORT_MIN_TREFFER = 2      # this many known words a subject must have
# 🔑 MEASURED on 2026-09-27 against the whole stock (16,000 mails,
#    cross-check):
#      margin 1.6 -> 93.9 % right at 78.0 % coverage, 770 mistakes
#      margin 2.5 -> 98.3 % right at 69.4 % coverage, 190 mistakes
#      margin 3.5 -> 99.0 % right at 65.4 % coverage, 103 mistakes
#    2.5 is the knee: three quarters of the mistakes gone for nine points of
#    coverage. Above that it gets expensive and brings almost nothing. 🔴 A wrong
#    filing costs more than an open one: in the catch folder he SEES that
#    something is pending — in the wrong drawer he sees nothing.
WORT_ABSTAND = 2.5        # this far the first must be ahead of the second
# These folders do NOT teach: the catch folder is the question itself, and his own
# sent post carries the subjects of ALL categories (63 % of the archive) — it
# would connect every category with every word.
KEIN_WORTLEHRER = ("Unsortiert", "Eigene Post Archiv")

# Filler words. 🔴 A stop-word list is LANGUAGE, not logic — it belongs next to
# the patterns and not in a language file: it is never displayed.
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
    """Split a subject into comparable words.

    `pw.normal()` lower-cases and resolves umlauts — the same transcription the
    patterns use, so that „Rueckfrage“ and „Rückfrage“ are the same word. Pure
    numbers drop out: an order number is not a word, it occurs exactly once and
    would be equally worthless for every category.
    """
    h = pw.normal(text or "")
    return [w for w in re.findall(r"[a-z][a-z0-9]{2,}", h) if w not in STOPP]


def kategorie_des_ordners(ordner: str) -> str:
    """The top level — `Banking.PayPal` teaches for `Banking`.

    🔑 Learning happens across ALL his folders, not only the categories from
       `REGELN`: `Fahrrad`, `Hausbau`, `Menschen` are just as much his drawers.
       Learn only our own categories and you teach the program what it already
       knows.
    """
    erste = (ordner or "").split(".")[0].strip()
    if not erste or erste.upper() == "INBOX":
        return ""
    if any(erste == k or ordner == k for k in KEIN_WORTLEHRER):
        return ""
    return erste


def wortschatz_lernen(inv: dict, sichern: bool = True) -> dict:
    """Which words belong to which category? Counted from the inventory.

    What is counted per mail is the SET of its words, not every occurrence: a
    subject that says „rechnung“ three times is not three times as telling.
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
    # Words that occur only ONCE in their category are noise and bloat the file
    # (measured: 60 % of the entries, hardly any effect).
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
    """{word: in how many mails in total} and the total number of mails.

    Computed once per vocabulary and remembered on it — the cross-check calls the
    classification 16,000 times.
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
    """(category, margin) from the words — or ("", margin) when uncertain.

    🔴 THE FIRST ATTEMPT WAS WRONG, and the cross-check killed it. I had
       deliberately left out the prior knowledge about the size of the categories
       („the question is WHOSE vocabulary this is, not which category is the most
       frequent“). Measured: **25.7 % right** — and the confusion table showed
       why: 2,519 Technik mails wandered into `Gesundheit` (15 mails, 10 words).
       With add-1 smoothing an UNKNOWN word is cheap in a tiny category, because
       there you divide by a tiny sum. So the smallest category wins almost every
       vote.

    🔑 The right way is to sum only POSITIVE evidence and normalise it against
       the whole stock — per word `log( p(word|category) / p(word|all) )`. A word
       that occurs everywhere („rechnung“) carries almost nothing; a word that
       stands almost only in one category („statik“, „diskstation“) carries a lot.
       Words a category does NOT know do not count — otherwise the size of the
       denominator decides again instead of the content.

    `ohne` subtracts the words of ONE mail from its own category — only for the
    cross-check, so that nothing answers itself there.
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
            n_kat -= 1                      # the mail's own words do not count
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
                continue                    # the category does not know the word -> no evidence
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
    # 🔴 The winner also has to KNOW the words. A hit built on a single word is
    #    an accident, not a verdict.
    if belege.get(erster, 0) < WORT_MIN_TREFFER:
        return "", 0.0
    if len(rang) < 2:
        return erster, 99.0
    abstand = rang[0][1] - rang[1][1]
    if abstand < WORT_ABSTAND:
        return "", abstand
    return erster, abstand

def wortschatz_pruefen(inv: dict, ws: dict = None) -> dict:
    """Cross-check against our own stock: how often does the vocabulary get it
    right?

    🔑 Every mail is judged AFTER its own words have been subtracted from its
       category. Without that, every mail answers itself and the hit rate would be
       a self-report. That is the same mistake as a test bench measuring its own
       memory.
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
    """Let the configured model classify what the patterns did not recognise.

    🔴 Fails without costing the restructuring: no model, no network, a broken
    answer — then the derivation from the patterns stands. A judgement aid must
    never hold up the mail run (the same rule as with
    `ki_regeln_vorschlagen`).
    """
    # 🔴 Returns (count, reason). Before it was a bare 0 — and that is exactly
    #    why on 2026-09-27 NONE of the 234 domains from the catch folder appeared
    #    in the map although the page reported „AI ready“: the provider is
    #    „werkstatt“, and that works through wake-up calls, that is ASYNCHRONOUSLY.
    #    Here an answer is needed in the same moment. A silent 0 looks like „the
    #    model knew nothing“ and means „the model was never asked“.
    # 🔴 What comes back is (count, CODE, text). The code is for the branching,
    #    the text for the human. Branch on a German sentence
    #    („'Werkstatt' in grund“) and you build a bug into the next rewording —
    #    and into every translation.
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


# ── Use the AI in the workshop, where it runs ─────────────────────
# The Postwache uses the AI in the workshop, where it runs.
#
# 🔴 The workshop is a TASK BOOK, not a model endpoint. `local_engine` knows
#    `create_thread` and a queue — an agent picks the task up later. There is no
#    place there that answers a question in the same moment. Ignore that and you
#    build a call that waits 60 s and then has nothing.
#
# 🔑 So a ROUND TRIP over two runs, exactly as the rule suggestions have done
#    since 2.x: put the question down -> create a task -> collect the answer next
#    time. The Postwache never waits, and the answer is not lost when the
#    container happens to be off.
WERK_FRAGE = "postwache_domains.json"
WERK_ANTWORT = "postwache_domains_antwort.json"
WERK_DECKEL = 120          # this many domains per task, so it stays readable
# 🔴 For this long the same question is NOT repeated. On 2026-09-27 the second
#    `plan` run created task #11 within a minute with exactly the same 120 domains
#    as #10 — a plan may run often, but a task book with the same task in it three
#    times is useless. An agent needs time.
WERK_WIEDERVORLAGE = 12 * 3600


def _werk_ordner() -> str:
    try:
        pfad = pw.konfig().get("werkstatt") or ""
    except Exception:
        return ""
    return pfad if pfad and os.path.isdir(pfad) else ""


def erlaubte_kategorien() -> list:
    """What is ALLOWED to come back as a category.

    🔑 The categories from `REGELN` AND the ones he created himself (from the
       learned vocabulary). Allow only our own and you let the model talk past
       `Fahrrad` and `Hausbau`.
    """
    aus_regeln = {z.split(".")[0] for z, _, _, _ in REGELN}
    aus_ordnern = set((wortschatz_laden().get("kategorien") or {}).keys())
    return sorted((aus_regeln | aus_ordnern) - {AUFFANG, EIGENE_POST})


def werkstatt_fragen(offen: dict) -> int:
    """Put the open domains in front of the workshop and create a task.

    Returns the task number, 0 when nothing worked. Only the domain and example
    SUBJECTS are handed over — no message body, no addresses of people. The same
    data protection as with `pw.uebergeben()`.
    """
    ordner = _werk_ordner()
    if not ordner or not offen:
        return 0
    erlaubt = erlaubte_kategorien()
    namen = sorted(offen, key=lambda d: -len(offen[d]))[:WERK_DECKEL]
    # 🔴 Already asked and no answer yet? Then wait, do not ask again. The SET
    #    of domains is compared, not their order.
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
    # 🔴 The same jam bolt as with the wake-up call for unclear mails: if too
    #    many are already untouched, the QUESTION is put down but no new task is
    #    created. The agent finds it as soon as it works through the open one.
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
    """Collect the workshop's answer and take it into the map.

    🔴 Every line is checked against `erlaubte_kategorien()`. An answer out of a
       task book is FOREIGN TEXT — it may not invent a category and may not
       determine a folder that should not exist.

    The file is renamed after being read, not deleted: delete it and you can no
    longer look up afterwards what the agent said.
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
            continue                     # „none fits“ is a valid answer
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
    """Derive folders from the inventory for the senders no rule matches.

    Returns {domain: "Category.Brand"}. The result is remembered, so that the same
    domain is not judged anew on the next plan — and so that he can read up WHY a
    folder is called what it is called.
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
    # 🔴 THE ORDER CANNOT BE SWAPPED, and for one reason: precision before
    #    coverage.
    #    1. `kategorie_aus_inhalt` — hand-written patterns, narrowly framed and
    #       measured against the stock („Kfz-Versicherer“ -> insurance).
    #    2. `kategorie_aus_wortschatz` — LEARNED from his own folders, broad but
    #       statistical: a single frequent word can pull.
    #    3. the model — only when a provider answers synchronously.
    #    With the vocabulary in front it would override the checked patterns; at
    #    the back it would never get a turn, because the AI (provider
    #    „werkstatt“) never answers.
    ws = wortschatz_laden()
    # 1) derive from the subjects
    for d, betreffe in offen.items():
        if d in karte:
            continue
        kat = kategorie_aus_inhalt(betreffe)
        if kat:
            karte[d] = {"kategorie": kat, "quelle": "muster"}
    # 2) what is left: the learned vocabulary
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
    # 3) what is STILL left after that, show to the model
    #
    # 🔑 This is exactly where imitation reaches its limit, measured on
    #    2026-09-27: the vocabulary got 45 of the 234 open domains, because it only
    #    knows words that OCCUR in his folders. „Please rejoin Test4Theory“ or
    #    „Mafia Wars jetzt auch auf Deutsch“ he never filed — that needs knowledge
    #    of the world, and only a model has that.
    rest = {d: b for d, b in offen.items() if d not in karte}
    if mit_ki and rest:
        gefragt, code, grund = ki_kategorien(rest, karte)
        if code == "werkstatt":
            # The round trip: FIRST collect what was asked last time, THEN ask
            # about the rest. The other way round you ask the same thing twice.
            genommen = werkstatt_antwort_holen(karte)
            if genommen:
                rest = {d: b for d, b in offen.items() if d not in karte}
            if rest:
                werkstatt_fragen(rest)
        elif code:
            print("Modell nicht gefragt: %s" % grund)
        elif gefragt:
            print("vom Modell eingeordnet: %d Domains" % gefragt)

    # 3) build folder names — a folder of its OWN only from the threshold up
    #
    # 🔴 AND NEVER from a vocabulary decision. Measured on 2026-09-27 after the
    #    first armed run: the vocabulary produced `Reisen.Samsung`,
    #    `Kinder.Endomondo`, `Versicherungen.Fastspring`, `Gaming.Highresaudio`.
    #    The vocabulary GUESSES the category from words — it does not know the
    #    brand. `Reisen.Allgemein` is a wrong drawer and shows up at the next
    #    refinement; `Reisen.Samsung` is a wrongly NAMED folder and stays for ever.
    #    A guessed name is worse than a guessed drawer.
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
    """Where does THIS one new mail belong? For the watchman, not for the plan.

    When new mail arrives it must always be analysed and filed, and where new
    folders are needed it should create them on its own.

    🔑 The decision is REMEMBERED. With a single mail `kategorie_aus_inhalt()`
       sees only one subject — tomorrow the same sender might come out differently,
       and then his post would lie in two folders. Decided once, the same from then
       on.

    🔴 A folder of its own per sender only appears from `SCHWELLE_EIGENER_ORDNER`
       mails on. Otherwise the mailbox grows by one folder per newsletter he
       receives once. Until then: `Category.Allgemein`. Merging happens at the next
       `plan`/`anwenden`.
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
            # 🔑 The same middle stage as in the plan: what the patterns do not
            #    know, the vocabulary learned from his folders often still can.
            kat, _abstand = kategorie_aus_wortschatz([m.get("betreff") or ""])
            quelle = "wortschatz-neu"
        if not kat:
            return "", "", False
        eintrag = {"kategorie": kat, "quelle": quelle, "anzahl": 0}
    eintrag["anzahl"] = int(eintrag.get("anzahl") or 0) + 1
    marke = marke_aus_domain(dom)
    # 🔴 The same limit as in the plan: never a brand folder from the vocabulary.
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

    # Stage 2a: learn the vocabulary from his OWN folders. 🔑 Must stand BEFORE
    # `struktur_lernen` — that one asks it.
    stand_schreiben(schritt="plan", laeuft=True, phase="wortschatz",
                    text="Wortschatz wird gelernt")
    ws = wortschatz_lernen(inv)
    _WS_ZWISCHEN["ws"] = ws          # the cache has to know the new one
    pr = wortschatz_pruefen(inv, ws)
    print("Wortschatz: %d Kategorien, %d Woerter, Kreuzprobe %.0f%% Treffer "
          "bei %.0f%% Abdeckung"
          % (len(ws["kategorien"]),
             sum(len(d) for d in ws["kategorien"].values()),
             100 * pr["treffer"], 100 * pr["abdeckung"]))

    # Stage 2b: derive what no rule matches from the content
    stand_schreiben(schritt="plan", laeuft=True, phase="ableiten",
                    text="Struktur wird abgeleitet")
    abgeleitet = struktur_lernen(inv)
    print("aus den Inhalten abgeleitet: %d Absender-Domains" % len(abgeleitet))

    bewegungen, bleibt, nach_ziel, unsortiert = [], 0, {}, []
    for ordner, v in sorted(inv["ordner"].items()):
        if v["tabu"] or ordner.upper() == "INBOX":
            continue          # taboo folders and the inbox are never cleared
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
    """A short print of the plan. Applying it demands this as approval — so that
    nobody executes a plan they have not read, and no old plan can still go off
    after a new inventory."""
    import hashlib
    h = hashlib.sha256(open(pfad, "rb").read()).hexdigest()
    return h[:12]


def journal_schreiben(satz: dict) -> None:
    with open(out_pfad("umbau_journal.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(satz, ensure_ascii=False) + "\n")


def ordner_anlegen(pf, pfad: str) -> str:
    """Create a folder at the ROOT (the namespace is ""), then CHECK that it
    appears in LIST. A folder the server has named differently from what was
    intended is the first step towards post in a place nobody finds."""
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
                # 🔴 Check the number space: if UIDVALIDITY changes, every
                #    remembered UID is worthless — and then nothing is guessed.
                jetzt = pf.uidvalidity(quelle)
                soll = liste[0].get("uidvalidity") or 0
                if soll and jetzt and jetzt != soll:
                    print("   ! %s: UIDVALIDITY %s != %s im Plan — uebersprungen"
                          % (quelle, jetzt, soll))
                    uebersprungen += len(liste)
                    continue

                # 1) fetch the Message-Ids of the planned UIDs and compare.
                # 🔴 The key is the Message-Id, not the UID. If it does not match,
                #    a DIFFERENT mail lies there than in the plan — and that one is
                #    not touched.
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

                # 2) group by target, only what the Message-Id confirms
                je_ziel = {}
                for b in liste:
                    if not mid_bestaetigt(b.get("mid"), nach_mid.get(b["uid"], "")):
                        uebersprungen += 1
                        continue
                    je_ziel.setdefault(b["ziel"], []).append(b)

                # 3) copy per target as a UID set. An IMAP COPY over a set is
                #    ONE instruction: if "OK" comes back, EVERYTHING is copied —
                #    only then is it ticked off. 17,600 individual copies with an
                #    expunge each would have taken over an hour.
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
                # 4) ONE expunge per folder, only after all copies are
                #    confirmed. Before that no mail is removed — only marked.
                if etwas_abgehakt:
                    pf.m.expunge()
                print("   %-28s %5d bewegt" % (quelle, sum(len(g) for g in je_ziel.values())))
                stand_schreiben(fortschritt=getan, text="fertig: %s" % quelle)
            finally:
                try:
                    pf.m.select("INBOX", readonly=False)
                except Exception:
                    pass
        # 🔴 Still INSIDE the open connection: after the restructuring the
        #    learned filing points at folders that have just become empty.
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
    """Everything from the journal back to where it came from. That is the reason
    every move is logged with its source AND Message-Id."""
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
    # 🔴 Put a finished journal aside, do not delete it. If it stayed, the page
    #    would keep showing „N moves“ and a second „bring everything back“ would
    #    look for mails that have long been back — which looks like an error and
    #    is not one.
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
    """An open connection — with a retry on a network hiccup.

    🔴 The mailbox is sometimes unreachable. Measured: the provider takes 8
    simultaneous connections without complaint, so there is NO rate limit. But the
    Pi's log holds two genuine dropouts („Temporary failure in name resolution“,
    2026-09-21 and -23) — a two-second DNS hiccup must not abort a six-minute run.

    The wait doubles (2 s, 4 s). On the last attempt the error flies on — it
    belongs visibly in the status, not swallowed.
    """
    huelle = offen = None
    for i in range(1, versuche + 1):
        try:
            huelle = pw.Postfach(zug, schreiben=schreiben)
            # 🔴 Pass on the result of `__enter__()`, not the shell. With
            #    `Postfach` both are the same — but a context manager should not
            #    rely on that.
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
    """For the duration of the block, every piece of state belongs to THIS mailbox.

    🔴 `pw.state_pfad()` hangs on the global `_PF_ID`, and only
    `pw.pf_waehlen()` sets that. From the command line it is EMPTY — then
    `load(ABLAGE)` reads the global file instead of `state/pf/<id>/ablage.json`,
    finds nothing and dutifully reports success for nothing. That is exactly how
    `ablage_veralten()` failed on 2026-09-27 (returned `False`, map unchanged).

    🔑 This is the same rule that `do_POST` enforces for the page: ONE place
    chooses the mailbox, before every action. The one action that forgets it is
    exactly the one that works in the wrong place.
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
        # Restore, do not clear: a change has to return to where it came from
        # (the same lesson as with the folder change in IMAP).
        pw.pf_waehlen(self.vorher)


def ablage_erneuern(pf=None) -> str:
    """Relearn the watchman's map IMMEDIATELY.

    🔴 Merely declaring it invalid is not enough: `ablage_frisch()` only learns
    on the next run that sees any post at all. Until then the old map would stand —
    on 2026-09-27 with 46 folders, 29 of which were empty after the restructuring.
    When the connection is open anyway, learning happens NOW.

    If that fails, at least the marker is deleted (fallback).
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
    """Declare the watchman's learned map invalid.

    🔴 Without this the watchman sorts, after the restructuring, into folders
    that no longer exist: `ablage.json` knew 46 folders, 29 of them were empty
    after the run and get removed. Otherwise `ablage_frisch()` only relearns after
    24 h — and until then every move fails. The restructuring has to take the map
    with it.
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


# ── Stage 4: remove empty folders ───────────────────────────────
# What the Postwache may do is remove folders that are no longer needed, but
# only when there are no mails in them any more.
#
# 🔑 „NO LONGER NEEDED“ IS NOT THE SAME AS „EMPTY“. A folder he created himself
#    and that is waiting for post (`Github`, `Linkedin`, `Traderepublic`) is
#    empty — but needed. Dispensable is a folder THE RESTRUCTURING emptied. Hence
#    two classes, and only the first is removed without asking.
#
# 🔴 This is the ONLY exception to the principle „nothing is ever deleted“ — and
#    it applies expressly only to the FOLDER, never to a mail. The count is
#    fetched from the server immediately BEFORE removal, not from the inventory:
#    post could have arrived between measuring and removing.

# Special folders are recognised by their FLAGS, not by their name. A mailbox in
# English calls the trash something else — the flag is the same.
SONDER_FLAGS = ("Noselect", "Trash", "Junk", "Drafts", "Sent",
                "Archive", "All", "Flagged", "Important")


def _list_mit_flags(pf) -> dict:
    """{folder name: flags} — we need the flags, `ordner_liste()` throws them away."""
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
    """Does this folder carry a special-use flag? Then never touch it."""
    f = (flags or "").lower()
    return any(("\\" + s.lower()) in f for s in SONDER_FLAGS)


def _ordner_leer(pf, name: str):
    """(empty?, count) — LIVE from the server, not from the inventory.

    🔴 No answer does NOT mean empty. Read a missing answer as „nothing in it“
    and you remove a folder you never counted.
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
    """Remove empty, dispensable folders. Without `--scharf` nothing happens.

    🔴 A folder a rule of HIS OWN points at stays — even when empty. That is
    exactly how `Auto.Autohaus-nord` disappeared on 2026-09-27, a folder he had
    created himself; afterwards his rule pointed into nothing, and the watchman
    could no longer have put new post there. Empty does not mean dispensable when
    somebody is waiting for it.
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
    # 🔴 A pure collecting folder like `Shopping` NEVER had post of its own —
    #    only its 24 children. Once those are cleared away it is just as
    #    dispensable. So whether a DESCENDANT had post counts too.
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
    # His own targets, read ONCE — they are taboo like the trash.
    eigene_ziele = set(eigene_regeln().values())
    with verbindung(zug, schreiben=not trocken) as pf:
        flaggen = _list_mit_flags(pf)
        kinder = {}
        for n in flaggen:
            eltern = n.rsplit(pf.trenner, 1)[0] if pf.trenner in n else ""
            if eltern:
                kinder.setdefault(eltern, set()).add(n)

        # Deepest first: only once the children are gone is the parent childless.
        reihe = sorted(flaggen, key=lambda x: (-x.count(pf.trenner), x))
        for nr, name in enumerate(reihe, 1):
            # 🔴 The progress has to run during the CHECK, not only during the
            #    removal: the search asks each of the 168 folders separately for
            #    its count. Report only from the first hit and you show „0“ for
            #    minutes.
            stand_schreiben(phase="pruefen", fortschritt=nr, gesamt=len(reihe),
                            text="geprueft: %d/%d (%s)" % (nr, len(reihe), name))
            grund = ""
            if name.upper() == "INBOX" or tabu(name):
                grund = "tabu"
            elif name in eigene_ziele:
                # 🔴 A rule of HIS OWN points at this one. Empty does not mean
                #    dispensable when somebody is waiting for it.
                grund = "Ziel einer eigenen Absenderregel"
            elif sonderordner(flaggen.get(name, "")):
                grund = "Sonderordner (%s)" % (flaggen.get(name) or "").strip()
            elif name in geplant:
                grund = "Ziel im aktuellen Plan"
            elif kinder.get(name):
                grund = "hat noch %d Unterordner" % len(kinder[name])
            elif not hatte_post.get(name, False) and not auch_vorher_leere:
                # 🔴 A folder in a BRANCH the restructuring has cleared away may
                #    go with it. `Shopping.Amazon` was already empty before — but
                #    its 23 siblings are gone, and a single leftover keeps the
                #    whole old tree alive. A folder with NO parent (`Github`,
                #    `Linkedin`) stays: he created that one and may be waiting for
                #    post.
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
                # 🔴 Carry it in the preview as well, otherwise the dry run looks
                #    different from the armed run: a parent would appear to
                #    „still have subfolders“ there which in truth disappear with
                #    it.
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
    # 🔴 Count first, then list — and the expected reasons LAST. The first
    #    version showed 40 lines, all „target in the current plan“: exactly the
    #    cases one expects anyway. Why `Github` stays fell off the end. A listing
    #    that shows only the expected is not one.
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
    """Learn, cross-check, show — without doing anything to the mailbox.

    🔑 The cross-check is the point. A classifier that does not name its hit rate
       is making a claim.
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
        # What is characteristic is not the MOST FREQUENT word but the one that
        # stands disproportionately often in THIS category.
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
        # 🔴 The default is DRY. Whoever wants folders removed says so expressly.
        return ordner_raeumen(trocken="scharf" not in args,
                              auch_vorher_leere="auch-vorher-leere" in args)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
