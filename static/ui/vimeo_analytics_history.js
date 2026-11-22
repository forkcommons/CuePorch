const BASE = '/api/analytics/vimeo-history';
const $ = (id) => document.getElementById(id);
const details = $('vimeoHistoryDetails');
const form = $('vimeoHistoryForm');
const statusEl = $('vimeoHistoryStatus');
const selectEl = $('vimeoHistorySelect');
const summaryEl = $('vimeoHistorySummary');
const tableEl = $('vimeoHistoryTable');
const pageLabel = $('vimeoHistoryPage');
const prevBtn = $('vimeoHistoryPrev');
const nextBtn = $('vimeoHistoryNext');
const pageSize = 100;
const state = { reportId: null, page: 1, pageCount: 1, summary: null };
let selection = 0;
const labels = {
    video_title: "Vimeo title", average_percent_watched: "Average watched (%)",
    average_seconds_watched: "Average watched (seconds)", total_seconds_watched: "Total watched (seconds)",
};
const label = (key) => labels[key] ?? key[0].toUpperCase() + key.slice(1).replaceAll("_", " ");
const setStatus = (m) => { statusEl.textContent = m; };
function headers() {
    const k = $('reportKey')?.value.trim();
    return k ? { Authorization: `Bearer ${k}` } : {};
}
async function refreshReports() {
    const res = await fetch(BASE, { headers: headers() });
    if (!res.ok)
        throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    selectEl.replaceChildren();
    const opt = new Option('— select —', '');
    selectEl.appendChild(opt);
    for (const r of data.reports)
        selectEl.appendChild(new Option(`${r.first} to ${r.last} · ${r.count} rows · ${r.id.slice(0, 8)}`, r.id));
}
async function loadReport(id, page) {
    const selected = ++selection;
    setStatus('Loading…');
    try {
        const res = await fetch(`${BASE}/${encodeURIComponent(id)}?page=${page}`, { headers: headers() });
        if (!res.ok)
            throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (selected !== selection)
            return;
        state.summary = data;
        state.reportId = data.id;
        state.page = data.page;
        state.pageCount = Math.max(1, Math.ceil(data.count / pageSize));
        setStatus(`${data.count} rows · ${data.matched} matched · ${data.unmatched} unmatched`);
        renderSummary();
        renderTable(data.rows);
        renderPager();
    }
    catch (err) {
        setStatus(`Error: ${err.message}`);
    }
}
function renderSummary() {
    summaryEl.replaceChildren();
    const s = state.summary;
    if (!s)
        return;
    const info = document.createElement('p');
    info.textContent = `First ${s.first} · Last ${s.last} · Rows ${s.count} · Matched ${s.matched} · Unmatched ${s.unmatched} · Columns ${s.columns.map(label).join(', ') || '—'}`;
    summaryEl.appendChild(info);
    const tot = document.createElement('p');
    if (s.total) {
        const parts = Object.entries(s.total).map(([k, v]) => `${label(k)} ${v}`);
        tot.textContent = `Provider total: ${parts.length ? parts.join(', ') : '—'}`;
    }
    else {
        tot.textContent = 'Provider total: (not reported)';
    }
    summaryEl.appendChild(tot);
}
function renderTable(rows) {
    tableEl.replaceChildren();
    const s = state.summary;
    if (!s)
        return;
    const thead = document.createElement('thead');
    const hr = document.createElement('tr');
    for (const h of ['Vimeo video ID', 'CuePorch video', ...s.columns.map(label)]) {
        const th = document.createElement('th');
        th.scope = 'col';
        th.textContent = h;
        hr.appendChild(th);
    }
    thead.appendChild(hr);
    const tbody = document.createElement('tbody');
    for (const row of rows) {
        const tr = document.createElement('tr');
        const add = (v) => { const td = document.createElement('td'); td.textContent = v; tr.appendChild(td); };
        add(row.source_id ?? '');
        add(row.title ?? 'Unmatched');
        for (const c of s.columns) {
            if (Object.prototype.hasOwnProperty.call(row.metrics, c))
                add(String(row.metrics[c]));
            else if (Object.prototype.hasOwnProperty.call(row.dimensions, c))
                add(row.dimensions[c] ?? '');
            else
                add('');
        }
        tbody.appendChild(tr);
    }
    tableEl.appendChild(thead);
    tableEl.appendChild(tbody);
}
function renderPager() {
    pageLabel.textContent = `Page ${state.page}`;
    prevBtn.disabled = state.page <= 1;
    nextBtn.disabled = state.page >= state.pageCount;
}
form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const submitter = e.submitter;
    const action = submitter?.value === 'preview' ? 'preview' : 'import';
    const fd = new FormData(form);
    const url = action === 'import' ? BASE : `${BASE}?preview=1`;
    ++selection;
    const buttons = Array.from(form.querySelectorAll('button'));
    for (const button of buttons)
        button.disabled = true;
    setStatus(action === 'import' ? 'Importing…' : 'Previewing…');
    try {
        const res = await fetch(url, { method: 'POST', headers: headers(), body: fd });
        if (!res.ok) {
            const detail = await res.json().catch(() => ({}));
            throw new Error(detail.error ?? `HTTP ${res.status}`);
        }
        const summary = await res.json();
        if (action === 'import') {
            setStatus(`Imported ${summary.count} rows (id ${summary.id})`);
            await refreshReports();
            selectEl.value = summary.id;
            await loadReport(summary.id, 1);
        }
        else {
            state.summary = summary;
            state.reportId = null;
            state.page = 1;
            state.pageCount = 1;
            setStatus(`Preview: ${summary.count} rows · ${summary.matched} matched · ${summary.unmatched} unmatched`);
            renderSummary();
            renderTable([]);
            renderPager();
        }
    }
    catch (err) {
        setStatus(`Error: ${err.message}`);
    }
    finally {
        for (const button of buttons)
            button.disabled = false;
    }
});
details.addEventListener('toggle', () => {
    if (details.open)
        refreshReports().catch((e) => setStatus(`Error: ${e.message}`));
});
selectEl.addEventListener('change', () => { const v = selectEl.value; if (v)
    loadReport(v, 1); });
prevBtn.addEventListener('click', () => { if (state.reportId && state.page > 1)
    loadReport(state.reportId, state.page - 1); });
nextBtn.addEventListener('click', () => { if (state.reportId && state.page < state.pageCount)
    loadReport(state.reportId, state.page + 1); });
export {};
