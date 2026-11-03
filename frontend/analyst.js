/*
 * Analyst dashboard behavior: fetch GET /api/records (the SAME existing list
 * endpoint the records page uses) and render each record linking into the
 * record detail page as the de-identified analyst report
 * (record.html?run=<run_prefix>&view=analyst). Plain vanilla JS, no build.
 *
 * 🩹 Demo shortcut: there is no analyst-specific backend here -- this reuses the
 * public records list, and the de-identification happens in the record detail's
 * analyst (redacted) view. "analyst" is framing, not an enforced authorization
 * boundary. -> Production: a server that only ever returns PHI-masked records
 * to an authenticated analyst, with audit logging.
 */

'use strict';

function $(id) { return document.getElementById(id); }

function setStatus(msg, kind) {
  var el = $('status');
  if (!el) return;
  el.textContent = msg || '';
  el.className = 'status' + (kind ? ' ' + kind : '');
}

function renderRecords(records) {
  var list = $('record-list');
  var empty = $('empty-state');
  if (!list) return;
  list.innerHTML = '';

  if (!records || records.length === 0) {
    if (empty) empty.hidden = false;
    return;
  }
  if (empty) empty.hidden = true;

  records.forEach(function (rec) {
    var li = document.createElement('li');

    var link = document.createElement('a');
    // Open the record detail framed as the de-identified analyst report.
    link.href =
      'record.html?run=' + encodeURIComponent(rec.run_prefix) + '&view=analyst';
    link.textContent = rec.room || rec.run_prefix;
    li.appendChild(link);

    var meta = document.createElement('div');
    meta.className = 'record-meta';
    meta.textContent =
      'run: ' + rec.run_prefix +
      (rec.session_start ? '  ·  session start: ' + rec.session_start : '') +
      '  ·  opens as: de-identified analyst report';
    li.appendChild(meta);

    list.appendChild(li);
  });
}

async function loadRecords() {
  setStatus('Loading records…');
  try {
    var resp = await fetch('/api/records');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    var data = await resp.json();
    var records = (data && data.records) || [];
    renderRecords(records);
    setStatus(
      records.length === 1
        ? '1 record available for review.'
        : records.length + ' records available for review.',
      'ok'
    );
  } catch (err) {
    setStatus('Could not load records: ' + err.message, 'error');
    renderRecords([]);
  }
}

loadRecords();
