#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Checks that every program file comes along on EVERY rollout path.

🔴 This probe exists because the same trap sprang twice:

  · 3.1.0 — `locales/` was missing from `deploy.sh`. The page would have shown
    its KEYS instead of text on the Pi, and nothing would have reported it.
  · 3.2.0 — `ollama_einrichten.py` is DELIVERED by the page. Without it the
    download answers 404 — noticed only on the double-click.

Both are mistakes no syntax checker and no test finds, because the program is
entirely in order. It just does not arrive.

The paths are: the deploy to one's own machine (deploy.sh + pi-install.sh), the
container image (Dockerfile) and the public tree (veroeffentlichen.py).
"""
import io, os, re, sys

HIER = os.path.dirname(os.path.abspath(__file__))
fehler = []

# What belongs to the PROGRAM — not to the state, not to the toolbox.
# Whatever is missing here is checked nowhere: the list is the statement.
# 🔴 2026-09-27: `umbau.py` had NOT been on this list since 4.0.0 — and exactly
#    for that reason it was missing from the Dockerfile and the public tree
#    without any probe reporting it. The list is the statement: what is missing
#    here is checked nowhere. With the migration (4.4.0) it was entered together
#    with the file.
# 🔴 2026-09-28, 5.0.0: `klient.py` and `post_klient.html` entered here TOGETHER
#    with the files. The client is the mail program — without those two the page
#    at /post would be a 500 and nothing would say why.
PROGRAMM = ["postwache.py", "post_web.py", "post_web.html", "VERSION",
            "ollama_einrichten.py", "umbau.py", "umzug.py",
            "klient.py", "post_klient.html", "post_mobil.html"]
ORDNER = ["locales"]

WEGE = {
    "deploy.sh":          io.open(os.path.join(HIER, "deploy.sh"), encoding="utf-8").read(),
    "pi-install.sh":      io.open(os.path.join(HIER, "pi-install.sh"), encoding="utf-8").read(),
    "Dockerfile":         io.open(os.path.join(HIER, "Dockerfile"), encoding="utf-8").read(),
    "veroeffentlichen.py": io.open(os.path.join(HIER, "veroeffentlichen.py"), encoding="utf-8").read(),
}


def probe(name, ok, zusatz=""):
    print("  %s %s%s" % ("OK  " if ok else "FEHL", name, ("  — " + zusatz) if zusatz else ""))
    if not ok:
        fehler.append(name)


print("\n── 1. Die Programmdateien gibt es ueberhaupt ──")
for datei in PROGRAMM:
    probe("%s liegt im Baum" % datei, os.path.isfile(os.path.join(HIER, datei)))
for d in ORDNER:
    probe("%s/ liegt im Baum" % d, os.path.isdir(os.path.join(HIER, d)))

print("\n── 2. Jede Programmdatei kommt auf JEDEM Weg mit ──")
for weg, inhalt in WEGE.items():
    fehlt = [d for d in PROGRAMM if d not in inhalt]
    probe("%s traegt alle Programmdateien" % weg, not fehlt, ", ".join(fehlt))
    fehlt_o = [d for d in ORDNER if d not in inhalt]
    probe("%s traegt alle Programmordner" % weg, not fehlt_o, ", ".join(fehlt_o))

print("\n── 3. Was die Seite ausliefert, muss neben ihr liegen ──")
# 🔴 The reverse view: the page reads files through `neben_dem_programm()`.
# Every name mentioned there MUST appear in PROGRAMM — otherwise a file nobody has
# on the list falls through again next time.
web = io.open(os.path.join(HIER, "post_web.py"), encoding="utf-8").read()
geliefert = set(re.findall(r'neben_dem_programm\(\s*["\']([\w.\-/]+)["\']', web))
# Delivered through a constant (INSTALLER_SKRIPT = "…")
for name, wert in re.findall(r'^([A-Z_]+)\s*=\s*"([\w.\-/]+)"\s*$', web, re.M):
    if "neben_dem_programm(%s)" % name in web:
        geliefert.add(wert)
unbekannt = sorted(geliefert - set(PROGRAMM))
probe("jede ausgelieferte Datei steht auf der Liste", not unbekannt,
      ", ".join(unbekannt) if unbekannt else "%d geprueft" % len(geliefert))

print("\n── 4. Das Briefing des Werkstatt-Agenten hat einen Weg ──")
# 🔴 2026-09-27: `werkstatt/CLAUDE.md` lay ONLY on the Pi. It tells the agent
#    that the vault mirror exists — without the briefing it judges by domain and
#    subject alone, and nobody notices, because the program is flawless. The same
#    trap as with `locales/` and `umbau.py`, only one level up.
BRIEFING = os.path.join("werkstatt", "CLAUDE.md")
probe("%s liegt im Baum" % BRIEFING, os.path.isfile(os.path.join(HIER, BRIEFING)))
probe("deploy.sh rollt das Briefing aus",
      "werkstatt/CLAUDE.md" in WEGE["deploy.sh"])
# And it has to name the agent's two paths — otherwise it does not know them.
try:
    brief = io.open(os.path.join(HIER, BRIEFING), encoding="utf-8").read()
except OSError:
    brief = ""
for weg in ("vault-mirror", "vault-inbox"):
    probe("Briefing nennt ~/%s" % weg, weg in brief)
# The watchman has to CHECK both paths — the agent cannot report its own defect,
# its note needs the very drop folder that is missing.
wach = io.open(os.path.join(HIER, "postwache.py"), encoding="utf-8").read()
probe("der Waechter prueft den Arbeitsplatz",
      "arbeitsplatz_pruefen()" in wach and 'ARBEITSPLATZ = ("vault-mirror"' in wach)

print("\n%s  %d Fehlschlaege" % ("ALLES GRUEN" if not fehler else "ROT", len(fehler)))
sys.exit(1 if fehler else 0)
