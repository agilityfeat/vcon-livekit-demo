/*
 * Patient "Start call" behavior -- a REAL patient<->bot LiveKit call.
 *
 * Flow on "Start call":
 *   1. POST /api/token with the chosen room + a patient identity. The backend
 *      mints a LiveKit access token with a room-join + publish grant.
 *   2. Connect to LIVEKIT_URL with the LiveKit browser client (imported from a
 *      CDN via ESM -- no bundler), enabling the microphone so the human patient
 *      talks to the SAME room the agent worker auto-joins, i.e. the intake bot.
 *   3. Subscribe to and play the bot's REMOTE audio track so the patient
 *      actually HEARS the intake assistant: each subscribed audio track is
 *      attached to an <audio> element in a hidden sink (#remote-audio) so it
 *      routes to the output device, and detached again when it goes away.
 *   4. Surface connection state + a live mic level so the demo feels real.
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
  Track,
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
  meter: null,
  // Media elements created by attaching subscribed remote audio tracks. We
  // keep references so teardown can detach + remove them (otherwise a second
  // call would stack stale <audio> elements in the sink).
  remoteEls: []
};

/**
 * Play a subscribed REMOTE audio track by attaching it to an <audio> element
 * in the hidden #remote-audio sink. attach() returns a media element wired to
 * the track; appending it to the DOM is what makes it route to the output.
 */
function attachRemoteTrack(track) {
  if (track.kind !== Track.Kind.Audio) return;
  var sink = $('remote-audio');
  if (!sink) return;
  var el = track.attach();
  current.remoteEls.push(el);
  sink.appendChild(el);
}

/** Detach + remove the element(s) created for a track that went away. */
function detachRemoteTrack(track) {
  // detach() with no args detaches from all attached elements and returns them.
  var els = track.detach();
  if (!Array.isArray(els)) els = els ? [els] : [];
  for (var i = 0; i < els.length; i++) {
    var el = els[i];
    if (el && el.parentNode) el.parentNode.removeChild(el);
    var idx = current.remoteEls.indexOf(el);
    if (idx !== -1) current.remoteEls.splice(idx, 1);
  }
}

/** Remove any remaining attached remote-audio elements (used on teardown). */
function clearRemoteAudio() {
  for (var i = 0; i < current.remoteEls.length; i++) {
    var el = current.remoteEls[i];
    if (el && el.parentNode) el.parentNode.removeChild(el);
  }
  current.remoteEls = [];
  var sink = $('remote-audio');
  if (sink) sink.textContent = '';
}

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
  var room = $('room-input').value.trim() || 'intake';
  var name = $('name-input').value.trim() || 'Patient';

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

    // Play the bot's audio. Registered BEFORE connect() so a track that is
    // already published when we join is not missed.
    lkRoom.on(RoomEvent.TrackSubscribed, function (track) {
      attachRemoteTrack(track);
    });
    lkRoom.on(RoomEvent.TrackUnsubscribed, function (track) {
      detachRemoteTrack(track);
    });

    await lkRoom.connect(creds.url, creds.token);

    // The Start-call flow runs inside the button-click gesture, so autoplay is
    // already unblocked; startAudio() is belt-and-suspenders to resume playback
    // on client versions that gate audio behind an explicit user gesture.
    if (typeof lkRoom.startAudio === 'function') {
      try { await lkRoom.startAudio(); } catch (e) { /* ignore */ }
    }

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
  clearRemoteAudio();
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
  setStatus('Call ended. The record will appear on the doctor and analyst dashboards once the ' +
    'enrichment pipeline finishes.', 'ok');
}

$('start-call-btn').addEventListener('click', startCall);
$('end-call-btn').addEventListener('click', endCall);
