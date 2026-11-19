// The data is gzipped JSON in base64: a data: URL decodes it, DecompressionStream unpacks it.
const D = await (async () => {
  const res = await fetch('data:application/gzip;base64,' + document.getElementById('data').textContent.trim());
  return new Response(res.body.pipeThrough(new DecompressionStream('gzip'))).json();
})();
// Band ends come as per-mille of the median away from it.
for (const m of D.machines) for (const b of Object.values(m.bench)) for (const s of Object.values(b)) {
  s.lo = s.lo.map((d, i) => d == null ? null : s.m[i] - Math.abs(s.m[i]) * d / 1000);
  s.hi = s.hi.map((d, i) => d == null ? null : s.m[i] + Math.abs(s.m[i]) * d / 1000);
}
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const FONT = getComputedStyle(document.body).fontFamily;
const H = 180, MAX_CASES = 5, GRID_MAX = 12;
const state = { machine: 0, metric: D.metrics[0].key, range: null, layout: 'auto', sort: 'attention' };
for (const [k, v] of new URLSearchParams(location.hash.slice(1))) {
  if (k === 'm' && D.machines[+v]) state.machine = +v;
  if (k === 'metric' && D.metrics.some(m => m.key === v)) state.metric = v;
  if (k === 'r' && /^\d+-\d+$/.test(v)) state.range = v.split('-').map(Number);
  if (k === 'layout' && ['auto', 'grid', 'table'].includes(v)) state.layout = v;
  if (k === 'sort' && ['attention', 'change', 'name'].includes(v)) state.sort = v;
}
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const M = () => D.machines[state.machine];
const meta = () => D.metrics.find(m => m.key === state.metric);
const nRuns = () => M().points.length;
const run = pos => M().points[pos];
// Dirty points and points off the history are drawn, but aren't part of its line or steps.
const usable = pos => { const p = run(pos); return !p.dirty && !p.off; };
// The history's commits after point a up to and including point b, or null when either is off it.
function between(a, b) {
  const ca = run(a)?.ci, cb = run(b)?.ci;
  return ca == null || cb == null ? null : D.chain.slice(Math.min(ca, cb) + 1, Math.max(ca, cb) + 1);
}
const commitLink = (c, text = c.slice(0, 7)) => D.commitUrl ? `<a href="${esc(D.commitUrl.replace('{commit}', c))}" target="_blank" rel="noopener"><code>${esc(text)}</code></a>` : `<code>${esc(text)}</code>`;
function rangeText(a, b) {
  // Without the repository there is no history: name the point's own commit.
  if (!D.chain.length) return run(b).c ? `${commitLink(run(b).c)} <span class="subj">${esc(run(b).s)}</span>` : `<span class="hint">#${b + 1}</span>`;
  const cs = between(a, b);
  if (!cs) return `<span class="hint">not in the history of ${esc(D.rev)}</span>`;
  if (cs.length === 1) return `${commitLink(cs[0][0])} <span class="subj">${esc(cs[0][1])}</span>`;
  const link = D.compareUrl ? ` <a href="${esc(D.compareUrl.replace('{base}', run(a).c).replace('{head}', run(b).c))}" target="_blank" rel="noopener">compare</a>` : '';
  return `<code>${esc(run(a).c.slice(0, 7))}..${esc(run(b).c.slice(0, 7))}</code> <span class="subj" title="${esc(cs.map(([h, t]) => h.slice(0, 7) + ' ' + t).join('\n'))}">${cs.length} commits</span>${link}`;
}
const short = pos => { const r = run(pos); return r ? (r.c ? r.c.slice(0, 7) : '#' + (pos + 1)) : ''; };
const esc = s => String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
const natural = (a, b) => a.localeCompare(b, undefined, { numeric: true });
const median = a => { const s = [...a].sort((x, y) => x - y), h = s.length >> 1; return s.length % 2 ? s[h] : (s[h - 1] + s[h]) / 2; };
const push = (map, k, v) => (map.get(k) || map.set(k, []).get(k)).push(v);
const colors = () => [1, 2, 3, 4, 5].map(i => css('--s' + i));

const UNITS = {
  time: [['s', 1e9], ['ms', 1e6], ['µs', 1e3], ['ns', 1]],
  bytes: [['GiB', 2 ** 30], ['MiB', 2 ** 20], ['KiB', 1024], ['B', 1]],
  bytes_rate: [['GiB/s', 2 ** 30], ['MiB/s', 2 ** 20], ['KiB/s', 1024], ['B/s', 1]],
  rate: [['G/s', 1e9], ['M/s', 1e6], ['k/s', 1e3], ['/s', 1]],
  count: [['G', 1e9], ['M', 1e6], ['k', 1e3], ['', 1]],
};
function fmt(v, kind = meta().kind) {
  if (v == null || !isFinite(v)) return '–';
  if (kind === 'ratio') return (+v.toPrecision(3)) + '×';
  const a = Math.abs(v);
  for (const [u, s] of UNITS[kind]) if (a >= s) return (+(v / s).toPrecision(3)) + (u ? ' ' + u : '');
  return (+v.toPrecision(3)) + ' ' + UNITS[kind].at(-1)[0];
}
const pct = d => (d > 0 ? '+' : '') + (d * 100).toFixed(Math.abs(d) < 0.1 ? 1 : 0) + '%';
// allowed: a worse step within an allowlist rule's threshold, which mew compare's gate lets pass.
function stepClass(d, allowed = false) {
  const dir = meta().dir;
  return !dir ? 'neutral' : ((d > 0) === (dir === 'lower') ? (allowed ? 'allowed' : 'worse') : 'better');
}
const stepText = s => pct(s.d) + (s.k > 1 ? ` ×${s.k}` : '') + (s.a ? ' allowed' : '');

// ---- items: a benchmark, or a family of 2-5 cases drawn as one chart ----------------
let items = [];
const S = name => M().bench[name][state.metric];
function lastPos(s) { for (let i = s.m.length - 1; i >= 0; i--) if (s.m[i] != null && usable(i)) return i; return -1; }
function buildItems() {
  const fams = new Map();
  for (const n of Object.keys(M().bench).filter(n => M().bench[n][state.metric]).sort(natural)) {
    const b = n.indexOf('[');
    push(fams, b < 0 ? n : n.slice(0, b), n);
  }
  const out = [];
  for (const [key, cases] of fams) {
    if (cases.length >= 2 && cases.length <= MAX_CASES) out.push({ key, cases, family: true });
    else for (const c of cases) out.push({ key: c, cases: [c], family: false });
  }
  out.forEach(prepare);
  return out;
}
function prepare(it) {
  const ss = it.cases.map(S), breaks = new Set(), steps = new Map();
  for (const s of ss) {
    let prev;
    for (let i = 0; i < s.m.length; i++) {
      if (s.m[i] == null) continue;
      if (prev !== undefined && s.v[i] !== prev) breaks.add(i);
      prev = s.v[i];
    }
    for (const [i, prev, d, , a] of s.steps) push(steps, i, [prev, d, a]);
  }
  it.breaks = [...breaks].sort((a, b) => a - b);
  it.steps = [...steps].map(([i, xs]) => ({ i, prev: Math.min(...xs.map(x => x[0])), d: median(xs.map(x => x[1])), k: xs.length, a: xs.every(x => x[2]) })).sort((a, b) => a.i - b.i);
  // The noisiest case speaks for a family, and so does an allowlist rule one case has.
  const sds = ss.map(s => s.noise).filter(x => x != null);
  it.noise = sds.length ? Math.max(...sds) : null;
  it.rule = ss.find(s => s.rule)?.rule || null;
  // The smallest step looked for, as in Python: a stricter rule lowers the threshold.
  it.threshold = it.rule?.[0] != null ? Math.min(D.threshold, it.rule[0] / 100) : D.threshold;
  it.last = Math.max(...ss.map(lastPos));
  // A family's cases differ by orders of magnitude: draw each over its typical value,
  // per version, so they share an axis and a step in all of them lines up.
  if (it.family) it.norm = ss.map(s => {
    const by = new Map();
    s.m.forEach((m, i) => { if (m != null && usable(i)) push(by, s.v[i], m); });
    const ref = new Map([...by].map(([v, ms]) => [v, median(ms)]));
    return s.m.map((m, i) => m == null || !ref.has(s.v[i]) ? null : m / ref.get(s.v[i]));
  });
}
const label = (it, name) => name.slice(it.key.length).replace(/^\[|\]$/g, '');
function title(it) {
  const i = it.key.lastIndexOf('::');
  return `<span>${esc(it.key.slice(0, i + 2))}</span>${esc(it.key.slice(i + 2))}` + (it.family ? `<em>${it.cases.length} cases</em>` : '') + noiseHTML(it);
}
// "±1.4%": the run-to-run noise, amber when NOISE_K times it is past the threshold,
// since then only steps past NOISE_K times the noise count.
function noiseHTML(it) {
  const rule = it.rule && `Allowlisted${it.rule[0] == null ? ', exempt' : ` to ${it.rule[0]}%`}: ${it.rule[1]}`;
  if (it.noise == null) return rule ? `<em title="${esc(rule)}">allowlisted</em>` : '';
  const least = D.noiseK * it.noise, noisy = least > it.threshold;
  const tip = [
    `Run-to-run noise of the point medians${it.family ? ', noisiest case' : ''}.`,
    noisy && `A step must clear ${pct(least).slice(1)} (${D.noiseK}× the noise), not ${pct(it.threshold).slice(1)}.`,
    rule,
  ].filter(Boolean).join(' ');
  return `<em class="noise${noisy ? ' noisy' : ''}" title="${esc(tip)}">±${(it.noise * 100).toFixed(1)}%${rule ? ' · allowlisted' : ''}</em>`;
}
const layout = () => state.layout !== 'auto' ? state.layout : (items.length > GRID_MAX ? 'table' : 'grid');

// ---- range: one x window shared by every chart ------------------------------------
let plots = new Map(), overview = null, syncing = false;
const full = () => [-0.5, nRuns() - 0.5];
const cur = () => state.range ? [state.range[0] - 0.5, state.range[1] + 0.5] : full();
const inView = pos => { const [a, b] = cur(); return pos >= a && pos <= b; };
function setRange(r, src) {
  if (r) {
    let [a, b] = [Math.max(0, Math.ceil(r[0])), Math.min(nRuns() - 1, Math.floor(r[1]))];
    if (b - a < 4) { const c = (a + b) / 2; a = Math.max(0, Math.floor(c - 2)); b = Math.min(nRuns() - 1, a + 4); }
    r = (a === 0 && b === nRuns() - 1) ? null : [a, b];
  }
  state.range = r;
  const [min, max] = cur();
  syncing = true;
  for (const u of plots.values()) if (u !== src || !r) u.setScale('x', { min, max });
  syncing = false;
  overview && overview.redraw(false);
  if (layout() === 'table') updateTable();
  renderSteps(); applyFilter(); writeHash(); markPreset();
}

// ---- canvas helpers ----------------------------------------------------------------
function vline(u, x, color, dash, text) {
  const ctx = u.ctx, px = u.valToPos(x, 'x', true), { top, height, left, width } = u.bbox;
  if (px < left - 1 || px > left + width + 1) return;
  const r = uPlot.pxRatio;
  ctx.save(); ctx.strokeStyle = color; ctx.lineWidth = r; ctx.setLineDash(dash.map(d => d * r));
  ctx.beginPath(); ctx.moveTo(px, top - 2 * r); ctx.lineTo(px, top + height); ctx.stroke();
  if (text) {
    ctx.setLineDash([]); ctx.fillStyle = color; ctx.font = `600 ${11 * r}px ${FONT}`;
    const right = px + 4 * r + ctx.measureText(text).width > left + width;
    ctx.textAlign = right ? 'right' : 'left';
    ctx.fillText(text, px + (right ? -4 : 4) * r, top - 5 * r);
  }
  ctx.restore();
}
function axes(kind) {
  const common = { stroke: css('--muted'), grid: { stroke: css('--grid'), width: 1 }, ticks: { show: false }, font: `10px ${FONT}` };
  return [
    { ...common, space: 70, size: 22, incrs: [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000, 10000],
      values: (u, s) => s.map(v => Number.isInteger(v) ? short(v) : '') },
    { ...common, size: 66, values: (u, s) => s.map(v => fmt(v, kind)) },
  ];
}
const yRange = (u, min, max) => {
  if (min == null) return [0, 1];
  const pad = (max - min) * 0.1 || Math.abs(max) * 0.05 || 1;
  return [min - pad, max + pad];
};

// ---- the full chart for one item -----------------------------------------------------
function chartData(it) {
  // Break lines where the code changed, with a null half-way between the runs.
  const br = new Set(it.breaks), xs = [];
  for (let i = 0; i < nRuns(); i++) { if (br.has(i)) xs.push(i - 0.5); xs.push(i); }
  // null breaks a line (a version change, a missing measurement); uPlot spans undefined,
  // so the history's line runs past dirty and branch points, drawn hollow on their own.
  const col = (arr, aside = false) => xs.map(x => !Number.isInteger(x) ? null : usable(x) !== aside ? arr[x] : (aside ? null : undefined));
  if (it.family) return [xs, ...it.norm.map(l => col(l)), ...it.norm.map(l => col(l, true))];
  const s = S(it.cases[0]);
  return [xs, col(s.hi), col(s.lo), col(s.m), col(s.m, true)];
}
function valueHTML(it, pos) {
  const p = pos ?? it.last;
  if (p < 0) return '';
  let lastUsable = nRuns() - 1; while (lastUsable > 0 && !usable(lastUsable)) lastUsable--;
  const at = pos != null ? short(p) : p === lastUsable ? 'latest' : `last run, ${short(p)}`;
  if (!it.family) {
    const s = S(it.cases[0]);
    if (s.m[p] == null) return `${at}: not run`;
    return `${at}: <b>${fmt(s.m[p])}</b> (${fmt(s.lo[p])} – ${fmt(s.hi[p])})`;
  }
  const cs = colors();
  return `${at}: ` + it.cases.map((c, j) => `<span class="lg"><i style="background:${cs[j]}"></i>${esc(label(it, c))} <b>${fmt(S(c).m[p])}</b></span>`).join('');
}
function makePlot(el, valEl, it) {
  const cs = colors(), card = css('--card');
  const aside = c => ({ stroke: c, paths: () => null, points: { show: true, size: 7, width: 1.5, fill: card, stroke: c } });
  const series = it.family
    ? [{}, ...it.cases.map((c, j) => ({ stroke: cs[j], width: 1.5, spanGaps: false, points: { size: 4, fill: cs[j], stroke: cs[j], space: 9 } })),
       ...it.cases.map((c, j) => aside(cs[j]))]
    : [{}, { stroke: 'transparent', points: { show: false } }, { stroke: 'transparent', points: { show: false } },
       { stroke: cs[0], width: 1.5, spanGaps: false, points: { size: 4, fill: cs[0], stroke: cs[0], space: 7 } }, aside(cs[0])];
  let down = null;
  const u = new uPlot({
    width: el.clientWidth, height: H, padding: [18, 10, 0, 0], legend: { show: false },
    scales: { x: { time: false }, y: { range: yRange } },
    axes: axes(it.family ? 'ratio' : meta().kind),
    series, bands: it.family ? [] : [{ series: [1, 2], fill: css('--band') }],
    cursor: {
      sync: { key: 'mew-' + state.machine, setSeries: false },
      drag: { x: true, y: false, setScale: true },
      points: { size: 8 },
      bind: { dblclick: () => () => { setRange(null); return null; } },
    },
    hooks: {
      draw: [u => {
        for (const b of it.breaks) vline(u, b - 0.5, css('--changed'), [5, 3], '≠ code changed');
        for (const s of it.steps) vline(u, s.i - 0.5, css(s.a ? '--muted' : '--' + stepClass(s.d)), [2, 2], stepText(s));
      }],
      setScale: [(u, key) => { if (key === 'x' && u._ready && !syncing) setRange([u.scales.x.min, u.scales.x.max], u); }],
      setCursor: [u => {
        const i = u.cursor.idx, x = i == null ? null : u.data[0][i], pos = Number.isInteger(x) ? x : null;
        valEl.innerHTML = valueHTML(it, pos);
        if (u.cursor.event) showInfo(pos, it);
      }],
    },
  }, chartData(it), el);
  u.over.addEventListener('mousedown', e => { down = e.clientX; });
  u.over.addEventListener('click', e => {
    const i = u.cursor.idx, x = i == null ? null : u.data[0][i];
    if (Math.abs(e.clientX - down) > 3 || !Number.isInteger(x)) return;
    if (e.shiftKey && pin && pin.it === it) openCompare(it, pin.pos, x);
    else { pin = { it, pos: x }; openCompare(it, x, null); }
  });
  syncing = true; const [min, max] = cur(); u.setScale('x', { min, max }); syncing = false;
  u._ready = true; u._el = el;
  return u;
}
function showInfo(pos, it) {
  const el = $('#info');
  if (pos == null) {
    el.innerHTML = 'Hover a chart for a run, drag to zoom every chart, double-click to reset. Click a point to pin it, shift-click another to compare.';
    return;
  }
  const r = run(pos), s = it && !it.family ? S(it.cases[0]) : null, ver = s && s.v[pos] != null ? D.versions[s.v[pos]] : null;
  let prev = pos - 1; while (prev >= 0 && !usable(prev)) prev--;
  const since = usable(pos) && prev >= 0 ? between(prev, pos) : null;
  const flags = [r.k > 1 && `${r.k} runs pooled`, r.dirty && 'dirty: uncommitted edits', r.off && `not in the history of ${D.rev}`, r.rw && 'rewritten since it ran'].filter(Boolean);
  el.innerHTML = `<b>#${pos + 1}</b> · <b>${esc(r.c.slice(0, 10))}</b> ${esc(r.s)} · ${esc(r.d)}` + (r.n ? ` · ${esc(r.n)}` : '') +
    (since && since.length > 1 ? ` · ${since.length} commits since the previous point` : '') +
    flags.map(f => ` · <span class="flag">${f}</span>`).join('') + (ver ? ` · ${esc(ver)}` : '');
}

// ---- two points compared: what changed, the commits between, a mew compare command ----
let pin = null;
const shortName = it => it.key.slice(it.key.lastIndexOf('::') + 2);
const shq = t => /^[\w./:@=+-]+$/.test(t) ? t : `'${t.replace(/'/g, `'\\''`)}'`;
function compareCommand(it, a, b) {
  // mew compare pools a file's runs and applies --where to every file alike, so cut
  // each point's runs out of the archive first: run ids are unique in the rows.
  const f = D.title, gz = f.endsWith('.gz');
  const sel = (pos, out) => (gz ? `gzip -dc ${shq(f)} | grep -F` : 'grep -F') +
    ' ' + run(pos).ids.map(id => `-e ${id}`).join(' ') + (gz ? '' : ' ' + shq(f)) + ` > ${out}`;
  return [sel(a, 'base.jsonl'), sel(b, 'head.jsonl'), `mew compare base.jsonl head.jsonl -F -k ${shq(it.key)}`].join('\n');
}
function openCompare(it, a, b) {
  const el = $('#cmp');
  el.hidden = false;
  const close = '<button class="x" aria-label="Close">×</button>';
  if (b == null || a === b) {
    el.innerHTML = `<div class="cmp-head"><span><b>Pinned #${a + 1}</b> ${commitLink(run(a).c)} ${esc(run(a).s)}. Shift-click another point of ${esc(shortName(it))} to compare.</span>${close}</div>`;
  } else {
    if (a > b) [a, b] = [b, a];
    pin = { it, pos: a };
    const rows = it.cases.map(c => {
      const s = S(c), x = s.m[a], y = s.m[b], nm = it.family ? label(it, c) : shortName(it);
      if (x == null || y == null) return `<li><span class="delta hint">–</span>${esc(nm)}: not measured at both points</li>`;
      const d = y / x - 1, apart = s.hi[a] < s.lo[b] || s.hi[b] < s.lo[a];
      return `<li><span class="delta ${stepClass(d)}">${pct(d)}</span>${esc(nm)}: ${fmt(x)} → ${fmt(y)} ` +
        `<span class="hint">(repetitions ${apart ? "don't overlap" : 'overlap'})</span></li>`;
    });
    const cs = between(a, b);
    const commits = cs
      ? `<details${cs.length <= 8 ? ' open' : ''}><summary>${cs.length} commit${cs.length === 1 ? '' : 's'} after #${a + 1}, up to #${b + 1}</summary>` +
        `<ol>${cs.slice(-300).map(([h, t]) => `<li>${commitLink(h)} ${esc(t)}</li>`).join('')}</ol></details>`
      : `<p class="hint">${D.chain.length ? `A point is not in the history of ${esc(D.rev)}, so there is no commit range between them.` : "Run mew report from the project's repository to list the commits between the points."}</p>`;
    el.innerHTML = `<div class="cmp-head"><span><b>#${a + 1}</b> ${commitLink(run(a).c)} → <b>#${b + 1}</b> ${commitLink(run(b).c)} · ${esc(it.key)}</span>${close}</div>` +
      `<ul class="cmp-rows">${rows.join('')}</ul>${commits}` +
      `<div class="cmd"><pre>${esc(compareCommand(it, a, b))}</pre><button class="copy">Copy</button></div>`;
    el.querySelector('.copy').addEventListener('click', e => {
      navigator.clipboard?.writeText(compareCommand(it, a, b)).then(() => { e.target.textContent = 'Copied'; });
    });
  }
  el.querySelector('.x').addEventListener('click', () => { el.hidden = true; pin = null; });
}

// ---- overview: suite index over the machine's whole history -----------------------
function allSteps() {
  const out = [];
  for (const [name, b] of Object.entries(M().bench)) { const s = b[state.metric]; if (s) for (const [i, prev, d, p, a] of s.steps) out.push({ name, i, prev, d, p, a }); }
  return out.sort((a, b) => b.i - a.i || Math.abs(b.d) - Math.abs(a.d));
}
function makeOverview() {
  const el = $('#overview'), idx = M().index[state.metric], steps = allSteps();
  $('#suite-hint').textContent = 'Geometric mean of every benchmark over its typical value (1× = typical' + (meta().dir ? ', up = worse' : '') + '). Drag to choose the range.';
  overview = new uPlot({
    width: el.clientWidth, height: 110, legend: { show: false },
    scales: { x: { time: false, range: () => full() }, y: { range: yRange } },
    axes: axes('ratio'),
    series: [{}, { stroke: css('--s1'), width: 1.25, spanGaps: true, points: { show: false } }],
    cursor: { drag: { x: true, y: false, setScale: false }, points: { show: false }, sync: { key: 'mew-' + state.machine, setSeries: false },
      bind: { dblclick: () => () => { setRange(null); return null; } } },
    hooks: {
      draw: [u => {
        const ctx = u.ctx, r = uPlot.pxRatio, { top, height } = u.bbox;
        for (const s of steps) {
          ctx.fillStyle = css(s.a ? '--muted' : '--' + stepClass(s.d));
          ctx.fillRect(u.valToPos(s.i - 0.5, 'x', true) - r, top + height - 7 * r, 2 * r, 7 * r);
        }
        if (state.range) {
          const [a, b] = cur(), l = u.valToPos(a, 'x', true), w = u.valToPos(b, 'x', true) - l;
          ctx.fillStyle = css('--sel'); ctx.fillRect(l, top, w, height);
          ctx.strokeStyle = css('--s1'); ctx.lineWidth = r; ctx.strokeRect(l, top, w, height);
        }
      }],
      setSelect: [u => {
        if (u.select.width <= 2) return;
        setRange([u.posToVal(u.select.left, 'x'), u.posToVal(u.select.left + u.select.width, 'x')]);
        u.setSelect({ left: 0, width: 0, top: 0, height: 0 }, false);
      }],
      setCursor: [u => { if (u.cursor.event) showInfo(u.cursor.idx ?? null, null); }],
    },
  }, [idx.map((_, i) => i), idx], el);
}

// ---- steps list ------------------------------------------------------------------------
function renderSteps() {
  const steps = allSteps().filter(s => inView(s.i));
  $('#steps-title').textContent = `Steps in view (${steps.length}), newest first`;
  $('#steps').innerHTML = steps.slice(0, 300).map(s => {
    const fn = s.name.slice(s.name.lastIndexOf('::') + 2);
    return `<li data-i="${s.i}" data-prev="${s.prev}" data-name="${esc(s.name)}" title="p = ${s.p}${s.a ? ', allowed by the allowlist' : ''}"><span class="delta ${stepClass(s.d, s.a)}">${pct(s.d)}</span>` +
      `<span class="name">${esc(fn)}</span>${rangeText(s.prev, s.i)}</li>`;
  }).join('') || '<li class="hint">None in this range.</li>';
}
$('#steps').addEventListener('click', e => {
  const li = e.target.closest('li[data-i]'); if (!li) return;
  const i = +li.dataset.i, w = Math.max(30, Math.round(nRuns() / 10)), j = items.findIndex(it => it.cases.includes(li.dataset.name));
  setRange([i - w, i + w]);
  if (j < 0) return;
  openCompare(items[j], +li.dataset.prev, i);
  const el = layout() === 'grid' ? $(`.card[data-j="${j}"]`) : $(`tr.brow[data-j="${j}"]`);
  if (!el) return;
  el.hidden = false;
  if (layout() === 'table') openDetail(el, true);
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
});

// ---- grid layout -----------------------------------------------------------------------
const io = new IntersectionObserver(es => {
  for (const e of es) if (e.isIntersecting) {
    io.unobserve(e.target);
    const it = items[+e.target.dataset.j];
    plots.set(it.key, makePlot(e.target.querySelector('.plot'), e.target.querySelector('.val'), it));
  }
}, { rootMargin: '600px 0px' });
function renderGrid() {
  $('#cards').innerHTML = items.map((it, j) =>
    `<article class="card" data-j="${j}"><h3>${title(it)}</h3><div class="val"></div><div class="plot"></div></article>`).join('');
  for (const c of $$('.card')) { c.querySelector('.val').innerHTML = valueHTML(items[+c.dataset.j], null); io.observe(c); }
}

// ---- table layout: a sparkline row per item, expanding to the full chart ------------
function changeInView(it) {
  // Median of the first runs in view against the last, within the newest version in view.
  const [a, b] = cur();
  let best = null;
  for (const c of it.cases) {
    const s = S(c), pts = [];
    for (let i = Math.max(0, Math.ceil(a)); i <= Math.min(nRuns() - 1, Math.floor(b)); i++) if (s.m[i] != null && usable(i)) pts.push(i);
    if (!pts.length) continue;
    const seg = pts.filter(i => s.v[i] === s.v[pts.at(-1)]);
    if (seg.length < 2) continue;
    const k = Math.max(1, Math.min(5, seg.length >> 1));
    const d = median(seg.slice(-k).map(i => s.m[i])) / median(seg.slice(0, k).map(i => s.m[i])) - 1;
    if (best == null || Math.abs(d) > Math.abs(best)) best = d;
  }
  return best;
}
function drawSpark(cv, it) {
  const w = cv.clientWidth, h = cv.clientHeight, r = devicePixelRatio || 1;
  if (!w) return;
  cv.width = Math.round(w * r); cv.height = Math.round(h * r);
  const ctx = cv.getContext('2d'); ctx.setTransform(r, 0, 0, r, 0, 0);
  const [a, b] = cur(), i0 = Math.max(0, Math.ceil(a)), i1 = Math.min(nRuns() - 1, Math.floor(b));
  const lines = it.family ? it.norm : [S(it.cases[0]).m], cs = it.family ? colors() : [css('--s1')];
  let lo = Infinity, hi = -Infinity;
  for (const l of lines) for (let i = i0; i <= i1; i++) if (l[i] != null) { lo = Math.min(lo, l[i]); hi = Math.max(hi, l[i]); }
  if (!isFinite(lo)) return;
  // Span at least the step threshold either side of the middle, so noise on a flat
  // benchmark draws flat instead of filling the row like a real change.
  const mid = (lo + hi) / 2, half = Math.max((hi - lo) / 2 * 1.08, Math.abs(mid) * it.threshold) || 1;
  lo = mid - half; hi = mid + half;
  const X = i => (i - a) / (b - a) * w, Y = v => h - (v - lo) / (hi - lo) * h;
  for (const br of it.breaks) if (inView(br)) { ctx.fillStyle = css('--changed'); ctx.fillRect(X(br - 0.5) - 0.5, 0, 1, h); }
  for (const s of it.steps) if (inView(s.i)) { ctx.fillStyle = css(s.a ? '--muted' : '--' + stepClass(s.d)); ctx.fillRect(X(s.i - 0.5) - 1, 0, 2, h); }
  const brs = new Set(it.breaks);
  lines.forEach((l, j) => {
    ctx.strokeStyle = cs[j]; ctx.lineWidth = 1.25; ctx.lineJoin = 'round'; ctx.beginPath();
    let pen = false;
    for (let i = i0; i <= i1; i++) {
      if (l[i] == null || brs.has(i) || !usable(i)) pen = false;
      if (l[i] == null || !usable(i)) continue;
      pen ? ctx.lineTo(X(i), Y(l[i])) : ctx.moveTo(X(i), Y(l[i]));
      pen = true;
    }
    ctx.stroke();
  });
}
function renderTable() {
  $('#tbody').innerHTML = items.map((it, j) =>
    `<tr class="brow" data-j="${j}"><td class="nm">${title(it)}</td><td class="sp"><canvas class="spark"></canvas></td>` +
    `<td class="num latest"></td><td class="num chg"></td><td class="badges"></td></tr>`).join('');
  updateTable();
}
function updateTable() {
  const rows = $$('#tbody tr.brow'), info = new Map();
  for (const tr of rows) {
    const it = items[+tr.dataset.j], d = changeInView(it), steps = it.steps.filter(s => inView(s.i)).reverse();
    const [, b] = cur(), end = Math.min(nRuns() - 1, Math.floor(b));
    let p = end; while (p >= 0 && !(usable(p) && it.cases.some(c => S(c).m[p] != null))) p--;
    tr.querySelector('.latest').innerHTML = it.family ? `<span class="hint">${it.cases.length} cases</span>` : fmt(p >= 0 ? S(it.cases[0]).m[p] : null);
    const big = d != null && Math.abs(d) >= it.threshold;
    tr.querySelector('.chg').innerHTML = d == null ? '–' : `<span class="${big ? stepClass(d, it.rule && (it.rule[0] == null || Math.abs(d) * 100 <= it.rule[0])) : 'hint'}">${pct(d)}</span>`;
    tr.querySelector('.badges').innerHTML = steps.slice(0, 3).map(s =>
      `<span class="badge ${stepClass(s.d, s.a)}" title="${esc(short(s.i) + ' ' + run(s.i).s)}">${stepText(s)}</span>`).join('') +
      (steps.length > 3 ? `<span class="hint">+${steps.length - 3}</span>` : '');
    drawSpark(tr.querySelector('canvas.spark'), it);
    info.set(tr, { d, last: steps.length ? steps[0].i : -1, name: it.key });
  }
  const cmp = {
    name: (a, b) => natural(a.name, b.name),
    change: (a, b) => Math.abs(b.d ?? 0) - Math.abs(a.d ?? 0) || natural(a.name, b.name),
    attention: (a, b) => b.last - a.last || Math.abs(b.d ?? 0) - Math.abs(a.d ?? 0) || natural(a.name, b.name),
  }[state.sort];
  const tbody = $('#tbody');
  for (const tr of [...rows].sort((x, y) => cmp(info.get(x), info.get(y)))) {
    const detail = tr.nextElementSibling?.classList.contains('detail') ? tr.nextElementSibling : null;
    tbody.append(tr);
    if (detail) tbody.append(detail);
  }
}
function openDetail(tr, open) {
  const it = items[+tr.dataset.j], next = tr.nextElementSibling, isOpen = next?.classList.contains('detail');
  if (isOpen) {
    if (open) return;
    plots.get(it.key)?.destroy(); plots.delete(it.key); next.remove(); tr.classList.remove('open');
    return;
  }
  const d = document.createElement('tr');
  d.className = 'detail';
  d.innerHTML = '<td colspan="5"><div class="val"></div><div class="plot"></div></td>';
  tr.after(d); tr.classList.add('open');
  d.querySelector('.val').innerHTML = valueHTML(it, null);
  plots.set(it.key, makePlot(d.querySelector('.plot'), d.querySelector('.val'), it));
}
$('#tbody').addEventListener('click', e => { const tr = e.target.closest('tr.brow'); if (tr) openDetail(tr); });

// ---- filter, hash, presets, rebuild ----------------------------------------------------
function applyFilter() {
  const q = $('#q').value.toLowerCase(), only = $('#only').checked;
  let shown = 0;
  for (const el of $$('.card, tr.brow')) {
    const it = items[+el.dataset.j];
    el.hidden = !it.cases.some(c => c.toLowerCase().includes(q)) || (only && !it.steps.some(s => inView(s.i)));
    if (el.nextElementSibling?.classList.contains('detail')) el.nextElementSibling.hidden = el.hidden;
    shown += !el.hidden;
  }
  $('#empty').hidden = shown > 0;
}
function writeHash() {
  const q = new URLSearchParams({ m: state.machine, metric: state.metric });
  if (state.range) q.set('r', state.range.join('-'));
  if (state.layout !== 'auto') q.set('layout', state.layout);
  if (state.sort !== 'attention') q.set('sort', state.sort);
  history.replaceState(null, '', '#' + q);
}
function markPreset() {
  const n = state.range ? state.range[1] - state.range[0] + 1 : null, atEnd = !state.range || state.range[1] === nRuns() - 1;
  for (const b of $$('.presets button'))
    b.classList.toggle('on', b.dataset.last === 'all' ? !state.range : atEnd && n === +b.dataset.last);
}
function rebuild() {
  for (const u of plots.values()) u.destroy();
  plots.clear(); io.disconnect();
  if (overview) overview.destroy();
  if (state.range && state.range[1] >= nRuns()) state.range = null;
  items = buildItems();
  const m = M(), benchmarks = items.reduce((n, it) => n + it.cases.length, 0);
  $('#meta').textContent = `${m.points.length} points (${m.points.reduce((n, p) => n + p.k, 0)} runs) on this machine, ordered ${D.chain.length ? `along the first-parent history of ${D.rev}` : 'by date'}, ${run(0).d.slice(0, 10)} – ${run(nRuns() - 1).d.slice(0, 10)} · ` +
    `${benchmarks} benchmarks in ${items.length} charts · median of repetitions, band min–max · ` +
    `steps: ≥${+(D.threshold * 100).toPrecision(4)}% and ≥${D.noiseK}× the noise between the medians of ${D.window} points each side, every point past every other (p < 0.01)`;
  for (const o of $('#metric').options) o.disabled = !Object.values(m.bench).some(b => b[o.value]);
  const mode = layout();
  $('#cards').hidden = mode !== 'grid'; $('#table').hidden = mode !== 'table'; $('#sort-wrap').hidden = mode !== 'table';
  $('#cards').innerHTML = ''; $('#tbody').innerHTML = '';
  makeOverview();
  mode === 'grid' ? renderGrid() : renderTable();
  renderSteps(); applyFilter(); writeHash(); markPreset(); showInfo(null);
}

$('#machine').innerHTML = D.machines.map((m, i) => `<option value="${i}">${esc(m.label)} (${m.points.reduce((n, p) => n + p.k, 0)} runs)</option>`).join('');
$('#metric').innerHTML = D.metrics.map(m => `<option value="${esc(m.key)}">${esc(m.label)}</option>`).join('');
$('#machine').value = state.machine; $('#metric').value = state.metric; $('#layout').value = state.layout; $('#sort').value = state.sort;
$('#machine').addEventListener('change', e => { state.machine = +e.target.value; state.range = null; rebuild(); });
$('#metric').addEventListener('change', e => { state.metric = e.target.value; rebuild(); });
$('#layout').addEventListener('change', e => { state.layout = e.target.value; rebuild(); });
$('#sort').addEventListener('change', e => { state.sort = e.target.value; updateTable(); writeHash(); });
$('#q').addEventListener('input', applyFilter); $('#only').addEventListener('change', applyFilter);
for (const b of $$('.presets button'))
  b.addEventListener('click', () => setRange(b.dataset.last === 'all' ? null : [nRuns() - +b.dataset.last, nRuns() - 1]));
let pending = 0;
new ResizeObserver(() => {
  cancelAnimationFrame(pending);
  pending = requestAnimationFrame(() => {
    for (const u of plots.values()) u.setSize({ width: u._el.clientWidth, height: H });
    if (overview) overview.setSize({ width: $('#overview').clientWidth, height: 110 });
    if (layout() === 'table') for (const tr of $$('#tbody tr.brow')) drawSpark(tr.querySelector('canvas.spark'), items[+tr.dataset.j]);
  });
}).observe($('main'));
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', rebuild);
rebuild();
