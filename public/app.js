const $ = (id) => document.getElementById(id);
const money = (n) => `${n < 0 ? '-' : ''}$${Math.abs(n).toFixed(2)}`;
const pctStr = (v) => `${v >= 0 ? '+' : ''}${v.toFixed(1)}%`;

let apiKeySaved = false;
let debounceTimer = null;

const LIVE_CONFIG_FIELDS = [
  ['maxSolPerTrade', 'Max SOL per trade'],
  ['maxConcurrentPositions', 'Max concurrent positions'],
  ['dailyLossLimitSol', 'Daily loss limit (SOL)'],
  ['slippagePct', 'Slippage %'],
  ['priorityFeeSol', 'Priority fee (SOL)'],
];
let liveConfigLoaded = false;

const CONFIG_FIELDS = [
  ['positionSize', 'Position size ($)'],
  ['maxOpenPositions', 'Max open positions'],
  ['momentumPriceThreshold', 'Momentum: price %'],
  ['momentumVolThreshold', 'Momentum: volume %'],
  ['takeProfitPct', 'Take-profit %'],
  ['stopLossPct', 'Stop-loss %'],
  ['trailingStopPct', 'Trailing stop %'],
  ['dailyLossLimit', 'Daily loss limit ($)'],
  ['dailyProfitFloor', 'Daily profit floor ($)'],
  ['dailyProfitCap', 'Daily profit cap ($)'],
];

function buildConfigGrid(config) {
  const grid = $('configGrid');
  if (grid.childElementCount) return; // build once, update values separately
  CONFIG_FIELDS.forEach(([key, label]) => {
    const field = document.createElement('label');
    field.className = 'field';
    field.innerHTML = `<span>${label}</span><input type="number" data-key="${key}" />`;
    grid.appendChild(field);
  });
  grid.querySelectorAll('input').forEach(inp => {
    inp.addEventListener('change', () => {
      const patch = {}; patch[inp.dataset.key] = Number(inp.value);
      postJSON('/api/config', patch);
    });
  });
}

function setConfigValues(config) {
  document.querySelectorAll('#configGrid input').forEach(inp => {
    if (document.activeElement !== inp) inp.value = config[inp.dataset.key];
  });
  $('autoTuneCheck').checked = !!config.autoTune;
}

async function postJSON(url, body) {
  const res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  return res.json().catch(() => ({}));
}

function buildLiveConfigGrid(cfg) {
  const grid = $('liveConfigGrid');
  if (grid.childElementCount) return;
  LIVE_CONFIG_FIELDS.forEach(([key, label]) => {
    const field = document.createElement('label');
    field.className = 'field';
    field.innerHTML = `<span>${label}</span><input type="number" step="0.001" data-key="${key}" />`;
    grid.appendChild(field);
  });
  grid.querySelectorAll('input').forEach(inp => {
    inp.addEventListener('change', () => {
      const patch = {}; patch[inp.dataset.key] = Number(inp.value);
      postJSON('/api/live/config', patch);
    });
  });
}

function setLiveConfigValues(cfg) {
  document.querySelectorAll('#liveConfigGrid input').forEach(inp => {
    if (document.activeElement !== inp) inp.value = cfg[inp.dataset.key];
  });
  if (document.activeElement !== $('liveKeypairPath')) $('liveKeypairPath').value = cfg.keypairPath || '';
  if (document.activeElement !== $('liveRpcUrl')) $('liveRpcUrl').value = cfg.rpcUrl || '';
  if (document.activeElement !== $('livePool')) $('livePool').value = cfg.pool || 'auto';
}

function renderLive(state) {
  const cfg = state.liveTrading;
  buildLiveConfigGrid(cfg);
  setLiveConfigValues(cfg);

  $('liveStateBadge').textContent = cfg.enabled ? 'LIVE TRADING ON' : 'LIVE TRADING OFF';
  $('liveStateBadge').classList.toggle('active', cfg.enabled);
  $('liveEnableBox').classList.toggle('hidden', cfg.enabled);
  $('liveDisableBtn').classList.toggle('hidden', !cfg.enabled);

  $('liveErrBox').classList.toggle('hidden', !cfg.lastError);
  if (cfg.lastError) $('liveErrBox').textContent = cfg.lastError;

  if (state.liveDailyStats.halted) {
    $('liveHaltBanner').textContent = `Live trading halted for today: ${state.liveDailyStats.halted}.`;
    $('liveHaltBanner').classList.remove('hidden');
  } else {
    $('liveHaltBanner').classList.add('hidden');
  }

  const pnlEl = $('livePnlVal');
  pnlEl.textContent = `${state.liveDailyStats.pnlSol.toFixed(6)} SOL`;
  pnlEl.className = 'cval ' + (state.liveDailyStats.pnlSol >= 0 ? 'good' : 'bad');
  $('liveWlVal').textContent = `${state.liveDailyStats.wins}W – ${state.liveDailyStats.losses}L`;

  const posBody = document.querySelector('#livePosTable tbody');
  posBody.innerHTML = state.livePositions.map(p => {
    const chg = p.currentPrice ? ((p.currentPrice - p.entryPrice) / p.entryPrice) * 100 : 0;
    return `<tr><td>${p.tokenName}</td><td>${p.entryPrice.toFixed(6)}</td><td>${p.currentPrice ? p.currentPrice.toFixed(6) : '—'}</td><td class="chg ${chg >= 0 ? 'up' : 'down'}">${pctStr(chg)}</td><td>${p.solSpent}</td></tr>`;
  }).join('');
  $('livePosEmpty').classList.toggle('hidden', state.livePositions.length > 0);

  const tradeBody = document.querySelector('#liveTradeTable tbody');
  tradeBody.innerHTML = state.liveTrades.map(t => `<tr><td>${t.token}</td><td>${t.action}</td><td>${t.solSpent}</td><td class="${t.pnlSol == null ? '' : (t.pnlSol >= 0 ? 'up' : 'down')}">${t.pnlSol == null ? '—' : t.pnlSol.toFixed(6)}</td><td><a href="${t.explorerUrl}" target="_blank" rel="noopener" style="color:var(--dim)">view</a></td></tr>`).join('');
  $('liveTradeEmpty').classList.toggle('hidden', state.liveTrades.length > 0);
}

async function refreshLiveStatus() {
  try {
    const s = await fetch('/api/live/status').then(r => r.json());
    $('liveWalletVal').textContent = s.walletPublicKey ? `${s.walletPublicKey.slice(0, 4)}…${s.walletPublicKey.slice(-4)}` : 'not configured';
    $('liveBalVal').textContent = s.solBalance != null ? `${s.solBalance.toFixed(4)} SOL available` : '';
  } catch (e) {}
}
setInterval(refreshLiveStatus, 5000);

function render(state) {
  // header / run button
  $('runBtn').textContent = state.running ? 'RUNNING' : 'PAUSED';
  $('runBtn').className = 'btn' + (state.running ? ' running' : '');

  // halt banner
  if (state.dailyStats.halted) {
    $('haltBanner').textContent = `Trading halted for today: ${state.dailyStats.halted}. New entries are blocked; existing positions still manage themselves.`;
    $('haltBanner').classList.remove('hidden');
  } else {
    $('haltBanner').classList.add('hidden');
  }

  // data source
  $('simBtn').classList.toggle('active', state.dataMode === 'simulated');
  $('liveBtn').classList.toggle('active', state.dataMode === 'live');
  const statusText = state.dataMode === 'live'
    ? (state.wsStatus === 'connected' ? 'connected' : state.wsStatus === 'connecting' ? 'connecting…' : state.wsStatus === 'error' ? 'connection failed — retrying' : 'idle')
    : '';
  $('wsStatus').textContent = statusText;
  $('errBox').classList.toggle('hidden', state.wsStatus !== 'error');
  if (state.wsStatus === 'error') $('errBox').textContent = 'Live feed keeps dropping. Check your network / firewall, or that outbound WebSocket connections to pumpportal.fun are allowed from this machine.';

  $('pumpPortalNoticeBox').classList.toggle('hidden', !state.pumpPortalNotice);
  if (state.pumpPortalNotice) $('pumpPortalNoticeBox').textContent = `PumpPortal: ${state.pumpPortalNotice}`;

  if (!apiKeySaved && document.activeElement !== $('apiKeyInput')) {
    $('apiKeyInput').placeholder = state.hasApiKey ? 'key saved — paste to replace' : 'paste key to enable real trade data';
  }
  if (document.activeElement !== $('ntfyInput')) $('ntfyInput').value = state.ntfyTopic || '';

  $('newFeed').innerHTML = state.newTokenFeed.slice(0, 6).map(f => `<div class="micro">${f.symbol || f.name || f.mint.slice(0, 8)}</div>`).join('') || '<div class="micro">—</div>';
  $('migFeed').innerHTML = state.migrationFeed.slice(0, 6).map(f => `<div class="micro" style="color:var(--green)">${f.mint.slice(0, 8)}</div>`).join('') || '<div class="micro">—</div>';
  $('debugFeed').innerHTML = state.debugFeed.map(m => `<div>${JSON.stringify(m.data)}</div>`).join('') || '<div>no messages yet</div>';

  // ticker tape
  const tapeItems = state.tokens.length ? state.tokens : [];
  $('tape-inner').innerHTML = tapeItems.length === 0 ? 'waiting for tokens…' :
    [...tapeItems, ...tapeItems].map(t => {
      const sig = t.signal || { priceChange: 0, hit: false };
      const cls = sig.hit ? 'hit' : '';
      const dirCls = sig.priceChange >= 0 ? 'up' : 'down';
      return `<span class="${cls}">${t.name} ${t.price ? t.price.toFixed(6) : '—'} <span class="${dirCls}">${pctStr(sig.priceChange)}</span></span>`;
    }).join('  ·  ');

  // paper balance
  $('balanceVal').textContent = money(state.balance);
  $('balanceVal').className = 'cval ' + (state.balance > 0 ? 'good' : 'bad');
  $('lockedVal').textContent = state.lockedInPositions > 0 ? `${money(state.lockedInPositions)} locked in ${state.positions.length} open position${state.positions.length === 1 ? '' : 's'}` : '';

  // stat cards
  const pnlEl = $('pnlVal');
  pnlEl.textContent = money(state.dailyStats.pnl);
  pnlEl.className = 'cval ' + (state.dailyStats.pnl >= 0 ? 'good' : 'bad');
  $('wlVal').textContent = `${state.dailyStats.wins}W – ${state.dailyStats.losses}L`;
  $('posVal').textContent = `${state.positions.length} / ${state.config.maxOpenPositions}`;
  const deployed = state.positions.reduce((a, p) => a + p.size, 0);
  $('posSub').textContent = money(deployed) + ' deployed';
  $('limVal').textContent = money(-state.config.dailyLossLimit);
  $('limSub').textContent = `floor ${money(state.config.dailyProfitFloor)} · cap ${money(state.config.dailyProfitCap)}`;

  // equity chart
  drawEquity(state.equity, state.config);

  // open positions
  $('posTitle').textContent = `Open positions (${state.positions.length})`;
  const posBody = document.querySelector('#posTable tbody');
  posBody.innerHTML = state.positions.map(p => {
    const chg = p.currentPrice ? ((p.currentPrice - p.entryPrice) / p.entryPrice) * 100 : 0;
    return `<tr><td>${p.tokenName}</td><td>${p.entryPrice.toFixed(6)}</td><td>${p.currentPrice ? p.currentPrice.toFixed(6) : '—'}</td><td class="chg ${chg >= 0 ? 'up' : 'down'}">${pctStr(chg)}</td><td>${money(p.size)}</td></tr>`;
  }).join('');
  $('posEmpty').classList.toggle('hidden', state.positions.length > 0);

  // feedback + self-tune
  $('feedbackList').innerHTML = state.feedback.map(f => `<li>${f}</li>`).join('');
  $('autoTuneBtn').innerHTML = `<span class="bulb"></span> SELF-TUNE ${state.config.autoTune ? 'ON' : 'OFF'}`;
  $('autoTuneBtn').classList.toggle('active', state.config.autoTune);
  $('tuneNote').textContent = state.config.autoTune
    ? `Self-tune is on — every 8 closed trades it re-reads the last 15 and applies one bounded step per param that needs it. It only adjusts entry/exit strategy params — position size and daily loss/profit limits are never touched automatically.`
    : `Self-tune is off — these are suggestions only. Turn it on to let the bot apply small bounded adjustments itself as it trades.`;

  // tuning log
  $('tuneTitle').textContent = `Auto-tune log (${state.tuningLog.length})`;
  $('tuneEmpty').classList.toggle('hidden', state.tuningLog.length > 0);
  $('tuneList').innerHTML = state.tuningLog.map(entry => `
    <div class="tune-entry">
      <div class="ts">${new Date(entry.at).toLocaleString()}</div>
      ${entry.changes.map(c => `<div class="change"><span class="param">${c.param}:</span> ${c.from} → ${c.to}<div class="why">${c.reason}</div></div>`).join('')}
    </div>`).join('');

  // trade log
  const tradeBody = document.querySelector('#tradeTable tbody');
  tradeBody.innerHTML = state.trades.slice(0, 10).map(t => `<tr><td>${t.token}</td><td class="chg ${t.pnl >= 0 ? 'up' : 'down'}">${money(t.pnl)}</td><td style="color:var(--dim)">${t.reason}</td><td>${new Date(t.closedAt).toLocaleTimeString()}</td></tr>`).join('');
  $('tradeEmpty').classList.toggle('hidden', state.trades.length > 0);

  // config
  buildConfigGrid(state.config);
  setConfigValues(state.config);

  // live trading (real funds)
  renderLive(state);

  // reports
  $('reportsTitle').textContent = `Daily reports (${state.reports.length})`;
  $('reportsEmpty').classList.toggle('hidden', state.reports.length > 0);
  $('reportsList').innerHTML = state.reports.map(r => `
    <div class="report-entry">
      <div class="top"><span class="date">${r.date}</span><span class="${r.pnl >= 0 ? 'up' : 'down'}">${money(r.pnl)}</span></div>
      <div class="stats">${r.wins}W / ${r.losses}L · ${r.tradeCount} trades${r.haltedReason ? ` · halted: ${r.haltedReason}` : ''}</div>
      ${r.feedback.map(f => `<div class="note">› ${f}</div>`).join('')}
    </div>`).join('');
}

function drawEquity(equity, config) {
  const canvas = $('equityChart');
  const ctx = canvas.getContext('2d');
  const w = canvas.width = canvas.clientWidth;
  const h = canvas.height = 120;
  ctx.clearRect(0, 0, w, h);
  if (!equity.length) return;

  const values = equity.map(e => e.v);
  const lo = Math.min(0, -config.dailyLossLimit, ...values);
  const hi = Math.max(config.dailyProfitFloor, ...values, 1);
  const range = hi - lo || 1;
  const x = (i) => (i / Math.max(1, equity.length - 1)) * (w - 10) + 5;
  const y = (v) => h - 10 - ((v - lo) / range) * (h - 20);

  function hline(v, color) {
    ctx.strokeStyle = color; ctx.setLineDash([3, 3]); ctx.beginPath();
    ctx.moveTo(0, y(v)); ctx.lineTo(w, y(v)); ctx.stroke(); ctx.setLineDash([]);
  }
  hline(0, '#3a3320');
  hline(-config.dailyLossLimit, '#7a2e2e');
  hline(config.dailyProfitFloor, '#2e5c3a');

  ctx.strokeStyle = '#ffb000';
  ctx.lineWidth = 1.75;
  ctx.beginPath();
  equity.forEach((e, i) => { const px = x(i), py = y(e.v); if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py); });
  ctx.stroke();
}

// ---------- wiring ----------
$('runBtn').addEventListener('click', () => {
  const nowRunning = $('runBtn').textContent === 'RUNNING';
  postJSON('/api/control', { running: !nowRunning });
});

$('depositBtn').addEventListener('click', () => {
  const amt = Number($('fundsInput').value);
  if (!amt || amt <= 0) return;
  postJSON('/api/balance', { action: 'deposit', amount: amt });
  $('fundsInput').value = '';
});
$('withdrawBtn').addEventListener('click', () => {
  const amt = Number($('fundsInput').value);
  if (!amt || amt <= 0) return;
  postJSON('/api/balance', { action: 'withdraw', amount: amt });
  $('fundsInput').value = '';
});

$('simBtn').addEventListener('click', () => postJSON('/api/datamode', { mode: 'simulated' }));
$('liveBtn').addEventListener('click', () => postJSON('/api/datamode', { mode: 'live' }));

$('toggleData').addEventListener('click', () => $('dataBody').classList.toggle('hidden'));
$('toggleConfig').addEventListener('click', () => $('configBody').classList.toggle('hidden'));
$('toggleReports').addEventListener('click', () => $('reportsBody').classList.toggle('hidden'));
$('toggleTune').addEventListener('click', () => $('tuneBody').classList.toggle('hidden'));
$('debugToggle').addEventListener('click', () => $('debugFeed').classList.toggle('hidden'));

$('autoTuneBtn').addEventListener('click', () => {
  const on = $('autoTuneBtn').classList.contains('active');
  postJSON('/api/config', { autoTune: !on });
});
$('autoTuneCheck').addEventListener('change', (e) => postJSON('/api/config', { autoTune: e.target.checked }));
$('resetConfig').addEventListener('click', () => {
  fetch('/api/state').then(r => r.json()); // no-op fetch just to keep UX snappy
  postJSON('/api/config', {
    positionSize: 40, maxOpenPositions: 3, momentumPriceThreshold: 9, momentumVolThreshold: 45,
    takeProfitPct: 18, stopLossPct: 8, trailingStopPct: 6, dailyLossLimit: 150, dailyProfitFloor: 250, dailyProfitCap: 1000,
  });
});

$('apiKeyInput').addEventListener('change', (e) => {
  apiKeySaved = true;
  postJSON('/api/apikey', { apiKey: e.target.value });
});
$('ntfyInput').addEventListener('change', (e) => postJSON('/api/ntfy', { topic: e.target.value }));

// ---------- live trading wiring ----------
$('toggleLive').addEventListener('click', () => $('liveBody').classList.toggle('hidden'));

$('liveKeypairPath').addEventListener('change', (e) => postJSON('/api/live/config', { keypairPath: e.target.value }));
$('liveRpcUrl').addEventListener('change', (e) => postJSON('/api/live/config', { rpcUrl: e.target.value }));
$('livePool').addEventListener('change', (e) => postJSON('/api/live/config', { pool: e.target.value }));

$('liveConfirmInput').addEventListener('input', (e) => {
  $('liveEnableBtn').disabled = e.target.value !== 'ENABLE LIVE TRADING';
});

$('liveEnableBtn').addEventListener('click', async () => {
  const result = await postJSON('/api/live/enable', { confirmPhrase: $('liveConfirmInput').value });
  $('liveConfirmInput').value = '';
  $('liveEnableBtn').disabled = true;
  if (!result.ok) alert(`Could not enable live trading: ${result.error || 'unknown error'}`);
});

$('liveDisableBtn').addEventListener('click', () => postJSON('/api/live/disable', {}));

// ---------- live connection to our own server ----------
function connect() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${location.host}`);
  ws.onmessage = (evt) => { try { render(JSON.parse(evt.data)); } catch (e) {} };
  ws.onclose = () => setTimeout(connect, 2000);
}
fetch('/api/state').then(r => r.json()).then(render).catch(() => {});
connect();

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}
