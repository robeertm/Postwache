# Changelog

All notable changes to the Postwache are documented here.
Dates are ISO; versions follow `MAJOR.MINOR.PATCH`.

This file starts with the first public release. The project was developed
privately before that; the summary under *0.1.0 – 2.6.2* lists what arrived
along the way rather than every single step.

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
