<h1 align="center">Postwache</h1>

<p align="center"><i>„Hier geht die Post ab" — where the mail gets moving.</i></p>

<p align="center">
  <b>A watchman for your mailbox — and, since 5.0, a mail client.</b><br>
  It sorts the noise <i>out</i>. What matters stays where you already look.<br>
  And when you want to read it, write it or answer it, you do that here too.
</p>

<p align="center">
  <img alt="Licence" src="https://img.shields.io/badge/licence-source--available-8a8a8a">
  <img alt="Python" src="https://img.shields.io/badge/python-3.9%2B-3776ab">
  <img alt="Dependencies" src="https://img.shields.io/badge/dependencies-none-2ea043">
  <img alt="Image" src="https://img.shields.io/badge/ghcr.io-postwache-0b5">
</p>

![The mail client](docs/screenshots/20-client-wide.png)

<p align="center"><i>The client. Next to every mail stands the watchman's verdict —
which drawer, which folder, and why.</i></p>

---

## The one idea

Most mail tools ask *"where does this belong?"* and file everything. The
Postwache asks the opposite question:

> **What in here is just noise?**

Newsletters, adverts, delivery notices, backup reports — out of the inbox. A
bill, a letter from the authorities, a security warning, a message from a real
person — **left exactly where you already look for it.**

🔴 A sorter that tidies away something important is more dangerous than no
sorter at all. That single sentence shapes every decision in this program.

## It learns from you, not from a model

It reads how **you** have been filing mail for years and copies you: if three
mails from your energy supplier ended up in `Invoices`, that is where the next
one goes.

Since 4.5.0 it goes one step further. Your own folders are a **labelled training
set**: the watchman counts which *words in subject lines* belong to which
category across everything you have already filed, and classifies unknown senders
with that. Measured on a real 17,500-mail mailbox by cross-validation — every
mail judged *after* its own words were subtracted from its own category:

> **98.3 % accuracy at 69.4 % coverage — with no model, no API key and no network.**

The remaining 30 % it leaves in a catch-all folder and **says so**. A wrong drawer
is more expensive than an open question: in the catch-all you can *see* that
something is pending.

That is also why it costs nothing to run: **there is no language model in the
loop.** The watchman is plain Python, it looks at the new mail once a minute, and
a quiet run takes 0.2 seconds.

You can look at what it learned — `umbau.py wortschatz` prints the characteristic
words per category, the accuracy, and the most frequent confusions. A classifier
that will not tell you its hit rate is a claim, not a measurement.

## What it does

| | |
|---|---|
| 📥 **Several mailboxes** | Each with its own learned filing, its own sender profiles, its own attachment index. They never mix. |
| 🔇 **Sorts the noise out** | Into the folders you already use — and, when a sender keeps coming back, into a new one it creates for them. Nothing is ever deleted. |
| 🏗 **Reorganises the whole mailbox** | Read (read-only) → plan (touches nothing) → run. Every move is journalled and reversible; empty folders are only removed when you say so. |
| 📦 **Moves you to another provider** | Read at A, create at B, **verify**, and only then — as a separate step with its own release — empty A. Mail without a Message-Id is never deleted, because it could not be found again. |
| 🔔 **Tells you what matters** | Deadlines, authorities and banks, security warnings, real people — immediately, by Telegram. |
| 📎 **Finds documents** | Every attachment is indexed **without downloading it** (IMAP `BODYSTRUCTURE`). Search years back: *"every mail with a PDF from my insurer"*. |
| 🗂 **Hands papers on** | PDFs can go straight to [DocuSort](https://github.com/robeertm/DocuSort) — see below. |
| 🧠 **Optional judgment helper** | For what imitation cannot know, a language model can classify a sender or **propose a rule**. Local (Ollama), a service, or an asynchronous task queue. Off by default — everything above works without it. |
| 🧾 **You can see what it handed off** | Every task it created, with state and age. Nothing started after two hours? It says so. Three untouched? It stops creating new ones. |
| 🌍 **Five languages** | German, English, Spanish, French, Italian — one click, and that includes the messages it sends when nobody is looking. |
| ↩️ **Everything is reversible** | Every move is journalled. One click puts a mail back; one click puts them all back. |
| 🛑 **Two emergency stops** | A file on disk, and a switch in Home Assistant. Either one alone stops it. |
| ✉️ **A full mail client** | Folder tree, list, preview pane, search, reply, forward, attachments, drafts, sending. Some thirty settings, from where the preview sits to when a mail counts as read. Every row shows the first words of the letter — fetched in pieces, one request per shape, and still `BODY.PEEK`. |
| ✨ **Everything new, in one view** | Above the inbox: every new mail out of every folder at once, each row saying where it is now. The view the watchman makes necessary — it is the one that moved the mail away. |
| 📬 **One click from the watchman to the mailbox** | The tab row carries a door to `/post` — a real link that works before any script has run. |
| 📱 **A version of its own for phones** | Not the wide page made narrow: one sheet at a time, a bar at the bottom, finger targets from 44 px, swipe to archive or delete, long-press to choose several and act on them at once. |
| 🔒 **A lock in front of the mail** | The client asks for an access word before it shows a single line of a letter. Optionally the watchman page too. |
| 👂 **New mail arrives by itself** | The page listens (IMAP `IDLE`) instead of asking every minute: measured **0.13 s** from arrival to the top of the list, with nobody pressing anything. One waiting request, one connection at the provider, and it lets go when nobody is looking. |
| ☀️ **Day and night** | One button, two positions — and night is the default, full stop: the page does not ask your device. The same colours either way — the brown-black night becomes warm paper, and the gold stays gold. |
| 🗂 **Folders you can actually handle** | Fold a branch or the whole tree with one button, on both surfaces. Drag a folder somewhere else (the column scrolls along while you do), or delete one — and the question carries the number of mails it would take with it. |
| 🧠 **And the watchman learns every folder** | Created or deleted — in the Postwache, in another mail program, on the provider's page — the learned map is reconciled against what really exists. A rule pointing at a folder nobody has any more would make the filing fail every five minutes, silently. |
| 🖐 **Whole folders travel** | Drag a folder onto another and it moves with its mail and its subfolders — one `RENAME`, not thousands of copies. The watchman's learned filing, its document index and its journal are carried over in the same breath. |

## What the watchman will not do

- **It never deletes.** The watchman has no call that deletes a mail. It moves,
  and it sets `\Deleted` only *after* a copy the server has confirmed.
- **It never marks mail as read.** Every fetch uses `BODY.PEEK`. In learning mode
  the mailbox is opened read-only, so even a programming mistake cannot change
  anything.
- **It never arms itself.** Until you say so, it only writes down what it
  *would* do.

**And the client?** The client is your hand, so it can do what you tell it to:
mark as read, flag, move, delete, send. The line between the two is drawn on
purpose and it is measurable — the test bench reads every IMAP command the client
sends:

- browsing opens a folder **read-only** (`EXAMINE`); only an action opens it for
  writing;
- **not one fetch without `BODY.PEEK`** — displaying a mail does not make it read.
  *When* it counts as read is a setting, and one of its positions is „by hand
  only";
- deleting means the bin. Only inside the bin, or when you say so explicitly, is
  anything removed for good;
- if a copy fails while moving, nothing is marked deleted and nothing is expunged.

![Documents in the post](docs/screenshots/03-documents.png)

## The mail client

![Everything new](docs/screenshots/32-client-new.png)

<p align="center"><i>Everything new, out of every folder at once — each row says
where the mail is now. The view the watchman makes necessary: it is the one that
moved the mail out of the inbox. All data invented.</i></p>

![The phone version](docs/screenshots/27-phone-list.png)

![Choosing several on the phone](docs/screenshots/34-phone-select.png)

<p align="center"><i>Long-press a row and the avatars become ticks: mark several
read, flag them, move them, delete them. A selection that spans folders goes out
as one request per folder. All data invented.</i></p>

Open `http://<host>:8110/post`. A phone gets the phone version, everything else
the wide one, and either can be switched by hand — the choice is remembered.

**New mail shows up by itself.** The page does not ask every minute — it listens.
IMAP has `IDLE` for exactly this: the server speaks up when something happens, so
the page leaves one request waiting and that request comes back the moment there
is something to say. Measured on a running page, with nobody touching anything:
**0.13 s** on the wide version, **0.22 s** on the phone. The listening post keeps
its own connection (an `IDLE` sits on one for minutes, and the warm connection is
the one every click goes through), it cannot write — the folder is opened
`EXAMINE`, read-only, by construction — and it lets go after 90 seconds without a
listener, so a tab left open at night holds nothing at the provider until
morning. A server without `IDLE` is asked politely every 20 seconds instead.
Nothing is ever redrawn under your hands: a selection, an open window, a focused
field or a search all hold the list still, while the folder counts keep moving.

![The day mode](docs/screenshots/36-client-day.png)

<p align="center"><i>The same house by daylight. Not another colour family — the
brown-black night becomes warm paper, and the gold stays gold. All data
invented.</i></p>

**Day and night, one button, two positions** — and **night is the default**,
whatever the device says. A dark house should not hand a light page to somebody
who never chose one, so only an explicit *day* turns the light on. The choice
lives on the device, because a phone in a dark room and a desk in the sun are two
different answers to the same question. The letter itself follows too — it is a
document of its own inside the frame, so the page tells the engine which time of
day is in force.

**Whole folders travel.** Drag a folder onto another one and it moves with its
mail *and* its subfolders — one IMAP `RENAME`, not thousands of copies. Drag a
mail row onto a folder and it goes there; drag a row that is part of your
selection and the whole selection goes. On a phone a long press picks a folder up
and the same sheet you move mail with puts it down. Special folders stay where
they are: Sent, Drafts, Trash and Junk are positions, not drawers.

And the watchman is told, in the same breath — that is the larger half of the
job. Its learned filing map decides where post goes, so an old folder name left
in it would make the next run file into a folder that is not there: the copy
fails, the mail stays in the inbox, and it fails again five minutes later with
nobody but the log to see it. The document index, the journal (the way back for
every mail it ever moved) and the folders you pinned by hand in the settings all
move with it. If that half fails, the page says so: the folder moved, the
watchman did not understand it.

**The same goes for folders you create or delete** — and „delete" includes the
one you deleted in a completely different mail program. That is why the map is
not kept up by remembering what the Postwache itself did, but by asking the
server what exists: once on every run (a single `LIST`), and at once after every
folder action in the client. A new folder enters the map with zero mails and is a
target from its first second — the name bridge hangs only on the name, so an
empty folder „Steuer" can take post from `steuer@…` right away.

**Folders fold** — one branch, or the whole tree with one button, on both
surfaces. While you drag a folder the column scrolls along with you, so the top
of a long list is reachable from the bottom. The icons answer to being touched,
and a folder with unread mail breathes slowly; a system set to less motion gets
neither. Deleting a folder is the one thing here that cannot be undone, so the
engine counts before it asks: *„Delete “Garden” with 77 mails for good?"*

**The lock is part of it, not an accessory.** The overview page has always shown
subject and sender only, and the reason was written down: not enough to spread an
inbox across a page *without a login*. A client shows the body, so it brings the
login. The word is hashed with PBKDF2 (240,000 rounds) in a `0600` file; the
session is an `HttpOnly` cookie; wrong guesses are slowed down from the fifth
attempt. Attachment links and inline images sit behind the same door — links get
forwarded, and a link that works without the word is the hole the lock was built
to close.

**Foreign HTML has three walls around it**, each independent of the others: an
allow list that rebuilds the mail out of what is permitted, a content-security
policy inside the frame (`script-src 'none'`), and the browser's own sandbox
without `allow-scripts`. Remote images are off — an image fetched from outside is
a receipt that you opened the mail, at the second you opened it. The count stands
above the letter, and one click loads them for this mail.

**And a link that lies is named.** If the visible text says `www.your-bank.example`
while the target is somewhere else, that is the oldest trick there is; the same
goes for punycode hosts and bare IP addresses.

![Composing](docs/screenshots/24-client-compose.png)

Writing works as you would expect — reply, reply to all, forward with the original
attachments, drafts in the drafts folder, a copy in the sent folder. The address
book is the one the watchman has been keeping for weeks: everybody who has written
to you, most frequent first. Sending needs an outgoing server; it is guessed from
your IMAP host and can be checked with a button that **logs in and hangs up
again** — a test that sends a real mail to prove it works is not a test.

## Install

### One command

```bash
curl -fsSL https://raw.githubusercontent.com/robeertm/Postwache/main/deploy/install.sh | sh
```

Writes a `docker-compose.yml`, pulls the image, starts it on port 8110.

### Docker Compose by hand

```yaml
services:
  postwache:
    image: ghcr.io/robeertm/postwache:latest
    container_name: postwache
    restart: unless-stopped
    ports:
      - "8110:8110"
    environment:
      - TZ=Europe/Berlin
      - POSTWACHE_TAKT=60     # seconds between runs; a container has no cron
    volumes:
      - ./data:/data          # credentials, what it learned, the log
```

```bash
mkdir -p data && docker compose up -d
```

### Over Tailscale — one command

A private network beats a forwarded port. Paste a Tailscale auth key and you
are done:

```bash
./deploy/tailscale.sh tskey-auth-xxxxxxxxxxxx
```

That is the whole setup. The Postwache is then at
`https://postwache.<your-tailnet>.ts.net` — HTTPS with a certificate Tailscale
fetches and renews by itself, no port open anywhere, no reverse proxy, and
nobody outside your tailnet can even knock. Which matters more here than for
most things: this page can read your mail.

Get the key from the Tailscale admin console → *Settings → Keys →
Generate auth key* (switch on **Reusable**). And once, in *Settings → DNS*,
turn on **MagicDNS** and **HTTPS Certificates** — the only thing the script
cannot do for you, and it will tell you if it is still missing.

<details><summary>What the script does, if you would rather do it by hand</summary>

It writes two lines into `.env` and starts the stack:

```
TS_AUTHKEY=tskey-auth-xxxxxxxxxxxx
COMPOSE_FILE=docker-compose.yml:docker-compose.tailscale.yml
```

`COMPOSE_FILE` is the part worth knowing: with it in `.env`, a plain
`docker compose up -d` uses the Tailscale overlay from then on — you never have
to remember `-f docker-compose.yml -f docker-compose.tailscale.yml` again. The
overlay changes only what must change: the Postwache gives up its published
port and runs inside the `tailscale` container's network, which is what lets
`tailscale serve` reach it on `127.0.0.1` without opening anything.

The auth key is needed once, to join. Afterwards this machine's identity sits
in `./tailscale/state` (git-ignored), so the key can be revoked and the
container keeps running.

</details>

### From source

No third-party packages — the standard library is enough.

```bash
git clone https://github.com/robeertm/Postwache.git && cd Postwache
POSTWACHE_HOME=~/.postwache python3 post_web.py     # the page on :8110
```

Run the watchman from cron, once a minute:

```
* * * * * POSTWACHE_HOME=$HOME/.postwache /usr/bin/python3 /path/to/postwache.py >/dev/null 2>&1
```

### Try it without a mailbox

```bash
python3 demo/demo_daten.py /tmp/postwache-demo
POSTWACHE_HOME=/tmp/postwache-demo python3 post_web.py
```

Invented data, a filled page, nothing connected. The screenshots on this page
come from exactly that.

## First run

1. Open `http://<host>:8110` → **Einstellungen** → add a mailbox (IMAP address,
   password, server). The connection is tested immediately.
2. Leave it in **learning mode**. It reads the last 300 mails to learn where
   things belong, moves nothing, and tells nobody.
3. Look at what it *would* do. When that looks right, arm it.

![The watchman by daylight](docs/screenshots/38-watch-day.png)

<p align="center"><i>The watchman page in day mode. All data invented.</i></p>

![Settings](docs/screenshots/02-settings.png)

![Tasks in the workshop](docs/screenshots/11-tasks.png)

## Reorganising, and moving house

![Reorganising the mailbox](docs/screenshots/09-restructure.png)

Three stages, separate and in this order: **read** (read-only), **plan** (does not
touch the mailbox), **run**. You see the planned folder tree with a measured count
per folder before anything moves, and the plan carries a fingerprint — running it
requires the fingerprint of the plan you actually looked at, so a plan nobody read
cannot be executed.

![Moving to another provider](docs/screenshots/10-move.png)

Between two providers there is no `COPY`: every mail is read at A and **created
anew** at B, which means it then exists twice. That is why emptying A is a separate
stage with its own release token, and why it only deletes what it has just verified
is present at B. Special folders are matched by IMAP **flag**, not by name, and the
personal namespace is **asked for**, never guessed.

## The judgment helper (optional)

When mail fits into no drawer, the watchman can ask a language model for a
**rule** — never for a decision about a single mail, and it never applies the
rule itself. You see the proposal and take it or leave it.

| Provider | What leaves your machine |
|---|---|
| **off** (default) | nothing — unclear mail simply waits on the page |
| **Ollama** | nothing — the model runs on your own hardware |
| OpenAI / Anthropic | sender, name, subject, and why it was unclear |
| your own agent | the same, written into a folder you name |

🔴 **Never the body of a mail, never an attachment.** Whatever you pick.

![The judgment helper](docs/screenshots/04-ai.png)

### A local model, in one click

A local model is the only setting where *nothing at all* leaves your house, so
the Postwache makes it the easy one.

**If you already run Ollama**, press **Find a model**. The Postwache looks on
its own machine, on the host of its container, and on the computer you have the
page open on — then offers what it found, with a model already picked.

🔴 **It looks from the watchman's side, not from your browser's.** Your browser
sits on the machine where Ollama runs; it would happily report "reachable"
while the Postwache — on a Raspberry Pi in the cupboard — cannot get there at
all. The question is never whether *you* can reach it.

**If you don't have Ollama yet**, download the setup for your system, double-
click it, and it does the rest: install Ollama, make it listen where the
Postwache can reach it, pull a model, write the address into your settings —
and then ask *the Postwache* whether it works. Not the machine it runs on. The
Postwache.

![One click to a local model](docs/screenshots/05-ollama.png)

⚠️ If the model ends up on a different machine than the Postwache, Ollama has
to listen on the network, and Ollama has no password — anyone on that network
can then use it. The setup says so and asks before it does it. On one machine,
none of this applies.

The setup itself speaks English, whatever language the page is set to.

## Languages

**German, English, Spanish, French, Italian.** One click in the settings, and
the whole thing changes — the page, the drawer names, the history, the daily
summary and every Telegram message.

![Switching the language](docs/screenshots/08-languages.png)

🔴 **The language is a setting of the installation, not a cookie.** The
watchman writes its history and sends its messages when nobody is looking; a
cookie in somebody's browser cannot tell it which language to use. If you need
two languages in one house, you need two Postwachen.

Strings live in `locales/<code>.json` as flat key/value files — 396 keys, and
the probe refuses to pass if one of them is missing, orphaned, or has lost a
`{placeholder}` in translation. Adding a language is one file and a line in
`SPRACHEN`; anything missing falls back to German rather than showing a blank.

That count covers more than the buttons. Every answer the server gives you —
"Saved.", "The mailbox refuses: …" — and every line the watchman writes about a
mail — *why* it was filed there, *why* it was unclear — is translated too. A
page in English that answers in German is a page that was only half done.

## Works with DocuSort

<table>
<tr><td width="62%">

[**DocuSort**](https://github.com/robeertm/DocuSort) is the other half of the
same idea: it files the *documents* — invoices, statements, contracts — reads
the amounts, and matches them against your bank bookings.

The Postwache hands it the paper. When mail arrives with a PDF, the attachment
goes straight into DocuSort through the same front door a browser uses
(`POST /upload`, with its own service account you can switch off at any time).
Photos, calendar invitations and signature images stay out. Anything that looks
like phishing is never handed on.

You can also search **backwards**: *"every mail with a PDF from the tax office"*,
select them, and send the lot over in one go. Already-handed-over documents are
marked, so you never send the same paper twice.

**There is nothing to connect.** Install the two together and the link is
already made:

```bash
curl -fsSL https://raw.githubusercontent.com/robeertm/Postwache/main/deploy/install-both.sh | bash
```

One secret in one `.env` is read by both sides — DocuSort creates the service
account, the Postwache writes it down. Nobody types anything.

Running both already, separately? Then it is one click each side: DocuSort
shows a **pairing line** under *Settings → Postwache*; paste it here under
*Pairing line from DocuSort*. Or press *Find DocuSort* and the Postwache looks
for it where it can actually be reached — same Docker network, same machine,
or your tailnet. No network is scanned; only places that follow from how the
two are installed get asked.

</td><td>

**Together**

```
  mailbox
     │
     ▼
 Postwache   ← noise out,
     │         documents found
     ▼
 DocuSort    ← filed, amount read,
     │         booking matched
     ▼
  answered
```

</td></tr>
</table>

## Privacy

- Credentials live in `0600` files inside your data directory. They are never
  printed, never logged, never sent to the browser — the page only ever learns
  *whether* a password is set.
- **The client has a lock**, and it is not optional: without an access word it
  hands out no mail at all. The same word can be required for the watchman page
  as well (a setting, off by default) — and then for its data, not merely its
  view.
- The watchman page itself has no account. Put the whole thing behind your own
  reverse proxy or a private network (Tailscale, WireGuard) regardless; it is
  built to be reachable only by you.
- **The message body is stored nowhere.** It is read to classify a mail and to
  show it to you, and then it is gone. The history records who was written to,
  never what was written.
- Nothing is sent anywhere unless you configure it: Telegram, DocuSort and the
  judgment helper are each off until you turn them on.

## Updating

**It happens by itself.** Both the `docker-compose.yml` in this repository and
the one-command installer ship **Watchtower switched on**: a new image is
pulled nightly at 04:00 and the container recreated. You do not have to do
anything.

```
WATCHTOWER_SCHEDULE=0 30 3 * * *     # in .env, if you want a different time
```

What it costs, stated plainly: Watchtower needs the **Docker socket**, which is
effectively root on the host. It is mounted read-only, but it is still a
privilege you are handing to a container. Not willing? Delete the `watchtower`
service and do it by hand — because **Docker never re-pulls a running
container**, `:latest` being a label and not a subscription:

```bash
docker compose pull && docker compose up -d
```

**Already running a Watchtower of your own?** Check whether it names the
containers it watches. One started with a list of names — `watchtower app1
app2` — updates only those and will *not* pick the Postwache up; one started
with no names watches everything and will. Ours names only `postwache`, so the
two never fight over the same container. If yours already covers everything,
delete our `watchtower` service and let yours do the work.

## Honest limitations

- **The code and its comments are German.** The interface is not — see
  *Languages* above — but anyone reading the source will find German in it.
  That is where this program grew up. The one-click Ollama setup prints in
  English regardless of the page language.
- **No user accounts.** One page, one household. See *Privacy* above.
- **IMAP only.** No Exchange, no Gmail API — an IMAP account of any provider.
- It was built for one household and now runs in more than one. Bugs you find
  are bugs nobody has seen yet.

## Licence

Source-available, not open source. You may read it, run it privately, and study
it; you may not sell it or ship it as your own. See [LICENSE](LICENSE).
