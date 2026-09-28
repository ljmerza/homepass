# HomePass

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Shareable guest links for controlling Home Assistant devices**

Create time-limited links that give guests control of specific Home Assistant
entities — lights, locks, thermostats, fans, and more. Guests get a
mobile-friendly PWA with real-time state updates. No HA accounts needed, no app
installs, just a link.

> HomePass is a fork of [Rohithkadaveru/ha-pass](https://github.com/Rohithkadaveru/ha-pass),
> which has been unmaintained since April 2026. Original work is MIT-licensed and
> copyright Rohith Kadaveru; see [LICENSE](LICENSE).

## Screenshots

<p align="center">
  <img src="docs/admin-dashboard.png" width="250" alt="Admin dashboard">
  <img src="docs/admin-dark-extend.png" width="250" alt="Extend expiry (dark mode)">
  <img src="docs/admin-entity-picker.png" width="250" alt="Entity picker">
  <img src="docs/guest-pwa.png" width="250" alt="Guest PWA">
</p>

## Features

- **Scoped guest tokens** — each token grants access to a specific set of entities
- **Time-limited access** — tokens expire after a chosen duration, or never
- **Scheduled start** — an optional start time; the link can be shared early and shows a countdown until then
- **Weekly access windows** — limit a link to recurring days and times (overnight windows supported), in Home Assistant's time zone
- **Single-use links** — a link that stops after one (or N) guest commands; opening it or a chat-app preview never uses it up
- **Optional PIN** — a 4–8 digit PIN on top of the link, enforced on every guest endpoint,
  with revocable links that skip it and a per-link choice of whether it is remembered
- **Single-device lock** — optionally lock a link to the first browser that claims it; other devices are refused
- **Proximity requirement** — mark individual entities as usable only from inside HA's `zone.home`
- **Home-network-only controls** — mark individual entities as commandable only from your home network's IP ranges
- **Per-entity overrides** — rename an entity for the guest, and opt lights into a brightness slider and colour controls
- **Camera streaming** — read-only live views; no `camera.*` service is reachable
- **Entity templates and label filtering** — save a named selection, filter the picker by HA label, bulk-add every match
- **Entity suggestions** — opt-in "Doors & locks" and "Lights" chips that pre-fill a new link's selection for review
- **Slug rotation** — mint a new link for an existing token and retire the old one
- **Real-time updates** — SSE-powered live state changes with automatic reconnect
- **Installable PWA** — guests can add it to their home screen for an app-like experience
- **Dark mode** — system-aware with manual override
- **State-coloured cards** — guest cards in the style of Home Assistant's Mushroom cards: the round icon shape takes its colour from what the entity is doing (a lit lamp amber, or its own bulb colour; a locked door green, an unlocked one red), and goes neutral when idle
- **Languages** — guest pages in all 24 official EU languages, picked from the guest's browser; the admin dashboard in English or Spanish, auto-detected or pinned per browser
- **Admin dashboard** — create, revoke, extend, and monitor tokens
- **Live settings** — change the app name, contact message, Guest URL, colours and log retention from the dashboard, no restart
- **Recent activity** — see guest link opens and commands in the admin dashboard
- **Service allowlist** — only the services a domain's guest controls actually call are permitted
- **Rate limiting** — 300 requests/minute and 3000 requests/hour per token on commands
- **IP allowlisting** — optionally restrict tokens to specific CIDRs
- **Country allowlisting** — optionally restrict a link to visitors from chosen countries, using an offline GeoIP database
- **Public REST API** — optional, key-authenticated `/api/v1` for automations and Node-RED, with Swagger docs (off by default)

## Installation

### Home Assistant Add-on (recommended)

1. Add this repository in **Settings → Add-ons → Add-on Store → ⋮ → Repositories**:

   ```
   https://github.com/ljmerza/homepass
   ```

2. Find **HomePass** in the store and click **Install**.
3. Go to the **Configuration** tab and set your options.
4. Start the add-on.
5. Click **Open Web UI** or find HomePass in the HA sidebar.

Admin access works through the HA sidebar — no separate login needed. Guest
links use the direct port (`http://<your-ha-ip>:5880/g/{slug}`) so visitors
don't need HA accounts. Without a Guest URL, the sidebar builds those links from
the host name and Network port the Supervisor reports, so a remapped port is
followed automatically.

### Docker Compose

```yaml
services:
  homepass:
    image: ghcr.io/ljmerza/homepass:latest
    restart: unless-stopped
    ports:
      - 5880:5880
    volumes:
      - ./data:/data
    environment:
      - ADMIN_USERNAME=admin
      - ADMIN_PASSWORD=changeme
      - HA_BASE_URL=http://homeassistant.local:8123
      - HA_TOKEN=your_long_lived_token_here
```

```bash
docker compose up -d
```

### Docker Run

```bash
docker run -d --restart unless-stopped \
  -p 5880:5880 \
  -v ./data:/data \
  -e ADMIN_USERNAME=admin \
  -e ADMIN_PASSWORD=changeme \
  -e HA_BASE_URL=http://homeassistant.local:8123 \
  -e HA_TOKEN=your_long_lived_token_here \
  ghcr.io/ljmerza/homepass:latest
```

The admin dashboard is at `http://localhost:5880/admin/dashboard`.

> **Note:** Docker deployments need a [long-lived access token](https://developers.home-assistant.io/docs/auth_api/#long-lived-access-token) from Home Assistant. Create one in your HA profile under **Security → Long-Lived Access Tokens**, from a user with **Administrator** enabled — Home Assistant only lets admins POST to `/api/events/`, so a non-admin token runs guest commands fine but cannot fire HomePass's activity events. The add-on handles this automatically.

## Configuration

### Add-on Options

Set these in **Settings → Add-ons → HomePass → Configuration**:

| Option | Description | Default |
|---|---|---|
| **Admin Username** | Username for direct-port admin access (not needed for sidebar) | — |
| **Admin Password** | Password for direct-port admin access (min 8 chars) | — |
| **App Name** | Display name shown to guests | `Home Access` |
| **Contact Message** | Message shown when a guest link expires | `Please request a new link...` |
| **Background Color** | Hex color for page background | `#F2F0E9` |
| **Primary Color** | Hex color for accents and buttons | `#D9523C` |
| **Guest URL** | External base URL for guest links (e.g. `https://guest.myhouse.com`) | — |
| **Time Zone** | IANA zone for weekly access windows (e.g. `Europe/Madrid`); empty uses Home Assistant's | — |
| **Home Network Ranges** | Comma-separated CIDRs that count as your home network (e.g. `192.168.1.0/24`). Empty turns home-network-only controls off | — |
| **Enable API** | Serve the REST API under `/api/v1` | `false` |
| **API Token** | `X-API-Key` value for the API (min 32 chars, required when enabled) | — |

### Settings in the dashboard

App name, contact message, Guest URL, both colours and access-log retention
can also be changed live from the dashboard's **Settings** dialog. Precedence,
highest first:

1. A value saved in the dashboard (stored in the SQLite database)
2. The add-on option, or the environment variable in Docker
3. The built-in default

**Revert** removes the dashboard value so the option applies again. Nothing is
copied into the database on upgrade, so an install that never opens the dialog
behaves exactly as before. Credentials and connection settings (`ADMIN_*`,
`HA_BASE_URL`, `HA_TOKEN`, `DB_PATH`, `PORT`) can only be set as options or
environment variables.

### Docker Environment Variables

| Variable | Description | Required | Default |
|---|---|---|---|
| `ADMIN_USERNAME` | Admin login username | Yes | — |
| `ADMIN_PASSWORD` | Admin login password (min 8 chars) | Yes | — |
| `HA_BASE_URL` | Home Assistant base URL | Yes | — |
| `HA_TOKEN` | HA long-lived access token | Yes | — |
| `DB_PATH` | SQLite database path | No | `/data/db.sqlite` |
| `PORT` | HTTP port to listen on | No | `5880` |
| `APP_NAME` | Display name shown to guests | No | `Home Access` |
| `CONTACT_MESSAGE` | Message shown on expired pages | No | `Please request a new link...` |
| `ACCESS_LOG_RETENTION_DAYS` | Days to retain access logs | No | `90` |
| `BRAND_BG` | Background color (hex) | No | `#F2F0E9` |
| `BRAND_PRIMARY` | Primary/accent color (hex) | No | `#D9523C` |
| `GUEST_URL` | External base URL for guest links | No | — |
| `TIMEZONE` | IANA zone for weekly access windows; unset uses Home Assistant's | No | — |
| `LOCAL_NETWORK_CIDRS` | Comma-separated CIDRs that count as the home network | No | — |
| `GEOIP_DB_PATH` | IP-to-country CSV for country allowlists (see below) | No | `/app/geoip/dbip-country-lite.csv.gz` |
| `API_ENABLED` | Serve the REST API under `/api/v1` | No | `false` |
| `API_TOKEN` | `X-API-Key` value for the API (min 32 chars) | When `API_ENABLED` | — |

## Home Assistant Activity Events

HomePass emits a `homepass_activity` event after a valid guest page load and after
a successful guest command. These events are best-effort notification hooks:
HomePass logs and drops event failures without blocking the guest. HomePass also
writes matching Home Assistant Logbook entries for the Activity view.

Event payloads do not include the guest slug, internal token ID, or client IP
address.

```json
{
  "schema_version": 1,
  "activity": "command",
  "token_label": "Cleaner",
  "target_entity_id": "lock.front_door",
  "service": "lock.unlock"
}
```

`activity` is either `page_load` or `command`. `page_load` means the guest link
URL was requested; link previews, scanners, stale bookmarks, and refreshes can
also trigger it. Use `command` for higher-signal notifications.

```yaml
alias: HomePass guest activity notification
triggers:
  - trigger: event
    event_type: homepass_activity
conditions:
  - condition: template
    value_template: "{{ trigger.event.data.activity == 'command' }}"
actions:
  - action: notify.mobile_app_phone
    data:
      title: "Guest access"
      message: >
        {{ trigger.event.data.token_label }} used
        {{ trigger.event.data.service }}
        on {{ trigger.event.data.target_entity_id }}
```

## Languages

Guest links — the app itself and the expired, PIN, device-claim and "in use on
another device" pages — follow the language the guest's browser asks for, in
any of the 24 official EU languages (bg, cs, da, de, el, en, es, et, fi, fr, ga,
hr, hu, it, lt, lv, mt, nl, pl, pt, ro, sk, sl, sv). Anything else gets English.
There is no language switch on the guest side: the browser is the one thing the
page knows about its reader.

The admin dashboard speaks English and Spanish. It follows the browser too, and
**Settings → Language** pins one for that browser (a cookie scoped to the
dashboard; **Automatic** clears it).

What is translated is HomePass's own text. The names you give — the app name,
the contact message, token labels, per-entity display names — are shown as you
wrote them, and so are Home Assistant's own entity names and select options.
Error details from the API stay in English; the guest page translates the
refusals it shows to a guest.

Strings live in `app/locales/<guest|admin>/<lang>.json`, one flat file per
language with English as the reference; `tests/test_i18n.py` fails if any
catalogue's keys, placeholders or plural forms drift from English's. Everything
beyond English and Spanish was machine translated and has not been reviewed by
native speakers — corrections are welcome.

## Supported Entity Types

### Controllable Domains

Each domain lists exactly the services its guest control calls, and nothing more.

| Domain | Allowed Services |
|---|---|
| `light` | `turn_on`, `turn_off`, `toggle` |
| `switch` | `turn_on`, `turn_off`, `toggle` |
| `input_boolean` | `turn_on`, `turn_off`, `toggle` |
| `group` | `turn_on`, `turn_off`, `toggle` |
| `climate` | `set_temperature`, `set_hvac_mode`, `turn_on`, `turn_off` |
| `lock` | `lock`, `unlock`, `open` |
| `alarm_control_panel` | `alarm_arm_home`, `alarm_arm_away`, `alarm_arm_night`, `alarm_disarm` |
| `media_player` | `media_play`, `media_pause`, `media_stop`, `volume_set`, `media_play_pause`, `turn_on`, `turn_off` |
| `cover` | `open_cover`, `close_cover`, `stop_cover` |
| `fan` | `turn_on`, `turn_off`, `toggle`, `set_percentage` |
| `button` | `press` |
| `time` | `set_value` |
| `datetime` | `set_value` |

### Helper Domains

Home Assistant helpers, from **Settings → Devices & Services → Helpers**.

| Domain | Allowed Services |
|---|---|
| `input_number` | `set_value` |
| `input_text` | `set_value` |
| `input_select` | `select_option` |
| `input_datetime` | `set_datetime` |
| `input_button` | `press` |
| `counter` | `increment`, `decrement`, `reset` |
| `timer` | `start`, `pause`, `cancel` |

### Read-Only Domains

| Domain | Access |
|---|---|
| `sensor` | Real-time state display only |
| `binary_sensor` | Real-time state display only |
| `camera` | Live view only — every `camera.*` service is refused |
| `schedule` | Current state only — its services rewrite the whole weekly schedule |

`script`, `scene` and `automation` are excluded on purpose: they run arbitrary
automations and bypass entity scoping. `alarm_control_panel.alarm_trigger` is
excluded for the same reason — no arm/disarm widget needs to set off a siren.

## Guest Link Options

Every option below is set per link, either when the link is created or afterwards
from the token's card in the admin dashboard.

### Timing

Three modes: **Single use**, **No expiry**, or **Set a period** with a start and
an end. Before its start a link answers "not active yet" with a countdown, so it
can be shared straight away. **Activate Now** opens a scheduled link early
without moving its end, and **Schedule** on the card edits the timing later
(`PATCH /admin/tokens/{id}/schedule`).

Under **Set a period → Advanced**, a link can be limited to weekly windows —
`{"weekdays": [1, 3], "start": "09:00", "end": "13:00"}`, 0 = Monday — inside
its period. An end before the start crosses midnight, the weekday is the day the
window opens, and `24:00` is accepted as an end. Windows are evaluated in Home
Assistant's configured time zone (read from `/api/config`) unless the
`timezone` option overrides it; if neither can be read, windowed links fail
closed. Outside every window every guest route is refused, and the SSE stream
and camera views stop when a window closes.

**Single use** (`max_uses`, 1–1000) counts one use per guest command Home
Assistant accepts. Page loads, `/state`, the stream and cameras never count, so
a chat app unfurling the link cannot spend it; the claim is atomic and refunded
if the HA call fails. Renew on a used-up link resets its count.

The API's `POST /admin/tokens` takes exactly one of `expires_in_seconds` (a
duration, measured from the start) or `expires_at` (an absolute epoch second,
taken as written).

### PIN

A 4–8 digit PIN, optional and off by default. With one set, the link shows a PIN
screen first, and every guest endpoint behind it — the page, the state feed, the
SSE stream, commands and both camera routes — is refused until the PIN is
entered. A correct entry lasts 24 hours, or until the link expires, whichever is
sooner.

PINs are bcrypt-hashed and write-only. The dashboard reports whether a link has
one; it can never show you what it is. **A forgotten PIN is replaced, not
recovered** — set a new one. Setting or clearing a PIN signs out every guest who
had already entered the old one.

**Remember PIN** is per link and on by default. Off, the PIN session is a
browser-session cookie (no `Max-Age`), so the guest enters the PIN again on
their next visit; the signed 24-hour expiry still applies on top. Turning it off
signs out sessions that were being remembered.

**Links without PIN** (`/g/{slug}?c=<code>`) are minted from the dashboard with
an optional label. The code is 192 random bits, returned once and stored as a
SHA-256 hash. Opening one checks it in constant time against that token's links,
rate-limited per IP and per link, sets the same PIN session cookie a correct PIN
would, and redirects to the bare `/g/{slug}` so the code leaves the address bar.
The code is honoured on that page only — never on `/state`, `/stream`,
`/command` or the camera routes — and skips the PIN but not the IP or country
allowlist, expiry, revocation, a use limit, the schedule or a device lock (the
redirect lands on the claim screen or the countdown when those apply). The
session it sets names the link, so revoking or rotating a link signs out the
devices it let in. Changing or clearing the PIN, or rotating the slug, deletes
every link. Up to 20 per token.

Signing out reaches open connections too. Changing the PIN, turning Remember PIN
off, or revoking or rotating a link without PIN pushes `access_changed` down the
link's live streams, which hang up and reload the page — onto the PIN screen for
a device that was signed out. Behind that push, the SSE stream and live camera
views re-run their gate every 30 seconds, so a device that no longer passes it
stops receiving state and frames even if the push never reached it.

### Single-device lock

Optional and off by default, set when the link is created or later with **Lock
to One Device** on its card. The link then belongs to the first browser that
taps **Use this device** on it; every other device gets a "link in use on
another device" page, and every guest endpoint — the page, the state feed, the
SSE stream, commands and both camera routes — refuses it.

- **Opening the link never claims it.** Chat apps fetch a pasted link to build a
  preview card, and a claim on page load would hand the link to that fetcher
  before the guest ever tapped it. Claiming is a form POST from the claim
  screen, which names neither the link nor anything on it.
- **"One device" means one browser's cookies.** The claim is an `HttpOnly`,
  `SameSite=Lax` cookie scoped to the link; only its SHA-256 is stored. A guest
  who clears cookies, switches browsers, or claimed the link inside a chat app's
  built-in browser (Instagram, Facebook and some others keep their own cookie
  jar) is refused like a stranger. That is the tradeoff: HomePass does not guess
  which of two browsers is "really" the guest, because any rule loose enough to
  let the guest's other browser in lets a forwarded link in too. The claim screen
  tells the guest to open the link in their normal browser first, the refusal
  page says the same, and **Unbind Device** on the card lets the next device
  claim it. On iOS a Home Screen web app may also keep cookies separate from
  Safari, so a guest who adds the page to their home screen after claiming may
  need an unbind.
- **Rotate Link releases the claim**, since it hands the link to someone new.
  Extending or renewing keeps it. Claims and refusals appear in Recent Activity.

With a PIN as well, the PIN comes first: a second device learns nothing about
the link, not even that it is claimed, until it has entered the PIN. A
scheduled link can be claimed before it opens.

### Proximity requirement

Per entity, off by default. With it on, pressing that one control asks the
guest's browser where it is, and the command only goes through if the position
falls inside Home Assistant's `zone.home`. Everything else on the link is
unaffected, and a link with nothing gated never references the geolocation API at
all.

Two limitations, both real:

- **The link has to be served over HTTPS.** Browsers withhold location on plain
  HTTP, so the control refuses rather than quietly passing.
- **The position is self-reported by the browser.** A guest willing to edit the
  request can claim to be anywhere. Treat it as friction against casual use of
  the link from elsewhere — the same caveat the IP allowlist carries — not as
  proof anyone is at the door.

It fails closed: no fix, a fix older than two minutes, or a `zone.home` that
cannot be read all refuse the command.

### Home network only

Per entity, off by default, and only offered once **Home Network Ranges** is
set. The guest can see the control from anywhere, but a command for it only goes
through when the request comes from one of those ranges — the house Wi-Fi, for
instance. Adding a lock, cover, button or input button to a link ticks it
automatically; untick it for a guest who needs the garage from the road. Other
entities on the link are unaffected, and an empty option turns every flag off.

It is per entity rather than a fixed rule for those domains, matching the
proximity requirement: the add-on option says what the home network is, and
each link says which controls need it — including ones a domain list would miss,
like an alarm panel's disarm.

The check uses the same client address as the IP allowlist (see
[Trusted proxies](#trusted-proxies)). Two setups need thought:

- **Tunnels and public URLs.** A guest on your Wi-Fi who opens the public link
  (a Cloudflare Tunnel, Nabu Casa, or hairpin NAT) usually reaches HomePass from
  your public IP, not a LAN address. Add your public address to the ranges, or
  have LAN clients resolve the guest hostname to the LAN address.
- **Being on the network is not being at the door.** Anyone on the Wi-Fi, or on
  a VPN into it, passes. Combine it with the proximity requirement if that
  matters.

### Per-entity display options

Open any selected entity in the picker to set:

- **Display name** — up to 64 characters, shown to the guest instead of the Home
  Assistant name. Blank falls back to the HA name.
- **Show brightness slider** (lights) — off by default; the guest gets on/off only.
- **Show colour controls** (lights) — off by default. The guest gets an RGB wheel,
  a warm–cool temperature slider, or both, depending on what Home Assistant
  reports the light supports.

Neither light toggle gates the command path — putting a light on a link is what
grants `light.turn_on`. They decide what the guest UI draws.

### Entity templates, labels and rotation

- **Templates** — save the picker's current selection under a name and load it
  into a later link. Loading adds to the selection rather than replacing it, and
  entities Home Assistant no longer has are dropped.
- **Label filter** — filter the picker by Home Assistant label, and **Add all**
  every entity matching the current filter. Labels are read over HA's WebSocket
  API; if they cannot be read the filter is hidden rather than shown empty.
- **Rotate Link** — mints a new slug for an existing token. The old link stops
  working immediately, including any open SSE stream. Entities, overrides,
  expiry, PIN and access history survive, and a guest holding a PIN session is
  asked for the PIN again. Links without PIN are deleted.

### IP allowlist

An optional comma-separated list of CIDRs, set when the link is created. It
checks the client address described under [Trusted proxies](#trusted-proxies).

### Trusted proxies

Every per-address decision — the IP and country allowlists, the home-network
gate and the rate limits — uses one client address. `X-Forwarded-For` is only
believed when the connection comes from a trusted proxy, and is then read right
to left, skipping trusted hops, so a client cannot choose its own address by
sending the header. Loopback is always trusted. The **Trusted Proxies** option
(`TRUSTED_PROXIES`, comma-separated CIDRs) sets the rest; left empty, the add-on
trusts Home Assistant's internal network (`172.30.32.0/23`: ingress and proxy
add-ons such as NGINX Proxy Manager or Cloudflared), and standalone trusts
loopback only.

A proxy that is not trusted is treated as the client: every guest behind it
shares its address, allowlists refuse them, and HomePass logs a warning once
naming the address to add.

### Country allowlist

An optional comma-separated list of ISO country codes (`GB, IE`), set when the
link is created. The whole link — page, state, stream, commands, cameras, the
PIN and claim forms — then only opens from addresses registered to one of those
countries. Requests from inside **Home Network Ranges** always pass, since a LAN
address has no country — judged the same way as home-network-only controls, so a
forged `X-Forwarded-For` does not earn the exemption; any other address the database cannot place is refused,
and so is everything if no database is installed. Codes are checked against the
installed database when the link is created, so a typo like `UK` (for `GB`) is
rejected rather than locking every guest out. It applies alongside the IP
allowlist, and carries the same reverse-proxy requirement.

It is coarse by nature: it says where an address is registered, not where the
guest is. A VPN, a corporate network or a roaming SIM can put a guest in another
country without their moving.

The lookup is offline. The image downloads DB-IP's free **IP to Country Lite**
database when it is built and reads it from disk, so guest addresses are never
sent anywhere. That database is licensed under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/), which allows shipping
it in the image with attribution: [IP Geolocation by DB-IP](https://db-ip.com).
It is refreshed monthly upstream and each image carries the copy current at its
build, so accuracy slowly drifts until the next release. For Docker installs,
`GEOIP_DB_PATH` can point at a newer file in the same layout
(`first_ip,last_ip,country_code` CSV, optionally gzipped). The table is loaded
in the background at startup when a live link has a country allowlist, or on
first use otherwise — a second or two of parsing, longer on a Raspberry Pi, and
roughly 10 MB of memory. Installs with no country-restricted link never load it.

## Public API

Off by default. Set **Enable API** and an **API Token** of at least 32
characters (`API_ENABLED=true` / `API_TOKEN=...` for Docker), then send the
token as `X-API-Key`:

```bash
curl -H "X-API-Key: $HOMEPASS_API_TOKEN" http://<your-ha-ip>:5880/api/v1/tokens

curl -X POST -H "X-API-Key: $HOMEPASS_API_TOKEN" -H "Content-Type: application/json" \
  -d '{"label": "Cleaner", "entity_ids": ["lock.front_door"], "expires_at": 1798761600}' \
  http://<your-ha-ip>:5880/api/v1/tokens
```

Endpoints under `/api/v1/tokens`: list, create, get, `PATCH` (label, entities,
expiry, PIN, `remember_pin`, `device_binding`), `DELETE`, `PUT .../{id}/schedule`
(start, end, weekly windows, use limit), `POST .../{id}/revoke`, `renew`,
`activate`, `rotate-slug`, `duplicate` and `unbind`, and
`.../{id}/access-codes` (list, create, `{code_id}/rotate`, `DELETE {code_id}`)
for links without PIN. Create accepts every field the dashboard sets, and
responses carry them back. Each one runs the dashboard's own logic, so
validation and side effects (open guest tabs being told a link was revoked, for
instance) are identical. Expiries take `expires_at` (Unix seconds) or
`expires_in_seconds`. A PIN can be set but is never returned.

The key is the only credential — a dashboard session does not unlock the API.
Swagger UI is at `/api/docs` (linked from the dashboard header) and the schema
at `/api/openapi.json`; both need a dashboard login, and the schema also accepts
the key. Swagger UI is bundled into the image at build time, pinned and
checksummed, so the docs page runs under the same Content Security Policy as
the rest of the app and nothing is loaded from a CDN.

## Limits

Hardcoded, not configurable.

| Limit | Value |
|---|---|
| Guest commands | 300/minute and 3000/hour, per link |
| Camera snapshots | 120/minute, per link |
| Concurrent camera streams | 8, per link |
| Refused proximity checks | 5/minute and 30/hour, per link |
| PIN attempts | 5/minute and 20/hour per IP; 15/minute and 100/hour per link |
| Link-without-PIN checks | 10/minute and 60/hour per IP; 30/minute and 300/hour per link |
| Admin login attempts | 5/minute per IP |
| Public API requests | 120/minute per IP |
| Access log retention | 90 days, via `ACCESS_LOG_RETENTION_DAYS` |

The command allowance is deliberately loose on the short window: the colour wheel
streams throttled updates for as long as the guest drags it. The hourly cap is
the real ceiling.

## Architecture

```
Browser (Guest PWA)
    │
    ├── GET  /g/{slug}               → PWA shell (HTML), or the PIN screen
    │         ?c=<code>              → link without PIN: sets the session, redirects
    ├── POST /g/{slug}/pin           → PIN entry
    ├── POST /g/{slug}/bind          → claim a device-locked link
    ├── GET  /g/{slug}/manifest.json → PWA manifest
    ├── GET  /g/{slug}/state         → initial entity states
    ├── GET  /g/{slug}/stream        → SSE real-time updates
    ├── GET  /g/{slug}/camera/{id}   → camera still and MJPEG stream
    └── POST /g/{slug}/command       → service call proxy
                                      │
                                      ▼
                                  HomePass
                                  (FastAPI)
                                      │
                                      ├── REST API → Home Assistant
                                      └── WebSocket → HA event bus
```

## Disclaimer

HomePass is not affiliated with, endorsed by, or associated with Home Assistant
or Nabu Casa Inc. "Home Assistant" is a trademark of Nabu Casa Inc.

## License

[MIT](LICENSE)
