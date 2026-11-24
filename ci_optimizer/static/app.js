const REASON_LABELS = {
  coverage: '历史覆盖',
  dependency_d1: '一级依赖',
  dependency_graph: '反向依赖',
  domain_safety_net: '同域兜底',
  smoke: '冒烟',
  recent_failure: '近期失败',
  quarantined_monitor: '隔离观察',
  cofailure: '共同失败',
  failure_prediction: '风险预测',
  risk_budget: '安全追加',
};

const REASON_DESCRIPTIONS = {
  coverage: '历史覆盖率记录显示该测试覆盖本次修改的模块/文件。',
  dependency_d1: '该测试覆盖直接依赖本次改动的下游模块。',
  dependency_graph: '模块依赖图的反向传递闭包表明该测试可能受影响。',
  domain_safety_net: '安全档对改动业务域追加的整域兜底测试。',
  smoke: '跨域关键路径冒烟测试，始终运行。',
  recent_failure: '最近 7 次提交内失败过，保留以验证修复并防止反复。',
  quarantined_monitor: '已隔离的不稳定测试仍继续运行和记录，但失败不阻塞合并。',
  cofailure: '历史数据中，该测试曾与同类模块/层/改动规模一起失败。',
  failure_prediction: '跨模块风险模型给出的高失败概率候选。',
  risk_budget: '为把残余漏检估计压到安全预算内而追加的测试。',
};

const STATUS_LABELS = {
  quarantined: '隔离中',
  broken_suspect: '疑似破坏',
  healthy: '健康/已放回',
};

const CLASS_LABELS = {
  flaky: '真不稳定',
  flaky_then_broken: '先 flaky 后被弄坏',
  broken: '提交破坏',
  broken_episode: '历史破坏区间',
  unstable_suspect: '不稳定嫌疑',
  stable: '稳定',
};

let overviewMetrics = null;
let commitCache = [];
let activeSha = null;
let itemOffset = 0;
const itemLimit = 100;
let itemQuery = '';
let flakyTimer = null;
let testSearchTimer = null;

function $(sel) { return document.querySelector(sel); }
function $$(sel) { return Array.from(document.querySelectorAll(sel)); }

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
}
function pct(x, digits = 1) { return `${(Number(x || 0) * 100).toFixed(digits)}%`; }
function num(x) { return Number(x || 0).toLocaleString('zh-CN'); }
function minutes(sec) { return `${(Number(sec || 0) / 60).toFixed(1)} 分钟`; }
function shortSha(sha) { return String(sha || '').slice(0, 8); }
function debounce(fn, wait) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), wait); };
}

async function api(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

function showToast(message) {
  const el = $('#toast');
  el.textContent = message;
  el.classList.remove('hidden');
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => el.classList.add('hidden'), 2600);
}

function setupNav() {
  const titles = {
    overview: ['半年 CI 回放总览', '20,400 个测试、模块依赖图、历史覆盖、共同失败概率与夜间全量兜底'],
    commits: ['按改动挑选测试', '每次提交选中的测试、排序分数和可解释原因'],
    flaky: ['不稳定测试识别', '同一代码既过又不过、不稳定分数、原因分类和历史通过率曲线'],
    quarantine: ['隔离与自动恢复', '已知 flaky 不阻塞合并，但继续运行；持续确定性失败会升级为真实破坏'],
    report: ['开发者运行报告', '跳过数量、耗时、真实失败、已知不稳定失败和漏检校准'],
  };
  $$('.nav-btn').forEach(btn => btn.addEventListener('click', () => {
    $$('.nav-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const view = btn.dataset.view;
    $$('.view').forEach(v => v.classList.remove('active'));
    $(`#view-${view}`).classList.add('active');
    $('#page-title').textContent = titles[view][0];
    $('#page-sub').textContent = titles[view][1];
    if (view === 'report') loadReport();
  }));
}

function renderOverview(m) {
  overviewMetrics = m;
  const state = $('#data-state');
  if (!m || !m.scale || !m.safe) {
    state.textContent = '未生成数据';
    state.className = 'pill bad';
    $('#kpi-grid').innerHTML = `<div class="empty">请先运行 <span class="mono">python -m ci_optimizer demo</span> 生成半年模拟历史。</div>`;
    return;
  }
  state.textContent = `已连接 · ${m.scale.commits} 次提交`;
  state.className = 'pill ok';

  const s = m.scale, safe = m.safe, acc = m.accuracy, amort = m.nightly_amortized;
  const cards = [
    { label: '测试总量', value: num(s.tests), sub: `基线 ${minutes(s.baseline_wall_sec)}/次`, cls: '' },
    { label: '安全档平均选词', value: num(safe.avg_selected), sub: `跳过 ${pct(safe.skip_ratio)} · ${num(safe.avg_skipped)} 个`, cls: 'brand' },
    { label: 'PR 反馈耗时', value: minutes(safe.avg_duration_sec), sub: `下降 ${pct(safe.duration_reduction)}`, cls: 'good' },
    { label: '新破坏漏检率', value: pct(safe.new_failure_miss_rate, 2), sub: `${safe.missed_new_true_failures}/${safe.new_true_failures} 个新失败`, cls: safe.new_failure_miss_rate > .04 ? 'warn' : 'good' },
    { label: 'Flaky F1', value: acc.f1.toFixed(3), sub: `P ${pct(acc.precision)} / R ${pct(acc.recall)}`, cls: 'brand' },
    { label: '破坏被错隔离', value: acc.wrong_masked_bad_pairs, sub: `普通破坏测试对 / ${acc.bad_episode_test_pairs}`, cls: acc.wrong_masked_bad_pairs ? 'bad' : 'good' },
  ];
  $('#kpi-grid').innerHTML = cards.map(c => `
    <div class="kpi ${c.cls}">
      <div class="kpi-label">${c.label}</div>
      <div class="kpi-value">${c.value}</div>
      <div class="kpi-sub" title="${escapeHtml(c.sub)}">${escapeHtml(c.sub)}</div>
    </div>`).join('');

  $('#tier-table').innerHTML = `
    <table class="kv-table">
      <tr><td>快速档平均测试</td><td>${num(m.fast.avg_selected)}（跳过 ${pct(m.fast.skip_ratio)}）</td></tr>
      <tr><td>快速档耗时</td><td>${minutes(m.fast.avg_duration_sec)} · ↓${pct(m.fast.duration_reduction)}</td></tr>
      <tr><td>快速档新破坏漏检</td><td>${pct(m.fast.new_failure_miss_rate, 2)}</td></tr>
      <tr><td><b>安全档平均测试</b></td><td><b>${num(safe.avg_selected)}（跳过 ${pct(safe.skip_ratio)}）</b></td></tr>
      <tr><td><b>安全档耗时</b></td><td><b>${minutes(safe.avg_duration_sec)} · ↓${pct(safe.duration_reduction)}</b></td></tr>
      <tr><td>安全档新破坏漏检</td><td>${pct(safe.new_failure_miss_rate, 2)}</td></tr>
      <tr><td>夜间全量</td><td>${s.nightly_full_runs} 次</td></tr>
      <tr><td>摊夜间成本后</td><td>${num(amort.avg_tests_per_commit)} 测试/提交 · ↓${pct(amort.duration_reduction)}</td></tr>
    </table>
    <div class="callout good">开发者等待时间按安全档显著下降；夜间全量兜底保证任何漏检最迟 24 小时内被发现。</div>
  `;

  $('#miss-calibration').innerHTML = `
    <table class="kv-table">
      <tr><td>全部真实失败漏检率</td><td>${pct(safe.true_failure_miss_rate, 3)}</td></tr>
      <tr><td>新引入破坏漏检率</td><td>${pct(safe.new_failure_miss_rate, 2)}</td></tr>
      <tr><td>每次提交模型预测漏检数</td><td>${safe.avg_predicted_missed_per_commit.toFixed(3)}</td></tr>
      <tr><td>快速档新破坏漏检率</td><td>${pct(m.fast.new_failure_miss_rate, 2)}</td></tr>
    </table>
    <div class="callout warn">预测值来自覆盖缺口、图距离、历史共同失败和在线校准；页面提交详情还展示基于夜间反馈的 Wilson 95% 上界。</div>
  `;

  $('#quality-panel').innerHTML = `
    ${progressBar('精确率', acc.precision)}
    ${progressBar('召回率', acc.recall)}
    ${progressBar('原因 Top-1', acc.cause_accuracy)}
    <table class="kv-table" style="margin-top:10px">
      <tr><td>TP / FP / FN / TN</td><td>${acc.tp}/${acc.fp}/${acc.fn}/${acc.tn}</td></tr>
      <tr><td>转变测试正确改判</td><td>${acc.transition_correct}/${acc.transition_total}</td></tr>
      <tr><td>转变后 12 次内未升级</td><td>${acc.transition_not_escalated_12_runs}</td></tr>
    </table>
  `;
}

function progressBar(label, value) {
  return `<div style="margin:10px 0">
    <div class="cause-title"><span>${label}</span><span>${pct(value)}</span></div>
    <div class="progress"><i style="width:${Math.max(2, value * 100)}%"></i></div>
  </div>`;
}

function renderSelectionTrend(commits) {
  const el = $('#selection-trend');
  if (!commits || !commits.length) { el.innerHTML = '<div class="empty">暂无回放结果</div>'; return; }
  const data = commits.slice().reverse();
  const W = 920, H = 270, pad = { l: 48, r: 16, t: 18, b: 34 };
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  const maxY = Math.max(...data.map(d => d.selected_count)) * 1.12;
  const x = i => pad.l + (data.length === 1 ? iw / 2 : i * iw / (data.length - 1));
  const y = v => pad.t + ih - v / maxY * ih;
  const ticks = [0, .25, .5, .75, 1].map(t => {
    const val = maxY * t;
    return `<line class="grid-line" x1="${pad.l}" x2="${W-pad.r}" y1="${y(val)}" y2="${y(val)}"/>
      <text class="axis-text" x="${pad.l-8}" y="${y(val)+4}" text-anchor="end">${Math.round(val/100)/10}k</text>`;
  }).join('');
  const barW = Math.max(1.5, iw / data.length * .62);
  const bars = data.map((d, i) => `
    <rect class="bar-selected" x="${x(i)-barW/2}" y="${y(d.selected_count)}" width="${barW}"
      height="${pad.t+ih-y(d.selected_count)}" rx="1.5">
      <title>#${d.seq} ${escapeHtml(d.message)}：${num(d.selected_count)} selected</title>
    </rect>
    ${d.missed_new_true_failures ? `<circle class="bar-miss" cx="${x(i)}" cy="${pad.t+8}" r="3.2"><title>新破坏漏检 ${d.missed_new_true_failures}</title></circle>` : ''}
  `).join('');
  const labelIdx = [0, Math.floor(data.length/2), data.length-1];
  const labels = labelIdx.map(i => `<text class="axis-text" x="${x(i)}" y="${H-10}" text-anchor="middle">${escapeHtml((data[i].created_at||'').slice(0,10))}</text>`).join('');
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="selection trend">
    <defs><linearGradient id="areaGradient" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stop-color="#4f46e5"/><stop offset="100%" stop-color="#4f46e5" stop-opacity="0"/></linearGradient></defs>
    ${ticks}${bars}${labels}
  </svg>`;
}

function renderCommitTable(commits) {
  commitCache = commits;
  const tbody = $('#commit-table tbody');
  tbody.innerHTML = commits.map(c => `
    <tr data-sha="${c.sha}" class="commit-row ${c.sha === activeSha ? 'active' : ''}">
      <td>
        <div class="commit-sha">${shortSha(c.sha)}</div>
        <div class="commit-message muted" title="${escapeHtml(c.message)}">${escapeHtml(c.message)}</div>
      </td>
      <td><b>${num(c.selected_count)}</b><div class="muted">${pct(c.selected_count / 20400, 0)}</div></td>
      <td>${c.true_failures ? `<span class="badge ${c.missed_true_failures ? 'status-broken_suspect' : 'truth-no'}">${c.true_failures}</span>` : '<span class="muted">0</span>'}</td>
      <td>${c.missed_new_true_failures ? `<span class="badge status-broken_suspect">新 ${c.missed_new_true_failures}</span>` : (c.missed_true_failures ? `<span class="badge status-quarantined">旧 ${c.missed_true_failures}</span>` : '<span class="badge truth-no">0</span>')}</td>
    </tr>`).join('');
  $$('.commit-row').forEach(row => row.addEventListener('click', () => selectCommit(row.dataset.sha)));
}

async function loadCommits() {
  const data = await api('/api/commits?limit=600');
  renderCommitTable(data.commits);
  renderSelectionTrend(data.commits);
  if (!activeSha && data.commits.length) selectCommit(data.commits[0].sha);
}

function filterCommitTable() {
  const q = $('#commit-search').value.trim().toLowerCase();
  const rows = commitCache.filter(c => !q || `${c.message} ${c.sha} ${(c.domains||[]).join(' ')} ${c.author}`.toLowerCase().includes(q));
  renderCommitTable(rows);
}

async function selectCommit(sha) {
  activeSha = sha;
  itemOffset = 0;
  itemQuery = '';
  $('#test-search').value = '';
  $$('.commit-row').forEach(r => r.classList.toggle('active', r.dataset.sha === sha));
  await loadSelection();
}

async function loadSelection() {
  if (!activeSha) return;
  const params = new URLSearchParams({ sha: activeSha, q: itemQuery, limit: itemLimit, offset: itemOffset });
  const data = await api(`/api/selection?${params}`);
  if (data.error) { showToast(data.error); return; }
  const c = data.commit, s = data.snapshot;
  $('#commit-summary-card').classList.remove('empty');
  $('#commit-summary-card').innerHTML = `
    <div class="card-head">
      <div>
        <h2>${escapeHtml(c.message)}</h2>
        <p><span class="commit-sha">${shortSha(c.sha)}</span> · ${escapeHtml(c.author)} · ${escapeHtml(c.created_at)} · ${escapeHtml(c.change_kind)} / ${escapeHtml(c.size_bucket)}</p>
      </div>
      <span class="pill ok">安全档</span>
    </div>
    ${s ? `
    <div class="metric-row">
      ${metricBox(num(s.selected_count), `选中 / ${num(s.total_count)}`)}
      ${metricBox(pct(s.skipped_count / s.total_count), `跳过 ${num(s.skipped_count)}`)}
      ${metricBox(minutes(s.selected_duration_sec), `基线 ${minutes(s.baseline_duration_sec)}`)}
      ${metricBox(pct(s.estimated_miss_rate, 2), `Wilson95 上界 ${pct(s.miss_rate_upper95, 1)}`)}
    </div>
    <div class="mini-stats">
      <span class="badge truth-no">选中真实失败 ${s.selected_true_failures}</span>
      <span class="badge ${s.missed_true_failures ? 'status-broken_suspect' : 'truth-no'}">漏检真实失败 ${s.missed_true_failures}</span>
      <span class="badge ${s.missed_new_true_failures ? 'status-broken_suspect' : 'truth-no'}">漏检新破坏 ${s.missed_new_true_failures}</span>
      <span class="badge status-quarantined">flaky 失败 ${s.flaky_failures}</span>
      <span class="badge quarantined_monitor">隔离观察 ${s.quarantined_count}</span>
      <span class="badge smoke">模型预测漏检数 ${Number(s.estimated_missed_failures).toFixed(3)}</span>
    </div>
    ${s.missed_true_failures ? `<div class="callout bad">有 ${s.missed_true_failures} 个真实失败未被 PR 选词命中；已由当天夜间全量兜底发现，并进入下一次选词/校准。${(data.missed_tests||[]).map(t => `<div class="mono" style="margin-top:4px">${escapeHtml(t.nodeid)}</div>`).join('')}</div>` : '<div class="callout good">本次没有真实失败被漏掉。</div>'}
    ` : '<div class="empty">该提交没有选词快照</div>'}
  `;

  $('#commit-files-card').classList.remove('hidden');
  $('#commit-files').innerHTML = data.files.map(f => `<span class="chip" title="${escapeHtml(f.domain || '')}/${escapeHtml(f.layer || '')}">${escapeHtml(f.path)}</span>`).join('');
  $('#selected-table-card').classList.remove('hidden');
  $('#selected-count-label').textContent = `共 ${num(data.total)} 个匹配；按失败风险从高到低排序`;
  $('#selected-tbody').innerHTML = data.items.map(item => {
    const riskClass = item.score >= 25 ? 'risk-high' : item.score >= 8 ? 'risk-mid' : 'risk-low';
    const details = (item.reason_codes || []).map(code => {
      const specific = item.reason_detail && item.reason_detail[code] ? `：${item.reason_detail[code]}` : '';
      return `${REASON_LABELS[code] || code}${specific}\n${REASON_DESCRIPTIONS[code] || ''}`;
    }).join('\n\n');
    return `<tr>
      <td class="muted">${item.rank}</td>
      <td class="risk-pill ${riskClass}">${item.score.toFixed(1)}</td>
      <td class="nodeid mono">${escapeHtml(item.nodeid)}<div class="muted" style="font-family:inherit">${escapeHtml(item.domain)} · ${escapeHtml(item.owner)}</div></td>
      <td><span class="badge">${escapeHtml(item.suite)}</span></td>
      <td class="mono">${(item.duration_ms/1000).toFixed(2)}s</td>
      <td title="${escapeHtml(details)}">${(item.reason_codes||[]).map(code => `<span class="badge ${escapeHtml(code)}">${REASON_LABELS[code] || code}</span>`).join('')}</td>
    </tr>`;
  }).join('');
  const start = data.total === 0 ? 0 : itemOffset + 1;
  const end = Math.min(data.total, itemOffset + itemLimit);
  $('#page-info').textContent = `${start}-${end} / ${num(data.total)}`;
  $('#prev-page').disabled = itemOffset === 0;
  $('#next-page').disabled = end >= data.total;
  loadRunReport(data.commit.sha);
}

async function loadRunReport(sha) {
  const card = $('#run-report-card');
  const content = $('#run-report-content');
  card.classList.remove('hidden');
  content.textContent = '加载运行报告…';
  try {
    const report = await api(`/api/run_report?sha=${encodeURIComponent(sha)}`);
    content.textContent = report.markdown || '暂无报告';
  } catch (err) {
    content.textContent = `运行报告加载失败：${err.message}`;
  }
}

function metricBox(value, label) {
  return `<div class="metric-box"><div class="v">${value}</div><div class="l">${escapeHtml(label)}</div></div>`;
}

async function loadFlaky() {
  const filter = $('#flaky-filter').value;
  const q = $('#flaky-search').value.trim();
  const data = await api(`/api/flaky?filter=${encodeURIComponent(filter)}&q=${encodeURIComponent(q)}&limit=400`);
  const tbody = $('#flaky-table tbody');
  if (!data.tests.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty">没有匹配的测试</td></tr>`;
    return;
  }
  tbody.innerHTML = data.tests.map(t => {
    const cause = (t.causes || [])[0];
    const pass = t.pass_rate == null ? 1 : t.pass_rate;
    return `<tr data-id="${t.id}">
      <td class="nodeid mono">${escapeHtml(t.nodeid)}<div class="muted" style="font-family:inherit">${escapeHtml(t.domain)} · ${escapeHtml(t.owner)}</div></td>
      <td><span class="badge class-${t.classification || 'stable'}">${CLASS_LABELS[t.classification] || t.classification || '未判定'}</span><br><span class="badge status-${t.status || 'healthy'}" style="margin-top:4px">${STATUS_LABELS[t.status] || t.status || '-'}</span></td>
      <td class="${pass < .9 ? 'risk-high' : ''}"><b>${pct(pass, 1)}</b><div class="muted">${t.fail_count || 0} 次首跑失败</div></td>
      <td><b>${(t.flake_score || 0).toFixed(2)}</b></td>
      <td>${t.mixed_sha_count || 0}</td>
      <td>${t.flip_count || 0}</td>
      <td>${cause ? `<span class="badge">${escapeHtml(cause.label)}</span>` : '<span class="muted">—</span>'}</td>
      <td>${t.truth_flaky ? '<span class="badge truth-yes">真值 flaky</span>' : '<span class="badge truth-no">真值稳定/破坏</span>'}${t.broken_seq ? '<br><span class="badge status-broken_suspect" style="margin-top:4px">埋点：后弄坏</span>' : ''}</td>
    </tr>`;
  }).join('');
  $$('#flaky-table tbody tr[data-id]').forEach(tr => tr.addEventListener('click', () => loadFlakyDetail(Number(tr.dataset.id))));
}

function passRateChart(daily) {
  const W = 900, H = 260, pad = { l: 46, r: 16, t: 18, b: 34 };
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  const rates = daily.map(d => d.pass_rate);
  let min = Math.min(...rates, 0.82);
  min = Math.max(0, Math.floor((min - .04) * 20) / 20);
  const max = 1;
  const x = i => pad.l + (daily.length <= 1 ? iw/2 : i * iw / (daily.length - 1));
  const y = v => pad.t + ih - (v - min) / (max - min || 1) * ih;
  const line = daily.map((d,i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(d.pass_rate).toFixed(1)}`).join(' ');
  const area = `${line} L${x(daily.length-1)},${pad.t+ih} L${x(0)},${pad.t+ih} Z`;
  const grid = [min, (min+1)/2, 1].map(v => `
    <line class="grid-line" x1="${pad.l}" x2="${W-pad.r}" y1="${y(v)}" y2="${y(v)}"/>
    <text class="axis-text" x="${pad.l-8}" y="${y(v)+4}" text-anchor="end">${pct(v,0)}</text>`).join('');
  const failDots = daily.map((d,i) => d.fails ? `<circle class="fail-dot" cx="${x(i)}" cy="${y(d.pass_rate)}" r="${Math.min(6, 2 + d.fails)}"><title>${d.day}: ${d.fails} failures</title></circle>` : '').join('');
  const labels = [0, Math.floor(daily.length/2), daily.length-1].map(i => `<text class="axis-text" x="${x(i)}" y="${H-10}" text-anchor="middle">${daily[i].day}</text>`).join('');
  return `<svg viewBox="0 0 ${W} ${H}">
    <defs><linearGradient id="passArea" x1="0" x2="0" y1="0" y2="1"><stop offset="0%" stop-color="#12b76a"/><stop offset="100%" stop-color="#12b76a" stop-opacity="0"/></linearGradient></defs>
    ${grid}<path d="${area}" fill="url(#passArea)" opacity=".16"/><path class="chart-line" d="${line}" stroke="#12b76a"/>${failDots}${labels}
  </svg>`;
}

async function loadFlakyDetail(id) {
  const d = await api(`/api/flaky_detail?id=${id}`);
  if (d.error) { showToast(d.error); return; }
  const t = d.test, f = d.finding;
  const el = $('#flaky-detail');
  el.classList.remove('hidden');
  el.innerHTML = `
    <div class="card">
      <div class="card-head">
        <div><h2 class="mono" style="font-size:14px">${escapeHtml(t.nodeid)}</h2>
        <p>${escapeHtml(t.domain)} · ${escapeHtml(t.suite)} · ${escapeHtml(t.owner)} · 真值 archetype=${escapeHtml(t.archetype_gt || '无')}</p></div>
        <button class="secondary" id="close-detail">关闭详情</button>
      </div>
      <div class="metric-row">
        ${metricBox(f ? pct(f.pass_rate,1) : '—', '历史首跑通过率')}
        ${metricBox(f ? f.mixed_sha_count : 0, '同 SHA 混合次数')}
        ${metricBox(f ? f.flip_count : 0, '相邻运行翻转')}
        ${metricBox(f ? f.flake_score.toFixed(2) : '—', 'Flake 分数')}
      </div>
      <div class="detail-grid">
        <div>
          <h2>历史每日通过率曲线</h2>
          <p>红点表示当天首跑失败，点大小随失败次数变化。</p>
          ${passRateChart(d.daily)}
        </div>
        <div>
          <h2>可能原因分类</h2>
          ${(f?.causes || []).length ? f.causes.map(c => `
            <div class="cause-card">
              <div class="cause-title"><span>${escapeHtml(c.label)}</span><span>${pct(c.confidence,0)}</span></div>
              <div class="progress"><i style="width:${c.confidence*100}%"></i></div>
              <p>${escapeHtml(c.evidence)}</p>
            </div>`).join('') : '<div class="empty">没有足够上下文证据</div>'}
        </div>
      </div>
    </div>
    <div class="detail-grid">
      <div class="card">
        <div class="card-head"><h2>最近失败/重跑上下文</h2></div>
        <div class="table-scroll" style="max-height:360px">
          <table class="table compact">
            <thead><tr><th>Seq</th><th>尝试</th><th>状态</th><th>错误/上下文</th></tr></thead>
            <tbody>${d.observations.map(o => `<tr>
              <td>${o.seq}</td><td>${o.attempt}</td>
              <td><span class="badge ${o.status === 'fail' ? 'status-broken_suspect' : 'truth-no'}">${o.status}</span></td>
              <td>${escapeHtml(o.error_type || '')}<div class="muted">${escapeHtml(o.message || '')}</div>${o.preceding_nodeid ? `<div class="muted">prev: ${escapeHtml(o.preceding_nodeid)}</div>` : ''}</td>
            </tr>`).join('')}</tbody>
          </table>
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h2>隔离时间线</h2></div>
        <div class="timeline">
          ${d.events.length ? d.events.map(e => `<div class="event-line">
            <div class="event-type ${escapeHtml(e.event_type)}">${escapeHtml(e.event_type)}</div>
            <div><b>#${e.seq}</b> · ${escapeHtml(e.reason)}<div class="muted">${escapeHtml(e.created_at)} · score ${e.flake_score}</div></div>
          </div>`).join('') : '<div class="empty">无隔离事件</div>'}
        </div>
      </div>
    </div>`;
  $('#close-detail').addEventListener('click', () => el.classList.add('hidden'));
  el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

async function loadEvents() {
  const d = await api('/api/quarantine_events?limit=300');
  $('#events-tbody').innerHTML = d.events.map(e => `<tr>
    <td>${e.seq}</td><td class="muted">${escapeHtml(e.created_at)}</td>
    <td class="nodeid mono">${escapeHtml(e.nodeid)}<div class="muted" style="font-family:inherit">${escapeHtml(e.domain)}</div></td>
    <td><span class="event-type ${escapeHtml(e.event_type)}">${escapeHtml(e.event_type)}</span></td>
    <td>${escapeHtml(e.reason)}</td><td>${e.flake_score}</td>
  </tr>`).join('');
}

async function loadReport() {
  const d = await api('/api/report');
  $('#report-content').textContent = d.markdown;
}

function setupInteractions() {
  $('#refresh-btn').addEventListener('click', () => location.reload());
  $('#commit-search').addEventListener('input', debounce(filterCommitTable, 120));
  $('#flaky-filter').addEventListener('change', loadFlaky);
  $('#flaky-search').addEventListener('input', () => {
    clearTimeout(flakyTimer);
    flakyTimer = setTimeout(loadFlaky, 220);
  });
  $('#test-search').addEventListener('input', e => {
    clearTimeout(testSearchTimer);
    const value = e.target.value.trim();
    testSearchTimer = setTimeout(() => { itemQuery = value; itemOffset = 0; loadSelection(); }, 220);
  });
  $('#prev-page').addEventListener('click', () => { itemOffset = Math.max(0, itemOffset - itemLimit); loadSelection(); });
  $('#next-page').addEventListener('click', () => { itemOffset += itemLimit; loadSelection(); });
  $('#reload-report').addEventListener('click', loadReport);
}

async function init() {
  setupNav();
  setupInteractions();
  try {
    const overview = await api('/api/overview');
    renderOverview(overview);
    await loadCommits();
    await loadFlaky();
    await loadEvents();
  } catch (err) {
    $('#data-state').textContent = 'API 未就绪';
    $('#data-state').className = 'pill bad';
    $('#kpi-grid').innerHTML = `<div class="empty">无法加载数据：${escapeHtml(err.message)}<br><br>请先运行 <span class="mono">python -m ci_optimizer demo --serve</span></div>`;
    console.error(err);
  }
}

init();
