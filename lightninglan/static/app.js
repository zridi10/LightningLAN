/* LightningLAN web UI. Plain ES2017, no build step.
 *
 * Every piece of text that came from a file name or a peer is inserted with textContent,
 * never as HTML. State-changing requests carry the X-LightningLAN-Client header the server
 * requires (it blocks cross-site requests from other web pages).
 */
(function () {
  'use strict';

  var CLIENT_HEADER = { 'X-LightningLAN-Client': 'ui' };
  var ACTIVE = { queued: true, downloading: true, verifying: true };
  var JUNK = { 'thumbs.db': true, 'ehthumbs.db': true, 'desktop.ini': true, '.ds_store': true };
  var TEMP_SUFFIXES = ['.part', '.partial', '.crdownload', '.tmp'];
  var THEMES = ['synthwave', 'bliss', 'deepsea', 'light', 'dark'];  // the first one is the default
  var THEME_NAMES = { synthwave: 'Synthwave', bliss: 'Bliss', deepsea: 'Deep Sea', light: 'Light', dark: 'Dark' };

  var state = {
    status: null,
    files: [],
    filesEtag: null,
    peers: [],
    ignored: [],
    downloads: [],
    sort: { key: 'name', dir: 1 },
    filter: '',
    sharer: '',  // a peer ID: only the files that peer shares
    knownPeers: null,
    downloadStatus: {},
    exited: false,  // LightningLAN quit (the page has nothing left to talk to)
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
    var mode = null;
    try { mode = localStorage.getItem('lightninglan-theme'); } catch (e) { /* private mode */ }
    return THEMES.indexOf(mode) === -1 ? THEMES[0] : mode;  // Synthwave unless another one was picked
  }

  function applyTheme(mode) {
    document.documentElement.setAttribute('data-theme', mode);
  }

  function saveTheme(mode) {
    try { localStorage.setItem('lightninglan-theme', mode); } catch (e) { /* private mode */ }
    applyTheme(mode);
  }

  function fillThemeChoice(select) {
    THEMES.forEach(function (mode) {
      var option = el('option', null, THEME_NAMES[mode]);
      option.value = mode;
      select.appendChild(option);
    });
    select.value = storedTheme();
  }

  // ----- header and status ----------------------------------------------------------------

  async function refreshStatus() {
    state.status = await api('GET', '/api/status');
    renderStatus();
  }

  function renderStatus() {
    var s = state.status;
    $('self-name').textContent = s.name;
    $('self-name').title = s.name;
    document.title = 'LightningLAN — ' + s.name;
    $('lock').hidden = !s.passphrase_enabled;
    $('share-dir').textContent = s.share_dir;
    $('share-dir').title = s.share_dir;
    $('downloads-dir').textContent = s.downloads_dir;
    $('downloads-dir').title = s.downloads_dir;
    var summary = plural(s.shared_files, 'file') + ' shared';
    if (s.indexing_pending) summary += ' · indexing ' + plural(s.indexing_pending, 'file') + '…';
    $('share-summary').textContent = summary;
    $('welcome').hidden = !(s.first_run && !welcomeDismissed());
    $('welcome-share').textContent = s.share_dir;
    $('welcome-name').textContent = s.name;
    renderSharing();
    renderPhones();
  }

  // ----- your name, edited in place at the top ---------------------------------------------

  function startNameEdit() {
    var input = $('name-input');
    input.value = state.status ? state.status.name : '';
    $('self-name').hidden = true;
    $('name-edit').hidden = true;
    $('name-form').hidden = false;
    input.focus();
    input.select();
  }

  function endNameEdit() {
    $('name-form').hidden = true;
    $('self-name').hidden = false;
    $('name-edit').hidden = false;
  }

  async function saveName(e) {
    e.preventDefault();
    var name = $('name-input').value.trim();
    if (state.status && name === state.status.name) { endNameEdit(); return; }
    var save = $('name-save');
    save.disabled = true;
    try {
      var result = await api('POST', '/api/settings', { name: name });
      endNameEdit();
      toast('You are now ' + result.settings.name + '.', 'ok');
      refreshStatus().catch(function () {});
    } catch (err) {
      toast(err.message, 'error');
      $('name-input').focus();
    } finally {
      save.disabled = false;
    }
  }

  async function rollName() {
    try {
      $('name-input').value = (await api('GET', '/api/random-name')).name;
    } catch (e) {
      toast('Could not pick a name: ' + e.message, 'error');
    }
    $('name-input').focus();
  }

  // ----- Stop and Start -------------------------------------------------------------------

  function isStopped() { return !!state.status && state.status.online === false; }

  function renderSharing() {
    var stopped = isStopped();
    $('stopped-banner').hidden = !stopped || state.exited;
    $('stop-sharing').hidden = stopped;
    $('start-sharing').hidden = !stopped;
    renderPeerCount();
    renderPeers();
  }

  async function setSharing(on) {
    if (!on && !window.confirm('Stop LightningLAN on this computer? Other peers will no longer see your files, ' +
        'and your downloads pause until you press Start LightningLAN.')) return;
    var buttons = [$('stop-sharing'), $('start-sharing'), $('banner-start')];
    buttons.forEach(function (b) { b.disabled = true; });
    try {
      state.status = await api('POST', on ? '/api/start' : '/api/stop');
      renderStatus();
      toast(on ? 'LightningLAN is sharing again.' : 'LightningLAN is stopped.', on ? 'ok' : undefined);
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      buttons.forEach(function (b) { b.disabled = false; });
    }
    refreshPeers().catch(function () {});
    refreshFiles().catch(function () {});
    scheduleDownloads(100);
  }

  async function quitApp() {
    if (!window.confirm('Quit LightningLAN completely? To share again later, open LightningLAN again.')) return;
    try { await api('POST', '/api/shutdown'); } catch (e) { /* it may already be gone */ }
    state.exited = true;
    if (state.session) state.session.close();
    $('stopped-banner').hidden = true;
    var banner = $('offline-banner');
    banner.textContent = 'LightningLAN has quit. You can close this tab.';
    banner.hidden = false;
  }

  // ----- phones ---------------------------------------------------------------------------
  // The phone dialog shows a QR code with a link to the phone page, which LightningLAN serves to
  // phones on the same Wi-Fi. The link holds a secret key, so only phones that scanned it get in;
  // "Disconnect phones" replaces the key.

  var phoneLinks = [];
  var phonesSeen = 0;

  function drawQr(rows) {
    var qr = $('phone-qr');
    var size = rows.length + 8;  // a quiet zone of four modules all round
    qr.textContent = '';
    qr.setAttribute('viewBox', '0 0 ' + size + ' ' + size);
    qr.appendChild(svg('rect', { width: size, height: size, fill: '#ffffff' }));
    var d = '';
    rows.forEach(function (row, y) {
      for (var x = 0; x < row.length; x++) {
        if (row[x] !== '1') continue;
        var run = 1;
        while (row[x + run] === '1') run++;
        d += 'M' + (x + 4) + ' ' + (y + 4) + 'h' + run + 'v1h-' + run + 'z';
        x += run - 1;
      }
    });
    qr.appendChild(svg('path', { d: d, fill: '#000000', 'shape-rendering': 'crispEdges' }));
  }

  function showPhoneLink() {
    var link = phoneLinks[$('phone-address').selectedIndex] || phoneLinks[0];
    $('phone-no-network').hidden = !!link;
    $('qr-box').hidden = !link;
    $('phone-url').textContent = link ? link.url : '';
    if (link) drawQr(link.qr);
  }

  function fillPhoneDialog(data) {
    var select = $('phone-address');
    var previous = select.value;
    phoneLinks = data.links;
    select.textContent = '';
    phoneLinks.forEach(function (link) {
      var option = el('option', '', link.address);
      option.value = link.address;
      option.selected = link.address === previous;
      select.appendChild(option);
    });
    $('phone-address-row').hidden = phoneLinks.length < 2;
    showPhoneLink();
  }

  async function openPhone() {
    try {
      fillPhoneDialog(await api('GET', '/api/phone'));
    } catch (e) {
      toast('Could not set up the phone link: ' + e.message, 'error');
      return;
    }
    renderPhones();
    var dialog = $('phone-dialog');
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
  }

  function closePhone() {
    var dialog = $('phone-dialog');
    if (typeof dialog.close === 'function') dialog.close();
    else dialog.removeAttribute('open');
  }

  async function newPhoneKey() {
    if (!window.confirm('Disconnect every phone? To connect again, a phone has to scan the new QR code.')) return;
    try {
      fillPhoneDialog(await api('POST', '/api/phone/new-key'));
      toast('Phones disconnected. The QR code is new.', 'ok');
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  function renderPhones() {
    var n = state.status ? state.status.phones || 0 : 0;
    var label = n ? plural(n, 'phone') + ' connected' : 'Use LightningLAN on your phone';
    $('phone-badge').hidden = n === 0;
    $('phone-badge').textContent = n > 1 ? String(n) : '';
    $('phone-open').title = label;
    $('phone-open').setAttribute('aria-label', label);
    $('phone-connected').textContent = n ? '✓ ' + plural(n, 'phone') + ' connected right now.' :
      'No phone connected yet.';
    $('phone-connected').classList.toggle('ok', n > 0);
    if (n > phonesSeen && $('phone-dialog').open) toast('Phone connected!', 'ok');
    phonesSeen = n;
  }

  // ----- the web UI session ---------------------------------------------------------------
  // While the page is open, one tab keeps /api/ui-session open (a Web Lock picks which, so any
  // number of tabs share one connection). When the last tab closes, LightningLAN stops sharing;
  // opening the page again starts it again. The stream also says when Stop or Start was pressed.

  function holdSession() {
    function open() {
      var source = state.session = new EventSource('/api/ui-session');
      source.onmessage = function () {
        if (state.exited) return;
        refreshStatus().catch(function () {});
        refreshPeers().catch(function () {});
        scheduleDownloads(100);
      };
      return new Promise(function () {});  // keep the lock for as long as this tab is open
    }
    if (navigator.locks && typeof navigator.locks.request === 'function') {
      navigator.locks.request('lightninglan-ui-session', open);
    } else {
      open();
    }
  }

  // ----- first-run welcome ----------------------------------------------------------------

  function welcomeDismissed() {
    try { return localStorage.getItem('lightninglan-welcome-dismissed') === '1'; } catch (e) { return false; }
  }

  function dismissWelcome() {
    try { localStorage.setItem('lightninglan-welcome-dismissed', '1'); } catch (e) { /* private mode */ }
    $('welcome').hidden = true;
  }

  // ----- settings -------------------------------------------------------------------------

  var loadedSettings = null;

  function showSettingsError(message) {
    $('settings-error').textContent = message || '';
    $('settings-error').hidden = !message;
  }

  async function openSettings() {
    try {
      loadedSettings = await api('GET', '/api/settings');
    } catch (e) {
      toast('Could not load the settings: ' + e.message, 'error');
      return;
    }
    var s = loadedSettings;
    showSettingsError('');
    $('set-name').value = s.name;
    $('set-share').value = s.share_dir;
    $('set-downloads').value = s.downloads_dir;
    $('set-pass').value = s.passphrase;
    $('set-pass').type = 'password';
    $('set-pass-toggle').textContent = 'Show';
    $('set-theme').value = storedTheme();
    $('set-url').textContent = s.url;
    $('set-config').textContent = s.config_path || '';
    document.querySelectorAll('[data-pick]').forEach(function (b) { b.hidden = !s.can_pick_folder; });
    var dialog = $('settings');
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
    $('set-name').focus();
  }

  function closeSettings() {
    var dialog = $('settings');
    if (typeof dialog.close === 'function') dialog.close();
    else dialog.removeAttribute('open');
    applyTheme(storedTheme());  // a theme tried out but not saved
  }

  async function saveSettings(e) {
    e.preventDefault();
    var s = loadedSettings || {};
    var body = {};
    var fields = { name: 'set-name', share_dir: 'set-share', downloads_dir: 'set-downloads' };
    Object.keys(fields).forEach(function (key) {
      var value = $(fields[key]).value.trim();
      if (value !== s[key]) body[key] = value;
    });
    if ($('set-pass').value !== s.passphrase) body.passphrase = $('set-pass').value;
    saveTheme($('set-theme').value);
    if (!Object.keys(body).length) { closeSettings(); return; }
    var save = $('settings-save');
    save.disabled = true;
    showSettingsError('');
    try {
      var result = await api('POST', '/api/settings', body);
      closeSettings();
      var message = 'Settings saved.';
      if (result.changed.indexOf('passphrase') !== -1) {
        message += ' Peers now reconnect using the ' + (body.passphrase ? 'new' : 'removed') + ' passphrase.';
      }
      if (result.changed.indexOf('share_dir') !== -1) message += ' Indexing the new shared folder…';
      toast(message, 'ok');
      state.filesEtag = null;
      refreshStatus().catch(function () {});
      refreshFiles().catch(function () {});
      refreshPeers().catch(function () {});
    } catch (err) {
      showSettingsError(err.message);
    } finally {
      save.disabled = false;
    }
  }

  async function pickFolder(button) {
    var input = $(button.dataset.pick);
    var label = button.textContent;
    button.disabled = true;
    button.textContent = 'Choose in the window that opened…';
    showSettingsError('');
    try {
      var r = await api('POST', '/api/pick-folder', {
        purpose: button.dataset.pick === 'set-share' ? 'share' : 'downloads',
        initial: input.value.trim() || null
      });
      if (r.path) input.value = r.path;
    } catch (e) {
      showSettingsError(e.message);
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
  }

  function togglePassphrase() {
    var input = $('set-pass');
    var show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    $('set-pass-toggle').textContent = show ? 'Hide' : 'Show';
  }

  function renderPeerCount() {
    var n = state.peers.length;
    $('peer-count').textContent = isStopped() ? 'Stopped' : n === 0 ? 'No peers online' : plural(n, 'peer') + ' online';
    $('peer-pill').className = 'pill ' + (n && !isStopped() ? 'pill-on' : 'pill-off');
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
    $('peers-empty').textContent = isStopped() ? 'Stopped: not connected to other computers.' :
      'Looking for other computers running LightningLAN on your network…';

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

  // ----- ThunderCLOUD (every file on the network, merged) -----------------------------------

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
      if (state.sharer && !f.sources.some(function (s) { return s.peer_id === state.sharer; })) return false;
      return !filter || f.name.toLowerCase().indexOf(filter) !== -1 ||
        f.aliases.some(function (a) { return a.toLowerCase().indexOf(filter) !== -1; });
    });
    items.sort(function (a, b) {
      var c = 0;
      if (key === 'size') c = a.size - b.size;
      else if (key === 'mtime') c = a.mtime - b.mtime;
      else if (key === 'owners') c = collator.compare(ownerSortKey(a), ownerSortKey(b)) || (b.sources.length - a.sources.length);
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

  // The "Shared by" choice: everyone, or one of the peers sharing something right now.
  function fillSharers() {
    var select = $('sharer');
    var sharers = {};
    state.files.forEach(function (f) {
      f.sources.forEach(function (s) { sharers[s.peer_id] = { name: s.self ? 'You' : s.name, self: s.self }; });
    });
    if (state.sharer && !sharers[state.sharer]) state.sharer = '';  // went offline, or shares nothing now
    var ids = Object.keys(sharers).sort(function (a, b) {
      return (sharers[b].self ? 1 : 0) - (sharers[a].self ? 1 : 0) || collator.compare(sharers[a].name, sharers[b].name);
    });
    var sig = ids.map(function (id) { return id + '=' + sharers[id].name; }).join('|');
    if (select.dataset.sig !== sig) {  // rebuilt only when the sharers change, so an open list stays open
      select.dataset.sig = sig;
      select.textContent = '';
      var all = el('option', null, 'Shared by anyone');
      all.value = '';
      select.appendChild(all);
      ids.forEach(function (id) {
        var option = el('option', null, sharers[id].name);
        option.value = id;
        select.appendChild(option);
      });
    }
    select.value = state.sharer;
    select.hidden = ids.length < 2;
  }

  function renderFiles() {
    var tbody = $('file-rows');
    fillSharers();
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
        'running LightningLAN to appear.';
    } else if (!items.length) {
      empty.textContent = state.filter.trim() ? 'No files match “' + state.filter.trim() + '”.' : 'No files from them.';
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
    var senders = d.swarm ? d.swarm.peers.filter(function (x) { return x.bytes > 0; }) : [];
    if (senders.length > 1) parts.push('from ' + senders[0].name + ' and ' + plural(senders.length - 1, 'other peer'));
    else if (d.source) parts.push('from ' + d.source.name);
    if (d.status === 'paused') parts.push('paused');
    if (d.status === 'canceled') return 'Canceled';
    if (d.status === 'queued') parts.push(d.note ? 'queued' : 'starting…');
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
        var cat = el('button', 'nyan-host');
        cat.type = 'button';
        cat.appendChild(nyan());
        var body = el('div', 'dl-body');
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
        [top, bar, meta, note, error, actions].forEach(function (c) { body.appendChild(c); });
        li.appendChild(cat);
        li.appendChild(body);
        item = downloadEls[d.id] = { li: li, cat: cat, name: name, pct: pct, bar: bar, meta: meta, note: note,
          error: error, actions: actions, status: null, d: d };
        cat.addEventListener('click', catClicked.bind(null, item));
      }
      item.d = d;
      item.li.className = 'dl ' + d.status;
      setCat(item.cat, d.status, 'the download of ' + d.name);
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
    renderNyanAll();
  }

  var downloadTimer = null;

  function scheduleDownloads(delay) {
    clearTimeout(downloadTimer);
    downloadTimer = setTimeout(async function () {
      if (state.exited) return;
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

  // ----- Nyan Cat download indicators -----------------------------------------------------
  // A spinning Nyan Cat shows that files are downloading: one on each download, and one in the
  // top bar for all of them. Pointing at it shows a pause sign; a click pauses, and another
  // click resumes. It is drawn from the pixel map below, so the page needs no image files.

  var SVG_NS = 'http://www.w3.org/2000/svg';
  var NYAN_SPRITE = [
    '....kkkkkkkkkkkk.......',
    '...kcccccccccccck......',
    '...kccpppppppkcck....k.',
    '...kcppsppppkgkck...kgk',
    '...kcpppppspkggkkkkkggk',
    '...kcpppppppkgggggggggk',
    'kkkkcpspppppkgwkgggwkgk',
    'kggkcppppsppkgkkgkgkkgk',
    'kkkkcpppppppkrrgggggrrk',
    '...kccpppppskggkgkgkggk',
    '...kccccccccckgggggggk.',
    '....kkkkkkkkkkkkkkkkk..',
    '....kgk.kgk..kgk..kgk..',
    '....kkk.kkk..kkk..kkk..'
  ];
  var NYAN_COLORS = { k: '#000000', c: '#ffcc99', p: '#ff99ff', s: '#ff3399', g: '#999999', w: '#ffffff', r: '#ff9999' };
  var RAINBOW = ['#ff0000', '#ff9900', '#ffff00', '#33ff00', '#0099ff', '#6633ff'];
  var nyanTemplate = null;

  function svg(tag, attrs) {
    var e = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs).forEach(function (k) { e.setAttribute(k, String(attrs[k])); });
    return e;
  }

  function drawNyan() {
    var art = svg('svg', { viewBox: '0 0 100 100', 'aria-hidden': 'true', focusable: 'false' });
    // The rainbow trails the cat around the circle in 15° steps that alternate in and out,
    // fading towards its far end.
    for (var step = 0; step < 15; step++) {
      var trail = svg('g', { opacity: step < 4 ? (step + 1) * 0.2 : 1 });
      for (var band = 0; band < RAINBOW.length; band++) {
        var r = 39.5 - band * 3.4 + (step % 2 ? 0.8 : 0);
        var around = 2 * Math.PI * r;
        trail.appendChild(svg('circle', {
          cx: 50, cy: 50, r: r.toFixed(2), fill: 'none', stroke: RAINBOW[band], 'stroke-width': 3.6,
          'stroke-dasharray': (around * 15.5 / 360).toFixed(2) + ' ' + around.toFixed(2),
          transform: 'rotate(' + (37 + step * 15) + ' 50 50)'
        }));
      }
      art.appendChild(trail);
    }
    // The cat, one path per colour, at the top of the circle facing the way it spins.
    var cat = svg('g', { transform: 'translate(33.15 7.6) scale(1.9)' });
    Object.keys(NYAN_COLORS).forEach(function (key) {
      var d = '';
      NYAN_SPRITE.forEach(function (row, y) {
        for (var x = 0; x < row.length; x++) {
          if (row[x] !== key) continue;
          var run = 1;
          while (row[x + run] === key) run++;
          d += 'M' + x + ' ' + y + 'h' + run + 'v1h-' + run + 'z';
          x += run - 1;
        }
      });
      if (d) cat.appendChild(svg('path', { d: d, fill: NYAN_COLORS[key] }));
    });
    art.appendChild(cat);
    return art;
  }

  function overlayIcon(className, path) {
    var icon = svg('svg', { viewBox: '0 0 24 24', class: className, 'aria-hidden': 'true', focusable: 'false' });
    icon.appendChild(svg('path', { d: path }));
    return icon;
  }

  function nyan() {
    if (!nyanTemplate) {
      nyanTemplate = el('span', 'nyan');
      var orbit = el('span', 'nyan-orbit');
      orbit.appendChild(drawNyan());
      var overlay = el('span', 'nyan-overlay');
      overlay.appendChild(overlayIcon('nyan-pause', 'M7 5h3.5v14H7zM13.5 5H17v14h-3.5z'));
      overlay.appendChild(overlayIcon('nyan-play', 'M8 5v14l11-7z'));
      nyanTemplate.appendChild(orbit);
      nyanTemplate.appendChild(overlay);
    }
    return nyanTemplate.cloneNode(true);
  }

  function setCat(button, status, what) {
    var paused = status === 'paused';
    button.hidden = !ACTIVE[status] && !paused;
    button.classList.toggle('paused', paused);
    button.classList.toggle('busy', status === 'verifying');
    var label = status === 'verifying' ? 'Checking SHA-256…' : (paused ? 'Resume ' : 'Pause ') + what;
    button.title = label;
    button.setAttribute('aria-label', label);
  }

  function catClicked(item) {
    var d = item.d;
    if (d.status === 'paused') downloadAction(d, 'resume');
    else if (d.status === 'queued' || d.status === 'downloading') downloadAction(d, 'pause');
  }

  function renderNyanAll() {
    var active = state.downloads.filter(function (d) { return ACTIVE[d.status]; }).length;
    var paused = state.downloads.filter(function (d) { return d.status === 'paused'; }).length;
    var button = $('nyan-all');
    setCat(button, active ? 'downloading' : paused ? 'paused' : 'none', 'all downloads');
    $('nyan-all-label').textContent = active ? plural(active, 'file') + ' downloading' :
      plural(paused, 'download') + ' paused';
  }

  async function nyanAllClicked() {
    var active = state.downloads.some(function (d) { return ACTIVE[d.status]; });
    try {
      await api('POST', active ? '/api/downloads/pause-all' : '/api/downloads/resume-all');
    } catch (e) {
      toast(e.message, 'error');
    }
    scheduleDownloads(100);
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
    xhr.setRequestHeader('X-LightningLAN-Client', 'ui');
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
    xhr.onerror = function () { finishUpload(job, false, 'Connection to LightningLAN lost.'); };
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
    if (state.failures >= 2 && !state.exited) $('offline-banner').hidden = false;
  }

  function every(ms, fn) {
    var running = false;
    async function tick() {
      if (running || state.exited) return;
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
    fillThemeChoice($('set-theme'));
    $('set-theme').addEventListener('change', function (e) { applyTheme(e.target.value); });  // try it out
    $('settings').addEventListener('close', function () { applyTheme(storedTheme()); });  // closed with Esc
    $('sharer').addEventListener('change', function (e) {
      state.sharer = e.target.value;
      renderFiles();
    });
    $('name-edit').addEventListener('click', startNameEdit);
    $('name-form').addEventListener('submit', saveName);
    $('name-dice').addEventListener('click', rollName);
    $('name-cancel').addEventListener('click', endNameEdit);
    $('name-input').addEventListener('keydown', function (e) {
      if (e.key === 'Escape') endNameEdit();
    });
    $('phone-open').addEventListener('click', openPhone);
    $('phone-close').addEventListener('click', closePhone);
    $('phone-new-key').addEventListener('click', newPhoneKey);
    $('phone-address').addEventListener('change', showPhoneLink);
    $('settings-open').addEventListener('click', openSettings);
    $('settings-cancel').addEventListener('click', closeSettings);
    $('settings-form').addEventListener('submit', saveSettings);
    $('set-pass-toggle').addEventListener('click', togglePassphrase);
    document.querySelectorAll('[data-pick]').forEach(function (b) {
      b.addEventListener('click', function () { pickFolder(b); });
    });
    $('welcome-dismiss').addEventListener('click', dismissWelcome);
    $('welcome-settings').addEventListener('click', function () { dismissWelcome(); openSettings(); });

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
    $('stop-sharing').addEventListener('click', function () { setSharing(false); });
    $('start-sharing').addEventListener('click', function () { setSharing(true); });
    $('banner-start').addEventListener('click', function () { setSharing(true); });
    $('quit').addEventListener('click', quitApp);
    $('nyan-all').insertBefore(nyan(), $('nyan-all-label'));
    $('nyan-all').addEventListener('click', nyanAllClicked);
    window.addEventListener('pageshow', function (e) {
      if (e.persisted) window.location.reload();  // back from the browser's page cache: start afresh
    });

    holdSession();
    setupDragAndDrop();
    window.LightningPlay.mount({ api: '/api/play', client: CLIENT_HEADER, toast: toast });  // play.js
    every(5000, refreshStatus);
    every(2000, refreshPeers);
    every(2000, refreshFiles);
    scheduleDownloads(0);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
