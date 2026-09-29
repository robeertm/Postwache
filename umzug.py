#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""umzug.py — bring a whole mailbox to another provider.

Two providers are named (which already works), then they can
run in parallel or say: transfer mail from provider A to provider B and sort
it straight away into a nice structure according to the content of the mail; once
the migration is finished you can delete everything at provider A or set up a
permanent redirection of the mail through the Postwache, so that provider A keeps
no mail but everything arrives at provider B and is filed there.

Six stages, each callable on its own, each harmless by itself:

  1. pruefen        both providers reachable? B writable? what is already there?
  2. plan           WHAT goes WHERE — without touching a single mail
  3. uebertragen    read at A, create at B, CHECK. A stays untouched.
  4. abgleich       has everything really arrived at B?
  5. quelle-leeren  only now, with its own approval, only what is provably at B
  6. umleitung      from now on continuously: new post at A -> filed at B

The filing is NOT reinvented: `umzug` asks the same `umbau` that took his mailbox
from 53 to 132 folders on 2026-09-27. A mail lands at B where it would have landed
at A — only without the intermediate step.

🔴 THE LIMIT OF THIS PROGRAM: two providers are two servers, and between two
   servers there is no COPY. Every mail is READ at A (BODY.PEEK) and CREATED ANEW
   at B (APPEND). That is a copy, not a move — afterwards the mail exists TWICE.
   Exactly why „quelle-leeren“ is a stage of its own with its own approval and not
   the silent end of „uebertragen“.

🔴 IDENTITY IS THE MESSAGE-ID, NOT THE UID. UIDs apply per server and per
   folder; after the APPEND the same mail has a different one at B. Whoever wants
   to delete at A has to be able to FIND IT AGAIN at B — and whoever has no
   Message-Id cannot be found again. A mail without a Message-Id is transferred and
   NEVER deleted at A. Better twice than gone.

🔴 AN EMPTY SEARCH RESULT IS NOT A CONFIRMATION. The same trap as in the
   restructuring on 2026-09-27: „nothing found“ does not mean „correct“. A mail is
   confirmed only when its Message-Id was ACTUALLY READ at B. Hence
   `U.mid_bestaetigt()` — the same pure function, the same test.

🔴 SPECIAL FOLDERS GO BY THE FLAG, NOT BY THE NAME. Sent mail at A is called
   „Sent Items“, at B perhaps „Gesendet“ or „INBOX.Sent“. Map by name and you
   create a second sent folder at B. `\\Sent` -> `\\Sent`.

🔴 TRASH AND SPAM DO NOT COME ALONG. They are intent, not content. Sent items
   and drafts do come along — that is his own record. The archive comes along and
   is SORTED on the way, as in the restructuring: he wanted archived mail sorted
   and taken out of the archive.
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

# ── What does NOT come along ────────────────────────────────
# 🔴 Narrower than `umbau.TABU`! The restructuring must not TOUCH sent items,
#    because they are meant to stay where they are. In a migration they are meant
#    to COME ALONG — just unsorted, into the sent folder at B. What stays behind
#    is only what he has already thrown away or what the provider filed as spam.
BLEIBT_ZURUECK = {
    "trash", "papierkorb", "deleted items", "gelöschte objekte",
    "spam", "junk", "junk e-mail", "unerwünscht",
}

# Flags whose content goes 1:1 into the folder with the same flag at B instead of
# into the content structure. `\Archive` is deliberately NOT among them.
UNSORTIERT_UEBER = ("Sent", "Drafts")

# Only these flags come along. `\Deleted` would be a delete order at B, `\Recent`
# an APPEND may not set at all, and custom keywords ($Label1) are rejected by many
# servers and take the whole APPEND down with them.
MARKEN_ERLAUBT = ("\\Seen", "\\Answered", "\\Flagged", "\\Draft")

BLOCK = 40          # this many mails per FETCH round; one mail can be megabytes
MAX_MAIL = 40 * 1024 * 1024   # 40 MB — above that it is reported, not transferred


def out_pfad(name: str) -> str:
    return U.out_pfad(name)


# ── Status for the page ──────────────────────────────────────
# Built like `umbau.stand_schreiben`, with a file of its own: a migration can take
# hours, and somebody has to see where it stands without reading a log —
# how far the folder deletion has got, for instance.
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
    """Every transferred mail with its origin, target and Message-Id.

    🔑 This is the only bridge between two servers: at A there is one UID, at B
       another, and all they share is the Message-Id. Without this journal
       „quelle-leeren“ could not know WHICH mail may go at A.
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


# ── The two providers ──────────────────────────────────────
def paar(von_id: str, nach_id: str):
    """The two accounts — or an error message that names the reason.

    🔴 A and B must not be the same mailbox. A migration onto itself would be an
       APPEND of every mail into its own folder: every mail twice, and
       „quelle-leeren“ would afterwards check both halves of the same stock against
       each other. The ADDRESS is compared, not the id — two entries with the same
       address are the same mailbox.
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
    """Trash/spam — the folder AND everything below it.

    🔴 EVERY path segment is checked, not the whole name. „INBOX.Spam.Alt“ neither
       ends with „spam“ nor begins with it — so a prefix or suffix comparison would
       not have taken it along. The leading „INBOX“ is dropped, because at many
       providers it stands in front of every folder.
    """
    n = (name or "").lower().replace("/", ".")
    stuecke = [t for t in n.split(".") if t]
    if stuecke and stuecke[0] == "inbox":
        stuecke = stuecke[1:]
    return any(t in BLEIBT_ZURUECK for t in stuecke)


def flaggen_karte(pf) -> dict:
    """{flag without backslash (lower case): folder name} — for the special
    folders.

    If the same folder comes with two flags, the first applies; if a flag comes
    twice, the FIRST folder applies. Both are rare and both would otherwise be a
    silent decision.
    """
    raus = {}
    for name, flaggen in U._list_mit_flags(pf).items():
        for s in U.SONDER_FLAGS:
            marke = s.lower()
            if ("\\" + marke) in (flaggen or "").lower() and marke not in raus:
                raus[marke] = name
    return raus


def marken_saeubern(roh: str) -> str:
    """Only the four flags that an APPEND tolerates everywhere."""
    vorhanden = (roh or "").lower()
    behalten = [m for m in MARKEN_ERLAUBT if m.lower() in vorhanden]
    return ("(%s)" % " ".join(behalten)) if behalten else None


def mid_normal(mid: str) -> str:
    """Make a Message-Id comparable. Servers return it with and without angle
    brackets, some with spaces — the core is what is compared."""
    s = (mid or "").strip()
    if s.startswith("<") and s.endswith(">"):
        s = s[1:-1]
    return s.strip().lower()


def mids_im_ordner(pf, name: str, ab_uid: int = 0) -> dict:
    """{Message-Id: UID} of a folder. Read-only, BODY.PEEK, in blocks.

    🔑 Reading goes through FETCH, not through `SEARCH HEADER Message-Id`. A
       search would be one round trip PER MAIL and would have to put the Message-Id
       in quotes — with an id that contains a quote the search breaks or, worse,
       silently finds the wrong thing. A block FETCH reads 300 ids at once and has
       no quoting problem.

    🔴 The `finally` returns to INBOX — the same lesson as everywhere: a change
       has to return to where it came from.
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
    """What is already at B? {Message-Id: folder} across ALL folders.

    🔑 This is the duplicate lock: a mail whose id already lies somewhere at B is
       not created a second time. Without it, an aborted and restarted migration
       would be a mailbox with everything twice — and a `uebertragen` that gets
       pressed twice is the normal case, not the exception.

    🔴 Counting goes across all folders, not only the planned target folder.
       Otherwise a later refined rule creates the same mail a second time,
       just elsewhere — and both halves look right.
    """
    raus = {}
    # 🔴 Ask ONCE, not per folder. `_list_mit_flags` is a LIST on the server —
    #    inside the loop that would be 132 requests for 132 folders, for an answer
    #    that does not change during the run.
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
                    # 🔑 Writable? A migration that only notices at mail 12,000
                    #    that B may not create folders has worked for two hours
                    #    and achieved nothing. Hence a probe folder: create it,
                    #    look it up in LIST, remove it.
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
    """Headers of ALL mails at A — without touching one.

    🔴 A file of its OWN, not `inventar.json.gz`. That one belongs to the
       restructuring and tells it which folder USED TO have post — without it
       `umbau.py ordner` may not remove a single folder. Overwrite it here and you
       take away the restructuring's memory.

    Unchanged folders are carried over from the last run (signature from
    UIDVALIDITY/MESSAGES/UIDNEXT, one round trip per folder).
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

    # Where do the special folders belong at B? By the FLAG, not by the name.
    stand_schreiben(phase="ziele", text="Sonderordner bei B werden erfragt")
    with U.verbindung(b, schreiben=False) as pfb:
        sonder_b = flaggen_karte(pfb)
        trenner_b = pfb.trenner
    print("Sonderordner bei B: %s"
          % (", ".join("\\%s=%s" % (k.capitalize(), v)
                       for k, v in sorted(sonder_b.items())) or "keine"))

    # 🔑 The structure is learned with THE SAME teacher as in the restructuring —
    #    nothing reinvented. 🔴 Only one detail has to differ: when learning,
    #    `umbau` skips the inbox, because in a grown mailbox only the remainder
    #    lies there. In a MIGRATION the inbox can be the entire stock (a provider
    #    without folders) — and then the teacher would have nothing to read. So it
    #    gets to see the inbox under a neutral name; nothing changes for the mails.
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
                # At B the folder with the same flag; if B has none, the folder
                # keeps its name — a new „Gesendet“ would be a guess.
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
    """Where does THIS server create folders: at the root or under INBOX?

    🔴 Some providers answer with the personal namespace `""` — folders belong at
       the ROOT. Others answer `"INBOX."`, and there a folder at the root is not
       visible. So the namespace is ASKED FOR (NAMESPACE) and not guessed; if the
       server does not answer, the root stands — which is immediately visible as a
       missing folder, while a wrong „INBOX.“ silently builds a folder inside a
       folder.
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
    """The canonical path `Banking.PayPal` in THIS server's spelling.

    🔴 In the restructuring the dot is the SEPARATOR, not part of a name
       (`marke_aus_domain` throws dots out of folder names). Pass the path unchanged
       to a server whose separator is „/“ and you create ONE folder called
       „Banking.PayPal“ instead of „PayPal“ under „Banking“.
    """
    teile = [t for t in (ziel or "").split(".") if t]
    voll = trenner.join(teile)
    if praefix and not voll.startswith(praefix):
        voll = praefix + voll
    return voll


def mail_holen(pf, uid: int):
    """(raw mail, flags, timestamp) of a mail — with PEEK, so that it stays unread
    at A. Returns (None, None, None) when the UID delivers nothing.

    🔴 Line endings are brought to CRLF. The wire format demands it, and a server
       that accepts a mail with bare LF appends the next header to the body — a
       mistake that only shows up weeks later.
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
    """The print of the current migration plan — the approval to transfer.

    A function of its own, so that the page and the command line use THE SAME
    number. Two places computing the same print themselves are two chances to
    compute it differently.
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

            # 🔑 What is already at B? Without this list a second run is a
            #    mailbox with everything twice.
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
                # 🔴 UIDVALIDITY lock: if the folder has a new number space, the
                #    plan's UIDs point at different mails. Then NOTHING is
                #    transferred but reported.
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

                # ── CHECK ─────────────────────────────────────
                # 🔴 Only now does a mail count as arrived. The APPEND's return
                #    value only says the server ACCEPTED the order. It is
                #    confirmed once its id has been READ at B — an empty answer
                #    is no confirmation (2026-09-27).
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
    """Has everything really arrived at B? Counted LIVE, not from the journal.

    🔑 The journal says what the program DID. This stage says what IS REALLY
       THERE. Only the second question may decide about „delete at A“ — a journal
       also survives a mailbox that has been reset in the meantime.
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
    """Which mails at A may go? (list, reason counters)

    🔴 THREE locks, all three have to agree:
       1. the Message-Id lies at B — checked LIVE, not from the journal
       2. the folder's number space at A is unchanged
       3. the UID at A still carries exactly this Message-Id TODAY
       If one fails, the mail stays. A mail without a Message-Id never appears
       here — it never passed lock 1.
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
            # 🔴 LAST lock: does the UID still carry the same mail? Between
            #    reading the list and this moment something may have happened at
            #    A. Id is compared against id, not number against number.
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


# ── Stage 6: permanent redirection ────────────────────────────
# Or set up a permanent redirection of the mail through the Postwache, so
# that provider A keeps no mail but everything arrives at provider B and is filed
# there.
#
# 🔑 This is not a forward at the PROVIDER. A provider-side forward cannot be
#    filed and cannot be checked by the Postwache — it sends a copy off and
#    forgets it. Here the watchman collects the post at A, puts it into the RIGHT
#    folder at B, checks that it is there, and only then clears up at A. The same
#    three steps as in the migration, only every minute instead of once.
#
# 🔴 The redirection is GLOBAL, not per mailbox: it describes a PAIR. As a
#    setting of one mailbox it would exist twice and could contradict itself.
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
    """Switch the redirection on or off. Returns an error message or "".

    🔴 Only what can be verified is switched on: both mailboxes have to exist and
       be different. A redirection onto itself would be a loop creating every mail
       anew every minute.
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
    """One pass of the redirection: inbox at A -> filed at B.

    Called by the watchman on EVERY run, that is every minute. Hence: no inventory,
    no plan, no journal per mail — only the inbox, and only up to the cap. What does
    not come along today comes along in the next minute.

    🔴 The order cannot be swapped: create, CHECK, then delete at A. Hang the
       deletion on the APPEND's return value and you delete post that never
       arrived.
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
                sicher = {}        # {folder at A: [UIDs]} — only after the check
                angelegt = []
                for m in saetze:
                    # 🔑 TWO ROUTES, and the difference matters:
                    #
                    #    „posteingang“ (the default) puts the mail into B's
                    #    INBOX — exactly as if B had received it itself. The rest
                    #    is done by the watchman at B: report, file, documents to
                    #    DocuSort, statistics. That is the whole machinery that
                    #    already exists.
                    #
                    #    „sortiert“ puts it straight into the right folder — for
                    #    the case where no watchman runs at B at all.
                    #
                    # 🔴 The difference is not a matter of taste: a mail put
                    #    directly into „Banking.PayPal“ is NEVER seen by the
                    #    watchman at B — it looks at the inbox. No Telegram, no
                    #    document handover, no statistics. That is why the default
                    #    is the inbox.
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
                        # Already lies there — then it may go at A without
                        # being created again.
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
                # ── CHECK, and only then delete ──────────────────────
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
    """How are the mailboxes running right now? For the page and for `lage`.

    „parallel“ is not a setting but the ABSENCE of a redirection with several
    mailboxes switched on — each is checked and sorted for itself. That was already
    so in 3.0.0 and remains the normal case.
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
        # 🔴 The default is DRY. Whoever deletes at A says so expressly — and
        #    needs the fingerprint of the list they have read to do it.
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
