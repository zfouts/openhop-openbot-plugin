# Setup

For openHop Repeater 1.1.4 or newer, which has the Plugins page. The plugin
manager runs the bot, restarts it, keeps its settings and shows its log.

## What you need

- An openHop Repeater 1.1.4+ that already hears your mesh.
- The wheel `openhop_openbot_plugin-<version>-py3-none-any.whl` from the
  [latest release](https://github.com/zfouts/openhop-openbot-plugin/releases).
- Internet from the repeater host for installing dependencies. Weather
  lookups (`!wx`, which calls Nominatim and Open-Meteo) also need it at run time,
  as do the optional CoreScope map links (for the link shortener).

## Step 1. Install the wheel

On **Plugins**, choose **Install** and upload the wheel. Creating the
environment takes a minute or two on a Raspberry Pi. **OpenBot** then
appears as `DISABLED`. Leave it off for now.

## Step 2. Generate the bot's identity

The bot is a real mesh node with its own keypair.

```bash
/var/lib/openhop_repeater/plugins/openhop.openbot/current/venv/bin/openhop-openbot keygen
```

In Docker:

```bash
docker exec openhop-repeater \
  /var/lib/openhop_repeater/plugins/openhop.openbot/current/venv/bin/openhop-openbot keygen
```

Save the `identity_key` line. It is the private key. Lose it and the bot comes
back as a new node.

## Step 3. Add the companion to the repeater

In the repeater's `config.yaml`:

```yaml
identities:
  companions:
    - name: "YOUR-NODE-NAME"
      identity_key: "PASTE-IDENTITY-KEY-HERE"
      settings:
        node_name: "YOUR-NODE-NAME"
        tcp_port: 5001
        bind_address: "127.0.0.1"
        tcp_timeout: 0
```

Pick a name that is yours (`alice-bot`), not a generic one. This is the name
the radio starts with. Once the bot is running, `node.name` in the plugin page
(step 4) takes over and renames it. Restart the repeater and look for
`Loaded companion 'YOUR-NODE-NAME': hash=0x.., port=5001, bind=127.0.0.1`.

## Step 4. Configure in the plugin page

Open the plugin's page on the Plugins screen. Set:

| Setting | Notes |
|---|---|
| Companion host/port | `127.0.0.1` and `5001`, matching step 3. |
| Node name | Renames the radio. If set and different from the companion's current name, the bot renames the companion when it connects and whenever you change it. openHop saves the new name into the companion's `node_name` in `config.yaml`, so it survives restarts. Empty leaves the radio's name alone. Max 31 bytes (UTF-8); a longer name is reported as a config problem and not applied. Other nodes see it after the next advert (see [Advertising](#advertising)). |
| Latitude, longitude | Position is used for `!path` distances. If left empty, the bot uses a position advertised on the companion. `!wx` does not use it: users ask for a place (`!wx 76131`, `!wx Karlsruhe`). |
| Timezone, units | `America/Chicago` and `imperial` by default. `metric` gives km. |
| Path hash bytes | `2` by default. Applied to the radio on every connect. |
| Channels | Slots the bot joins. `#name` channels need no secret; others need `secret` in hex. |

Changes are saved to `config.json` and picked up within about 2 seconds. No
restart.

Every key is in [CONTRACT.md](CONTRACT.md) and the shipped
`config.default.json`.

## Step 5. Enable and check the log

Press **Enable**, open **Logs**. Within seconds:

```
device clock sync -> ok
path hash: 2-byte -> ok
channel slot 1 = '#bot' -> ok
```

On every connect the bot:

- sets the radio's clock (a repeater with no RTC will otherwise stamp wrong
  times);
- sets the path hash width from `node.path_hash_bytes`;
- writes each configured channel to its slot, then reads back all slots. This
  is additive: slots you did not list are left alone, and Public (slot 0) is
  never set;
- reads the companion's contacts to learn names.

## Step 6. Try it

In the plugin page, use the test box (it sends a `test` action, see
[CONTRACT.md](CONTRACT.md#the-_cmd-mailbox)). It runs the real engine and never
transmits. Then send `!ping` from a phone on the `#bot` channel.

## Scoping

A command fires only when every check passes.

| Check | Global | Per command | Empty means |
|---|---|---|---|
| Channel | `scope.channels` | `commands[].channels` | every slot except protected |
| Region | `scope.regions` | `commands[].regions` | any region, including unscoped |
| DMs | `scope.allow_dm` | `commands[].dm` | inherit |
| Hops | `scope.max_hops` | | no limit |

A per-command value, when set, replaces the global one. When empty (or `null`
for `dm`), it inherits. Channel names ignore case and the leading `#`.

`scope.protected_channels` defaults to `["Public"]` and always wins: even if a
command lists Public, the bot stays silent there. The shipped default scope is
`#bot` only. Widen it deliberately.

Other rules:

- The trigger is matched at the start of the message, case-insensitively, on a
  word boundary. `!path` does not match `!pathx`. The longest matching trigger
  wins.
- The bot ignores its own messages.
- A reply that renders empty is not sent and does not use a cooldown.
- `cooldown_s` is per command, requester, channel and region.
  `limits.user_cooldown_s` is a floor across all commands for one requester.

## Regions

MeshCore regions are transport-code scopes: a flood packet carries a code
derived from a region name such as `#tx-dfw`. These are the same names your
repeater's regions use.

List the regions you care about in the top-level `regions`. The bot checks each
incoming request's code against that list and classifies the request:

| Value | Meaning |
|---|---|
| `#name` | Scoped to that configured region |
| `*` | A plain, unscoped flood |
| `?` | Scoped to a region not in your list, or the frame could not be matched |
| `direct` | Arrived on a direct route; no region applies |

`scope.regions` and `commands[].regions` take region names, and `*` to allow
unscoped floods. Rules once a region list is non-empty:

- `*` must be listed to answer unscoped requests.
- `?` never matches. The bot fails closed rather than answer something it
  cannot place.
- `direct` always matches.

With `limits.reply_in_request_region` on (default), the reply goes out in the
region the request used: a `#tx-dfw` question gets a `#tx-dfw` answer, and an
unscoped one gets an unscoped answer. DMs are not region-scoped. Announcements
take an explicit `region`: empty for the companion's default scope,
`*` for unscoped, or `#name`.

Private `$` regions are not supported. Only `#name` regions can be matched.

## `!path`

```
@[bob] [3h] 03b9,4216,d924 https://v.gd/AbC123
```

`[3h]` is the hop count, followed by the hop hashes in travel order (the
requester's first relay first, this node's neighbour last). With CoreScope
configured (see [CoreScope map links](#corescope-map-links-optional)) the
reply ends with a short link to that route on CoreScope's map. Without it,
the reply is the hop count and hashes only.

The reply always fits one frame. A long chain keeps its first and last hops
and drops hashes from the middle, marked with `…`, so the link still fits.

For hop names and distance instead, use the `{pathnames}` template token in
a command of your own:

```
@[bob] [3h] ~14.2mi RptA>RptB>c3d4?
```

There, `~14.2mi` is the estimated distance (`>` instead of `~` when only part
of the chain could be measured). A hop that cannot be named is shown as its
hash; a trailing `?` means the hash matches more than one node. Long names are
shortened to 10 and then 6 characters, then bare hashes, to stay in one frame.
It resolves against this node's roster (plus CoreScope's nodes, when
configured), and distances need positions: set `node.latitude`/`longitude`
(or advertise a position on the companion).

When a request used 1-byte path hashes, the bot also sends a second message
asking the user to enable 2-byte hashes (at most once per 15 minutes per
sender). Short hashes make hops ambiguous.

## `!mpath`

`!path` shows the one route that reached this node. `!mpath` shows every route
the message took to CoreScope's observers. It needs CoreScope (see [CoreScope
map links](#corescope-map-links-optional)). Without it, `!mpath` stays silent
and `!help` leaves it out.

```
@[bob] asking CoreScope's observers, back in 30s
@[bob] 9 routes to 6 observers, 1-5 hops, 2 direct https://v.gd/AbC123
```

A flood keeps reaching observers for tens of seconds after the first copy
lands, so the bot answers in two parts. It acknowledges straight away, waits
`corescope.mpath_wait_s` seconds (default 30), then asks CoreScope for every
copy of the request's packet and replies on the same channel or DM.

- A **route** is one observer and one distinct hop chain. An observer that
  heard the message over three chains counts three routes.
- **Direct** routes reached an observer with no relays.
- The link opens CoreScope's page for the packet (`<base_url>/#/packet/<HASH>`),
  which lists each observer and the path its copy took. It is shortened like
  `!path`'s links.

The request costs two messages of airtime, so `!mpath` has a 30 s cooldown.

New installs have the `!mpath` command. If your saved commands came from an
older version, open the CoreScope tab and click **Add the !mpath command**,
then save.

## Usage

The bot records every request that matches a command trigger in its SQLite
database (`openbot.db`, table `usage`). Each row has:

- the time it was heard and the command id;
- whether it was answered or skipped, and for a skip the category: cooldown,
  out of scope, empty reply, or other;
- the channel (or `dm`), the region, and the sender.

Sender names are stored, and rows are kept for 366 days. The existing
maintenance prunes older ones. The data stays on the node.

Not recorded: messages that match no command, and the plugin page's test box
(dry runs).

The **Usage** tab shows it:

- Presets: 24 h, 7 d, 30 d, 90 d, 1 y. Or pick a From and To date. The same
  date twice is one day. Dates are in the node's timezone (`node.timezone`).
- Hourly bars for windows up to 48 h, daily bars beyond.
- Breakdowns by command (answered and skipped), the top 10 requesters,
  channels, regions, and skip reasons.

Each query goes through the `_cmd` mailbox, which saves the config, the same
as the test box. If you have unsaved edits you are asked first. The bot must
be running to answer.

## Advertising

The Status tab has two buttons:

- **Advert to neighbours**: a zero-hop advert, heard by nodes in direct range.
- **Advert to whole mesh**: a flood advert. It asks you to confirm first.

Use them after you change `node.name`, so other nodes pick up the new name
without waiting for the next advert. The bot must be
connected to the companion.

## Airtime limits

| Setting | Default | Effect |
|---|---|---|
| `limits.tx_gap_s` | 2.5 | Minimum seconds between transmissions. |
| `limits.tx_max_per_min` | 8 | Cap per minute. The rest wait. |
| `limits.user_cooldown_s` | 5 | Per-requester floor across commands. |

Everything the bot sends (replies, announcements, manual sends) goes through one
queue. It holds at most 12 messages; beyond that they are dropped. Extra
messages from one reply are spaced 5 s apart. Text is clamped to 160 bytes, cut
on a character boundary, never mid-character.

## Announcements

```json
"announcements": [
  {"id": "morning", "enabled": true, "channel": "#bot", "region": "",
   "schedule": "0 8 * * *", "text": "Good morning, {date}. {forecast 76131}"}
]
```

`schedule` is 5-field cron in `node.timezone`, or `every 3600` (seconds; `m`,
`h`, `d` suffixes work).

## Weather (`!wx`)

`!wx <place>` answers for the place asked about, anywhere: a postal code
(`!wx 76131`), a city (`!wx Karlsruhe`, `!wx Fort Worth, TX`) or coordinates
(`!wx 32.9,-97.3`). The lookup is OpenStreetMap Nominatim and the forecast is
Open-Meteo: current conditions plus three days, in the place's own time zone.

```
Fort Worth, TX: 64F Cloudy, wind 3mph | Today 69/53F Cloudy 12% | Tue 74/58F Cloudy 19%
```

| Setting | Default | Meaning |
|---|---|---|
| `weather.units` | `auto` | `auto` picks F/mph for the US (and the few other Fahrenheit countries) and C/km/h elsewhere, by the place asked about. Or force `imperial` / `metric`. |
| `weather.default_country` | `us` | A bare postal code is ambiguous (76131 is Fort Worth and Karlsruhe). It is searched in this country first, then worldwide. Users can add a country: `!wx 76131 DE`. Empty = worldwide. |
| `weather.contact` | empty | Optional email or URL added to the User-Agent, as the Nominatim and Open-Meteo usage policies ask. |

Lookups are cached (places 30 days, forecasts 10 minutes) and Nominatim is
held to one request per second. Open-Meteo's free API is for non-commercial
use.

## CoreScope map links (optional)

If you run or use a [CoreScope](https://github.com/Kpa-clawbot/CoreScope)
instance (a MeshCore analyzer), tick **Use CoreScope (map links in !path,
and !mpath)** on the CoreScope tab (`corescope.enabled`), enter its address
and pick a link shortener. This also turns on [`!mpath`](#mpath). `!path`
then links to the route on CoreScope's map:

```
<base_url>/#/map?packet=956DB1890EBA6B1D&hops=03b9,4216,d924
```

`packet` is the request's packet hash (the same one the firmware and
CoreScope compute), and `hops` the hop hashes the reply shows. The map loads
the packet by its hash, so it draws the route when CoreScope's observers heard
the same packet; `hops` makes it show the path this node heard.

The link is shortened before it goes on the air:

| `corescope.shortener` | Service |
|---|---|
| `v.gd` (default) | [v.gd](https://v.gd/apishorteningreference.php). 200 links an hour per IP. |
| `is.gd` | [is.gd](https://is.gd/apishorteningreference.php), v.gd's sister service, same limits. |
| `tinyurl.com` | TinyURL's keyless `api-create.php` endpoint. |
| `da.gd` | [da.gd](https://da.gd/help). |
| `custom` | Your own shortener running the same software as v.gd and is.gd. Set its address and, if it needs one, its key. |
| `none` | No shortener: the full link, used only when it fits beside the hashes (short paths). |

Each link is shortened once per run. If the shortener fails or is
rate-limited, `!path` uses the full link if it fits beside every hash, and
otherwise sends the hashes alone. Services that need an account token
(Bitly, TinyURL's v2 API) are not offered.

### Custom shortener

Pick **Custom (v.gd API)** to use a shortener you run yourself. It must speak
v.gd's API: the bot calls

```
<shortener_url>/create.php?format=simple&key=<shortener_key>&url=<map link>
```

and expects the short link back as plain text. `key` is left out when no key
is set. Only links on the shortener's own host (under its path, if it has
one) are accepted. The key is never written to the log.

| Setting | Default | Meaning |
|---|---|---|
| `corescope.enabled` | `false` | The tick. |
| `corescope.base_url` | empty | The address you open CoreScope at, without `/api`, for example `https://analyzer.example.net` or `http://10.0.0.5:3000`. http and https both work. |
| `corescope.shortener` | `v.gd` | One of the services above. |
| `corescope.shortener_url` | empty | For `custom`: the shortener's https address, for example `https://short.example.org`. |
| `corescope.shortener_key` | empty | For `custom`: sent as the `key` parameter. Leave empty if your shortener needs none. |
| `corescope.mpath_wait_s` | `30` | Seconds `!mpath` waits for the flood to settle before asking CoreScope, 5 to 300. |

What is sent: the shortener gets the full map link (packet hash and hop
hashes) and nothing else. `!path` makes no request to CoreScope itself.
`!mpath` sends CoreScope only the packet hash (`GET /api/packets/<hash>`), and
the shortener gets the packet page link.

`{pathnames}` (names and distance, see [`!path`](#path)) also uses CoreScope
when it is on: it asks which nodes match each hop hash
(`GET /api/resolve-hops?hops=90e2,fe27`), sending only the hashes, and picks
among them by geography along the chain to this node, so a same-prefix
repeater far away is not chosen just because CoreScope lists it first. If
CoreScope is unreachable or slow (5 s), it answers from the local roster.

## Updating

The Status tab's **Updates** card shows the version running and the latest
release on GitHub. When the release is newer, a notice appears above every
tab with an **Update now** button (the card has the same button and the
release notes).

The check and the update are openHop's plugin updater, not code in the bot.
The page asks openHop (`GET /api/plugins/updates`), which asks GitHub for the
latest full release of the plugin's repository and compares versions the way
pip does. Clicking update (`POST /api/plugins/update`) has openHop download
that release's wheel, stop the bot, install it and start it again. Settings
and data are kept. The bot is silent for about a minute, then the page reloads
on the new version. The page checks when it opens, when you click **Check
again**, and every six hours while it stays open.

- Only full releases are offered. The rolling `edge` pre-release never is.
- An edge build counts as newer than the release it was built from, so
  `2.4.1+edge.17` is offered `2.4.2`, not `2.4.1`.
- openHop installs whatever wheel is attached to the latest release of the
  repository, over HTTPS. It checks the plugin id, not a checksum.
- The repository must be public: openHop calls GitHub without signing in.

**One-time step for a wheel install.** openHop only records which repository
a plugin comes from when it installs it from its catalogue. After installing
by uploading the wheel, the card says **Unavailable**. Record the repository
once, and mark the plugin as a local install, as root or the repeater user, on
the repeater (inside its container if it runs in one):

```bash
python3 - <<'EOF'
import json, pathlib
p = pathlib.Path("/var/lib/openhop_repeater/plugins/openhop.openbot/state.json")
s = json.loads(p.read_text())
s["repository"] = "zfouts/openhop-openbot-plugin"
s["source"] = "local"
p.write_text(json.dumps(s, indent=2) + "\n")
EOF
```

Set `source` too. Without it, openHop labels the plugin a catalogue install
after its first one-click update. It then looks for OpenBot in its own
catalogue, which doesn't list it, and every later check fails with `plugin not
in catalogue`. openHop keeps a `source` that is already set.

openHop keeps the field through later installs and updates, uploaded or not.

## Troubleshooting

| Symptom | Check |
|---|---|
| Updates card says **Unavailable** | The plugin was installed from an uploaded wheel. Do the one-time step in [Updating](#updating). |
| Updates card says GitHub did not find the repository | openHop asks GitHub without signing in, so it cannot see a private repository. Update checks need the repository to be public. |
| Check fails with `plugin not in catalogue: openhop.openbot` | openHop marked the plugin as a catalogue install during a one-click update. Set `"source": "local"` in its `state.json` (the one-time step in [Updating](#updating)), then click **Check again**. |
| Updates card says **Check failed** | The repeater could not reach GitHub, or hit GitHub's limit of 60 unauthenticated API calls an hour (shared with openHop's own plugin checks). Wait and click **Check again**. |
| Update failed | The notice shows openHop's error. A failed download changes nothing. A failed install happens after openHop has stopped the bot, so check the plugin's state on the Plugins page and start it if needed; its log has the details. |
| State `FAILED` or log shows `Companion connection lost ... retry` | Companion not listening. Check step 3, the port, and that nothing else (another plugin, a phone app) holds the companion. |
| Bot never answers | Look at the activity list in the plugin page. A `skip` entry names the reason: `channel ... not in scope`, `region ... not in scope`, `protected channel`, `requester cooldown`, `command cooldown`. |
| Silent when you list regions | The request was `?` or unscoped. Add the region to the top-level `regions`, and add `*` to allow unscoped. |
| Silent on Public | By design. |
| `!path` has no link | CoreScope is off, the frame could not be matched to the message (no packet hash), or the shortener failed and the full link did not fit. Check the log for `link shortening failed`. |
| The map link shows no route | CoreScope's observers did not hear that packet. The link only draws packets CoreScope has. |
| `!mpath` never answers | CoreScope is off, or no enabled command uses `{mpath}` (CoreScope tab: **Add the !mpath command**). |
| `!mpath` says `no CoreScope observer reported hearing that` | None of CoreScope's observers heard the packet, or it had not reached CoreScope by the time the bot asked. Raise `corescope.mpath_wait_s`. |
| `!mpath` says `can't trace that copy: no packet hash` | The raw frame could not be matched to the message. Retry. |
| `{pathnames}` shows only hashes or no distance | The roster is young: wait for adverts or turn on CoreScope. Distances need `node.latitude`/`longitude` and relays that advertise positions. |
| `hop hashes unavailable for that copy` | The raw frame could not be matched to the message. Retry. |
| `!wx` replies with usage | It needs a place: `!wx 76131`, `!wx Karlsruhe`, `!wx 32.9,-97.3`. |
| `!wx <place>` says `couldn't find` | Nominatim had no match. Try `City, Country` (`Paris, FR`) or a postal code with a country (`76131 DE`). |
| `!wx 76131` gives the wrong country | Set `weather.default_country`, or ask with the country: `!wx 76131 DE`. |
| Usage tab says the bot didn't answer | The bot is not running, or the mailbox timed out. Check the plugin state and the log, then try again. |
| Name didn't change on other nodes | Other nodes learn the new name from an advert. Send one from the Status tab ([Advertising](#advertising)). |
| `device clock sync -> REFUSED` | The radio rejected the time set. The bot continues; check repeater firmware. |
| Channel slot `REFUSED` | Bad name or secret for that slot. Hex secrets only. |
| Edits not applied | Config is re-read within 2 s. A syntax error in hand-edited `config.json` keeps the old config; check the log. |
| Duplicate replies | Another bot answers the same trigger on the same channel. Keep triggers or channels distinct. |

Logs: the Plugins page, or `.../openhop.openbot/logs/plugin.log`. Set
`log_level` to `DEBUG` for more.

## Day to day

- **Restart**, **Logs** on the Plugins page. Five unexpected exits in a minute
  put it in `FAILED`.
- **Upgrade**: the update button on the Status tab ([Updating](#updating)),
  or upload the new wheel. `data/` survives either way.
- **Uninstall**: the manager asks whether to delete `data/` (and the roster
  with it). The companion in `config.yaml` is yours to remove.
