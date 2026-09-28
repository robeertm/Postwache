# Changelog

All notable changes to the Postwache are documented here.
Dates are ISO; versions follow `MAJOR.MINOR.PATCH`.

This file starts with the first public release. The project was developed
privately before that; the summary under *0.1.0 – 2.6.2* lists what arrived
along the way rather than every single step.

## [5.0.0] – 2026-09-28

### The Postwache becomes a mail client
The watchman stays exactly what it was. Next to it there is now a full client:
folder tree, list, preview pane, search, reply, forward, attachments, drafts,
sending — and some thirty switches that set all of it.

| | |
|---|---|
| `klient.py` | the engine: IMAP access, HTML cleaner, lock, outgoing mail |
| `post_klient.html` | the wide version — three panes, keyboard shortcuts |
| `post_mobil.html` | the phone version — one sheet at a time, bottom bar, swipe |
| `probe_klient.py` | 172 probes against a **fake IMAP server** |

### The lock is part of the feature, not an accessory
Since 1.0 the overview page has shown subject and sender only, and the reason was
written down: *not enough to spread an inbox across a web page **without a
login***. A client shows the body — so it brings the login with it.

* Access word, PBKDF2 with 240,000 rounds, `state/klient.json` at 0600.
* Session as an `HttpOnly` cookie, `SameSite=Strict`, `Secure` only behind TLS.
* A brake against guessing: from the fifth wrong try 30 s, then 60, 120, 300, 900
  — per **address**, not per word.
* **Every** client action and **every** file route (attachment, inline image,
  source view) sits behind it. Measured against the running page: 16 of 19 actions
  answer „locked" without a cookie; the three open ones are the door itself.
* Optionally (off by default) the same word locks the **watchman page** as well —
  and then `/api/lage` too, not merely the view.

### Three walls around foreign HTML, not one
1. An **allow list** rather than a block list (`HTMLParser`): what is not named
   does not get through.
2. A **`Content-Security-Policy`** inside the frame: `script-src 'none'`, and
   `img-src` decides for the browser, not for the filter.
3. The **`sandbox` attribute** without `allow-scripts`.

Measured against twelve attacks — script, `onerror`, `javascript:`, nested frame,
form, SVG with script, `meta refresh`, `@import`, `behavior:`, tracking pixel,
remote background image. Remote images are **off**, and the count stands above the
letter. A link whose text names a different domain than its target is reported, as
are punycode hosts and bare IP addresses.

### What this client can do that others cannot
Next to every mail stands the **watchman's verdict**: which drawer, which folder,
and why — out of its learned filing. One click files it there. The address book is
`absender.json`: the watchman has kept it for weeks, nobody had to maintain it.

### Room, and the phone
* From 1400 px the folder column and the list grow, from 1800 px again. The
  **letter keeps its measure** (74 characters): a 200-character line is harder to
  read, not easier. HTML mail keeps its own width — a newsletter is built for
  600 px.
* The phone version is **not the wide page made narrow**: one sheet at a time, a
  bar at the bottom (the top of a six-inch screen is out of thumb reach), finger
  targets from 44 px, swipe right to archive and left to delete, safe-area insets,
  16 px inputs so iOS does not zoom.
* The switch follows the device — and a choice made by hand is **remembered**.

### Three faults only the PICTURE showed
* **„Ivo Sandstr��m".** `email.message_from_bytes()` reads header lines as ASCII
  and replaces every other byte with U+FFFD — the information is gone **before**
  anyone could decode it, and plenty of real mail sends its umlauts raw. The bytes
  are now decoded first and parsed afterwards; the watchman benefits too.
* **„09:44 AM" on a page set to 24 hours.** The clock followed the language
  instead of the setting.
* **„To: me" under every row of the inbox.** Of course it is. In the sent folder
  the opposite holds — there the recipient is the only interesting name.

### And two the test bench found
* A stored outgoing server was **gone on the next read**: the mailbox record is
  rebuilt from a fixed set of fields and everything else drops out silently.
* **Renaming a mailbox deleted it as well.** A record you do not fully own is
  extended, never rebuilt.

### The probes
A **fake IMAP server** records every command, a **fake SMTP server** accepts
letters and throws them away. That makes the following measurable rather than
claimed: not a single fetch without `BODY.PEEK`; browsing opens a folder with
`EXAMINE` and only an action with `SELECT`; without `MOVE` it is copy → mark →
expunge, in that order; **and if the copy fails, nothing is marked deleted and
nothing is expunged**. The outgoing check logs in and hangs up — it sends nothing.
A blind copy is in the filed copy but not in the sent mail. The history records
**who**, never **what**.

## [4.6.0] – 2026-09-27

### The source is commented in English
Counted beforehand: **1,679 comment lines, 225 docstrings** and ~130 lines in shell
and HTML — across 17 files and some 15,000 lines of program. All of it is
translated.

**Identifiers stay German** (`ziel_fuer`, `ablage_lernen`, `nachziehen`). They are
this program's vocabulary; renaming them would be a different change with a
different risk. The interface and the answers remain in five languages — not a line
of those was touched. And the owner's **quotations stay in the original**: a quote is
evidence, not commentary; translated it is no longer his sentence, and the reason a
bolt exists would lose its source. An English gloss stands next to it where the
meaning carries weight.

### 🔑 A promise needs a checker, or it creeps back
`probe_sprachen.py` has a **section 5**: every comment in 15 files is read,
quotations are removed — and whatever still contains German filler words is **red**.
Counter-tested: an inserted German comment is found, and after taking it back it is
green again.

Two sources of false alarms had to be solved, and both are instructive.
**Identifiers are not prose** — `darf = bool(ziel)` and „`ohne` subtracts …“ were
reported as German; now everything before a line's `#`, backticks and string
literals are dropped before the check. And **consecutive comment lines are ONE
block** — checked line by line, a multi-line quotation is torn apart and the checker
flagged exactly the lines that are allowed to stay German. A checker that reports
wrongly gets switched off.

### Tooling instead of hand work
The translation went by **line ranges**, not by text search: a German opening quote
with an ASCII closing one, and a rule of dashes of unknown length, brought down the
first two attempts. The replacer verifies before every write that **every** affected
line really is a comment — and that once prevented a line of code from being
overwritten.

## [4.5.3] – 2026-09-27

### Adding an address had no effect — because the same list existed twice
The owner's addresses were kept in **two** places in the state file: under
`eigene_adressen`, read by the learning run, and inside the „own post" rule
itself, read by the filing decision. The placeholder in `ziel_fuer` filled the
rule's addresses only when the rule had **none** — and it had some. So the copy
decided, and a newly entered address changed nothing: no error, no trace, just a
mail left in the catch folder. Writing is not taking effect.

`ziel_fuer` now uses the **union** of both lists instead of one as a fallback for
the other, so a second list can no longer override the first even if it comes
back. The duplicate in the state file was emptied as well — one truth, one place.

**Measured:** all **17,966** mails through `ziel_fuer`, before against after —
**exactly one** difference, the intended one. Device accounts on the same domain
are unchanged: an address rule still beats the catch-all rule.

The new case is **red against the previous code** — the original failure, not a
restatement of the fix. `probe_umbau.py` **150 cases**.

## [4.5.2] – 2026-09-27

### A workplace cannot report its own defect
The agent that answers the watchman's questions reads the owner's knowledge base
through a read-only mirror (`~/vault-mirror`) and hands its notes back through a
drop folder (`~/vault-inbox`). Both are created when a project is provisioned.
This project's working directory had been built **by hand** — and neither link
was set.

So the agent classified **199 senders** from nothing but their domain and subject
lines, and it said so, in a note it wrote to the drop folder that did not exist.
Three notes, including explicit requests to review uncertain decisions, reached
nobody.

**Now the watchman checks it from its own side** — `arbeitsplatz_pruefen()`, two
`isdir` questions per run, reported to the journal when the finding changes, at
most every 12 h after that, and once more when it is fixed. Texts in all five
languages. The watchman may traverse the agent's home but not list it, so each
path is asked for by name.

### The briefing had no rollout path
The file that makes the agent useful — what it must never touch, the two task
shapes, how the watchman thinks — existed only on the machine it ran on,
unversioned. The next rebuild of that working directory would have removed it
silently, and nothing would have been missing except the quality of the answers.
It is now part of the repository, has its own step in the deploy script (which
also verifies the two links), and four cases in `probe_ausrollen.py`. The same
trap as `locales/` in 3.1.0 and `umbau.py` in 4.0.0, one level up.

### A right answer for the wrong reason
The agent left one domain uncategorised because its only subject line was
„test". That was correct — but not for that reason: the domain has **two faces**,
some addresses belong to the owner, others to a device with 164 mails already
filed under `Technik.Synology`. A domain→category rule would have dragged
personal mail into the device folder. Written into the briefing as the worked
example of why the mirror matters.

### Verified
`probe_umbau.py` **147 cases** (+7) — the new case runs against a broken **and** a
repaired workplace, so it is a measurement rather than a promise about source
code. `probe_ausrollen.py` green with four new cases.

## [4.5.1] – 2026-09-27

### 🔴 This repository was deleted and rebuilt
The previous history contained personal data in **all three commits** — most
seriously the **name and email address of a real person**, used as an example in
a documentation line, plus device names, two hard-coded private addresses and the
owner's mail provider. Nobody had forked or starred it. The repository was
deleted rather than rewritten, and re-published as a single commit. The container image at
`ghcr.io/robeertm/postwache` carried the same files — deleting a repository does
**not** remove its images — and has been deleted as well.

### The fix is a different place, not a better pattern
The publish step used to **scrub** personal data out of the source on its way to
the public tree. That is a net, not a wall: 20 of 74 rules in the catalogue
carried real addresses — family, advisers, tradespeople, an employer. As long as
those live in the source, a regular expression decides whether they become public.

**The personal part of the catalogue now lives in a state file** — same drawer as
the credentials, `0600`, on no rollout list. The source contains only rules that
apply to everyone (PayPal, Amazon, Telekom). *What nobody can publish, nobody has
to filter out.*

🔴 Order is logic, and it has to survive the move: the first attempt put all
personal rules first, which placed "own sent mail" ahead of the device rules and
sent **823** router reports into the archive instead of Technology. Every entry
now carries its original **position** and the two halves are interleaved back
together. Verified against 17,966 mails: **0 differences**.

### Added: an independent leak check, as a gate
🔑 *A filter that checks itself is not a check.* The scrubber reported "nothing
personal left" while six real names stood in its output. The new check

- has its **own** pattern list (email addresses that are not explicitly
  invented; private and tailnet addresses; internal paths; home directories),
- reads the **generated** tree, not the source,
- compares it against the state file — the real data — **without a single name
  in its own source**,
- inspects **every version of every file in the history**, because a repository
  does not forget,
- and the publish step now **fails** if it finds anything, so there is nothing
  to push.

Matches are anchored at word boundaries and the project's own repository URL is
exempt — without that it produced 14 findings, 11 of them noise. *A checker that
cries wolf stops being read, and then the real finding goes down with it.*

Counter-tested three ways: a known address planted in a file, a foreign address
it had never seen, and a leak that exists **only in an older commit** while the
working tree is clean — the exact shape of what happened here. All three red.

## [4.5.0] – 2026-09-27

### Added
- **A vocabulary learned from your own folders.** The watch counts which *words*
  in subject lines belong to which category across the mail you have already
  filed, and classifies unknown senders with it. Measured on a real 17,500-mail
  mailbox: **98.3 % accuracy at 69.4 % coverage** — with **no model, no API key
  and no network**. This is the watch's founding principle applied to the subject
  instead of the sender: *it can only imitate*.
- `umbau.py wortschatz` prints the vocabulary, the characteristic words per
  category and a **cross-check** — every mail judged *after* its own words were
  subtracted from its own category. Without that subtraction every mail answers
  itself and the accuracy figure is a self-assessment.
- **Task tracking.** The watch keeps its own ledger of the wake-up calls it
  created, asks the workshop for their state on every run, and reports **once**
  per task when nobody has started after two hours. A new card shows number,
  title, state and age. Above three untouched tasks it stops creating new ones —
  the *finding* is still recorded, only the wake-up call is skipped.

### Fixed
- **Classification never asked a model at all.** The synchronous path began with
  `if provider == "workshop": return 0` — and the provider *was* the workshop
  (which is asynchronous by nature). Meanwhile the readiness check reported
  "ready". Of 234 unknown sender domains, **none** had ever been classified. A
  silent `return 0` looks like "the model knew nothing" and means "the model was
  never asked". It now returns a **reason code**, the page says what is missing,
  and an asynchronous provider gets a proper **round trip**: put the question
  down, create one task, collect the answer on a later run. Every line of that
  answer is checked against the allowed categories — an answer from a task queue
  is **foreign text**.
- **Ten tasks sat untouched for sixteen days without a single error.** The cause
  was outside this program, but the lesson is inside it: creating a task is not a
  result, it is a claim until somebody has started it. *Tick the box on the
  target system's confirmation, and give that confirmation a deadline.*
- A MIME-encoded display name spanning two lines made `parseaddr` return half the
  **name** as the address — 11 obvious travel mails had no sender domain at all
  and no rule could match them. Fixed at the single choke point all four callers
  now share: the normal path first, decoding only as a **fallback** when no "@"
  came out, so the repair cannot break what already works.
- The incremental inventory now records a **reader version**. A fingerprint over
  UIDVALIDITY/MESSAGES/UIDNEXT says the *mail* has not changed — not that we read
  it the same way. Without it the sender fix above would have stayed invisible.

### Changed
- A vocabulary decision never creates a **brand** folder, only
  `Category.General`. The vocabulary estimates the category from words; it does
  not know the brand. A wrong drawer is noticed at the next refinement — a wrongly
  **named** folder stays forever. *A guessed name is worse than a guessed drawer.*

### Fixed (found by looking at the picture, not by a checker)
- Two buttons in the documents card had been **hard-coded German** in the markup
  since 2.6.0 and showed up on every English page. The translation check only
  ever inspected the **JavaScript** — static text between tags was invisible to
  it. It now also checks every `<button>`, `<option>`, `<label>`, heading,
  `<summary>`, `<th>` and `<a>` **without** a translation key, plus `title=` and
  `placeholder=` in the markup.
- In the redirect card the checkbox and radio buttons sat **above** their labels
  instead of beside them, and the two mailbox selectors stacked vertically.
- `…/#move` did not open that tab: the hash was matched against a **second,
  hard-coded** list of views that nobody updated when the fourth tab arrived.
  There is one list now.
- Any URL with a query string returned **404** — the page path was compared
  against `"/"` including the query. `?standbild=1` now also freezes the page
  after the first load, so documentation screenshots are reproducible.

### Notes
- Everything above works **without any AI**. If you want a model, three providers
  are asked synchronously: **Ollama** (local — the one-click installer has shipped
  since 3.2.0), **OpenAI**, **Anthropic**.
- 127 checks in the restructuring probe. The counter-test was **green** on the
  first attempt, because it only reverted the weighting and not the skipping of
  unknown words — a counter-test that does not reproduce the original failure
  measures your own memory. 17 new translation keys (522 → 539).

## [4.4.1] – 2026-09-27

### Fixed
- **"Senders without a home": the button created the folder but left the mail
  behind.** Applying a new sender rule retroactively searched the **inbox only**.
  That was correct until 4.0.0 — unrecognised mail *stayed* there. Since the
  restructuring it is filed into the catch-all folder, so the message "nothing of
  it was in the inbox" was **true and useless**: the folder was created, the rule
  was right, and the mail was nowhere the user looked.
  🔑 *An assumption about the state of a mailbox has to travel with you when you
  change the state of that mailbox.*
- Retroactive moves now cover the **inbox**, the **catch-all** folder and every
  **`…Allgemein`** folder — the places where mail sits because nobody has decided
  yet — and **only** those: a curated folder is a decision already taken, and a
  new rule must not overturn it retroactively. The reply now names where the mail
  came from, and the journal records the real source folder (not a blanket
  "INBOX", which would send an undo to the wrong place).
- **The language check had been reporting green for a hole in its own pattern:**
  `\bnicht\b` does not match the German "nichts", and only lines containing
  `"text":` were inspected at all. Widened on both counts — which surfaced **12
  untranslated server replies** in one go, including one in the page that had been
  there since 2.x. 17 new keys, two changed (504 → 522).

### Changed
- Clearer layout for "senders without a home": two labelled groups with an "or"
  between them instead of four controls at equal spacing — **the spacing is the
  grouping** — and buttons that name the consequence ("Move here", "Create &
  move") instead of the mechanism.

## [4.4.0] – 2026-09-27

### Added
- **Mailbox migration between two providers.** Six separate stages, each
  harmless on its own: `pruefen` (both reachable, is B writable), `plan` (what
  goes where — touches nothing), `uebertragen` (read at A, create at B, then
  **verify**), `abgleich` (count live: is everything at B), `quelle-leeren`
  (delete at A — dry run by default, own release token), `umleitung` (a standing
  redirect, every minute).
- **Mail is filed while it moves.** The migration asks the same classifier the
  restructuring uses, so a message arrives at B in the folder it would have
  ended up in at A — without the intermediate step.
- **Standing redirect through the watch instead of a provider-side forward.** A
  provider forward cannot be sorted and cannot be verified: it sends a copy and
  forgets it. Here the watch collects mail at A, files it at B, verifies it is
  there, and only then clears it at A. Default target is B's **inbox**, so the
  watch at B still reports, files, hands documents over and counts statistics.
- Fourth tab **Move** with the six stages, progress, the target tree at B and
  the redirect switches. 62 new keys in all five languages (442 → 504).
- `probe_umzug.py` — 88 checks, run once against a deliberately broken build.

### Notes on the design
- **There is no `COPY` between two servers.** Every message is read at A
  (`BODY.PEEK[]`) and created at B (`APPEND`). That is a copy: afterwards the
  mail exists **twice**. This is why emptying A is a separate stage with its own
  release token, not the silent end of the transfer.
- **Identity is the Message-Id, not the UID.** After the `APPEND` the same mail
  has a different UID at B. A mail **without** a Message-Id is transferred and
  **never** deleted at A — it could not be found again.
- Deleting at A requires all three: the Message-Id verified **live** at B, the
  folder's UIDVALIDITY unchanged, and the UID at A still carrying that very
  Message-Id. An empty search result is **not** a confirmation.
- Special folders are matched by **flag**, not by name (`\Sent` → `\Sent`):
  matching by name creates a second Sent folder at the target.
- The canonical `.` in folder paths is a **separator**, not a character in a
  name. It is translated into the target server's delimiter, and the personal
  namespace is **asked for** (`NAMESPACE`), never guessed.
- Only `\Seen \Answered \Flagged \Draft` travel along. `\Deleted` would be
  a delete order at the target, `\Recent` is illegal in `APPEND`, and a custom
  keyword makes many servers reject the whole `APPEND`.

### Fixed
- `umbau.py` had been missing from the container image and the public tree since
  4.0.0, and the rollout check reported green — because the file was never on its
  **PROGRAMM** list. That list is the promise: what is not on it is checked
  nowhere. Both `umbau.py` and `umzug.py` are now on every rollout path.

## [4.3.1] – 2026-09-27

### Changed
- A folder that was already empty may now be removed if its **parent** held
  mail — one protected leftover otherwise keeps a whole dismantled branch
  alive. Top-level folders without a parent are still kept: the user created
  those.

### Performance
- The inventory only reads folders that changed. A signature of
  `UIDVALIDITY` + `MESSAGES` + `UIDNEXT` is fetched in a single round trip and
  compared against the previous run; `--voll` forces a full read. After the
  reorganisation the mailbox has 132 folders instead of 53 for the same mail,
  and the cost sits in the folder, not in the mail.

## [4.3.0] – 2026-09-27

### Fixed
- **A multi-purpose sender's main domain no longer decides where mail goes.**
  A comparison portal sells travel *and* insurance; filing all of it under one
  of them is a coin flip. The whole address (local part, subdomain) plus the
  subject now decide, with a per-domain fallback when nothing matches. Rules
  that match an exact address are untouched — they are already as precise as it
  gets.
- Word stems instead of whole words in those patterns: "insurer" does not
  contain "insurance".

## [4.2.0] – 2026-09-27

### Added
- **Progress for every long phase** — checking folders, removing them, and
  relearning the map (which takes minutes and used to be silent). The page
  shows the phase, a progress bar, how long the run has been going, and says
  so explicitly after two minutes without a sign of life.

### Fixed
- A transient network failure no longer aborts a long run: the connection is
  retried three times with a growing pause, and the last failure is surfaced in
  the status rather than swallowed.
- Starting a step now resets the previous step's counters, which otherwise
  showed as progress that did not exist.
- Phase names are no longer assembled at runtime — a composed translation key
  is invisible to the language check, and so is a missing translation.

## [4.1.0] – 2026-09-27

### Added
- **Removing empty folders** — the single exception to "nothing is ever
  deleted", and it applies to the *folder*, never to a mail. A folder goes only
  when all of these hold: not the inbox and not a protected folder, no
  special-use flag (recognised by `\Trash`/`\Junk`/`\Sent`/`\Drafts`/
  `\Archive` — the flag, never the name), not a target of the current plan, no
  remaining children, it held mail before the reorganisation, and a live
  `MESSAGES 0`. No answer from the server counts as *not empty*.
- Folders that were already empty before are kept — the user created those.
- Dry run is the default; the page asks before removing, and every removed
  folder is journalled.

### Fixed
- After a reorganisation the guard's learned map pointed at folders that had
  just been emptied. It is now relearned immediately, inside the still-open
  connection.

## [4.0.0] – 2026-09-27

### Added
- **Mailbox reorganisation (`umbau.py`)** in three separate stages: `inventar`
  (read-only census of every header), `plan` (computes a proposal without
  touching the mailbox) and `anwenden` (moves, and only against the plan's
  `sha256` fingerprint). `zurueck` returns every move to its origin.
- **Structure derived from content**, not from a fixed list: brand names are
  taken from the domain (`news.miele.de` → *Miele*), categories from a majority
  vote over subjects. A configured model refines what the patterns miss.
- **New mail sorts itself** (`selbst_sortieren`, on by default) and creates the
  folder it needs. Phishing still always stays put, and alerts are unchanged.
- A third tab on the web page drives all of it, with live progress.

### Changed
- This lifts the 2.0 rule "only file where you have filed yourself" — it is now
  a switch, not a law.

### Safety
- Nothing is ever deleted: copy first, flag `\Deleted` only on a confirmed
  copy, one `expunge` per folder, every move journalled and reversible.
- `Sent Items`, `Drafts`, `Trash` and `Spam` are never touched.
- A move only happens when the UID's `Message-Id` matches the plan — an empty
  lookup counts as *not confirmed*, never as confirmation.

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
