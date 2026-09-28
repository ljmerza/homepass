"""The dashboard side of remember-PIN and links without PIN.

Same harness as test_picker_js.py: the real dashboard is rendered, its inline
script is run in Node against a stub DOM, and the shipped functions are called
directly. Skipped when node is not installed.
"""
import pytest

from tests.test_picker_js import _run, node

pytestmark = pytest.mark.skipif(node is None, reason="node is not installed")

# Records every fetch as {url, method, body} and answers from a queue. An
# unqueued call — the list refreshes a save kicks off — gets an empty list.
_FETCH_RECORDER = """
const calls = [];
const replies = [];
globalThis.fetch = async (url, opts = {}) => {
  calls.push({ url, method: opts.method || 'GET', body: opts.body ? JSON.parse(opts.body) : null });
  const r = replies.shift() || { ok: true, body: [] };
  return { ok: r.ok, json: async () => r.body };
};
const copied = [];
navigator.clipboard = { writeText: async t => { copied.push(t); } };
window.isSecureContext = true;
"""


async def test_create_sends_remember_pin_from_the_checkbox(client, admin_session, mock_ha_client):
    out = await _run(client, admin_session, _FETCH_RECORDER + """
    const sent = [];
    for (const checked of [true, false]) {
      openCreateModal();
      document.getElementById('f-label').value = 'Guest';
      document.getElementById('f-remember-pin').checked = checked;
      createPicker.selected.add('light.a');
      replies.push({ ok: true, body: { slug: 'abc' } });
      await submitCreate();
      sent.push(calls.filter(c => c.url.endsWith('/admin/tokens') && c.method === 'POST').pop().body.remember_pin);
    }
    console.log(JSON.stringify({ sent }));
    """)
    assert out["sent"] == [True, False]


async def test_new_token_form_defaults_to_remembering(client, admin_session, mock_ha_client):
    out = await _run(client, admin_session, """
    document.getElementById('f-remember-pin').checked = false;
    openCreateModal();
    console.log(JSON.stringify({ checked: document.getElementById('f-remember-pin').checked }));
    """)
    assert out["checked"] is True


async def test_duplicate_carries_remember_pin(client, admin_session, mock_ha_client):
    out = await _run(client, admin_session, _FETCH_RECORDER + """
    allEntities = [{ entity_id: 'light.a', friendly_name: 'A', domain: 'light', state: 'on' }];
    replies.push({ ok: true, body: {
      id: 't1', label: 'Stay', entity_ids: ['light.a'], ip_allowlist: null,
      created_at: 1700000000, starts_at: null, expires_at: 1700086400,
      max_uses: null, access_windows: null, remember_pin: false,
    } });
    await duplicateToken('t1');
    console.log(JSON.stringify({ checked: document.getElementById('f-remember-pin').checked }));
    """)
    assert out["checked"] is False


async def test_copy_link_without_pin_mints_a_link_and_copies_it(
    client, admin_session, mock_ha_client
):
    out = await _run(client, admin_session, _FETCH_RECORDER + """
    tokens = [{ id: 't1', slug: 'abc123', has_pin: true }];
    replies.push({ ok: true, body: { id: 'c1', label: null, code: 'Zz_-0123456789abcdefghijklmnopqr' } });
    await copyLinkWithoutPin('t1');
    await new Promise(r => r());
    console.log(JSON.stringify({ calls, copied }));
    """)
    assert out["calls"][0]["url"].endswith("/admin/tokens/t1/access-codes")
    assert out["calls"][0]["method"] == "POST"
    assert out["copied"] == ["http://testserver/g/abc123?c=Zz_-0123456789abcdefghijklmnopqr"]


async def test_failed_mint_copies_nothing(client, admin_session, mock_ha_client):
    out = await _run(client, admin_session, _FETCH_RECORDER + """
    tokens = [{ id: 't1', slug: 'abc123', has_pin: true }];
    replies.push({ ok: false, body: { detail: 'too many' } });
    await copyLinkWithoutPin('t1');
    console.log(JSON.stringify({ copied }));
    """)
    assert out["copied"] == []


async def test_access_link_list_escapes_labels(client, admin_session, mock_ha_client):
    out = await _run(client, admin_session, """
    renderAccessLinks([{ id: 'c1', label: '<img src=x onerror=alert(1)>', created_at: 1, last_used_at: null }]);
    console.log(JSON.stringify({ html: document.getElementById('pin-links-list').innerHTML }));
    """)
    assert "<img" not in out["html"]
    assert "&lt;img" in out["html"]
    assert "Never used" in out["html"]


async def test_remember_toggle_is_reverted_when_the_save_fails(
    client, admin_session, mock_ha_client
):
    out = await _run(client, admin_session, _FETCH_RECORDER + """
    pinTokenId = 't1';
    const box = document.getElementById('pin-remember-field');
    box.checked = false;
    replies.push({ ok: false, body: {} });
    await saveRememberPin(false);
    console.log(JSON.stringify({ call: calls[0], checked: box.checked }));
    """)
    assert out["call"]["url"].endswith("/admin/tokens/t1/remember-pin")
    assert out["call"]["method"] == "PATCH"
    assert out["call"]["body"] == {"remember_pin": False}
    assert out["checked"] is True
