/*
 * Records list page: fetch GET /api/records and render each record, linking to
 * the detail page (record.html?run=<run_prefix>). Plain vanilla JS, no build.
 *
 * The backend returns records whether it is scanning S3 (BUCKET_NAME set) or
 * serving the single offline fixture (BUCKET_NAME unset); the shape is the
 * same, so this page does not care which mode is active.
 */

'use strict';

function $(id) { return document.getElementById(id); }

function setStatus(msg, kind) {
  var el = $('status');
  el.textContent = msg || '';
  el.className = 'status' + (kind ? ' ' + kind : '');
}

function renderRecords(records) {
  var list = $('record-list');
  var empty = $('empty-state');
  list.innerHTML = '';

  if (!records || records.length === 0) {
    empty.hidden = false;
    return;
  }
  empty.hidden = true;

  records.forEach(function (rec) {
    var li = document.createElement('li');

    var link = document.createElement('a');
    link.href = 'record.html?run=' + encodeURIComponent(rec.run_prefix);
    link.textContent = rec.room || rec.run_prefix;
    li.appendChild(link);

    var meta = document.createElement('div');
    meta.className = 'record-meta';
    meta.textContent =
      'run: ' + rec.run_prefix +
      (rec.session_start ? '  ·  session start: ' + rec.session_start : '');
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
        ? '1 record.'
        : records.length + ' records.',
      'ok'
    );
  } catch (err) {
    setStatus('Could not load records: ' + err.message, 'error');
    renderRecords([]);
  }
}

loadRecords();
