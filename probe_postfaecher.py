#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Prueft das Fundament von 3.0.0: Konfiguration, mehrere Postfaecher, Migration.

Laeuft gegen einen WEGWERF-Zustandsordner, nie gegen einen echten. Der Waechter
liest seine Pfade beim Import aus `~/scripts/postwache` — die werden hier
umgebogen, bevor irgendetwas geschrieben wird.
"""
import io, json, os, shutil, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import postwache as W

fehler = []


def probe(name, bedingung, zusatz=""):
    ok = bool(bedingung)
    print("  %s %s%s" % ("OK  " if ok else "FEHL", name, ("  — " + zusatz) if zusatz else ""))
    if not ok:
        fehler.append(name)


def frischer_ordner():
    d = tempfile.mkdtemp(prefix="postwache-probe-")
    W.BASE, W.STATE, W.OUT = d, os.path.join(d, "state"), os.path.join(d, "out")
    W.DISABLED, W.PAUSE = os.path.join(d, "DISABLED"), os.path.join(d, "PAUSE")
    os.makedirs(W.STATE, exist_ok=True)
    os.makedirs(W.OUT, exist_ok=True)
    W._KONFIG_ZWISCHEN = None
    W.pf_waehlen("")
    return d


def schreib(name, daten):
    with io.open(os.path.join(W.STATE, name), "w", encoding="utf-8") as fh:
        json.dump(daten, fh)


# ── 1. Wohin eine Datei gehoert ──────────────────────────────────────────
print("\n── 1. Global bleibt global, Postfach-Zustand wandert ──")
d = frischer_ordner()
probe("global ohne Postfach", W.state_pfad("einstellungen.json") == os.path.join(W.STATE, "einstellungen.json"))
probe("Postfach-Datei ohne Auswahl noch flach", W.state_pfad("lauf.json") == os.path.join(W.STATE, "lauf.json"))
W.pf_waehlen("zweitpf")
probe("Postfach-Datei mit Auswahl im Unterordner",
      W.state_pfad("lauf.json") == os.path.join(W.STATE, "pf", "zweitpf", "lauf.json"))
probe("globale Datei bleibt global, auch bei Auswahl",
      W.state_pfad("einstellungen.json") == os.path.join(W.STATE, "einstellungen.json"))
W.save("lauf.json", {"uid": 42})
probe("save legt den Unterordner an", os.path.isfile(os.path.join(W.STATE, "pf", "zweitpf", "lauf.json")))
probe("und load findet ihn wieder", (W.load("lauf.json", {}) or {}).get("uid") == 42)
W.pf_waehlen("anderes")
probe("ein anderes Postfach sieht davon NICHTS", W.load("lauf.json", None) is None)
W.pf_waehlen("")
shutil.rmtree(d, ignore_errors=True)

# ── 2. Migration aus 2.x ─────────────────────────────────────────────────
print("\n── 2. Migration: ein Postfach wird eine Liste ──")
d = frischer_ordner()
schreib("zugang.json", {"adresse": "post@beispiel.de", "passwort": "geheim",
                        "server": "imap.beispiel.de", "port": 993})
for name in ("lauf.json", "koepfe.json", "absender.json", "ablage.json",
             "statistik.json", "anhaenge.json", "zaehler.json"):
    schreib(name, {"marke": name})
schreib("einstellungen.json", {"scharf": True})
faecher = W.postfaecher()
probe("genau ein Postfach", len(faecher) == 1, str([f["id"] for f in faecher]))
probe("heisst standard", faecher and faecher[0]["id"] == "standard")
probe("Adresse uebernommen", faecher and faecher[0]["adresse"] == "post@beispiel.de")
probe("Passwort uebernommen", faecher and faecher[0]["passwort"] == "geheim")
umgezogen = [n for n in ("lauf.json", "koepfe.json", "absender.json", "ablage.json",
                         "statistik.json", "anhaenge.json", "zaehler.json")
             if os.path.isfile(os.path.join(W.STATE, "pf", "standard", n))]
probe("alle sieben Zustandsdateien umgezogen", len(umgezogen) == 7, "%d/7" % len(umgezogen))
liegengeblieben = [n for n in umgezogen if os.path.isfile(os.path.join(W.STATE, n))]
probe("und NICHT doppelt liegengeblieben", not liegengeblieben, str(liegengeblieben))
probe("Einstellungen blieben global", os.path.isfile(os.path.join(W.STATE, "einstellungen.json")))
probe("zugang.json blieb als Sicherung", os.path.isfile(os.path.join(W.STATE, "zugang.json")))
probe("postfaecher.json ist 0600",
      oct(os.stat(os.path.join(W.STATE, "postfaecher.json")).st_mode & 0o777) == "0o600")
W.pf_waehlen("standard")
probe("der alte Zustand ist unter dem neuen Namen lesbar",
      (W.load("ablage.json", {}) or {}).get("marke") == "ablage.json")
W.pf_waehlen("")
probe("zweiter Aufruf migriert NICHT noch einmal", len(W.postfaecher()) == 1)
shutil.rmtree(d, ignore_errors=True)

# ── 3. Die Liste ist streng ──────────────────────────────────────────────
print("\n── 3. Was die Liste annimmt und was nicht ──")
d = frischer_ordner()
schreib("postfaecher.json", {"liste": [
    {"id": "a", "adresse": "eins@beispiel.de", "passwort": "x"},
    {"id": "a", "adresse": "doppelt@beispiel.de"},          # gleiche Kennung
    {"id": "b", "adresse": "", "passwort": "x"},            # ohne Adresse
    {"id": "c/../d", "adresse": "drei@beispiel.de"},        # Pfadtrick
    {"id": "d", "adresse": "vier@beispiel.de", "an": False},
]})
f = W.postfaecher()
ids = [e["id"] for e in f]
probe("doppelte Kennung faellt weg", ids.count("a") == 1)
probe("ohne Adresse faellt weg", "b" not in ids)
probe("Pfadtrick entschaerft", "cd" in ids, str(ids))
probe("abgeschaltetes bleibt in der Liste", "d" in ids)
probe("aber nur aktive laufen", [e["id"] for e in f if e["an"]] == ["a", "cd"])
probe("Server wird aus der Adresse vorgeschlagen",
      f[0]["server"] == "imap.beispiel.de", f[0]["server"])
probe("Name faellt auf die Adresse zurueck", f[0]["name"] == "eins@beispiel.de")
probe("zugang() liefert das erste", W.zugang()["id"] == "a")
probe("zugang(id) liefert das benannte", W.zugang("cd")["adresse"] == "drei@beispiel.de")
W.pf_waehlen("cd")
probe("zugang() folgt der Auswahl", W.zugang()["id"] == "cd")
W.pf_waehlen("")
shutil.rmtree(d, ignore_errors=True)

# ── 4. Umgebung: erkannt, aber ueberschreibbar ───────────────────────────
print("\n── 4. Umgebung erkennen statt einprogrammieren ──")
d = frischer_ordner()
k = W.konfig()
probe("ohne konfig.json gibt es eine Seitenadresse", k["seite"].startswith("http"), k["seite"])
probe("Home Assistant nur, wenn eine Token-Datei da ist",
      bool(k["ha"]["url"]) == os.path.isfile(W.ENVFILE_STANDARD))
probe("ha_an() sagt dasselbe", W.ha_an() == bool(k["ha"]["url"] and k["ha"]["token_datei"]))
W._KONFIG_ZWISCHEN = None
schreib("konfig.json", {"seite": "https://post.beispiel.de/", "ha": {"url": "", "token_datei": ""},
                        "werkstatt": "", "imap_server": "imap.anbieter.de"})
k = W.konfig()
probe("Seitenadresse aus der Datei, ohne Schraegstrich", k["seite"] == "https://post.beispiel.de")
probe("Home Assistant laesst sich abschalten", not W.ha_an())
probe("Werkstatt laesst sich abschalten", k["werkstatt"] == "")
schreib("postfaecher.json", {"liste": [{"id": "x", "adresse": "x@y.de"}]})
probe("Server aus der Konfiguration schlaegt die Ableitung",
      W.postfaecher()[0]["server"] == "imap.anbieter.de")
shutil.rmtree(d, ignore_errors=True)

print("\n%s  %d Proben, %d Fehlschlaege"
      % ("ALLES GRUEN" if not fehler else "ROT", 26, len(fehler)))
sys.exit(1 if fehler else 0)
