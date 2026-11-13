#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Postwache — the conversation view (5.32.0).

The goal: every mail shows its whole conversation — the owner's own replies
and forwards included, wherever they were filed — one message below the other,
however many there are.

🔴 MEASURED BEFORE (13.11.2026, real mailbox, 11 605 mails, read-only, counts
only): 1 188 mails carry In-Reply-To/References; the owner's own mail lies in
the sent folder (340) AND in filed folders (1 068 — mail sent from the phone is
filed by the watchman like everything else). Of 591 own replies and forwards,
427 can be tied to their conversation by the headers, 51 only by subject and
people, 113 not at all. 1 758 Message-Ids exist in more than one folder.

🔑 So the view is built from the search index — it already holds every folder,
and with schema 4 also the reference chain, the subject without „Re:/AW:/Fwd:",
the people and whether a row was delivered by the provider. Nothing here talks
to the mail server: the skeleton of a conversation is a lookup in memory plus
one query. The bodies are fetched when a message is opened, through the same
door as every other mail (`klient_mail`), with the same three walls.

🔴 Two kinds of membership, never mixed silently:
- by the headers (JWZ-style: a mail and everything its chain names, and every
  mail naming it — robust against missing links, because a dangling reference
  still joins the two mails that both name it), and
- „probably part of it": same subject core, a shared person, a time window,
  AND one side a reply/forward or written by the owner. Those rows are marked.
"""
from __future__ import annotations

import email.utils
import json
import re
import threading
import time

# ── Subjects ────────────────────────────────────────────────────────────
# Reply and forward prefixes in the languages mail programs actually write.
# „R:" is left out on purpose — too many real subjects begin with a letter
# and a colon.
_ANTWORT = ("re", "aw", "antw", "antwort", "rif", "res", "sv", "vs", "odp",
            "ynt", "atb", "vá", "ref", "réf", "回复", "答复")
_WEITER = ("fwd", "fw", "wg", "weitergeleitet", "tr", "rv", "i", "enc",
           "doorst", "vb", "pd", "trs", "转发", "fwd2")
_PRAEFIX = re.compile(
    r"^\s*(?:\[[^\]]{1,40}\]\s*)?(%s)\s*(?:\[\d+\]|\(\d+\)|\^\d+)?\s*[:：]\s*"
    % "|".join(sorted((re.escape(x) for x in _ANTWORT + _WEITER), key=len, reverse=True)),
    re.I)
_LISTENMARKE = re.compile(r"^\s*\[[^\]]{1,40}\]\s*")

KORB = ("papierkorb", "spam", "entwuerfe")     # never part of a conversation
FENSTER_S = 60 * 86400        # „probably": the time window around the thread
GROESSTE = 200                # messages shown at most (the newest)
BEZUG_MAX = 40                # reference ids kept per row


def betreff_kern(betreff: str) -> tuple:
    """(folded core, kind) — kind 0 = none, 1 = reply, 2 = forward. Several
    prefixes in any order are peeled off; the first one decides the kind."""
    b, art = str(betreff or "").strip(), 0
    for _ in range(12):
        m = _PRAEFIX.match(b)
        if not m:
            break
        if not art:
            art = 2 if m.group(1).lower() in _WEITER else 1
        b = b[m.end():].strip()
    b = _LISTENMARKE.sub("", b)
    try:
        from suchindex import falten
        kern = falten(b)
    except Exception:                                 # noqa: BLE001
        kern = b.lower()
    return re.sub(r"\s+", " ", kern).strip()[:300], art


def ids_aus(text: str) -> list:
    """Message-Ids out of In-Reply-To/References — lower case, without the
    angle brackets, each once. A very long chain keeps its first id (the root)
    and the newest ones, the way RFC 5322 says a chain may be shortened."""
    raus, gesehen = [], set()
    for x in re.findall(r"<([^<>\s]{1,250})>", str(text or "")):
        x = x.lower()
        if x not in gesehen:
            gesehen.add(x)
            raus.append(x)
    if len(raus) > BEZUG_MAX:
        raus = raus[:1] + raus[-(BEZUG_MAX - 1):]
    return raus


def mid_norm(mid: str) -> str:
    m = re.search(r"<([^<>\s]{1,250})>", str(mid or ""))
    return (m.group(1) if m else str(mid or "").strip()).lower()[:250]


def adressen_kopf(msg) -> str:
    """From, To, Cc — the people of a mail, as lower-case addresses."""
    try:
        from postwache import dekodieren
    except Exception:                                 # noqa: BLE001
        dekodieren = str
    werte = [dekodieren(str(v)) for k in ("From", "To", "Cc")
             for v in (msg.get_all(k) or [])]
    adr = {a.lower() for _, a in email.utils.getaddresses(werte) if a and "@" in a}
    return " ".join(sorted(adr))[:2000]


_ADRESSE = re.compile(r"[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}")


def teilnehmer_mit_text(teilnehmer: str, text: str) -> str:
    """A forward's participants plus the addresses its quoted header names."""
    alle = set((teilnehmer or "").split())
    for a in _ADRESSE.findall((text or "")[:20000])[:20]:
        alle.add(a.lower())
    return " ".join(sorted(alle))[:3000]


# ── The map: which row belongs to which conversation ────────────────────
class _Karte:
    def __init__(self, schluessel):
        self.schluessel = schluessel
        self.wurzel = {}          # row id → root node
        self.mitglieder = {}      # root node → [row ids]
        self.groesse = {}         # root node → distinct messages (no trash/spam/drafts)
        self.knoten = {}          # normalised message id → root node
        self.gebaut_ms = 0.0


_KARTEN = {}
_K_SCHLOSS = threading.Lock()


def _si():
    import suchindex
    return suchindex


def _schluessel(pf_id: str, db) -> tuple:
    p = _si()._posten(pf_id)
    zahl, hoch = db.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM mails "
                            "WHERE weg=0").fetchone()
    return (p.gen, zahl, hoch)


def _bauen(db, schluessel) -> _Karte:
    t0 = time.time()
    k = _Karte(schluessel)
    eltern = {}

    def finde(x):
        wurzel = x
        while True:
            e = eltern.get(wurzel, wurzel)
            if e == wurzel:
                break
            wurzel = e
        while x != wurzel:                         # path compression
            e = eltern.get(x, x)
            eltern[x] = wurzel
            x = e
        return wurzel

    zeilen = db.execute(
        "SELECT m.id, m.msgid, m.bezug, COALESCE(o.rolle, ''), m.flaggen FROM mails m "
        "LEFT JOIN ordner o ON o.name=m.ordner WHERE m.weg=0").fetchall()
    knoten_je_zeile = {}
    for rid, mid, bezug, _rolle, _fl in zeilen:
        eigen = mid_norm(mid) or ("#%d" % rid)
        knoten_je_zeile[rid] = eigen
        wa = finde(eigen)
        for b in (bezug or "").split():
            wb = finde(b)
            if wa != wb:
                eltern[wb] = wa
    zaehlen = {}
    for rid, _mid, _bezug, rolle, fl in zeilen:
        w = finde(knoten_je_zeile[rid])
        k.wurzel[rid] = w
        k.mitglieder.setdefault(w, []).append(rid)
        if rolle not in KORB and "\\draft" not in (fl or ""):
            zaehlen.setdefault(w, set()).add(knoten_je_zeile[rid])
    for w, s in zaehlen.items():
        k.groesse[w] = len(s)
    for rid, eigen in knoten_je_zeile.items():
        k.knoten[eigen] = k.wurzel[rid]
    k.gebaut_ms = round((time.time() - t0) * 1000, 1)
    return k


def karte(pf_id: str, db) -> _Karte:
    """The conversation map of one mailbox — built once, again only when the
    index changed (the keeper's generation, the row count, the newest row)."""
    s = _schluessel(pf_id, db)
    with _K_SCHLOSS:
        k = _KARTEN.get(pf_id)
        if k is not None and k.schluessel == s:
            return k
    neu = _bauen(db, s)
    with _K_SCHLOSS:
        _KARTEN[pf_id] = neu
    return neu


def vergessen(pf_id: str = "") -> None:
    with _K_SCHLOSS:
        if pf_id:
            _KARTEN.pop(pf_id, None)
        else:
            _KARTEN.clear()


# ── Who is „me" ──────────────────────────────────────────────────────────
def _eigen_pruefer(pf_id: str):
    """A row is the owner's own mail when it lies in the sent folder, when its
    sender is one of the owner's addresses, or when the sender is on the main
    address's domain AND the provider did not deliver it (no envelope header:
    a copy that was filed, not a mail that arrived — a family member on the
    same domain sends mail that DOES arrive)."""
    try:
        import klient as KL
        eigene = KL.eigene_adressen(pf_id)
        haupt = str((KL._zugang(pf_id) or {}).get("adresse") or "").lower()
    except Exception:                                 # noqa: BLE001
        eigene, haupt = set(), ""
    dom = haupt.rsplit("@", 1)[-1] if "@" in haupt else ""

    def eigen(adresse: str, rolle: str, zustell: int) -> bool:
        a = (adresse or "").lower()
        if rolle == "gesendet" or (a and a in eigene):
            return True
        return bool(dom and a.endswith("@" + dom) and not zustell)
    return eigen, eigene, dom


# ── The skeleton of one conversation ────────────────────────────────────
_SPALTEN = ("m.id, m.ordner, m.uid, m.datum, m.msgid, m.flaggen, m.satz, m.bezug, "
            "m.fbetreff, m.praefix, m.teilnehmer, m.zustell, COALESCE(o.rolle, ''), "
            "COALESCE(o.kurz, m.ordner), COALESCE(o.zeige, m.ordner)")


def _zeilen(db, ids) -> list:
    raus = []
    ids = list(ids)
    for i in range(0, len(ids), 500):
        stueck = ids[i:i + 500]
        raus += db.execute("SELECT %s FROM mails m LEFT JOIN ordner o ON o.name=m.ordner "
                           "WHERE m.weg=0 AND m.id IN (%s)" % (_SPALTEN, ",".join("?" * len(stueck))),
                           stueck).fetchall()
    return raus


def _zeit(satz: dict, datum) -> float:
    """The order of a conversation is the order of WRITING: the Date header.
    An imported archive has an arrival time of the day it was imported."""
    try:
        from datetime import datetime
        z = satz.get("zeit")
        if z:
            return datetime.fromisoformat(z).timestamp()
    except (TypeError, ValueError):
        pass
    return float(datum or 0)


def _gegenueber(teilnehmer: str, eigene: set, dom: str) -> set:
    return {a for a in (teilnehmer or "").split()
            if a not in eigene and not (dom and a.endswith("@" + dom))}


def geruest(pf_id: str, ordner: str, uid: int, msgid: str = "",
            vermutlich: bool = True) -> dict:
    """Every message of the conversation the given mail belongs to — header
    data only, oldest first. Never a server command."""
    SI = _si()
    t0 = time.time()
    if not SI.eingeschaltet():
        return {"ok": True, "im_index": False, "nachrichten": []}
    try:
        leser = SI._Leser(pf_id)
        db = leser.__enter__()
    except FileNotFoundError:
        return {"ok": True, "im_index": False, "nachrichten": []}
    try:
        return _geruest(pf_id, db, ordner, uid, msgid, vermutlich, t0)
    finally:
        leser.__exit__(None, None, None)


def _geruest(pf_id, db, ordner, uid, msgid, vermutlich, t0) -> dict:
    start = db.execute("SELECT id FROM mails WHERE ordner=? AND uid=? AND weg=0",
                       (ordner, int(uid or 0))).fetchone()
    if start is None and msgid:
        # A mail moved since the last comparison is the same mail.
        start = db.execute("SELECT id FROM mails WHERE msgid=? AND weg=0 "
                           "ORDER BY id DESC LIMIT 1", (msgid.strip(),)).fetchone()
    if start is None:
        return {"ok": True, "im_index": False, "nachrichten": []}
    start = start[0]
    k = karte(pf_id, db)
    w = k.wurzel.get(start)
    sicher = set(k.mitglieder.get(w, [start]))
    zeilen = {z[0]: z for z in _zeilen(db, sicher)}
    eigen, eigene, dom = _eigen_pruefer(pf_id)

    def lebt(z) -> bool:
        return z[0] == start or (z[12] not in KORB and "\\draft" not in (z[5] or ""))

    kern = {z[0]: z for z in zeilen.values() if lebt(z)}
    vermutet = set()
    if vermutlich and kern:
        vermutet = _vermutet(db, k, kern, eigen, eigene, dom)
        if vermutet:
            for z in _zeilen(db, vermutet):
                if lebt(z):
                    kern.setdefault(z[0], z)
    # One message, several folders: shown once, the other folders named.
    gruppen = {}
    for rid, z in kern.items():
        gruppen.setdefault(mid_norm(z[4]) or ("#%d" % rid), []).append(z)
    nachrichten = []
    for schl, liste in gruppen.items():
        liste.sort(key=lambda z: (z[0] != start, z[12] == "gesendet", -z[0]))
        z = liste[0]
        satz = json.loads(z[6] or "{}")
        ist_eigen = any(eigen(satz.get("adresse") or "", y[12], y[11]) for y in liste)
        fl = " ".join(y[5] or "" for y in liste)
        richtung = ("weitergeleitet" if ist_eigen and z[9] == 2 else
                    "gesendet" if ist_eigen else "empfangen")
        nachrichten.append({
            "id": z[0], "ordner": z[1], "uid": z[2], "zeige": z[13], "pfad": z[14],
            "auch": sorted({y[13] for y in liste[1:] if y[13] != z[13]}),
            "zeit": satz.get("zeit") or "", "_t": _zeit(satz, z[3]),
            "von": satz.get("von") or "", "adresse": satz.get("adresse") or "",
            "an": satz.get("an") or "", "kopie": satz.get("kopie") or "",
            "betreff": satz.get("betreff") or "", "auszug": satz.get("auszug") or "",
            "anhang": int(satz.get("anhang") or 0), "message_id": satz.get("message_id") or "",
            "richtung": richtung,
            "vermutlich": not any(y[0] in sicher for y in liste),
            "aktuell": any(y[0] == start for y in liste),
            "gelesen": "\\seen" in fl, "beantwortet": "\\answered" in fl,
            "weitergeleitet": "$forwarded" in fl,
        })
    nachrichten.sort(key=lambda n: (n["_t"], n["id"]))
    gekuerzt = 0
    if len(nachrichten) > GROESSTE:
        # Keep the newest — and the opened one, wherever it stands.
        gekuerzt = len(nachrichten) - GROESSTE
        bleibt = nachrichten[-GROESSTE:]
        if not any(n["aktuell"] for n in bleibt):
            bleibt = [n for n in nachrichten if n["aktuell"]] + bleibt[1:]
        nachrichten = bleibt
    for n in nachrichten:
        n.pop("_t", None)
        n.pop("id", None)
    return {"ok": True, "im_index": True, "nachrichten": nachrichten,
            "n": len(nachrichten), "gekuerzt": gekuerzt,
            "vermutlich": sum(1 for n in nachrichten if n["vermutlich"]),
            "stand": "%d.%d.%d" % k.schluessel,
            "ms": round((time.time() - t0) * 1000, 1)}


def _vermutet(db, k: _Karte, kern: dict, eigen, eigene: set, dom: str) -> set:
    """Rows that probably belong — by subject, people and time, never by the
    subject alone. Returns row ids (whole header-conversations of a match)."""
    kerne = {z[8] for z in kern.values() if z[8] and len(z[8]) >= 3}
    if not kerne:
        return set()
    zeiten = [_zeit(json.loads(z[6] or "{}"), z[3]) for z in kern.values()]
    von, bis = min(zeiten) - FENSTER_S, max(zeiten) + FENSTER_S
    platz = ",".join("?" * len(kerne))
    kandidaten = db.execute(
        "SELECT %s FROM mails m LEFT JOIN ordner o ON o.name=m.ordner "
        "WHERE m.weg=0 AND m.fbetreff IN (%s) LIMIT 600" % (_SPALTEN, platz),
        list(kerne)).fetchall()
    bekannt = {mid_norm(z[4]) for z in kern.values()}
    wurzeln_kern = {k.wurzel.get(r) for r in kern}

    def seite(z):
        satz = json.loads(z[6] or "{}")
        return (eigen(satz.get("adresse") or "", z[12], z[11]),
                _gegenueber(z[10], eigene, dom), int(z[9] or 0))
    kern_seiten = [seite(z) for z in kern.values()]
    raus = set()
    for z in kandidaten:
        if z[0] in kern or z[12] in KORB or "\\draft" in (z[5] or ""):
            continue
        if k.wurzel.get(z[0]) in wurzeln_kern or mid_norm(z[4]) in bekannt:
            continue
        t = _zeit(json.loads(z[6] or "{}"), z[3])
        if not (von <= t <= bis):
            continue
        y_eigen, y_leute, y_art = seite(z)
        for x_eigen, x_leute, x_art in kern_seiten:
            # 🔴 A shared person is required ALWAYS — a stranger's mail with the
            # same subject is not part of this conversation. And one side must
            # be a reply/forward or the owner's: two monthly invoices with the
            # same subject from the same sender are two letters, not a thread.
            if not (y_leute & x_leute):
                continue
            if y_eigen != x_eigen or y_art or x_art:
                raus.update(k.mitglieder.get(k.wurzel.get(z[0]), [z[0]]))
                break
    return raus


def ordner_fertig(pf_id: str, ordner: str) -> bool:
    """Has the index read this folder completely at least once?"""
    try:
        with _si()._Leser(pf_id) as db:
            r = db.execute("SELECT fertig FROM ordner WHERE name=?", (ordner,)).fetchone()
        return bool(r and r[0])
    except Exception:                                 # noqa: BLE001
        return False


# ── Numbers for the list ────────────────────────────────────────────────
def zahlen(pf_id: str, mids: list) -> dict:
    """{message id as given: messages in its conversation} — only where there
    are at least two. By the headers only: the list does not guess."""
    SI = _si()
    if not SI.eingeschaltet() or not mids:
        return {}
    try:
        with SI._Leser(pf_id) as db:
            k = karte(pf_id, db)
    except FileNotFoundError:
        return {}
    raus = {}
    for roh in mids[:600]:
        w = k.knoten.get(mid_norm(roh))
        n = k.groesse.get(w, 0) if w is not None else 0
        if n >= 2:
            raus[str(roh)] = n
    return raus


def kurz(pf_id: str, ordner: str, uid: int, msgid: str = "") -> list:
    """The read door's view: header data of the conversation, nothing else."""
    g = geruest(pf_id, ordner, uid, msgid, vermutlich=True)
    felder = ("zeit", "von", "adresse", "an", "betreff", "zeige", "richtung",
              "vermutlich", "aktuell", "ordner", "uid")
    return [{f: n.get(f) for f in felder} for n in g.get("nachrichten") or []]
