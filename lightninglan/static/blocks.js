/* Falling blocks: the solo game in While you wait (a Tetris-style game). Plain ES2017.
 *
 * play.js calls LightningBlocks.create() and shows its element in the Solo section. Everything
 * happens on this device; the best score is remembered in this browser.
 *
 * Keys: left and right move, up (or X) turns clockwise, Z turns the other way, down drops faster,
 * space drops all the way, P pauses. The buttons under the board do the same on a phone.
 */
(function () {
  'use strict';

  var COLS = 10;
  var ROWS = 20;
  var BEST_KEY = 'lightninglan-blocks-best';
  var PIECES = {
    I: ['....', 'IIII', '....', '....'],
    O: ['OO', 'OO'],
    T: ['.T.', 'TTT', '...'],
    S: ['.SS', 'SS.', '...'],
    Z: ['ZZ.', '.ZZ', '...'],
    J: ['J..', 'JJJ', '...'],
    L: ['..L', 'LLL', '...']
  };
  var COLORS = { I: '#22d3ee', O: '#facc15', T: '#a855f7', S: '#22c55e', Z: '#ef4444', J: '#3b82f6', L: '#f97316' };
  var LINE_POINTS = [0, 100, 300, 500, 800];
  var KICKS = [0, -1, 1, -2, 2];

  function el(tag, className, text) {
    var e = document.createElement(tag);
    if (className) e.className = className;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }

  function button(text, label, onClick) {
    var b = el('button', 'btn secondary blocks-key', text);
    b.type = 'button';
    b.setAttribute('aria-label', label);
    b.addEventListener('click', function (e) { e.preventDefault(); onClick(); });
    return b;
  }

  function matrix(kind) { return PIECES[kind].map(function (row) { return row.split('').map(function (c) { return c === '.' ? '' : c; }); }); }

  function rotate(m, clockwise) {
    var n = m.length;
    var out = [];
    for (var r = 0; r < n; r++) {
      out.push([]);
      for (var c = 0; c < n; c++) out[r].push(clockwise ? m[n - 1 - c][r] : m[c][n - 1 - r]);
    }
    return out;
  }

  function loadBest() {
    try { return Number(localStorage.getItem(BEST_KEY)) || 0; } catch (e) { return 0; }
  }

  function saveBest(n) {
    try { localStorage.setItem(BEST_KEY, String(n)); } catch (e) { /* private mode */ }
  }

  function create() {
    var root = el('div', 'blocks');
    var board = el('canvas', 'blocks-board');
    board.setAttribute('role', 'img');
    board.setAttribute('aria-label', 'Falling blocks board');
    var side = el('div', 'blocks-side');
    var nextCanvas = el('canvas', 'blocks-next');
    var stats = el('div', 'blocks-stats');
    var message = el('p', 'blocks-message', 'Stack the falling blocks and fill rows to clear them.');
    var startButton = el('button', 'btn', 'Start');
    startButton.type = 'button';
    side.appendChild(el('span', 'muted small', 'Next'));
    side.appendChild(nextCanvas);
    side.appendChild(stats);
    side.appendChild(startButton);
    var area = el('div', 'blocks-area');
    area.appendChild(board);
    area.appendChild(side);
    root.appendChild(message);
    root.appendChild(area);
    var keys = el('div', 'blocks-keys');
    root.appendChild(keys);
    root.appendChild(el('p', 'muted small blocks-help', 'Keys: ← → move, ↑ turn, ↓ faster, space drops, P pauses.'));

    var g = { grid: [], piece: null, next: null, bag: [], score: 0, lines: 0, level: 1, running: false, over: false,
      paused: false, best: loadBest(), active: false, last: 0, acc: 0, cell: 24 };

    function newGrid() {
      g.grid = [];
      for (var r = 0; r < ROWS; r++) { g.grid.push([]); for (var c = 0; c < COLS; c++) g.grid[r].push(''); }
    }

    function fromBag() {
      if (!g.bag.length) {
        g.bag = Object.keys(PIECES);
        for (var i = g.bag.length - 1; i > 0; i--) {
          var j = Math.floor(Math.random() * (i + 1));
          var t = g.bag[i]; g.bag[i] = g.bag[j]; g.bag[j] = t;
        }
      }
      return g.bag.pop();
    }

    function collides(m, x, y) {
      for (var r = 0; r < m.length; r++) {
        for (var c = 0; c < m[r].length; c++) {
          if (!m[r][c]) continue;
          var bx = x + c;
          var by = y + r;
          if (bx < 0 || bx >= COLS || by >= ROWS) return true;
          if (by >= 0 && g.grid[by][bx]) return true;
        }
      }
      return false;
    }

    function spawn() {
      var kind = g.next || fromBag();
      g.next = fromBag();
      var m = matrix(kind);
      g.piece = { kind: kind, m: m, x: Math.floor((COLS - m.length) / 2), y: kind === 'I' ? -1 : 0 };
      if (collides(g.piece.m, g.piece.x, g.piece.y)) {
        g.over = true;
        g.running = false;
        g.piece = null;
        if (g.score > g.best) { g.best = g.score; saveBest(g.best); }
        message.textContent = 'Game over! ' + g.score + ' points.' + (g.score && g.score === g.best ? ' A new best!' : '');
        startButton.textContent = 'Play again';
      }
    }

    function start() {
      newGrid();
      g.bag = [];
      g.next = null;
      g.score = 0;
      g.lines = 0;
      g.level = 1;
      g.over = false;
      g.paused = false;
      g.running = true;
      g.acc = 0;
      message.textContent = 'Fill whole rows to clear them. Every 10 rows, it gets faster.';
      startButton.textContent = 'Pause';
      spawn();
      draw();
    }

    function lock() {
      var p = g.piece;
      p.m.forEach(function (row, r) {
        row.forEach(function (v, c) { if (v && p.y + r >= 0) g.grid[p.y + r][p.x + c] = v; });
      });
      var cleared = 0;
      for (var r = ROWS - 1; r >= 0; r--) {
        if (g.grid[r].every(function (v) { return v; })) {
          g.grid.splice(r, 1);
          g.grid.unshift(new Array(COLS).fill(''));
          cleared++;
          r++;
        }
      }
      if (cleared) {
        g.score += LINE_POINTS[cleared] * g.level;
        g.lines += cleared;
        g.level = 1 + Math.floor(g.lines / 10);
      }
      spawn();
    }

    function shift(dx) {
      if (!playing()) return;
      if (!collides(g.piece.m, g.piece.x + dx, g.piece.y)) { g.piece.x += dx; draw(); }
    }

    function turn(clockwise) {
      if (!playing()) return;
      var m = rotate(g.piece.m, clockwise);
      for (var i = 0; i < KICKS.length; i++) {
        if (!collides(m, g.piece.x + KICKS[i], g.piece.y)) {
          g.piece.m = m;
          g.piece.x += KICKS[i];
          draw();
          return;
        }
      }
    }

    function fall(byPlayer) {
      if (!playing()) return false;
      if (!collides(g.piece.m, g.piece.x, g.piece.y + 1)) {
        g.piece.y++;
        if (byPlayer) g.score += 1;
        return true;
      }
      lock();
      return false;
    }

    function drop() {
      if (!playing()) return;
      var n = 0;
      while (!collides(g.piece.m, g.piece.x, g.piece.y + 1)) { g.piece.y++; n++; }
      g.score += 2 * n;
      lock();
      draw();
    }

    function playing() { return g.running && !g.paused && !g.over && g.piece; }

    function togglePause() {
      if (!g.running && !g.over) return;
      if (g.over || !g.running) { start(); return; }
      g.paused = !g.paused;
      startButton.textContent = g.paused ? 'Resume' : 'Pause';
      message.textContent = g.paused ? 'Paused.' : 'Fill whole rows to clear them. Every 10 rows, it gets faster.';
      draw();
    }

    startButton.addEventListener('click', function () {
      if (!g.running || g.over) start();
      else togglePause();
    });

    [['◀', 'Move left', function () { shift(-1); }], ['⟲', 'Turn left', function () { turn(false); }],
      ['⟳', 'Turn right', function () { turn(true); }], ['▼', 'Down', function () { fall(true); draw(); }],
      ['⤓', 'Drop', drop], ['▶', 'Move right', function () { shift(1); }]].forEach(function (k) {
      keys.appendChild(button(k[0], k[1], k[2]));
    });

    document.addEventListener('keydown', function (e) {
      if (!g.active || !g.running) return;
      var t = e.target;
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.tagName === 'SELECT')) return;
      var handled = true;
      switch (e.key) {
        case 'ArrowLeft': shift(-1); break;
        case 'ArrowRight': shift(1); break;
        case 'ArrowDown': fall(true); draw(); break;
        case 'ArrowUp': case 'x': case 'X': turn(true); break;
        case 'z': case 'Z': turn(false); break;
        case ' ': drop(); break;
        case 'p': case 'P': case 'Escape': togglePause(); break;
        default: handled = false;
      }
      if (handled) e.preventDefault();
    });

    function size() {
      var width = Math.min(root.clientWidth || 360, 560);
      var cell = Math.floor(Math.min((width - 120) / COLS, (window.innerHeight * 0.62) / ROWS));
      g.cell = Math.max(14, Math.min(30, cell));
      var ratio = window.devicePixelRatio || 1;
      board.width = COLS * g.cell * ratio;
      board.height = ROWS * g.cell * ratio;
      board.style.width = COLS * g.cell + 'px';
      board.style.height = ROWS * g.cell + 'px';
      nextCanvas.width = 4 * 18 * ratio;
      nextCanvas.height = 4 * 18 * ratio;
      nextCanvas.style.width = '72px';
      nextCanvas.style.height = '72px';
    }

    function square(ctx, x, y, s, color) {
      ctx.fillStyle = color;
      ctx.fillRect(x + 1, y + 1, s - 2, s - 2);
      ctx.fillStyle = 'rgba(255,255,255,0.25)';
      ctx.fillRect(x + 1, y + 1, s - 2, Math.max(2, s / 6));
    }

    function draw() {
      var ratio = window.devicePixelRatio || 1;
      var ctx = board.getContext('2d');
      var s = g.cell;
      ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      ctx.fillStyle = '#0f172a';
      ctx.fillRect(0, 0, COLS * s, ROWS * s);
      ctx.strokeStyle = 'rgba(255,255,255,0.06)';
      for (var c = 1; c < COLS; c++) { ctx.beginPath(); ctx.moveTo(c * s, 0); ctx.lineTo(c * s, ROWS * s); ctx.stroke(); }
      for (var r = 1; r < ROWS; r++) { ctx.beginPath(); ctx.moveTo(0, r * s); ctx.lineTo(COLS * s, r * s); ctx.stroke(); }
      g.grid.forEach(function (row, rr) {
        row.forEach(function (v, cc) { if (v) square(ctx, cc * s, rr * s, s, COLORS[v]); });
      });
      var p = g.piece;
      if (p) {
        var ghost = p.y;
        while (!collides(p.m, p.x, ghost + 1)) ghost++;
        p.m.forEach(function (row, rr) {
          row.forEach(function (v, cc) {
            if (!v) return;
            if (ghost + rr >= 0) {
              ctx.strokeStyle = 'rgba(255,255,255,0.35)';
              ctx.strokeRect((p.x + cc) * s + 2, (ghost + rr) * s + 2, s - 4, s - 4);
            }
            if (p.y + rr >= 0) square(ctx, (p.x + cc) * s, (p.y + rr) * s, s, COLORS[v]);
          });
        });
      }
      if (g.paused || g.over || !g.running) {
        ctx.fillStyle = 'rgba(15,23,42,0.55)';
        ctx.fillRect(0, 0, COLS * s, ROWS * s);
      }
      var nctx = nextCanvas.getContext('2d');
      nctx.setTransform(ratio, 0, 0, ratio, 0, 0);
      nctx.clearRect(0, 0, 72, 72);
      if (g.next) {
        var m = matrix(g.next);
        var off = (4 - m.length) * 9;
        m.forEach(function (row, rr) { row.forEach(function (v, cc) { if (v) square(nctx, off + cc * 18, off + rr * 18, 18, COLORS[v]); }); });
      }
      stats.textContent = '';
      [['Score', g.score], ['Rows', g.lines], ['Level', g.level], ['Best', Math.max(g.best, g.score)]].forEach(function (pair) {
        var line = el('div', 'blocks-stat');
        line.appendChild(el('span', 'muted small', pair[0]));
        line.appendChild(el('strong', null, pair[1]));
        stats.appendChild(line);
      });
    }

    function frame(now) {
      if (!g.active) return;
      var dt = Math.min(100, now - (g.last || now));
      g.last = now;
      if (playing()) {
        g.acc += dt;
        var interval = Math.max(70, 800 - (g.level - 1) * 70);
        if (g.acc >= interval) {
          g.acc = 0;
          fall(false);
          draw();
        }
      }
      requestAnimationFrame(frame);
    }

    newGrid();
    return {
      element: root,
      setActive: function (on) {
        if (on === g.active) return;
        g.active = on;
        if (on) {
          size();
          draw();
          g.last = 0;
          requestAnimationFrame(frame);
        } else if (g.running && !g.paused && !g.over) {
          togglePause();  // leaving the tab pauses the game
        }
      }
    };
  }

  window.LightningBlocks = { create: create };
})();
