'use strict';

// ── Telegram WebApp init ─────────────────────────────────────────
const tg = window.Telegram?.WebApp;
if (tg) {
  tg.ready();
  tg.expand();
  try { tg.setHeaderColor('#1A1A2E'); } catch(e) {}
  try { tg.setBackgroundColor('#1A1A2E'); } catch(e) {}
}

const BASE = '';

// ── State ────────────────────────────────────────────────────────
let userId      = null;
let userName    = null;
let balance     = 0;
let minesCount  = 3;
let lastBet     = 100;
let minesGame   = null;
let topupMethod = 'crypto';
let soundEnabled = true;

// ── Init ─────────────────────────────────────────────────────────
window.addEventListener('DOMContentLoaded', () => {
  const tgUser = tg?.initDataUnsafe?.user;
  if (tgUser) {
    userId   = tgUser.id;
    userName = tgUser.first_name || tgUser.username || String(tgUser.id);
  } else {
    userId   = 0;
    userName = 'Гость';
  }

  // Restore saved settings
  minesCount   = parseInt(localStorage.getItem('mines_count')) || 3;
  lastBet      = parseInt(localStorage.getItem('mines_bet'))   || 100;
  soundEnabled = localStorage.getItem('sound_enabled') !== 'false';

  setHeaderUser();
  updateSoundBtn();
  buildMineCountGrid();
  setupBetInput();
  setupCustomCountInput();
  setupTopupInput();
  loadProfile();
  loadHome();

  // Restore bet field
  const betInp = document.getElementById('mines-bet-input');
  if (betInp && lastBet) betInp.value = lastBet;
  updateMultPreview();

  // Start ambient sound on first interaction
  document.addEventListener('click', () => { resumeAudio(); }, { once: true });
});

// ── Tab switching ────────────────────────────────────────────────
function switchTab(tab) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
  const navBtn = document.querySelector(`[data-tab="${tab}"]`);
  if (navBtn) navBtn.classList.add('active');

  if (tab === 'home')     loadHome();
  if (tab === 'roulette') loadRooms();
  if (tab === 'history')  loadHistory();
  if (tab === 'mines')    syncMinesState();
}

// ── Header ───────────────────────────────────────────────────────
function setHeaderUser() {
  const initial = (userName || '?')[0].toUpperCase();
  document.getElementById('header-initial').textContent = initial;
  document.getElementById('header-name').textContent    = userName || 'Пользователь';
}

function updateHeaderBalance(val) {
  balance = parseFloat(val) || 0;
  document.querySelector('#header-balance .balance-val').textContent = fmtMoney(balance);
  const hb = document.getElementById('home-balance');
  if (hb) hb.textContent = fmtMoney(balance);
}

// ── API helpers ──────────────────────────────────────────────────
async function api(path, body) {
  try {
    const opts = body
      ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
      : { method: 'GET' };
    const r = await fetch(BASE + '/api' + path, opts);
    return await r.json();
  } catch(e) {
    return { ok: false, error: String(e) };
  }
}

// ── Loader ───────────────────────────────────────────────────────
function showLoader() { document.getElementById('loader').classList.remove('hidden'); }
function hideLoader() { document.getElementById('loader').classList.add('hidden'); }

// ── Toast ────────────────────────────────────────────────────────
let _toastTimer;
function toast(msg, duration = 2500) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  el.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => {
    el.classList.remove('show');
    setTimeout(() => el.classList.add('hidden'), 350);
  }, duration);
}

// ── Format ───────────────────────────────────────────────────────
function fmtMoney(v) {
  v = parseFloat(v) || 0;
  return v % 1 === 0 ? v.toFixed(0) : v.toFixed(2);
}
function fmtNum(n) { return Number(n).toLocaleString('ru-RU'); }
function setText(id, val) { const el = document.getElementById(id); if (el) el.textContent = val; }

// ════════════════════════════════════════════════════════════════
// 🔊  SOUND ENGINE  (Web Audio API, procedural)
// ════════════════════════════════════════════════════════════════
let _audioCtx  = null;
let _ambientNodes = null;

function getCtx() {
  if (!_audioCtx) _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  return _audioCtx;
}

function resumeAudio() {
  try { getCtx().resume(); } catch(e) {}
}

// Master gain check
function canPlay() { return soundEnabled; }

// ─ Basic tone ─
function tone(freq, type, duration, vol, when = 0, detune = 0) {
  if (!canPlay()) return;
  try {
    const ctx  = getCtx();
    const t    = ctx.currentTime + when;
    const osc  = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = type;
    osc.frequency.setValueAtTime(freq, t);
    if (detune) osc.detune.setValueAtTime(detune, t);
    gain.gain.setValueAtTime(vol, t);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + duration);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(t);
    osc.stop(t + duration + 0.01);
  } catch(e) {}
}

// ─ Noise burst ─
function noise(duration, vol, filterFreq = 300, when = 0) {
  if (!canPlay()) return;
  try {
    const ctx  = getCtx();
    const t    = ctx.currentTime + when;
    const sr   = ctx.sampleRate;
    const buf  = ctx.createBuffer(1, sr * duration, sr);
    const data = buf.getChannelData(0);
    for (let i = 0; i < data.length; i++) {
      data[i] = (Math.random() * 2 - 1) * (1 - i / data.length);
    }
    const src    = ctx.createBufferSource();
    const filter = ctx.createBiquadFilter();
    const gain   = ctx.createGain();
    src.buffer   = buf;
    filter.type  = 'lowpass';
    filter.frequency.setValueAtTime(filterFreq, t);
    gain.gain.setValueAtTime(vol, t);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + duration);
    src.connect(filter);
    filter.connect(gain);
    gain.connect(ctx.destination);
    src.start(t);
  } catch(e) {}
}

// ─ SOUNDS ─────────────────────────────────────────────────────
function soundClick() {
  tone(1200, 'sine', 0.06, 0.12);
}

function soundStart() {
  // Rising arpeggio
  [440, 554, 659, 880].forEach((f, i) => tone(f, 'sine', 0.22, 0.18, i * 0.07));
  noise(0.15, 0.04, 2000, 0.05);
}

function soundSafe() {
  // Crystal ping with harmonics
  tone(1047, 'sine',     0.4,  0.22);
  tone(2093, 'sine',     0.25, 0.08);
  tone(1047, 'triangle', 0.15, 0.06, 0.01, 5);
}

function soundSafeHighMult() {
  // Higher pitch for bigger multipliers
  tone(1318, 'sine',     0.45, 0.24);
  tone(2637, 'sine',     0.28, 0.09);
  tone(1318 * 1.5, 'sine', 0.12, 0.04, 0.02);
}

function soundExplosion() {
  // Boom + rumble
  noise(0.6,  0.55, 180, 0);
  noise(0.3,  0.4,  80,  0.05);
  tone(60,  'sine', 0.5, 0.3, 0);
  tone(40,  'sine', 0.4, 0.2, 0.05);
  tone(100, 'sine', 0.2, 0.15, 0.02);
}

function soundCashout() {
  // Victory fanfare
  [523, 659, 784, 1047].forEach((f, i) => {
    tone(f, 'sine',     0.35, 0.2,  i * 0.09);
    tone(f, 'triangle', 0.2,  0.07, i * 0.09 + 0.02);
  });
  noise(0.12, 0.03, 3000, 0.0);
}

function soundWin() {
  // Big win
  [523, 659, 784, 1047, 1318].forEach((f, i) => {
    tone(f, 'sine',     0.45, 0.22, i * 0.08);
    tone(f * 2, 'sine', 0.2,  0.05, i * 0.08 + 0.01);
  });
}

// ─ AMBIENT ────────────────────────────────────────────────────
function startAmbient() {
  if (!canPlay() || _ambientNodes) return;
  try {
    const ctx  = getCtx();
    const osc1 = ctx.createOscillator();
    const osc2 = ctx.createOscillator();
    const lfo  = ctx.createOscillator();
    const lfoG = ctx.createGain();
    const gain = ctx.createGain();

    osc1.type = 'sine';   osc1.frequency.value = 55;
    osc2.type = 'sine';   osc2.frequency.value = 82.5;
    lfo.type  = 'sine';   lfo.frequency.value  = 0.15;
    lfoG.gain.value = 4;
    gain.gain.value = 0;

    lfo.connect(lfoG);
    lfoG.connect(osc1.frequency);
    osc1.connect(gain);
    osc2.connect(gain);
    gain.connect(ctx.destination);

    osc1.start(); osc2.start(); lfo.start();
    gain.gain.linearRampToValueAtTime(0.04, ctx.currentTime + 2);

    _ambientNodes = { osc1, osc2, lfo, gain };
  } catch(e) {}
}

function stopAmbient() {
  if (!_ambientNodes) return;
  try {
    const ctx = getCtx();
    _ambientNodes.gain.gain.linearRampToValueAtTime(0.0001, ctx.currentTime + 1);
    setTimeout(() => {
      try { _ambientNodes.osc1.stop(); _ambientNodes.osc2.stop(); _ambientNodes.lfo.stop(); } catch(e) {}
      _ambientNodes = null;
    }, 1100);
  } catch(e) {}
}

// ─ Sound toggle ───────────────────────────────────────────────
function toggleSound() {
  soundEnabled = !soundEnabled;
  localStorage.setItem('sound_enabled', soundEnabled);
  updateSoundBtn();
  if (!soundEnabled) stopAmbient();
  else startAmbient();
}

function updateSoundBtn() {
  const btn = document.getElementById('sound-toggle');
  if (!btn) return;
  btn.textContent = soundEnabled ? '🔊' : '🔇';
  btn.classList.toggle('muted', !soundEnabled);
}

// ════════════════════════════════════════════════════════════════
// 🏠  HOME
// ════════════════════════════════════════════════════════════════
async function loadProfile() {
  const d = await api('/profile?uid=' + userId);
  if (d.ok) updateHeaderBalance(d.balance);
}

async function loadHome() {
  const [pd, sd] = await Promise.all([
    api('/profile?uid=' + userId),
    api('/stats')
  ]);
  if (pd.ok) updateHeaderBalance(pd.balance);
  if (sd.ok) {
    const s = sd.data;
    setText('st-total-bet', fmtNum(Math.round(s.total_bet))  + ' ₽');
    setText('st-total-won', fmtNum(Math.round(s.total_won))  + ' ₽');
    setText('st-games',     fmtNum(s.games_played));
    setText('st-players',   fmtNum(s.players));
  }
}

// ════════════════════════════════════════════════════════════════
// 🎲  ROOMS
// ════════════════════════════════════════════════════════════════
async function loadRooms() {
  const d  = await api('/rooms');
  const el = document.getElementById('rooms-list');
  if (!d.ok || !d.rooms.length) {
    el.innerHTML = `<div class="empty-state"><div class="empty-state-icon">🎲</div>Нет активных комнат</div>`;
    return;
  }
  el.innerHTML = d.rooms.map(r => `
    <div class="room-card">
      <div class="room-card-left">
        <div class="room-name">${esc(r.name)}</div>
        <div class="room-meta">Игроков: ${r.players}${r.min_players ? ' / мин ' + r.min_players : ''} · Ставка: ${r.min_bet}–${r.max_bet} ₽</div>
      </div>
      <div class="room-badge">${r.status === 'countdown' ? '⏳ Скоро' : '✅ Ждёт'}</div>
    </div>`).join('');
}

// ════════════════════════════════════════════════════════════════
// 📜  HISTORY
// ════════════════════════════════════════════════════════════════
async function loadHistory() {
  const d  = await api('/history');
  const el = document.getElementById('history-list');
  if (!d.ok || !d.history.length) {
    el.innerHTML = `<div class="empty-state"><div class="empty-state-icon">📜</div>История пока пуста</div>`;
    return;
  }
  el.innerHTML = d.history.map(h => {
    const ts = (h.timestamp || '').slice(0, 16).replace('T', ' ');
    return `
      <div class="hist-card">
        <div class="hist-winner">🏆 ${esc(h.winner_username || String(h.winner_id))}</div>
        <div class="hist-prize">+${fmtNum(Math.round(h.prize))} ₽</div>
        <div class="hist-meta">
          <span>🏠 ${esc(h.room_name || 'Стандартная')}</span>
          <span>👥 ${h.players_count}</span>
          <span>🏦 ${fmtNum(Math.round(h.bank))} ₽</span>
          <span>🕐 ${ts}</span>
        </div>
      </div>`;
  }).join('');
}

// ════════════════════════════════════════════════════════════════
// 💣  MINES
// ════════════════════════════════════════════════════════════════

// ─ Math ───────────────────────────────────────────────────────
function minesMultiplier(mines, revealed) {
  if (revealed === 0) return 1.0;
  const total = 25;
  let prob = 1.0;
  for (let i = 0; i < revealed; i++) prob *= (total - mines - i) / (total - i);
  return Math.round(0.97 / prob * 100) / 100;
}

// ─ Build preset buttons ────────────────────────────────────────
function buildMineCountGrid() {
  const counts = [1, 2, 3, 5, 7, 10, 15, 20, 24];
  const grid   = document.getElementById('mine-count-grid');
  if (!grid) return;
  grid.innerHTML = counts.map(n => `
    <button class="mine-preset-btn${n === minesCount ? ' selected' : ''}"
            onclick="selectMineCount(${n})">${n}</button>`).join('');
}

function selectMineCount(n) {
  n = Math.max(1, Math.min(24, parseInt(n) || 1));
  minesCount = n;
  // Update presets
  document.querySelectorAll('.mine-preset-btn').forEach(b => {
    b.classList.toggle('selected', parseInt(b.textContent) === n);
  });
  // Update custom input
  const ci = document.getElementById('mine-count-custom');
  if (ci) ci.value = '';
  updateMultPreview();
}

function setupCustomCountInput() {
  const inp = document.getElementById('mine-count-custom');
  if (!inp) return;
  inp.addEventListener('input', () => {
    let v = parseInt(inp.value);
    if (isNaN(v) || v < 1) return;
    if (v > 24) { inp.value = 24; v = 24; }
    minesCount = v;
    // Deselect presets
    document.querySelectorAll('.mine-preset-btn').forEach(b => {
      b.classList.toggle('selected', parseInt(b.textContent) === v);
    });
    updateMultPreview();
  });
  inp.addEventListener('blur', () => {
    let v = parseInt(inp.value);
    if (isNaN(v) || v < 1 || v > 24) {
      inp.value = '';
      return;
    }
    inp.value = v;
    minesCount = v;
    updateMultPreview();
  });
}

// ─ Multiplier preview ─────────────────────────────────────────
function updateMultPreview() {
  const el = document.getElementById('mines-mult-preview');
  if (!el) return;
  const m   = minesCount;
  const max = 25 - m - 1;
  if (max <= 0) { el.innerHTML = ''; return; }
  const pts = [1, 3, 5, 10].filter(r => r <= max);
  if (!pts.length) { el.innerHTML = ''; return; }
  el.innerHTML = pts.map(r =>
    `<span>${r} яч → <strong>×${minesMultiplier(m, r).toFixed(2)}</strong></span>`
  ).join('');
}

// ─ Bet helpers ────────────────────────────────────────────────
function setupBetInput() {
  const inp = document.getElementById('mines-bet-input');
  if (!inp) return;
  inp.addEventListener('input', updateMultPreview);
  updateMultPreview();
}

function setBet(v)   {
  soundClick();
  document.getElementById('mines-bet-input').value = v;
  updateMultPreview();
}
function setBetMax() { setBet(Math.min(1000, Math.floor(balance))); }

// ─ Sync with server ───────────────────────────────────────────
async function syncMinesState() {
  const d = await api('/mines/state?uid=' + userId);
  if (d.ok && d.active) {
    minesGame = d.game;
    showMinesBoard();
    startAmbient();
  } else {
    minesGame = null;
    showMinesSetup();
  }
}

function showMinesSetup() {
  document.getElementById('mines-setup').style.display = '';
  document.getElementById('mines-game').style.display  = 'none';
  stopAmbient();
}

// ─ START GAME ─────────────────────────────────────────────────
async function startMines() {
  const betInp = document.getElementById('mines-bet-input');
  const bet    = parseInt(betInp.value);
  if (!bet || bet < 1 || bet > 1000) { toast('Введи ставку от 1 до 1000 ₽'); return; }
  if (bet > balance) { toast('Недостаточно средств 💸'); return; }
  if (minesCount < 1 || minesCount > 24) { toast('Мины: 1–24'); return; }

  soundClick();
  showLoader();
  const d = await api('/mines/start', { uid: userId, bet, mines: minesCount });
  hideLoader();

  if (!d.ok) { toast(d.error || 'Ошибка запуска'); return; }

  // Save settings
  lastBet = bet;
  localStorage.setItem('mines_count', minesCount);
  localStorage.setItem('mines_bet',   bet);

  minesGame = d.game;
  updateHeaderBalance(d.balance);
  showMinesBoard();
  soundStart();
  startAmbient();
}

// ─ SHOW BOARD ─────────────────────────────────────────────────
function showMinesBoard() {
  document.getElementById('mines-setup').style.display = 'none';
  document.getElementById('mines-game').style.display  = '';
  document.getElementById('mines-result').classList.add('hidden');
  renderMinesGrid();
  updateMinesMeta();
}

// ─ RENDER GRID ────────────────────────────────────────────────
function renderMinesGrid(showAll = false, hitCell = null) {
  const g    = minesGame;
  const grid = document.getElementById('mines-grid');
  if (!g || !grid) return;
  grid.innerHTML = '';

  const revealed = new Set(g.revealed || []);
  const minePos  = showAll ? new Set(g.mines_positions || []) : new Set();
  const hit      = hitCell !== null ? hitCell : (g.hit_cell !== undefined ? g.hit_cell : null);

  for (let i = 0; i < 25; i++) {
    const btn = document.createElement('button');
    btn.className = 'mine-cell';
    btn.dataset.i = i;

    if (i === hit) {
      btn.classList.add('revealed-mine', 'dead');
      btn.textContent = '💥';
    } else if (revealed.has(i)) {
      btn.classList.add('revealed-safe', 'dead');
      btn.textContent = '💎';
    } else if (showAll && minePos.has(i)) {
      btn.classList.add('show-mine', 'dead');
      btn.textContent = '💣';
    } else if (!g.active) {
      btn.classList.add('dead');
      btn.textContent = '⬜';
    } else {
      btn.textContent = '⬜';
      btn.onclick = () => tapCell(i, btn);
    }
    grid.appendChild(btn);
  }
}

// ─ UPDATE META / PROGRESS ─────────────────────────────────────
function updateMinesMeta() {
  const g = minesGame;
  if (!g) return;

  const safeRevealed = (g.revealed || []).filter(i => i !== g.hit_cell).length;
  const safeTotal    = 25 - g.mines;
  const mult  = minesMultiplier(g.mines, safeRevealed);
  const pay   = (g.bet * mult).toFixed(2);

  setText('mg-bet',    g.bet + ' ₽');
  setText('mg-mines',  g.mines);
  setText('mg-mult',   '×' + mult.toFixed(2));
  setText('mg-payout', pay + ' ₽');

  const cashBtn   = document.getElementById('mines-cash-btn');
  const cashVal   = document.getElementById('cash-val');
  const cashMult  = document.getElementById('cash-mult-label');
  if (cashVal)  cashVal.textContent  = pay;
  if (cashMult) cashMult.textContent = '×' + mult.toFixed(2);
  if (cashBtn)  cashBtn.disabled = safeRevealed === 0 || !g.active;

  // Progress bar
  const fill  = document.getElementById('mines-progress-fill');
  const label = document.getElementById('mines-progress-label');
  if (fill)  fill.style.width = (safeTotal > 0 ? safeRevealed / safeTotal * 100 : 0) + '%';
  if (label) label.textContent = safeRevealed + ' / ' + safeTotal;
}

// ─ TAP CELL ───────────────────────────────────────────────────
async function tapCell(i, btnEl) {
  if (!minesGame || !minesGame.active) return;

  // Optimistic visual: flash the cell
  if (btnEl) { btnEl.style.opacity = '0.5'; btnEl.style.pointerEvents = 'none'; }

  const d = await api('/mines/cell', { uid: userId, cell: i });
  if (!d.ok) { toast(d.error || 'Ошибка'); if (btnEl) { btnEl.style.opacity = ''; btnEl.style.pointerEvents = ''; } return; }

  minesGame = d.game;

  if (d.hit) {
    // ── MINE HIT ──────────────────────────────────────────────
    stopAmbient();
    soundExplosion();
    renderMinesGrid(true, i);
    updateMinesMeta();
    updateHeaderBalance(d.balance);
    // Animate hit cell
    setTimeout(() => {
      const hitBtn = document.querySelector(`[data-i="${i}"]`);
      if (hitBtn) hitBtn.classList.add('revealed-mine');
    }, 50);
    showMinesResult(false, g => ({
      icon:  '💥',
      title: 'Мина!',
      sub:   `Ставка ${g.bet} ₽ потеряна\nМин было: ${g.mines}`
    }));
    return;
  }

  if (d.auto_cashout) {
    // ── ALL SAFE CELLS ────────────────────────────────────────
    stopAmbient();
    soundWin();
    renderMinesGrid(true);
    updateMinesMeta();
    updateHeaderBalance(d.balance);
    showMinesResult(true, g => ({
      icon:  '🏆',
      title: 'Победа!',
      sub:   `Все ячейки открыты!\nВыигрыш: ${parseFloat(d.payout).toFixed(2)} ₽ (×${d.mult})`
    }));
    return;
  }

  // ── SAFE CELL ─────────────────────────────────────────────
  const mult = minesMultiplier(minesGame.mines, (minesGame.revealed || []).length);
  mult >= 2 ? soundSafeHighMult() : soundSafe();

  // Animate the revealed cell
  renderMinesGrid();
  updateMinesMeta();

  // Pop animation on new cell
  const newBtn = document.querySelector(`.mine-cell.revealed-safe:not(.glow)`);
  document.querySelectorAll('.mine-cell.revealed-safe').forEach((b, idx) => {
    if (idx === (minesGame.revealed || []).length - 1) {
      b.classList.add('pop-in');
      setTimeout(() => { b.classList.remove('pop-in'); b.classList.add('glow'); }, 300);
    }
  });
}

// ─ CASHOUT ────────────────────────────────────────────────────
async function cashoutMines() {
  if (!minesGame || !minesGame.active) return;
  soundClick();
  showLoader();
  const d = await api('/mines/cash', { uid: userId });
  hideLoader();
  if (!d.ok) { toast(d.error || 'Ошибка'); return; }

  minesGame = d.game;
  stopAmbient();
  soundCashout();
  renderMinesGrid(true);
  updateMinesMeta();
  updateHeaderBalance(d.balance);
  showMinesResult(true, g => ({
    icon:  '💰',
    title: 'Выигрыш забран!',
    sub:   `${parseFloat(d.payout).toFixed(2)} ₽ (×${d.mult})\nОткрыто ячеек: ${(g.revealed || []).length}`
  }));
}

// ─ RESULT CARD ────────────────────────────────────────────────
function showMinesResult(win, msgFn) {
  const msg = msgFn(minesGame || {});
  const el  = document.getElementById('mines-result');
  el.className = 'mines-result-card ' + (win ? 'win' : 'lose');
  setText('result-icon',  msg.icon  || (win ? '🏆' : '💥'));
  setText('result-title', msg.title || '');
  const sub = document.getElementById('result-sub');
  if (sub) sub.innerHTML = (msg.sub || '').replace(/\n/g, '<br>');
  el.classList.remove('hidden');

  const cashBtn = document.getElementById('mines-cash-btn');
  if (cashBtn) cashBtn.disabled = true;
}

// ─ PLAY AGAIN (same settings) ─────────────────────────────────
function playAgain() {
  soundClick();
  minesGame = null;
  // Keep minesCount and lastBet, just restart
  const betInp = document.getElementById('mines-bet-input');
  if (betInp) betInp.value = lastBet;
  updateMultPreview();
  // Don't re-show setup, go straight if balance ok
  if (lastBet > 0 && balance >= lastBet) {
    document.getElementById('mines-setup').style.display = '';
    document.getElementById('mines-game').style.display  = 'none';
    document.getElementById('mines-result').classList.add('hidden');
    // Auto-start after brief delay
    setTimeout(() => startMines(), 120);
  } else {
    changeSettings();
  }
}

// ─ CHANGE SETTINGS ────────────────────────────────────────────
function changeSettings() {
  soundClick();
  minesGame = null;
  buildMineCountGrid();
  const betInp = document.getElementById('mines-bet-input');
  if (betInp) betInp.value = lastBet;
  updateMultPreview();
  document.getElementById('mines-setup').style.display = '';
  document.getElementById('mines-game').style.display  = 'none';
}

// ════════════════════════════════════════════════════════════════
// 💳  TOPUP
// ════════════════════════════════════════════════════════════════
function selectMethod(m) {
  topupMethod = m;
  document.querySelectorAll('.method-card').forEach(c => c.classList.remove('active'));
  document.getElementById('method-' + m).classList.add('active');
  document.getElementById('topup-crypto').classList.toggle('hidden', m !== 'crypto');
  document.getElementById('topup-card').classList.toggle('hidden',   m !== 'card');
  soundClick();
}

function setTopup(v) {
  document.getElementById('topup-amount').value = v;
  updateUsdtPreview();
  soundClick();
}

function setupTopupInput() {
  const inp = document.getElementById('topup-amount');
  if (inp) inp.addEventListener('input', updateUsdtPreview);
}

function updateUsdtPreview() {
  const rub = parseInt(document.getElementById('topup-amount').value) || 0;
  const el  = document.getElementById('topup-usdt-preview');
  if (!el) return;
  if (!rub) { el.innerHTML = ''; return; }
  const usdt = (rub / 90).toFixed(4);
  el.innerHTML = `<span>${rub} ₽ = <strong>${usdt} USDT</strong></span>`;
}

async function createTopupInvoice() {
  const rub = parseInt(document.getElementById('topup-amount').value);
  if (!rub || rub < 90) { toast('Минимум 90 ₽'); return; }
  soundClick();
  showLoader();
  const d = await api('/topup/create', { uid: userId, amount_rub: rub });
  hideLoader();
  if (!d.ok) { toast(d.error || 'Ошибка создания счёта'); return; }
  if (d.url) {
    if (tg) tg.openLink(d.url);
    else window.open(d.url, '_blank');
  }
}

function openBot() {
  soundClick();
  if (tg) tg.close();
}

// ── Escape HTML ──────────────────────────────────────────────────
function esc(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
