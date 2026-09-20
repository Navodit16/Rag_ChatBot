/**
 * RAG Output Pipeline — Frontend Logic
 * Handles: query submission, API fetch, result rendering, accordion behaviour.
 */

const API_BASE = "";

// ── Auth Token Management ──
const isLocal = ["localhost", "127.0.0.1"].includes(window.location.hostname);
let authToken = localStorage.getItem("rag_access_token");

function showLoginModal() {
    document.getElementById('login-modal').classList.remove('hidden');
}
function hideLoginModal() {
    document.getElementById('login-modal').classList.add('hidden');
}

if (!isLocal && !authToken) {
    window.addEventListener('DOMContentLoaded', showLoginModal);
}

window.addEventListener('DOMContentLoaded', () => {
    const loginBtn = document.getElementById('login-submit-btn');
    const loginInput = document.getElementById('login-token-input');
    if (loginBtn) {
        loginBtn.addEventListener('click', () => {
            const token = loginInput.value.trim();
            if (!token) return;
            authToken = token;
            localStorage.setItem('rag_access_token', token);
            hideLoginModal();
            loadCrosscheckFilters();  // reload filters now that we have auth
        });
        loginInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') loginBtn.click();
        });
    }
});

function authFetch(url, options = {}) {
    if (authToken) {
        options.headers = options.headers || {};
        options.headers['Authorization'] = `Bearer ${authToken}`;
    }
    return fetch(url, options).then(res => {
        if (res.status === 403 && !isLocal) {
            localStorage.removeItem('rag_access_token');
            authToken = null;
            showLoginModal();
        }
        return res;
    });
}

// ── DOM refs ──────────────────────────────────────────────────
const queryInput      = document.getElementById('query-input');
const submitBtn       = document.getElementById('submit-btn');
const loadingOverlay  = document.getElementById('loading-overlay');
const errorBanner     = document.getElementById('error-banner');
const resultsContainer= document.getElementById('results-container');

// Final answer
const finalAnswerText = document.getElementById('final-answer-text');

// Doc stream
const docChunks       = document.getElementById('doc-chunks');

// Image stream
const imgTop7Chunks   = document.getElementById('img-top7-chunks');
const imgTopName      = document.getElementById('img-top-name');
const imgAllChunks    = document.getElementById('img-all-chunks');

// Excel stream
const excelChunk      = document.getElementById('excel-chunk');
const excelSqlQuery   = document.getElementById('excel-sql-query');
const excelSqlResult  = document.getElementById('excel-sql-result');
const excelLlmOutput  = document.getElementById('excel-llm-output');

// History
const historyContent  = document.getElementById('history-content');


// ── Accordion logic ───────────────────────────────────────────
document.querySelectorAll('.accordion-trigger').forEach(btn => {
    btn.addEventListener('click', () => {
        const accordion = btn.closest('.accordion');
        accordion.classList.toggle('open');
    });
});


// ── Submit handler ────────────────────────────────────────────
submitBtn.addEventListener('click', handleSubmit);
queryInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && e.ctrlKey) handleSubmit();
});

async function handleSubmit() {
    const query = queryInput.value.trim();
    if (!query) return;

    // Reset UI
    showLoading(true);
    hideError();
    resultsContainer.classList.remove('visible');

    // Close all accordions
    document.querySelectorAll('.accordion.open').forEach(a => a.classList.remove('open'));

    try {
        const res = await authFetch(`${API_BASE}/query`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query }),
        });

        if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: res.statusText }));
            throw new Error(err.detail || `HTTP ${res.status}`);
        }

        const data = await res.json();
        renderResults(data);
    } catch (err) {
        showError(err.message || 'An unexpected error occurred.');
    } finally {
        showLoading(false);
    }
}


// ── Render results ────────────────────────────────────────────
function renderResults(data) {
    // 1. Final Answer
    finalAnswerText.innerHTML = renderMarkdown(data.final_answer || 'No answer generated.');

    // 2. Stream: Documents
    renderDocStream(data.stream_doc);

    // 3. Stream: Images
    renderImageStream(data.stream_image);

    // 4. Stream: Excel
    renderExcelStream(data.stream_excel);

    // 5. History
    renderHistory(data.history_snapshot);

    // Show results
    resultsContainer.classList.add('visible');
}


// ── Doc stream rendering ──────────────────────────────────────
function renderDocStream(stream) {
    docChunks.innerHTML = '';
    const chunks = stream?.chunks || [];

    if (chunks.length === 0) {
        docChunks.innerHTML = '<p class="no-data">No document chunks retrieved.</p>';
    } else {
        chunks.forEach(c => {
            docChunks.appendChild(createChunkCard(c.index, c.text, `Source: ${c.source}`));
        });
    }
}


// ── Image stream rendering ────────────────────────────────────
function renderImageStream(stream) {
    imgTop7Chunks.innerHTML = '';
    imgAllChunks.innerHTML = '';

    const top7 = stream?.top7_chunks || [];
    if (top7.length === 0) {
        imgTop7Chunks.innerHTML = '<p class="no-data">No image chunks retrieved.</p>';
    } else {
        top7.forEach(c => {
            imgTop7Chunks.appendChild(
                createChunkCard(c.index, c.text, `Person: ${c.person} · Source: ${c.source}`)
            );
        });
    }

    // Top entity
    if (stream?.top_name) {
        imgTopName.innerHTML = `<span class="entity-badge">${escapeHtml(stream.top_name)}</span>`;
    } else {
        imgTopName.innerHTML = '<p class="no-data">No entity identified.</p>';
    }

    // All chunks for entity
    const allChunks = stream?.all_name_chunks || [];
    if (allChunks.length === 0) {
        imgAllChunks.innerHTML = '<p class="no-data">No additional chunks.</p>';
    } else {
        allChunks.forEach(c => {
            imgAllChunks.appendChild(
                createChunkCard(c.index, c.text, `Person: ${c.person} · Source: ${c.source}`)
            );
        });
    }
}


// ── Excel stream rendering ───────────────────────────────────
function renderExcelStream(stream) {
    // Schema chunk
    if (stream?.chunk) {
        excelChunk.innerHTML = `<span class="chunk-text">${escapeHtml(stream.chunk)}</span>`;
    } else {
        excelChunk.innerHTML = '<p class="no-data">No schema chunk retrieved.</p>';
    }

    // SQL query
    if (stream?.sql_query) {
        excelSqlQuery.innerHTML = `<pre class="sql-code">${escapeHtml(stream.sql_query)}</pre>`;
    } else {
        excelSqlQuery.innerHTML = '<p class="no-data">No SQL query generated.</p>';
    }

    // SQL result
    renderSqlResult(stream?.sql_result);

    // LLM output
    excelLlmOutput.innerHTML = renderMarkdown(stream?.llm_output || 'No LLM output.');
}


function renderSqlResult(result) {
    if (result === null || result === undefined) {
        excelSqlResult.innerHTML = '<p class="no-data">No data returned.</p>';
        return;
    }

    if (typeof result === 'string') {
        // Error string
        excelSqlResult.innerHTML = `<p class="sql-error">${escapeHtml(result)}</p>`;
        return;
    }

    if (Array.isArray(result) && result.length === 0) {
        excelSqlResult.innerHTML = '<p class="no-data">Query returned 0 rows.</p>';
        return;
    }

    if (Array.isArray(result)) {
        // Build HTML table
        const headers = Object.keys(result[0]);
        let html = '<div class="table-scroll"><table class="result-table"><thead><tr>';
        headers.forEach(h => html += `<th>${escapeHtml(h)}</th>`);
        html += '</tr></thead><tbody>';
        result.forEach(row => {
            html += '<tr>';
            headers.forEach(h => {
                const val = row[h];
                html += `<td>${val !== null && val !== undefined ? escapeHtml(String(val)) : ''}</td>`;
            });
            html += '</tr>';
        });
        html += '</tbody></table></div>';
        excelSqlResult.innerHTML = html;
    }
}


// ── History rendering ─────────────────────────────────────────
function renderHistory(snapshot) {
    if (!snapshot || snapshot.length === 0) {
        historyContent.innerHTML = '<p class="no-data">No previous responses.</p>';
        return;
    }

    let html = '<ol class="history-list">';
    snapshot.forEach(item => {
        html += `<li>${renderMarkdown(item)}</li>`;
    });
    html += '</ol>';
    historyContent.innerHTML = html;
}


// ── Helpers ───────────────────────────────────────────────────
function createChunkCard(index, text, subtitle) {
    const card = document.createElement('div');
    card.className = 'chunk-card';
    card.innerHTML = `
        <div class="chunk-header">
            <span class="chunk-badge">${index}</span>
            <span class="chunk-source">${escapeHtml(subtitle)}</span>
        </div>
        <div class="chunk-text">${escapeHtml(text)}</div>
    `;
    return card;
}

function escapeHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

/**
 * Lightweight markdown → HTML for LLM output.
 * Handles: **bold**, *italic*, bullet lists, newlines.
 */
function renderMarkdown(text) {
    if (!text) return '';
    let html = escapeHtml(text);
    // **bold** — non-greedy, works across newlines (s flag)
    html = html.replace(/\*\*([^*]+?)\*\*/g, '<strong>$1</strong>');
    // Bullet lists: lines starting with  *  or  -  (with space after)
    html = html.replace(/^\*\s+(.+)$/gm, '<li>$1</li>');
    html = html.replace(/^-\s+(.+)$/gm, '<li>$1</li>');
    // Wrap consecutive <li> items in <ul>
    html = html.replace(/((?:<li>.*?<\/li>\s*)+)/g, '<ul>$1</ul>');
    // *italic* (single asterisk, only AFTER bold and bullets are done)
    html = html.replace(/\*([^*\n]+)\*/g, '<em>$1</em>');
    // Newlines → <br>
    html = html.replace(/\n/g, '<br>');
    // Clean up <br> inside <ul>
    html = html.replace(/<ul><br>/g, '<ul>');
    html = html.replace(/<br><\/ul>/g, '</ul>');
    return html;
}

function showLoading(visible) {
    submitBtn.disabled = visible;
    if (visible) {
        loadingOverlay.classList.add('visible');
    } else {
        loadingOverlay.classList.remove('visible');
    }
}

function showError(msg) {
    errorBanner.textContent = msg;
    errorBanner.style.display = 'block';
}

function hideError() {
    errorBanner.style.display = 'none';
    errorBanner.textContent = '';
}


// ═══════════════════════════════════════════════════════════════
// Crosscheck Dashboard
// ═══════════════════════════════════════════════════════════════

const ccProfile   = document.getElementById('cc-profile');
const ccMonth     = document.getElementById('cc-month');
const ccSubmitBtn = document.getElementById('cc-submit-btn');
const ccSummary   = document.getElementById('cc-summary');
const ccResults   = document.getElementById('cc-results');
const ccWarning   = document.getElementById('cc-warning');

let ccFiltersData = null;  // cached {profiles, months}

// Load filters on page load
window.addEventListener('DOMContentLoaded', loadCrosscheckFilters);

async function loadCrosscheckFilters() {
    try {
        const res = await authFetch(`${API_BASE}/crosscheck/filters`);
        if (!res.ok) return;
        ccFiltersData = await res.json();

        ccProfile.innerHTML = '<option value="">-- Select --</option>';
        (ccFiltersData.profiles || []).forEach(p => {
            const opt = document.createElement('option');
            opt.value = p; opt.textContent = p;
            ccProfile.appendChild(opt);
        });
    } catch (e) {
        console.error('Failed to load crosscheck filters:', e);
    }
}

// Profile change → populate months
ccProfile.addEventListener('change', () => {
    const p = ccProfile.value;
    ccMonth.innerHTML = '';
    ccSubmitBtn.disabled = true;

    if (!p || !ccFiltersData) {
        ccMonth.innerHTML = '<option value="">-- Select profile first --</option>';
        ccMonth.disabled = true;
        return;
    }

    const months = ccFiltersData.months[p] || [];
    if (months.length === 0) {
        ccMonth.innerHTML = '<option value="">No visit data yet</option>';
        ccMonth.disabled = true;
        return;
    }

    ccMonth.disabled = false;
    ccMonth.innerHTML = '<option value="">-- Select month --</option>';
    months.forEach(m => {
        const opt = document.createElement('option');
        opt.value = m;
        // Format MM_YYYY as "MM / YYYY" for display
        const parts = m.split('_');
        opt.textContent = parts.length === 2 ? `${parts[0]} / ${parts[1]}` : m;
        ccMonth.appendChild(opt);
    });
});

// Month change → enable submit
ccMonth.addEventListener('change', () => {
    ccSubmitBtn.disabled = !ccMonth.value || !ccProfile.value;
});

// Submit crosscheck
ccSubmitBtn.addEventListener('click', runCrosscheck);

async function runCrosscheck() {
    const profile = ccProfile.value;
    const monthYear = ccMonth.value;
    if (!profile || !monthYear) return;

    ccSubmitBtn.disabled = true;
    ccSummary.classList.add('hidden');
    ccResults.classList.add('hidden');
    ccWarning.classList.add('hidden');
    showLoading(true);

    try {
        const res = await authFetch(
            `${API_BASE}/crosscheck?profile=${encodeURIComponent(profile)}&month_year=${encodeURIComponent(monthYear)}`
        );
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();

        if (data.error) {
            ccWarning.textContent = data.error;
            ccWarning.classList.remove('hidden');
            return;
        }

        if (data.warning) {
            ccWarning.textContent = data.warning;
            ccWarning.classList.remove('hidden');
        }

        renderCrosscheckResults(data);
    } catch (e) {
        ccWarning.textContent = `Crosscheck failed: ${e.message}`;
        ccWarning.classList.remove('hidden');
    } finally {
        showLoading(false);
        ccSubmitBtn.disabled = false;
    }
}

function renderCrosscheckResults(data) {
    const profile = data.profile || '';
    const monthYear = data.month_year || '';

    // Summary stats
    document.getElementById('cc-met-count').textContent = data.total_met || 0;
    document.getElementById('cc-notmet-count').textContent = data.total_not_met || 0;
    document.getElementById('cc-unmatched-count').textContent = (data.unmatched_visits || []).length;
    document.getElementById('cc-total-count').textContent = data.total_in_list || 0;
    ccSummary.classList.remove('hidden');

    // Helper: build image URL
    const imgUrl = (filename) =>
        `/static_profiles/${encodeURIComponent(profile)}/${encodeURIComponent(monthYear)}/${encodeURIComponent(filename)}`;

    // Met doctors list (text only)
    const metList = document.getElementById('cc-met-list');
    const metDocs = data.met_doctors || [];
    if (metDocs.length === 0) {
        metList.innerHTML = '<div class="cc-empty">No doctors met this month.</div>';
    } else {
        metList.innerHTML = metDocs.map(d => `
            <div class="cc-item">
                <span class="cc-item-name">\u2611 ${escapeHtml(d.name)}</span>
                <span class="cc-item-badge met">\u00d7${d.visit_count}</span>
            </div>
        `).join('');
    }

    // Not met doctors list (text only)
    const notMetList = document.getElementById('cc-notmet-list');
    const notMetDocs = data.not_met_doctors || [];
    if (notMetDocs.length === 0) {
        notMetList.innerHTML = '<div class="cc-empty">All doctors have been visited! \ud83c\udf89</div>';
    } else {
        notMetList.innerHTML = notMetDocs.map(name => `
            <div class="cc-item">
                <span class="cc-item-name">\u2610 ${escapeHtml(name)}</span>
            </div>
        `).join('');
    }

    // Unmatched visits list (with thumbnails + lightbox)
    const unmatchedList = document.getElementById('cc-unmatched-list');
    const unmatched = data.unmatched_visits || [];
    if (unmatched.length === 0) {
        unmatchedList.innerHTML = '<div class="cc-empty">No extra visits outside the working list.</div>';
    } else {
        unmatchedList.innerHTML = unmatched.map(v => {
            const url = v.image ? imgUrl(v.image) : '';
            const thumb = url
                ? `<img src="${url}" alt="${escapeHtml(v.image)}" class="cc-thumb" title="Click to view full image" data-full="${url}" onerror="this.style.display='none'">`
                : '';
            return `
                <div class="cc-item">
                    <span class="cc-item-name">\u26a0 ${escapeHtml(v.name)}</span>
                    <span class="cc-item-badge unmatched">\u00d7${v.visit_count} \u00b7 ${escapeHtml(v.image || '')}</span>
                    ${thumb ? `<div class="cc-thumbs">${thumb}</div>` : ''}
                </div>
            `;
        }).join('');

        // Attach lightbox click handlers
        unmatchedList.querySelectorAll('.cc-thumb').forEach(img => {
            img.addEventListener('click', () => openLightbox(img.dataset.full));
        });
    }

    ccResults.classList.remove('hidden');
}

// ── Lightbox overlay ──
function openLightbox(src) {
    let overlay = document.getElementById('cc-lightbox');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'cc-lightbox';
        overlay.className = 'cc-lightbox';
        overlay.innerHTML = '<img>';
        overlay.addEventListener('click', () => overlay.classList.remove('visible'));
        document.body.appendChild(overlay);
    }
    overlay.querySelector('img').src = src;
    overlay.classList.add('visible');
}
