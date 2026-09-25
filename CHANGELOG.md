# Changelog

All notable changes to the Postwache are documented here.
Dates are ISO; versions follow `MAJOR.MINOR.PATCH`.

This file starts with the first public release. The project was developed
privately before that; the summary under *0.1.0 – 2.6.2* lists what arrived
along the way rather than every single step.

## [3.2.0] – 2026-09-25

### Added
- **A local model in one click.** Press *Find a model* and the Postwache looks
  for an Ollama on its own machine, on its container host, and on the computer
  the page is open on — then offers what it found with a usable model already
  picked (it skips embedding and vision models, which cannot propose a rule).
- 🔴 **It looks from the watchman's side, not from the browser's.** The browser
  runs on the machine where Ollama sits; it would report "reachable" while the
  Postwache on a small machine elsewhere cannot get there at all. The question
  was never whether *you* can reach it.
- **A setup you download and double-click** (macOS, Windows, Linux), with the
  address of your own Postwache already in it. It installs Ollama (Homebrew /
  the official script / winget), makes it listen where the Postwache can reach
  it, pulls a model, writes the setting — and then **asks the Postwache**
  whether it works, rather than reporting success from the machine it runs on.
- ⚠️ Putting Ollama on the network means anyone on that network can use it —
  Ollama has no password. The setup says that in plain words and asks first.
  On a single machine it never comes up.

### Changed
- **The answers are translated too, not only the page.** Every reply the server
  sends ("Saved.", "The mailbox refuses: …") and every line the watchman writes
  about a mail — why it was filed there, why it was unclear, where it belongs —
  now follows the language of the installation. 396 keys per language.
  🔴 Until now an English Postwache explained every single mail in German. The
  page was translated in 3.1.0; the answers were not, and nothing said so.
- All screenshots are now taken from the English demo, and the demo no longer
  invents its own wording: it renders the exact sentences a real installation
  writes.

### Added (probes)
- `probe_ausrollen.py` — every program file has to travel on *every* rollout
  path (deploy, container image, public tree). This trap has now been sprung
  twice: `locales/` in 3.1.0, the Ollama setup in 3.2.0. Both times the program
  was perfectly fine. It just did not arrive.
- `probe_sprachen.py` gained two checks: no German sentence left in the
  **server's answers** (the gap this release closes), and no invented key —
  a key handed around as a variable has no call site to find, and a typo in one
  never shows up as an error, only as a key printed on the page.

### Fixed
- The generated Windows launcher wrote `%%TEMP%%`, which a `.bat` takes
  literally. Visible only in the generated file, never in the source.
- Two numbers the demo never set: the sender count read "0 senders", and the
  daily curve claimed to start on a date two years before its first bar.

## [3.1.0] – 2026-09-25

### Added
- **Five languages: German, English, Spanish, French, Italian.** One click in
  the settings changes everything — the page, the drawer names, the history,
  the daily summary and every Telegram message. 288 keys per language, none
  missing.
- Strings live in `locales/<code>.json`, flat key/value, **next to the program**
  rather than in the state directory: they belong to the code. The table is
  placed **into the page** as it is served, not fetched afterwards — otherwise
  the page stands there in raw keys for a blink, and on a phone that blink is
  what you see.
- 🔴 **The language is a setting of the installation, not a cookie.** The
  watchman writes its history and its messages when nobody is looking; a cookie
  cannot tell it which language to use.
- Date and time formatting follow the language. An English page with German
  dates looks like a job half done.
- `probe_sprachen.py`: every language knows every key, no orphans, and
  **placeholders survive translation** (a lost `{n}` means a missing number in
  the sentence). It also refuses to pass while a German sentence is left in the
  JavaScript — which is exactly the half that gets forgotten.

### Fixed
- 🔴 A **local variable named `t`** shadowed the translation function for a
  whole block — six places in the JavaScript, a dozen in the watchman. The
  function is now called `txt()`: two characters more, and the whole class of
  bug is gone.
- 🔴 **The fallback destroyed the normal case.** `if (typeof T === "undefined")
  { var T = {} … }` collides with the injected `const T`: "Identifier 'T' has
  already been declared". That is a *parse* error, so **not one line** of the
  script runs — while the page still looks almost normal, because the static
  HTML is there. Only the empty buttons give it away.
- The deployment did not carry `locales/` along. The page would have shown its
  keys, and nothing would have reported it.

## [3.0.0] – 2026-09-25

First public release.

### Added
- **Several mailboxes.** Each one keeps its own learned filing, sender
  profiles, attachment index and counters under `state/pf/<id>/`; what is
  shared stays shared (settings, credentials for Telegram and DocuSort, the
  journal, the log). 🔴 The whole change hangs on three lines: `load()` and
  `save()` are the single place that knows where a file lives, so the hundred
  call sites below never had to learn about mailboxes.
- **A settings view.** The one-pager was getting long. What *happened* stays on
  the overview; everything you can *change* moved behind a tab — mailboxes,
  arming, what interrupts you, Telegram, DocuSort, the judgment helper, and the
  environment.
- **An optional judgment helper.** When mail fits no drawer, a language model
  can propose a **rule** — local (Ollama) or a service (OpenAI, Anthropic), or
  handed to your own agent through a folder. Off by default. 🔴 It proposes;
  a person arms. A model that is wrong would otherwise move mail to a place
  nobody looks, and the mistake only shows up when something is missing.
- **Docker image**, a one-command installer, and a demo generator that fills a
  page with invented data so you can look before you connect anything.

### Changed
- **Nothing environment-specific is compiled in any more.** `konfig.json` says
  where the page is reachable, whether Home Assistant is connected and where an
  agent folder lives. Without that file the environment is **detected** — so an
  existing install keeps its emergency switch instead of losing it quietly to a
  "sensible default" of nothing.
- `POSTWACHE_HOME` decides where everything lives. In a container that is a
  mounted volume; on a machine it is wherever it grew.
- Because a container has no cron, the page can tick the watchman itself
  (`POSTWACHE_TAKT`). 🔴 Only when asked: next to a real cron that would be a
  second clock, and two runs on one mailbox write over each other's state.

### Fixed
- The learned filing of a removed mailbox is **kept**, not deleted. Removing a
  mailbox used to be the one irreversible click in the program.

## [0.1.0 – 2.6.2] – 2026-09-11 … 2026-09-25 (before the public release)

- A watchman that reads only the **new** mail (by UID range), classifies it by
  fixed readable rules, and costs nothing to run.
- The principle that shaped everything: **what matters stays in the inbox**;
  only noise is sorted out, and only into folders you already use.
- Learning from your own filing instead of a category list — with a hard rail:
  the watchman may only move mail where *you* have put mail from that sender
  before.
- Immediate notice by Telegram for deadlines, authorities, security warnings
  and real people; one daily summary for everything else.
- A journal of every move, and the way back — one mail or all of them.
- Statistics about your own mailbox: per day, per hour, per weekday, who writes
  most.
- **Documents in the post**: every attachment indexed from the IMAP
  `BODYSTRUCTURE` — filename, type and size **without downloading a single
  byte** — searchable years back, and handed to DocuSort on request.
