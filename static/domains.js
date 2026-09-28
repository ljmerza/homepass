// Shared domain configuration — single source of truth for guest + admin UIs.
// To add a new HA entity domain, edit only this file.
const DOMAIN_ORDER = ['camera','light','switch','input_boolean','group','climate','lock','alarm_control_panel',
  'media_player','cover','fan','button','input_button','counter','timer','input_number','input_text',
  'input_select','time','datetime','input_datetime','schedule','sensor','binary_sensor'];
const DOMAIN_LABELS = {
  camera: 'Cameras', light: 'Lights', switch: 'Switches', input_boolean: 'Switches', group: 'Groups',
  climate: 'Climate', lock: 'Locks', alarm_control_panel: 'Alarm', media_player: 'Media',
  cover: 'Covers', fan: 'Fans', button: 'Buttons', input_button: 'Buttons',
  counter: 'Counters', timer: 'Timers', input_number: 'Numbers', input_text: 'Text',
  input_select: 'Selectors', time: 'Time', datetime: 'Date & Time', input_datetime: 'Date & Time',
  schedule: 'Schedules', sensor: 'Sensors', binary_sensor: 'Binary Sensors',
};
const DOMAIN_ICONS = {
  camera: 'videocam', light: 'lightbulb', switch: 'toggle_on', input_boolean: 'toggle_on', group: 'workspaces',
  climate: 'thermostat', lock: 'lock', alarm_control_panel: 'security', media_player: 'speaker',
  cover: 'blinds', fan: 'mode_fan', button: 'radio_button_checked', input_button: 'radio_button_checked',
  counter: 'exposure_plus_1', timer: 'timer', input_number: 'tune', input_text: 'text_fields',
  input_select: 'list', time: 'schedule', datetime: 'event', input_datetime: 'event',
  schedule: 'calendar_month', sensor: 'sensors', binary_sensor: 'motion_sensor_active',
};
// Domains that share a label share a colour, so the guest reads one "Switches"
// or "Buttons" group even though HA splits them across two domains.
const DOMAIN_COLORS = {
  camera: { bg: 'bg-indigo-500/10', text: 'text-indigo-500', icon: 'bg-indigo-500' },
  light: { bg: 'bg-amber-500/10', text: 'text-amber-500', icon: 'bg-amber-500' },
  switch: { bg: 'bg-teal-600/10', text: 'text-teal-600', icon: 'bg-teal-600' },
  input_boolean: { bg: 'bg-teal-600/10', text: 'text-teal-600', icon: 'bg-teal-600' },
  group: { bg: 'bg-teal-600/10', text: 'text-teal-600', icon: 'bg-teal-600' },
  climate: { bg: 'bg-blue-500/10', text: 'text-blue-500', icon: 'bg-blue-500' },
  lock: { bg: 'bg-red-500/10', text: 'text-red-500', icon: 'bg-red-500' },
  alarm_control_panel: { bg: 'bg-rose-600/10', text: 'text-rose-600', icon: 'bg-rose-600' },
  media_player: { bg: 'bg-purple-500/10', text: 'text-purple-500', icon: 'bg-purple-500' },
  cover: { bg: 'bg-sky-500/10', text: 'text-sky-500', icon: 'bg-sky-500' },
  fan: { bg: 'bg-emerald-500/10', text: 'text-emerald-500', icon: 'bg-emerald-500' },
  button: { bg: 'bg-orange-500/10', text: 'text-orange-500', icon: 'bg-orange-500' },
  input_button: { bg: 'bg-orange-500/10', text: 'text-orange-500', icon: 'bg-orange-500' },
  counter: { bg: 'bg-yellow-500/10', text: 'text-yellow-600', icon: 'bg-yellow-600' },
  timer: { bg: 'bg-pink-500/10', text: 'text-pink-500', icon: 'bg-pink-500' },
  input_number: { bg: 'bg-violet-500/10', text: 'text-violet-500', icon: 'bg-violet-500' },
  input_text: { bg: 'bg-slate-500/10', text: 'text-slate-500', icon: 'bg-slate-500' },
  input_select: { bg: 'bg-fuchsia-500/10', text: 'text-fuchsia-500', icon: 'bg-fuchsia-500' },
  // camera already owns indigo, so the time family takes green instead.
  time: { bg: 'bg-green-600/10', text: 'text-green-600', icon: 'bg-green-600' },
  datetime: { bg: 'bg-green-600/10', text: 'text-green-600', icon: 'bg-green-600' },
  input_datetime: { bg: 'bg-green-600/10', text: 'text-green-600', icon: 'bg-green-600' },
  schedule: { bg: 'bg-gray-500/10', text: 'text-gray-500', icon: 'bg-gray-500' },
  sensor: { bg: 'bg-cyan-500/10', text: 'text-cyan-600', icon: 'bg-cyan-600' },
  binary_sensor: { bg: 'bg-lime-500/10', text: 'text-lime-600', icon: 'bg-lime-600' },
};

// ── Entity state colours (guest cards) ───────────────────────────────
// What an entity is *doing*, not what kind of thing it is, tints the round
// shape behind its icon on a guest card: the most recognisable trait of the
// Mushroom Lovelace cards the guest page borrows its look from (the look, not
// the code). An idle entity goes neutral, so a room of switched-off lights
// reads as calm rather than as a wall of amber, and a lock says green for
// secured and red for open before the guest has read a word.
//
// DOMAIN_COLORS above stays as it is — one colour per kind of thing — for the
// admin picker, where the question is "what is this" rather than "what is it
// doing".
//
// Deliberately separate from the brand colours an admin configures
// (--color-primary and friends): those drive the interactive chrome — toggles,
// buttons, focus rings — while these say what the house is doing. Tying the two
// together would show "cooling" in red on a red-branded install.
//
// Every class is written out whole rather than assembled from parts, so
// Tailwind — which scans static/**/*.js, see tailwind.config.js — emits it.
const NEUTRAL_COLOR = { bg: 'bg-soot/[0.07] dark:bg-white/10', text: 'text-muted' };

const STATE_COLORS = {
  green: { bg: 'bg-emerald-500/15', text: 'text-emerald-600 dark:text-emerald-400' },
  red: { bg: 'bg-red-500/15', text: 'text-red-500 dark:text-red-400' },
  amber: { bg: 'bg-amber-500/15', text: 'text-amber-500' },
  orange: { bg: 'bg-orange-500/15', text: 'text-orange-500 dark:text-orange-400' },
  blue: { bg: 'bg-blue-500/15', text: 'text-blue-500 dark:text-blue-400' },
  sky: { bg: 'bg-sky-500/15', text: 'text-sky-500 dark:text-sky-400' },
  purple: { bg: 'bg-purple-500/15', text: 'text-purple-500 dark:text-purple-400' },
  teal: { bg: 'bg-teal-600/15', text: 'text-teal-600 dark:text-teal-400' },
  indigo: { bg: 'bg-indigo-500/15', text: 'text-indigo-500 dark:text-indigo-400' },
  pink: { bg: 'bg-pink-500/15', text: 'text-pink-500 dark:text-pink-400' },
  yellow: { bg: 'bg-yellow-500/15', text: 'text-yellow-600 dark:text-yellow-400' },
  violet: { bg: 'bg-violet-500/15', text: 'text-violet-500 dark:text-violet-400' },
  fuchsia: { bg: 'bg-fuchsia-500/15', text: 'text-fuchsia-500 dark:text-fuchsia-400' },
  cyan: { bg: 'bg-cyan-500/15', text: 'text-cyan-600 dark:text-cyan-400' },
  slate: { bg: 'bg-slate-500/15', text: 'text-slate-500 dark:text-slate-400' },
};

// Domains whose state is a reading or a stored value, not an on/off signal.
// There is no "idle" to grey out, so each keeps one colour whenever it has a
// value at all — the hue the admin picker already gives the domain.
const VALUE_DOMAIN_COLORS = {
  camera: 'indigo', counter: 'yellow', input_number: 'violet', input_text: 'slate',
  input_select: 'fuchsia', time: 'green', datetime: 'green', input_datetime: 'green',
  sensor: 'cyan',
};

// hvac_action says what the unit is doing this minute. The mode is the
// fallback, for a thermostat idling in a mode or one that reports no action at
// all — an idle heater still reads as a heater, not as "cooling".
const CLIMATE_ACTION_COLORS = { heating: 'red', preheating: 'red', cooling: 'blue', drying: 'amber', fan: 'teal' };
const CLIMATE_MODE_COLORS = { heat: 'red', cool: 'blue', heat_cool: 'amber', auto: 'amber', dry: 'amber', fan_only: 'teal' };

// A light's own colour, as Mushroom shows it, when it has one worth showing.
// Near-white — a white bulb, or a colour bulb in white mode — would all but
// vanish on a light card, so those keep the plain amber.
function lightRgb(state) {
  const attrs = state?.attributes || {};
  if (attrs.color_mode === 'color_temp') return null;
  const rgb = attrs.rgb_color;
  if (!Array.isArray(rgb) || rgb.length !== 3) return null;
  const channels = rgb.map(Number);
  if (!channels.every(Number.isFinite)) return null;
  const [r, g, b] = channels.map(c => Math.min(255, Math.max(0, Math.round(c))));
  if (Math.max(r, g, b) - Math.min(r, g, b) < 48) return null;
  return [r, g, b];
}

// { bg, text } — the class strings for the shape and its icon — plus `rgb`
// when the colour is the device's own and has to be applied as a style: a
// bulb's colour is not a class Tailwind could have emitted ahead of time.
function entityColor(domain, state) {
  const s = state?.state || 'unknown';
  if (s === 'unavailable') return NEUTRAL_COLOR;
  // A button that has never been pressed reports 'unknown'. That is its
  // resting state, not a fault, so buttons are let through.
  if (s === 'unknown' && domain !== 'button' && domain !== 'input_button') return NEUTRAL_COLOR;

  if (VALUE_DOMAIN_COLORS[domain]) return STATE_COLORS[VALUE_DOMAIN_COLORS[domain]];

  switch (domain) {
    case 'light': {
      if (s !== 'on') return NEUTRAL_COLOR;
      const rgb = lightRgb(state);
      return rgb ? { ...STATE_COLORS.amber, rgb } : STATE_COLORS.amber;
    }
    // A lock is the one entity whose colour carries a real warning, so it
    // stays coloured either way instead of going neutral when secured. The
    // states in between are amber: neither secured nor a fault yet.
    case 'lock':
      if (s === 'locked') return STATE_COLORS.green;
      if (s === 'locking' || s === 'unlocking' || s === 'opening') return STATE_COLORS.amber;
      return STATE_COLORS.red;
    // Read the same way as a lock: armed is secured. Disarmed is the everyday
    // state rather than a warning, so it goes quiet.
    case 'alarm_control_panel':
      if (s === 'triggered') return STATE_COLORS.red;
      if (s === 'arming' || s === 'pending' || s === 'disarming') return STATE_COLORS.amber;
      if (s.startsWith('armed')) return STATE_COLORS.green;
      return NEUTRAL_COLOR;
    // Stateless triggers: the state is a last-pressed timestamp, so there is
    // no idle to grey out.
    case 'button':
    case 'input_button':
      return STATE_COLORS.orange;
    case 'cover':
      return s === 'closed' ? NEUTRAL_COLOR : STATE_COLORS.sky;
    case 'climate': {
      if (s === 'off') return NEUTRAL_COLOR;
      const action = state?.attributes?.hvac_action;
      if (CLIMATE_ACTION_COLORS[action]) return STATE_COLORS[CLIMATE_ACTION_COLORS[action]];
      return STATE_COLORS[CLIMATE_MODE_COLORS[s]] || STATE_COLORS.blue;
    }
    case 'media_player':
      return s === 'off' || s === 'idle' || s === 'standby' ? NEUTRAL_COLOR : STATE_COLORS.purple;
    case 'switch':
    case 'input_boolean':
    case 'group':
    case 'fan':
      return s === 'on' ? STATE_COLORS.teal : NEUTRAL_COLOR;
    case 'timer':
      if (s === 'active') return STATE_COLORS.pink;
      return s === 'paused' ? STATE_COLORS.amber : NEUTRAL_COLOR;
    case 'schedule':
      return s === 'on' ? STATE_COLORS.green : NEUTRAL_COLOR;
    default:
      // binary_sensor, and any domain added later: on is the one state worth
      // drawing attention to.
      return s === 'on' ? STATE_COLORS.amber : NEUTRAL_COLOR;
  }
}

// The name a domain is shown under, in the page's language (tr() is in
// util.js). DOMAIN_LABELS itself stays English on purpose: the admin picker
// also groups by it — input_boolean joins switch because both say "Switches" —
// and that grouping must not change with the language.
function domainLabel(domain) {
  const key = `domain.${domain}`;
  if (hasTr(key)) return tr(key);
  return DOMAIN_LABELS[domain] || (domain.charAt(0).toUpperCase() + domain.slice(1));
}
