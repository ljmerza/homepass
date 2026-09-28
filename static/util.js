// ── Shared Utilities ──────────────────────────────────────────────

/** HTML-escape a string for safe innerHTML insertion. */
function esc(str) {
  return String(str ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

/** Convert entity_id dots to hyphens for use in DOM IDs. */
function cssId(s) { return s.replace(/\./g, '-'); }

// ── Focus Trap (for modal dialogs) ───────────────────────────────
let _previouslyFocused = null;

const _FOCUSABLE_SELECTOR =
  'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

function trapFocus(modal) {
  _previouslyFocused = document.activeElement;
  const focusable = modal.querySelectorAll(_FOCUSABLE_SELECTOR);
  if (!focusable.length) return;
  focusable[0].focus();
  modal.addEventListener('keydown', _handleTrapKeydown);
}

function _handleTrapKeydown(e) {
  if (e.key !== 'Tab') return;
  const focusable = this.querySelectorAll(_FOCUSABLE_SELECTOR);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault();
    last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault();
    first.focus();
  }
}

function releaseFocus() {
  if (_previouslyFocused && typeof _previouslyFocused.focus === 'function') {
    _previouslyFocused.focus();
  }
  _previouslyFocused = null;
}

// ── i18n ─────────────────────────────────────────────────────────
// Each page declares `const I18N = { lang, strings }` as the first line of its
// own inline script (see app/i18n.py), with English already merged under the
// page's language. These read it at call time, never at load: this file runs
// before that script does.
//
// Named tr/trn rather than t: `t` is the dashboard's name for a token in every
// callback that walks the token list, and would shadow a global of that name.

function _i18n() {
  return (typeof I18N !== 'undefined' && I18N) || { lang: 'en', strings: {} };
}

function i18nLang() { return _i18n().lang || 'en'; }

function hasTr(key) {
  return Object.prototype.hasOwnProperty.call(_i18n().strings || {}, key);
}

// {name} placeholders only; one with no matching param is left as written.
function _fillPlaceholders(text, params, escapeValues) {
  return String(text).replace(/\{(\w+)\}/g, (m, name) => {
    if (!params || !Object.prototype.hasOwnProperty.call(params, name)) return m;
    return escapeValues ? esc(params[name]) : String(params[name]);
  });
}

/** Plain-text string for `key`. The caller escapes it like any other text. A
 *  key the catalogue lacks comes back as itself, so a typo shows on screen. */
function tr(key, params) {
  return _fillPlaceholders(hasTr(key) ? _i18n().strings[key] : key, params, false);
}

/** For the few `_html` strings that carry markup. The catalogue text is
 *  trusted and inserted as is; every value in `params` is escaped. `rawParams`
 *  is for markup the caller built itself (an icon), and is inserted unescaped. */
function trHtml(key, params, rawParams) {
  const text = _fillPlaceholders(hasTr(key) ? _i18n().strings[key] : esc(key), params, true);
  return _fillPlaceholders(text, rawParams, false);
}

// CLDR category for n in the page's language: "one", "few", "many"... Polish
// and Irish need more than English's two, so a counted string is a family of
// keys (`key.one`, `key.few`, `key.other`) and this picks the member.
function _pluralCategory(n) {
  try { return new Intl.PluralRules(i18nLang()).select(n); }
  catch { return n === 1 ? 'one' : 'other'; }
}

/** A counted string: `key.<category>`, falling back to `key.other`. `{n}` is
 *  filled with the count. */
function trn(key, n, params) {
  const exact = `${key}.${_pluralCategory(n)}`;
  return tr(hasTr(exact) ? exact : `${key}.other`, { n, ...params });
}

/** Locale for dates and numbers: the page's language, keeping the browser's
 *  own region when it speaks that language — an en-GB browser keeps day-first
 *  dates, and a pinned Spanish dashboard on an English browser gets Spanish. */
function uiLocale() {
  const lang = i18nLang();
  const nav = (typeof navigator !== 'undefined' && navigator.language) || '';
  return nav.toLowerCase().split('-')[0] === lang ? nav : lang;
}
