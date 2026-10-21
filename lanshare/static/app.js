/* LanShare web UI. Plain ES2017, no build step.
 *
 * Every piece of text that came from a file name or a peer is inserted with textContent,
 * never as HTML. State-changing requests carry the X-LanShare-Client header the server
 * requires (it blocks cross-site requests from other web pages).
 */
(function () {
  'use strict';

  var CLIENT_HEADER = { 'X-LanShare-Client': 'ui' };
  var ACTIVE = { queued: true, downloading: true, verifying: true };
  var JUNK = { 'thumbs.db': true, 'ehthumbs.db': true, 'desktop.ini': true, '.ds_store': true };
  var TEMP_SUFFIXES = ['.part', '.partial', '.crdownload', '.tmp'];
  var THEMES = ['auto', 'light', 'dark'];

  var state = {
    status: null,
    files: [],
    filesEtag: null,
    peers: [],
    ignored: [],
    downloads: [],
    sort: { key: 'name', dir: 1 },
    filter: '',
    knownPeers: null,
    downloadStatus: {},
    stopped: false,
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

  function formatDate(seconds) {
    if (!seconds) return '';
    try {
      return new Date(seconds * 1000).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
    } catch (e) {
      return new Date(seconds * 1000).toLocaleString();
    }
  }

  function plural(n, word) { return n + ' ' + word + (n === 1 ? '' : 's'); }

  function percent(d) {
    return Math.floor((d.size ? d.received / d.size : (d.status === 'completed' ? 1 : 0)) * 100);
  }

  function hue(text) {
    var h = 0;
    for (var i = 0; i < text.length; i++) h = (h * 31 + text.charCodeAt(i)) % 360;
    return h;
  }

  function initials(name) {
    var words = name.trim().split(/\s+/).filter(Boolean);
    var letters = words.length > 1 ? words[0][0] + words[1][0] : (words[0] || '?').slice(0, 2);
    return letters.toUpperCase();
  }

  function errorMessage(data, status) {
    if (data && typeof data.detail === 'string') return data.detail;
    if (data && Array.isArray(data.detail) && data.detail.length) return data.detail[0].msg || 'Invalid request';
    if (data && data.error) return data.error;
    return 'HTTP ' + status;
  }

  async function api(method, path, body) {
    var opts = { method: method, headers: {}, cache: 'no-store' };
    if (method !== 'GET') Object.assign(opts.headers, CLIENT_HEADER);
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    var r = await fetch(path, opts);
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
    try { return localStorage.getItem('lanshare-theme') || 'auto'; } catch (e) { return 'auto'; }
  }

  function applyTheme(mode) {
    if (mode === 'auto') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', mode);
    var label = 'Theme: ' + (mode === 'auto' ? 'automatic' : mode);
    $('theme-toggle').title = label;
    $('theme-toggle').setAttribute('aria-label', label + ' (click to change)');
  }

  function cycleTheme() {
    var next = THEMES[(THEMES.indexOf(storedTheme()) + 1) % THEMES.length];
    try { localStorage.setItem('lanshare-theme', next); } catch (e) { /* private mode */ }
    applyTheme(next);
  }

  // ----- header and status ----------------------------------------------------------------

  async function refreshStatus() {
    state.status = await api('GET', '/api/status');
    var s = state.status;
    $('self-name').textContent = s.name;
    $('self-name').title = s.name;
    document.title = 'LanShare — ' + s.name;
    $('lock').hidden = !s.passphrase_enabled;
    $('share-dir').textContent = s.share_dir;
    $('share-dir').title = s.share_dir;
    $('downloads-dir').textContent = s.downloads_dir;
    $('downloads-dir').title = s.downloads_dir;
    var summary = plural(s.shared_files, 'file') + ' shared';
    if (s.indexing_pending) summary += ' · indexing ' + plural(s.indexing_pending, 'file') + '…';
    $('share-summary').textContent = summary;
  }

  function renderPeerCount() {
    var n = state.peers.length;
    $('peer-count').textContent = n === 0 ? 'No peers online' : plural(n, 'peer') + ' online';
    $('peer-pill').className = 'pill ' + (n ? 'pill-on' : 'pill-off');
  }

  // ----- peers ----------------------------------------------------------------------------

  async function refreshPeers() {
    var data = await api('GET', '/api/peers');
    var previous = state.knownPeers;
    var now = {};
    data.peers.forEach(function (p) { now[p.peer_id] = p.name; });
    if (previous) {
      Object.keys(now).forEach(function (id) { if (!previous[id]) toast(now[id] + ' joined'); });
      Object.keys(previous).forEach(function (id) { if (!now[id]) toast(previous[id] + ' went offline', 'error'); });
    }
    state.knownPeers = now;
    state.peers = data.peers;
    state.ignored = data.ignored;
    renderPeers();
    renderPeerCount();
  }

  function renderPeers() {
    var list = $('peers');
    list.textContent = '';
    state.peers.forEach(function (p) {
      var li = el('li', 'peer');
      var avatar = el('div', 'avatar', initials(p.name));
      avatar.style.background = 'hsl(' + hue(p.peer_id) + ', 55%, 46%)';
      var body = el('div', 'peer-body');
      var name = el('div', 'peer-name', p.name);
      name.title = p.name;
      body.appendChild(name);
      var meta = plural(p.files, 'file') + ' · ' + formatSize(p.total_size);
      if (p.indexing) meta += ' · indexing…';
      meta += ' · ' + p.address;
      body.appendChild(el('div', 'peer-meta', meta));
      li.appendChild(avatar);
      li.appendChild(body);
      list.appendChild(li);
    });
    $('peers-empty').hidden = state.peers.length > 0;

    var ignored = $('ignored');
    ignored.textContent = '';
    ignored.hidden = state.ignored.length === 0;
    if (state.ignored.length) {
      ignored.appendChild(el('div', '', 'Not connected (' + state.ignored.length + '):'));
      state.ignored.forEach(function (p) {
        var line = el('div');
        line.appendChild(el('strong', '', p.name));
        line.appendChild(document.createTextNode(' — ' + (p.reason || 'rejected')));
        ignored.appendChild(line);
      });
    }
  }

  // ----- Common Space ---------------------------------------------------------------------

  async function refreshFiles() {
    var headers = {};
    if (state.filesEtag) headers['If-None-Match'] = state.filesEtag;
    var r = await fetch('/api/common', { headers: headers, cache: 'no-store' });
    if (r.status === 304) return;
    if (!r.ok) throw new Error('HTTP ' + r.status);
    var data = await r.json();
    state.filesEtag = r.headers.get('ETag');
    state.files = data.files;
    state.indexingPending = data.indexing_pending;
    renderFiles();
  }

  function ownerSortKey(f) {
    var names = f.sources.map(function (s) { return s.self ? '' : s.name.toLowerCase(); });
    return names.sort().join(',');
  }

  var collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });

  function sortedFiles() {
    var key = state.sort.key;
    var dir = state.sort.dir;
    var filter = state.filter.trim().toLowerCase();
    var items = state.files.filter(function (f) {
      return !filter || f.name.toLowerCase().indexOf(filter) !== -1 ||
        f.aliases.some(function (a) { return a.toLowerCase().indexOf(filter) !== -1; });
    });
    items.sort(function (a, b) {
      var c = 0;
      if (key === 'size') c = a.size - b.size;
      else if (key === 'mtime') c = a.mtime - b.mtime;
      else if (key === 'owners') c = (b.sources.length - a.sources.length) || collator.compare(ownerSortKey(a), ownerSortKey(b));
      if (c === 0) c = collator.compare(a.name, b.name);
      return c * dir;
    });
    return items;
  }

  function activeDownloadFor(fileId) {
    for (var i = 0; i < state.downloads.length; i++) {
      var d = state.downloads[i];
      if (d.file_id === fileId && (ACTIVE[d.status] || d.status === 'completed')) return d;
    }
    return null;
  }

  function fillActionCell(cell, f) {
    var d = f.local ? null : activeDownloadFor(f.id);
    var sig = f.local ? 'local' : !d ? 'button' : d.status === 'completed' ? 'done' :
      d.status === 'verifying' ? 'verifying' : 'pct' + percent(d);
    if (cell.dataset.sig === sig) return;  // unchanged: keep the element (and its focus)
    cell.dataset.sig = sig;
    cell.textContent = '';
    if (f.local) {
      cell.appendChild(el('span', 'state-label', 'In your share'));
      return;
    }
    if (d && d.status === 'completed') {
      cell.appendChild(el('span', 'state-label ok', 'Saved ✓'));
      return;
    }
    if (d) {
      cell.appendChild(el('span', 'state-label', d.status === 'verifying' ? 'Verifying…' : percent(d) + '%'));
      return;
    }
    var btn = el('button', 'btn', 'Download');
    btn.type = 'button';
    btn.addEventListener('click', function () { startDownload(f, btn); });
    cell.appendChild(btn);
  }

  function renderFiles() {
    var tbody = $('file-rows');
    var items = sortedFiles();
    var rows = document.createDocumentFragment();
    items.forEach(function (f) {
      var tr = el('tr');
      tr.dataset.id = f.id;

      var nameCell = el('td', 'name');
      var slash = f.name.lastIndexOf('/');
      if (slash !== -1) nameCell.appendChild(el('span', 'folder', f.name.slice(0, slash + 1)));
      nameCell.appendChild(document.createTextNode(f.name.slice(slash + 1)));
      if (f.aliases.length) nameCell.appendChild(el('span', 'alias', 'also named: ' + f.aliases.join(', ')));
      tr.appendChild(nameCell);

      var size = el('td', 'num', formatSize(f.size));
      size.title = f.size.toLocaleString() + ' bytes';
      tr.appendChild(size);

      var ownersCell = el('td');
      var owners = el('div', 'owners');
      f.sources.forEach(function (s) {
        var chip = el('span', 'owner' + (s.self ? ' you' : ''), s.self ? 'You' : s.name);
        chip.title = s.self ? 'In your shared folder as ' + s.file_name : s.name + ': ' + s.file_name;
        owners.appendChild(chip);
      });
      ownersCell.appendChild(owners);
      tr.appendChild(ownersCell);

      tr.appendChild(el('td', 'when hide-narrow', formatDate(f.mtime)));

      var action = el('td', 'action-col');
      fillActionCell(action, f);
      tr.appendChild(action);
      rows.appendChild(tr);
    });
    tbody.textContent = '';
    tbody.appendChild(rows);

    var empty = $('files-empty');
    if (!state.files.length) {
      empty.textContent = 'Nothing shared yet. Drop files above to share them, or wait for other computers ' +
        'running LanShare to appear.';
    } else if (!items.length) {
      empty.textContent = 'No files match “' + state.filter.trim() + '”.';
    }
    empty.hidden = items.length > 0;

    var total = state.files.reduce(function (sum, f) { return sum + f.size; }, 0);
    var summary = plural(state.files.length, 'file') + ' · ' + formatSize(total);
    if (items.length !== state.files.length) summary = items.length + ' of ' + summary;
    $('file-summary').textContent = state.files.length ? summary : '';

    document.querySelectorAll('#files th[data-sort]').forEach(function (th) {
      var active = th.dataset.sort === state.sort.key;
      th.setAttribute('aria-sort', active ? (state.sort.dir > 0 ? 'ascending' : 'descending') : 'none');
      th.querySelector('.arrow').textContent = active ? (state.sort.dir > 0 ? '▲' : '▼') : '';
    });
  }

  function refreshActionCells() {
    var byId = {};
    state.files.forEach(function (f) { byId[f.id] = f; });
    document.querySelectorAll('#file-rows tr').forEach(function (tr) {
      var f = byId[tr.dataset.id];
      if (f) fillActionCell(tr.lastChild, f);
    });
  }

  function setSort(key) {
    if (state.sort.key === key) state.sort.dir = -state.sort.dir;
    else state.sort = { key: key, dir: key === 'name' || key === 'owners' ? 1 : -1 };
    renderFiles();
  }

  // ----- downloads ------------------------------------------------------------------------

  async function startDownload(f, button) {
    button.disabled = true;
    try {
      var d = await api('POST', '/api/downloads', { file_id: f.id });
      state.downloadStatus[d.id] = d.status;
      state.downloads = [d].concat(state.downloads.filter(function (x) { return x.id !== d.id; }));
      renderDownloads();
      refreshActionCells();
      scheduleDownloads(300);
    } catch (e) {
      toast('Could not start the download: ' + e.message, 'error');
      button.disabled = false;
    }
  }

  async function downloadAction(d, action) {
    try {
      if (action === 'remove') await api('DELETE', '/api/downloads/' + encodeURIComponent(d.id));
      else if (action === 'show') await api('POST', '/api/open', { target: 'download', download_id: d.id });
      else await api('POST', '/api/downloads/' + encodeURIComponent(d.id) + '/' + action);
    } catch (e) {
      toast(e.message, 'error');
    }
    scheduleDownloads(100);
  }

  async function refreshDownloads() {
    var data = await api('GET', '/api/downloads');
    data.downloads.forEach(function (d) {
      var before = state.downloadStatus[d.id];
      if (before && before !== d.status) {
        if (d.status === 'completed') toast('Downloaded ' + d.name, 'ok');
        else if (d.status === 'failed') toast(d.name + ': ' + (d.error || 'download failed'), 'error');
      }
      state.downloadStatus[d.id] = d.status;
    });
    state.downloads = data.downloads;
    renderDownloads();
    refreshActionCells();
  }

  var downloadEls = {};

  function actionButtons(d) {
    var specs = [];
    if (ACTIVE[d.status]) specs = [['Pause', 'pause'], ['Cancel', 'cancel']];
    else if (d.status === 'paused') specs = [['Resume', 'resume'], ['Cancel', 'cancel']];
    else if (d.status === 'failed') specs = [['Resume', 'resume'], ['Remove', 'remove']];
    else if (d.status === 'completed') specs = [['Show in folder', 'show'], ['Remove', 'remove']];
    else specs = [['Remove', 'remove']];
    return specs;
  }

  function downloadMeta(d) {
    var parts = [];
    if (d.status === 'completed') return 'Saved as ' + d.path;
    if (d.status === 'verifying') return 'Checking SHA-256…';
    parts.push(formatSize(d.received) + ' of ' + formatSize(d.size));
    if (d.status === 'downloading' && d.speed) parts.push(formatSize(d.speed) + '/s');
    if (d.source) parts.push('from ' + d.source.name);
    if (d.status === 'paused') parts.push('paused');
    if (d.status === 'canceled') return 'Canceled';
    if (d.status === 'queued') parts.push('starting…');
    return parts.join(' · ');
  }

  function renderDownloads() {
    var list = $('downloads');
    var seen = {};
    state.downloads.forEach(function (d, index) {
      seen[d.id] = true;
      var item = downloadEls[d.id];
      if (!item) {
        var li = el('li', 'dl');
        var top = el('div', 'dl-top');
        var name = el('span', 'dl-name');
        var pct = el('span', 'dl-pct');
        top.appendChild(name);
        top.appendChild(pct);
        var bar = el('progress');
        bar.max = 100;
        var meta = el('div', 'dl-meta');
        var note = el('div', 'dl-note');
        var error = el('div', 'dl-error');
        var actions = el('div', 'dl-actions');
        [top, bar, meta, note, error, actions].forEach(function (c) { li.appendChild(c); });
        item = downloadEls[d.id] = { li: li, name: name, pct: pct, bar: bar, meta: meta, note: note,
          error: error, actions: actions, status: null };
      }
      item.li.className = 'dl ' + d.status;
      item.name.textContent = d.name;
      item.name.title = d.name;
      var p = percent(d);
      item.pct.textContent = d.status === 'completed' ? 'Done' : p + '%';
      item.bar.value = p;
      item.bar.setAttribute('aria-label', d.name + ' ' + p + '%');
      item.meta.textContent = downloadMeta(d);
      item.note.textContent = d.note || '';
      item.note.hidden = !d.note;
      item.error.textContent = d.error || '';
      item.error.hidden = !d.error;
      if (item.status !== d.status) {
        item.status = d.status;
        item.actions.textContent = '';
        actionButtons(d).forEach(function (spec) {
          var b = el('button', 'btn secondary', spec[0]);
          b.type = 'button';
          b.addEventListener('click', function () { downloadAction(d, spec[1]); });
          item.actions.appendChild(b);
        });
      }
      if (list.children[index] !== item.li) list.insertBefore(item.li, list.children[index] || null);
    });
    Object.keys(downloadEls).forEach(function (id) {
      if (!seen[id]) {
        downloadEls[id].li.remove();
        delete downloadEls[id];
      }
    });
    $('downloads-empty').hidden = state.downloads.length > 0;
    $('clear-downloads').hidden = !state.downloads.some(function (d) {
      return d.status === 'completed' || d.status === 'canceled';
    });
  }

  var downloadTimer = null;

  function scheduleDownloads(delay) {
    clearTimeout(downloadTimer);
    downloadTimer = setTimeout(async function () {
      if (state.stopped) return;
      try {
        await refreshDownloads();
        connectionOk();
      } catch (e) {
        connectionFailed(e);
      }
      var busy = state.downloads.some(function (d) { return ACTIVE[d.status]; });
      scheduleDownloads(busy ? 700 : 3000);
    }, delay);
  }

  // ----- uploads --------------------------------------------------------------------------

  var uploadQueue = [];
  var uploadsRunning = 0;

  function isShareable(name) {
    var lower = name.toLowerCase();
    if (name.charAt(0) === '.' || name.slice(0, 2) === '~$' || JUNK[lower]) return false;
    return !TEMP_SUFFIXES.some(function (s) { return lower.slice(-s.length) === s; });
  }

  function queueUploads(entries) {
    var skipped = 0;
    entries.forEach(function (entry) {
      var parts = entry.path.split('/');
      if (!parts.every(isShareable)) { skipped++; return; }
      var li = el('li');
      var name = el('span', 'u-name', entry.path);
      var status = el('span', 'u-state', 'Waiting…');
      var bar = el('progress');
      bar.max = 100;
      bar.value = 0;
      li.appendChild(name);
      li.appendChild(status);
      li.appendChild(bar);
      $('uploads').appendChild(li);
      uploadQueue.push({ file: entry.file, path: entry.path, li: li, status: status, bar: bar });
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
    job.bar.value = ok ? 100 : job.bar.value;
    if (ok) {
      setTimeout(function () { job.li.remove(); }, 6000);
      state.filesEtag = null;
      setTimeout(function () { refreshFiles().catch(function () {}); }, 600);
    } else {
      var dismiss = el('button', 'text-btn', 'Dismiss');
      dismiss.type = 'button';
      dismiss.addEventListener('click', function () { job.li.remove(); });
      job.li.appendChild(dismiss);
      toast('Could not add ' + job.path + ': ' + message, 'error');
    }
    pumpUploads();
  }

  function sendUpload(job) {
    uploadsRunning++;
    var xhr = new XMLHttpRequest();
    var url = '/api/upload?name=' + encodeURIComponent(job.path);
    if (job.file.lastModified) url += '&mtime=' + job.file.lastModified;
    xhr.open('POST', url);
    xhr.setRequestHeader('X-LanShare-Client', 'ui');
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    job.status.textContent = 'Uploading…';
    xhr.upload.onprogress = function (e) {
      if (!e.lengthComputable) return;
      var p = Math.floor(e.loaded / e.total * 100);
      job.bar.value = p;
      job.status.textContent = p + '% of ' + formatSize(e.total);
    };
    xhr.onload = function () {
      var data = null;
      try { data = JSON.parse(xhr.responseText); } catch (e) { data = null; }
      if (xhr.status === 201 && data) {
        finishUpload(job, true, data.renamed ? 'Added as ' + data.name : 'Added');
      } else {
        finishUpload(job, false, errorMessage(data, xhr.status));
      }
    };
    xhr.onerror = function () { finishUpload(job, false, 'Connection to LanShare lost.'); };
    xhr.send(job.file);
  }

  function readAllEntries(reader) {
    return new Promise(function (resolve, reject) {
      var all = [];
      (function next() {
        reader.readEntries(function (batch) {
          if (!batch.length) return resolve(all);
          all = all.concat(Array.prototype.slice.call(batch));
          next();
        }, reject);
      })();
    });
  }

  async function walkEntry(entry, prefix) {
    if (entry.isFile) {
      var file = await new Promise(function (resolve, reject) { entry.file(resolve, reject); });
      return [{ file: file, path: prefix + file.name }];
    }
    if (entry.isDirectory) {
      if (!isShareable(entry.name)) return [];
      var children = await readAllEntries(entry.createReader());
      var out = [];
      for (var i = 0; i < children.length; i++) {
        out = out.concat(await walkEntry(children[i], prefix + entry.name + '/'));
      }
      return out;
    }
    return [];
  }

  async function handleDrop(dataTransfer) {
    // Entries must be taken synchronously, before the drop event handler returns.
    var items = dataTransfer.items;
    var entries = [];
    if (items && items.length && typeof items[0].webkitGetAsEntry === 'function') {
      for (var i = 0; i < items.length; i++) {
        var entry = items[i].kind === 'file' ? items[i].webkitGetAsEntry() : null;
        if (entry) entries.push(entry);
      }
    }
    var files = Array.prototype.slice.call(dataTransfer.files || []);
    if (!entries.length) {
      queueUploads(files.map(function (f) { return { file: f, path: f.name }; }));
      return;
    }
    try {
      var collected = [];
      for (var j = 0; j < entries.length; j++) collected = collected.concat(await walkEntry(entries[j], ''));
      queueUploads(collected);
    } catch (e) {
      toast('Could not read the dropped items: ' + e.message, 'error');
    }
  }

  function setupDragAndDrop() {
    var zone = $('drop-zone');
    var input = $('file-input');
    var overlay = $('drag-overlay');
    var depth = 0;

    function hasFiles(e) {
      return e.dataTransfer && Array.prototype.indexOf.call(e.dataTransfer.types || [], 'Files') !== -1;
    }

    zone.addEventListener('click', function () { input.click(); });
    zone.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); }
    });
    input.addEventListener('change', function () {
      queueUploads(Array.prototype.map.call(input.files, function (f) { return { file: f, path: f.name }; }));
      input.value = '';
    });

    window.addEventListener('dragenter', function (e) {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth++;
      overlay.hidden = false;
      zone.classList.add('active');
    });
    window.addEventListener('dragover', function (e) {
      if (!hasFiles(e)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'copy';
    });
    window.addEventListener('dragleave', function (e) {
      if (!hasFiles(e)) return;
      depth = Math.max(0, depth - 1);
      if (!depth) { overlay.hidden = true; zone.classList.remove('active'); }
    });
    window.addEventListener('drop', function (e) {
      if (!hasFiles(e)) return;
      e.preventDefault();  // never let the browser navigate to a dropped file
      depth = 0;
      overlay.hidden = true;
      zone.classList.remove('active');
      handleDrop(e.dataTransfer);
    });
  }

  // ----- connection state and polling -----------------------------------------------------

  function connectionOk() {
    state.failures = 0;
    $('offline-banner').hidden = true;
  }

  function connectionFailed() {
    state.failures++;
    if (state.failures >= 2 && !state.stopped) $('offline-banner').hidden = false;
  }

  function every(ms, fn) {
    var running = false;
    async function tick() {
      if (running || state.stopped) return;
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
    tick();
    return setInterval(tick, ms);
  }

  // ----- wiring ---------------------------------------------------------------------------

  function init() {
    applyTheme(storedTheme());
    $('theme-toggle').addEventListener('click', cycleTheme);

    document.querySelectorAll('#files th[data-sort] button').forEach(function (b) {
      b.addEventListener('click', function () { setSort(b.parentElement.dataset.sort); });
    });
    $('filter').addEventListener('input', function (e) {
      state.filter = e.target.value;
      renderFiles();
    });
    $('clear-downloads').addEventListener('click', async function () {
      try { await api('POST', '/api/downloads/clear'); } catch (e) { toast(e.message, 'error'); }
      scheduleDownloads(50);
    });
    document.querySelectorAll('[data-open]').forEach(function (b) {
      b.addEventListener('click', function () {
        api('POST', '/api/open', { target: b.dataset.open }).catch(function (e) { toast(e.message, 'error'); });
      });
    });
    $('quit').addEventListener('click', async function () {
      if (!window.confirm('Stop LanShare on this computer? Other peers will no longer see your files.')) return;
      try { await api('POST', '/api/shutdown'); } catch (e) { /* it may already be gone */ }
      state.stopped = true;
      var banner = $('offline-banner');
      banner.textContent = 'LanShare has stopped. You can close this tab.';
      banner.hidden = false;
    });

    setupDragAndDrop();
    every(5000, refreshStatus);
    every(2000, refreshPeers);
    every(2000, refreshFiles);
    scheduleDownloads(0);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
