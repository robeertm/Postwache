#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Prueft die Uebersetzung — und zwar das, was man sonst uebersieht.

🔴 Die Lehre aus DocuSort 0.56/0.57: dort waren die Jinja-Texte uebersetzt und
die Zeichenketten IM JAVASCRIPT nicht. Gesucht wird deshalb nicht „steht da
Deutsch zwischen Tags", sondern „steht irgendwo im JavaScript noch ein
deutscher Satz".

Dazu: jede Sprache muss dieselben Schluessel haben wie die Rueckfallsprache,
und keine darf einen Platzhalter verlieren ({n} fehlt = Zahl fehlt im Text).
"""
import io, json, os, re, sys

HIER = os.path.dirname(os.path.abspath(__file__))
RUECKFALL = "de"
fehler = []


def probe(name, ok, zusatz=""):
    print("  %s %s%s" % ("OK  " if ok else "FEHL", name, ("  — " + zusatz) if zusatz else ""))
    if not ok:
        fehler.append(name)


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

# ── 2. Kein deutscher Satz mehr im JavaScript ────────────────────────────
print("\n── 2. Keine deutschen Saetze mehr in der Seite ──")
html = io.open(os.path.join(HIER, "post_web.html"), encoding="utf-8").read()
js = html.split("<script>", 1)[1].rsplit("</script>", 1)[0]
# Kommentare raus — die duerfen deutsch bleiben, sie erreichen niemanden.
js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
js = re.sub(r"(?m)^\s*//.*$", "", js)
# Woerter, die nur in deutschen SAETZEN vorkommen (nicht in Bezeichnern).
VERDACHT = re.compile(
    r"[\"'`][^\"'`\n]*?\b("
    r"nicht|noch|kein|keine|keinen|wird|wurde|werden|bitte|deine|deinen|dein|"
    r"du|dich|dir|schon|immer|alle|jede|jeder|hier|dort|damit|weil|aber|"
    r"sonst|wenn|dann|mehr|ohne|durch|zwischen|gehen|gibt|steht|liegt"
    r")\b[^\"'`\n]*[\"'`]")
treffer = []
for i, z in enumerate(js.splitlines(), 1):
    if 'txt("' in z or "txt('" in z:
        # Zeilen mit Uebersetzungsaufruf duerfen Schluessel enthalten
        z = re.sub(r"txt\(\s*[\"'][\w.]+[\"']", "txt(", z)
    m = VERDACHT.search(z)
    # CSS-Klassen und DOM-Namen sind keine Saetze: ein einzelnes Wort ohne
    # Leerzeichen kann kein deutscher Satz sein.
    if m and " " not in m.group(0)[1:-1].strip():
        continue
    if m and "data-t" not in z:
        treffer.append("Zeile %d: %s" % (i, m.group(0)[:64]))
probe("kein deutscher Satz im JavaScript", not treffer, "%d Stellen" % len(treffer))
for x in treffer[:12]:
    print("        " + x)

# ── 3. Jeder benutzte Schluessel existiert ───────────────────────────────
print("\n── 3. Jeder benutzte Schluessel ist auch hinterlegt ──")
# 🔴 `t\(` ohne Begrenzung trifft mitten in einem anderen Namen: in
# `zeigeAnsicht('uebersicht')` steckt ein „t(" am Ende. Ohne den Blick nach
# links meldet der Finder „uebersicht" als fehlenden Schluessel — und wer zwei
# Fehlalarme sieht, schaut beim dritten Mal nicht mehr hin.
benutzt = set(re.findall(r"""(?<![\w.$])txt\(\s*["']([\w.]+)["']""", html))
# 🔴 Zusammengesetzte Schluessel (t("ds.stand." + stand)) enden auf einem Punkt —
# sie sind kein Schluessel, sondern ein Praefix. Wer sie hier mitzaehlt, meldet
# ewig „unbekannt" und der Finder wird ignoriert.
benutzt = {k for k in benutzt if not k.endswith(".")}
benutzt |= set(re.findall(r'data-t(?:-titel|-platzhalter|-html)?="([\w.]+)"', html))
unbekannt = sorted(benutzt - set(basis))
# 🔴 Ein Aufruf mit VARIABLEM Schluessel — txt(el.dataset.t) — hat keinen
# Literal, den man nachschlagen koennte. Gesucht wird deshalb zusaetzlich nach
# der alten, umbenannten Form: ein uebrig gebliebenes `t(` ist ein sicherer
# Absturz („t is not defined"), und zwar erst im Browser.
uebrig = [z for i, z in enumerate(js.splitlines(), 1)
          if re.search(r"(?<![\w.$])t\(", z)]
probe("kein alter t()-Aufruf mehr", not uebrig,
      (uebrig[0].strip()[:60] + " …") if uebrig else "")
probe("alle benutzten Schluessel hinterlegt", not unbekannt,
      ", ".join(unbekannt[:5]) if unbekannt else "%d benutzt" % len(benutzt))
ungenutzt = sorted(set(basis) - benutzt)
# Schluessel, die der WAECHTER benutzt, stehen nicht im HTML.
# Schluessel, die nur der WAECHTER benutzt (w.*) oder die zusammengesetzt
# werden, stehen nicht als Literal im HTML.
ungenutzt = [k for k in ungenutzt if not k.startswith(("w.", "schublade.", "ds.stand.",
                                                       "ki.name.", "ki.hilfe."))]
probe("keine verwaisten Schluessel", not ungenutzt,
      ", ".join(ungenutzt[:5]) if ungenutzt else "")

print("\n%s  %d Fehlschlaege" % ("ALLES GRUEN" if not fehler else "ROT", len(fehler)))
sys.exit(1 if fehler else 0)
