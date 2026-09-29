#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Checks the pairing with DocuSort — the automatic one and the one-click one.

    python3 probe_kopplung.py

Needs no network and no running DocuSort: every check either reads a file or
calls a function directly.

🔑 What it guards, and why each one is here:

  · **The address rule.** The pairing word travels over that connection, so
    https is mandatory — with exactly two exceptions, neither of which ever
    leaves the machine: this machine itself, and a name without a dot while we
    run in a container (a compose service name, resolved inside the Docker
    network). 🔴 A home-network address in the clear is NOT one of them, and a
    probe that let that through would quietly undo the rule.

  · **The environment sets up, it does not overwrite.** What a person typed on
    the page has to survive a restart. But an entry that CAME from the
    environment must follow a changed key, or the handover breaks after a
    rotation and nobody knows why.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

HIER = os.path.dirname(os.path.abspath(__file__))
F = []


def pruefe(name, ist, soll=True, hinweis=""):
    F.append((name, ist, soll))
    print(("  OK   " if ist == soll else "  FEHL ") + name
          + (("   — " + hinweis) if hinweis else "")
          + ("" if ist == soll else "   ist=%r soll=%r" % (ist, soll)))


def lies(*teile):
    return io.open(os.path.join(HIER, *teile), encoding="utf-8").read()


def im_modul(heim, code, **umgebung):
    """Run a snippet inside post_web, with its own state directory."""
    u = dict(os.environ)
    u["POSTWACHE_HOME"] = heim
    u.update({k: str(v) for k, v in umgebung.items()})
    r = subprocess.run(
        [sys.executable, "-c", "import sys;sys.path.insert(0, %r);"
         "import post_web;" % HIER + code],
        capture_output=True, text=True, env=u, timeout=120, cwd=HIER)
    return (r.stdout + r.stderr).strip()


BASIS = tempfile.mkdtemp(prefix="pw-kopplung-")

print("\n── 1. The address rule ─────────────────────────────────────────────")
h = os.path.join(BASIS, "regel")
os.makedirs(h)
FAELLE = [
    ("https://haus.example.ts.net", True,  "https is always fine"),
    ("http://127.0.0.1:8080",       True,  "this machine"),
    ("http://localhost:8080",       True,  "this machine"),
    ("http://192.168.1.50:8080",    False, "clear text across the LAN"),
    ("http://nas.fritz.box:8080",   False, "a name with a dot, in the clear"),
    ("ftp://somewhere",             False, "not the web at all"),
]
for url, erlaubt, warum in FAELLE:
    aus = im_modul(h, "print(repr(post_web.ds_adresse_pruefen(%r)))" % url)
    pruefe("%-30s %s" % (url, "allowed" if erlaubt else "refused"),
           aus.endswith("''"), erlaubt, warum)

aus = im_modul(h, "print(repr(post_web.ds_adresse_pruefen('http://docusort:8080')))")
pruefe("a bare service name is refused OUTSIDE a container", aus.endswith("''"), False)
aus = im_modul(h, "post_web.im_container=lambda: True;"
                  "print(repr(post_web.ds_adresse_pruefen('http://docusort:8080')))")
pruefe("and allowed INSIDE one", aus.endswith("''"), True,
       "a compose service name on the Docker network")

print("\n── 2. The environment sets up, it does not overwrite ───────────────")
h1 = os.path.join(BASIS, "leer")
os.makedirs(h1)
aus = im_modul(h1, "print(repr(post_web.ds_aus_umgebung()))",
               POSTWACHE_DS_URL="", POSTWACHE_DS_PASSWORT="")
pruefe("with nothing in the environment, nothing is written", aus.endswith("''"))

h2 = os.path.join(BASIS, "vonhand", "state")
os.makedirs(h2)
io.open(os.path.join(h2, "docusort.json"), "w", encoding="utf-8").write(json.dumps(
    {"url": "https://von-hand.example.ts.net", "benutzer": "Ich",
     "passwort": "handgetippt", "aktiv": True}))
im_modul(os.path.join(BASIS, "vonhand"), "post_web.ds_aus_umgebung()",
         POSTWACHE_DS_URL="http://127.0.0.1:8123",
         POSTWACHE_DS_PASSWORT="aus-der-umgebung")
d = json.load(io.open(os.path.join(h2, "docusort.json"), encoding="utf-8"))
pruefe("an entry made by a person stays untouched",
       d["passwort"] == "handgetippt", hinweis=d["url"])

h3 = os.path.join(BASIS, "ausumgebung")
os.makedirs(h3)
for wort in ("erstes-wort", "zweites-wort"):
    im_modul(h3, "post_web.ds_aus_umgebung()",
             POSTWACHE_DS_URL="http://127.0.0.1:8123", POSTWACHE_DS_PASSWORT=wort)
d = json.load(io.open(os.path.join(h3, "state", "docusort.json"), encoding="utf-8"))
pruefe("but an entry that came from the environment follows a new key",
       d["passwort"] == "zweites-wort", hinweis=d["passwort"])
pruefe("and it is written with 0600",
       oct(os.stat(os.path.join(h3, "state", "docusort.json")).st_mode & 0o777)
       == "0o600")

print("\n── 3. The pairing line ─────────────────────────────────────────────")
import base64
inhalt = {"url": "https://ds.example.ts.net", "benutzer": "Postwache",
          "passwort": "geheim-123"}
zeile = "pw1." + base64.urlsafe_b64encode(
    json.dumps(inhalt, separators=(",", ":")).encode()).decode().rstrip("=")
h4 = os.path.join(BASIS, "zeile")
os.makedirs(h4)
im_modul(h4, "post_web.ds_kopplung_einloesen({'zeile': %r})" % zeile)
p4 = os.path.join(h4, "state", "docusort.json")
pruefe("a pairing line is written down", os.path.exists(p4))
if os.path.exists(p4):
    d = json.load(io.open(p4, encoding="utf-8"))
    pruefe("with address, user and word from the line",
           d.get("url") == inhalt["url"] and d.get("benutzer") == "Postwache"
           and d.get("passwort") == "geheim-123")
    pruefe("and marked as NOT coming from the environment",
           d.get("aus_umgebung") is False,
           hinweis="otherwise the next restart would overwrite it")

for kaputt, was in ((zeile.replace("pw1.", "xx2."), "wrong marker"),
                    ("pw1.!!!nonsense!!!", "broken content"),
                    ("", "empty")):
    h5 = tempfile.mkdtemp(dir=BASIS)
    aus = im_modul(h5, "print(post_web.ds_kopplung_einloesen({'zeile': %r})['ok'])"
                   % kaputt)
    pruefe("%-16s is refused" % was, aus.strip().endswith("False"))
    pruefe("%-16s writes nothing" % was,
           not os.path.exists(os.path.join(h5, "state", "docusort.json")))

print("\n── 4. What ships with it ───────────────────────────────────────────")
haupt = lies("docker-compose.yml")
pruefe("Watchtower is switched ON in the compose file",
       "watchtower:" in haupt and "# watchtower:" not in haupt)
pruefe("and it is the maintained fork, not the one that stopped moving",
       "ghcr.io/nicholas-fedor/watchtower" in haupt
       and "containrrr/watchtower" not in haupt)
pruefe("it watches only this container",
       haupt.rstrip().endswith("- postwache"),
       hinweis="so someone else's Watchtower is not fought over")

beide = lies("docker-compose.both.yml")
pruefe("the both-file exists and demands the shared secret",
       "${PAIRING_SECRET:?" in beide)
pruefe("both sides get the SAME secret",
       beide.count("${PAIRING_SECRET:?") == 2)
pruefe("and the Postwache is told where DocuSort is",
       "POSTWACHE_DS_URL=http://docusort:8080" in beide)

for name in ("deploy/tailscale.sh", "deploy/install-both.sh"):
    p = os.path.join(HIER, name)
    pruefe("%s is there and executable" % name,
           os.path.isfile(p) and bool(os.stat(p).st_mode & 0o111))

ts = lies("deploy", "tailscale.sh")
pruefe("the Tailscale script writes COMPOSE_FILE into .env",
       "COMPOSE_FILE" in ts and "docker-compose.tailscale.yml" in ts,
       hinweis="so a plain `docker compose up -d` keeps using the overlay")

shutil.rmtree(BASIS, ignore_errors=True)
schlecht = [n for n, i, s in F if i != s]
print("\n%s  %d Proben, %d Fehlschlaege"
      % ("🔴 ROT" if schlecht else "GRUEN", len(F), len(schlecht)))
for n in schlecht:
    print("   FEHL:", n)
sys.exit(1 if schlecht else 0)
