/*
 * Tiny app-shell script shared by every page: highlight the active nav link.
 *
 * No framework, no build step. Each page's <body> nav links carry a
 * `data-page` attribute; the matching one is marked `.active`. Kept trivially
 * small on purpose -- the real per-page behavior lives in call.js / records.js
 * / record.js.
 */

'use strict';

(function highlightActiveNav() {
  // Derive the current page key from the filename (default to "index").
  var path = window.location.pathname;
  var file = path.substring(path.lastIndexOf('/') + 1) || 'index.html';
  var page = file.replace(/\.html$/, '') || 'index';
  // The doctor/analyst sign-in shells and their dashboards share one nav entry
  // each (Doctor -> doctor-signin, Analyst -> analyst-signin), so map the
  // dashboard page keys onto their sign-in nav link to keep it highlighted.
  var navAlias = { doctor: 'doctor-signin', analyst: 'analyst-signin' };
  if (navAlias[page]) page = navAlias[page];
  var links = document.querySelectorAll('.app-nav a[data-page]');
  for (var i = 0; i < links.length; i++) {
    if (links[i].getAttribute('data-page') === page) {
      links[i].classList.add('active');
    }
  }
})();
