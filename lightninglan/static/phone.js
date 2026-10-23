/* LightningLAN on a phone. Plain ES2017, no build step.
 *
 * Served by a computer running LightningLAN to a phone on the same Wi-Fi, after the phone
 * scanned the QR code in that computer's web UI (the key from it lives in a cookie). Every piece
 * of text that came from a file name or a peer is inserted with textContent, never as HTML.
 */
(function () {
  'use strict';

  var CLIENT_HEADER = { 'X-LightningLAN-Client': 'phone' };
  var THEMES = ['auto', 'light', 'dark', 'synthwave', 'bliss', 'deepsea'];  // as on the computer's page
  var THEME_NAMES = { auto: 'automatic', light: 'light', dark: 'dark', synthwave: 'Synthwave', bliss: 'Bliss',
    deepsea: 'Deep Sea' };
  var JUNK = { 'thumbs.db': true, 'ehthumbs.db': true, 'desktop.ini': true, '.ds_store': true };
  var TEMP_SUFFIXES = ['.part', '.partial', '.crdownload', '.tmp'];

  var state = {
    status: null,
    paired: null,  // unknown until the first answer
    files: [],
    filesEtag: null,
    filter: '',
    sort: 'name',
    session: null,
    failures: 0
  };

  // ----- small helpers --------------------------------------------------------------------

  function $(id) { return document.getElementById(id); }

  function el(tag, className, text) {
    var e = document.createElement(tag);
    if (className) e.className = className;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }

  function formatSize(n) {
    if (n < 1024) return n + ' B';
    var units = ['KB', 'MB', 'GB', 'TB'];
    var i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
    return (n >= 100 ? n.toFixed(0) : n.toFixed(1)) + ' ' + units[i];
  }

  function formatDay(seconds) {
    if (!seconds) return '';
    try {
      return new Date(seconds * 1000).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
    } catch (e) {
      return new Date(seconds * 1000).toLocaleDateString();
    }
  }

  function plural(n, word) { return n + ' ' + word + (n === 1 ? '' : 's'); }

  function hue(text) {
    var h = 0;
    for (var i = 0; i < text.length; i++) h = (h * 31 + text.charCodeAt(i)) % 360;
    return h;
  }

  function errorMessage(data, status) {
    if (data && typeof data.detail === 'string') return data.detail;
    if (data && data.error) return data.error;
    return 'HTTP ' + status;
  }

  function NotPaired() { this.message = 'not paired'; }

  async function api(method, path) {
    var opts = { method: method, headers: {}, cache: 'no-store', credentials: 'same-origin' };
    if (method !== 'GET') Object.assign(opts.headers, CLIENT_HEADER);
    var r = await fetch(path, opts);
    if (r.status === 401) throw new NotPaired();
    var data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    if (!r.ok) throw new Error(errorMessage(data, r.status));
    return data;
  }

  function toast(message, kind) {
    var t = el('div', 'toast' + (kind ? ' ' + kind : ''), message);
    t.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    $('toasts').appendChild(t);
    setTimeout(function () { t.remove(); }, kind === 'error' ? 9000 : 4500);
  }

  // ----- theme ----------------------------------------------------------------------------

  function storedTheme() {
    var mode = 'auto';
    try { mode = localStorage.getItem('lightninglan-theme') || 'auto'; } catch (e) { /* private mode */ }
    return THEMES.indexOf(mode) === -1 ? 'auto' : mode;
  }

  function applyTheme(mode) {
    if (mode === 'auto') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', mode);
    var label = 'Theme: ' + THEME_NAMES[mode];
    $('theme-toggle').title = label;
    $('theme-toggle').setAttribute('aria-label', label + ' (tap to change)');
  }

  function cycleTheme() {
    var next = THEMES[(THEMES.indexOf(storedTheme()) + 1) % THEMES.length];
    try { localStorage.setItem('lightninglan-theme', next); } catch (e) { /* private mode */ }
    applyTheme(next);
    toast('Theme: ' + THEME_NAMES[next]);
  }

  // ----- connecting: paired, or how to pair ---------------------------------------------------

  function showPairing() {
    state.paired = false;
    if (state.session) { state.session.close(); state.session = null; }
    $('phone-ui').hidden = true;
    $('peer-pill').hidden = true;
    $('stopped-banner').hidden = true;
    $('pair').hidden = false;
    if (window.location.hash === '#expired') {
      $('pair-title').textContent = 'That QR code is out of date';
      $('pair-text').textContent = 'Scan the new one: on your computer, open LightningLAN and click the phone ' +
        'button at the top.';
    }
  }

  function showPaired() {
    if (state.paired) return;
    state.paired = true;
    $('pair').hidden = true;
    $('phone-ui').hidden = false;
    $('peer-pill').hidden = false;
    if (window.location.hash) history.replaceState(null, '', window.location.pathname);
    holdSession();
    refreshFiles().catch(function () {});
  }

  async function refreshStatus() {
    var s;
    try {
      s = await api('GET', '/phone/api/status');
    } catch (e) {
      if (e instanceof NotPaired) { showPairing(); return; }
      throw e;
    }
    state.status = s;
    showPaired();
    $('pc-name').textContent = s.name;
    $('send-name').textContent = s.name;
    $('stopped-name').textContent = s.name;
    document.title = 'LightningLAN — ' + s.name;
    $('stopped-banner').hidden = s.online;
    var n = s.peers_online;
    $('peer-count').textContent = !s.online ? 'Stopped' : n === 0 ? 'No other peers' : plural(n, 'peer') + ' online';
    $('peer-pill').className = 'pill ' + (s.online ? 'pill-on' : 'pill-off');
    renderFiles();
  }

  // While this page is showing it holds a session, which keeps LightningLAN sharing on the
  // computer even if its own page there was closed. It also says at once when Stop or Start is pressed.
  function holdSession() {
    if (state.session) return;
    state.session = new EventSource('/phone/api/session');
    state.session.onmessage = function () { refreshStatus().catch(function () {}); };
  }

  // ----- ThunderCLOUD -------------------------------------------------------------------------

  async function refreshFiles() {
    if (!state.paired) return;
    var headers = {};
    if (state.filesEtag) headers['If-None-Match'] = state.filesEtag;
    var r = await fetch('/phone/api/files', { headers: headers, cache: 'no-store', credentials: 'same-origin' });
    if (r.status === 304) return;
    if (r.status === 401) { showPairing(); return; }
    if (!r.ok) throw new Error('HTTP ' + r.status);
    var data = await r.json();
    state.filesEtag = r.headers.get('ETag');
    state.files = data.files;
    renderFiles();
  }

  var collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

  function sortedFiles() {
    var filter = state.filter.trim().toLowerCase();
    var items = state.files.filter(function (f) {
      return !filter || f.name.toLowerCase().indexOf(filter) !== -1 ||
        f.aliases.some(function (a) { return a.toLowerCase().indexOf(filter) !== -1; });
    });
    items.sort(function (a, b) {
      var c = 0;
      if (state.sort === 'new') c = b.mtime - a.mtime;
      else if (state.sort === 'size') c = b.size - a.size;
      return c || collator.compare(a.name, b.name);
    });
    return items;
  }

  function extension(name) {
    var base = name.slice(name.lastIndexOf('/') + 1);
    var dot = base.lastIndexOf('.');
    var ext = dot > 0 ? base.slice(dot + 1) : '';
    return ext && ext.length <= 4 ? ext.toUpperCase() : 'FILE';
  }

  function owners(f) {
    var pc = state.status ? state.status.name : 'the computer';
    return f.sources.map(function (s) { return s.self ? pc : s.name; }).join(', ');
  }

  function fileItem(f, online) {
    var li = el('li', 'pf');
    var ext = extension(f.name);
    var badge = el('span', 'pf-ext', ext);
    badge.style.background = 'hsl(' + hue(ext) + ', 60%, 44%)';
    li.appendChild(badge);

    var body = el('div', 'pf-body');
    var name = el('div', 'pf-name');
    var slash = f.name.lastIndexOf('/');
    if (slash !== -1) name.appendChild(el('span', 'folder', f.name.slice(0, slash + 1)));
    name.appendChild(document.createTextNode(f.name.slice(slash + 1)));
    body.appendChild(name);
    body.appendChild(el('div', 'pf-meta', formatSize(f.size) + ' · ' + formatDay(f.mtime) + ' · on ' + owners(f)));
    li.appendChild(body);

    var get = el('a', 'btn pf-get', 'Get');
    get.href = '/phone/api/file/' + encodeURIComponent(f.id);
    get.setAttribute('download', f.name.slice(slash + 1));
    get.setAttribute('aria-label', 'Download ' + f.name.slice(slash + 1));
    if (!online) {
      get.classList.add('disabled');
      get.setAttribute('aria-disabled', 'true');
    }
    get.addEventListener('click', function (e) {
      if (!state.status || !state.status.online) {
        e.preventDefault();
        toast('LightningLAN is stopped on the computer. Start it there first.', 'error');
        return;
      }
      toast('Downloading ' + f.name.slice(slash + 1) + '… Your browser shows how it is going.');
    });
    li.appendChild(get);
    return li;
  }

  function renderFiles() {
    if (!state.paired) return;
    var online = !!(state.status && state.status.online);
    var items = sortedFiles();
    var list = $('file-list');
    var rows = document.createDocumentFragment();
    items.forEach(function (f) { rows.appendChild(fileItem(f, online)); });
    list.textContent = '';
    list.appendChild(rows);

    var empty = $('files-empty');
    if (!state.files.length) {
      empty.textContent = 'Nothing shared yet. Send a file from this phone, or wait for computers running ' +
        'LightningLAN to share some.';
    } else if (!items.length) {
      empty.textContent = 'No files match “' + state.filter.trim() + '”.';
    }
    empty.hidden = items.length > 0;

    var total = state.files.reduce(function (sum, f) { return sum + f.size; }, 0);
    var summary = plural(state.files.length, 'file') + ' · ' + formatSize(total);
    if (items.length !== state.files.length) summary = items.length + ' of ' + summary;
    $('file-summary').textContent = state.files.length ? summary : '';
  }

  // ----- sending files from the phone -------------------------------------------------------

  var uploadQueue = [];
  var uploadsRunning = 0;

  function isShareable(name) {
    var lower = name.toLowerCase();
    if (name.charAt(0) === '.' || name.slice(0, 2) === '~$' || JUNK[lower]) return false;
    return !TEMP_SUFFIXES.some(function (s) { return lower.slice(-s.length) === s; });
  }

  function queueUploads(files) {
    var skipped = 0;
    files.forEach(function (file) {
      if (!isShareable(file.name)) { skipped++; return; }
      var li = el('li');
      var status = el('span', 'u-state', 'Waiting…');
      var bar = el('progress');
      bar.max = 100;
      bar.value = 0;
      li.appendChild(el('span', 'u-name', file.name));
      li.appendChild(status);
      li.appendChild(bar);
      $('uploads').appendChild(li);
      uploadQueue.push({ file: file, li: li, status: status, bar: bar });
    });
    if (skipped) toast('Skipped ' + plural(skipped, 'hidden or temporary file') + '.');
    pumpUploads();
  }

  function pumpUploads() {
    while (uploadsRunning < 2 && uploadQueue.length) sendUpload(uploadQueue.shift());
  }

  function finishUpload(job, ok, message) {
    uploadsRunning--;
    job.li.className = ok ? 'done' : 'error';
    job.status.textContent = message;
    if (ok) {
      job.bar.value = 100;
      setTimeout(function () { job.li.remove(); }, 6000);
      state.filesEtag = null;
      setTimeout(function () { refreshFiles().catch(function () {}); }, 800);
    } else {
      var dismiss = el('button', 'text-btn', 'Dismiss');
      dismiss.type = 'button';
      dismiss.addEventListener('click', function () { job.li.remove(); });
      job.li.appendChild(dismiss);
      toast('Could not send ' + job.file.name + ': ' + message, 'error');
    }
    pumpUploads();
  }

  function sendUpload(job) {
    uploadsRunning++;
    var xhr = new XMLHttpRequest();
    var url = '/phone/api/upload?name=' + encodeURIComponent(job.file.name);
    if (job.file.lastModified) url += '&mtime=' + job.file.lastModified;
    xhr.open('POST', url);
    xhr.setRequestHeader('X-LightningLAN-Client', 'phone');
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    job.status.textContent = 'Sending…';
    xhr.upload.onprogress = function (e) {
      if (!e.lengthComputable) return;
      var p = Math.floor(e.loaded / e.total * 100);
      job.bar.value = p;
      job.status.textContent = p + '% of ' + formatSize(e.total);
    };
    xhr.onload = function () {
      var data = null;
      try { data = JSON.parse(xhr.responseText); } catch (e) { data = null; }
      if (xhr.status === 201 && data) finishUpload(job, true, data.renamed ? 'Sent as ' + data.name : 'Sent ✓');
      else if (xhr.status === 401) { finishUpload(job, false, 'This phone is no longer connected.'); showPairing(); }
      else finishUpload(job, false, errorMessage(data, xhr.status));
    };
    xhr.onerror = function () {
      finishUpload(job, false, 'The connection to the computer was lost. Keep this page open while files send.');
    };
    xhr.send(job.file);
  }

  // ----- connection state and polling -----------------------------------------------------

  function connectionOk() {
    state.failures = 0;
    $('offline-banner').hidden = true;
  }

  function connectionFailed() {
    state.failures++;
    if (state.failures >= 2) $('offline-banner').hidden = false;
  }

  var ticks = [];

  function every(ms, fn) {
    var running = false;
    async function tick() {
      if (running || document.hidden) return;  // a phone in a pocket doesn't need to poll
      running = true;
      try {
        await fn();
        connectionOk();
      } catch (e) {
        connectionFailed(e);
      } finally {
        running = false;
      }
    }
    ticks.push(tick);
    tick();
    return setInterval(tick, ms);
  }

  // ----- wiring ---------------------------------------------------------------------------

  function init() {
    applyTheme(storedTheme());
    $('theme-toggle').addEventListener('click', cycleTheme);
    $('send').addEventListener('click', function () { $('file-input').click(); });
    $('file-input').addEventListener('change', function () {
      queueUploads(Array.prototype.slice.call($('file-input').files));
      $('file-input').value = '';
    });
    $('filter').addEventListener('input', function (e) {
      state.filter = e.target.value;
      renderFiles();
    });
    $('sort').addEventListener('change', function (e) {
      state.sort = e.target.value;
      renderFiles();
    });
    document.addEventListener('visibilitychange', function () {
      if (!document.hidden) ticks.forEach(function (tick) { tick(); });  // back from the pocket: catch up now
    });
    window.addEventListener('pageshow', function (e) {
      if (e.persisted) window.location.reload();  // back from the browser's page cache: start afresh
    });

    every(5000, refreshStatus);
    every(3000, refreshFiles);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
