/*
 * Doctor dashboard behavior: render a list of UPCOMING intake calls and wire
 * the UI-only "schedule a call" affordance. Plain vanilla JS, no build, no
 * module.
 *
 * 🩹 Demo shortcut: the upcoming-calls list below is MOCK client-side data --
 * there is NO scheduling backend and NO persistence. The ENTER CALL links just
 * open the live call screen (call.html) with a ?room= hint, and the schedule
 * form only shows a non-persisting confirmation; nothing is saved anywhere.
 * -> Production: fetch the authenticated clinician's real upcoming calls from a
 * scheduling service and persist new ones.
 */

'use strict';

function $(id) { return document.getElementById(id); }

// Mock upcoming intake calls. Hardcoded on the client for the demo.
var UPCOMING_CALLS = [
  {
    patient: 'Jordan Patient',
    time: 'Today, 2:30 PM',
    reason: 'New patient intake',
    room: 'intake'
  },
  {
    patient: 'Alex Morgan',
    time: 'Today, 3:15 PM',
    reason: 'Medication review follow-up',
    room: 'intake-amorgan'
  },
  {
    patient: 'Sam Rivera',
    time: 'Tomorrow, 9:00 AM',
    reason: 'Post-op check-in',
    room: 'intake-srivera'
  }
];

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function renderUpcoming(calls) {
  var list = $('upcoming-call-list');
  if (!list) return;
  list.innerHTML = '';

  calls.forEach(function (call) {
    var li = document.createElement('li');
    li.className = 'upcoming-call';

    var info = document.createElement('div');
    info.className = 'upcoming-call-info';
    info.innerHTML =
      '<span class="upcoming-call-patient">' + escapeHtml(call.patient) + '</span>' +
      '<span class="upcoming-call-time">' + escapeHtml(call.time) + '</span>' +
      '<span class="upcoming-call-reason">' + escapeHtml(call.reason) + '</span>';
    li.appendChild(info);

    var enter = document.createElement('a');
    enter.className = 'btn-primary enter-call-btn';
    // Link into the live call screen with a room hint. call.html reads the
    // ?room= query below and pre-fills #room-input (see the inline handoff in
    // doctor.js is not needed -- the hint is purely a convenience).
    enter.href = 'call.html?room=' + encodeURIComponent(call.room);
    enter.innerHTML = '<span aria-hidden="true">📞</span> Enter call';
    li.appendChild(enter);

    list.appendChild(li);
  });
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

renderUpcoming(UPCOMING_CALLS);
wireSchedule();
