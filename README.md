# openhop-openbot-plugin

**OpenBot** is a MeshCore command bot that runs as a plugin on your
[openHop](https://openhop.dev) Repeater. It shares the repeater's radio, joins
the mesh as its own node, and answers commands. It runs entirely on the
repeater, with no server.

## Commands

| Command | Reply |
|---|---|
| `!path` | The hops your message took, with an optional map link |
| `!ping` | Hop count and time in flight |
| `!mpath` | Every route your message took to CoreScope's observers, with a link (needs CoreScope) |
| `!seen <name>` | When a node was last heard |
| `!whois <hash>` | Node names for a hop hash |
| `!stats` | Roster and traffic counts |
| `!wx <place>` | Weather forecast (off by default) |
| `!help` | The list of commands |

You can switch each one on or off and add your own commands from the
plugin's page in the openHop web UI.

## Features

- Custom commands with reply templates ([docs/TEMPLATES.md](docs/TEMPLATES.md))
- Scope commands by channel, region, DM or hop count. The bot never replies on Public.
- Per-user and per-command cooldowns, plus airtime limits
- Scheduled announcements
- Usage charts for each command, user, channel and region
- One-click updates from GitHub releases

## Install

You need openHop Repeater 1.1.4 or newer.

1. Download the wheel from [Releases](https://github.com/zfouts/openhop-openbot-plugin/releases)
   and upload it on the repeater's **Plugins** page.
2. Generate the bot's identity:
   ```bash
   /var/lib/openhop_repeater/plugins/openhop.openbot/current/venv/bin/openhop-openbot keygen
   ```
3. Add that identity under `identities.companions` in the repeater's
   `config.yaml`, then restart the repeater. The bot needs its own companion,
   because a companion accepts only one client at a time.
4. Open the plugin's page. Set the node name, location and channels, then
   save and enable the plugin.

The full walkthrough is in [docs/SETUP.md](docs/SETUP.md).

`v*` releases are stable. The `edge` release is rebuilt on every push to `main`.

## Docs

- [SETUP.md](docs/SETUP.md): install, configuration, troubleshooting
- [TEMPLATES.md](docs/TEMPLATES.md): reply template syntax and examples
- [CONTRACT.md](docs/CONTRACT.md): `config.json`, `runtime.json` and the command mailbox

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

## Credits

- [Dhovin's meshhub-bot-openhop](https://github.com/Dhovin/meshhub-bot-openhop/)
  set the openHop plugin pattern this follows.
- Weather data by [Open-Meteo.com](https://open-meteo.com/) (CC BY 4.0).
- Geocoding by [OpenStreetMap Nominatim](https://nominatim.org/). Data ©
  OpenStreetMap contributors (ODbL).

## License

MIT. See [LICENSE](LICENSE).
