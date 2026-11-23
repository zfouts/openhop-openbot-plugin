# Plugin contract: config, mailbox, runtime

openHop's plugin manager gives a plugin two files in its data directory
(`$OPENHOP_PLUGIN_DATA`, normally
`/var/lib/openhop_repeater/plugins/openhop.openbot/data/`):

| File | Written by | Read by | API |
|---|---|---|---|
| `config.json` | the web UI (`POST /api/plugins/settings`) or the operator | the bot, re-read within 2 s of any change | `GET/POST /api/plugins/settings?id=openhop.openbot` |
| `runtime.json` | the bot only, every few seconds and on change | the web UI | `GET /api/plugins/runtime?id=openhop.openbot` |

The bot never writes `config.json`, so a save from the UI can never race the
bot. Everything the bot wants to tell the UI goes in `runtime.json`.

## config.json

The shipped `config.default.json` is the complete schema. A saved config is
deep-merged over it, so a partial config is valid. Lists replace, they do
not merge.

### Radio

The companion reports the radio it transmits on; the bot shows it as
`runtime.status.radio` (`null` until connected) and uses it to estimate
airtime. The preamble length is not reported: `node.preamble_symbols`
(4-64, default 16, as most MeshCore radios use) sets it for the estimate.
It changes nothing on air.

### Node name

`node.name` is not just a label. If it is set and differs from the
companion's current name, the bot renames the companion (MeshCore
`CMD_SET_ADVERT_NAME`) when it connects and whenever `node.name` changes.
openHop saves the new name into the companion's `node_name` in the
repeater's `config.yaml`, so it survives restarts. Empty leaves the radio's
name alone. The limit is 31 bytes (UTF-8); a longer name is reported in
`config_problems` and not applied. Other nodes see the new name after the
next advert (the `advert` action sends one).

### Scoping

A command fires only when **every** scope check passes. Each command's own
`channels`, `regions` and `dm` override the global `scope` block; an empty
list (or `null` for `dm`) means "inherit the global value".

| Check | Global key | Per-command key | Empty means |
|---|---|---|---|
| Channel the request arrived on | `scope.channels` | `commands[].channels` | every channel slot except `scope.protected_channels` |
| Region the request arrived under | `scope.regions` | `commands[].regions` | any region, including unscoped |
| Direct messages | `scope.allow_dm` | `commands[].dm` | inherit (`null`) |
| Hop count | `scope.max_hops` | | no limit (`null`) |

Channel names compare case-insensitively with or without the leading `#`.
`scope.protected_channels` always wins: a command never answers on Public
even if it is listed.

### Regions

MeshCore regions are transport-code scopes. A region-scoped flood packet
carries a 16-bit code computed from the region's key
(`sha256("#name")[:16]`) and the packet. The bot learns which region a
request arrived under by checking the code against every name in the
top-level `regions` list. A request's region is one of:

| Value | Meaning |
|---|---|
| `#name` | Arrived scoped to that configured region |
| `*` | Arrived as a plain, unscoped flood |
| `?` | Scoped to a region not in `regions`, or the raw frame was not matched |
| `direct` | Arrived on a direct route (no region applies) |

`scope.regions` / `commands[].regions` take region names and may also list
`*` to allow unscoped floods. When a region list is non-empty, `?` never
matches (the bot fails closed), and `direct` always matches.

Every scoped packet the radio hears (any payload type) is also tested against
`regions` and, with CoreScope on, up to 64 regions from CoreScope's list, and
counted per region and UTC day in `openbot.db` (`region_heard`, 30 days). See
`runtime.regions` below.

With `limits.reply_in_request_region` on, a reply is sent scoped to the
region its request arrived under (unscoped if it arrived unscoped), so a
`#tx-dfw` question gets a `#tx-dfw` answer. Announcements and manual sends take an
explicit `region`: `""` uses the companion's default scope, `*` forces
unscoped, `#name` scopes to that region.

### Commands

```json
{"id": "path", "enabled": true, "trigger": "!path", "aliases": ["!p"],
 "description": "...", "reply": "{path}", "channels": [], "regions": [],
 "dm": null, "cooldown_s": 10}
```

`trigger` is matched case-insensitively against the start of the message
text (after the sender's `name: ` prefix), on a word boundary; the longest
match wins. `aliases` are extra triggers for the same command: they share
its id, cooldown and usage, and `{help}` lists only `trigger`. An alias that
repeats any trigger or earlier alias is dropped and reported in
`config_problems`; triggers always win over aliases. Everything after the
matched trigger or alias is `{args}`.

### Per-sender limits

A request that passes scope must also pass, in order:

| Limit | Key | Default | Refused as |
|---|---|---|---|
| Gap between one sender's commands | `limits.user_cooldown_s` | `5` | `requester cooldown` |
| The command's own cooldown | `commands[].cooldown_s` | per command | `command cooldown` |
| Answered commands per sender per window | `limits.user_max_requests` per `limits.user_window_s` | `8` per `600` | `requester rate limit (N per Ws)` |

Limits are kept per sender identity: on a channel the sender's name,
case-insensitively (a channel message carries nothing else); on a DM
`dm:` and the sender's 6-byte public key prefix in hex. All three follow a
channel sender across channels and regions. `user_max_requests` `0` turns
the cap off; `user_window_s` is 10-86400. A reply that renders empty gives
back what it took. Dry runs (`test`) skip these limits.

### Bans

```json
"bans": [{"pattern": "^spam", "ignore_case": true, "enabled": true, "note": ""}],
"auto_ban": {"enabled": false, "strikes": 5, "window_s": 600, "ban_h": 24}
```

A banned sender gets no command replies and no unscoped replies; the request
is recorded as skipped (`banned (...)`) and holds no cooldown. Their messages
still count in the roster and traffic.

`bans[].pattern` is a Python regular expression searched anywhere in the
sender's name (cut to 64 characters); anchor it (`^bob$`) to match a whole
name. `ignore_case` and `enabled` default to `true`. A pattern that is empty,
does not compile, or matches an empty name (so would match everyone, such as
`spammer|`) is skipped and reported in `config_problems`, as is a `bans`
that is not a list. Rules are numbered by their position as written
(`ban #3`). On a DM the name is the contact's name, or the first 8 hex digits
of its public key when the contact is unknown.

With `auto_ban.enabled`, every request refused by the per-sender cap
(`requester rate limit`) is a strike; cooldown refusals are not, since that
is what a retry over a lossy link looks like. `strikes` (1-1000) within
`window_s` (10-86400) bans that identity for `ban_h` hours (0.1-8760). With
`limits.user_max_requests` `0` nothing is counted, which is reported.
Automatic bans live in `openbot.db`, not in config; they show in
`runtime.auto_bans` (by identity, so a DM sender is `dm:<hex>`) and end on
their own, or with the `unban` action. The bot transmits nothing when it bans
someone. Anyone can type any name on a channel, so a name can be banned by
someone flooding as it; a DM identity can not.

### Unscoped traffic and replies

Every channel message the radio hears (the bot's own excepted) is counted
per hour, channel and scope: a configured `#region`, `other` (scoped to a
region not in `regions`), `*` (a plain flood), `?` (the raw frame could not
be matched) or `direct`. Counts are kept 366 days and reported by `usage`.

```json
"nudges": [{"channel": "#chat", "enabled": true, "region": "",
            "per_sender": 1, "per_channel": 6, "window_h": 24,
            "text": "@[{sender}] your message had no region, ..."}]
```

A `nudges` entry turns on, for one channel, a reply to anyone heard sending
a plain, unscoped flood there. The bot considers a reply only when the frame
it heard was a plain flood **and** decrypts, with that channel's key, to
exactly the message (same text and timestamp); a message it cannot verify is
never answered. It then waits 8 seconds and sends nothing if any copy of the
same packet carrying transport codes was heard meanwhile (or in the 2 minutes
before), since the plain copy may be a replay or a bridge's. The key is the
slot's `secret` or hashtag key from `channels`, or Public's well-known key;
a channel whose key the bot does not have never gets a reply, which is
logged once. Each sender gets at most `per_sender` (1-100) on that channel,
and the channel at most `per_channel` (1-1000), in any `window_h` hours
(0.1-720); both count replies queued, and one the send queue refuses is given
back. `text` is a template, rendered with the message's context.
`region` is where the reply is sent: `""` the companion default, `*`
unscoped, `#name` that region. No reply goes to a banned sender, the bot
itself, a request over `scope.max_hops`, or a protected channel: an entry for
a protected channel is skipped and reported. One entry per channel.

`reply` is a template; see [TEMPLATES.md](TEMPLATES.md). A reply that renders
empty is not sent and does not use the cooldown.

### Announcements

```json
{"id": "morning", "enabled": true, "channel": "#bot", "region": "",
 "schedule": "0 8 * * *", "text": "Good morning, {date}. {forecast 76131}"}
```

`schedule` is a 5-field cron expression in `node.timezone`, or
`every <seconds>`.

### Operator notifications

```json
"notify": {
  "targets": [{"kind": "channel", "channel": "#ops", "region": ""},
              {"kind": "dm", "pubkey": "<64 hex>", "name": "zach"}],
  "events": {"update": false, "ignored": false, "started": false, "problems": false},
  "max_per_hour": 6
}
```

Off until there is a target and an event is on. A `channel` target is a
configured slot (hashtag or private), never a protected channel; `region` as
for announcements. A `dm` target is a companion contact by full public key;
the bot can only DM contacts the companion has, and reports the ones it
can't. Each event goes to every target as one message clamped to 160 bytes,
through the TX queue (kind `notify`). `max_per_hour` (1-60, at least the
number of targets) caps frames on the air across every event: one event to
three targets counts three, and an event goes to every target or none. Extra
ones are dropped. Events:

| Event | When | Repeats |
|---|---|---|
| `update` | GitHub's newest stable release with a wheel (the repository in openHop's `state.json`, else `zfouts/openhop-openbot-plugin`) is newer than the running version. Checked only while `update` is on: from 1 minute after start, every 6 hours (1 hour after a failure), with a plain `openhop-openbot/<version>` User-Agent. Found while the companion is down, it is sent once it reconnects. | Once per version; remembered in `data/notify_state.json` once queued |
| `ignored` | A banned sender's command was ignored, or an automatic ban began | Once per sender (DM senders by key) per 24 hours, and at most 3 events an hour |
| `started` | The first companion connection after the bot starts | Once per run |
| `problems` | New config problems after a save (the count only: problem text can quote config); CoreScope's region list failing 3 times in a row; 3 transmissions failing within 10 minutes (notifications themselves excluded) | Once per kind per 6 hours |

### CoreScope

```json
"corescope": {"enabled": false, "base_url": "", "regions": true, "shortener": "v.gd",
              "shortener_url": "", "shortener_key": "", "mpath_wait_s": 30}
```

With `corescope.enabled`, `{path}` appends
`<base_url>/#/map?packet=<PACKET HASH>&hops=<h1>,<h2>,...`, shortened by
`corescope.shortener` (`v.gd`, `is.gd`, `tinyurl.com`, `da.gd`, `custom`, or
`none` for the full link). `custom` calls
`<shortener_url>/create.php?format=simple[&key=<shortener_key>]&url=<link>`
(v.gd's API) and accepts only https links on that host; `shortener_url` must
be https, and a missing or invalid one is reported in `config_problems`. The link needs the request frame's packet hash; without one,
or without CoreScope, `{path}` is the hop count and hashes only. A shortener
failure falls back to the full link if it fits beside every hash, else no
link. An unknown `shortener` is reported in `config_problems` and treated as
`none`.

`{pathnames}` fetches CoreScope's candidates for the chain
(`GET <base_url>/api/resolve-hops?hops=<h1>,<h2>,...`) and adds them to the
roster for that lookup. `corescope.base_url` must be `http://` or `https://`;
an invalid one logs `corescope disabled: ...` (no links, local roster only).
Any CoreScope failure (5 s timeout, non-200, malformed body, over 1 MB)
falls back to the local roster alone.

`corescope.regions` (default `true`; a non-boolean is reported in
`config_problems` and `true` is used) controls only the region list for the
Scope & Regions tab: the 6-hourly fetch, the `corescope_regions` action and
listening for CoreScope's regions on air. Map links, `{pathnames}` and
`{mpath}` follow `corescope.enabled` alone.

`{mpath}` renders empty without CoreScope (and `{help}` then leaves out any
command whose reply uses it). With CoreScope it renders an acknowledgement
and, `mpath_wait_s` seconds later (5-300, default 30; anything else is
reported in `config_problems` and 30 is used), the bot fetches
`GET <base_url>/api/packets/<packet hash>` and sends a second message to the
same channel or DM, in the same region:
`@[<sender>] <N> routes to <M> observers, <lo>-<hi> hops[, <D> direct] <link>`.
A route is one observer and one distinct hop chain; observations with
`direction: "tx"` or an unreadable `path_json` are skipped. The link is
`<base_url>/#/packet/<PACKET HASH>`, shortened like `{path}`'s, dropped
whole if it does not fit. A 404 answers `no CoreScope observer reported
hearing that`; any other failure answers `CoreScope lookup failed, try again
later`. No packet hash (unmatched frame, or a dry run) answers `can't trace
that copy: no packet hash` with no follow-up. At most 16 follow-ups wait at
once; more are dropped with an `error` activity entry.

## The `_cmd` mailbox

The UI triggers one-off actions by saving config with a `_cmd` object:

```json
"_cmd": {"id": "c-1731170000-ab12", "action": "test", "args": {...}}
```

Each `id` runs once. The bot records the result under
`runtime.mailbox.results[id]` and adds the id to `runtime.mailbox.processed`.
The UI should create a fresh id per click and poll the runtime for its
result.

| action | args | result |
|---|---|---|
| `test` | `text`, `channel` (or `"dm"`), `sender`, `region`, `hops` (optional hex chain) | `{"matched": "id" or null, "reply": "...", "reason": "..."}`, plus `"nudge": "..."` when `region` is `*` and the channel has an unscoped reply. Never transmits. |
| `send` | `channel` (name or idx), `text`, `region` | `{"queued": true}` |
| `announce_now` | `id` | `{"queued": true}` |
| `clear_cooldowns` | | `{"cleared": n}` |
| `ban_test` | `name` | `{"name": "...", "banned": "ban #1 ^spam"}`: the rule or automatic ban that applies, `""` when none. Uses the saved config. |
| `unban` | `sender` | `{"sender": "bob", "lifted": bool}`: lifts an automatic ban and clears that sender's strikes. |
| `provision_channels` | | `{"channels": [...]}` |
| `notify_test` | | `{"sent": n, "failed": ["zach: not a contact ..."]}`. A test message to every target, whatever the event switches; error when nothing could be sent. |
| `corescope_regions` | | `{"regions": n, "partial": null \| "why"}`. Fetches CoreScope's region list now (`/api/scope-stats?window=7d` and `/api/scope-audit?window=1h`) and caches it in `data/corescope_regions.json`. Error when CoreScope or its region list (`corescope.regions`) is off, or the stats fetch fails; `partial` is set when only the repeater counts failed. The bot also fetches on start and every 6 hours. |
| `usage` | `{"range": "24h"\|"7d"\|"30d"\|"90d"\|"1y"}` or `{"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}` | The usage report below. Dates are in `node.timezone`. |
| `advert` | `{"flood": true\|false}` (default `true`) | `{"sent": bool, "flood": bool}`. Error if the companion is not connected. |

`usage` reads the `usage` table in `openbot.db`: one row per request that
matched a command trigger (not messages that matched none, and not `test`
dry runs), kept 366 days. Result:

```json
{"tz": "America/Chicago", "from": 1731000000.0, "to": 1731086400.0,
 "label": "Last 24 hours", "bucket": "hour",
 "totals": {"requests": 12, "answered": 10, "skipped": 2},
 "commands": [{"id": "path", "trigger": "!path", "answered": 6, "skipped": 1}],
 "series": [{"t": 1731000000.0, "label": "14:00", "counts": {"path": 2, "ping": 1}}],
 "requesters": [{"name": "bob", "requests": 5}],
 "channels": [{"name": "#bot", "requests": 9}],
 "regions": [{"name": "*", "requests": 9}],
 "skipped": [{"reason": "cooldown", "count": 2}],
 "traffic": {"bucket": "hour",
             "totals": {"scoped": 30, "unscoped": 60, "unknown": 5, "direct": 0,
                        "messages": 95, "nudges": 2, "unscoped_pct": 66.7},
             "series": [{"t": 1731000000.0, "label": "14:00",
                         "counts": {"scoped": 3, "unscoped": 5, "unknown": 0, "direct": 0}}],
             "channels": [{"name": "#chat", "scoped": 20, "unscoped": 40, "unknown": 3,
                           "direct": 0, "nudges": 2, "messages": 63, "unscoped_pct": 66.7}],
             "regions": [{"name": "#tx-dfw", "requests": 25},
                         {"name": "other regions", "requests": 5}]}}
```

`airtime` covers what the bot transmitted (the `tx_log` table, kept 366
days): every reply, follow-up, unscoped reply, announcement, manual send and
advert, as `sent`, `failed` (the companion refused it) or `dropped` (queue
full, or stale after 60 s):

```json
"airtime": {"bucket": "hour",
            "totals": {"sent": 57, "failed": 0, "dropped": 4, "bytes": 3900,
                       "airtime_ms": 25800.0, "unestimated": 0, "share_pct": 0.03},
            "busiest_hour": {"t": 1731099600.0, "label": "Nov 22 22:00",
                             "airtime_ms": 1800.0, "share_pct": 0.05},
            "kinds": [{"kind": "reply", "sent": 29, "failed": 0, "dropped": 1,
                       "bytes": 2000, "airtime_ms": 13000.0}],
            "series": [{"t": 1731000000.0, "label": "14:00", "counts": {"reply": 2}}]}
```

Sizes and airtime are estimates: the size from the text (the companion builds
the packet: header, region codes when scoped, channel hash, MAC and 16-byte
AES blocks over the timestamp, flags and `name: text`), the airtime from that
size with the radio settings the companion reports (frequency, bandwidth,
SF, CR) and `node.preamble_symbols`, using openhop_core's
`calculate_lora_airtime_ms`. A DM along a known route also carries its hop
hashes, which the bot can not see; they are not counted. `unestimated` counts
messages sent before the radio settings were known. `share_pct` is airtime
over the window; `busiest_hour` is the clock hour with the most. Only the
bot's own transmission is counted, not repeaters forwarding it.

Skip reasons are `banned`, `cooldown`, `rate limit`, `out of scope`,
`empty reply` or `other`. In `traffic`, `scoped` is a configured region or
`other`; `unscoped_pct` is unscoped over scoped plus unscoped, leaving out
`unknown` and `direct` (`null` when both are zero). An hour that straddles
the window's start counts in full.

`bucket` is `hour` for windows up to 48 h, else `day`. An unknown range, bad
dates, `from` after `to`, a window over 366 days, or a `from` in the future
comes back as the action's error.

## runtime.json

```json
{
  "id": "openhop.openbot", "version": "1.0.0",
  "timestamp": 1731170000, "started_at": 1731160000,
  "status": {"companion": "connected", "node_name": "alice-bot",
             "pubkey": "ab12...", "latitude": 32.9, "longitude": -97.3,
             "radio": {"freq_khz": 910525, "bw_hz": 250000, "sf": 10, "cr": 5,
                       "preamble": 16},
             "config_problems": []},
  "counters": {"rx_messages": 0, "rx_frames": 0, "commands": 0,
               "replies": 0, "skipped": 0, "tx_sent": 0, "tx_dropped": 0,
               "tx_queue": 0},
  "channels": [{"idx": 1, "name": "#bot", "ok": true}],
  "regions_seen": {"#tx-dfw": 12, "*": 40, "?": 3, "direct": 1},
  "regions": {"window_days": 7, "scoped_packets": 5210,
              "watching": ["#tx-dfw", "#tx-fw"],
              "heard": [{"name": "#tx-dfw", "packets": 412,
                         "last_heard": 1731170000, "verified": true}],
              "corescope": {"enabled": true, "fetched_at": 1731160000,
                            "error": null, "error_at": null, "partial": null,
                            "regions": [{"name": "#tx-dfw", "packets": 1203,
                                         "repeaters": 9}]}},
  "activity": [{"ts": 1731170000, "kind": "reply", "channel": "#bot",
                "region": "#tx-dfw", "sender": "bob", "text": "...",
                "detail": "path"}],
  "roster": {"nodes": 120, "repeaters": 40,
             "recent": [{"pubkey": "ab12cd34", "name": "RptA",
                         "type": "repeater", "last_heard": 1731170000,
                         "hops": 2, "snr": 5.5, "latitude": 32.9,
                         "longitude": -97.3}]},
  "builtins": [{"name": "path", "help": "..."}],
  "commands": [{"id": "path", "fires": 3, "last_fired": 1731170000}],
  "auto_bans": [{"sender": "bob", "since": 1731160000.0, "until": 1731246400.0,
                 "reason": "5 refusals by the per-sender cap in 600s"}],
  "update": {"running": "1.7.0", "latest": "1.7.0", "url": "https://github.com/...",
             "available": false, "checked_at": 1731170000, "error": null},
  "contacts": [{"name": "zach", "pubkey": "<64 hex>"}],
  "notify": {"sent": 3, "dropped": 0},
  "mailbox": {"processed": ["c-..."], "results": {"c-...": {"ok": true}}}
}
```

`regions.heard` lists every region with a count kept (30 days): `packets` is
the last `window_days` UTC days, `last_heard` the latest packet at all.
`verified` is `packets >= 3` and a Poisson tail below 1e-6, with
`scoped_packets / 65536` expected chance matches. `watching` is every region
the bot tests packets against. `corescope.regions` is empty, and `corescope.enabled`
false, when CoreScope or its region list (config `corescope.regions`) is off;
the list is also empty when the cached one came from another base URL.

`activity.kind` is one of `rx`, `reply`, `skip`, `nudge` (an unscoped
reply), `ban` (an automatic ban began or was lifted), `notify` (an operator
notification was queued), `announce`, `tx`, `error`. `contacts` is the
companion's contact list (up to 300), for picking notification admins.
`update` is the bot's own release check; `notify` counts notifications sent
and dropped by the hourly cap since start. `activity` holds the newest 100 entries, newest last.
`roster.recent` holds up to 50 nodes, newest first.

## Updates (openHop's API, not the bot)

The Status tab's update check and update button call openHop's plugin
updater directly; the bot is not involved and `runtime.json` only matters
for spotting the new version after a restart.

| Call | What the page uses |
|---|---|
| `GET /api/plugins/updates?id=openhop.openbot[&refresh=1]` | `installedVersion`, `latestVersion`, `updateAvailable`, `repository` (null for a wheel install with no recorded repository; the reply then carries a `reason`), `releaseTag`, `releaseUrl`, `releaseNotes` (only when an update is available). openHop caches GitHub's answer for 10 minutes; `refresh=1` skips the cache. |
| `POST /api/plugins/update {"id": "openhop.openbot", "version": "<latestVersion>"}` | Downloads the release's single `.whl`, stops the plugin, installs it, starts it again. Returns `{"plugin": {...status...}}` when done. The version is pinned to the one the user confirmed. |

After the POST, or if it times out (HTTP 502/504, `outcome: "unknown"`, or a
dropped connection), the page polls `runtime.json` until `version` equals the
target and `timestamp` is fresh, then reloads, because the page itself ships
in the new release.
