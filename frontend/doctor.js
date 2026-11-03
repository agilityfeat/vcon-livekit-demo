/*
 * Doctor dashboard behavior: fetch the REAL call records (GET /api/records --
 * the SAME existing list endpoint the analyst dashboard uses) and render each
 * one as a dashboard entry. Plain vanilla JS, no build, no module.
 *
 * Each record is a COMPLETED, signed intake call. Clicking an entry opens the
 * shared record detail page as the doctor's FULL, non-redacted clinical view
 * (record.html?run=<run_prefix>&view=doctor -- doctor is record.js's default
 * view anyway), and every entry carries an ENTER CALL link into the live call
 * screen (call.html?room=<room>) keyed on the record's room.
 *
 * 🩹 Demo shortcut: there is no doctor-specific backend here -- this reuses the
 * public records list, and the ENTER CALL links just open call.html with a
 * ?room= hint (call.html's inline prefill script copies it into #room-input
 * before the frozen call.js runs). The schedule form below is a UI-only
 * affordance: nothing is persisted and there is NO scheduling backend. →
 * Production: fetch the authenticated clinician's own patient roster/records
 * from a role-gated service and persist newly scheduled calls.
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
  var list = $('upcoming-call-list');
  if (!list) return;
  list.innerHTML = '';

  if (!records || records.length === 0) {
    var empty = document.createElement('li');
    empty.className = 'note';
    empty.textContent = 'No records yet. They appear here once an intake call has been processed.';
    list.appendChild(empty);
    return;
  }

  records.forEach(function (rec) {
    var li = document.createElement('li');
    li.className = 'upcoming-call';

    var info = document.createElement('div');
    info.className = 'upcoming-call-info';

    // Clickable title -> the doctor's full, non-redacted clinical detail view.
    var link = document.createElement('a');
    link.href =
      'record.html?run=' + encodeURIComponent(rec.run_prefix) + '&view=doctor';
    link.className = 'upcoming-call-patient';
    link.textContent = rec.room || rec.run_prefix;
    info.appendChild(link);

    var meta = document.createElement('div');
    meta.className = 'record-meta';
    meta.textContent =
      'run: ' + rec.run_prefix +
      (rec.session_start ? '  ·  session start: ' + rec.session_start : '') +
      '  ·  opens as: full clinical report';
    info.appendChild(meta);

    li.appendChild(info);

    var enter = document.createElement('a');
    enter.className = 'btn-primary enter-call-btn';
    // Link into the live call screen with a room hint. call.html has a small
    // inline prefill script (before the frozen call.js) that reads this ?room=
    // query and copies it into #room-input, so the doctor lands in this
    // record's room instead of the default 'intake'.
    enter.href = 'call.html?room=' + encodeURIComponent(rec.room || rec.run_prefix);
    enter.innerHTML = '<span aria-hidden="true">📞</span> Enter call';
    li.appendChild(enter);

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
      records.length === 0
        ? 'No records yet.'
        : records.length === 1
          ? '1 call record available.'
          : records.length + ' call records available.',
      'ok'
    );
  } catch (err) {
    setStatus('Could not load records: ' + err.message, 'error');
    renderRecords([]);
  }
}

function wireSchedule() {
  var btn = $('schedule-btn');
  var form = $('schedule-form');
  var note = $('schedule-note');
  if (!btn || !form) return;

  btn.addEventListener('click', function () {
    var open = !form.hidden;
    form.hidden = open;
    btn.setAttribute('aria-expanded', open ? 'false' : 'true');
  });

  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    var patient = ($('schedule-patient') && $('schedule-patient').value) || 'this patient';
    if (note) {
      note.textContent =
        '🩹 Demo only, nothing is saved. In production this would schedule a call with ' +
        patient + '.';
      note.className = 'status ok';
    }
  });
}

loadRecords();
wireSchedule();
