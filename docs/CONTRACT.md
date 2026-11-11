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

With `limits.reply_in_request_region` on, a reply is sent scoped to the
region its request arrived under (unscoped if it arrived unscoped), so a
`#tx-dfw` question gets a `#tx-dfw` answer. Announcements and manual sends take an
explicit `region`: `""` uses the companion's default scope, `*` forces
unscoped, `#name` scopes to that region.

### Commands

```json
{"id": "path", "enabled": true, "trigger": "!path", "description": "...",
 "reply": "{path}", "channels": [], "regions": [], "dm": null, "cooldown_s": 10}
```

`trigger` is matched case-insensitively against the start of the message
text (after the sender's `name: ` prefix). Everything after the trigger is
`{args}`. `cooldown_s` is per command, per requester, per channel/DM, and
per region. `limits.user_cooldown_s` is a global per-requester floor across all
commands.

`reply` is a template; see [TEMPLATES.md](TEMPLATES.md). A reply that renders
empty is not sent and does not use the cooldown.

### Announcements

```json
{"id": "morning", "enabled": true, "channel": "#bot", "region": "",
 "schedule": "0 8 * * *", "text": "Good morning, {date}. {forecast 76131}"}
```

`schedule` is a 5-field cron expression in `node.timezone`, or
`every <seconds>`.

### CoreScope

```json
"corescope": {"enabled": false, "base_url": "", "shortener": "v.gd",
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
| `test` | `text`, `channel` (or `"dm"`), `sender`, `region`, `hops` (optional hex chain) | `{"matched": "id" or null, "reply": "...", "reason": "..."}`. Never transmits. |
| `send` | `channel` (name or idx), `text`, `region` | `{"queued": true}` |
| `announce_now` | `id` | `{"queued": true}` |
| `clear_cooldowns` | | `{"cleared": n}` |
| `provision_channels` | | `{"channels": [...]}` |
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
 "skipped": [{"reason": "cooldown", "count": 2}]}
```

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
             "config_problems": []},
  "counters": {"rx_messages": 0, "rx_frames": 0, "commands": 0,
               "replies": 0, "skipped": 0, "tx_sent": 0, "tx_dropped": 0,
               "tx_queue": 0},
  "channels": [{"idx": 1, "name": "#bot", "ok": true}],
  "regions_seen": {"#tx-dfw": 12, "*": 40, "?": 3, "direct": 1},
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
  "mailbox": {"processed": ["c-..."], "results": {"c-...": {"ok": true}}}
}
```

`activity.kind` is one of `rx`, `reply`, `skip`, `announce`, `tx`,
`error`. `activity` holds the newest 100 entries, newest last.
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
