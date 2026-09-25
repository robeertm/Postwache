#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Prueft, dass jede Programmdatei auf JEDEM Ausrollweg mitkommt.

🔴 Diese Probe gibt es, weil dieselbe Falle zweimal zugeschlagen hat:

  · 3.1.0 — `locales/` fehlte in `deploy.sh`. Die Seite haette auf dem Pi ihre
    SCHLUESSEL angezeigt statt Texte, und nichts haette das gemeldet.
  · 3.2.0 — `ollama_einrichten.py` wird von der Seite AUSGELIEFERT. Fehlt sie,
    antwortet der Download mit 404 — zu merken erst beim Doppelklick.

Beides sind Fehler, die kein Syntaxpruefer und kein Test findet, weil das
Programm vollstaendig in Ordnung ist. Es kommt nur nicht an.

Die Wege sind: der Deploy auf den eigenen Rechner (deploy.sh + pi-install.sh),
das Container-Abbild (Dockerfile) und der oeffentliche Baum
(veroeffentlichen.py).
"""
import io, os, re, sys

HIER = os.path.dirname(os.path.abspath(__file__))
fehler = []

# Was zum PROGRAMM gehoert — nicht zum Zustand, nicht zum Werkzeugkasten.
# Was hier fehlt, wird auch nirgends geprueft: die Liste ist die Ansage.
PROGRAMM = ["postwache.py", "post_web.py", "post_web.html", "VERSION",
            "ollama_einrichten.py"]
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
# 🔴 Der umgekehrte Blick: die Seite liest Dateien ueber `neben_dem_programm()`.
# Jeder dort genannte Name MUSS in PROGRAMM stehen — sonst faellt beim
# naechsten Mal wieder eine Datei durch, die niemand auf der Liste hat.
web = io.open(os.path.join(HIER, "post_web.py"), encoding="utf-8").read()
geliefert = set(re.findall(r'neben_dem_programm\(\s*["\']([\w.\-/]+)["\']', web))
# Ueber eine Konstante ausgeliefert (INSTALLER_SKRIPT = "…")
for name, wert in re.findall(r'^([A-Z_]+)\s*=\s*"([\w.\-/]+)"\s*$', web, re.M):
    if "neben_dem_programm(%s)" % name in web:
        geliefert.add(wert)
unbekannt = sorted(geliefert - set(PROGRAMM))
probe("jede ausgelieferte Datei steht auf der Liste", not unbekannt,
      ", ".join(unbekannt) if unbekannt else "%d geprueft" % len(geliefert))

print("\n%s  %d Fehlschlaege" % ("ALLES GRUEN" if not fehler else "ROT", len(fehler)))
sys.exit(1 if fehler else 0)
