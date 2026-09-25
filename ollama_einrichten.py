#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Postwache — one-click setup for a local model (Ollama).

Downloaded from your own Postwache and started with a double-click. It does
the four things that otherwise take a manual afternoon:

  1. install Ollama if it isn't there (Homebrew on macOS, the official
     install script on Linux, winget on Windows),
  2. make sure it listens where the Postwache can actually reach it,
  3. pull a model,
  4. tell the Postwache the address and model — and then ask the POSTWACHE
     whether it works. Not this machine. The Postwache.

Usage (the launcher fills this in for you):

    python3 ollama_einrichten.py --postwache http://postwache.local:8110

🔴 Nothing here touches your mailbox and nothing is uploaded. The only thing
that leaves this machine is the address of this machine, sent to the Postwache
you downloaded the script from.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request

OLLAMA_PORT = 11434
STANDARD_MODELL = "llama3.1:8b"          # same default the Postwache uses
KLEIN_MODELL = "llama3.2:3b"             # for machines with little memory
# Same order of preference the Postwache uses when it finds several models —
# 🔴 two lists for one decision drift apart, and nobody notices which one won.
WUNSCH = ("llama3.1:8b", "llama3.2:3b", "qwen2.5:7b-instruct",
          "qwen2.5:14b-instruct", "mistral:7b", "gemma2:9b")
UNTAUGLICH = ("embed", "bge-", "minilm", "clip", "rerank", "nomic-", "llava",
              "moondream")   # embedders and vision models cannot propose a rule
WARTEN_START = 40                        # seconds we give `ollama serve`
WARTEN_PRUEFUNG = 150                    # the first answer loads the model

ROT, GRUEN, GELB, GRAU, AUS = "\033[31m", "\033[32m", "\033[33m", "\033[90m", "\033[0m"
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    ROT = GRUEN = GELB = GRAU = AUS = ""


def schritt(t): print("\n%s▸ %s%s" % (GELB, t, AUS), flush=True)
def gut(t):     print("%s  ✓ %s%s" % (GRUEN, t, AUS), flush=True)
def info(t):    print("%s    %s%s" % (GRAU, t, AUS), flush=True)
def warn(t):    print("%s  ! %s%s" % (GELB, t, AUS), flush=True)


def ende(t: str, code: int = 1):
    print("\n%s✋ %s%s" % (ROT, t, AUS), flush=True)
    if sys.stdin.isatty():
        try:
            input("\nPress Enter to close this window. ")
        except (EOFError, KeyboardInterrupt):
            pass
    sys.exit(code)


def frage(text: str, ja_ist_vorgabe: bool = True) -> bool:
    """A yes/no question. Without a terminal (double-clicked in some desktop
    environments) we take the default rather than hang forever."""
    if not sys.stdin.isatty():
        info("%s  → %s (no terminal, taking the default)"
             % (text, "yes" if ja_ist_vorgabe else "no"))
        return ja_ist_vorgabe
    hinweis = "[Y/n]" if ja_ist_vorgabe else "[y/N]"
    try:
        a = input("\n  %s %s " % (text, hinweis)).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ja_ist_vorgabe if not a else a.startswith(("y", "j"))


def da(prog: str) -> bool:
    return shutil.which(prog) is not None


# ── HTTP ─────────────────────────────────────────────────────────────────────
def _ctx(unsicher: bool):
    if not unsicher:
        return None
    c = ssl.create_default_context()
    c.check_hostname = False
    c.verify_mode = ssl.CERT_NONE
    return c


def holen(url: str, zeit: float = 3.0, unsicher: bool = False):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=zeit, context=_ctx(unsicher)) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "{}")


def senden(url: str, rumpf: dict, zeit: float = 30.0, unsicher: bool = False):
    roh = json.dumps(rumpf).encode()
    req = urllib.request.Request(url, data=roh,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=zeit, context=_ctx(unsicher)) as r:
        return json.loads(r.read().decode("utf-8", "replace") or "{}")


# ── Ollama ───────────────────────────────────────────────────────────────────
def ollama_antwortet(adresse: str, zeit: float = 2.0) -> list:
    """Model list, or an empty list. Never raises."""
    try:
        return [str((m or {}).get("name") or "")
                for m in (holen(adresse.rstrip("/") + "/api/tags", zeit).get("models") or [])]
    except Exception:
        return []


def ollama_installieren() -> None:
    """macOS prefers Homebrew, Linux the official script, Windows winget —
    exactly the order DocuSort's bridge installer uses."""
    if da("ollama"):
        gut("Ollama is already installed.")
        return
    system = platform.system()
    schritt("Installing Ollama")
    if system == "Darwin":
        if da("brew"):
            info("via Homebrew …")
            subprocess.check_call(["brew", "install", "ollama"])
            return gut("Ollama installed.")
        if frage("Homebrew not found. Run the official installer "
                 "(curl -fsSL https://ollama.com/install.sh | sh)?"):
            subprocess.check_call(["/bin/bash", "-c",
                                   "curl -fsSL https://ollama.com/install.sh | sh"])
            return gut("Ollama installed.")
    elif system == "Linux":
        if frage("Run the official installer "
                 "(curl -fsSL https://ollama.com/install.sh | sh)?"):
            subprocess.check_call(["/bin/bash", "-c",
                                   "curl -fsSL https://ollama.com/install.sh | sh"])
            return gut("Ollama installed.")
    elif system == "Windows":
        if da("winget") and frage("Install Ollama via winget?"):
            subprocess.check_call(
                ["winget", "install", "--silent", "--accept-source-agreements",
                 "--accept-package-agreements", "Ollama.Ollama"])
            return gut("Ollama installed.")
        ende("Ollama not found. Install it from "
             "https://ollama.com/download/windows and run this file again.")
    else:
        ende("Unsupported platform: %s. Install Ollama from https://ollama.com "
             "and run this file again." % system)
    ende("Cannot continue without Ollama. Install it from https://ollama.com "
         "and run this file again.")


def ollama_starten(bindung: str) -> bool:
    """Start `ollama serve` in the background, bound to `bindung`.

    🔴 The bind address is the whole point of this script. Ollama listens on
    127.0.0.1 by default — perfectly right, and perfectly useless when the
    Postwache runs on a different machine."""
    umgebung = dict(os.environ, OLLAMA_HOST="%s:%d" % (bindung, OLLAMA_PORT))
    protokoll = os.path.join(os.path.expanduser("~"), "ollama-postwache.log")
    schritt("Starting Ollama (listening on %s:%d)" % (bindung, OLLAMA_PORT))
    try:
        with open(protokoll, "ab") as fh:
            kw = {"stdout": fh, "stderr": fh, "env": umgebung}
            if platform.system() == "Windows":
                kw["creationflags"] = 0x00000008      # DETACHED_PROCESS
            else:
                kw["start_new_session"] = True
            subprocess.Popen(["ollama", "serve"], **kw)
    except Exception as e:
        warn("Could not start Ollama: %s" % e)
        return False
    info("log: %s" % protokoll)
    for _ in range(WARTEN_START):
        if ollama_antwortet("http://127.0.0.1:%d" % OLLAMA_PORT, 1.0):
            gut("Ollama is up.")
            return True
        time.sleep(1)
    warn("Ollama did not answer within %d s." % WARTEN_START)
    return False


def dauerhaft_binden(bindung: str) -> None:
    """Make the bind address survive a restart. Every system has its own way,
    and none of them is `ollama serve` — on macOS the menu-bar app wins, on
    Linux systemd does."""
    system = platform.system()
    wert = "%s:%d" % (bindung, OLLAMA_PORT)
    if system == "Darwin":
        # launchctl setenv reaches apps started from the Dock afterwards;
        # no sudo, no plist editing.
        try:
            subprocess.check_call(["launchctl", "setenv", "OLLAMA_HOST", wert])
            gut("OLLAMA_HOST=%s set for this login session." % wert)
            info("To make it permanent across reboots, add to your shell profile:")
            info("  launchctl setenv OLLAMA_HOST %s" % wert)
        except Exception as e:
            warn("Could not set OLLAMA_HOST: %s" % e)
    elif system == "Linux":
        info("If Ollama runs as a systemd service, it needs the same setting:")
        info("  sudo systemctl edit ollama")
        info("  [Service]")
        info('  Environment="OLLAMA_HOST=%s"' % wert)
        info("  sudo systemctl restart ollama")
    elif system == "Windows":
        try:
            subprocess.check_call(["setx", "OLLAMA_HOST", wert],
                                  stdout=subprocess.DEVNULL)
            gut("OLLAMA_HOST=%s stored for your user account." % wert)
            info("Quit Ollama in the system tray and start it again to apply it.")
        except Exception as e:
            warn("Could not store OLLAMA_HOST: %s" % e)


def modell_holen(modell: str) -> bool:
    schritt("Pulling model %s — this downloads several GB the first time" % modell)
    try:
        return subprocess.call(["ollama", "pull", modell]) == 0
    except Exception as e:
        warn("Pull failed: %s" % e)
        return False


# ── Der Weg zur Postwache ────────────────────────────────────────────────────
def zerlegen(origin: str):
    from urllib.parse import urlsplit
    t = urlsplit(origin if "://" in origin else "http://" + origin)
    port = t.port or (443 if t.scheme == "https" else 80)
    return t.scheme, (t.hostname or "127.0.0.1"), port


def eigene_adresse(host: str, port: int) -> str:
    """The address THIS machine has from the Postwache's point of view.

    🔴 Asking the hostname would give the wrong answer on any machine with
    more than one network — a VPN, a docker bridge, two Wi-Fi adapters. The
    routing table knows; a connected UDP socket asks it without sending a
    single packet."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, port))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main() -> int:
    p = argparse.ArgumentParser(description="Set up a local model for the Postwache.")
    p.add_argument("--postwache", required=True, help="e.g. http://postwache.local:8110")
    p.add_argument("--modell", default="", help="model to pull (default: %s)" % STANDARD_MODELL)
    p.add_argument("--unsicher", action="store_true",
                   help="accept a self-signed certificate on the Postwache")
    a = p.parse_args()
    origin = a.postwache.rstrip("/")
    schema, host, port = zerlegen(origin)

    print("%s\nPostwache — local model setup%s" % (GELB, AUS))
    print("  Postwache: %s" % origin)

    # ── 1. Erreichen wir die Postwache ueberhaupt? ───────────────────────────
    schritt("Reaching the Postwache")
    try:
        lage = holen(origin + "/api/lage", 6.0, a.unsicher)
    except Exception as e:
        ende("Cannot reach %s (%s).\nIs the Postwache running, and is this "
             "machine on the same network?" % (origin, e))
    gut("Postwache %s answers." % (lage.get("version") or "?"))

    # ── 2. Steht sie auf DIESEM Rechner? ─────────────────────────────────────
    meine = eigene_adresse(host, port)
    hier = meine.startswith("127.") or host in ("localhost", "127.0.0.1", "::1")
    if hier:
        bindung, sichtbar = "127.0.0.1", "127.0.0.1"
        info("The Postwache runs on this machine — nothing needs to be exposed.")
    else:
        bindung, sichtbar = "0.0.0.0", meine
        info("The Postwache runs elsewhere; it will reach this machine at %s." % meine)

    # ── 3. Ollama installieren ───────────────────────────────────────────────
    ollama_installieren()

    # ── 4. Laeuft sie, und laeuft sie an der richtigen Stelle? ───────────────
    schritt("Checking Ollama")
    lokal = ollama_antwortet("http://127.0.0.1:%d" % OLLAMA_PORT)
    ziel_url = "http://%s:%d" % (sichtbar, OLLAMA_PORT)
    erreichbar = lokal if hier else ollama_antwortet(ziel_url)

    if erreichbar:
        gut("Ollama answers at %s." % ziel_url)
    elif lokal and not hier:
        # Der haeufigste echte Fall: die App laeuft, aber nur fuer sich selbst.
        warn("Ollama runs, but only for this machine (127.0.0.1).")
        print("\n  The Postwache sits on another computer, so Ollama has to")
        print("  listen on the network as well. That means: anyone on your")
        print("  local network can then use this model — Ollama has no")
        print("  password. On a home network that is usually fine; on a")
        print("  shared or public one it is not.")
        if not frage("Let Ollama listen on the network (0.0.0.0:%d)?" % OLLAMA_PORT,
                     ja_ist_vorgabe=False):
            ende("Nothing changed. You can also run the Postwache and Ollama "
                 "on the same machine, then none of this is needed.", 0)
        dauerhaft_binden("0.0.0.0")
        if platform.system() == "Darwin":
            info("Restarting the Ollama app so it picks up the new setting …")
            subprocess.call(["osascript", "-e", 'quit app "Ollama"'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(2)
            subprocess.call(["open", "-a", "Ollama"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(4)
        erreichbar = ollama_antwortet(ziel_url, 4.0)
        if not erreichbar:
            ollama_starten("0.0.0.0")
            erreichbar = ollama_antwortet(ziel_url, 4.0)
    else:
        if not hier and not frage(
                "Ollama has to listen on the network (0.0.0.0:%d) so the "
                "Postwache can reach it. Anyone on your local network can then "
                "use the model — Ollama has no password. Continue?"
                % OLLAMA_PORT, ja_ist_vorgabe=False):
            ende("Nothing changed.", 0)
        if not hier:
            dauerhaft_binden("0.0.0.0")
        ollama_starten(bindung)
        erreichbar = ollama_antwortet(ziel_url, 4.0)

    if not erreichbar:
        ende("Ollama is not reachable at %s.\nIf a firewall is in the way, "
             "allow port %d for your local network." % (ziel_url, OLLAMA_PORT))
    gut("%d model(s) available." % len(erreichbar))

    # ── 5. Modell ────────────────────────────────────────────────────────────
    modell = a.modell.strip()
    if not modell:
        # Erst nehmen, was schon da ist — ein Modell zu holen, das daneben
        # liegt, waeren mehrere Gigabyte fuer nichts.
        brauchbar = [m for m in erreichbar
                     if not any(u in m.lower() for u in UNTAUGLICH)]
        modell = ""
        for w in WUNSCH:
            for m in brauchbar:
                if m == w or m.split(":")[0] == w.split(":")[0]:
                    modell = m
                    break
            if modell:
                break
        modell = modell or (brauchbar[0] if brauchbar else STANDARD_MODELL)
    if modell not in erreichbar:
        if not modell_holen(modell):
            if modell != KLEIN_MODELL and frage(
                    "Pull failed. Try the smaller %s instead?" % KLEIN_MODELL):
                modell = KLEIN_MODELL
                if not modell_holen(modell):
                    ende("Could not pull a model.")
            else:
                ende("Could not pull a model.")
        gut("Model %s ready." % modell)
    else:
        gut("Model %s is already there." % modell)

    # ── 6. Der Postwache Bescheid sagen ──────────────────────────────────────
    schritt("Telling the Postwache")
    try:
        antwort = senden(origin + "/api/ki",
                         {"anbieter": "ollama", "url": ziel_url, "modell": modell},
                         30.0, a.unsicher)
    except Exception as e:
        ende("Could not save the setting: %s" % e)
    if not antwort.get("ok"):
        ende("The Postwache refused the setting: %s" % (antwort.get("text") or "?"))
    gut("Provider set to Ollama, %s, model %s." % (ziel_url, modell))

    # ── 7. 🔴 Und jetzt fragen wir die POSTWACHE, nicht uns selbst ───────────
    schritt("Asking the Postwache whether it actually works")
    info("The first answer loads the model into memory — this can take a minute.")
    try:
        probe = senden(origin + "/api/ki_pruefen", {}, WARTEN_PRUEFUNG, a.unsicher)
    except Exception as e:
        ende("The Postwache could not be asked: %s\nThe setting is saved; open "
             "the page and press „check“ there." % e)
    if not probe.get("ok"):
        ende("The Postwache cannot use the model yet:\n  %s\n\nMost often a "
             "firewall on this machine is blocking port %d."
             % (probe.get("text") or "?", OLLAMA_PORT))

    print("\n%s✓ Done — the Postwache answers: %s%s"
          % (GRUEN, probe.get("text") or "ok", AUS))
    print("\n  From now on the Postwache asks this machine when a mail fits no")
    print("  drawer. Nothing leaves your home.")
    print("\n  Address : %s" % ziel_url)
    print("  Model   : %s" % modell)
    print("  Page    : %s" % origin)
    if sys.stdin.isatty():
        try:
            input("\nPress Enter to close this window. ")
        except (EOFError, KeyboardInterrupt):
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(130)
