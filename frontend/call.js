/*
 * Patient "Start call" behavior -- a REAL patient<->bot LiveKit call.
 *
 * Flow on "Start call":
 *   1. POST /api/token with the chosen room + a patient identity. The backend
 *      mints a LiveKit access token with a room-join + publish grant.
 *   2. Connect to LIVEKIT_URL with the LiveKit browser client (imported from a
 *      CDN via ESM -- no bundler), enabling the microphone so the human patient
 *      talks to the SAME room the agent worker auto-joins, i.e. the intake bot.
 *   3. Surface connection state + a live mic level so the demo feels real.
 *
 * The backend returns the LiveKit `url` alongside the token (read from the same
 * .env the agent uses), so the browser never hardcodes the server URL.
 *
 * 🩹 Shortcuts: no auth (identity/room chosen client-side), the live human
 * doctor is stubbed (patient<->bot only; the doctor views the record later),
 * and the client is CDN-loaded. → Production: authenticated patients,
 * server-minted tokens bound to a verified record, role-gated access, and a
 * bundled/pinned client.
 */

import {
  Room,
  RoomEvent,
  createLocalAudioTrack
} from 'https://cdn.jsdelivr.net/npm/livekit-client/+esm';

// ES modules are strict by default, so no 'use strict' directive is needed.

function $(id) { return document.getElementById(id); }

function setStatus(msg, kind) {
  var el = $('status');
  el.textContent = msg || '';
  el.className = 'status' + (kind ? ' ' + kind : '');
}

function setConnState(label, kind) {
  $('conn-state').textContent = label;
  var dot = $('conn-dot');
  dot.className = 'dot' + (kind ? ' ' + kind : '');
}

// Module-scoped call state so "End call" can tear everything down.
var current = {
  room: null,
  micTrack: null,
  meter: null
};

/** Ask the backend to mint a patient token for `room`. Returns {token, url}. */
async function fetchToken(room, name) {
  // A demo-stable but unique-ish identity. → Production: a server-assigned
  // identity bound to the authenticated patient, never chosen client-side.
  var identity = 'patient-' + Math.random().toString(36).slice(2, 8);
  var resp = await fetch('/api/token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ room: room, identity: identity, name: name })
  });
  if (!resp.ok) {
    var detail = await resp.text();
    throw new Error('token request failed (HTTP ' + resp.status + '): ' + detail);
  }
  return resp.json();
}

/** Drive the mic-level meter from the published audio track (WebAudio). */
function startMicMeter(mediaStreamTrack) {
  try {
    var AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx || !mediaStreamTrack) return null;
    var ctx = new AudioCtx();
    var source = ctx.createMediaStreamSource(new MediaStream([mediaStreamTrack]));
    var analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    source.connect(analyser);
    var data = new Uint8Array(analyser.frequencyBinCount);
    var bar = $('mic-level');
    var raf;
    function tick() {
      analyser.getByteTimeDomainData(data);
      var peak = 0;
      for (var i = 0; i < data.length; i++) {
        var v = Math.abs(data[i] - 128);
        if (v > peak) peak = v;
      }
      var pct = Math.min(100, Math.round((peak / 128) * 160));
      bar.style.width = pct + '%';
      raf = requestAnimationFrame(tick);
    }
    tick();
    return function stop() {
      if (raf) cancelAnimationFrame(raf);
      try { ctx.close(); } catch (e) { /* ignore */ }
      bar.style.width = '0%';
    };
  } catch (e) {
    return null;
  }
}

async function startCall() {
  var room = $('room-input').value.trim() || 'intake-demo';
  var name = $('name-input').value.trim() || 'Demo Patient';

  $('start-call-btn').disabled = true;
  setConnState('Requesting access…', 'connecting');
  setStatus('Minting a patient token…');

  try {
    var creds = await fetchToken(room, name);
    if (!creds.url) {
      throw new Error(
        'backend did not return a LiveKit url -- set LIVEKIT_URL in .env'
      );
    }

    setStatus('Connecting to the room…');
    setConnState('Connecting…', 'connecting');

    var lkRoom = new Room();
    current.room = lkRoom;

    lkRoom.on(RoomEvent.Disconnected, function () {
      setConnState('Disconnected', '');
      teardownUi();
    });
    lkRoom.on(RoomEvent.ParticipantConnected, function (p) {
      setStatus('Participant joined: ' + (p.identity || '(unknown)'), 'ok');
    });

    await lkRoom.connect(creds.url, creds.token);

    // Publish the microphone so the patient can talk to the bot.
    var micTrack = await createLocalAudioTrack();
    current.micTrack = micTrack;
    await lkRoom.localParticipant.publishTrack(micTrack);

    $('mic-state').textContent = 'live';
    current.meter = startMicMeter(micTrack.mediaStreamTrack);

    setConnState('Connected to "' + room + '"', 'connected');
    setStatus('You are connected. Start speaking to the intake assistant.', 'ok');

    $('end-call-btn').hidden = false;
    $('end-call-btn').disabled = false;
  } catch (err) {
    setConnState('Connection failed', 'error');
    setStatus('Could not start the call: ' + err.message, 'error');
    $('start-call-btn').disabled = false;
    await teardownCall();
  }
}

async function teardownCall() {
  if (current.meter) { current.meter(); current.meter = null; }
  if (current.micTrack) {
    try { current.micTrack.stop(); } catch (e) { /* ignore */ }
    current.micTrack = null;
  }
  if (current.room) {
    try { await current.room.disconnect(); } catch (e) { /* ignore */ }
    current.room = null;
  }
}

function teardownUi() {
  $('mic-state').textContent = 'off';
  $('end-call-btn').hidden = true;
  $('start-call-btn').disabled = false;
}

async function endCall() {
  setStatus('Ending the call…');
  await teardownCall();
  setConnState('Not connected', '');
  teardownUi();
  setStatus('Call ended. The record will appear on the Records page once the ' +
    'enrichment pipeline finishes.', 'ok');
}

$('start-call-btn').addEventListener('click', startCall);
$('end-call-btn').addEventListener('click', endCall);
