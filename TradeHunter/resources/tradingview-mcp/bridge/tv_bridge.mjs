// TradeHunter — TV Bridge
// -----------------------------------------------------------------------------
// A tiny, ZERO-DEPENDENCY local sidecar that lets the TradeHunter web app draw
// MATP / MBP levels on the user's OWN TradingView (web, in Chrome).
//
// Why this exists (see CLAUDE.md + resources/tradingview-mcp/bridge/README.md):
//   The TradingView MCP drives TV over Chrome DevTools Protocol (CDP), which is
//   localhost-only. TradeHunter is served from Hermes, so the SERVER can never
//   reach a user's local TV. Instead, each user runs THIS bridge on their own
//   machine; the MATP page fetches http://127.0.0.1:9223/plot?... and the bridge
//   drives that user's own Chrome. => every user draws on their OWN chart; the
//   server never touches a TV.
//
// Zero dependencies on purpose (portability): uses only Node built-ins —
//   node:http + global fetch + global WebSocket (Node >= 22). No `npm install`,
//   so the whole thing travels with the Dropbox-synced repo and runs on any PC
//   with Node >= 22. (Node here is v24; fine.)
//
// It reuses the EXACT chart calls the MCP core uses:
//   chart = window.TradingViewApi._activeChartWidgetWV.value()
//   chart.setSymbol(sym, {})
//   chart.createShape({time, price}, {shape:'horizontal_line', overrides:{...}})
//   chart.getAllShapes() / chart.removeEntity(id)
//
// Idempotent by design: it tracks the shape ids it created per symbol on a page
// global (window.__TH_LINES) and removes ITS OWN previous MATP/MBP lines before
// redrawing — so clicking a ticker again REFRESHES the two lines in place, never
// stacks duplicates, and never touches the user's manual drawings.
// -----------------------------------------------------------------------------

import http from 'node:http';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const BRIDGE_VERSION = '1.1.0'; // features: auto-launch, autosave, PNA header, locked lines (frozen+disableSelection), label-based de-dupe
const BRIDGE_PORT = Number(process.env.TH_TV_BRIDGE_PORT || 9223);
const CDP_PORT = Number(process.env.TH_TV_CDP_PORT || 9222);
const CDP_HOST = process.env.TH_TV_CDP_HOST || '127.0.0.1';

const CHART_API = 'window.TradingViewApi._activeChartWidgetWV.value()';

// --- CDP: find the TradingView chart tab in Chrome and eval JS in it ---------

async function findTvTarget() {
  let list;
  try {
    const r = await fetch(`http://${CDP_HOST}:${CDP_PORT}/json/list`);
    list = await r.json();
  } catch (e) {
    throw new Error(
      `Can't reach Chrome's debug port on ${CDP_HOST}:${CDP_PORT}. ` +
      `Launch Chrome with --remote-debugging-port=${CDP_PORT} (use launch_tv_bridge.bat).`
    );
  }
  const pages = (list || []).filter((t) => t.type === 'page');
  const t =
    pages.find((t) => /tradingview\.com\/chart/i.test(t.url || '')) ||
    pages.find((t) => /tradingview/i.test(t.url || ''));
  if (!t || !t.webSocketDebuggerUrl) {
    throw new Error(
      'No TradingView chart tab found in Chrome. Open https://www.tradingview.com/chart/ ' +
      '(logged in) in the debug Chrome, then try again.'
    );
  }
  return t;
}

// Evaluate an expression on a specific CDP target. One fresh WebSocket per call.
function evalOnTarget(target, expression, awaitPromise = true, timeoutMs = 9000) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    const finish = (fn, arg) => {
      if (settled) return;
      settled = true;
      try { ws.close(); } catch { /* ignore */ }
      fn(arg);
    };
    const timer = setTimeout(
      () => finish(reject, new Error('CDP eval timed out (chart not ready?)')),
      timeoutMs
    );
    ws.addEventListener('open', () => {
      ws.send(JSON.stringify({
        id: 1,
        method: 'Runtime.evaluate',
        params: { expression, returnByValue: true, awaitPromise },
      }));
    });
    ws.addEventListener('message', (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch { return; }
      if (msg.id !== 1) return;
      clearTimeout(timer);
      if (msg.error) return finish(reject, new Error(msg.error.message || 'CDP error'));
      const r = msg.result;
      if (r && r.exceptionDetails) {
        const d = r.exceptionDetails;
        return finish(reject, new Error(d.exception?.description || d.text || 'JS eval error'));
      }
      finish(resolve, r && r.result ? r.result.value : undefined);
    });
    ws.addEventListener('error', () => {
      clearTimeout(timer);
      finish(reject, new Error('CDP WebSocket error'));
    });
  });
}

const API_READY_EXPR = '!!(window.TradingViewApi && window.TradingViewApi._activeChartWidgetWV)';

// Locate a Chrome (or Edge) executable to auto-launch on Windows.
function findBrowserPath() {
  const pf = process.env['ProgramFiles'] || 'C:\\Program Files';
  const pfx = process.env['ProgramFiles(x86)'] || 'C:\\Program Files (x86)';
  const lad = process.env['LOCALAPPDATA'] || '';
  const cands = [
    process.env.TH_CHROME,
    path.join(pf, 'Google/Chrome/Application/chrome.exe'),
    path.join(pfx, 'Google/Chrome/Application/chrome.exe'),
    lad && path.join(lad, 'Google/Chrome/Application/chrome.exe'),
    path.join(pfx, 'Microsoft/Edge/Application/msedge.exe'),
    path.join(pf, 'Microsoft/Edge/Application/msedge.exe'),
  ].filter(Boolean);
  return cands.find((p) => { try { return fs.existsSync(p); } catch { return false; } }) || null;
}

function launchBrowser() {
  const browser = findBrowserPath();
  if (!browser) {
    throw new Error('No TradingView tab, and no Chrome/Edge found to auto-launch. Run launch_tv_bridge.bat, or set TH_CHROME to your browser exe.');
  }
  const userDataDir = process.env.TH_TV_USER_DATA_DIR
    || path.join(process.env['LOCALAPPDATA'] || os.tmpdir(), 'TradeHunterTV');
  console.log('[tv-bridge] auto-launching browser:', browser, '(profile:', userDataDir + ')');
  spawn(browser, [
    `--remote-debugging-port=${CDP_PORT}`,
    `--user-data-dir=${userDataDir}`,
    'https://www.tradingview.com/chart/',
  ], { detached: true, stdio: 'ignore' }).unref();
}

// Poll until a TradingView tab exists AND its chart API is loaded.
async function waitForReadyTarget(timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const t = await findTvTarget();
      const ready = await evalOnTarget(t, API_READY_EXPR, false, 3000).catch(() => false);
      if (ready) return t;
    } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 700));
  }
  return null;
}

// Is Chrome's debug port reachable at all (regardless of whether a TV tab exists)?
async function cdpPortUp() {
  try { await fetch(`http://${CDP_HOST}:${CDP_PORT}/json/version`); return true; }
  catch { return false; }
}

// Guarantee a ready TradingView tab, launching the browser if none is running.
// This is what makes "Plot on TV" work even after you've closed Chrome — as long
// as the bridge itself is running.
let launching = null;
async function ensureChrome() {
  // fast path: already ready
  let t = await waitForReadyTarget(1200);
  if (t) return t;

  if (await cdpPortUp()) {
    // Chrome is up (debug port open) but the chart isn't ready yet — give it longer.
    t = await waitForReadyTarget(20000);
    if (t) return t;
    throw new Error('A debug Chrome is running but TradingView did not become ready. Open a tradingview.com/chart tab in it.');
  }

  // No debug Chrome at all — launch it (once, even under concurrent clicks).
  if (!launching) {
    launching = (async () => {
      launchBrowser();
      return waitForReadyTarget(28000);
    })().finally(() => { launching = null; });
  }
  t = await launching;
  if (t) return t;
  throw new Error('Launched the browser but TradingView did not become ready in time — try the plot again in a few seconds.');
}

// Evaluate in the TV page, auto-launching Chrome if needed.
async function cdpEval(expression, awaitPromise = true, timeoutMs = 12000) {
  const target = await ensureChrome();
  return evalOnTarget(target, expression, awaitPromise, timeoutMs);
}

// --- The plot expression (runs inside the TV page) ---------------------------
// Removes our own previous MATP/MBP lines for THIS symbol, sets the symbol if
// needed, then draws the two horizontal lines and records their ids.

function buildPlotExpr({ symbol, matp, mbp }) {
  const S = JSON.stringify(symbol);
  const MATP = matp == null ? 'null' : Number(matp);
  const MBP = mbp == null ? 'null' : Number(mbp);
  return `
(function () {
  return new Promise(function (resolve) {
    try {
      var chart = ${CHART_API};
      if (!chart) return resolve({ ok: false, error: 'TradingView chart API not available on this tab' });
      var SYM = ${S};
      var MATP = ${MATP};
      var MBP = ${MBP};
      window.__TH_LINES = window.__TH_LINES || {};
      // key cleanup on the bare symbol (ignore EXCHANGE: prefix)
      var KEY = SYM.indexOf(':') >= 0 ? SYM.split(':').pop().toUpperCase() : SYM.toUpperCase();

      function draw() {
        var api = ${CHART_API};
        // 1) remove OUR previous MATP/MBP lines on the CURRENT symbol by reading each
        //    horizontal line's label. This checks the chart itself, so it works even
        //    after TV was closed/reopened (the in-page id map is gone, but the saved
        //    lines came back with the layout) — no more duplicates. Manual drawings are
        //    left alone; only lines labelled "MATP <n>" / "MBP <n>" (ours) are removed.
        //    TV scopes drawings per symbol, so getAllShapes() here only sees this symbol.
        var MINE = /^(MATP|MBP)\s+[0-9.]/;
        api.getAllShapes().forEach(function (s) {
          if (s.name !== 'horizontal_line') return;
          try {
            var sh = api.getShapeById(s.id);
            var props = sh && (sh.getProperties ? sh.getProperties() : (sh.properties ? sh.properties() : null));
            var txt = props && props.text;
            if (txt && MINE.test(String(txt))) api.removeEntity(s.id);
          } catch (e) { /* skip */ }
        });
        window.__TH_LINES[KEY] = [];
        // 2) pick a visible bar time for the anchor point (line spans the chart anyway)
        var t;
        try { var vr = api.getVisibleRange(); t = Math.floor((vr.from + vr.to) / 2); }
        catch (e) { t = Math.floor(Date.now() / 1000); }
        // 3) snapshot ids, draw, then diff after a settle to capture the new ids
        var before = api.getAllShapes().map(function (s) { return s.id; });
        function line(price, color, label) {
          api.createShape({ time: t, price: price }, {
            shape: 'horizontal_line',
            disableSelection: true,   // can't be grabbed/selected. (The create-time
                                      // lock/frozen option is ignored by TV — the real
                                      // lock is set via setProperties below.)
            overrides: { linecolor: color, linewidth: 2, linestyle: 0,
                         showLabel: true, textcolor: color, fontsize: 11,
                         horzLabelsAlign: 'right', text: label },
            text: label
          });
        }
        if (MATP != null) line(MATP, '#f97316', 'MATP ' + MATP);
        if (MBP != null) line(MBP, '#16a34a', 'MBP ' + MBP);
        setTimeout(function () {
          var after = api.getAllShapes().map(function (s) { return s.id; });
          var added = after.filter(function (id) { return before.indexOf(id) < 0; });
          window.__TH_LINES[KEY] = (window.__TH_LINES[KEY] || []).concat(added);
          // Lock (freeze) the lines we just drew so they can't be moved/edited. frozen
          // must be set AFTER creation (the create-time option is a no-op). removeEntity
          // still works on frozen lines, so the replace-not-duplicate redraw is unaffected.
          added.forEach(function (id) {
            try { var sh = api.getShapeById(id); if (sh && sh.setProperties) sh.setProperties({ frozen: true }); } catch (e) {}
          });
          // Persist to the account layout so the lines survive a reload and show up
          // wherever the user opens this layout. autosave() is TradingView's own
          // silent debounced save (no "Save as" dialog); only actually persists when
          // logged in AND on a saved/named layout. Fall back to saveChart().
          var saved = false;
          try {
            var A = window.TradingViewApi;
            if (A && typeof A.autosave === 'function') { A.autosave(); saved = true; }
            else if (A && typeof A.saveChart === 'function') { A.saveChart(); saved = true; }
          } catch (e) {}
          resolve({ ok: true, symbol: SYM, added: added.length, total: after.length, saved: saved });
        }, 350);
      }

      // set the symbol first if the chart isn't already on it, then draw
      var cur = '';
      try { cur = (chart.symbol() || '').toUpperCase(); } catch (e) {}
      if (cur && cur.indexOf(KEY) >= 0) {
        draw();
      } else {
        try { chart.setSymbol(SYM, {}); } catch (e) { return resolve({ ok: false, error: 'setSymbol failed: ' + e.message }); }
        setTimeout(draw, 900);
      }
    } catch (e) {
      resolve({ ok: false, error: e.message });
    }
  });
})()
`;
}

// --- HTTP server -------------------------------------------------------------

function send(res, code, obj, cors) {
  const body = JSON.stringify(obj);
  res.writeHead(code, {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': cors || '*',
    'Access-Control-Allow-Methods': 'GET, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Allow-Private-Network': 'true',
    'Cache-Control': 'no-store',
  });
  res.end(body);
}

function sendHtml(res, html) {
  res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(html);
}

const server = http.createServer(async (req, res) => {
  const origin = req.headers.origin || '*';
  const url = new URL(req.url, `http://127.0.0.1:${BRIDGE_PORT}`);

  if (req.method === 'OPTIONS') {
    // Chrome Private Network Access: a public origin (app.tradehunter.net) calling a
    // local address (127.0.0.1) sends a preflight with Access-Control-Request-Private-
    // Network: true and REQUIRES this allow header back, or the fetch is blocked.
    res.writeHead(204, {
      'Access-Control-Allow-Origin': origin,
      'Access-Control-Allow-Methods': 'GET, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
      'Access-Control-Allow-Private-Network': 'true',
    });
    return res.end();
  }

  // Health: is the bridge up and can it see a TV tab?
  if (url.pathname === '/health') {
    try {
      const t = await findTvTarget();
      return send(res, 200, { ok: true, bridge: 'up', version: BRIDGE_VERSION, tv_tab: t.url }, origin);
    } catch (e) {
      return send(res, 200, { ok: false, bridge: 'up', version: BRIDGE_VERSION, tv_tab: null, error: e.message }, origin);
    }
  }

  // Plot MATP / MBP on the user's TV.
  if (url.pathname === '/plot') {
    const symbol = (url.searchParams.get('symbol') || '').trim().toUpperCase();
    const exchange = (url.searchParams.get('exchange') || '').trim().toUpperCase();
    const matpRaw = url.searchParams.get('matp');
    const mbpRaw = url.searchParams.get('mbp');
    const nav = url.searchParams.get('nav') === '1'; // window.open fallback wants HTML
    const matp = matpRaw != null && matpRaw !== '' && matpRaw !== '-' ? Number(matpRaw) : null;
    const mbp = mbpRaw != null && mbpRaw !== '' && mbpRaw !== '-' ? Number(mbpRaw) : null;

    if (!symbol) {
      if (nav) return sendHtml(res, htmlResult(false, 'missing symbol'));
      return send(res, 400, { ok: false, error: 'missing symbol' }, origin);
    }
    const tvSym = exchange ? `${exchange}:${symbol}` : symbol;

    try {
      console.log(`[tv-bridge] plot ${tvSym} matp=${matp} mbp=${mbp} …`);
      const result = await cdpEval(buildPlotExpr({ symbol: tvSym, matp, mbp }));
      console.log(`[tv-bridge] plot ${tvSym} ->`, JSON.stringify(result));
      if (nav) return sendHtml(res, htmlResult(!!(result && result.ok), (result && result.error) || '', symbol));
      return send(res, 200, result || { ok: false, error: 'no result' }, origin);
    } catch (e) {
      console.error(`[tv-bridge] plot ${tvSym} ERROR:`, e.message);
      if (nav) return sendHtml(res, htmlResult(false, e.message, symbol));
      return send(res, 502, { ok: false, error: e.message }, origin);
    }
  }

  return send(res, 404, { ok: false, error: 'not found' }, origin);
});

// Tiny self-closing page for the window.open() fallback (used only if a browser
// blocks the direct loopback fetch as mixed content).
function htmlResult(ok, err, sym) {
  const msg = ok ? `Plotted ${sym || ''} on TradingView ✓` : `Couldn't plot: ${err || 'error'}`;
  return `<!doctype html><meta charset="utf-8"><title>TV bridge</title>
<body style="font:14px system-ui;background:#0f172a;color:#e2e8f0;margin:0;display:grid;place-items:center;height:100vh">
<div style="text-align:center">${msg}<div style="color:#64748b;font-size:12px;margin-top:8px">this tab closes itself…</div></div>
<script>setTimeout(function(){window.close();},900);</script></body>`;
}

server.listen(BRIDGE_PORT, '127.0.0.1', () => {
  console.log(`[tv-bridge] v${BRIDGE_VERSION} listening on http://127.0.0.1:${BRIDGE_PORT}`);
  console.log(`[tv-bridge] will drive Chrome CDP at ${CDP_HOST}:${CDP_PORT} (open a tradingview.com/chart tab there)`);
  console.log('[tv-bridge] endpoints: /health  /plot?symbol=NVDA&exchange=NASDAQ&matp=180.5&mbp=165.0');
});

server.on('error', (e) => {
  if (e.code === 'EADDRINUSE') {
    console.error(`[tv-bridge] port ${BRIDGE_PORT} already in use — is the bridge already running?`);
    process.exit(1);
  }
  throw e;
});
