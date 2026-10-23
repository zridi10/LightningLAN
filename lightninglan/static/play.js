/* While you wait: head-to-head games, on the computer's page and on the phone page.
 * Plain ES2017, no build step.
 *
 * The page's own script calls LightningPlay.mount({ api, client, toast }). The games themselves
 * (rules, invitations, the other player's moves) are handled by LightningLAN (play.py, games.py);
 * this only shows them and sends your moves. The solo game is blocks.js. Names from other peers go
 * in with textContent, never as HTML.
 */
(function () {
  'use strict';

  var MARK_TEXT = { x: 'X', o: 'O' };
  var CHOICE = { rock: { icon: '✊', name: 'Rock' }, paper: { icon: '✋', name: 'Paper' },
    scissors: { icon: '✌️', name: 'Scissors' } };
  var CARDS = ['🍎', '🍋', '🍇', '🍉', '🍒', '🥝',
    '🍍', '🥥'];  // apple, lemon, grapes, watermelon, cherries, kiwi, pineapple, coconut
  var ABOUT = {
    tictactoe: 'The classic: three in a row on a 3×3 grid.',
    ultimate: 'Nine boards in one. Where you play picks the board your opponent must play in next. ' +
      'Win three boards in a row.',
    connect4: 'Drop discs into seven columns. First to four in a row, in any direction, wins.',
    checkers: 'Move diagonally, jump to capture (jumps are a must, and chains keep going), and reach the far ' +
      'side to be crowned. Take every piece, or leave them no move.',
    dots: 'Take turns drawing a line between two dots. Close a box to claim it and go again. Most boxes wins.',
    battleship: 'Hide five ships, then take turns firing. Every shot gets a hit or a miss. Sink the whole fleet ' +
      'first. Both fleets are checked at the end, so nobody can fib.',
    memory: 'Take turns turning over two cards. Find a pair to keep it and go again. Most pairs wins.',
    rps: 'Both lock in a choice, then both are revealed together. Rock beats scissors, scissors beat paper, ' +
      'paper beats rock.',
    typing: 'The same sentence for both of you. Fastest accurate finish wins. Each side times itself.',
    math: 'Ten quick sums. Whoever gets each one right faster gets the point. Each side times itself.'
  };
  var KINDS = ['tictactoe', 'ultimate', 'connect4', 'checkers', 'dots', 'battleship', 'memory', 'rps', 'typing', 'math'];
  var COLOR_NAME = { connect4: { x: 'red', o: 'yellow' }, checkers: { x: 'red', o: 'black' } };
  var TAB_KEY = 'lightninglan-tab';
  var SVG_NS = 'http://www.w3.org/2000/svg';

  var opts = null;
  var state = {
    data: null,
    etag: null,
    open: null,  // the game shown on the board
    mode: 'versus',  // or 'solo'
    picks: {},  // the opponent and best-of chosen for each game, kept across redraws
    seenInvites: null,
    seenGames: {},
    busy: false,
    timer: null,
    path: { id: null, squares: [] },  // checkers: the move being picked
    live: {},  // typing race and math sprint: their play area, kept across redraws
    solo: null
  };

  function $(id) { return document.getElementById(id); }

  function el(tag, className, text) {
    var e = document.createElement(tag);
    if (className) e.className = className;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }

  function svg(tag, attrs) {
    var e = document.createElementNS(SVG_NS, tag);
    Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, String(attrs[k])); });
    return e;
  }

  function button(text, className, onClick) {
    var b = el('button', className || 'btn', text);
    b.type = 'button';
    if (onClick) b.addEventListener('click', onClick);
    return b;
  }

  function seconds(ms) { return (ms / 1000).toFixed(2) + ' s'; }

  // ----- tabs -------------------------------------------------------------------------------

  function playVisible() { return !$('play-panel').hidden; }

  function showTab(name) {
    var play = name === 'play';
    $('files-panel').hidden = play;
    $('play-panel').hidden = !play;
    $('tab-files').setAttribute('aria-selected', play ? 'false' : 'true');
    $('tab-play').setAttribute('aria-selected', play ? 'true' : 'false');
    try { localStorage.setItem(TAB_KEY, name); } catch (e) { /* private mode */ }
    if (play) refresh(true);
    if (state.solo) state.solo.setActive(play && state.mode === 'solo');
  }

  // ----- talking to LightningLAN ------------------------------------------------------------

  async function request(method, path, body) {
    var o = { method: method, headers: {}, cache: 'no-store', credentials: 'same-origin' };
    if (method !== 'GET') Object.assign(o.headers, opts.client);
    if (method === 'GET' && state.etag) o.headers['If-None-Match'] = state.etag;
    if (body !== undefined) {
      o.headers['Content-Type'] = 'application/json';
      o.body = JSON.stringify(body);
    }
    var r = await fetch(opts.api + path, o);
    if (method === 'GET' && r.status === 304) return null;
    var data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    if (!r.ok) {
      var err = new Error(data && typeof data.detail === 'string' ? data.detail : 'HTTP ' + r.status);
      err.status = r.status;
      throw err;
    }
    if (method === 'GET') state.etag = r.headers.get('ETag');
    return data;
  }

  async function refresh(now) {
    if (now) state.etag = null;
    try {
      var data = await request('GET', '');
      if (data) {
        noticeChanges(data);
        state.data = data;
        render();
      }
    } catch (e) {
      /* the page's own polling reports lost contact */
    }
  }

  function schedule() {
    clearTimeout(state.timer);
    var d = state.data;
    var busy = d && (d.invites.length || d.games.some(function (g) { return g.status === 'playing' || g.status === 'waiting'; }));
    state.timer = setTimeout(async function () {
      await refresh(false);
      schedule();
    }, playVisible() || busy ? 1000 : 3000);
  }

  async function act(path, body) {
    if (state.busy) return null;
    state.busy = true;
    try {
      var result = await request('POST', path, body);
      await refresh(true);
      return result;
    } catch (e) {
      opts.toast(e.message, 'error');
      await refresh(true);
      return null;
    } finally {
      state.busy = false;
    }
  }

  function move(g, body) { return act('/' + g.id + '/move', body); }

  // Toasts for what happened while you weren't looking, and the badge on the tab.
  function noticeChanges(data) {
    var ids = {};
    data.invites.forEach(function (inv) {
      ids[inv.id] = true;
      if (state.seenInvites && !state.seenInvites[inv.id]) {
        opts.toast(inv.from.name + ' challenges you to ' + inv.title + (inv.kind === 'rps' ? ' (best of ' + inv.best_of + ')' : '') + '!');
      }
    });
    state.seenInvites = ids;
    data.games.forEach(function (g) {
      var before = state.seenGames[g.id];
      if (before === 'waiting' && g.status === 'playing') {
        opts.toast(g.opponent.name + ' accepted: ' + g.title + ' is on!');
        if (!state.open) state.open = g.id;
      } else if (before === 'waiting' && g.status === 'declined') {
        opts.toast(g.opponent.name + ' said no thanks to ' + g.title + '.');
      } else if (before === 'playing' && g.status === 'over' && state.open !== g.id) {
        opts.toast(g.title + ' with ' + g.opponent.name + ': ' + resultText(g));
      }
      state.seenGames[g.id] = g.status;
    });
    var waiting = data.invites.length + data.games.filter(function (g) { return g.your_turn; }).length;
    var badge = $('play-badge');
    badge.hidden = !waiting;
    badge.textContent = waiting ? String(waiting) : '';
  }

  // ----- what to say ------------------------------------------------------------------------

  function resultText(g) {
    var reason = g.reason ? ' ' + g.reason + '.' : '';
    if (g.result === 'won') return 'You won!' + reason;
    if (g.result === 'lost') return g.opponent.name + ' won.' + reason;
    return 'It’s a draw.' + reason;
  }

  function statusText(g) {
    var who = g.opponent.name;
    var away = g.opponent.online ? '' : ' (' + who + ' is offline right now)';
    if (g.status === 'waiting') return 'Waiting for ' + who + ' to accept…' + away;
    if (g.status === 'declined' || g.status === 'canceled') return g.reason + '.';
    if (g.status === 'over') return resultText(g);
    var mine = g.your_turn;
    switch (g.kind) {
      case 'rps':
        if (mine) return 'Pick one!' + (g.rps.they_locked ? ' ' + who + ' has locked in.' : '') + away;
        return 'You picked ' + CHOICE[g.rps.choice].name + '. Waiting for ' + who + '…' + away;
      case 'battleship':
        if (g.battle.phase === 'setup') {
          return g.battle.ready ? 'Waiting for ' + who + ' to place their ships…' + away
            : 'Place your ships (shuffle until you like them), then press Ready.';
        }
        return (mine ? 'Your shot: pick a square in ' + who + '’s waters.' : who + ' is aiming…') + away;
      case 'typing':
        if (g.race.my_time === null) return 'Type the sentence as fast as you can: the clock starts when it appears.';
        return 'You took ' + seconds(g.race.my_time) + '. Waiting for ' + who + ' to finish…' + away;
      case 'math':
        if (g.sprint.my_times.length < g.sprint.problems.length) return 'Answer ten sums as fast as you can.';
        return 'All done! Waiting for ' + who + ' (' + g.sprint.their_count + ' of 10)…' + away;
      case 'memory':
        if (!mine) return who + ' is turning cards…' + away;
        return g.pending === null ? 'Your turn: turn over two cards.' : 'Now pick a second card.';
      case 'dots':
        return (mine ? 'Your move: draw a line. Close a box to go again.' : who + '’s move…') + away;
      case 'connect4':
        return (mine ? 'You’re ' + COLOR_NAME.connect4[g.mark] + '. Pick a column.' : who + '’s move…') + away;
      case 'checkers':
        if (!mine) return who + '’s move…' + away;
        var jump = g.allowed.length && Math.abs(Math.floor(g.allowed[0][1] / 8) - Math.floor(g.allowed[0][0] / 8)) === 2;
        return 'You’re ' + COLOR_NAME.checkers[g.mark] + '. ' + (jump ? 'You must jump.' : 'Your move.');
      default:
        if (!mine) return who + '’s move…' + away;
        var you = 'You’re ' + MARK_TEXT[g.mark] + '. ';
        if (g.kind === 'ultimate') return you + (g.forced === null ? 'Your move: any open board.' : 'Your move, in the highlighted board.');
        return you + 'Your move.';
    }
  }

  // ----- the panel --------------------------------------------------------------------------

  function render() {
    var root = $('play');
    var d = state.data;
    root.textContent = '';
    var head = el('div', 'play-head');
    head.appendChild(el('h2', null, 'While you wait'));
    var modes = el('div', 'segmented');
    [['versus', 'Head-to-head'], ['solo', 'Solo']].forEach(function (m) {
      var b = button(m[1], 'seg' + (state.mode === m[0] ? ' on' : ''), function () {
        state.mode = m[0];
        render();
        if (state.solo) state.solo.setActive(state.mode === 'solo');
      });
      b.setAttribute('aria-pressed', state.mode === m[0] ? 'true' : 'false');
      modes.appendChild(b);
    });
    head.appendChild(modes);
    root.appendChild(head);
    if (state.mode === 'solo') {
      if (!state.solo && window.LightningBlocks) state.solo = window.LightningBlocks.create();
      if (state.solo) {
        root.appendChild(state.solo.element);
        state.solo.setActive(playVisible());
      } else {
        root.appendChild(el('p', 'empty', 'Solo games are coming soon.'));
      }
      return;
    }
    if (!d) {
      root.appendChild(el('p', 'empty', 'Loading…'));
      return;
    }
    d.invites.forEach(function (inv) { root.appendChild(inviteCard(inv)); });
    var open = d.games.filter(function (g) { return g.id === state.open; })[0];
    if (open) root.appendChild(gameCard(open));
    else state.open = null;
    var others = d.games.filter(function (g) { return g !== open; });
    if (others.length) {
      root.appendChild(el('h3', 'play-sub', 'Your games'));
      var list = el('ul', 'play-games');
      others.forEach(function (g) { list.appendChild(gameRow(g)); });
      root.appendChild(list);
    }
    root.appendChild(el('h3', 'play-sub', 'Start a game'));
    if (!d.opponents.length) {
      root.appendChild(el('p', 'muted small', 'Nobody to play yet. Games need the latest LightningLAN on the other ' +
        'computer or phone, on the same network.'));
    }
    var picker = el('div', 'play-picker');
    KINDS.forEach(function (kind) { if (d.titles[kind]) picker.appendChild(kindCard(kind, d)); });
    root.appendChild(picker);
  }

  function inviteCard(inv) {
    var card = el('div', 'play-invite');
    var text = el('p');
    text.appendChild(el('strong', null, inv.from.name));
    var extra = inv.kind === 'rps' ? ' (best of ' + inv.best_of + ').' :
      inv.kind === 'typing' || inv.kind === 'math' ? '.' : (inv.you_go_first ? '. You go first.' : '. They go first.');
    text.appendChild(document.createTextNode(' challenges you to ' + inv.title + extra));
    card.appendChild(text);
    var actions = el('div', 'play-actions');
    actions.appendChild(button('Play', 'btn', async function () {
      var r = await act('/' + inv.id + '/accept');
      if (r && r.game) { state.open = r.game.id; render(); }
    }));
    actions.appendChild(button('No thanks', 'btn secondary', function () { act('/' + inv.id + '/decline'); }));
    card.appendChild(actions);
    return card;
  }

  function gameRow(g) {
    var li = el('li', 'play-game' + (g.your_turn ? ' your-turn' : ''));
    var info = el('div', 'play-game-info');
    info.appendChild(el('strong', null, g.title + ' vs ' + g.opponent.name));
    info.appendChild(el('span', 'muted small', statusText(g)));
    li.appendChild(info);
    var actions = el('div', 'play-actions');
    if (g.status === 'playing' || g.status === 'waiting' || g.status === 'over') {
      actions.appendChild(button(g.your_turn ? 'Play' : 'Open', g.your_turn ? 'btn' : 'btn secondary',
        function () { state.open = g.id; render(); }));
    }
    if (g.status !== 'playing' && g.status !== 'waiting') {
      actions.appendChild(button('Close', 'text-btn', function () { act('/' + g.id + '/close'); }));
    }
    li.appendChild(actions);
    return li;
  }

  var BOARDS = {
    tictactoe: tictactoe, ultimate: ultimate, connect4: connect4, checkers: checkers, dots: dots,
    battleship: battleship, memory: memory, rps: rps, typing: typing, math: math
  };

  function gameCard(g) {
    var card = el('div', 'play-board-card');
    var top = el('div', 'play-board-head');
    var title = el('div');
    title.appendChild(el('strong', null, g.title + (g.kind === 'rps' ? ' · best of ' + g.best_of : '')));
    title.appendChild(el('span', 'muted', ' vs ' + g.opponent.name));
    top.appendChild(title);
    top.appendChild(button('All games', 'text-btn', function () { state.open = null; render(); }));
    card.appendChild(top);
    card.appendChild(el('p', 'play-status' + (g.status === 'over' ? ' ' + g.result : ''), statusText(g)));
    if (g.score && g.kind !== 'checkers') card.appendChild(scoreLine(g));
    card.appendChild(BOARDS[g.kind](g));
    var actions = el('div', 'play-actions');
    if (g.status === 'playing') {
      actions.appendChild(button('Resign', 'text-btn danger', function () {
        if (window.confirm('Give up this game?')) act('/' + g.id + '/resign');
      }));
    } else if (g.status === 'waiting') {
      actions.appendChild(button('Withdraw', 'text-btn', function () { act('/' + g.id + '/withdraw'); }));
    } else {
      actions.appendChild(button('Rematch', 'btn', async function () {
        var r = await act('/' + g.id + '/rematch');
        if (r && r.game) { state.open = r.game.id; render(); }
      }));
      actions.appendChild(button('Close', 'text-btn', function () { state.open = null; act('/' + g.id + '/close'); }));
    }
    card.appendChild(actions);
    return card;
  }

  function scoreLine(g) {
    var unit = g.kind === 'dots' ? 'boxes' : 'pairs';
    var line = el('div', 'play-score');
    line.appendChild(el('span', 'me', 'You ' + g.score[0]));
    line.appendChild(el('span', 'muted', unit));
    line.appendChild(el('span', 'them', g.score[1] + ' ' + g.opponent.name));
    return line;
  }

  // ----- tic-tac-toe and ultimate -------------------------------------------------------------

  function cell(g, index, extraClass) {
    var mark = g.board[index];
    var b = el('button', 'ttt-cell' + (mark ? ' ' + mark : '') + (extraClass || ''), mark ? MARK_TEXT[mark] : '');
    b.type = 'button';
    var playable = g.allowed.indexOf(index) !== -1;
    b.disabled = !playable;
    if (g.last === index) b.classList.add('last');
    if (playable) {
      b.classList.add('playable');
      b.setAttribute('aria-label', 'Play here');
      b.addEventListener('click', function () { move(g, { cell: index }); });
    }
    return b;
  }

  function tictactoe(g) {
    var grid = el('div', 'ttt');
    for (var i = 0; i < 9; i++) grid.appendChild(cell(g, i, g.line.indexOf(i) !== -1 ? ' win' : ''));
    return grid;
  }

  function ultimate(g) {
    var grid = el('div', 'uttt');
    var activeBoards = {};
    g.allowed.forEach(function (m) { activeBoards[Math.floor(m / 9)] = true; });
    for (var b = 0; b < 9; b++) {
      var owner = g.small[b];
      var small = el('div', 'ttt small' + (activeBoards[b] ? ' active' : '') + (owner ? ' decided ' + owner : '') +
        (g.line.indexOf(b) !== -1 ? ' win' : ''));
      for (var c = 0; c < 9; c++) small.appendChild(cell(g, b * 9 + c));
      if (owner === 'x' || owner === 'o') small.appendChild(el('span', 'big-mark ' + owner, MARK_TEXT[owner]));
      grid.appendChild(small);
    }
    return grid;
  }

  // ----- four in a row ------------------------------------------------------------------------

  function connect4(g) {
    var grid = el('div', 'c4');
    for (var i = 0; i < 42; i++) {
      (function (index) {
        var col = index % 7;
        var playable = g.allowed.indexOf(col) !== -1;
        var mark = g.board[index];
        var b = el('button', 'c4-cell' + (playable ? ' playable' : ''));
        b.type = 'button';
        b.disabled = !playable;
        b.setAttribute('aria-label', 'Column ' + (col + 1));
        var disc = el('span', 'c4-disc' + (mark ? ' ' + mark : '') + (g.line.indexOf(index) !== -1 ? ' win' : '') +
          (g.last === index ? ' last' : ''));
        b.appendChild(disc);
        if (playable) b.addEventListener('click', function () { move(g, { cell: col }); });
        grid.appendChild(b);
      })(i);
    }
    return grid;
  }

  // ----- checkers -----------------------------------------------------------------------------

  function checkers(g) {
    var sel = state.path.id === g.id ? state.path.squares : [];
    if (!g.your_turn) sel = [];
    var paths = g.allowed;
    var matching = paths.filter(function (p) { return sel.every(function (s, i) { return p[i] === s; }); });
    var starts = {};
    paths.forEach(function (p) { starts[p[0]] = true; });
    var next = {};
    if (sel.length) matching.forEach(function (p) { if (p.length > sel.length) next[p[sel.length]] = true; });
    var flip = g.seat === 1;  // your pieces are always at the bottom
    var wrap = el('div', 'checkers-wrap');
    var counts = el('div', 'play-score');
    counts.appendChild(el('span', 'me', 'You ' + g.score[0]));
    counts.appendChild(el('span', 'muted', 'pieces'));
    counts.appendChild(el('span', 'them', g.score[1] + ' ' + g.opponent.name));
    wrap.appendChild(counts);
    var grid = el('div', 'checkers');
    for (var d = 0; d < 64; d++) {
      (function (square) {
        var r = Math.floor(square / 8);
        var c = square % 8;
        var dark = (r + c) % 2 === 1;
        var piece = g.board[square];
        var b = el('button', 'ck-square' + (dark ? ' dark' : ''));
        b.type = 'button';
        if (g.last.indexOf(square) !== -1) b.classList.add('last');
        if (piece) {
          var p = el('span', 'ck-piece ' + piece.toLowerCase() + (piece === piece.toUpperCase() ? ' king' : ''),
            piece === piece.toUpperCase() ? '♛' : '');
          b.appendChild(p);
        }
        var canStart = starts[square] && !sel.length;
        var selected = sel.length && sel[sel.length - 1] === square;
        if (canStart || (sel.length && square === sel[0])) b.classList.add('movable');
        if (selected) b.classList.add('selected');
        if (next[square]) b.classList.add('target');
        b.disabled = !(starts[square] || next[square]);
        b.addEventListener('click', function () { pickSquare(g, square, sel, next, matching, starts); });
        grid.appendChild(b);
      })(flip ? 63 - d : d);
    }
    wrap.appendChild(grid);
    return wrap;
  }

  function pickSquare(g, square, sel, next, matching, starts) {
    if (!next[square]) {
      if (starts[square]) {
        state.path = { id: g.id, squares: [square] };  // pick up a piece (or another one)
        render();
      }
      return;
    }
    var path = sel.concat([square]);
    var complete = matching.filter(function (p) {
      return p.length === path.length && p.every(function (s, i) { return s === path[i]; });
    })[0];
    state.path = { id: g.id, squares: complete ? [] : path };
    if (complete) move(g, { path: complete });
    else render();
  }

  // ----- dots and boxes -------------------------------------------------------------------------

  function dots(g) {
    var size = 5;
    var gap = 60;
    var pad = 14;
    var full = pad * 2 + gap * size;
    var pic = svg('svg', { viewBox: '0 0 ' + full + ' ' + full, 'class': 'dots', role: 'img', 'aria-label': 'Dots and boxes' });
    g.boxes.forEach(function (owner, b) {
      if (!owner) return;
      var r = Math.floor(b / size);
      var c = b % size;
      pic.appendChild(svg('rect', { x: pad + c * gap + 3, y: pad + r * gap + 3, width: gap - 6, height: gap - 6, rx: 6,
        'class': 'dots-box ' + (owner === g.mark ? 'me' : 'them') }));
    });
    var allowed = {};
    g.allowed.forEach(function (l) { allowed[l] = true; });
    for (var line = 0; line < 60; line++) {
      (function (index) {
        var x1, y1, x2, y2;
        if (index < 30) {
          var r = Math.floor(index / size);
          var c = index % size;
          x1 = pad + c * gap; y1 = pad + r * gap; x2 = x1 + gap; y2 = y1;
        } else {
          var v = index - 30;
          var rr = Math.floor(v / (size + 1));
          var cc = v % (size + 1);
          x1 = pad + cc * gap; y1 = pad + rr * gap; x2 = x1; y2 = y1 + gap;
        }
        var owner = g.lines[index];
        if (owner) {
          pic.appendChild(svg('line', { x1: x1, y1: y1, x2: x2, y2: y2,
            'class': 'dots-line ' + (owner === g.mark ? 'me' : 'them') + (g.last === index ? ' last' : '') }));
        } else if (allowed[index]) {
          var hit = svg('line', { x1: x1, y1: y1, x2: x2, y2: y2, 'class': 'dots-slot', tabindex: 0, role: 'button',
            'aria-label': 'Draw this line' });
          hit.addEventListener('click', function () { move(g, { cell: index }); });
          hit.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') move(g, { cell: index }); });
          pic.appendChild(hit);
        }
      })(line);
    }
    for (var r2 = 0; r2 <= size; r2++) {
      for (var c2 = 0; c2 <= size; c2++) pic.appendChild(svg('circle', { cx: pad + c2 * gap, cy: pad + r2 * gap, r: 5, 'class': 'dots-dot' }));
    }
    return pic;
  }

  // ----- battleship ---------------------------------------------------------------------------

  function grid10(className, label) {
    var box = el('div', 'bs-side');
    box.appendChild(el('span', 'bs-label', label));
    var grid = el('div', 'bs-grid ' + className);
    box.appendChild(grid);
    return { box: box, grid: grid };
  }

  function battleship(g) {
    var b = g.battle;
    var wrap = el('div', 'bs');
    var shipAt = {};
    b.ships.forEach(function (ship, i) { ship.forEach(function (c) { shipAt[c] = i; }); });
    var incoming = {};
    b.incoming.forEach(function (s) { incoming[s.cell] = s.result || 'pending'; });
    var mine = grid10('mine', 'Your fleet');
    for (var c = 0; c < 100; c++) {
      var sq = el('span', 'bs-cell' + (shipAt[c] !== undefined ? ' ship' : '') + (incoming[c] ? ' ' + incoming[c] : ''));
      mine.grid.appendChild(sq);
    }
    if (b.phase === 'setup') {
      wrap.appendChild(mine.box);
      var setup = el('div', 'play-actions');
      if (!b.ready) {
        setup.appendChild(button('Shuffle ships', 'btn secondary', function () { move(g, { shuffle: true }); }));
        setup.appendChild(button('Ready', 'btn', function () { move(g, { ready: true }); }));
      }
      setup.appendChild(el('span', 'muted small', b.they_ready ? g.opponent.name + ' is ready.' : g.opponent.name + ' is placing ships…'));
      wrap.appendChild(setup);
      return wrap;
    }
    var shots = {};
    b.outgoing.forEach(function (s) { shots[s.cell] = s.result || 'pending'; });
    var revealed = {};
    b.their_ships.forEach(function (ship) { ship.forEach(function (c) { revealed[c] = true; }); });
    var theirs = grid10('theirs', g.opponent.name + '’s waters');
    for (var t = 0; t < 100; t++) {
      (function (cellIndex) {
        var cls = 'bs-cell' + (shots[cellIndex] ? ' ' + shots[cellIndex] : '') + (revealed[cellIndex] ? ' ship' : '');
        if (g.your_turn && !shots[cellIndex]) {
          var btn = el('button', cls + ' aim');
          btn.type = 'button';
          btn.setAttribute('aria-label', 'Fire at ' + 'ABCDEFGHIJ'[Math.floor(cellIndex / 10)] + (cellIndex % 10 + 1));
          btn.addEventListener('click', function () { move(g, { shot: cellIndex }); });
          theirs.grid.appendChild(btn);
        } else {
          theirs.grid.appendChild(el('span', cls));
        }
      })(t);
    }
    wrap.appendChild(theirs.box);
    wrap.appendChild(mine.box);
    wrap.appendChild(el('p', 'muted small', 'Ships sunk: ' + b.sunk[0] + ' of 5 of theirs, ' + b.sunk[1] + ' of 5 of yours.'));
    return wrap;
  }

  // ----- memory match -------------------------------------------------------------------------

  function memory(g) {
    var grid = el('div', 'memory');
    g.cards.forEach(function (picture, i) {
      var owner = g.matched[i];
      var up = picture !== null;
      var b = el('button', 'mem-card' + (up ? ' up' : '') + (owner ? ' matched ' + (owner === g.mark ? 'me' : 'them') : '') +
        (g.shown.indexOf(i) !== -1 ? ' miss' : ''), up ? CARDS[picture] : '?');
      b.type = 'button';
      var playable = g.allowed.indexOf(i) !== -1 && !up;
      b.disabled = !playable;
      if (playable) b.addEventListener('click', function () { move(g, { cell: i }); });
      grid.appendChild(b);
    });
    return grid;
  }

  // ----- rock-paper-scissors ------------------------------------------------------------------

  function rps(g) {
    var box = el('div', 'rps');
    var score = el('div', 'rps-score');
    score.appendChild(el('span', null, 'You ' + g.rps.score[0]));
    score.appendChild(el('span', 'muted', 'first to ' + g.rps.target));
    score.appendChild(el('span', null, g.rps.score[1] + ' ' + g.opponent.name));
    box.appendChild(score);
    if (g.your_turn) {
      var picks = el('div', 'rps-picks');
      Object.keys(CHOICE).forEach(function (choice) {
        var b = button('', 'rps-pick', function () { move(g, { choice: choice }); });
        b.appendChild(el('span', 'rps-icon', CHOICE[choice].icon));
        b.appendChild(el('span', null, CHOICE[choice].name));
        picks.appendChild(b);
      });
      box.appendChild(picks);
    }
    if (g.rps.rounds.length) {
      var rounds = el('ol', 'rps-rounds');
      g.rps.rounds.slice().reverse().forEach(function (r) {
        var li = el('li', r.result);
        li.appendChild(el('span', 'rps-icon', CHOICE[r.mine].icon));
        li.appendChild(el('span', null, r.result === 'won' ? 'beats' : r.result === 'lost' ? 'loses to' : 'ties'));
        li.appendChild(el('span', 'rps-icon', CHOICE[r.theirs].icon));
        rounds.appendChild(li);
      });
      box.appendChild(rounds);
    }
    return box;
  }

  // ----- typing race and math sprint (timed here, on this device) ----------------------------------

  function countdown(area, then) {
    var n = 3;
    area.textContent = '';
    var big = el('div', 'race-count', String(n));
    area.appendChild(big);
    var t = setInterval(function () {
      n--;
      if (n > 0) big.textContent = String(n);
      else { clearInterval(t); then(); }
    }, 700);
  }

  function liveArea(g, make) {
    var live = state.live[g.id];
    if (!live) live = state.live[g.id] = make();
    return live;
  }

  function typing(g) {
    var r = g.race;
    if (r.my_time !== null || g.status !== 'playing') {
      delete state.live[g.id];
      var box = el('div', 'race');
      box.appendChild(el('p', 'race-sentence done', r.sentence));
      var times = el('div', 'play-score');
      times.appendChild(el('span', 'me', 'You ' + (r.my_time !== null ? seconds(r.my_time) : '—')));
      times.appendChild(el('span', 'them', g.opponent.name + ' ' + (r.their_time !== null ? seconds(r.their_time) : r.they_done ? 'finished' : '…')));
      box.appendChild(times);
      return box;
    }
    return liveArea(g, function () {
      var area = el('div', 'race');
      area.appendChild(button('Start', 'btn', function () {
        countdown(area, function () {
          area.textContent = '';
          var shown = el('p', 'race-sentence');
          var spans = r.sentence.split('').map(function (ch) { var s = el('span', null, ch); shown.appendChild(s); return s; });
          var input = el('textarea', 'race-input');
          input.setAttribute('rows', '3');
          input.setAttribute('autocomplete', 'off');
          input.setAttribute('autocapitalize', 'off');
          input.setAttribute('spellcheck', 'false');
          input.setAttribute('aria-label', 'Type the sentence');
          var clock = el('div', 'race-clock', '0.00 s');
          area.appendChild(shown);
          area.appendChild(input);
          area.appendChild(clock);
          var started = performance.now();
          var tick = setInterval(function () { clock.textContent = seconds(performance.now() - started); }, 50);
          input.addEventListener('input', function () {
            var typed = input.value;
            spans.forEach(function (s, i) {
              s.className = i < typed.length ? (typed[i] === r.sentence[i] ? 'ok' : 'bad') : '';
            });
            if (typed === r.sentence) {
              clearInterval(tick);
              var ms = Math.round(performance.now() - started);
              input.disabled = true;
              clock.textContent = seconds(ms);
              move(g, { time_ms: ms });
            }
          });
          input.focus();
        });
      }));
      return area;
    });
  }

  function math(g) {
    var s = g.sprint;
    var total = s.problems.length;
    if (s.my_times.length >= total || g.status !== 'playing') {
      delete state.live[g.id];
      var box = el('div', 'race');
      var list = el('ol', 'sprint-list');
      s.problems.forEach(function (p, i) {
        var mine = s.my_times[i];
        var theirs = s.their_times[i];
        var li = el('li', theirs === undefined || mine === undefined ? '' : mine < theirs ? 'won' : mine > theirs ? 'lost' : '');
        li.appendChild(el('span', null, p.question + ' = ' + p.answer));
        li.appendChild(el('span', 'muted', (mine !== undefined ? seconds(mine) : '—') + ' vs ' +
          (theirs !== undefined ? seconds(theirs) : '…')));
        list.appendChild(li);
      });
      box.appendChild(list);
      return box;
    }
    var live = liveArea(g, function () {
      var area = el('div', 'race');
      area.dataset.index = String(s.my_times.length);
      area.appendChild(button('Start', 'btn', function () {
        countdown(area, function () {
          area.textContent = '';
          var counter = el('div', 'muted small');
          var question = el('div', 'sprint-question');
          var input = el('input', 'sprint-input');
          input.setAttribute('inputmode', 'numeric');
          input.setAttribute('autocomplete', 'off');
          input.setAttribute('aria-label', 'Your answer');
          var them = el('div', 'muted small sprint-them');
          [counter, question, input, them].forEach(function (x) { area.appendChild(x); });
          var shownAt = 0;
          function show() {
            var k = Number(area.dataset.index);
            if (k >= total) { question.textContent = 'Done!'; input.disabled = true; return; }
            counter.textContent = 'Sum ' + (k + 1) + ' of ' + total;
            question.textContent = s.problems[k].question + ' = ?';
            input.value = '';
            shownAt = performance.now();
            input.focus();
          }
          input.addEventListener('input', function () {
            var k = Number(area.dataset.index);
            if (k < total && input.value.trim() !== '' && Number(input.value) === s.problems[k].answer) {
              var ms = Math.max(100, Math.round(performance.now() - shownAt));
              area.dataset.index = String(k + 1);
              move(g, { time_ms: ms });
              show();
            }
          });
          show();
        });
      }));
      return area;
    });
    var themLine = live.querySelector('.sprint-them');
    if (themLine) themLine.textContent = g.opponent.name + ': ' + s.their_count + ' of ' + total;
    return live;
  }

  // ----- starting a game ------------------------------------------------------------------------

  function kindCard(kind, d) {
    var card = el('div', 'play-kind');
    card.appendChild(el('strong', null, d.titles[kind]));
    card.appendChild(el('p', 'muted small', ABOUT[kind]));
    var pick = state.picks[kind] || (state.picks[kind] = { peer: '', bestOf: 3 });
    var row = el('div', 'play-actions');
    var who = el('select', 'select');
    who.setAttribute('aria-label', 'Who to play ' + d.titles[kind] + ' with');
    if (!d.opponents.some(function (o) { return o.peer_id === pick.peer; })) pick.peer = d.opponents.length ? d.opponents[0].peer_id : '';
    d.opponents.forEach(function (o) {
      var option = el('option', null, o.name);
      option.value = o.peer_id;
      option.selected = o.peer_id === pick.peer;
      who.appendChild(option);
    });
    who.disabled = !d.opponents.length;
    who.addEventListener('change', function () { pick.peer = who.value; });
    row.appendChild(who);
    if (kind === 'rps') {
      var best = el('select', 'select narrow');
      best.setAttribute('aria-label', 'Best of');
      d.best_of.forEach(function (n) {
        var option = el('option', null, 'Best of ' + n);
        option.value = String(n);
        option.selected = n === pick.bestOf;
        best.appendChild(option);
      });
      best.addEventListener('change', function () { pick.bestOf = Number(best.value); });
      row.appendChild(best);
    }
    var go = button('Challenge', 'btn', async function () {
      if (!pick.peer) return;
      var r = await act('/invite', { peer_id: pick.peer, kind: kind, best_of: pick.bestOf });
      if (r && r.id) { state.open = r.id; render(); }
    });
    go.disabled = !d.opponents.length;
    row.appendChild(go);
    card.appendChild(row);
    return card;
  }

  // ----- starting ---------------------------------------------------------------------------

  function mount(options) {
    opts = options;
    $('tab-files').addEventListener('click', function () { showTab('files'); });
    $('tab-play').addEventListener('click', function () { showTab('play'); });
    var tab = 'files';
    try { tab = localStorage.getItem(TAB_KEY) || 'files'; } catch (e) { /* private mode */ }
    showTab(tab === 'play' ? 'play' : 'files');
    render();
    refresh(true).then(schedule);
  }

  window.LightningPlay = { mount: mount };
})();
