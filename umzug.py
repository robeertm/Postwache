#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""umzug.py — ein ganzes Postfach zu einem anderen Anbieter bringen.

Der Besitzer, 27.09.2026: „man gibt zwei anbieter an, geht ja schon, dann kann man
die parallel betreiben oder man sagt uebertrage emails von anbieter a nach
anbieter b und sortiere sie gleich so wie wir es die letzten stunden getan
haben in eine schoene struktur je nach inhalt der mail, ist der umzug
abgeschlossen kann man bei anbieter a alles loeschen oder eine staendige
umleitung der mails ueber die postwache einrichten. so das anbieter a keine
mails mehr behaelt aber alles bei anbieter b ankommt und einsortiert wird"

Sechs Stufen, jede einzeln aufrufbar, jede fuer sich harmlos:

  1. pruefen        beide Anbieter erreichbar? B beschreibbar? was liegt dort?
  2. plan           WAS geht WOHIN — ohne eine einzige Mail anzufassen
  3. uebertragen    bei A lesen, bei B anlegen, NACHSEHEN. A bleibt unberuehrt.
  4. abgleich       ist bei B wirklich alles angekommen?
  5. quelle-leeren  erst jetzt, eigene Freigabe, nur was bei B nachweislich steht
  6. umleitung      ab jetzt laufend: neue Post bei A -> einsortiert bei B

Die Einsortierung ist NICHT neu erfunden: `umzug` fragt denselben `umbau`,
der dessen Postfach am 27.09. von 53 auf 132 Ordner gebracht hat. Eine Mail
landet bei B dort, wo sie bei A gelandet waere — nur ohne den Zwischenschritt.

🔴 DIE GRENZE DIESES PROGRAMMS: zwei Anbieter sind zwei Server, und zwischen
   zwei Servern gibt es kein COPY. Jede Mail wird bei A GELESEN (BODY.PEEK) und
   bei B NEU ANGELEGT (APPEND). Das ist eine Kopie, kein Verschieben — danach
   liegt die Mail ZWEIMAL. Genau darum ist „quelle-leeren" eine eigene Stufe
   mit eigener Freigabe und nicht das stille Ende von „uebertragen".

🔴 IDENTITAET IST DIE MESSAGE-ID, NICHT DIE UID. UIDs gelten je Server und je
   Ordner; nach dem APPEND hat dieselbe Mail bei B eine andere. Wer bei A
   loeschen will, muss sie bei B WIEDERFINDEN koennen — und wer keine
   Message-Id hat, ist nicht wiederfindbar. Eine Mail ohne Message-Id wird
   uebertragen und bei A NIEMALS geloescht. Lieber doppelt als weg.

🔴 EIN LEERES SUCHERGEBNIS IST KEINE BESTAETIGUNG. Dieselbe Falle wie am
   27.09. im Umbau: „nichts gefunden" heisst nicht „stimmt". Bestaetigt ist
   eine Mail nur, wenn ihre Message-Id bei B TATSAECHLICH GELESEN wurde.
   Deshalb `U.mid_bestaetigt()` — dieselbe reine Funktion, dieselbe Probe.

🔴 SONDERORDNER GEHEN AN DER FLAGGE, NICHT AM NAMEN. Gesendetes bei A heisst
   „Sent Items", bei B vielleicht „Gesendet" oder „INBOX.Sent". Wer nach Namen
   zuordnet, legt bei B einen zweiten Gesendet-Ordner an. `\\Sent` -> `\\Sent`.

🔴 PAPIERKORB UND SPAM ZIEHEN NICHT MIT. Sie sind Absicht, nicht Inhalt.
   Gesendetes und Entwuerfe ziehen mit — das ist dessen eigener Nachweis.
   Das Archiv zieht mit und wird dabei SORTIERT, so wie im Umbau: Der Besitzer wollte
   „auch archivierte mails sortieren und aus dem archiv holen".
"""

import contextlib
import email
import email.utils
import gzip
import imaplib
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import postwache as pw   # noqa: E402  — bewaehrtes IMAP, dekodieren()
import umbau as U        # noqa: E402  — Inventar, Regeln, Verbindung, Ordner anlegen

# ── Was NICHT mitzieht ───────────────────────────────────────────────────────
# 🔴 Enger als `umbau.TABU`! Der Umbau darf Gesendetes nicht ANFASSEN, weil es
#    dort bleiben soll. Beim Umzug soll es MITKOMMEN — nur eben unsortiert in
#    den Gesendet-Ordner bei B. Zurueck bleibt allein, was der Besitzer schon
#    weggeworfen oder was der Anbieter als Spam einsortiert hat.
BLEIBT_ZURUECK = {
    "trash", "papierkorb", "deleted items", "gelöschte objekte",
    "spam", "junk", "junk e-mail", "unerwünscht",
}

# Flaggen, deren Inhalt 1:1 in den gleichflaggigen Ordner bei B geht statt in
# die Inhaltsstruktur. `\Archive` steht bewusst NICHT dabei.
UNSORTIERT_UEBER = ("Sent", "Drafts")

# Nur diese Marken werden mitgenommen. `\Deleted` waere ein Loeschauftrag bei B,
# `\Recent` darf ein APPEND gar nicht setzen, eigene Schlagwoerter ($Label1)
# lehnen viele Server ab und reissen den ganzen APPEND mit.
MARKEN_ERLAUBT = ("\\Seen", "\\Answered", "\\Flagged", "\\Draft")

BLOCK = 40          # so viele Mails je FETCH-Runde; eine Mail kann Megabyte sein
MAX_MAIL = 40 * 1024 * 1024   # 40 MB — darueber wird gemeldet, nicht uebertragen


def out_pfad(name: str) -> str:
    return U.out_pfad(name)


# ── Stand fuer die Seite ─────────────────────────────────────────────────────
# Gleiche Bauart wie `umbau.stand_schreiben`, eigene Datei: ein Umzug kann
# Stunden dauern, und der Besitzer muss sehen, wo er steht, ohne ein Protokoll zu
# lesen. (der Besitzer, 27.09.: „sehe nicht wie weit das ordner loeschen ist.")
def stand_schreiben(**felder) -> None:
    d = stand_lesen()
    if felder.get("laeuft") and not d.get("laeuft"):
        felder.setdefault("begonnen", time.time())
    d.update(felder)
    d["zeit"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        with open(out_pfad("umzug_stand.json"), "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def stand_lesen() -> dict:
    try:
        return json.load(open(out_pfad("umzug_stand.json"), encoding="utf-8"))
    except Exception:
        return {}


def journal_schreiben(saetze: list) -> None:
    """Jede uebertragene Mail mit Herkunft, Ziel und Message-Id.

    🔑 Das ist die einzige Bruecke zwischen zwei Servern: bei A steht eine UID,
       bei B eine andere, gemeinsam haben sie nur die Message-Id. Ohne dieses
       Journal koennte „quelle-leeren" nicht wissen, WELCHE Mail bei A weg darf.
    """
    if not saetze:
        return
    with open(out_pfad("umzug_journal.jsonl"), "a", encoding="utf-8") as f:
        for s in saetze:
            f.write(json.dumps(s, ensure_ascii=False) + "\n")


def journal_lesen() -> list:
    raus = []
    try:
        with open(out_pfad("umzug_journal.jsonl"), encoding="utf-8") as f:
            for zl in f:
                zl = zl.strip()
                if not zl:
                    continue
                try:
                    raus.append(json.loads(zl))
                except ValueError:
                    continue
    except OSError:
        pass
    return raus


# ── Die beiden Anbieter ──────────────────────────────────────────────────────
def paar(von_id: str, nach_id: str):
    """Die zwei Zugaenge — oder eine Fehlermeldung, die den Grund nennt.

    🔴 A und B duerfen nicht dasselbe Postfach sein. Ein Umzug auf sich selbst
       waere ein APPEND jeder Mail in ihren eigenen Ordner: jede Mail doppelt,
       und „quelle-leeren" wuerde anschliessend beide Haelften desselben
       Bestandes gegeneinander pruefen. Verglichen wird die ADRESSE, nicht die
       Kennung — zwei Eintraege mit derselben Adresse sind dasselbe Postfach.
    """
    faecher = {f["id"]: f for f in pw.postfaecher()}
    if not faecher:
        return None, None, "Kein Postfach eingerichtet."
    if len(faecher) < 2:
        return None, None, ("Nur ein Postfach eingerichtet (%s). Ein Umzug "
                            "braucht zwei — zweites Postfach in den "
                            "Einstellungen anlegen." % ", ".join(faecher))
    ids = list(faecher)
    a = faecher.get(str(von_id or "").strip())
    b = faecher.get(str(nach_id or "").strip())
    if not a:
        return None, None, "Quelle %r gibt es nicht. Vorhanden: %s" % (von_id, ", ".join(ids))
    if not b:
        return None, None, "Ziel %r gibt es nicht. Vorhanden: %s" % (nach_id, ", ".join(ids))
    if a["id"] == b["id"]:
        return None, None, "Quelle und Ziel sind dasselbe Postfach."
    if a["adresse"].strip().lower() == b["adresse"].strip().lower():
        return None, None, ("Quelle und Ziel haben dieselbe Adresse (%s) — das "
                            "ist dasselbe Postfach unter zwei Kennungen."
                            % a["adresse"])
    return a, b, ""


def zurueckbleiber(name: str) -> bool:
    """Papierkorb/Spam — der Ordner UND alles darunter.

    🔴 Geprueft wird JEDES Wegstueck, nicht der ganze Name. „INBOX.Spam.Alt"
       endet nicht auf „spam" und beginnt nicht damit — waere also mit einem
       Praefix- oder Suffixvergleich nicht mitgezogen worden. Das fuehrende
       „INBOX" faellt weg, weil es bei vielen Anbietern vor jedem Ordner steht.
    """
    n = (name or "").lower().replace("/", ".")
    stuecke = [t for t in n.split(".") if t]
    if stuecke and stuecke[0] == "inbox":
        stuecke = stuecke[1:]
    return any(t in BLEIBT_ZURUECK for t in stuecke)


def flaggen_karte(pf) -> dict:
    """{Flagge ohne Backslash (klein): Ordnername} — fuer die Sonderordner.

    Kommt derselbe Ordner mit zwei Flaggen, gilt die erste; kommt eine Flagge
    zweimal, gilt der ERSTE Ordner. Beides ist selten und beides waere sonst
    eine stille Entscheidung.
    """
    raus = {}
    for name, flaggen in U._list_mit_flags(pf).items():
        for s in U.SONDER_FLAGS:
            marke = s.lower()
            if ("\\" + marke) in (flaggen or "").lower() and marke not in raus:
                raus[marke] = name
    return raus


def marken_saeubern(roh: str) -> str:
    """Nur die vier Marken, die ein APPEND ueberall vertraegt."""
    vorhanden = (roh or "").lower()
    behalten = [m for m in MARKEN_ERLAUBT if m.lower() in vorhanden]
    return ("(%s)" % " ".join(behalten)) if behalten else None


def mid_normal(mid: str) -> str:
    """Message-Id vergleichbar machen. Server geben sie mit und ohne spitze
    Klammern zurueck, manche mit Leerzeichen — verglichen wird der Kern."""
    s = (mid or "").strip()
    if s.startswith("<") and s.endswith(">"):
        s = s[1:-1]
    return s.strip().lower()


def mids_im_ordner(pf, name: str, ab_uid: int = 0) -> dict:
    """{Message-Id: UID} eines Ordners. readonly, BODY.PEEK, in Bloecken.

    🔑 Gelesen wird ueber FETCH, nicht ueber `SEARCH HEADER Message-Id`. Eine
       Suche waere ein Hin- und Rueckweg PRO MAIL und muesste die Message-Id in
       Anfuehrungszeichen setzen — bei einer Kennung mit Anfuehrungszeichen
       darin bricht die Suche oder, schlimmer, sie findet still das Falsche.
       Ein Block-FETCH liest 300 Kennungen auf einmal und hat kein Zitat-Problem.

    🔴 Das `finally` stellt auf INBOX zurueck — dieselbe Lehre wie ueberall:
       ein Wechsel muss dahin zurueck, wo er herkam.
    """
    raus = {}
    try:
        typ, _ = pf.m.select(pf._zitat(name), readonly=True)
        if typ != "OK":
            return raus
        bereich = "UID %d:*" % (int(ab_uid or 0) + 1) if ab_uid else "ALL"
        typ, daten = pf.m.uid("search", None, bereich)
        if typ != "OK" or not daten or not daten[0]:
            return raus
        uids = sorted(u for u in (int(x) for x in daten[0].split())
                      if u > int(ab_uid or 0))
        for i in range(0, len(uids), 300):
            teil = ",".join(str(u) for u in uids[i:i + 300])
            typ, antw = pf.m.uid("fetch", teil,
                                 "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
            if typ != "OK":
                continue
            uid_jetzt = 0
            for st in antw or []:
                if isinstance(st, bytes):
                    t = re.search(rb"UID (\d+)", st)
                    if t:
                        uid_jetzt = int(t.group(1))
                    continue
                if not isinstance(st, tuple) or len(st) < 2:
                    continue
                t = re.search(rb"UID (\d+)", st[0] or b"")
                uid = int(t.group(1)) if t else uid_jetzt
                mid = mid_normal(email.message_from_bytes(st[1]).get("Message-Id", ""))
                if mid:
                    raus[mid] = uid
    except Exception as e:
        print("   ! Kennungen aus %s: %s" % (name, str(e)[:120]))
    finally:
        try:
            pf.m.select("INBOX", readonly=True)
        except Exception:
            pass
    return raus


def b_bestand(pf, melden=None) -> dict:
    """Was liegt bei B schon? {Message-Id: Ordner} ueber ALLE Ordner.

    🔑 Das ist die Doppel-Sperre: eine Mail, deren Kennung bei B bereits
       irgendwo liegt, wird nicht ein zweites Mal angelegt. Ohne das waere ein
       abgebrochener und neu gestarteter Umzug ein Postfach mit allem doppelt —
       und ein `uebertragen`, das man zweimal druckt, ist der Normalfall, nicht
       die Ausnahme.

    🔴 Gezaehlt wird ueber alle Ordner, nicht nur ueber den geplanten Zielordner.
       Sonst legt eine spaeter verfeinerte Regel dieselbe Mail ein zweites Mal
       an, nur woanders — und beide Haelften sehen richtig aus.
    """
    raus = {}
    # 🔴 EINMAL fragen, nicht je Ordner. `_list_mit_flags` ist ein LIST am
    #    Server — in der Schleife waeren das bei 132 Ordnern 132 Abfragen fuer
    #    eine Antwort, die sich waehrend des Laufs nicht aendert.
    flaggen_alle = U._list_mit_flags(pf)
    for name in sorted(pf.ordner_liste()):
        if "\\noselect" in (flaggen_alle.get(name, "") or "").lower():
            continue
        if melden:
            melden(name)
        for mid, _uid in mids_im_ordner(pf, name).items():
            raus.setdefault(mid, name)
    return raus


# ── Stufe 1: pruefen ─────────────────────────────────────────────────────────
def pruefen(von_id: str, nach_id: str) -> int:
    a, b, fehler = paar(von_id, nach_id)
    if fehler:
        print(fehler)
        stand_schreiben(schritt="pruefen", laeuft=False, fehler=fehler)
        return 1
    stand_schreiben(schritt="pruefen", laeuft=True, fehler="", phase="verbinden",
                    von=a["id"], nach=b["id"],
                    text="%s → %s" % (a["name"], b["name"]))
    print("Quelle A: %s  (%s:%d)" % (a["name"], a["server"], a["port"]))
    print("Ziel   B: %s  (%s:%d)" % (b["name"], b["server"], b["port"]))
    lage = {"von": a["id"], "nach": b["id"]}
    for marke, zug in (("a", a), ("b", b)):
        try:
            with U.verbindung(zug, schreiben=(marke == "b")) as pf:
                flaggen_alle = U._list_mit_flags(pf)
                ordner = [o for o in pf.ordner_liste()
                          if "\\noselect" not in (flaggen_alle.get(o, "") or "").lower()]
                summe = 0
                for o in ordner:
                    sig = U.ordner_signatur(pf, o)
                    summe += sig[1] if sig else 0
                lage[marke] = {"ordner": len(ordner), "mails": summe,
                               "trenner": pf.trenner,
                               "sonder": flaggen_karte(pf)}
                print("\n%s %s: %d Ordner, %d Mails, Trenner %r"
                      % (marke.upper(), zug["name"], len(ordner), summe, pf.trenner))
                for flagge, name in sorted(lage[marke]["sonder"].items()):
                    print("     \\%-10s %s" % (flagge.capitalize(), name))
                if marke == "b":
                    # 🔑 Beschreibbar? Ein Umzug, der erst bei Mail 12.000
                    #    merkt, dass B keine Ordner anlegen darf, hat zwei
                    #    Stunden gearbeitet und nichts erreicht. Deshalb ein
                    #    Probeordner: anlegen, in LIST nachsehen, entfernen.
                    probe = "Postwache Probe %d" % int(time.time())
                    try:
                        U.ordner_anlegen(pf, probe)
                        pf.m.delete(pf._zitat(probe))
                        lage["b"]["schreibbar"] = True
                        print("     beschreibbar: ja (Probeordner angelegt und entfernt)")
                    except Exception as e:
                        lage["b"]["schreibbar"] = False
                        print("     ⚠ NICHT beschreibbar: %s" % str(e)[:140])
        except Exception as e:
            fehler = "%s nicht erreichbar: %s" % (marke.upper(), str(e)[:160])
            print("\n⚠ " + fehler)
            stand_schreiben(schritt="pruefen", laeuft=False, fehler=fehler)
            return 1
    if lage.get("b", {}).get("mails"):
        print("\nHinweis: B ist nicht leer (%d Mails). Das ist erlaubt — jede "
              "Mail, deren Kennung dort schon liegt, wird uebersprungen."
              % lage["b"]["mails"])
    stand_schreiben(schritt="pruefen", laeuft=False, lage=lage, fehler="",
                    text="A: %d Mails → B: %d Mails"
                         % (lage["a"]["mails"], lage["b"]["mails"]))
    print("\nWeiter mit:  umzug.py plan --von=%s --nach=%s" % (a["id"], b["id"]))
    return 0


# ── Stufe 2: plan ────────────────────────────────────────────────────────────
def inv_pfad(a_id: str) -> str:
    return out_pfad("umzug_inventar_%s.json.gz" % re.sub(r"[^a-zA-Z0-9_-]", "", a_id))


def inventar_a(a: dict, voll: bool = False) -> dict:
    """Kopfzeilen ALLER Mails bei A — ohne eine anzufassen.

    🔴 EIGENE Datei, nicht `inventar.json.gz`. Die gehoert dem Umbau und sagt
       ihm, welcher Ordner FRUEHER Post hatte — ohne sie darf
       `umbau.py ordner` keinen einzigen Ordner entfernen. Wer sie hier
       ueberschreibt, nimmt dem Umbau sein Gedaechtnis.

    Unveraenderte Ordner werden aus dem letzten Lauf uebernommen (Signatur aus
    UIDVALIDITY/MESSAGES/UIDNEXT, ein Hin- und Rueckweg je Ordner).
    """
    alt = {}
    if not voll:
        try:
            with gzip.open(inv_pfad(a["id"]), "rt", encoding="utf-8") as f:
                alt = json.load(f).get("ordner") or {}
        except Exception:
            alt = {}
    t0 = time.time()
    ordner = {}
    with U.verbindung(a, schreiben=False) as pf:
        flaggen_alle = U._list_mit_flags(pf)
        namen = [o for o in sorted(pf.ordner_liste())
                 if "\\noselect" not in (flaggen_alle.get(o, "") or "").lower()]
        stand_schreiben(schritt="plan", laeuft=True, phase="inventar",
                        gesamt=len(namen), fortschritt=0)
        for i, name in enumerate(namen, 1):
            sig = U.ordner_signatur(pf, name)
            frueher = alt.get(name) or {}
            uebernommen = (sig and frueher.get("signatur") == list(sig)
                           and int(frueher.get("leser") or 0) == U.LESER_FASSUNG)
            if uebernommen:
                ordner[name] = frueher
            else:
                mails, uidval = U.kopf_saetze(pf, name)
                ordner[name] = {
                    "uidvalidity": uidval,
                    "signatur": list(sig) if sig else None,
                    "leser": U.LESER_FASSUNG,
                    "flaggen": flaggen_alle.get(name, ""),
                    "tabu": zurueckbleiber(name),
                    "mails": mails,
                }
            stand_schreiben(fortschritt=i, text="%s (%d Mails)%s"
                            % (name, len(ordner[name]["mails"]),
                               " — unveraendert" if uebernommen else ""))
            print("   %-38s %6d Mails%s" % (name[:38], len(ordner[name]["mails"]),
                                            "  (unveraendert)" if uebernommen else ""))
        sonder = flaggen_karte(pf)
    inv = {"gemessen": time.strftime("%Y-%m-%dT%H:%M:%S"), "postfach": a["id"],
           "adresse": a["adresse"], "sonder": sonder, "ordner": ordner}
    with gzip.open(inv_pfad(a["id"]), "wt", encoding="utf-8") as f:
        json.dump(inv, f, ensure_ascii=False)
    summe = sum(len(v["mails"]) for v in ordner.values())
    print("Inventar A: %d Ordner, %d Mails (%.1fs)"
          % (len(ordner), summe, time.time() - t0))
    return inv


def plan(von_id: str, nach_id: str, grenze: int = 0, seit: str = "",
         voll: bool = False) -> int:
    a, b, fehler = paar(von_id, nach_id)
    if fehler:
        print(fehler)
        stand_schreiben(schritt="plan", laeuft=False, fehler=fehler)
        return 1
    stand_schreiben(schritt="plan", laeuft=True, fehler="", von=a["id"],
                    nach=b["id"], phase="inventar",
                    text="Inventar bei %s" % a["name"])
    inv = inventar_a(a, voll=voll)

    # Wohin gehoeren die Sonderordner bei B? An der FLAGGE, nicht am Namen.
    stand_schreiben(phase="ziele", text="Sonderordner bei B werden erfragt")
    with U.verbindung(b, schreiben=False) as pfb:
        sonder_b = flaggen_karte(pfb)
        trenner_b = pfb.trenner
    print("Sonderordner bei B: %s"
          % (", ".join("\\%s=%s" % (k.capitalize(), v)
                       for k, v in sorted(sonder_b.items())) or "keine"))

    # 🔑 Die Struktur wird mit DEMSELBEN Lehrer gelernt wie im Umbau — nichts
    #    neu erfunden. 🔴 Nur eine Kleinigkeit muss anders sein: `umbau`
    #    ueberspringt beim Lernen den Posteingang, weil dort im gewachsenen
    #    Postfach nur der Rest liegt. Bei einem UMZUG kann der Posteingang der
    #    ganze Bestand sein (ein Anbieter ohne Ordner) — dann haette der Lehrer
    #    nichts zu lesen. Deshalb bekommt er den Posteingang unter neutralem
    #    Namen zu sehen; an den Mails aendert das nichts.
    lehr_ordner = {}
    for name, v in inv["ordner"].items():
        schluessel = "Posteingang (Umzug)" if name.upper() == "INBOX" else name
        lehr_ordner[schluessel] = v
    stand_schreiben(phase="ableiten", text="Struktur wird aus den Inhalten abgeleitet")
    abgeleitet = U.struktur_lernen({"ordner": lehr_ordner})
    print("aus den Inhalten abgeleitet: %d Absender-Domains" % len(abgeleitet))

    bewegungen, zurueck_bleibt, nach_ziel, unsortiert, ohne_kennung = [], 0, {}, [], 0
    for ordner, v in sorted(inv["ordner"].items()):
        if v["tabu"]:
            zurueck_bleibt += len(v["mails"])
            continue
        flaggen = (v.get("flaggen") or "").lower()
        sonder_ziel = ""
        for marke in UNSORTIERT_UEBER:
            if ("\\" + marke.lower()) in flaggen:
                # Bei B der gleichflaggige Ordner; hat B keinen, behaelt der
                # Ordner seinen Namen — ein neuer „Gesendet" waere geraten.
                sonder_ziel = sonder_b.get(marke.lower(), ordner)
                break
        for m in v["mails"]:
            if seit and (m.get("datum") or "") < seit:
                continue
            if sonder_ziel:
                ziel, grund = sonder_ziel, "Sonderordner — zieht unsortiert mit"
            else:
                ziel, grund = U.ziel_fuer(m)
                if ziel == U.AUFFANG:
                    d = (m.get("von") or "").split("@")[-1].lower()
                    if d in abgeleitet:
                        ziel, grund = abgeleitet[d], "aus dem Inhalt abgeleitet"
            nach_ziel[ziel] = nach_ziel.get(ziel, 0) + 1
            if ziel == U.AUFFANG:
                unsortiert.append(m)
            ohne = not mid_normal(m.get("mid") or "")
            if ohne:
                ohne_kennung += 1
            bewegungen.append({
                "quelle": ordner, "uid": m["uid"], "uidvalidity": v["uidvalidity"],
                "ziel": ziel, "mid": m.get("mid") or "", "von": m.get("von") or "",
                "betreff": (m.get("betreff") or "")[:120], "datum": m.get("datum") or "",
                "grund": grund, "ohne_kennung": ohne,
            })
    if grenze:
        bewegungen = bewegungen[:grenze]

    p = {"erstellt": time.strftime("%Y-%m-%dT%H:%M:%S"),
         "von": a["id"], "von_adresse": a["adresse"],
         "nach": b["id"], "nach_adresse": b["adresse"],
         "inventar": inv["gemessen"], "trenner_b": trenner_b,
         "sonder_b": sonder_b, "ordner_neu": sorted(nach_ziel),
         "bewegungen": bewegungen}
    datei = out_pfad("umzug_plan.json.gz")
    with gzip.open(datei, "wt", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False)
    fp = U.fingerabdruck(datei)

    print("\n=== UMZUGSPLAN ===  %s → %s" % (a["name"], b["name"]))
    print("%d Mails ziehen um, %d bleiben zurueck (Papierkorb/Spam), %d Zielordner"
          % (len(bewegungen), zurueck_bleibt, len(nach_ziel)))
    if ohne_kennung:
        print("⚠ %d Mails haben KEINE Message-Id. Sie ziehen mit, werden bei "
              "A aber niemals geloescht — sie waeren dort nicht wiederfindbar."
              % ohne_kennung)
    print("\n%-34s %7s" % ("Zielordner bei B", "Mails"))
    for z in sorted(nach_ziel, key=lambda x: (-nach_ziel[x], x)):
        print("%-34s %7d" % (z[:34], nach_ziel[z]))
    if unsortiert:
        c = {}
        for m in unsortiert:
            d = (m.get("von") or "").split("@")[-1]
            c[d] = c.get(d, 0) + 1
        print("\n--- groesste unerkannte Absender (Auffang '%s') ---" % U.AUFFANG)
        for d, n in sorted(c.items(), key=lambda x: -x[1])[:25]:
            print("   %5d  %s" % (n, d))
    stand_schreiben(schritt="plan", laeuft=False, von=a["id"], nach=b["id"],
                    bewegungen=len(bewegungen), zurueck=zurueck_bleibt,
                    ohne_kennung=ohne_kennung, abdruck=fp,
                    baum=sorted(nach_ziel.items(), key=lambda x: (-x[1], x[0])),
                    text="%d Mails ziehen um" % len(bewegungen))
    print("\nPlan: %s\nFingerabdruck: %s" % (datei, fp))
    print("Uebertragen mit:  umzug.py uebertragen --freigabe=%s [--grenze=N] [--trocken]" % fp)
    return 0


# ── Stufe 3: uebertragen ─────────────────────────────────────────────────────
def praefix_b(pf) -> str:
    """Wo legt DIESER Server Ordner an: an der Wurzel oder unter INBOX?

    🔴 Mancher Anbieter antwortet mit dem persoenlichen Namensraum `""` — Ordner gehoeren
       an die WURZEL. Andere Anbieter antworten `"INBOX."`, und dort ist ein
       Ordner an der Wurzel nicht sichtbar. Der Namensraum wird deshalb
       ERFRAGT (NAMESPACE) und nicht geraten; antwortet der Server nicht,
       bleibt es die Wurzel — das ist an einem fehlenden Ordner sofort zu
       sehen, waehrend ein falsches „INBOX." still einen Ordner im Ordner baut.
    """
    try:
        typ, daten = pf.m.namespace()
        if typ == "OK" and daten:
            s = daten[0].decode("utf-8", "replace") if isinstance(daten[0], bytes) else str(daten[0])
            t = re.match(r'\(\("([^"]*)"', s)
            if t:
                return t.group(1)
    except Exception:
        pass
    return ""


def bei_b_name(ziel: str, trenner: str, praefix: str) -> str:
    """Der kanonische Pfad `Banking.PayPal` in der Schreibweise DIESES Servers.

    🔴 Der Punkt ist im Umbau der TRENNER, nicht Teil eines Namens
       (`marke_aus_domain` wirft Punkte aus Ordnernamen heraus). Wer den Pfad
       unveraendert an einen Server mit Trenner „/" gibt, legt EINEN Ordner
       namens „Banking.PayPal" an statt „PayPal" unter „Banking".
    """
    teile = [t for t in (ziel or "").split(".") if t]
    voll = trenner.join(teile)
    if praefix and not voll.startswith(praefix):
        voll = praefix + voll
    return voll


def mail_holen(pf, uid: int):
    """(Rohmail, Marken, Zeitpunkt) einer Mail — mit PEEK, damit sie bei A
    ungelesen bleibt. Gibt (None, None, None), wenn die UID nichts liefert.

    🔴 Zeilenenden werden auf CRLF gebracht. Das Netz-Format verlangt es, und
       ein Server, der eine Mail mit blossem LF annimmt, haengt die naechste
       Kopfzeile an den Rumpf — der Fehler faellt erst Wochen spaeter auf.
    """
    try:
        typ, daten = pf.m.uid("fetch", str(uid), "(FLAGS INTERNALDATE BODY.PEEK[])")
    except Exception as e:
        print("   ! UID %s nicht lesbar: %s" % (uid, str(e)[:120]))
        return None, None, None
    if typ != "OK" or not daten:
        return None, None, None
    roh = kopf = b""
    for st in daten:
        if isinstance(st, tuple) and len(st) >= 2:
            kopf = st[0] or b""
            roh = st[1] or b""
            break
    if not roh:
        return None, None, None
    roh = roh.replace(b"\r\n", b"\n").replace(b"\r", b"\n").replace(b"\n", b"\r\n")
    t = re.search(rb"FLAGS \(([^)]*)\)", kopf)
    marken = marken_saeubern(t.group(1).decode("utf-8", "replace") if t else "")
    zeit = None
    try:
        tupel = imaplib.Internaldate2tuple(kopf)
        if tupel:
            zeit = imaplib.Time2Internaldate(tupel)
    except Exception:
        zeit = None
    return roh, marken, zeit


def fingerabdruck_plan() -> str:
    """Der Abdruck des aktuellen Umzugsplans — die Freigabe zum Uebertragen.

    Eigene Funktion, damit die Seite und die Kommandozeile DIESELBE Zahl
    benutzen. Zwei Stellen, die denselben Abdruck selbst berechnen, sind zwei
    Gelegenheiten, ihn unterschiedlich zu berechnen.
    """
    return U.fingerabdruck(out_pfad("umzug_plan.json.gz"))


def uebertragen(freigabe: str, grenze: int = 0, trocken: bool = False) -> int:
    pfad = out_pfad("umzug_plan.json.gz")
    if not os.path.exists(pfad):
        print("Kein Plan. Erst:  umzug.py plan --von=... --nach=...")
        return 1
    fp = fingerabdruck_plan()
    if freigabe != fp:
        print("Freigabe passt nicht zum Plan.\n  Plan hat:  %s\n  angegeben: %s"
              % (fp, freigabe or "(nichts)"))
        return 1
    with gzip.open(pfad, "rt", encoding="utf-8") as f:
        p = json.load(f)
    a, b, fehler = paar(p["von"], p["nach"])
    if fehler:
        print(fehler)
        return 1
    bew = p["bewegungen"]
    if grenze:
        bew = bew[:grenze]
    nach_quelle = {}
    for s in bew:
        nach_quelle.setdefault(s["quelle"], []).append(s)

    stand_schreiben(schritt="uebertragen", laeuft=True, fehler="", von=a["id"],
                    nach=b["id"], phase="bestand", fortschritt=0, gesamt=len(bew),
                    trocken=bool(trocken), uebertragen=0, schon_dort=0,
                    text="%d Mails%s" % (len(bew), " (trocken)" if trocken else ""))
    print("%d Mails%s  %s → %s"
          % (len(bew), "  [TROCKEN]" if trocken else "", a["name"], b["name"]))

    getan = schon = fehl = gross = 0
    unbestaetigt = []
    with U.verbindung(a, schreiben=False) as pfa:
        with U.verbindung(b, schreiben=True) as pfb:
            praefix = praefix_b(pfb)
            print("Namensraum bei B: %r, Trenner %r" % (praefix, pfb.trenner))
            vorhanden = set(pfb.ordner_liste())
            for ziel in sorted({s["ziel"] for s in bew}):
                voll = bei_b_name(ziel, pfb.trenner, praefix)
                if voll in vorhanden:
                    continue
                if trocken:
                    print("   wuerde anlegen: %s" % voll)
                else:
                    print("   angelegt: %s" % U.ordner_anlegen(pfb, voll))
                    vorhanden.add(voll)

            # 🔑 Was liegt bei B schon? Ohne diese Liste ist ein zweiter Lauf
            #    ein Postfach mit allem doppelt.
            stand_schreiben(phase="bestand", text="Bestand bei B wird gelesen")
            bestand = b_bestand(pfb, melden=lambda n: stand_schreiben(
                text="Bestand bei B: %s" % n))
            print("bei B liegen schon %d Mails mit Kennung" % len(bestand))

            stand_schreiben(phase="uebertragen")
            for quelle, liste in sorted(nach_quelle.items()):
                typ, _ = pfa.m.select(pfa._zitat(quelle), readonly=True)
                if typ != "OK":
                    print("   ! %s bei A nicht waehlbar — uebersprungen" % quelle)
                    fehl += len(liste)
                    continue
                # 🔴 UIDVALIDITY-Sperre: hat der Ordner einen neuen Nummernkreis,
                #    zeigen die UIDs des Plans auf andere Mails. Dann wird
                #    NICHTS uebertragen, sondern gemeldet.
                jetzt = pfa.uidvalidity(quelle)
                soll = int(liste[0].get("uidvalidity") or 0)
                if soll and jetzt and jetzt != soll:
                    print("   ! %s: Nummernkreis geaendert (%d → %d) — "
                          "uebersprungen, Plan neu erstellen" % (quelle, soll, jetzt))
                    fehl += len(liste)
                    continue
                print("   %-34s %5d Mails" % (quelle[:34], len(liste)))
                uidnext_vorher, angelegt, journal = {}, [], []
                for satz in liste:
                    mid = mid_normal(satz.get("mid") or "")
                    ziel_voll = bei_b_name(satz["ziel"], pfb.trenner, praefix)
                    if mid and mid in bestand:
                        schon += 1
                        journal.append({"zeit": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                        "von": a["id"], "nach": b["id"],
                                        "quelle": quelle, "uid": satz["uid"],
                                        "uidvalidity": satz.get("uidvalidity") or 0,
                                        "ziel": bestand[mid], "mid": satz.get("mid") or "",
                                        "bei_b": True, "wie": "lag schon dort"})
                        continue
                    if trocken:
                        getan += 1
                        continue
                    if ziel_voll not in uidnext_vorher:
                        sig = U.ordner_signatur(pfb, ziel_voll)
                        uidnext_vorher[ziel_voll] = sig[2] if sig else 1
                    roh, marken, zeit = mail_holen(pfa, satz["uid"])
                    if roh is None:
                        fehl += 1
                        continue
                    if len(roh) > MAX_MAIL:
                        gross += 1
                        print("      ⚠ %.1f MB — zu gross, bleibt bei A: %s"
                              % (len(roh) / 1048576.0, (satz.get("betreff") or "")[:60]))
                        continue
                    try:
                        typ, _ = pfb.m.append(pfb._zitat(ziel_voll), marken, zeit, roh)
                    except Exception as e:
                        typ = "NO"
                        print("      ! APPEND %s: %s" % (ziel_voll, str(e)[:120]))
                    if typ != "OK":
                        fehl += 1
                        continue
                    if mid:
                        bestand[mid] = ziel_voll
                    angelegt.append((mid, ziel_voll, satz))
                    stand_schreiben(fortschritt=getan + len(angelegt) + schon,
                                    uebertragen=getan + len(angelegt), schon_dort=schon,
                                    text="%s → %s" % (quelle[:24], satz["ziel"][:24]))

                # ── NACHSEHEN ────────────────────────────────────────────────
                # 🔴 Erst jetzt gilt eine Mail als angekommen. Der Rueckgabewert
                #    des APPEND sagt nur, dass der Server den Auftrag ANGENOMMEN
                #    hat. Bestaetigt ist sie, wenn ihre Kennung bei B GELESEN
                #    wurde — leere Antwort ist keine Bestaetigung (27.09.).
                gefunden = {}
                for ziel_voll, uidnext in uidnext_vorher.items():
                    gefunden.update(mids_im_ordner(pfb, ziel_voll,
                                                   ab_uid=max(0, int(uidnext) - 1)))
                for mid, ziel_voll, satz in angelegt:
                    ist = mid if (mid and mid in gefunden) else ""
                    ok = U.mid_bestaetigt(mid, ist)
                    if ok:
                        getan += 1
                    else:
                        unbestaetigt.append((quelle, satz))
                    journal.append({"zeit": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                    "von": a["id"], "nach": b["id"],
                                    "quelle": quelle, "uid": satz["uid"],
                                    "uidvalidity": satz.get("uidvalidity") or 0,
                                    "ziel": ziel_voll, "mid": satz.get("mid") or "",
                                    "bei_b": bool(ok),
                                    "wie": "angelegt und nachgesehen" if ok
                                           else ("ohne Message-Id — bleibt bei A"
                                                 if not mid else "nicht wiedergefunden")})
                journal_schreiben(journal)
                pfa.m.select("INBOX", readonly=True)

    print("\nFERTIG%s: %d uebertragen und bestaetigt, %d lagen schon dort, "
          "%d nicht bestaetigt, %d Fehler, %d zu gross"
          % ("  [TROCKEN]" if trocken else "", getan, schon,
             len(unbestaetigt), fehl, gross))
    if unbestaetigt:
        print("\n⚠ NICHT bestaetigt — diese Mails bleiben bei A und "
              "duerfen dort nicht geloescht werden:")
        for quelle, satz in unbestaetigt[:20]:
            print("   %-24s %-30s %s" % (quelle[:24], (satz.get("von") or "")[:30],
                                         (satz.get("betreff") or "")[:50]))
        if len(unbestaetigt) > 20:
            print("   … und %d weitere" % (len(unbestaetigt) - 20))
    stand_schreiben(schritt="uebertragen", laeuft=False, uebertragen=getan,
                    schon_dort=schon, unbestaetigt=len(unbestaetigt),
                    fehler_zahl=fehl, zu_gross=gross,
                    text="%d uebertragen, %d lagen schon dort" % (getan, schon))
    if not trocken:
        print("\nWeiter mit:  umzug.py abgleich --von=%s --nach=%s" % (a["id"], b["id"]))
    return 0


# ── Stufe 4: abgleich ────────────────────────────────────────────────────────
def abgleich(von_id: str, nach_id: str) -> int:
    """Ist bei B wirklich alles angekommen? LIVE gezaehlt, nicht aus dem Journal.

    🔑 Das Journal sagt, was das Programm GETAN hat. Diese Stufe sagt, was
       WIRKLICH DA IST. Nur die zweite Frage darf ueber „bei A loeschen"
       entscheiden — ein Journal ueberlebt auch ein Postfach, das inzwischen
       zurueckgesetzt wurde.
    """
    a, b, fehler = paar(von_id, nach_id)
    if fehler:
        print(fehler)
        stand_schreiben(schritt="abgleich", laeuft=False, fehler=fehler)
        return 1
    stand_schreiben(schritt="abgleich", laeuft=True, fehler="", von=a["id"],
                    nach=b["id"], phase="lesen", text="Bestand bei A")
    bei_a, ohne_kennung, a_ordner = {}, 0, {}
    with U.verbindung(a, schreiben=False) as pfa:
        flaggen_alle = U._list_mit_flags(pfa)
        for name in sorted(pfa.ordner_liste()):
            if "\\noselect" in (flaggen_alle.get(name, "") or "").lower():
                continue
            if zurueckbleiber(name):
                continue
            stand_schreiben(text="A: %s" % name)
            mids = mids_im_ordner(pfa, name)
            sig = U.ordner_signatur(pfa, name)
            anzahl = sig[1] if sig else len(mids)
            a_ordner[name] = anzahl
            ohne_kennung += max(0, anzahl - len(mids))
            for mid in mids:
                bei_a.setdefault(mid, name)
    stand_schreiben(phase="lesen", text="Bestand bei B")
    with U.verbindung(b, schreiben=False) as pfb:
        bei_b = b_bestand(pfb, melden=lambda n: stand_schreiben(text="B: %s" % n))

    fehlt = sorted(m for m in bei_a if m not in bei_b)
    da = len(bei_a) - len(fehlt)
    print("\n=== ABGLEICH ===  %s → %s" % (a["name"], b["name"]))
    print("bei A mit Kennung: %d   davon bei B: %d   fehlt bei B: %d"
          % (len(bei_a), da, len(fehlt)))
    if ohne_kennung:
        print("bei A ohne Message-Id: %d — nicht abgleichbar, bleiben bei A"
              % ohne_kennung)
    print("bei B insgesamt mit Kennung: %d" % len(bei_b))
    if fehlt:
        c = {}
        for m in fehlt:
            c[bei_a[m]] = c.get(bei_a[m], 0) + 1
        print("\n--- fehlt noch, nach Ordner bei A ---")
        for name, n in sorted(c.items(), key=lambda x: -x[1]):
            print("   %5d  %s" % (n, name))
        print("\nNoch einmal uebertragen:  umzug.py plan --von=%s --nach=%s"
              % (a["id"], b["id"]))
    else:
        print("\nAlles angekommen. Bei A leeren (erst trocken):"
              "\n  umzug.py quelle-leeren --von=%s --nach=%s" % (a["id"], b["id"]))
    stand_schreiben(schritt="abgleich", laeuft=False, von=a["id"], nach=b["id"],
                    a_mit_kennung=len(bei_a), a_ohne_kennung=ohne_kennung,
                    bei_b=da, fehlt=len(fehlt), b_gesamt=len(bei_b),
                    vollstaendig=(not fehlt),
                    text="%d von %d bei B" % (da, len(bei_a)))
    return 0


# ── Stufe 5: bei A leeren ────────────────────────────────────────────────────
def loeschliste(a: dict, b: dict):
    """Welche Mails bei A duerfen weg? (Liste, Grundzaehler)

    🔴 DREI Sperren, alle drei muessen zustimmen:
       1. die Message-Id liegt bei B — LIVE nachgesehen, nicht aus dem Journal
       2. der Nummernkreis des Ordners bei A ist unveraendert
       3. die UID bei A traegt HEUTE noch genau diese Message-Id
       Faellt eine weg, bleibt die Mail liegen. Eine Mail ohne Message-Id
       kommt hier nie vor — sie hat Sperre 1 nie bestanden.
    """
    with U.verbindung(b, schreiben=False) as pfb:
        bei_b = b_bestand(pfb, melden=lambda n: stand_schreiben(text="B: %s" % n))
    gruende = {"bei B bestaetigt": 0, "nicht bei B": 0,
               "ohne Message-Id": 0, "Nummernkreis geaendert": 0,
               "UID traegt andere Mail": 0}
    weg = {}
    with U.verbindung(a, schreiben=False) as pfa:
        flaggen_alle = U._list_mit_flags(pfa)
        for name in sorted(pfa.ordner_liste()):
            if "\\noselect" in (flaggen_alle.get(name, "") or "").lower():
                continue
            if zurueckbleiber(name):
                continue
            stand_schreiben(text="A: %s" % name)
            sig = U.ordner_signatur(pfa, name)
            mids = mids_im_ordner(pfa, name)          # {mid: uid} — HEUTE gelesen
            anzahl = sig[1] if sig else len(mids)
            gruende["ohne Message-Id"] += max(0, anzahl - len(mids))
            for mid, uid in mids.items():
                if mid not in bei_b:
                    gruende["nicht bei B"] += 1
                    continue
                gruende["bei B bestaetigt"] += 1
                weg.setdefault(name, {"uidvalidity": sig[0] if sig else 0,
                                      "uids": [], "mids": []})
                weg[name]["uids"].append(uid)
                weg[name]["mids"].append(mid)
    return weg, gruende


def quelle_leeren(von_id: str, nach_id: str, freigabe: str = "",
                  scharf: bool = False) -> int:
    a, b, fehler = paar(von_id, nach_id)
    if fehler:
        print(fehler)
        stand_schreiben(schritt="leeren", laeuft=False, fehler=fehler)
        return 1
    stand_schreiben(schritt="leeren", laeuft=True, fehler="", von=a["id"],
                    nach=b["id"], phase="pruefen", scharf=bool(scharf),
                    text="Loeschliste wird geprueft")
    weg, gruende = loeschliste(a, b)
    anzahl = sum(len(v["uids"]) for v in weg.values())

    import hashlib
    abdruck = hashlib.sha256(json.dumps(
        {k: sorted(v["mids"]) for k, v in weg.items()},
        sort_keys=True).encode()).hexdigest()[:12]

    print("=== BEI A LEEREN ===  %s (Ziel war %s)" % (a["name"], b["name"]))
    print("%d Mails duerfen weg, verteilt auf %d Ordner" % (anzahl, len(weg)))
    for grund, n in sorted(gruende.items(), key=lambda x: -x[1]):
        if n:
            print("   %6d  %s" % (n, grund))
    print("\n%-38s %7s" % ("Ordner bei A", "weg"))
    for name in sorted(weg, key=lambda x: -len(weg[x]["uids"])):
        print("%-38s %7d" % (name[:38], len(weg[name]["uids"])))

    if not scharf:
        print("\nTROCKEN — es wurde nichts angefasst.")
        print("Fingerabdruck der Liste: %s" % abdruck)
        print("Scharf:  umzug.py quelle-leeren --von=%s --nach=%s "
              "--freigabe=%s --scharf" % (a["id"], b["id"], abdruck))
        stand_schreiben(schritt="leeren", laeuft=False, trocken=True,
                        abdruck=abdruck, weg=anzahl, gruende=gruende,
                        text="%d Mails duerften weg (trocken)" % anzahl)
        return 0
    if freigabe != abdruck:
        print("\nFreigabe passt nicht zur Liste.\n  Liste hat: %s\n  angegeben: %s"
              % (abdruck, freigabe or "(nichts)"))
        print("Die Liste wurde gerade neu gelesen — bei B hat sich etwas "
              "geaendert. Noch einmal trocken ansehen, dann freigeben.")
        stand_schreiben(schritt="leeren", laeuft=False,
                        fehler="Freigabe passt nicht zur Liste")
        return 1

    stand_schreiben(phase="loeschen", fortschritt=0, gesamt=anzahl)
    getan = uebersprungen = 0
    with U.verbindung(a, schreiben=True) as pfa:
        for name in sorted(weg):
            v = weg[name]
            typ, _ = pfa.m.select(pfa._zitat(name), readonly=False)
            if typ != "OK":
                print("   ! %s nicht waehlbar — bleibt unberuehrt" % name)
                uebersprungen += len(v["uids"])
                continue
            if v["uidvalidity"] and pfa.uidvalidity(name) != v["uidvalidity"]:
                print("   ! %s: Nummernkreis geaendert — bleibt unberuehrt" % name)
                uebersprungen += len(v["uids"])
                continue
            # 🔴 LETZTE Sperre: traegt die UID noch dieselbe Mail? Zwischen dem
            #    Lesen der Liste und diesem Augenblick kann bei A etwas passiert
            #    sein. Verglichen wird Kennung gegen Kennung, nicht Zahl gegen Zahl.
            jetzt = {u: m for m, u in mids_im_ordner(pfa, name).items()}
            typ, _ = pfa.m.select(pfa._zitat(name), readonly=False)
            sicher = []
            for uid, mid in zip(v["uids"], v["mids"]):
                if U.mid_bestaetigt(mid, jetzt.get(uid, "")):
                    sicher.append(uid)
                else:
                    uebersprungen += 1
            for i in range(0, len(sicher), 200):
                teil = ",".join(str(u) for u in sicher[i:i + 200])
                pfa.m.uid("store", teil, "+FLAGS", "(\\Deleted)")
            if sicher:
                pfa.m.expunge()
            getan += len(sicher)
            print("   %-34s %5d weg" % (name[:34], len(sicher)))
            stand_schreiben(fortschritt=getan, text="%s: %d weg" % (name[:24], len(sicher)))
            journal_schreiben([{"zeit": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                "von": a["id"], "nach": b["id"], "quelle": name,
                                "geloescht": len(sicher), "wie": "bei A geleert"}])
        try:
            pfa.m.select("INBOX", readonly=False)
        except Exception:
            pass
    print("\nFERTIG: %d Mails bei A geloescht, %d blieben liegen"
          % (getan, uebersprungen))
    stand_schreiben(schritt="leeren", laeuft=False, trocken=False, geloescht=getan,
                    liegen=uebersprungen,
                    text="%d bei A geloescht" % getan)
    return 0


# ── Stufe 6: staendige Umleitung ─────────────────────────────────────────────
# der Besitzer: „oder eine staendige umleitung der mails ueber die postwache
# einrichten. so das anbieter a keine mails mehr behaelt aber alles bei
# anbieter b ankommt und einsortiert wird"
#
# 🔑 Das ist keine Weiterleitung beim ANBIETER. Eine anbieterseitige
#    Weiterleitung kann die Postwache nicht einsortieren und nicht nachsehen —
#    sie schickt eine Kopie los und vergisst sie. Hier holt der Waechter die
#    Post bei A ab, legt sie bei B in den RICHTIGEN Ordner, sieht nach, dass sie
#    dort liegt, und raeumt erst dann bei A ab. Derselbe Dreischritt wie beim
#    Umzug, nur jede Minute statt einmal.
#
# 🔴 Die Umleitung ist GLOBAL, nicht je Postfach: sie beschreibt ein PAAR.
#    Als Einstellung eines Postfachs waere sie zweimal vorhanden und koennte
#    sich widersprechen.
UMLEITUNG = "umleitung.json"
DECKEL_STANDARD = 200


def umleitung_lesen() -> dict:
    v = pw.load(UMLEITUNG, {}) or {}
    if not isinstance(v, dict):
        v = {}
    ziel = str(v.get("ziel") or "posteingang").strip().lower()
    if ziel not in ("posteingang", "sortiert"):
        ziel = "posteingang"
    return {"an": bool(v.get("an")),
            "von": str(v.get("von") or "").strip(),
            "nach": str(v.get("nach") or "").strip(),
            "loeschen": bool(v.get("loeschen", True)),
            "ziel": ziel,
            "deckel": int(v.get("deckel") or DECKEL_STANDARD),
            "gesetzt": str(v.get("gesetzt") or "")}


def umleitung_setzen(an: bool, von: str = "", nach: str = "",
                     loeschen: bool = True, deckel: int = DECKEL_STANDARD,
                     ziel: str = "posteingang") -> str:
    """Umleitung ein- oder ausschalten. Gibt eine Fehlermeldung zurueck oder "".

    🔴 Eingeschaltet wird nur, was auch pruefbar ist: beide Postfaecher muessen
       existieren und verschieden sein. Eine Umleitung auf sich selbst waere
       eine Schleife, die jede Minute jede Mail neu anlegt.
    """
    alt = umleitung_lesen()
    if not an:
        pw.save(UMLEITUNG, {"an": False, "von": alt["von"], "nach": alt["nach"],
                            "loeschen": alt["loeschen"], "ziel": alt["ziel"],
                            "deckel": alt["deckel"],
                            "gesetzt": time.strftime("%Y-%m-%dT%H:%M:%S")}, 0o600)
        return ""
    a, b, fehler = paar(von or alt["von"], nach or alt["nach"])
    if fehler:
        return fehler
    pw.save(UMLEITUNG, {"an": True, "von": a["id"], "nach": b["id"],
                        "loeschen": bool(loeschen),
                        "ziel": ziel if ziel in ("posteingang", "sortiert")
                                else "posteingang",
                        "deckel": max(1, int(deckel or DECKEL_STANDARD)),
                        "gesetzt": time.strftime("%Y-%m-%dT%H:%M:%S")}, 0o600)
    return ""


def umleitung_lauf(melden=None) -> dict:
    """Ein Durchgang der Umleitung: Posteingang bei A -> einsortiert bei B.

    Wird vom Waechter bei JEDEM Lauf aufgerufen, also jede Minute. Deshalb:
    kein Inventar, kein Plan, kein Journal je Mail — nur der Posteingang, und
    nur bis zum Deckel. Was heute nicht mitkommt, kommt in der naechsten Minute.

    🔴 Die Reihenfolge ist nicht vertauschbar: anlegen, NACHSEHEN, dann bei A
       loeschen. Wer das Loeschen an den Rueckgabewert des APPEND haengt,
       loescht Post, die nie angekommen ist.
    """
    e = umleitung_lesen()
    if not e["an"]:
        return {}
    a, b, fehler = paar(e["von"], e["nach"])
    if fehler:
        pw.log("Umleitung nicht moeglich: %s" % fehler)
        return {"fehler": fehler}
    geholt = bestaetigt = geloescht = liegen = 0
    ziele_gesehen = {}
    try:
        with U.verbindung(a, schreiben=bool(e["loeschen"])) as pfa:
            saetze, _uidval = U.kopf_saetze(pfa, "INBOX")
            if not saetze:
                return {"neu": 0}
            saetze = saetze[:e["deckel"]]
            geholt = len(saetze)
            if melden:
                melden("%d Mails im Posteingang bei %s" % (geholt, a["name"]))
            with U.verbindung(b, schreiben=True) as pfb:
                praefix = praefix_b(pfb)
                vorhanden = set(pfb.ordner_liste())
                sicher = {}        # {Ordner bei A: [UIDs]} — erst nach dem Nachsehen
                angelegt = []
                for m in saetze:
                    # 🔑 ZWEI WEGE, und der Unterschied ist wichtig:
                    #
                    #    „posteingang" (Vorgabe) legt die Mail in den
                    #    POSTEINGANG bei B — genau so, als haette B sie selbst
                    #    empfangen. Den Rest macht der Waechter bei B: melden,
                    #    einsortieren, Dokumente an DocuSort, Statistik. Das ist
                    #    die ganze Maschinerie, die es schon gibt.
                    #
                    #    „sortiert" legt sie gleich in den richtigen Ordner —
                    #    fuer den Fall, dass bei B gar kein Waechter laeuft.
                    #
                    # 🔴 Der Unterschied ist kein Geschmack: eine Mail, die
                    #    direkt in „Banking.PayPal" gelegt wird, sieht der
                    #    Waechter bei B NIE — er sieht den Posteingang. Kein
                    #    Telegram, keine Dokumentenuebergabe, keine Statistik.
                    #    Darum ist die Vorgabe der Posteingang.
                    if e["ziel"] == "sortiert":
                        ziel, _grund, _neu = U.ziel_fuer_neue(m)
                        voll = bei_b_name(ziel or U.AUFFANG, pfb.trenner, praefix)
                    else:
                        voll = "INBOX"
                    if voll not in vorhanden:
                        try:
                            U.ordner_anlegen(pfb, voll)
                            vorhanden.add(voll)
                            pw.log("Umleitung: Ordner %s bei %s angelegt"
                                   % (voll, b["name"]))
                        except Exception as ex:
                            pw.log("Umleitung: Ordner %s nicht anlegbar: %s"
                                   % (voll, str(ex)[:120]))
                            liegen += 1
                            continue
                    mid = mid_normal(m.get("mid") or "")
                    if voll not in ziele_gesehen:
                        sig = U.ordner_signatur(pfb, voll)
                        ziele_gesehen[voll] = {"uidnext": sig[2] if sig else 1,
                                               "vorher": mids_im_ordner(pfb, voll)}
                    if mid and mid in ziele_gesehen[voll]["vorher"]:
                        # Liegt dort schon — dann darf sie bei A weg, ohne dass
                        # sie noch einmal angelegt wird.
                        sicher.setdefault("INBOX", []).append((m["uid"], mid))
                        bestaetigt += 1
                        continue
                    roh, marken, zeit = mail_holen(pfa, m["uid"])
                    if roh is None or len(roh) > MAX_MAIL:
                        liegen += 1
                        continue
                    try:
                        typ, _ = pfb.m.append(pfb._zitat(voll), marken, zeit, roh)
                    except Exception as ex:
                        pw.log("Umleitung: APPEND %s: %s" % (voll, str(ex)[:120]))
                        typ = "NO"
                    if typ != "OK":
                        liegen += 1
                        continue
                    angelegt.append((m["uid"], mid, voll))
                # ── NACHSEHEN, erst dann loeschen ─────────────────────────────
                gefunden = {}
                for voll, d in ziele_gesehen.items():
                    gefunden.update(mids_im_ordner(
                        pfb, voll, ab_uid=max(0, int(d["uidnext"]) - 1)))
                for uid, mid, voll in angelegt:
                    if U.mid_bestaetigt(mid, mid if (mid and mid in gefunden) else ""):
                        bestaetigt += 1
                        sicher.setdefault("INBOX", []).append((uid, mid))
                    else:
                        liegen += 1
            if e["loeschen"] and sicher.get("INBOX"):
                typ, _ = pfa.m.select("INBOX", readonly=False)
                jetzt = {u: mi for mi, u in mids_im_ordner(pfa, "INBOX").items()}
                pfa.m.select("INBOX", readonly=False)
                raus = [u for u, mi in sicher["INBOX"]
                        if U.mid_bestaetigt(mi, jetzt.get(u, ""))]
                for i in range(0, len(raus), 200):
                    pfa.m.uid("store", ",".join(str(u) for u in raus[i:i + 200]),
                              "+FLAGS", "(\\Deleted)")
                if raus:
                    pfa.m.expunge()
                geloescht = len(raus)
    except Exception as ex:
        pw.log("Umleitung abgebrochen: %s" % str(ex)[:160])
        return {"fehler": str(ex)[:160], "neu": geholt, "bestaetigt": bestaetigt,
                "geloescht": geloescht, "liegen": liegen}
    if geholt:
        pw.log("Umleitung %s → %s: %d gesehen, %d bei B bestaetigt, "
               "%d bei A geloescht, %d liegen geblieben"
               % (a["name"], b["name"], geholt, bestaetigt, geloescht, liegen))
    return {"neu": geholt, "bestaetigt": bestaetigt, "geloescht": geloescht,
            "liegen": liegen, "von": a["name"], "nach": b["name"]}


def betriebsart() -> dict:
    """Wie laufen die Postfaecher gerade? Fuer die Seite und fuer `lage`.

    „parallel" ist keine Einstellung, sondern die ABWESENHEIT einer Umleitung
    bei mehreren eingeschalteten Postfaechern — jedes wird fuer sich geprueft
    und sortiert. Das war schon in 3.0.0 so und bleibt der Normalfall.
    """
    faecher = pw.postfaecher()
    an = [f for f in faecher if f.get("an")]
    e = umleitung_lesen()
    if e["an"]:
        art = "umleitung"
    elif len(an) >= 2:
        art = "parallel"
    else:
        art = "einzeln"
    return {"art": art, "postfaecher": [{"id": f["id"], "name": f["name"],
                                         "an": bool(f.get("an"))} for f in faecher],
            "umleitung": e, "stand": stand_lesen()}


def lage() -> int:
    b = betriebsart()
    namen = {f["id"]: f["name"] for f in b["postfaecher"]}
    print("Betriebsart: %s" % b["art"])
    for f in b["postfaecher"]:
        print("   %-12s %-40s %s" % (f["id"], f["name"], "an" if f["an"] else "aus"))
    e = b["umleitung"]
    if e["an"]:
        print("Umleitung: %s → %s (%s), bei A loeschen: %s, "
              "Deckel %d/Lauf, gesetzt %s"
              % (namen.get(e["von"], e["von"]), namen.get(e["nach"], e["nach"]),
                 "in den Posteingang, Waechter sortiert" if e["ziel"] == "posteingang"
                 else "gleich einsortiert",
                 "ja" if e["loeschen"] else "nein", e["deckel"], e["gesetzt"] or "?"))
    else:
        print("Umleitung: aus")
    s = b["stand"]
    if s:
        print("Letzte Stufe: %s%s — %s (%s)"
              % (s.get("schritt", "?"), " (laeuft)" if s.get("laeuft") else "",
                 s.get("text", ""), s.get("zeit", "")))
    return 0


def main(argv) -> int:
    befehle = ("pruefen", "plan", "uebertragen", "abgleich", "quelle-leeren",
               "umleitung", "lage")
    if len(argv) < 2 or argv[1] not in befehle:
        print(__doc__)
        print("Aufruf:")
        print("  umzug.py pruefen       --von=<id> --nach=<id>")
        print("  umzug.py plan          --von=<id> --nach=<id> [--grenze=N] "
              "[--seit=JJJJ-MM-TT] [--voll]")
        print("  umzug.py uebertragen   --freigabe=<abdruck> [--grenze=N] [--trocken]")
        print("  umzug.py abgleich      --von=<id> --nach=<id>")
        print("  umzug.py quelle-leeren --von=<id> --nach=<id> [--freigabe=<abdruck>] [--scharf]")
        print("  umzug.py umleitung     --an --von=<id> --nach=<id> "
              "[--nicht-loeschen] [--gleich-sortieren] [--deckel=N]")
        print("  umzug.py umleitung     --aus")
        print("  umzug.py lage")
        return 2
    args = {}
    for a in argv[2:]:
        if a.startswith("--"):
            k, _, v = a[2:].partition("=")
            args[k] = v or "1"
    von, nach = args.get("von", ""), args.get("nach", "")
    grenze = int(args.get("grenze", 0) or 0)
    if argv[1] == "pruefen":
        return pruefen(von, nach)
    if argv[1] == "plan":
        return plan(von, nach, grenze, args.get("seit", ""), "voll" in args)
    if argv[1] == "uebertragen":
        return uebertragen(args.get("freigabe", ""), grenze, "trocken" in args)
    if argv[1] == "abgleich":
        return abgleich(von, nach)
    if argv[1] == "quelle-leeren":
        # 🔴 Vorgabe ist TROCKEN. Wer bei A loescht, sagt es ausdruecklich —
        #    und braucht dazu den Fingerabdruck der Liste, die er gelesen hat.
        return quelle_leeren(von, nach, args.get("freigabe", ""), "scharf" in args)
    if argv[1] == "umleitung":
        if "aus" in args:
            umleitung_setzen(False)
            print("Umleitung aus.")
            return 0
        fehler = umleitung_setzen(True, von, nach,
                                  loeschen="nicht-loeschen" not in args,
                                  deckel=int(args.get("deckel", DECKEL_STANDARD) or
                                             DECKEL_STANDARD),
                                  ziel=("sortiert" if "gleich-sortieren" in args
                                        else "posteingang"))
        if fehler:
            print(fehler)
            return 1
        return lage()
    if argv[1] == "lage":
        return lage()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
