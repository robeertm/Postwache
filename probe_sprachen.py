#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Checks the translation — specifically the parts that are otherwise overlooked.

🔴 The lesson from DocuSort 0.56/0.57: there the Jinja texts were translated and
the strings IN THE JAVASCRIPT were not. So what is searched for is not „is there
German between tags“ but „is there still a German sentence somewhere in the
JavaScript“.

On top of that: every language has to have the same keys as the fallback language,
and none may lose a placeholder ({n} missing = number missing from the text).
"""
import ast
import io, json, os, re, sys, tokenize

HIER = os.path.dirname(os.path.abspath(__file__))
RUECKFALL = "de"
fehler = []


def probe(name, ok, zusatz=""):
    print("  %s %s%s" % ("OK  " if ok else "FEHL", name, ("  — " + zusatz) if zusatz else ""))
    if not ok:
        fehler.append(name)


def leeren(treffer):
    """Replace a finding with as many newlines — the content is gone, the line
    numbers stay."""
    return "\n" * treffer.group(0).count("\n")


def sprachen():
    d = os.path.join(HIER, "locales")
    return sorted(f[:-5] for f in os.listdir(d) if f.endswith(".json"))


def lade(code):
    with io.open(os.path.join(HIER, "locales", "%s.json" % code), encoding="utf-8") as fh:
        return json.load(fh)


# ── 1. Vollstaendigkeit ──────────────────────────────────────────────────
print("\n── 1. Jede Sprache kennt jeden Schluessel ──")
basis = lade(RUECKFALL)
probe("Rueckfallsprache hat Texte", len(basis) > 100, "%d Schluessel" % len(basis))
for code in sprachen():
    if code == RUECKFALL:
        continue
    d = lade(code)
    fehlt = sorted(set(basis) - set(d))
    zuviel = sorted(set(d) - set(basis))
    probe("%s vollstaendig" % code, not fehlt,
          "%d fehlen, z.B. %s" % (len(fehlt), ", ".join(fehlt[:3])) if fehlt else "")
    probe("%s ohne Karteileichen" % code, not zuviel,
          ", ".join(zuviel[:3]) if zuviel else "")
    # Platzhalter muessen mitwandern
    schief = []
    for k, v in d.items():
        a = set(re.findall(r"\{(\w+)\}", basis.get(k, "")))
        b = set(re.findall(r"\{(\w+)\}", v))
        if a != b:
            schief.append("%s (%s statt %s)" % (k, sorted(b), sorted(a)))
    probe("%s behaelt die Platzhalter" % code, not schief,
          "; ".join(schief[:2]) if schief else "")

# ── 2. No German sentence left in the JavaScript ─────────────────
print("\n── 2. Keine deutschen Saetze mehr in der Seite ──")
# 🔴 2026-09-28: there are TWO pages now. Checking only the first one would let
#    every German sentence on the client page through — the same trap as in 2b,
#    one file further along. So: both, and the list is the statement.
SEITEN = ("post_web.html", "post_klient.html", "post_mobil.html")
seiten = {n: io.open(os.path.join(HIER, n), encoding="utf-8").read() for n in SEITEN}
html = "\n".join(seiten.values())
# Every script block, not „from the first to the last": between two blocks stands
# markup, and that belongs to section 2b.
js = "\n".join(b for inhalt in seiten.values()
                for b in re.findall(r"<script>(.*?)</script>", inhalt, re.S))
# Comments out — they are checked separately in section 5.
js = re.sub(r"/\*.*?\*/", leeren, js, flags=re.S)
js = re.sub(r"(?m)^\s*//.*$", "", js)
# Words that occur only in German SENTENCES (not in identifiers).
VERDACHT = re.compile(
    r"[\"'`][^\"'`\n]*?\b("
    r"nicht|noch|kein|keine|keinen|wird|wurde|werden|bitte|deine|deinen|dein|"
    r"du|dich|dir|schon|immer|alle|jede|jeder|hier|dort|damit|weil|aber|"
    r"sonst|wenn|dann|mehr|ohne|durch|zwischen|gehen|gibt|steht|liegt|"
    # 🔴 Added 2026-09-27: `\bnicht\b` does NOT match „nichts“. That is exactly
    #    why „Im Posteingang lag davon nichts.“ stayed unnoticed in German for
    #    months — a sentence he read on every click of „Here“.
    r"nichts|etwas|davon|dorthin|sofort|lag|lagen|angelegt|gespeichert|"
    r"fehlgeschlagen|mitgenommen|verschoben|blieb|blieben|geschuetzt"
    r")\b[^\"'`\n]*[\"'`]")
treffer = []
for i, z in enumerate(js.splitlines(), 1):
    if 'txt("' in z or "txt('" in z:
        # Lines with a translation call may contain keys
        z = re.sub(r"txt\(\s*[\"'][\w.]+[\"']", "txt(", z)
    m = VERDACHT.search(z)
    # CSS classes and DOM names are not sentences: a single word without a space
    # cannot be a German sentence.
    if m and " " not in m.group(0)[1:-1].strip():
        continue
    if m and "data-t" not in z:
        treffer.append("Zeile %d: %s" % (i, m.group(0)[:64]))
probe("kein deutscher Satz im JavaScript", not treffer, "%d Stellen" % len(treffer))
for x in treffer[:12]:
    print("        " + x)

# ── 3. Every key in use exists ─────────────────────────────────
# ── 2b. No German sentence in the STATIC markup ────────────────────
# 🔴 2026-09-27: section 2 only saw the JavaScript. Two buttons had stood in
#    hard-coded German IN THE MARKUP since 2.6.0 („Suchen“, „Rückstand jetzt
#    nachtragen“) and appeared that way on every English page. They were found by
#    no test bench but by an IMAGE of the English demo.
#    🔑 What is checked is exactly the places where text for humans stands and a
#    `data-t` belongs — not the whole document: a checker that suspects everything
#    gets switched off.
print("\n── 2b. Keine deutschen Saetze im statischen Markup ──")
markup = "\n".join(inhalt.split("<script>", 1)[0] for inhalt in seiten.values())
markup = re.sub(r"<!--.*?-->", leeren, markup, flags=re.S)   # comments may
markup = re.sub(r"<style[^>]*>.*?</style>", leeren, markup, flags=re.S)
BESCHRIFTET = ("button", "option", "label", "h1", "h2", "h3", "summary", "th", "a")
roh_markup = []
for tag in BESCHRIFTET:
    for m in re.finditer(r"<%s([^>]*)>([^<]+)</%s>" % (tag, tag), markup):
        attr, text = m.group(1), m.group(2).strip()
        if "data-t" in attr or not text or len(text) < 3:
            continue
        if VERDACHT.search('"' + text + '"'):
            zeile = markup[:m.start()].count("\n") + 1
            roh_markup.append("Zeile %d: <%s> %s" % (zeile, tag, text[:50]))
# Title and placeholder texts that stand directly in the markup as well.
for attr in ("title", "placeholder"):
    for m in re.finditer(r'\s%s="([^"]{4,})"' % attr, markup):
        if VERDACHT.search('"' + m.group(1) + '"'):
            zeile = markup[:m.start()].count("\n") + 1
            roh_markup.append("Zeile %d: %s= %s" % (zeile, attr, m.group(1)[:50]))
probe("statisches Markup ist uebersetzt", not roh_markup, "%d Stellen" % len(roh_markup))
for x in roh_markup[:8]:
    print("        " + x)

print("\n── 3. Jeder benutzte Schluessel ist auch hinterlegt ──")
# 🔴 `t\(` without a boundary matches inside another name: in
# `zeigeAnsicht('uebersicht')` there is a „t(“ at the end. Without looking to the
# left, the finder reports „uebersicht“ as a missing key — and whoever sees two
# false alarms stops looking on the third.
benutzt = set(re.findall(r"""(?<![\w.$])txt\(\s*["']([\w.]+)["']""", html))
# 🔴 Assembled keys (t("ds.stand." + stand)) end in a dot — they are not a key
# but a prefix. Count them here and you report „unknown“ for ever and the finder
# gets ignored.
benutzt = {k for k in benutzt if not k.endswith(".")}
benutzt |= set(re.findall(
    r'data-t(?:-titel|-platzhalter|-html|-aria)?="([\w.]+)"', html))
unbekannt = sorted(benutzt - set(basis))
# 🔴 A call with a VARIABLE key — txt(el.dataset.t) — has no literal one could
# look up. So the old, renamed form is searched for as well: a leftover `t(` is a
# certain crash („t is not defined“), and only in the browser.
uebrig = [z for i, z in enumerate(js.splitlines(), 1)
          if re.search(r"(?<![\w.$])t\(", z)]
probe("kein alter t()-Aufruf mehr", not uebrig,
      (uebrig[0].strip()[:60] + " …") if uebrig else "")
probe("alle benutzten Schluessel hinterlegt", not unbekannt,
      ", ".join(unbekannt[:5]) if unbekannt else "%d benutzt" % len(benutzt))
# 🔴 Not every key stands in the HTML. The watchman and the page write texts
# themselves (`W.txt("ki.lokal.nichts")`) — search only the HTML and you report
# exactly those as orphaned and then need a hand-maintained exception list that is
# wrong again next time. So look where they are used.
for datei in ("post_web.py", "postwache.py", "klient.py"):
    quelle = io.open(os.path.join(HIER, datei), encoding="utf-8").read()
    # `W.txt(` is the same call — the look to the left must not throw it away,
    # otherwise every text of the page counts as orphaned again.
    benutzt |= {k for k in re.findall(
        r"""(?<![\w.$])(?:W\.)?txt\(\s*["']([\w.]+)["']""", quelle)
                if not k.endswith(".")}

# 🔴 A key that does not stand there as `txt("…")` but travels through a loop as
# a value (`for stufe, wert, schluessel in (…, "w.ziel.immer")`) falls through every
# search for calls. A typo in it NEVER shows up: `txt()` dutifully returns the key
# and the page displays it. Hence: every string that LOOKS like a key has to be one.
wie_ein_schluessel = set()
# 🔴 The client's settings table carries its keys as VALUES („k.o_rechts" in a
#    list) — exactly the case this check exists for. So the page is read here too,
#    not just the Python.
# 🔴 And the WATCHMAN page as well. It was missing here — the comment above says
#    „the page is read here too" and meant the two client pages; a key carried as
#    a VALUE in `post_web.html` was therefore reported as orphaned, and an
#    invented one there was never caught at all. Found by the day/night button,
#    whose three keys live in exactly such a map.
for datei in ("post_web.py", "postwache.py", "klient.py", "post_web.html",
              "post_klient.html", "post_mobil.html"):
    quelle = io.open(os.path.join(HIER, datei), encoding="utf-8").read()
    wie_ein_schluessel |= set(re.findall(
        # 🔴 With DIGITS: „k.o_12h" and „k.o_3s" are keys like any other. Without
        # the 0-9 exactly those six fell through and were reported as orphaned —
        # a finder that is wrong about six things gets believed about none.
        r"""["']((?:w|a|ki|ds|k|schublade|allg|zeit)\.[a-z0-9_]+(?:\.[a-z0-9_]+)*)["']""",
        quelle))
DATEIENDUNG = ("json", "py", "html", "md", "sh", "txt", "log", "jsonl", "cron")
wie_ein_schluessel = {k for k in wie_ein_schluessel
                      if k.rsplit(".", 1)[-1] not in DATEIENDUNG}
erfunden = sorted(wie_ein_schluessel - set(basis))
probe("kein erfundener Schluessel im Quelltext", not erfunden,
      ", ".join(erfunden[:5]) if erfunden else "%d geprueft" % len(wie_ein_schluessel))
benutzt |= (wie_ein_schluessel & set(basis))

ungenutzt = sorted(set(basis) - benutzt)
# Keys the WATCHMAN uses do not stand in the HTML.
# Keys only the WATCHMAN uses (w.*) or that are assembled do not stand as a literal
# in the HTML.
# Assembled keys again, from the other end: `txt("k.e." + schluessel)` has no
# literal anywhere, so every settings label would count as orphaned.
ungenutzt = [k for k in ungenutzt if not k.startswith(("w.", "schublade.", "ds.stand.",
                                                       "ki.name.", "ki.hilfe.",
                                                       "k.e.", "k.h.", "k.warn."))]
probe("keine verwaisten Schluessel", not ungenutzt,
      ", ".join(ungenutzt[:5]) if ungenutzt else "")

# ── 4. No German sentence left in the ANSWERS ───────────────────
# 🔴 The lesson from 3.1.0: the PAGE was translated, the ANSWERS were not. Press a
# button in an English Postwache and you got „Gespeichert.“ — and on the overview
# page the reason for every single mail stood in German. The probe of point 2 did
# not see that: it only looks into the JavaScript.
print("\n── 4. Keine deutschen Saetze in den Antworten des Servers ──")
# 🔴 The ASSEMBLED answer as well. Before, only `satz["text"] =` stood here; a
#    plain `text = "..."` or `text += "..."` was invisible — and that is exactly how
#    the three German sentences in `ordner_anlegen` came about.
ANTWORT = re.compile(r'"text"\s*:|"fehler"\s*:|"grund"\s*:|return\s+(?:True|False)\s*,'
                     r'|satz\["text"\]\s*=|teile\.append\('
                     # Every line is checked ON ITS OWN, so `^` is already its
                     # start — a `(?m)` in the middle of the pattern is forbidden.
                     r'|^\s*text\s*\+?=|^\s*meldung\s*\+?=')
for datei in ("post_web.py", "postwache.py", "klient.py"):
    quelle = io.open(os.path.join(HIER, datei), encoding="utf-8").read()
    quelle = re.sub(r'"""(?:.|\n)*?"""', leeren, quelle)    # docstrings are checked in section 5
    quelle = re.sub(r"(?m)^\s*#.*$", "", quelle)           # comments too
    quelle = re.sub(r"(?m)\s+#\s.*$", "", quelle)
    roh = []
    for i, z in enumerate(quelle.splitlines(), 1):
        if not ANTWORT.search(z):
            continue
        # Lines with a translation call are the exact opposite of the finding.
        ohne = re.sub(r"""(?:W\.)?txt\(\s*["'][\w.]+["']""", "txt(", z)
        m = VERDACHT.search(ohne)
        if m and " " in m.group(0)[1:-1].strip():
            roh.append("Zeile %d: %s" % (i, m.group(0)[:64]))
    probe("%s antwortet uebersetzt" % datei, not roh, "%d Stellen" % len(roh))
    for x in roh[:8]:
        print("        " + x)

# ── 5. The COMMENTS are English ───────────────────────────────
# der Besitzer, 2026-09-27: „alle kommentierungen im gesamten postwache code sind
# deutsch, alle auf englisch umstellen!“
#
# 🔑 A translated product whose source is commented in one language only reads
#    for its owner. The identifiers stay German on purpose (`ziel_fuer`,
#    `ablage_lernen`) — they are the vocabulary of this program, and renaming them
#    would be a different change with a different risk.
#
# 🔴 QUOTATIONS STAY. What der Besitzer said is evidence, not commentary: a quote
#    translated into English is no longer his sentence, and the reason a bolt
#    exists would lose its source. So everything inside „…“ or „…" is removed
#    before the check — and only what remains has to be English.
DEUTSCHE_WORTE = re.compile(
    r"\b(der|die|das|und|nicht|nichts|ist|eine|einen|einem|einer|wird|wer|dann|"
    r"kein|keine|keinen|mit|von|auf|f\u00fcr|fuer|sich|dass|man|schon|noch|aber|oder|"
    r"wenn|weil|damit|nur|auch|im|zum|zur|dem|den|sie|seine|ihre|ueber|\u00fcber|"
    r"werden|haben|hat|sind|war|waere|kommt|steht|liegt|gibt|geht|macht|muss|"
    r"darf|soll|jede|jeder|wieder|beim|ohne|hier|dort|alles|etwas|deshalb|"
    r"trotzdem|sonst|genau|zwei|drei|ganz|erst|schlimmer|besser)\b", re.I)
ZITAT = re.compile(u"\u201e.*?[\u201c\"]", re.S)
# \U0001f534 Identifiers are not prose. This program is named in German
#    (`ziel_fuer`, `darf`, `ohne`) on purpose, and a comment that mentions a name
#    or a string literal is not a German comment. So code references in backticks,
#    quoted literals and the CODE part of a trailing comment are removed before
#    the check -- otherwise the checker reports `ohne` subtracts... as German and
#    gets switched off for crying wolf.
CODE = re.compile(r"`[^`]*`|\"[^\"\n]*\"|'[^'\n]*'")


def nur_kommentartext(roh):
    """Von einer Kommentierung nur den TEXT -- ohne Code, Zitate und Literale."""
    zeilen = []
    for z in roh.split("\n"):
        # A comment behind code: everything before the # is program, not text.
        if not z.lstrip().startswith(("#", "//", "/*", "*", "<!--")):
            t = re.split(r"\s#\s?|\s//\s?", z, 1)
            z = t[1] if len(t) > 1 else ""
        zeilen.append(z)
    text = "\n".join(zeilen)
    return CODE.sub(" ", ZITAT.sub(" ", text))


def deutsche_kommentare(pfad):
    """Every comment in which German remains after the quotations are removed."""
    text = io.open(os.path.join(HIER, pfad), encoding="utf-8").read()
    zeilen = text.split("\n")
    stellen, bloecke = [], []
    if pfad.endswith(".py"):
        baum = ast.parse(text)
        for n in ast.walk(baum):
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef)) and n.body \
               and isinstance(n.body[0], ast.Expr) \
               and isinstance(n.body[0].value, ast.Constant) \
               and isinstance(n.body[0].value.value, str):
                bloecke.append((n.body[0].lineno, n.body[0].end_lineno))
        # 🔴 CONSECUTIVE comment lines count as ONE block. Checked line by line,
        #    a quotation spanning several lines is torn apart and its German is
        #    reported as commentary -- the checker would then cry wolf about
        #    exactly the lines that are allowed to stay German.
        marken = []
        with io.open(os.path.join(HIER, pfad), encoding="utf-8") as fh:
            for tok in tokenize.generate_tokens(fh.readline):
                if tok.type == tokenize.COMMENT:
                    marken.append(tok.start[0])
        lauf = []
        for nr in marken:
            if lauf and nr == lauf[-1] + 1:
                lauf.append(nr)
            else:
                if lauf:
                    bloecke.append((lauf[0], lauf[-1]))
                lauf = [nr]
        if lauf:
            bloecke.append((lauf[0], lauf[-1]))
    else:
        for m in re.finditer(r"/\*.*?\*/|<!--.*?-->", text, re.S):
            bloecke.append((text.count("\n", 0, m.start()) + 1,
                            text.count("\n", 0, m.end()) + 1))
        for i, z in enumerate(zeilen, 1):
            if re.match(r"^\s*//", z) or re.search(r"\s//\s?\S", z):
                bloecke.append((i, i))
    for a, e in bloecke:
        roh = "\n".join(zeilen[i - 1] for i in range(a, e + 1))
        m = DEUTSCHE_WORTE.search(nur_kommentartext(roh))
        if m:
            stellen.append("Zeile %d: \u2026%s\u2026" % (a, m.group(0)))
    return stellen


print("\n\u2500\u2500 5. Die Kommentierung ist englisch \u2500\u2500")
for datei in ("postwache.py", "post_web.py", "umbau.py", "umzug.py",
              "post_web.html", "veroeffentlichen.py", "ollama_einrichten.py",
              "probe_umbau.py", "probe_umzug.py", "probe_sprachen.py",
              "probe_ausrollen.py", "probe_dokumente.py", "probe_postfaecher.py",
              "probe_leck.py", "demo/demo_daten.py",
              # since 5.0.0: the mail client
              "klient.py", "post_klient.html", "post_mobil.html",
              "probe_klient.py"):
    stellen = deutsche_kommentare(datei)
    probe("%s: Kommentare englisch" % datei, not stellen,
          "%d deutsche Stelle(n)" % len(stellen))
    for x in stellen[:6]:
        print("        " + x)

print("\n%s  %d Fehlschlaege" % ("ALLES GRUEN" if not fehler else "ROT", len(fehler)))
sys.exit(1 if fehler else 0)
