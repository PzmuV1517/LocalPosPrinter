// Public print page — no session; authorises each action with a temporary password.
(function () {
  var $ = function (id) { return document.getElementById(id); }

  // Which fields each format shows.
  var VIS = {
    plain: { text: 1 }, centered: { text: 1 }, boxed: { text: 1 },
    header_body: { title: 1, text: 1 }, banner: { title: 1, text: 1 },
    alert: { text: 1, alert_type: 1, service: 1 },
  };

  function applyVis() {
    var v = VIS[$('format').value] || {};
    var nodes = document.querySelectorAll('[data-field]');
    for (var i = 0; i < nodes.length; i++) {
      var k = nodes[i].getAttribute('data-field');
      nodes[i].classList.toggle('hidden', !v[k]);
    }
  }

  function payload() {
    var fmt = $('format').value, v = VIS[fmt] || {};
    var p = { format: fmt, password: $('password').value.trim(), print_mode: 'receipt' };
    if (v.title) p.title = $('title').value;
    if (v.text) p.text = $('text').value;
    if (v.alert_type) { p.alert_type = $('alert_type').value; p.service = $('service').value; p.sent_at = Math.floor(Date.now() / 1000); }
    return p;
  }

  function setResult(msg, ok) {
    var el = $('result'); el.textContent = msg; el.className = ok ? 'ok' : 'bad';
  }

  function doPreview() {
    if (!$('password').value.trim()) { setResult('Enter your password first', false); return; }
    fetch('/preview', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload()) })
      .then(function (r) {
        if (!r.ok) { return r.json().catch(function () { return {}; }).then(function (e) { setResult(e.error || ('Preview failed (' + r.status + ')'), false); }); }
        return r.blob().then(function (b) { $('preview').src = URL.createObjectURL(b); setResult('', true); });
      })
      .catch(function () { setResult('Network error', false); });
  }

  function doPrint() {
    if (!$('password').value.trim()) { setResult('Enter your password first', false); return; }
    fetch('/print', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload()) })
      .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
      .then(function (res) {
        if (res.ok) { setResult((res.d.message || 'Sent') + (res.d.usage_message ? ' — ' + res.d.usage_message : ''), true); }
        else { setResult(res.d.error || 'Print failed', false); }
      })
      .catch(function () { setResult('Network error', false); });
  }

  $('format').addEventListener('change', applyVis);
  $('previewBtn').addEventListener('click', doPreview);
  $('printBtn').addEventListener('click', doPrint);
  applyVis();
})();
