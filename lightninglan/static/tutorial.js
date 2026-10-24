/* LightningLAN's tutorial: a guided tour of the page. Plain ES2017, no build step.
 *
 * Each step dims the page, puts a spotlight on one element and explains it in a small card.
 * Next moves on and Back goes back; a step with `click` asks for a click on the spotlighted element
 * itself (Enter does it too). Skip, or Esc, ends the tour. Nothing else on the page can be
 * clicked meanwhile. app.js has the steps: window.LightningTutorial.start(steps, { onEnd }).
 *
 * A step: { target: CSS selector or function returning an element (none: the card in the middle),
 *           title, text (a string or a function returning one), click: true (see above),
 *           before: function run before it shows (to open the right tab) }.
 * A step whose element isn't on the page is shown in the middle instead.
 */
(function () {
  'use strict';

  var PAD = 6;  // room around the spotlighted element
  var GAP = 14;  // between it and the card
  var MARGIN = 16;  // the card stays this far from the window's edges
  var tour = null;

  function el(tag, className, text) {
    var e = document.createElement(tag);
    if (className) e.className = className;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }

  function button(label, className, onClick) {
    var b = el('button', className, label);
    b.type = 'button';
    b.addEventListener('click', onClick);
    return b;
  }

  function targetOf(step) {
    var t = typeof step.target === 'function' ? step.target() : step.target ? document.querySelector(step.target) : null;
    if (!t) return null;
    var r = t.getBoundingClientRect();
    return r.width || r.height ? t : null;  // hidden: nothing to point at
  }

  function setBox(node, x, y, w, h) {
    node.style.left = x + 'px';
    node.style.top = y + 'px';
    node.style.width = Math.max(0, w) + 'px';
    node.style.height = Math.max(0, h) + 'px';
  }

  // Puts the spotlight on the step's element and the card next to it (below if there's room, else above).
  function place() {
    if (!tour) return;
    var vw = document.documentElement.clientWidth;
    var vh = document.documentElement.clientHeight;
    var card = tour.card;
    var cw = card.offsetWidth;
    var ch = card.offsetHeight;
    var t = tour.target;
    if (!t || !t.isConnected) {
      tour.spot.hidden = true;
      setBox(tour.shades[0], 0, 0, vw, vh);
      tour.shades[0].classList.add('dim');
      for (var i = 1; i < 4; i++) setBox(tour.shades[i], 0, 0, 0, 0);
      card.style.left = Math.max(MARGIN, (vw - cw) / 2) + 'px';
      card.style.top = Math.max(MARGIN, (vh - ch) / 2) + 'px';
      return;
    }
    var r = t.getBoundingClientRect();
    var x = Math.max(0, r.left - PAD);
    var y = Math.max(0, r.top - PAD);
    var right = Math.min(vw, r.right + PAD);
    var bottom = Math.min(vh, r.bottom + PAD);
    tour.spot.hidden = false;
    tour.shades[0].classList.remove('dim');
    setBox(tour.spot, x, y, right - x, bottom - y);
    // Around the spotlight, see-through: they only stop clicks reaching the page.
    setBox(tour.shades[0], 0, 0, vw, y);
    setBox(tour.shades[1], 0, bottom, vw, vh - bottom);
    setBox(tour.shades[2], 0, y, x, bottom - y);
    setBox(tour.shades[3], right, y, vw - right, bottom - y);
    var top = bottom + GAP;
    if (top + ch > vh - MARGIN) top = y - GAP - ch >= MARGIN ? y - GAP - ch : Math.max(MARGIN, vh - ch - MARGIN);
    var left = Math.min(Math.max(MARGIN, (x + right) / 2 - cw / 2), vw - cw - MARGIN);
    card.style.left = Math.max(MARGIN, left) + 'px';
    card.style.top = top + 'px';
  }

  function show(index) {
    var steps = tour.steps;
    if (index < 0) index = 0;
    if (index >= steps.length) { stop(true); return; }
    var step = steps[index];
    if (step.before) step.before();
    unhookTarget();
    tour.index = index;
    tour.target = targetOf(step);
    var last = index === steps.length - 1;
    tour.count.textContent = (index + 1) + ' of ' + steps.length;
    tour.title.textContent = step.title;
    tour.text.textContent = typeof step.text === 'function' ? step.text() : step.text;
    var clicking = !!(step.click && tour.target);
    tour.hint.hidden = !clicking;
    tour.hint.textContent = clicking ? (step.hint || 'Click the highlighted button to carry on.') : '';
    tour.back.hidden = index === 0;
    tour.next.hidden = clicking;
    tour.next.textContent = index === 0 ? 'Show me around' : last ? 'Finish' : 'Next';
    tour.skip.hidden = last;
    tour.spot.classList.toggle('clickable', clicking);
    if (clicking) {
      tour.onTargetClick = function () { setTimeout(function () { if (tour) show(tour.index + 1); }, 0); };
      tour.target.addEventListener('click', tour.onTargetClick);
    }
    if (tour.target) tour.target.scrollIntoView({ block: 'nearest', inline: 'nearest' });
    place();
    (clicking ? tour.card : tour.next).focus({ preventScroll: true });
  }

  function unhookTarget() {
    if (tour && tour.target && tour.onTargetClick) tour.target.removeEventListener('click', tour.onTargetClick);
    if (tour) tour.onTargetClick = null;
  }

  function onKey(e) {
    if (!tour) return;
    if (e.key === 'Escape') {
      e.preventDefault();
      stop(true);
    } else if (e.key === 'Enter' && !(e.target instanceof HTMLButtonElement)) {
      e.preventDefault();
      if (tour.onTargetClick) tour.target.click();
      else show(tour.index + 1);
    } else if (e.key === 'ArrowRight' && !tour.onTargetClick) {
      e.preventDefault();
      show(tour.index + 1);
    } else if (e.key === 'ArrowLeft' && tour.index > 0) {
      e.preventDefault();
      show(tour.index - 1);
    } else if (e.key === 'Tab') {  // keep the keyboard in the card
      var buttons = Array.prototype.filter.call(tour.card.querySelectorAll('button'), function (b) { return !b.hidden; });
      if (!buttons.length) return;
      e.preventDefault();
      var at = buttons.indexOf(document.activeElement);
      var next = at === -1 ? 0 : (at + (e.shiftKey ? buttons.length - 1 : 1)) % buttons.length;
      buttons[next].focus();
    }
  }

  function stop(ended) {
    if (!tour) return;
    var t = tour;
    unhookTarget();
    clearInterval(t.timer);
    document.removeEventListener('keydown', onKey, true);
    window.removeEventListener('resize', place);
    window.removeEventListener('scroll', place, true);
    t.root.remove();
    tour = null;
    if (t.focus && t.focus.isConnected) t.focus.focus({ preventScroll: true });
    if (ended && t.onEnd) t.onEnd();
  }

  function start(steps, opts) {
    stop(false);
    opts = opts || {};
    var root = el('div', 'tour');
    var shades = [0, 1, 2, 3].map(function () { return root.appendChild(el('div', 'tour-shade')); });
    var spot = root.appendChild(el('div', 'tour-spot'));
    var card = root.appendChild(el('div', 'tour-card'));
    card.setAttribute('role', 'dialog');
    card.setAttribute('aria-modal', 'true');
    card.setAttribute('aria-labelledby', 'tour-title');
    card.setAttribute('aria-describedby', 'tour-text');
    card.tabIndex = -1;
    var count = card.appendChild(el('div', 'tour-count'));
    count.setAttribute('aria-hidden', 'true');
    var title = card.appendChild(el('h2', 'tour-title'));
    title.id = 'tour-title';
    var text = card.appendChild(el('p', 'tour-text'));
    text.id = 'tour-text';
    var hint = card.appendChild(el('p', 'tour-hint'));
    hint.setAttribute('aria-live', 'polite');
    var actions = card.appendChild(el('div', 'tour-actions'));
    tour = {
      steps: steps, index: -1, root: root, shades: shades, spot: spot, card: card, count: count, title: title,
      text: text, hint: hint, target: null, onTargetClick: null, onEnd: opts.onEnd, focus: document.activeElement,
      skip: button('Skip tutorial', 'text-btn tour-skip', function () { stop(true); }),
      back: button('Back', 'btn secondary', function () { show(tour.index - 1); }),
      next: button('Next', 'btn', function () { show(tour.index + 1); }),
      timer: setInterval(place, 300)  // the page keeps changing under it (new files, peers, downloads)
    };
    actions.appendChild(tour.skip);
    actions.appendChild(tour.back);
    actions.appendChild(tour.next);
    document.body.appendChild(root);
    document.addEventListener('keydown', onKey, true);
    window.addEventListener('resize', place);
    window.addEventListener('scroll', place, true);
    show(0);
  }

  window.LightningTutorial = { start: start, stop: function () { stop(true); }, running: function () { return !!tour; } };
})();
