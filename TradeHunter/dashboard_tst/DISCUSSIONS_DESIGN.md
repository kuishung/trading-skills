# Discussions — one thread per subject, on Discord and Telegram, read back into TradeHunter

**Status:** STRUCTURE BUILT v4.275 (2026-11-26). The subjects that open a discussion and the rule for when
one is opened are NOT decided yet (the user: *"i want to build the structure first, I have not decided which
topic or studies will be created from tradehunter. It can be a curated setup, it can be a macro study, it can be
a company insight. The manner how the thread is created from tradehunter has not been decided yet"*). This
document is the contract for the structure; §6 lists what is left to decide.

## 1. The purpose (the user, 2026-11-26)

> we moderator put up a topic or study in the chat either discord or telegram, there will be a thread created
> for that topic or studies and user can contribute in the thread which can be read in tradehunter

So: TradeHunter is the **source** of a discussion (a moderator opens it), the chats are where **members
contribute** (where they already are, on the phone), and TradeHunter is where the contributions are **read and
kept**. The subject can be anything the platform knows: a curated setup, a macro study, a company insight, a
study, or a free topic.

## 2. The shape

```
 subject (kind + ref)  ──open──►  Discussion  ──posts the opener──►  Discord thread      Telegram topic
   setup #12                        id, title,                        (in the channel)     (in the group with
   curated #40                      body, status,                                           Topics on)
   macro "inflation"                channel ids                            │                    │
   company "NVDA"                                                          ▼                    ▼
   topic (free)                   Contribution  ◄──the sync job reads──  members write      members write
                                    channel, external id, author,
                                    text, time  (one row per message,
                                    deduplicated per channel)
                                          │
                                          ▼
                                   the Discussions page / the strip on the subject's page
                                   (merged, time-ordered; a reply typed in the app is mirrored to both chats)
```

- **Discussion** (`discussions` table): `kind` (`setup | curated | macro | company | topic`), `ref` (the subject's
  key within its kind - the setup id, the curated call id, the macro topic key, the ticker; empty for a free
  topic), `title`, `body`, `status` (`open | closed`), who opened it and when, the channel ids
  (`discord_channel_id` + `discord_thread_id`, `telegram_chat_id` + `telegram_topic_id`), `last_activity_at`,
  `last_synced_at`, `n_contributions`, `channel_errors` (why a channel could not be opened, in words).
  One subject may have several discussions over time (a study re-opened); `for_subject()` returns the newest open one.
- **Contribution** (`contributions` table): `discussion_id`, `channel` (`app | discord | telegram`), the chat's
  `external_id` (message id - unique per discussion and channel, which makes the sync idempotent),
  `author_external_id` + `author_name` (the chat's name; `user_id` when the author is a known member - the
  Telegram link-up handshake of `services/telegram.py` can fill it later), `text`, `posted_at` (UTC),
  `reply_to_external_id`, `is_opener`, `mirrored` (the ids an app post got in each chat).

Portable ORM only, a migration (`f5a6b7c8d9e0`), JSON columns for the two small maps - the data-handling rule.

## 3. The service - `app/services/discussions.py`

- `open_discussion(db, *, kind, ref, title, body, user, channels=("discord", "telegram"), url=None)`: the row,
  then each channel that is configured and asked for: Discord = a thread in the configured channel with the
  opener as an embed (the bot, as the study threads already did); Telegram = a **forum topic** in the group
  (`telegram_group_id`, Settings → Integration) named after the title, with the opener posted into it. A channel
  that fails is recorded in `channel_errors` in words and never blocks the others. The opener is stored as the
  first Contribution (`channel=app`, `is_opener=True`).
- `post(db, discussion, text, user)`: stores an app Contribution and mirrors "Name: text" into both chats (ids kept
  in `mirrored`). Bot-authored chat messages are skipped by the sync, so a mirrored post is never stored twice.
- `sync_discord(db, discussion)`: reads the thread (the bot's read, with message ids now) and upserts.
- `sync_telegram(db, updates)`: routes each update with a `message_thread_id` to the discussion holding that
  `(telegram_chat_id, telegram_topic_id)`; others are ignored except for **learning the groups the bot is in**
  (any chat seen is listed on the Integration card, so the admin can pick the group id without reading JSON).
- `for_subject(db, kind, ref)`, `messages(db, discussion)` (merged and time-ordered for a page), `close()`.

## 4. The sync job - `app/services/discussion_sync.py`

A daemon thread inside the web app (like the macro refresh and the logo download), started at boot:

- **Telegram**: long-polls `getUpdates` (20 s) whenever a bot token is saved; the Bot API allows ONE poller per
  token, so nothing else may call `getUpdates` (the old `/start` answerer is folded into this poll). Each
  update is routed by `sync_telegram`; the offset is kept in the state file and losing it is harmless (the
  unique index deduplicates).
- **Discord**: every 60 s re-reads the threads of discussions active in the last 30 days (plus any discussion
  opened in the last hour), newest first.
- **State file** `dashboard_tst/state/discussions_sync.json`: running / last poll / last Discord pass / counts /
  the groups seen / the last error. The Data Ingest page's **Discussions** card and the Hermes tray line
  "Discussions: …" read it (the tray-sync rule). `TST_DISCUSSIONS_SYNC=0` switches the thread off.

## 5. What the member sees

- **Nav → Discussions** (`/discussions`, menu key `discussions`): every discussion, filter by kind and status, each
  with its kind badge, subject link, channel links (Open in Discord / Open in Telegram), contribution count and
  last activity. Moderators get **New discussion**: kind, ref, title, body, which chats. The detail view is the
  chat strip: contributions as bubbles with a channel badge (discord / telegram / web), a composer that posts
  through `post()`, and Close for moderators.
- **The strip on a subject's page** (`_discussion_chat.html`): the Studies page's right-hand chatroom now reads
  from this layer - a study's Discord thread becomes a `kind=setup` discussion the first time it is read, its
  legacy platform comments are still merged in, and "Start Discord thread" opens the discussion on every
  configured chat.

## 6. Left to decide (the user)

1. **Which subjects open a discussion, and when** - by hand from the Discussions page (built), automatically
   when a study / curated call / macro reading / company note is published, or by a button on each page.
2. **Which chat(s)** - both by default (built, with ticks per discussion), or one per kind.
3. ~~**Mirroring between the chats**~~ - **BUILT v4.278 as an admin switch, off by default** (the user: *"make it
   an optional setting by admin, if admin turn on the feature, then both discord and telegram message will be
   relayed"*): Settings → Integration → Discussions → "Relay replies between Discord and Telegram"
   (`app_settings` key `discussions_relay`). On: a reply stored from Telegram is posted into the Discord thread
   as "Name (Telegram): text" and a Discord reply into the Telegram topic as "Name (Discord): text"
   (`discussions._relay`, called by `_store`; what was sent is kept on the contribution's `mirrored` map). The
   bot's own copies are skipped by the sync, so nothing loops; replies stored before the switch are not re-sent.
   The Discussions page and the Data Ingest card show "Relay on / off".
4. **Member identity** - chat names today; linking a Telegram user id / Discord user id to a TradeHunter account
   (the handshake exists for Telegram) would let a contribution carry the member's role and avatar.

## 7. Telegram prerequisites (one-time, the admin)

A **group** (not the one-to-one bot chat) with **Topics switched on** (group settings → Topics), the bot added
as an **administrator** with **Manage Topics** (as an admin it also sees every message; a plain member bot sees
only commands). Then Settings → Integration → "Telegram group id": pick it from "groups the bot has seen" (send
any message in the group after adding the bot) or type the `-100…` id. Topic links are `https://t.me/c/<group id
without -100>/<topic id>`, which open for members of the group.
