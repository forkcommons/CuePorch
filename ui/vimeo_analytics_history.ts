type Summary = { id: string; source: "csv" | "api"; first: string; last: string; columns: string[]; count: number; matched: number; unmatched: number; total: Record<string, number> | null };
type Row = { source_id: string; work: string | null; title: string | null; dimensions: Record<string, string>; metrics: Record<string, number> };

const BASE = '/api/analytics/vimeo-history';
const $ = <T extends HTMLElement>(id: string) => document.getElementById(id) as T;
const details = $<HTMLDetailsElement>('vimeoHistoryDetails');
const form = $<HTMLFormElement>('vimeoHistoryForm');
const statusEl = $('vimeoHistoryStatus');
const selectEl = $<HTMLSelectElement>('vimeoHistorySelect');
const summaryEl = $('vimeoHistorySummary');
const tableEl = $<HTMLTableElement>('vimeoHistoryTable');
const pageLabel = $('vimeoHistoryPage');
const prevBtn = $<HTMLButtonElement>('vimeoHistoryPrev');
const nextBtn = $<HTMLButtonElement>('vimeoHistoryNext');

const pageSize = 100;
const state = { reportId: null as string | null, page: 1, pageCount: 1, summary: null as Summary | null };
let selection = 0;
const labels: Record<string, string> = {
  video_title: "Vimeo title", average_percent_watched: "Average watched (%)",
  average_seconds_watched: "Average watched (seconds)", total_seconds_watched: "Total watched (seconds)",
};
const label = (key: string) => labels[key] ?? key[0].toUpperCase() + key.slice(1).replaceAll("_", " ");
const setStatus = (m: string) => { statusEl.textContent = m; };

function headers(): HeadersInit {
  const k = $<HTMLInputElement>('reportKey')?.value.trim();
  return k ? { Authorization: `Bearer ${k}` } : {};
}

async function refreshReports(): Promise<void> {
  const res = await fetch(BASE, { headers: headers() });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data: { reports: Summary[] } = await res.json();
  selectEl.replaceChildren();
  const opt = new Option('— select —', '');
  selectEl.appendChild(opt);
  for (const r of data.reports) selectEl.appendChild(new Option(`${r.first} to ${r.last} · ${r.source === "api" ? "Vimeo API" : "CSV"} · ${r.count} rows · ${r.id.slice(0, 8)}`, r.id));
}

async function loadReport(id: string, page: number): Promise<void> {
  const selected = ++selection;
  setStatus('Loading…');
  try {
    const res = await fetch(`${BASE}/${encodeURIComponent(id)}?page=${page}`, { headers: headers() });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data: Summary & { page: number; rows: Row[] } = await res.json();
    if (selected !== selection) return;
    state.summary = data;
    state.reportId = data.id;
    state.page = data.page;
    state.pageCount = Math.max(1, Math.ceil(data.count / pageSize));
    setStatus(`${data.count} rows · ${data.matched} matched · ${data.unmatched} unmatched`);
    renderSummary(); renderTable(data.rows); renderPager();
  } catch (err) { setStatus(`Error: ${(err as Error).message}`); }
}

function renderSummary(): void {
  summaryEl.replaceChildren();
  const s = state.summary;
  if (!s) return;
  const info = document.createElement('p');
  info.textContent = `First ${s.first} · Last ${s.last} · Rows ${s.count} · Matched ${s.matched} · Unmatched ${s.unmatched} · Columns ${s.columns.map(label).join(', ') || '—'}`;
  summaryEl.appendChild(info);
  const tot = document.createElement('p');
  if (s.total) {
    const parts = Object.entries(s.total).map(([k, v]) => `${label(k)} ${v}`);
    tot.textContent = `Provider total: ${parts.length ? parts.join(', ') : '—'}`;
  } else {
    tot.textContent = 'Provider total: (not reported)';
  }
  summaryEl.appendChild(tot);
}

function renderTable(rows: Row[]): void {
  tableEl.replaceChildren();
  const s = state.summary;
  if (!s) return;
  const thead = document.createElement('thead');
  const hr = document.createElement('tr');
  for (const h of ['Vimeo video ID', 'CuePorch video', ...s.columns.map(label)]) {
    const th = document.createElement('th'); th.scope = 'col'; th.textContent = h; hr.appendChild(th);
  }
  thead.appendChild(hr);
  const tbody = document.createElement('tbody');
  for (const row of rows) {
    const tr = document.createElement('tr');
    const add = (v: string) => { const td = document.createElement('td'); td.textContent = v; tr.appendChild(td); };
    add(row.source_id ?? '');
    add(row.title ?? 'Unmatched');
    for (const c of s.columns) {
      if (Object.prototype.hasOwnProperty.call(row.metrics, c)) add(String(row.metrics[c]));
      else if (Object.prototype.hasOwnProperty.call(row.dimensions, c)) add(row.dimensions[c] ?? '');
      else add('');
    }
    tbody.appendChild(tr);
  }
  tableEl.appendChild(thead);
  tableEl.appendChild(tbody);
}

function renderPager(): void {
  pageLabel.textContent = `Page ${state.page}`;
  prevBtn.disabled = state.page <= 1;
  nextBtn.disabled = state.page >= state.pageCount;
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const submitter = (e as SubmitEvent).submitter as HTMLButtonElement | null;
  const action = submitter?.value === 'preview' ? 'preview' : 'import';
  const fd = new FormData(form);
  const url = action === 'import' ? BASE : `${BASE}?preview=1`;
  ++selection;
  const buttons = Array.from(form.querySelectorAll('button'));
  for (const button of buttons) button.disabled = true;
  setStatus(action === 'import' ? 'Importing…' : 'Previewing…');
  try {
    const res = await fetch(url, { method: 'POST', headers: headers(), body: fd });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.error ?? `HTTP ${res.status}`);
    }
    const summary: Summary = await res.json();
    if (action === 'import') {
      setStatus(`Imported ${summary.count} rows (id ${summary.id})`);
      await refreshReports();
      selectEl.value = summary.id;
      await loadReport(summary.id, 1);
    } else {
      state.summary = summary;
      state.reportId = null;
      state.page = 1; state.pageCount = 1;
      setStatus(`Preview: ${summary.count} rows · ${summary.matched} matched · ${summary.unmatched} unmatched`);
      renderSummary(); renderTable([]); renderPager();
    }
  } catch (err) { setStatus(`Error: ${(err as Error).message}`); }
  finally { for (const button of buttons) button.disabled = false; }
});

const apiForm = document.querySelector<HTMLFormElement>("#vimeoApiImportForm");
const apiBtn = document.querySelector<HTMLButtonElement>("#vimeoApiImportBtn");

apiForm?.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!apiBtn) return;

  // Snapshot the form, then clear the token field immediately.
  const data = new FormData(apiForm);
  const payload = {
    first: String(data.get("first") ?? ""),
    last: String(data.get("last") ?? ""),
    token: String(data.get("token") ?? "").trim(),
  };
  const tokenInput = apiForm.querySelector<HTMLInputElement>('input[name="token"]');
  if (tokenInput) tokenInput.value = "";

  ++selection;
  apiBtn.disabled = true;
  setStatus("Importing from Vimeo API…");

  try {
    const requestHeaders = new Headers(headers());
    requestHeaders.set("Content-Type", "application/json");

    const res = await fetch(BASE + "/import-source", {
      method: "POST",
      headers: requestHeaders,
      body: JSON.stringify(payload),
    });

    const body = await res.json().catch(() => null);
    if (!res.ok || typeof body?.id !== "string") {
      setStatus(body?.error ?? "The Vimeo API report could not be imported.");
      return;
    }
    await refreshReports();
    selectEl.value = body.id;
    await loadReport(body.id, 1);
  } catch {
    setStatus("Import failed.");
  } finally {
    apiBtn.disabled = false;
  }
});

details.addEventListener('toggle', () => {
  if (details.open) refreshReports().catch((e) => setStatus(`Error: ${(e as Error).message}`));
});
selectEl.addEventListener('change', () => { const v = selectEl.value; if (v) loadReport(v, 1); });
prevBtn.addEventListener('click', () => { if (state.reportId && state.page > 1) loadReport(state.reportId, state.page - 1); });
nextBtn.addEventListener('click', () => { if (state.reportId && state.page < state.pageCount) loadReport(state.reportId, state.page + 1); });
