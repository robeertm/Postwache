# Changelog

All notable changes to the Postwache are documented here.
Dates are ISO; versions follow `MAJOR.MINOR.PATCH`.

This file starts with the first public release. The project was developed
privately before that; the summary under *0.1.0 – 2.6.2* lists what arrived
along the way rather than every single step.

## [5.11.1] - 2026-10-02

### Fixed

**The container health check never worked.** Docker reported
`Up 6 hours (unhealthy)` and printed a Python SyntaxError next to it. The
HEALTHCHECK in the Dockerfile was a one-liner that wrote `\n` INSIDE a
double-quoted shell string. `sh -c` does not turn that into a newline: Python
received a backslash and an n and died before it looked at anything. The
failing streak was 392 — the check had failed on every single run since 3.0.0,
for six months, on every container that ever ran this image.

A `try:` block cannot live on one line, so the one-liner needed newlines it
could not have. The check is now a **file** (`gesundheit.py`), which can be
compiled, imported and run — and a bench and the pre-delivery gate now do
exactly that.

**It also knocks on a door of its own.** `/api/gesundheit` sits in front of
every lock check and does no work. The old check asked `/api/lage`, which
reads state and, since 5.11.0, starts a Tailscale subprocess: measured
120–320 ms against 3 ms for the page, inside a 5 second limit every 60
seconds. It was also behind the page lock, which is why the old check had to
count 401 as healthy — a special case that only existed because the wrong door
was used.

The port is asked, not assumed: `gesundheit.py` reads `POSTWACHE_WEB_PORT`,
the same environment variable `post_web.py` reads.

**A second, worse fault turned up while fixing it.** `post_web.py` used a bare
`VERSION` in two places — and there is no such name in the module. One of them
is `_ist_docusort()`, the check that asks „is that really a DocuSort over
there?". Its `NameError` ran into `except Exception: return ""` and was
swallowed, so the function had **always** answered „not a DocuSort" and the
automatic search could never find anything.

The coupling bench had not caught it because it replaces that function with a
stand-in to test the SEARCH — which left the function itself untested by
anything. It now has probes of its own, against a fake DocuSort on a real
socket.

## [5.11.0] - 2026-10-01

### Added

**Tailscale is now a field and a button in the settings.** It was possible
before, as a second compose file driven by a shell script — which assumes a
command line, an editor, and somebody who knows which directory they are
standing in. Settings → *On your phone, from anywhere* asks for one key and
then shows the address:

```
https://postwache.<your-tailnet>.ts.net
```

Open it on the phone, add it to the home screen, and it looks like an app.
Nothing is published to the internet and no port is forwarded.

This works because `tailscaled --tun=userspace-networking` needs neither
`NET_ADMIN` nor `/dev/net/tun` — measured in a bare container twice: as root,
and as the unprivileged user this image steps down to. The second measurement
was the one that mattered, because nothing here runs as root.

Three details decide whether it keeps working:

* the login lives in the mounted state directory. Inside the image it would be
  gone at the next `docker compose pull`, leaving a dead machine in your tailnet.
* it resumes by itself after a restart. With hourly updates, anything else would
  mean pressing the button every hour.
* the page is offered on the port the Postwache **actually** listens on, not on
  a default — that mix-up puts a valid certificate in front of nothing.

The Keys page has two buttons, and only the upper one works here: *Generate auth
key…* under **Auth keys**. *Generate access token…* is a key for the Tailscale
API and cannot log a machine in. The two differ at the front, so a key of the
wrong kind is turned away before anything is tried, with the right button named.

The sidecar route is unchanged, and remains the right one when Tailscale should
also carry other containers.

## [5.10.0] - 2026-09-30

### Changed

**The DocuSort pairing now looks on both ports.** DocuSort moved its default
web port from 8080 to 9876 with its own 0.67.0, because 8080 is already taken
on a lot of machines. The Postwache finds DocuSort by asking a handful of
addresses that follow from how the two are installed, and 8080 was baked into
that list.

Both ports are asked now, 9876 first: an installation older than DocuSort
0.67.0 still sits on 8080 and is still found, and when both answer the newer
one is the one reported, because the order of the list is the order of
preference.

**And the search runs side by side instead of one after another.** Every
unreachable address costs four seconds, and asking two ports turned five
candidates into nine — sequentially the button would have needed half a minute
to say "nothing found". `map` keeps the order, so the preference above is
untouched. Measured: nine addresses at one second each, done in 1.01 s.

`docker-compose.both.yml` and `deploy/install-both.sh` follow DocuSort's new
port.

## [5.9.2] - 2026-09-30

### Removed

**`--tiefer` from 5.9.0 is gone.** It tried to hand back the 68 px that a
home-screen app loses under its tab bar, by measuring `100lvh - 100svh`. It was
derived from measurements, it was green on the bench, and on a real phone it
moved nothing — twice. A safety net nobody has ever seen catch anything is not
a safety net, and the comment claiming it covered the leftover case was a claim
with no measurement behind it. The custom property, the `html[data-app]` rule,
the three fixed elements that used it and the `navigator.standalone` flag are
all removed.

What remains is the change that was shown to work: the two Apple meta tags in
the head are gone (5.9.1), and `theme-color` follows the light setting before
the first paint.

### Note

iOS keeps the mode a page was installed with. An icon put on the home screen
while those tags were still present will not pick up the new head — remove the
icon and add it again.

### Changed

`pruefstaende/probe_handyleiste.py` rewritten, 13 checks: the two tags are gone
(asked of the DOM, not of the text, because the comment explaining their removal
still spells their names out), `viewport-fit=cover` stays, the meta colour is
the night ground, the literal day colour in the head script matches the
stylesheet and is set before `<style>`, and the geometry — sheet fills the
window, bar ends on the bottom edge, the home indicator keeps its 34 px, the
header starts at 56. The counter-test puts both tags back, because otherwise
section 1 would only be testing absence, and an empty file has that too.

## [5.9.1] - 2026-09-30

### Fixed

**The cure for 5.9.0 was in the document head all along.** Two tags were asking
for the behaviour that 5.9.0 worked around:

```html
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
```

With them, a page put on the home screen is DRAWN from the very top of the
screen but MEASURED as though it began below the status bar, which is where the
68 px gap under the tab bar came from. Without them the system draws the status
bar itself, the page begins below it, and window and screen agree. Both tags
are gone; `viewport-fit=cover` stays, because the safe-area insets need it.

`--tiefer` from 5.9.0 stays as a safety net rather than as the cure: an icon
added to the home screen while those tags were still present keeps the mode it
was installed with, and that cannot be changed from the page. It measures
itself and is 0 wherever there is no gap, so on a freshly added icon it does
nothing.

**`theme-color` now follows the light setting before the first paint.** While
the page drew its own ground underneath the status bar the colour never
mattered; now the system draws that bar, and starting in day mode would have
put a black bar above a paper page. The switch already derived the colour from
the ground at runtime — only the head script was missing it.

### Changed

`pruefstaende/probe_handyleiste.py` - now 19 checks. It asks the DOM whether
the two tags are gone rather than searching the text, because the comment
explaining their removal still spells their names out, and it holds the literal
day colour in the head script against the `--grund` the stylesheet defines so
the two cannot drift apart. The phone-page check in `probe_klient.py` used one
of those tag names as its marker and would have stayed green for exactly that
reason; it now looks for the tab bar.

## [5.9.0] - 2026-09-30

### Fixed

**A page added to the home screen now uses the whole screen.** On iOS a page
started from the home screen gets a window that is shorter than the screen it
is drawn on — measured on a phone running iOS 18.7: screen 912 px,
`window.innerHeight` 844 px, `safe-area-inset-top` 68 px. The page is still
drawn from the very top of the screen, so the missing 68 px collect at the
BOTTOM, below the tab bar, where nothing can be placed.

`100lvh` knows the whole screen (912) while `100svh` knows only the window
(844), so the window is not really shorter — only the measure that
`position:fixed` counts against. The difference is the piece that goes missing,
and it measures itself: on a device without the flaw it comes out 0 and nothing
moves. The bottom bar now sits on the real edge of the screen and the message
list is 68 px taller.

Two things this depends on, both measured rather than assumed:

* `matchMedia("(display-mode: standalone)")` answers **false** on iOS even
  while the page is running as an app, so the obvious CSS query does nothing.
  The only reliable sign is `navigator.standalone`, and it has to be read in
  the document head, before the first paint, or the bar visibly drops a moment
  after the page appears.
* In Safari the same difference is the browser's own bars (786 - 678 = 108 px).
  Applying the correction there would push the tab bar behind the toolbar, so
  it is applied only in the home-screen app.

### Added

`pruefstaende/probe_handyleiste.py` - 14 checks in WebKit covering the geometry
of the bottom bar, the gate and the floating message, plus a counter-test
against the previous layout. Headless, `100lvh` and `100svh` are the same
number, so the bench proves the plumbing and a throwaway page on a real phone
proved the measurement.

## [5.8.1] – 2026-09-29

### Changed

**The benches are no longer published.** Nine `probe_*.py` files stood in the
root next to seven files of program — scaffolding in front of the house, and
the first thing anybody saw when they opened the project. They were never part
of the product: nothing imports them, no `Dockerfile` copies them, and no
rollout has ever carried them. They stay in the owner's tree from now on, so
the root shows the program and nothing else.

Earlier entries in this file still name them by filename. Those entries stay as
they were written — they record what happened at the time; this is the note
that explains where those files went.

## [5.8.0] – 2026-09-29

### Added

**More than one page at a time — or a whole folder.** Ticking every row only
ever reached the fifty on screen, and turning the page threw the selection
away. Now the selection lives across pages, and once a page is fully ticked the
bar offers **Select all 1,247** — meaning every mail the current view holds. A
search or a filter narrows it the same way: „all" is every HIT, never more than
what stands on screen, because the numbers come from the very list the page is
cut out of.

On the phone it is the same button in two steps rather than a fourth control in
a 320 px bar: the first press takes what is loaded, the second takes the folder.

### Fixed

🔴 **A large selection could not have worked before, and would have failed
silently.** An IMAP command is one line and servers cut it off after a few
kilobytes — 12,000 mails written out one by one are **60 KB**. They now go out
as RANGES (`1:12000` — seven bytes for that same folder), and a set that cannot
be folded together, such as scattered search hits, is additionally cut into
pieces short enough for one line. Measured: 3,000 scattered mails leave in 19
commands, the longest 931 bytes; a gapless folder leaves in exactly one.
The watchman's rule is untouched: `\Deleted` is set only after a confirmed
copy, now per piece.

Requests are cut the same way, 500 at a time, so no single request runs for
minutes — and between two pieces the bar says how far it has got.

## [5.7.1] – 2026-09-29

### Fixed

**No more quoting a person in a published file.** The rule that a public
document carries no verbatim quotation was guarded for the README and the
changelog — and the published SOURCE carried 21 of them, with a name and a
date in front, plus 36 further mentions of the owner's first name. Every one
of them is gone; the REASON each of them recorded stays, because it explains
why the code looks the way it does. Nothing about behaviour changed.

🔑 The new guard in `probe_sprachen.py` cannot work from a name — the bench is
published itself, so it must not carry one. It works from the LANGUAGE
instead: every comment here is English, so German prose inside quotation marks
is, by construction, somebody's own sentence being repeated. A quoted term is
not prose, and the difference is measured by density, not by length.

## [5.7.0] – 2026-09-29

### Changed

**Tailscale: from an instruction to one command.**

```bash
./deploy/tailscale.sh tskey-auth-xxxxxxxxxxxx
```

That is the whole setup. The script writes the key AND `COMPOSE_FILE` into
`.env`, starts everything, and prints the finished address — which it reads out
of `tailscale cert`, the same trick already used elsewhere in these projects.

🔑 `COMPOSE_FILE` is the real gain: with that line in `.env`, a plain
`docker compose up -d` uses both files from then on. Nobody has to remember
`-f docker-compose.yml -f docker-compose.tailscale.yml` again — and nobody
starts by accident with a published port and no tailnet because they forgot it
once.

**Watchtower is switched ON, no longer commented out.** Nightly at 04:00, the
time movable with `WATCHTOWER_SCHEDULE` in `.env`. It is the maintained fork
`ghcr.io/nicholas-fedor/watchtower`, and it watches ONLY the `postwache`
container — which is exactly why it does not fight with a Watchtower you
already run.

🔴 Worth knowing: an existing Watchtower does NOT automatically pick this up. One
started with a list of container names updates only those. The README says how
to tell.

### Added

**The connection to DocuSort makes itself.**

* **Installed together → no clicks at all.** `deploy/install-both.sh` or
  `docker-compose.both.yml`: one secret in one `.env`, read by both sides.
  DocuSort creates the service account with it, the Postwache writes it down.
* **Installed separately → one click each side.** DocuSort shows a **pairing
  line** under *Settings → Postwache*; there is a field for it here. Plus a
  **Find DocuSort** button that asks from *here* — because reachable means
  reachable by the one who has to arrive, not by the browser.

🔴 The https rule stays, with exactly two exceptions: this machine
(`localhost`, `127.0.0.1`) and a name without a dot (`docusort`) while the
Postwache itself runs in a container — then it is a service name from the same
compose file and the request never leaves the host. A home-network address is
still refused; a password in the clear across the LAN is precisely what the
rule exists to prevent.

**`probe_kopplung.py`** — 30 checks, no network needed: the address rule, „the
environment sets up, it does not overwrite", a rotated key, the pairing line,
and three broken lines that must write nothing.

### Fixed

🔴 **The build workflow also ran on tags — and pushed `:latest` along with it.**
Tagging an older version would have handed every customer OLD code as `latest`
on their next pull. It now builds from `main` only; every version already gets
its own image tag from that push.

### Also

**Tags and releases for every version** (v4.5.1 … v5.7.0), so there is a
downloadable archive for each.

## [5.6.0] – 2026-09-29

### Added

**Tailscale as the way in — no open port.** A new `docker-compose.tailscale.yml`
sits next to the main file as an OVERLAY, so an existing install keeps working
untouched and the private-network path is one extra `-f` away:

```bash
echo 'TS_AUTHKEY=tskey-auth-…' >> .env
docker compose -f docker-compose.yml -f docker-compose.tailscale.yml up -d
```

The Postwache is then at `https://postwache.<your-tailnet>.ts.net`, HTTPS with a
certificate Tailscale fetches and renews by itself. No port open, no reverse
proxy, no certificate to look after — and nobody outside the tailnet has
anything to knock on.

🔴 `ports: !reset []`, not `ports: []`. Compose MERGES lists across files: with
an empty list the published port from the main file stays, and a container that
publishes a port AND rides another's network is refused by Docker at start.
Found by actually rendering the merged file — `docker compose config` called the
broken version valid.

🔴 The auth key lives in `.env`, never in the repository, and is needed once:
afterwards this machine's identity sits in `tailscale/state/` (git-ignored) and
the key can be revoked. Counter-tested: without `TS_AUTHKEY` the start aborts
instead of quietly coming up with no network.

### Fixed

**The phone view, measured sheet by sheet.** A new instrument walks all ten
sheets of the mailbox and all four tabs of the watchman page at three widths
(320/390/430 px) in WebKit — the engine an iPhone actually draws with — and
measures horizontal scroll, overhang past the right edge, tap targets under
44 px, text under 12.5 px, clipped text and the iOS zoom trap (inputs under
16 px). Result: **0 horizontal scroll, 0 overhang, 0 clipped text, 0 zoom
traps**, on every sheet at every width.

Three things only the PICTURE showed, and all three are fixed:

* the unread filter's **●** stood in the header as a white lump beside three
  thin signs — as a glyph it is the size of ☰, only solid. What it means is the
  small dot the list puts on an unread mail, so it is drawn that size now; the
  tap target stays 44 × 44;
* „not calculated yet" appeared **twice in a row**, once as the subtitle and
  once in the card body. The subtitle says WHERE the numbers come from; with no
  numbers there is nothing to describe, so it stays empty;
* the history ended on a bare „ · " — the separator was printed whether or not
  anything followed it.

## [5.5.0] – 2026-09-29

### Changed

**Opening the mailbox now starts in „New", not in the inbox.**

That is not a matter of taste. The watchman moves new mail **out** of the inbox
into a dozen folders — so somebody who lands in the inbox is looking at the one
place that does NOT show what has arrived. The „New" view puts those folders
back together into one list, with the folder written beside every row.

Measured on both surfaces: a fresh connection opens „New"; choosing a folder
keeps that folder; the reload button does **not** pull you back; and only a new
connection starts at „New" again. A folder stays set beside it, because every
action that names no target falls back on it.

### Fixed

**A hole in the language bench.** The check that keeps comments English saw only
the FIRST line of a multi-line `/* … */` block: the following lines start with
no comment marker of their own, and the rule meant for „a comment behind code"
threw them away. German prose inside the body of a block comment had never been
looked at since the translation in 4.6.0. The decision is now made once per
BLOCK. After closing it the existing code proved clean — the only findings were
three comments from this very update.

🔑 And a quotation INSIDE a quotation is set with single marks (‚…'), or the
inner closing mark ends the outer one and half the sentence counts as prose
again.

## [5.4.2] – 2026-09-28

### 🔴 One slot, one meaning — the theme switch keeps its choice again

Reported from the phone: the day/night button „does not work". It did. Every
press was correct. What failed was the **slot its choice lives in**:
`localStorage["pw_ansicht"]`. Because under that very name the watchman page
remembered its **open tab** — same store, same origin. And a **cookie** of the
same name carries a third meaning: wide page or phone page.

Measured along the path a reader actually walks:

| | slot `pw_ansicht` | phone page |
|---|---|---|
| ☀ pressed on the phone | `"hell"` | light ✓ |
| reloaded | `"hell"` | light ✓ |
| **watchman page merely opened** | `"uebersicht"` | — |
| back to the phone | `"uebersicht"` | 🔴 **dark** |

The other direction fails just as reliably: the watchman's own theme button wrote
`"hell"` over the remembered tab, and the next tab click wrote the tab over the
theme choice. Two meanings in one slot erase each other **in a circle**.

🔴 The warning had been sitting two lines above it since 5.4.0: *„`licht`, NOT
`ansicht`: the mailbox already answers to `?ansicht=breit`"*. What got renamed
back then was the **parameter in the address** — not the **slot in the store**. A
half-finished rename, and the visible half was the correct one.

Now:

* theme choice → `pw_licht` (named after its `?licht=` parameter)
* watchman's open tab → `pw_reiter`
* wide-or-phone cookie → stays `pw_ansicht`; there the word is honest, it lives in
  a different store, and the server writes it

### 🔴 And a second sign that promised the same thing

The phone's list header carried a **◐** — the unread filter. A half-filled circle
is what a **day/night** switch looks like in almost every other program, and it
sat exactly where one is looked for. Pressing it gave a filtered list and no
light. It now wears **●**, the very mark the list puts on an unread mail.

The bench holds it: **no other control wears a half circle.** It reads the pages
without their comments — the probe is about what a page *shows*, not about what
it explains about itself (the first version turned red on my own note). Bench
therefore **321**.

### Test bench: 309 → **321 probes**

A schema instead of a spot check: all four storage slots are **declared with their
purpose**, and a slot appearing in a page that has no business with it turns red —
as does any slot name that was never declared at all. Plus the rule that would
have caught the half-finished rename: **the parameter in the address and the slot
in the store must say the same word.** Counter-tested against the old state: 7
probes red, the two unrelated slots green.

🔑 A duplicate `const` at least kills the script **loudly** (5.4.0). A shared
storage slot fails in **silence** — and looks exactly like a button that does not
work.

## [5.4.1] – 2026-09-28

### 🔴 Two positions, and night wins

The theme button had three: automatic · day · night. „Automatic" followed the
device — and a device set to light therefore handed a light page to somebody who
never chose one. It has **two** positions now, and **night is the default, full
stop**. The page does not ask the device at all any more; only an explicit *day*
turns the light on — the one in the store, or the one in the address.

Measured in a browser set to **light**:

```
page:   data-hell = null · ground rgb(18,16,12) · choice „dunkel" · button ☾
one press: day   ·   two presses: night again
```

The test bench holds it: **no page asks for `prefers-color-scheme` any more**, and
every page knows exactly two positions.

### Folders: fold, fold everything, delete — on **both** surfaces

* **Folding** is now on the phone too (it was wide-only) — with the same fixed
  slot for the triangle on **every** row. A piece of geometry that is only
  sometimes there is what put the tree on its head in 5.2.0.
* **One button for the whole tree**: as long as anything is open it closes
  everything; only when everything is closed does it offer to open. Two buttons
  would leave one of them idle half the time. Measured: 18 rows → 13 → 18.
* **Delete**: the ✕ on the folder row (wide) and the folder sheet (phone). 🔴 The
  only action in the whole client that cannot be taken back — so the engine is
  asked first and **counts while it refuses**: “Delete “Garden” with 77 mails for
  good? The mail is gone afterwards — for ever, with no trash to fish it out of.”
  Subfolders are refused (those first, each with its own count), special folders
  always.
* A **long press** on the phone now opens a small folder sheet with both ways —
  move *and* delete — instead of jumping straight into the folder picker.

### The watchman learns every folder that comes or goes

„By hand" means **anywhere** — in the Postwache, in another mail program, on the
provider's own page. So the learning does not hang off a button but off the only
thing that is always true: **the list the server gives.**

The learned map is reconciled against what really exists — on **every** watchman
run (one `LIST`, one command) and immediately after every create, delete and move
in the client.

| | |
|---|---|
| a new folder | enters the map with **zero** mails — and is a target at once: the name bridge hangs only on the NAME, so an empty folder „Steuer" can take post from `steuer@…` from its first second |
| a folder that is gone | leaves the map, **together with every rule that pointed at it** |
| 🔴 why that matters | a rule naming a folder nobody has any more makes the filing **fail** — every five minutes again, and nobody sees it but the log |
| 🔴 an empty list | is a **failed request**, not an empty mailbox: it deletes nothing |

*A correction to 5.4.0:* it said the filing would „create" a missing folder. That
is true only for the path where the watchman derives a folder **itself**; out of
the learned map the copy simply fails and the mail stays put. Same damage,
different mechanism.

### Waiting, made visible

A `RENAME` is **one** command — but the server may be carrying two thousand mails
while it runs, and a page that says nothing until it is over looks broken. There
is no progress to report (the command reports none), so the page shows the honest
thing: *what* is happening and that it can take a while — with a spinner, and the
message **stays** until it is done. Measured: still visible after 5.9 s, where an
ordinary message is gone after 5.

### The column travels with you

Dragging a folder from the very bottom to the top did not work when the list was
longer than the window. 🔴 And a scroll step **per event** would not have done
it: `dragover` only fires **while the pointer moves** — somebody who holds still
at the top edge and waits gets no further events at all. So the edge starts a
**timer**, and the closer to the edge, the faster. Measured: the column rolled
from 369 px to 0, and stopped the instant the drag ended.

### Folders that are alive

* The icon **answers**: it leans in on hover, ducks on a press, stands up when
  something is dragged over it.
* A folder with unread mail **breathes** — slowly (3.4 s), so it reads as „there
  is something here", not as an alarm.
* 🔴 Whoever set their system to less motion gets **none**:
  `prefers-reduced-motion` switches both off. An animation you cannot switch off
  is one you have to look away from.

### Test bench

274 → **309 probes**, all green. The fake server now remembers `CREATE` and
`DELETE` (a server that says OK and forgets proves nothing) and reports an honest
**zero** for a folder created a moment ago.

## [5.4.0] – 2026-09-28

### The listening post — hearing instead of asking

The page used to ask every minute whether anything new had arrived. That is, at
best, a minute late. IMAP has `IDLE` for exactly this: the server speaks up on
its own. So the page now leaves **one request waiting**, and it comes back the
moment the provider says a word.

**Measured on a running page**, a mail dropped into the mailbox, nobody touching
anything:

```
wide version:  the new mail is in the list after 0.13 s
phone:         after 0.22 s
requests used: 3 (two while loading, one waiting)
```

The listening post keeps **its own connection**, and that is not a detail: an
`IDLE` sits on a connection for minutes, and the warm connection is the one every
click goes through. Put the post on that lock and opening a mail would wait for
the next new mail.

* It belongs to the **mailbox**, not to the tab: three open tabs ask **one** post,
  which holds **one** connection at the provider.
* It **cannot write** — not out of discipline but by construction: the connection
  opens every folder `EXAMINE`, read-only.
* It **lets go**: 90 seconds without a listener and the connection is closed. A
  tab left open at night holds nothing at the provider until morning.
* A server **without** `IDLE` is not a reason to give up but a reason to ask
  politely: every 20 seconds. Still three times closer than before.
* Coming back to the tab refreshes at once.
* 🔑 Nothing is ever redrawn under your hands: a selection, an open window, a
  focused field, a search, any page but the first — each of them holds the list
  still. The folder **counts** move anyway; a counter steals nobody's click.
* `UIDNEXT` only grows when something actually **arrives** — that is how the page
  tells “new mail” from “something changed”.

### 🔴 The empty page after a delete

Delete 50 of 100 mails spread over two pages: the mails go, and “page 1 of 2”
stands there **empty** until you click to page two. The rows were taken out **by
hand**, and it stopped there — the counter, the page count and the rows that move
up into the gap all live on the server.

Taking the rows out is the **instant** answer; refetching is the **right** one —
in that order. Measured with a real delete on a running page:

```
before   page 1 of 2 · 15 mails · 10 rows · “1–5 of 15”
after    page 1 of 1 ·  5 mails ·  5 rows · “1–5 of 5”
counter-test without that one line:  0 rows, counter still says “1–5 of 5”
```

On the phone, which stacks its pages, the refill asks for exactly the span that
was already shown — **one** request, and the reader keeps their place.

### A day mode, in the same colours

Nothing is exchanged for another colour family: the brown-black night becomes
**warm paper**, the gold stays gold, the three lights over the page stay where
they are.

* **Three positions, one button**: automatic (follows the device) · day · night.
  On all three pages, in the same corner.
* The choice lives on the **device**, not in the mailbox: a phone in a dark room
  and a desk in the sun are two different answers to the same question.
* When the device switches at sunset, the page switches with it.
* 🔑 **Two golds**: one to **write** with, one to **fill** with. In the dark they
  are the same colour; on paper `#e0a458` as text is a hint, not a word — so the
  writing gold goes a few shades deeper while the filling gold stays exactly the
  Postwache's own.
* The **letter** turns light as well: it is a document of its own inside the
  frame, built by the engine, so the page says with every mail which time of day
  is in force.
* Signal colours go darker. `#34d399` on white is a colour nobody can read.
* `?licht=hell` in the address shows the other side without touching your own
  setting — that is also how the documentation takes its pictures.

### 🔴 Two names that were already taken

`const ANSICHTEN` for the day/night button — and `ANSICHTEN` is already **the
list of tabs** on the watchman page. A duplicate `const` is a **parse error**, and
a parse error kills the **whole** script: the page still draws its markup and
looks perfectly normal in a screenshot while not one line of it runs. Found
because the button stayed empty; the test bench now looks for a name declared
twice in any page (counter-test: it reports exactly `ANSICHTEN`).

The same one floor down: `?ansicht=` already means **wide or phone** for the
client. The parameter for the theme is therefore `?licht=`.

### Whole folders travel — and the watchman is told

* **Wide version:** drag a folder onto another one. The “own folders” heading is
  the target for “back to the top”.
* **Mail travels the same way:** drag a row onto a folder. Drag a row that is part
  of the **selection** and the whole selection goes; drag one outside it and only
  that one goes.
* **Phone:** press and hold a folder — it then travels in the same sheet that mail
  is moved with.
* The move is **one** IMAP command: `RENAME`. The server takes the mail **and** the
  subfolders with it, and the numbers stay. Copying would be thousands of mails
  over the wire and a window in which the same post lies in two places.
* **Special folders stay.** Sent, Drafts, Trash and Junk are not drawers somebody
  sorted into, they are **positions** — every mail program looks for them there.
* Into itself, into one of its own children, onto a name already taken: all
  refused beforehand. The subscription is renewed, for the folder and every child.

And the larger half: the provider needs one command, the watchman's memory needs
more. Carried over in the same breath:

| What | Why |
|---|---|
| the learned filing map | 🔴 it decides where post goes. Leave the old name in it and the next run files into a folder that is not there — **and filing creates it**. The mail would end up split. |
| the document index | it reaches for an attachment by folder and number. Old name, no attachment. |
| the journal | it is the **way back** for every mail the watchman ever moved. |
| what it kept of each mail | that is where the page reads which folder a mail went to. |
| folders pinned by hand in the settings | whoever chose their own archive should not lose it. |

If that half fails, the page says **both** halves: the folder has moved, the
watchman did not understand it. “Did not work” would be a lie, and the reader
would press again.

### And a slogan

„Hier geht die Post ab" — a German idiom for a place where things are really
happening, with the word for *mail* sitting in the middle of it. Every language
gets its own idiom about mail that moves, not a translation of the German one:
*Where the mail gets moving* · *Ici, ça bouge dans la boîte* · *Aquí el correo no
para* · *Qui la posta vola*.

### 🔴 A checkbox is not a text field

`input,select{…width:100%}` also applied to checkboxes and radio buttons: they
stretched across the whole row and pushed their own labels to the far edge. The
redirect card had looked broken since the day it was built, and two other places
had patched it with an inline style of their own — a rule that needs patching at
the call site is a rule in the wrong place. Now it is one rule, and the boxes
wear the house gold instead of the browser's blue.

### Test bench

242 → **274 probes**, all green. New: the listening post against the fake server
(which has learned `IDLE`, `UIDNEXT` and how to speak unasked), the folder move
including the memory, what each page promises about its day mode (**every light
token needs a dark counterpart** — a colour that only exists in daylight does not
exist at night), and the duplicate name. The language bench now also reads
`post_web.html` for keys that stand there as a **value**; it had been missing
from that list, and that is exactly where the three new ones were.

## [5.3.0] – 2026-09-28

### 🔴 `[hidden]` loses to every class with a `display`

The ✕ on the swipe hint **worked**: it set `hidden` and remembered it in
`localStorage`. The bar simply never went away — `.wischhinweis{display:flex}` is
a class selector and beats the browser's own `[hidden]{display:none}`. The
bookkeeping was right; the cascade was not.

Measured, not guessed: after the ✕ the element had `hidden: true`,
`localStorage: "1"` — and `getComputedStyle(...).display === "flex"`.

One line on all three pages settles it:

```css
[hidden]{display:none!important}
```

And it took **three more bugs** with it that nobody had reported: the Cc and Bcc
rows in the compose window (wide *and* phone) were always visible although they
carried `hidden` — so the “+ Cc” button did nothing visible. The test bench now
guards the line.

### Several at once — on the phone

* Two ways in: a **long press** on a row (what a phone teaches you) and a
  **button** in the bar (what you find when you do not know that).
* The avatars become **ticks**, in the same place, so the list does not shift.
* Header: ✕ · “N selected” · **Select all**. At the bottom a selection bar
  replaces the tabs: read · unread · flag · **move** · delete.
* 🔴 A long press still sends a `click` afterwards — the row would have been
  unchosen again immediately. The tap is barred for 800 ms.
* Swiping is off while choosing; back leaves the selection first.

### Moving is a sheet, not a number to type

Moving used to be a `prompt()` with a numbered list. Now it is the same folder
sheet as everywhere, with tree, indentation and guide line — for the one open
mail and for a whole selection. The folder the mails are already in is left out.

### 🔴 A UID is only valid inside its folder — for the selection too

The selection was keyed by **number**. In the “New” view the same number stands
in three folders: **one tick marked three rows**, and “mark read” would have gone
to the inbox three times. Measured: three ticks out of one click.

* The key is **folder + number** now, on both pages.
* Actions work on **(folder, number) pairs**, grouped per folder.
* Rows are addressed by their **place in the list**, not by number — otherwise a
  tap on the row from *House* opens the mail from the inbox.
* After an action rows are removed by **key**, not by number.

**Measured against the running server:** three mails from three folders chosen →
**three** requests, one per folder, each with the right number.

### 🔴 And one only the picture showed

The selection background was **translucent** — and behind every row lie the two
swipe actions. On every chosen mail that had been read, the bin shone through.
Two rules above it stands the comment warning about exactly that.

**Test bench:** `probe_klient.py` 216 → **225 probes**, all green.

## [5.2.0] – 2026-09-28

### ✨ Everything new, in one view

Above the inbox there is now a view of its own. It gathers from **every** folder,
and next to every line it says **where the mail is now**.

🔑 **This is the view the watchman makes necessary.** It carries new post into
its folders — and exactly because of that, "what came in" is no longer one folder
but twelve.

* Cheap by construction: the folder tree already knows the unread count of every
  folder from `STATUS`, so on the default setting *unread* only folders that have
  something unread are opened at all — usually two or three, not twenty.
* Instead of "unread" it can also be *since yesterday*, *last 3 days*, *last 7
  days* (IMAP `SINCE`). 🔴 That really means "since yesterday" and not "the last
  24 hours": `SINCE` compares the **date**, without a time — and the labels say so.
* Trash and drafts always stay out, junk on request.
* A folder with 2,000 unread newsletters does not eat the budget: **every folder
  gets the same share**, newest first, and when it had to cut, the line above
  says so.
* Sorted by **date** — the only order that means anything across folders. A UID
  is only comparable inside its own folder.
* Still `EXAMINE` and `BODY.PEEK`: looking changes nothing.

🔴 **And the trap behind it:** in this view the twelve rows on screen live in six
folders. Every action (flag, move, delete, open, reply, fetch an attachment) used
to take the folder from the view — which would have hit the **wrong mail** in
five cases out of six. Now one place answers it, and bulk actions are grouped by
folder: inside a folder that is exactly one group and exactly one request, as
before.

### 🔴 The folder tree stood on its head

The fold triangle was a **column that was only there sometimes**. A folder *with*
children therefore stood 1.4 rem further right than its own children, which were
indented one step **less**. The tree read inverted — and the indentation had been
right all along.

* The triangle is a **fixed column** on every row now, filled or empty.
* 🔑 Depth is measured **relatively**: nearly every own folder sits under
  `INBOX`, so counted absolutely the tree starts at step one and hangs in mid-air.
  The shallowest own folder is the left edge.
* **One branch per top-level folder**, with air between branches and a guide line
  under the parent.
* Role folders no longer fold: they are roles, not a hierarchy — and because they
  are called `INBOX.Sent` on most servers, the inbox carried a triangle that took
  every own folder with it when collapsed.

### 🔴 Two places decided the same thing

One function hid the folder controls, another put them back a millisecond later.
The new view drew its own toolbar and got the other one on top of it. One place
decides now; the other calls it.

**Test bench:** `probe_klient.py` 200 → **216 probes**, all green.

## [5.1.0] – 2026-09-28

### 🔴 A void element must not open a region
The reading pane stayed empty for nearly every HTML mail. The mail arrived
complete; the cleaner handed back the style block and nothing else.

`<meta>` sat in the "drop the content" list and **not** in the list of void
elements. A void element never has an end tag — so the counter went to 1 at the
`<meta http-equiv="Content-Type">` that stands at the top of almost every
newsletter, and never came back down. Everything after it was dropped. Only the
`<style>` block survived, because it is read before the counter is asked.

Measured with Playwright against **both** engines, Chromium and WebKit: frame
document 2,312 characters, body renders `""`, `scrollHeight 0`, 0 images.
Afterwards: 4,337 px tall, 2,388 characters of text, 26 images.

* The void list now holds **every** void element of HTML (`area base basefont br
  col embed frame hr img input isindex keygen link meta param source track wbr`),
  and a void or self-closed tag never raises the counter.
* The silent list holds only what really is not part of the letter. `head`,
  `body`, `form`, `button`, `option` are out of it: their content **is** the
  letter in a great many mails.
* Silence is closed by **name**, not by count — a forgotten `</script>` no longer
  takes the rest of the letter with it, and a stray `</iframe>` cannot lift a
  silence that was never set.
* **The wall behind the rule:** where the cleaner produces nothing visible, the
  letter is shown as text instead of not at all. An empty pane says "this mail is
  empty", and that was untrue.

**Run against the broken state: 8 of the new probes red.**

### The list looks like a mail program
* **A body excerpt in every row** — the first words of the letter under the
  subject. A list that shows only sender and subject makes you open a mail to
  find out whether it is worth opening.
  * Fetched in **pieces**: `BODY.PEEK[<part>]<0.900>`, never the whole mail. A
    mail with a ten-megabyte picture costs exactly as much as one without.
  * **One** fetch per shape, not one per mail: a page is grouped by part number
    and encoding, so fifty mails usually need two or three FETCHes.
  * A cut piece breaks both transfer encodings in its own way — base64 needs a
    length divisible by four, quoted-printable must not end inside an `=XX`, and
    half a UTF-8 character at the end is trimmed.
  * Can be switched off. And it is still `BODY.PEEK`: nothing is marked read.
* 🔴 **Every cell placed by hand — column AND row.** Automatic grid placement had
  put the date in the free column and pushed sender and subject to the far right,
  with the second line under the checkbox. It looked like a layout somebody had
  chosen. Nobody had.

### One house, one colour
* The same **three** lights as the watchman's page, on all three pages.
* The three columns are **cards** on that ground now instead of panes divided by
  hairlines — the same language as the watchman's stack of cards.

### One click to the mailbox
The watchman's tab row now carries **📬 Mailbox →** — not a tab but a door: a
real link to `/post` that works before any script has run.

### Also fixed
* With *images always* every mail was fetched **twice**, once without and once
  with images. Where the answer is known before the fetch, it is fetched once.
  And if the second pass fails, the letter stays readable instead of going blank.
* The fake IMAP server in the test bench now answers **partial fetches**.

**Test bench:** `probe_klient.py` 172 → **200 probes**, all green.

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
