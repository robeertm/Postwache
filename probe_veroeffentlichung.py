#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Checks whether this version can actually ARRIVE at the people who run it.

    python3 probe_veroeffentlichung.py

🔴 Why this bench exists (29.09.2026). DocuSort had been pushed, the container
   image was current, everything was green — and a running installation still
   reported itself up to date. Tag and release are HAND work, and they had been
   left undone, so a source install compared its own version against the newest
   RELEASE, found them equal and truthfully reported „up to date". A defect the
   interface reports as a success.

   „Published" is two halves here, and only one runs by itself:
     · push to `main`  → builds the image   (workflow, automatic)
     · tag + release   → the archive people download  (by hand — this bench)

🔑 Two rules this bench learned the hard way:

   · **It asks GitHub, not the local copy.** Neither `git fetch` nor
     `git ls-remote`: both go over SSH and HANG in a captured subprocess while
     running in 1.4 s from a shell. Over the API it also works for anyone who
     cloned without an SSH key.

   · **Being unable to ask is RED, not green.** A bench that reports „fine"
     when it has no connection checks nothing at all.
"""
import base64
import io
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

HIER = os.path.dirname(os.path.abspath(__file__))
REPO = "der Dienstbenutzer/Postwache"
BESITZER = REPO.split("/")[0]
F = []


def pruefe(name, ist, soll=True, hinweis=""):
    F.append((name, ist, soll))
    print(("  OK   " if ist == soll else "  FEHL ") + name
          + (("   — " + hinweis) if hinweis else ""))


def lies(*teile):
    return io.open(os.path.join(HIER, *teile), encoding="utf-8").read()


def _gh_marke():
    """`gh`'s token, if it is around — GHCR answers the package list only to a
    signed-in caller, and a bench that is permanently red gets switched off."""
    try:
        return subprocess.run(("gh", "auth", "token"), capture_output=True,
                              text=True, timeout=15).stdout.strip()
    except Exception:
        return ""


MARKE = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
         or _gh_marke())


def github(pfad):
    kopf = {"Accept": "application/vnd.github+json",
            "User-Agent": "postwache-probe-veroeffentlichung"}
    if MARKE:
        kopf["Authorization"] = "Bearer " + MARKE
    try:
        r = urllib.request.Request("https://api.github.com" + pfad, headers=kopf)
        with urllib.request.urlopen(r, timeout=25) as a:
            return json.load(a), ""
    except urllib.error.HTTPError as e:
        return None, "HTTP %d" % e.code
    except Exception as e:
        return None, str(e)[:120]


V = (os.environ.get("POSTWACHE_PRUEF_VERSION") or lies("VERSION")).strip()
TAG = "v" + V
print("\n── Version %s: kommt sie bei den Leuten an? ─────────────────────" % V)

pruefe("das Changelog kennt diese Version",
       ("## [%s]" % V) in lies("CHANGELOG.public.md")
       if os.path.exists(os.path.join(HIER, "CHANGELOG.public.md"))
       else ("## [%s]" % V) in lies("CHANGELOG.md"))

ref, fehler = github("/repos/%s/git/ref/tags/%s" % (REPO, TAG))
pruefe("es gibt den Tag %s auf GitHub" % TAG, ref is not None,
       hinweis=(("zeigt auf %s" % ref["object"]["sha"][:8]) if ref
                else "%s — ohne Tag gibt es kein Archiv zum Herunterladen" % fehler))
if ref is not None:
    inhalt, fehler = github("/repos/%s/contents/VERSION?ref=%s" % (REPO, TAG))
    if inhalt is None:
        pruefe("GitHub nach dem Inhalt des Tags fragen", False,
               hinweis="ging nicht: %s" % fehler)
    else:
        text = base64.b64decode(inhalt.get("content") or "").decode(
            "utf-8", "replace").strip()
        pruefe("im Release-Archiv steht wirklich Version %s" % V, text == V,
               hinweis="dort steht: %s" % (text or "—"))
    vgl, fehler = github("/repos/%s/compare/%s...main" % (REPO, TAG))
    pruefe("der Tag liegt auf der Linie von main",
           vgl is not None and vgl.get("status") in ("identical", "ahead", "behind"),
           hinweis=("main gegenueber dem Tag: %s" % vgl.get("status")) if vgl
                   else "ging nicht: %s" % fehler)

rel, fehler = github("/repos/%s/releases/latest" % REPO)
if rel is None:
    pruefe("GitHub nach dem neuesten Release fragen", False,
           hinweis="ging nicht: %s — ungeprueft ist nicht gruen" % fehler)
else:
    neuestes = (rel.get("tag_name") or "").lstrip("v")
    pruefe("das neueste Release auf GitHub ist %s" % V, neuestes == V,
           hinweis="GitHub sagt: %s (veroeffentlicht %s)"
                   % (neuestes or "—", rel.get("published_at") or "—"))
    pruefe("es ist kein Entwurf und keine Vorabfassung",
           not rel.get("draft") and not rel.get("prerelease"))
    pruefe("das Release hat einen Text",
           len((rel.get("body") or "").strip()) > 80,
           hinweis="%d Zeichen" % len((rel.get("body") or "").strip()))

pak, fehler = github("/users/%s/packages/container/postwache/versions" % BESITZER)
if pak is None:
    pruefe("GHCR nach den Abbild-Marken fragen", False,
           hinweis="ging nicht: %s" % fehler)
else:
    marken = set()
    for eintrag in pak[:12]:
        marken.update((eintrag.get("metadata") or {}).get("container", {})
                      .get("tags") or [])
    pruefe("es gibt ein Abbild mit der Marke %s" % V, V in marken,
           hinweis="vorhanden: " + ", ".join(sorted(m for m in marken if m)[:6]))
    pruefe("und „latest“ ist mitgezogen", "latest" in marken)

# 🔴 The workflow must NOT build on tags. It pushes `:latest`, so a tag on an
#    older version would hand every customer old code — that happened once.
wf = lies(".github", "workflows", "docker.yml")
pruefe("der Workflow baut NICHT auf Tags",
       'tags: ["v*"]' not in wf and "tags:" not in wf.split("workflow_dispatch")[0],
       hinweis="sonst schoebe ein alter Tag `:latest` zurueck")

schlecht = [n for n, i, s in F if i != s]
print("\n%s  %d Proben, %d Fehlschlaege"
      % ("🔴 ROT" if schlecht else "GRUEN", len(F), len(schlecht)))
for n in schlecht:
    print("   FEHL:", n)
if schlecht:
    print("\n🔑 Fehlt Tag oder Release, so nachziehen:\n"
          "   gh release create %s --repo %s --target main --latest \\\n"
          "     --title \"Postwache %s — <Schlagzeile>\" --notes-file <text.md>"
          % (TAG, REPO, V))
sys.exit(1 if schlecht else 0)
