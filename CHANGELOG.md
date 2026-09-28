# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

HomePass is a fork of [Rohithkadaveru/ha-pass](https://github.com/Rohithkadaveru/ha-pass),
unmaintained upstream since April 2026. Releases up to and including 0.2.4 are
upstream's; 0.3.0 is the first release from this fork.

## [1.1.0] - 2026-09-28

### Added

- **Absolute expiry.** Creating, extending or renewing a link takes either a
  duration or an exact end time (`expires_at`). The renew dialog's custom date
  is now the end you picked, not a duration re-anchored to a pending start.
- **Weekly access windows.** Limit a link to recurring days and times — say
  Tue/Thu 09:00–13:00 — with overnight windows supported. Evaluated in Home
  Assistant's time zone, or the new **Time Zone** option. Enforced on every
  guest endpoint; outside a window the page shows a countdown, a live page and
  its camera streams close when the window ends, and a link whose zone can't be
  read refuses (fails closed).
- **Single-use and N-use links.** A use is one guest command Home Assistant
  accepted, claimed atomically and refunded if HA fails. Opening the link — or a
  chat app building a preview of it — never spends a use. A spent link answers
  "Link Already Used"; Renew gives it its uses back.
- **Schedule editing.** A **Schedule** button on each card changes start, end,
  weekly times and use limit after creation, and open guest tabs re-check.
  The create form's timing is now three modes: single use, no expiry, or a
  period with optional weekly times.
- **Links without PIN.** A PIN-protected link can hand out extra
  `/g/<slug>?c=<code>` links (or QR codes) that skip the keypad and nothing
  else. Codes are 192-bit, shown once, stored hashed, accepted on the page only,
  and revocable or rotatable one at a time; revoking one signs out the devices
  it let in. Changing the PIN or rotating the link retires them all.
- **Remember PIN, per link.** Off, a correct PIN lasts only until the guest
  closes their browser. Turning it off signs out remembered sessions.
- **Lock a link to one device.** Optional. The first browser to tap "Use this
  device" owns the link; every other device is refused on every guest endpoint.
  Opening the link never claims it, so chat-app previews can't. **Unbind
  Device** lets the next device claim it; Rotate Link releases it too.
- **Home-network-only controls.** With the new **Home Network Ranges** option
  set, individual entities can be marked usable only from the home network.
  Locks, covers and buttons are pre-ticked when added. Viewing is never gated.
- **Country allowlist, per link.** Restrict a link to visitors from chosen
  countries using an offline DB-IP Lite database baked into the image (IP
  Geolocation by DB-IP, CC BY 4.0). Home-network addresses pass; addresses the
  database can't place are refused.
- **Public REST API.** Optional, off by default: **Enable API** plus an **API
  Token** (min 32 characters) serves `/api/v1` for automations and Node-RED,
  authenticated by `X-API-Key` and rate-limited per IP. It covers every token
  action and field the dashboard has — create, edit, schedule, revoke, renew,
  activate, rotate, duplicate, unbind and links without PIN — through the
  dashboard's own handlers. A self-hosted Swagger UI at `/api/docs` runs under
  the unchanged CSP and needs a dashboard login.
- **Settings from the dashboard.** App name, contact message, brand colours,
  Guest URL and access-log retention can be overridden from a **Settings**
  dialog without a restart; reverting hands control back to the add-on option.
- **Sidebar guest links follow the real port.** Under ingress with no Guest
  URL, links use the host port Supervisor maps to 5880 and the host's name, and
  the dashboard warns when that port is not published.
- **Entity suggestions.** "Doors & locks" and "Lights" chips in the create
  picker add likely entities for review; nothing is selected until pressed.
- **Remember me on the admin login.**
- **Trusted Proxies option.** Comma-separated CIDRs of reverse proxies allowed
  to report the visitor's address. Empty trusts Home Assistant's internal
  network in the add-on, and loopback only standalone. See Security below.
- **Guest pages in the 24 official EU languages**, picked from the browser, and
  the admin dashboard in English and Spanish with a per-browser language pin
  (Settings → Language). Catalogues other than English and Spanish are machine
  translated and unreviewed — corrections welcome.

### Changed

- **Duplicate carries more.** A copy keeps the source's timing mode, use limit,
  weekly times and length (starting now), remember-PIN, device-lock setting and
  country allowlist. The PIN, the start date, a device claim and links without
  PIN are never copied.
- **Guest gates run in one documented order:** dead link → IP allowlist →
  country → PIN (or a link without PIN) → device lock → schedule, then
  home network → proximity on commands.

- **Guest cards redesigned** in the Home Assistant Mushroom style: borderless
  cards with a round icon coloured by what the entity is doing (a locked lock
  green, an unlocked one red, a colour bulb its own colour), and plain section
  headings. Every control works as before.
- **Duplicate from the dashboard keeps per-entity settings**, including the
  proximity and home-network requirements, so a copy is never looser than its
  source.

### Security

- **`X-Forwarded-For` is only believed from trusted proxies.** It was taken
  from any caller, leftmost entry first, so a request could claim any address
  and pass the IP allowlist, the country allowlist and the home-network check,
  or dodge the rate limits. The header is now used only when the connection
  comes from a trusted proxy, read right to left across every header line. If
  your reverse proxy is not on Home Assistant's internal network or loopback,
  add it to **Trusted Proxies**, or every guest behind it will appear to come
  from the proxy (HomePass logs a warning naming the address).
- **Open streams and camera views now end when a device is signed out.**
  Changing the PIN, turning Remember PIN off, or revoking a link without PIN
  used to leave an already-open live view running until it reconnected. Guests
  are told immediately, and open streams re-check access every 30 seconds.

### Migrations

- Four new migrations (009–012): token timing columns, the
  `token_access_codes` table and `remember_pin`, device-binding / country /
  home-network columns, and the `app_settings` table. Existing tokens behave
  exactly as before.

## [1.0.2] - 2026-09-22

### Fixed

- **Guest icons rendered as plain words once the service worker was active.**
  The worker is served with the app's CSP, and a worker's `fetch()` is governed
  by `connect-src`, which was `'self'` only. Its cache-first handler for
  `fonts.googleapis.com` / `fonts.gstatic.com` was refused, so Material Symbols
  never loaded. Both hosts are now allowed in `connect-src`.
- **Local builds shipped a byte-identical service worker.** Without a `GIT_SHA`
  build arg, `sw.js` was always stamped `homepass-dev`, so browsers never
  installed an updated worker. The cache version now includes a build timestamp.
- **HomePass stayed down if Home Assistant wasn't up yet at startup.** A single
  failed connectivity check aborted startup, which is common after a host
  reboot. The check now retries every 5 seconds for up to 5 minutes.

## [1.0.1] - 2026-09-20

### Fixed

- **The guest service worker never controlled anything.** It was registered as
  `/static/sw.js`, and a worker's scope defaults to its own directory, so its
  scope was `/static/` — which does not cover the guest pages at `/g/<slug>`.
  No page was ever controlled, the fetch handler never ran, and the offline PWA
  shell has been inert since it was added. The worker is now served from
  `/g/sw.js` with scope `/g/`. It stays unregistered under Home Assistant
  ingress, as before. A guest who already has the old registration keeps it as
  an inert phantom entry; nothing needs clearing by hand.
- **Static assets could be served stale after an upgrade.** `dist.css`, the JS
  files and the PWA manifest's icon URLs carried no version, and the app sends
  no `Cache-Control` header, so browsers applied heuristic freshness and could
  keep an old copy without revalidating. Every static URL now carries a
  `?v=<build>` that changes with the build. The service worker's cache lookup
  ignores the query string, so the install-time precache still matches rather
  than silently falling through to the network on every load.

### Added

- `icon.png` and `logo.png` for the Home Assistant add-on store, plus this
  changelog, which Supervisor renders on the add-on page.

### Documentation

- `README.md` and `DOCS.md` now describe what the project actually does. The
  rate limit was documented as "30 req/min per token", a figure matching no
  constant in the codebase; the real limits are a 300/min burst plus a
  3000/hour sustained cap on commands. Controllable domains went from 8
  documented to 20, read-only from 2 to 4 (`camera` and `schedule` were
  undocumented), and the `PORT` environment variable was missing entirely.

## [1.0.0] - 2026-09-20

First release under the **HomePass** name.

### Breaking changes

Read these before upgrading — each one requires action or causes a one-time reset.

- **Add-on slug renamed `ha-pass` → `homepass`.** Home Assistant keys an add-on's
  identity *and* its `/data` directory off the slug, so Supervisor treats this as
  a new add-on: uninstall the old one, install this, and copy `/data` across
  manually or you lose every token, PIN and access log. Docker Compose users are
  unaffected.
- **Activity event renamed `ha_pass_activity` → `homepass_activity`.** Any
  automation triggering on the old name stops firing silently. Update its trigger.
- **Session cookies renamed.** Admin and guest PIN sessions are re-established
  once on first visit; guests with a saved PIN session re-enter their PIN.
- **Add-on image and repository URLs now point at this fork.** They previously
  pointed at upstream, so an add-on install pulled upstream's image rather than
  this one.

### Added

- **13 new entity domains** — `alarm_control_panel` (arm home/away/night +
  disarm; `alarm_trigger` deliberately excluded), `button`, `time`, `datetime`,
  `group`, and the Home Assistant helpers `input_number`, `input_text`,
  `input_select`, `input_datetime`, `input_button`, `counter`, `timer`, plus
  read-only `schedule`.
- **Optional per-token PIN.** Stored bcrypt-hashed and write-only. Enforced on
  *every* guest endpoint including both camera routes, with rate-limited attempts
  and constant-time comparison. Sessions are signed with a key derived from the
  token's own hash, so changing or clearing the PIN invalidates them.
- **Per-entity proximity requirement.** Gate an individual entity behind the
  guest being inside HA's `zone.home` — a door relay can be gated without gating
  the living-room lamp. Fails closed if the zone can't be read. The geolocation
  API is never referenced on a page whose token has no gated entity. A soft gate:
  the browser self-reports its position.
- **Colour control for lights**, opt-in per entity: an RGB wheel and a warm–cool
  temperature slider, with server-side payload validation.
- **Scheduled "valid from" start times.** Share a link days before check-in;
  expiry anchors to the start rather than to creation. Pending guests see the
  real card list greyed out with a live countdown and no access to real Home
  Assistant state, and the page unlocks over SSE without a manual reload.
  Includes an admin "Activate Now".
- **Entity templates, label filtering and slug rotation.** Save a named entity
  selection, filter the picker by HA label and bulk-add, and rotate a token's
  slug while keeping its entities, expiry, PIN and history.
- **Multi-window rate limiting** — a burst allowance plus a sustained hourly cap,
  with bucketed counters.
- The guest URL under the QR code is an openable link rather than inert text.

### Fixed

- **The guest PWA never worked behind Home Assistant ingress.** Its state,
  stream, command and camera URLs were root-absolute while static assets used the
  ingress base path, so every API call 404'd behind the Supervisor proxy.
- **A non-admin `HA_TOKEN` logged a warning on every single guest request,
  forever.** Home Assistant restricts `POST /api/events/` to admins, so activity
  events were refused while commands worked. The refusal is now explained once,
  actionably, and retried hourly. The logbook channel is unaffected and latches
  separately.
- Tailwind never scanned `static/domains.js`, so every domain colour was compiled
  out of the stylesheet.

### Removed

- The unused `rate_limit_rpm` column, which implied per-token limits were
  configurable.

### Changed

- CI moved onto [ljmerza/misc-actions](https://github.com/ljmerza/misc-actions):
  shared test, Docker build/push, provenance attestation and PR-image cleanup.
- **The test suite now runs in CI.** It never did before. Test count grew from
  **155 to 448**.

## [0.3.0] - 2026-09-19

First release from this fork.

### Added

- Camera streaming. `camera` joins the read-only domains and two guest endpoints
  relay frames from Home Assistant — a JPEG still and an MJPEG passthrough —
  without ever handing the guest an HA URL or token. Live views are capped per
  token rather than rate-limited per minute; stills use a separate limiter key.
- Per-token entity display overrides, so a guest sees the name you choose rather
  than the entity's `friendly_name`.

### Fixed

- Entity picker search only filtered the Available list, so searching appeared to
  do nothing to entities already on the token. Both lists now use the filtered
  set, with an "N of M" header and an empty state.
- Editing the middle of the picker's search box jumped the caret to the end on
  every keystroke.
- Search, sort and guest tile order now resolve a name the same way the UI
  displays it — display name, then `friendly_name`, then `entity_id` — so a
  renamed entity is findable by its new name.

### Changed

- The picker's filter chips are derived from `DOMAIN_ORDER` instead of a
  hand-maintained list, which had left camera entities reachable only by search.

## [0.2.4] - 2026-04-27

### Added

- Guest activity logging: activity events and Logbook entries for page loads and
  successful commands, plus a recent-activity panel with expandable history in
  the admin dashboard.

## [0.2.3] - 2026-04-26

### Added

- Duplicate action for tokens.

## [0.2.2] - 2026-04-26

### Added

- Read-only sensor support.
- Lock open support.

### Changed

- Expired tokens are retained rather than dropped, so they can be renewed.

## [0.2.1] - 2026-03-15

### Fixed

- QR codes were not scannable by the Android camera app: added the 4-module quiet
  zone required by ISO 18004, removed the rounded corner that clipped the finder
  patterns, scaled the canvas to `devicePixelRatio`, and switched to pure black
  for contrast.

## [0.2.0] - 2026-03-15

### Added

- **Home Assistant add-on support** — `config.yaml`, `repository.yaml`, `DOCS.md`,
  translations and `run.sh`, with ingress-aware routing, ingress auth bypass (HA
  sidebar access with no separate login) and a CSP that allows the ingress iframe.
- Runtime theme system: the `BRAND_BG` and `BRAND_PRIMARY` env vars derive the
  full colour palette, dark mode included, and override the compiled Tailwind
  defaults.
- CI syncs the add-on version in `config.yaml` from the pushed git tag.

### Changed

- Soft revoke moved from `DELETE /tokens/{id}` to `POST /tokens/{id}/revoke` and
  is now idempotent; hard delete took over `DELETE /tokens/{id}`.
- `theme-color` meta tags follow the configured background instead of a hardcoded
  value.

### Security

- Guest commands could bypass the entity allowlist via HA `label_id`; it is now a
  forbidden data key.
- Raw Home Assistant responses are no longer forwarded to guests, and error
  details are generic, so HA status codes and backend identity no longer leak.
- The `X-Ingress-Path` header is only trusted when a `SUPERVISOR_TOKEN` is
  present, preventing header spoofing.
- All 410 responses share one "Access unavailable" detail, preventing slug
  enumeration.

## [0.1.0] - 2026-02-26

Initial upstream release: a Home Assistant guest access proxy offering
time-limited, scoped device control through shareable links, with no HA accounts
needed.

### Added

- Scoped, expiring guest tokens and an admin dashboard to create, revoke and
  extend them.
- Guest PWA with live state over SSE.
- Service allowlist, per-token rate limiting and IP allowlisting.

[1.0.2]: https://github.com/ljmerza/homepass/compare/v1.0.1...v1.0.2
[1.0.0]: https://github.com/ljmerza/homepass/compare/v0.3.0...v1.0.0
[0.3.0]: https://github.com/ljmerza/homepass/compare/v0.2.4...v0.3.0
[0.2.4]: https://github.com/ljmerza/homepass/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/ljmerza/homepass/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/ljmerza/homepass/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/ljmerza/homepass/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/ljmerza/homepass/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/ljmerza/homepass/releases/tag/v0.1.0
