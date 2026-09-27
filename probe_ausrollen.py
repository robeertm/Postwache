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
# 🔴 27.09.2026: `umbau.py` stand seit 4.0.0 NICHT auf dieser Liste — und genau
#    deshalb fehlte es im Dockerfile und im oeffentlichen Baum, ohne dass eine
#    Probe etwas gemeldet haette. Die Liste ist die Ansage: was hier fehlt, wird
#    nirgends geprueft. Beim Umzug (4.4.0) ist es zusammen mit der Datei
#    eingetragen worden.
PROGRAMM = ["postwache.py", "post_web.py", "post_web.html", "VERSION",
            "ollama_einrichten.py", "umbau.py", "umzug.py"]
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

print("\n── 4. Das Briefing des Werkstatt-Agenten hat einen Weg ──")
# 🔴 27.09.2026: `werkstatt/CLAUDE.md` lag NUR auf dem Pi. Es sagt dem Agenten,
#    dass es den Vault-Spiegel gibt — ohne das Briefing urteilt er allein nach
#    Domain und Betreff, und niemand merkt es, weil das Programm einwandfrei ist.
#    Dieselbe Falle wie bei `locales/` und `umbau.py`, nur eine Ebene höher.
BRIEFING = os.path.join("werkstatt", "CLAUDE.md")
probe("%s liegt im Baum" % BRIEFING, os.path.isfile(os.path.join(HIER, BRIEFING)))
probe("deploy.sh rollt das Briefing aus",
      "werkstatt/CLAUDE.md" in WEGE["deploy.sh"])
# Und es muss die beiden Wege des Agenten benennen — sonst kennt er sie nicht.
try:
    brief = io.open(os.path.join(HIER, BRIEFING), encoding="utf-8").read()
except OSError:
    brief = ""
for weg in ("vault-mirror", "vault-inbox"):
    probe("Briefing nennt ~/%s" % weg, weg in brief)
# Der Wächter muss beide Wege PRUEFEN — der Agent kann seinen eigenen Mangel
# nicht melden, seine Notiz braucht ja denselben fehlenden Einwurf.
wach = io.open(os.path.join(HIER, "postwache.py"), encoding="utf-8").read()
probe("der Waechter prueft den Arbeitsplatz",
      "arbeitsplatz_pruefen()" in wach and 'ARBEITSPLATZ = ("vault-mirror"' in wach)

print("\n%s  %d Fehlschlaege" % ("ALLES GRUEN" if not fehler else "ROT", len(fehler)))
sys.exit(1 if fehler else 0)
