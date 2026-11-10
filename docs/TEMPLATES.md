# Reply templates

A command's `reply`, and an announcement's `text`, is a template: plain text
with `{placeholders}`.

## Syntax

- `{name}` inserts a context value or runs a built-in with no argument.
- `{name some text}` passes `some text` to the built-in: `{seen bob}`.
- **Nesting.** Two passes run, so one level works: `{roll {args}}`. Deeper
  levels stay literal.
- **Unknown names stay literal.** `{nosuch}` is sent as `{nosuch}`, which makes
  typos easy to spot in a test.
- **Failures render as `∅`.** A built-in that raises produces `∅`; the rest
  of the reply still goes out.
- **Brace neutralising.** Inserted values have `{` and `}` turned into `(` and
  `)`, and newlines into spaces. A node that names itself `{stats}` cannot
  inject a placeholder.
- **Byte cap.** The result is trimmed and clamped to 160 bytes of UTF-8, cut
  on a character boundary. Non-ASCII characters cost 2 to 4 bytes each.
- A reply that renders empty is not sent.

Try a template offline:

```bash
openhop-openbot render "pong {sender} {roll 2d6}" --sender bob
```

It prints the result and its byte count. Roster built-ins see an empty roster.

## Built-ins

| Name | What | Example output |
|---|---|---|
| `{sender}` | Who sent the command | `bob` |
| `{args}` | Text after the trigger | `2d6` |
| `{channel}` | Channel, `dm` for a DM | `#bot` |
| `{region}` | `#name`, `*`, `?` or `direct` | `#tx-dfw` |
| `{region_tag}` | ` [#name]` if region-scoped, else empty | ` [#tx-dfw]` |
| `{gateway}` | This bot's node name | `alice-bot` |
| `{snr}` / `{rssi}` | Signal the request was heard at, `?` if unknown | `5.5` |
| `{took}` | Seconds from the sender's own timestamp on the message to when this node heard it, one decimal. `?` without a timestamp, or when the sender's clock is ahead of ours or over an hour behind | `4.6` |
| `{hops}` | Hop count | `3` |
| `{time}` / `{date}` | Local time / date (node timezone) | `14:05 CST` / `2026-11-09` |
| `{path}` | Hop count and hop hashes of the request; with `corescope.enabled`, plus a short link to the route on CoreScope's map | `@[bob] [3h] 03b9,4216,d924 https://v.gd/AbC123` |
| `{pathbot}` | Alias of `{path}` | same |
| `{pathnames}` | Resolve the request's hops to names and distance. With `corescope.enabled`, CoreScope's nodes are added to the roster for the lookup | `@[bob] [3h] ~14.2mi RptA>RptB>c3d4?` |
| `{route}` | Raw hop hashes | `a1b2>c3d4` |
| `{seen NAME}` | When a node was last heard | `RptA (repeater) 5m ago 2h` |
| `{whois HEX}` | Names for a hop hash prefix (2-8 hex chars, up to 3) | `RptA, RptC` |
| `{stats}` | Roster counts | `120 nodes / 40 repeaters / 310 msgs 24h` |
| `{online}` | Heard in the last hour | `18 nodes heard in 1h (6 repeaters)` |
| `{new_nodes}` | First heard in 24h | `new 24h: Zed, Amy` |
| `{lastmsg NAME}` | Last thing a node said | `bob 3m ago in #bot: hi` |
| `{top_talkers N}` | Busiest senders, 24h (N up to 5, default 3) | `bob(12) / amy(7)` |
| `{top_relays N}` | Busiest relays in heard paths, 24h | `RptA(40) / RptB(22)` |
| `{forecast PLACE}` / `{wx PLACE}` | Open-Meteo forecast for a postal code, city or `lat,lon` anywhere, geocoded with OpenStreetMap Nominatim (see `weather.*` in SETUP). No argument replies with usage | `Karlsruhe, DE: 7C Lt rain, wind 1km/h \| Today 9/7C Lt showers 73% \| Wed 13/8C Lt showers 75%` |
| `{roll 2d6}` / `{roll 100}` | Dice, up to 20 dice | `2d6: 3+4 = 7` |
| `{pick a, b, c}` | One of the options | `b` |
| `{8ball}` | Magic 8-ball | `outlook good` |
| `{help}` | Triggers of enabled commands | `cmds: !path !ping !seen` |
| `{uptime}` | Bot uptime | `up 3d04h` |
| `{version}` | Plugin version | `openbot 1.0.0` |

`{path}` and `{pathnames}` also queue a second message asking for 2-byte
hashes when the request used 1-byte hashes (rate limited per sender).

## Worked examples

### Ping

```json
{"id": "ping", "trigger": "!ping", "enabled": true,
 "reply": "@[{sender}] pong! {hops} hops traversed, took {took} seconds.",
 "cooldown_s": 10}
```

`!ping` from bob at 4 hops: `@[bob] pong! 4 hops traversed, took 4.6 seconds.`

`{took}` relies on the sender's clock. Its timestamp is whole seconds, so
treat the figure as good to about a second; a sender with an unset or wrong
clock gets `?`. For a signal report instead, use
`@[{sender}] pong {hops}h snr {snr} via {gateway}{region_tag}`, which gives
`@[bob] pong 2h snr 5.5 via alice-bot [#tx-dfw]`.

### Path

```json
{"id": "path", "trigger": "!path", "enabled": true, "reply": "{path}",
 "cooldown_s": 10}
```

Gives `@[bob] [3h] 03b9,4216,d924 https://v.gd/AbC123`. The built-in already addresses the
sender, so the template is just `{path}`. See
[SETUP.md](SETUP.md#path) for how to read it.

### Dice with the user's arguments

```json
{"id": "roll", "trigger": "!roll", "reply": "@[{sender}] {roll {args}}"}
```

`!roll 2d6` gives `@[bob] 2d6: 3+4 = 7`. With no argument it rolls `1d6`.

### Weather announcement with cron

```json
{"id": "wx-morning", "enabled": true, "channel": "#bot", "region": "#tx-dfw",
 "schedule": "0 7 * * 1-5",
 "text": "{forecast 76131}"}
```

Weekdays at 07:00 in `node.timezone`, scoped to `#tx-dfw`. An announcement
has no requester, so name the place in the template. Use `"schedule": "every 6h"` for a plain interval. Mind the 160 byte
cap: if a forecast is long, the end is cut. Test first with `announce_now`
from the UI.

## Notes

- `{pathbot}` is an alias of `{path}`. `{pathnames}` gives names and distance
  inline.
- Roster functions (`{seen}`, `{stats}`, `{online}`, `{new_nodes}`,
  `{lastmsg}`, `{top_talkers}`, `{top_relays}`) count only what this radio
  heard. Messages are kept 7 days, nodes 60.
