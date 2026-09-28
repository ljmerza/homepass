# HomePass

Shareable guest links for controlling Home Assistant devices.

## What it does

HomePass lets you create time-limited guest links that expose specific Home Assistant entities (lights, locks, switches, cameras, etc.) to visitors. No HA account required. Guests get a mobile-friendly PWA with real-time state updates.

## Accessing the admin UI

After installing, HomePass appears in the Home Assistant side panel. Click it to open the admin dashboard. No separate login needed, HA handles authentication automatically.

For direct port access (e.g., `http://<your-ha-ip>:5880/admin/dashboard`), set **Admin Username** and **Admin Password** in the configuration below.

## How guest links work

1. In the admin dashboard, create an **access token** with selected entities and an expiration time.
2. Share the generated link (`http://<your-ha-ip>:5880/g/{slug}`) with your guest.
3. The guest opens the link on their phone. No app install or HA account needed.
4. When the token expires, the guest sees the contact message and can no longer control devices.

The slug in the link is the credential: anyone holding the link has the access. Share it the way you would share a password, and use **Revoke** or **Rotate Link** if it ends up somewhere it should not be.

## Configuration

Set these options in the add-on Configuration tab:

| Option | Description |
|--------|-------------|
| **Admin Username** | Username for direct port access. Not needed when using the HA side panel. |
| **Admin Password** | Password for direct port access (min 8 characters). Not needed when using the HA side panel. |
| **App Name** | Display name shown to guests (default: "Home Access") |
| **Contact Message** | Message shown when a guest link expires |
| **Background Color** | Hex color for page background (e.g., `#F2F0E9`) |
| **Primary Color** | Hex color for accents and buttons (e.g., `#D9523C`) |
| **Guest URL** | External base URL for guest links (e.g., `https://guest.myhouse.com`). Leave empty for local network. |
| **Time Zone** | IANA time zone weekly access windows are evaluated in (e.g., `Europe/Madrid`). Leave empty to use Home Assistant's own time zone, which is right for almost every install. |

These are the only add-on options. Everything else is set per link, in the dashboard.

## Choosing entities

The picker lists every entity in a domain HomePass supports: lights, switches, groups, climate, locks, alarm panels, media players, covers, fans, buttons, counters, timers, the `input_*` helpers, time and date helpers, and the read-only sensors, cameras and schedules. Scripts, scenes and automations are deliberately absent, because running one would take a guest outside the entities you picked.

Three ways to narrow the list, and they combine:

- **Domain chips** — Lights, Switches, Locks, Cameras, and so on.
- **Search** — matches the entity name, including a display name you have overridden.
- **Label** — filter by a Home Assistant label. Once a label or a search has narrowed the list, an **Add all** button appears and takes every match, not just the rows on screen.

If the label row is missing, HomePass could not read the label registry from Home Assistant. Everything else still works.

**Templates** save the current selection under a name so a later link can start from it. Loading a template adds to whatever is already selected, so two templates can be stacked; entities Home Assistant no longer has are dropped quietly.

### Per-entity options

Click a selected entity to open its options:

- **Display name** — what the guest sees instead of the Home Assistant name, up to 64 characters. Leave blank to use the HA name.
- **Show brightness slider** (lights only) — off by default, so a light is on/off unless you turn this on.
- **Show colour controls** (lights only) — also off by default. Gives the guest a colour wheel, a warm–cool temperature slider, or both, depending on what the bulb supports.
- **Require the guest to be at the property** — see below.

These belong to the link, not the entity. The same light can be on/off for the cleaner and fully adjustable for a house guest.

## When a link works

When creating a link, choose one of three modes under **Access**:

- **Single use** — the link stops working once it has been used (set **Uses** above 1 for a few uses). A use is one control the guest presses and Home Assistant accepts. Opening the link does not count, and neither does a chat app such as WhatsApp loading it to draw a preview card, so sending the link cannot use it up. A failed command does not count either. A link with only cameras and sensors on it is never used up. **Valid until** is optional.
- **No expiry** — works until you revoke it.
- **Set a period** — works from **Starts** to **Ends**. Leave the start blank to start now. The quick buttons (+1 day, +7 days, …) set the end relative to the start.

Before its start, a guest who opens the link sees a countdown and a greyed-out preview instead of working controls — no Home Assistant state reaches the page, so the link can be sent days early.

### Weekly times

In **Set a period**, tick **Advanced: only on certain days and times** to limit the link to recurring weekly windows inside the period — "Tuesdays and Thursdays 09:00–13:00" for a cleaner, say. You can add several. An end earlier than the start runs past midnight, and the days you pick are the days the window *opens*: Friday 22:00–02:00 runs from Friday night into Saturday morning. End at 00:00 to run to midnight; 00:00–00:00 is the whole day.

Times are in the house's time zone — Home Assistant's own, unless the **Time Zone** option overrides it. Outside every window the guest sees "Not active right now" with a countdown to the next one, and every part of the link — live state, commands, camera views — is refused. A page left open flips to the countdown when the window closes. If the time zone cannot be read, windowed links refuse access rather than guess.

### Changing it later

**Schedule** on a token's card edits all of the above after the link was created — start, end, weekly times and use limit — and moves any guest who has the link open onto the new schedule at once. Raising **Uses** on a used link grants the extra uses; **Reset the use count** starts it over. **Activate Now** opens a scheduled link straight away without moving its end. **Extend** / **Renew** pushes the end out, and on a used-up single-use link Renew also gives its uses back.

## PIN protection

You can put an optional 4–8 digit PIN on a link, either when creating it or later with the **Add PIN** button on its card. The guest is asked for the PIN before they see anything, and everything behind it — the page, live state, commands and camera views — stays closed until they enter it. One correct entry lasts 24 hours, or until the link expires if that is sooner.

Two things to know:

- **A forgotten PIN cannot be looked up.** PINs are stored hashed. The dashboard can tell you that a link has one; it can never tell you what it is. If a guest forgets it, set a new PIN and tell them the new one.
- **Changing or removing a PIN signs out everyone who had entered the old one.** They are asked again on their next action. It also retires every link without PIN (below).

Guessing is rate-limited, so a PIN entered wrongly several times in a row starts being refused for a minute at a time. Wait and try again.

**Remember the PIN on the guest's device** is on by default. Turn it off — in the create form, or in the PIN dialog, where it saves as soon as you tick it — and the guest's PIN entry only lasts until they close their browser, so they are asked again every time they reopen the link. That suits a link that never expires. Turning it off also asks anyone it was already remembered for to enter the PIN again. Some browsers restore a closed session when they reopen, and keep the entry with it; even then it never lasts past the usual 24 hours.

### Links without PIN

A PIN-protected card has **Copy link without PIN** and **QR without PIN**. Each makes a new link that opens straight to the controls, skipping the keypad — for a QR code on the fridge, say, while the link you text out still asks for the PIN. The guest is signed in the same way a correct PIN would sign them in, and the code is removed from the address bar straight away.

- **Each link is shown once.** Like PINs, links are stored hashed, so copy it or show its QR when you make it. Lost one? Make another.
- **Manage them in the PIN dialog** (**PIN Protected** on the card). It lists every link with an optional label, when it was made and when it was last used. **Rotate** replaces a link with a new one under the same label; **Revoke** removes it. Both sign out every device that had opened that link — other links and the PIN itself are unaffected.
- **Changing or removing the PIN, or rotating the token's link, retires all of them.**
- A link without PIN skips the PIN and nothing else. The IP allowlist, expiry, revocation and a scheduled start all still apply.
- Up to 20 links per token.

## Requiring the guest to be at the property

Any controllable entity can be marked **Require the guest to be at the property**. When the guest presses that one control, their browser is asked where it is, and the command only goes through if the position falls inside Home Assistant's `zone.home`. It is per entity — you can gate the gate release and leave the living-room lamp alone.

- **The guest link has to be served over HTTPS.** Set **Guest URL** to an `https://` address, behind a reverse proxy or Nabu Casa. Browsers refuse to report a location over plain HTTP, and the control refuses with them.
- **It is a deterrent, not proof.** The position comes from the guest's own browser, so someone determined can claim to be anywhere. It is the same kind of protection the IP allowlist gives: good against a guest casually using the link from their own house, not against someone trying to get around it.
- **It fails closed.** No location, a location more than two minutes old, or a `zone.home` that cannot be read all refuse the command.

`zone.home` is the Home zone under **Settings → Areas, labels & zones**. Its radius is what HomePass compares against, so widen it there if guests are being refused at the door.

## Cameras

Add a camera entity to a link and the guest gets a live view on their page. It is read-only: no camera service can be called through a guest link. The view stops when the guest closes or backgrounds the tab, and one link is limited to 8 live views at a time across all of the guest's devices.

## Managing a live link

Each token's card offers:

- **Extend** — push the expiry out. It reads **Renew** on a link that has already expired, been revoked or been used up.
- **Schedule** — change the start, end, weekly times or use limit. See [When a link works](#when-a-link-works).
- **Edit Entities** — change what is on the link, including the per-entity options.
- **Add PIN / PIN Protected** — set, change or remove the PIN, choose whether it is remembered, and manage links without PIN.
- **Copy link without PIN / QR without PIN** — on PIN-protected links only; see above.
- **Rotate Link** — generate a new link and kill the old one immediately. Entities, options, expiry, PIN and history are kept, so use this instead of rebuilding a token when a link has reached the wrong person. A guest who had entered the PIN is asked for it again, and links without PIN are retired.
- **Duplicate** — start a new link pre-filled with this one's entities, IP allowlist, remember-PIN setting and timing: the same mode, use limit, weekly times and length, starting now. The PIN itself is not copied.
- **Revoke** — stop the link working, keeping its history.
- **Delete** — remove the token and its history.

**IP Allowlist** is set when the link is created and is a comma-separated list of CIDRs (`192.168.1.0/24`). It only means anything if HomePass sits behind a reverse proxy that overwrites the client address; on a bare LAN setup it is easy to bypass. To change it later, duplicate the link and revoke the old one.

**Recent activity** in the dashboard shows link opens and commands. Access logs are kept for 90 days.

## Notifications

HomePass fires a `homepass_activity` event on Home Assistant's event bus when a guest link is opened and when a guest command succeeds, and writes a matching Logbook entry. Trigger an automation on that event to get a phone notification when the cleaner unlocks the door. The event carries the link's label, the entity and the service — never the slug or the guest's IP address. The README has a worked automation.

## Troubleshooting

**A guest link looks broken in the HA sidebar.** Guest links are meant to be opened outside Home Assistant, on the direct port or your **Guest URL**. The sidebar panel is the admin dashboard.

**The guest sees "This link is not active yet".** The link has a start time that has not arrived. Use **Activate Now** if that was a mistake.

**The guest sees "Not active right now".** The link has weekly times and it is outside all of them. Check the times against the zone shown under **Advanced** — it is the house's time zone, not the guest's.

**The guest sees "Link Already Used".** A single-use link was used. **Renew** it, or raise its uses under **Schedule**.

**A location-gated control says the link needs to be secure.** The guest is on `http://`. Set **Guest URL** to an HTTPS address.

**The guest gets "too many requests".** The per-link rate limits kicked in. They clear on their own within a minute.

**Nothing works and the add-on log says Home Assistant is unreachable.** HomePass proxies everything to HA and will not serve if it cannot reach it.
