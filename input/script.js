const API_URL = "";

// ── Auth Token Management ──
// Localhost: no token needed (server auto-bypasses).
// Cloudflare: token stored in localStorage (type once, never again).
const isLocal = ["localhost", "127.0.0.1"].includes(window.location.hostname);
let authToken = localStorage.getItem("rag_access_token");

function showLoginModal() {
    document.getElementById('login-modal').classList.remove('hidden');
}
function hideLoginModal() {
    document.getElementById('login-modal').classList.add('hidden');
}

// Show login modal on first Cloudflare visit (no token yet)
if (!isLocal && !authToken) {
    // Defer so DOM is ready
    window.addEventListener('DOMContentLoaded', showLoginModal);
}

// Login form handler (set up after DOM loads)
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
        });
        loginInput.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') loginBtn.click();
        });
    }
});

/**
 * Wrapper around fetch() that injects the auth token header.
 * On 403 from a remote server, clears the stored token and shows login.
 */
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

// --- Tab Navigation ---
document.querySelectorAll('.nav-item').forEach(item => {
    item.addEventListener('click', () => {
        document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
        document.querySelectorAll('.tab-pane').forEach(t => t.classList.remove('active'));
        item.classList.add('active');
        document.getElementById(item.dataset.tab).classList.add('active');
        
        // specific initializations
        if(item.dataset.tab === 'tab-img') fetchProfiles();
        if(item.dataset.tab === 'tab-doclist') fetchProfiles();
        if(item.dataset.tab === 'tab-del') {
            fetchProfiles(); // fetch profiles to populate dropdown
            fetchDelDocs(); fetchDelTables();
        }
    });
});

// --- UI Helpers ---
let toastTimeout;
function showToast(msg, type='info') {
    const toast = document.getElementById('toast');
    toast.textContent = msg;
    toast.className = `toast ${type} show`;
    clearTimeout(toastTimeout);
    toastTimeout = setTimeout(() => toast.classList.remove('show'), 4000);
}

const showLoading = (msg="Processing...") => {
    document.getElementById('loading-msg').textContent = msg;
    document.getElementById('loading-overlay').classList.remove('hidden');
};
const hideLoading = () => document.getElementById('loading-overlay').classList.add('hidden');

// --- Tab 1: Document Upload ---
const docUploadZone = document.getElementById('doc-upload-zone');
const docFileInput = document.getElementById('doc-file-input');
const docSubmitBtn = document.getElementById('doc-submit-btn');
let selectedDoc = null;

docUploadZone.addEventListener('click', () => docFileInput.click());
docFileInput.addEventListener('change', (e) => {
    if(e.target.files.length) {
        selectedDoc = e.target.files[0];
        docUploadZone.querySelector('h3').textContent = selectedDoc.name;
        docSubmitBtn.disabled = false;
    }
});
docSubmitBtn.addEventListener('click', async () => {
    if(!selectedDoc) return;
    docSubmitBtn.disabled = true;
    showLoading("Chunking and Embedding Document. DO NOT RELOAD THE PAGE!");
    const fd = new FormData(); fd.append("file", selectedDoc);
    try {
        const res = await authFetch(`${API_URL}/upload_doc`, {method: "POST", body: fd});
        if(!res.ok) throw new Error(await res.text());
        const data = await res.json();
        showToast(`Success! Inserted ${data.chunks_processed} chunks.`, 'success');
        docUploadZone.querySelector('h3').textContent = "Upload File";
        selectedDoc = null; docFileInput.value = '';
    } catch(err) {
        showToast("Error: " + err.message, "error");
        docSubmitBtn.disabled = false;
    } finally {
        hideLoading();
    }
});

// --- Tab 2: Images & Profiles ---
const profileSelect = document.getElementById('profile-select');
const newProfileName = document.getElementById('new-profile-name');
const currentProfileLabel = document.getElementById('current-profile-label');
const imgUploadZone = document.getElementById('img-upload-zone');
const imgFileInput = document.getElementById('img-file-input');
const imgSubmitBtn = document.getElementById('img-submit-btn');
const thumbnailGrid = document.getElementById('thumbnail-grid');
let selectedImages = []; // batch image upload array

const delProfileSelect = document.getElementById('del-profile-select');

async function fetchProfiles() {
    try {
        const res = await authFetch(`${API_URL}/profiles`);
        const data = await res.json();
        
        const curr = profileSelect.value;
        profileSelect.innerHTML = '<option value="">-- Select Profile --</option>';
        const currDel = delProfileSelect.value;
        delProfileSelect.innerHTML = '<option value="">-- All Profiles --</option>';
        
        // Also sync the Doctor List tab profile selector
        const dlProfileSelect = document.getElementById('dl-profile-select');
        const currDl = dlProfileSelect ? dlProfileSelect.value : '';
        if (dlProfileSelect) dlProfileSelect.innerHTML = '<option value="">-- Select Profile --</option>';
        
        data.profiles.forEach(p => {
            const opt = document.createElement('option');
            opt.value = p; opt.textContent = p;
            profileSelect.appendChild(opt);
            
            const optDel = document.createElement('option');
            optDel.value = p; optDel.textContent = p;
            delProfileSelect.appendChild(optDel);
            
            if (dlProfileSelect) {
                const optDoc = document.createElement('option');
                optDoc.value = p; optDoc.textContent = p;
                dlProfileSelect.appendChild(optDoc);
            }
        });
        
        if(data.profiles.includes(curr)) profileSelect.value = curr;
        if(data.profiles.includes(currDel)) delProfileSelect.value = currDel;
        if(dlProfileSelect && data.profiles.includes(currDl)) dlProfileSelect.value = currDl;
    } catch(e) { console.error("Error fetching profiles:", e); }
}

document.getElementById('create-profile-btn').addEventListener('click', async () => {
    const name = newProfileName.value.trim();
    if(!name) return showToast("Enter a profile name", "error");
    const fd = new FormData(); fd.append("name", name);
    try {
        const res = await authFetch(`${API_URL}/profiles`, {method: "POST", body: fd});
        if(!res.ok) throw new Error(await res.text());
        newProfileName.value = '';
        await fetchProfiles();
        profileSelect.value = name;
        profileSelect.dispatchEvent(new Event('change'));
        showToast(`Profile ${name} created`, "success");
    } catch(e) { showToast(e.message, "error"); }
});

document.getElementById('delete-profile-btn').addEventListener('click', async () => {
    const p = profileSelect.value;
    if(!p) return;
    if(!confirm(`Delete profile ${p} and all its images?`)) return;
    try {
        const res = await authFetch(`${API_URL}/profiles/${p}`, {method: "DELETE"});
        if(!res.ok) throw new Error(await res.text());
        await fetchProfiles();
        profileSelect.dispatchEvent(new Event('change'));
        showToast("Profile deleted", "success");
    } catch(e) { showToast(e.message, "error"); }
});

profileSelect.addEventListener('change', () => {
    const p = profileSelect.value;
    currentProfileLabel.textContent = p || '...';
    if(p) {
        imgUploadZone.style.pointerEvents = 'auto';
        imgUploadZone.style.opacity = '1';
        fetchProfileImages(p);
    } else {
        imgUploadZone.style.pointerEvents = 'none';
        imgUploadZone.style.opacity = '0.5';
        thumbnailGrid.innerHTML = '<div class="empty-text">Select a profile to view images</div>';
        selectedImages = [];
        imgSubmitBtn.disabled = true;
        imgUploadZone.querySelector('h3').textContent = "Upload Image";
    }
});

async function fetchProfileImages(profile) {
    try {
        const res = await authFetch(`${API_URL}/profiles/${profile}/images`);
        const data = await res.json();
        thumbnailGrid.innerHTML = '';
        if(data.images.length === 0) {
            thumbnailGrid.innerHTML = '<div class="empty-text">No images yet.</div>';
            return;
        }
        // Group images by month folder
        const groups = {};
        data.images.forEach(img => {
            const parts = img.split('/');
            const month = parts.length > 1 ? parts[0] : '__unsorted__';
            if (!groups[month]) groups[month] = [];
            groups[month].push(img);
        });
        // Sort months chronologically (MM_YYYY → YYYYMM for sorting)
        const sortedMonths = Object.keys(groups).sort((a, b) => {
            if (a === '__unsorted__') return 1;
            if (b === '__unsorted__') return -1;
            const [ma, ya] = a.split('_');
            const [mb, yb] = b.split('_');
            return (ya + ma).localeCompare(yb + mb);
        });
        sortedMonths.forEach(month => {
            const header = document.createElement('div');
            header.className = 'month-group-header';
            header.textContent = month === '__unsorted__' ? 'Unsorted' : month.replace('_', ' / ');
            thumbnailGrid.appendChild(header);
            const row = document.createElement('div');
            row.className = 'month-group-images';
            groups[month].forEach(img => {
                const imgEl = document.createElement('img');
                imgEl.src = `${API_URL}/static_profiles/${profile}/${img}`;
                imgEl.alt = img;
                imgEl.title = img;
                row.appendChild(imgEl);
            });
            thumbnailGrid.appendChild(row);
        });
    } catch(e) {}
}

imgUploadZone.addEventListener('click', () => imgFileInput.click());


imgFileInput.addEventListener('change', (e) => {
    const preview = document.getElementById('doctor-name-preview');
    selectedImages = [];
    if (!e.target.files.length) return;

    const files = Array.from(e.target.files);
    const valid = [];
    const invalid = [];

    files.forEach(file => {
        const nameNoExt = file.name.replace(/\.[^.]+$/, '').trim();
        if (!/^dr\b/i.test(nameNoExt)) {
            invalid.push(file.name);
            return;
        }
        const normalized = nameNoExt.replace(/,\s*/g, ' , ');
        const parts = normalized.split(/\s*,\s*|\s+(?=[Dd][Rr]\b)/);
        const doctors = parts.filter(p => /^dr\b/i.test(p.trim())).map(p => p.trim());
        if (doctors.length > 0) {
            valid.push({ file, doctors });
        } else {
            invalid.push(file.name);
        }
    });

    selectedImages = valid;

    if (valid.length === 0) {
        preview.className = 'doctor-preview error';
        preview.innerHTML = '⚠️ No valid files. Names must start with "Dr".';
        preview.classList.remove('hidden');
        imgSubmitBtn.disabled = true;
        imgUploadZone.querySelector('h3').textContent = "Upload Image";
        return;
    }

    // Show summary
    let html = `<strong>${valid.length} image${valid.length > 1 ? 's' : ''} ready</strong>`;
    if (invalid.length > 0) {
        html += ` <span style="color:var(--danger)">· ${invalid.length} skipped (bad name)</span>`;
    }
    html += '<div style="margin-top:6px; max-height:120px; overflow-y:auto; font-size:0.85em;">';
    valid.forEach(v => {
        html += `<div style="padding:2px 0;">📷 ${v.file.name} → ${v.doctors.map(d => `<span class="dr-tag">${d}</span>`).join('')}</div>`;
    });
    html += '</div>';

    preview.className = 'doctor-preview';
    preview.innerHTML = html;
    preview.classList.remove('hidden');
    imgSubmitBtn.disabled = false;
    imgUploadZone.querySelector('h3').textContent = `${valid.length} image${valid.length > 1 ? 's' : ''} selected`;
});

imgSubmitBtn.addEventListener('click', async () => {
    const p = profileSelect.value;
    if (!p || selectedImages.length === 0) return;
    imgSubmitBtn.disabled = true;

    const total = selectedImages.length;
    let succeeded = 0, failed = 0;
    const errors = [];

    showLoading(`Processing image 1 of ${total}...`);

    for (let i = 0; i < total; i++) {
        // Update progress
        document.getElementById('loading-msg').textContent = `Processing image ${i + 1} of ${total}...`;

        const fd = new FormData();
        fd.append("profile", p);
        fd.append("file", selectedImages[i].file);
        try {
            const res = await authFetch(`${API_URL}/upload_image`, { method: "POST", body: fd });
            if (!res.ok) {
                const errData = await res.json().catch(() => ({ detail: 'Upload failed' }));
                throw new Error(errData.detail || 'Upload failed');
            }
            succeeded++;
        } catch (err) {
            failed++;
            errors.push(`${selectedImages[i].file.name}: ${err.message}`);
        }
    }

    hideLoading();

    // Summary toast
    if (failed === 0) {
        showToast(`✅ All ${succeeded} image${succeeded > 1 ? 's' : ''} processed successfully!`, "success");
    } else {
        showToast(`${succeeded} succeeded, ${failed} failed. Check console for details.`, "error");
        console.error("Failed uploads:", errors);
    }

    document.getElementById('img-status').textContent = "";
    document.getElementById('doctor-name-preview').classList.add('hidden');
    fetchProfileImages(p);
    selectedImages = [];
    imgFileInput.value = '';
    imgUploadZone.querySelector('h3').textContent = "Upload Image";
});

// --- Tab 3: Excel Drag and Drop Flow (Adapted) ---
const excelState = {
    fileData: null, currentSheet: null, isDragging: false, startCell: null, endCell: null, selections: [],
    zoomLevel: 1.0, isMerging: false // zoom scale factor (0.4 – 2.0)
};
const ZOOM_MIN = 0.4, ZOOM_MAX = 2.0, ZOOM_STEP = 0.1;

const excelUploadZone = document.getElementById('excel-upload-zone');
const excelFileInput = document.getElementById('excel-file-input');
const sheetsPanel = document.getElementById('sheets-panel');
const sheetsList = document.getElementById('sheets-list');
const selectionsPanel = document.getElementById('selections-panel');
const selectionsList = document.getElementById('selections-list');
const tableWrapper = document.getElementById('table-wrapper');
const emptyState = document.getElementById('empty-state');
const selectionCounter = document.getElementById('selection-counter');
const excelConfirmBtn = document.getElementById('excel-confirm-btn');
const mergeBtn = document.getElementById('merge-tables-btn');

// ── Zoom Controls ────────────────────────────────────────────
function applyZoom() {
    const inner = document.querySelector('#table-wrapper > div');
    if (!inner) return;
    inner.style.transformOrigin = 'top left';
    inner.style.transform = `scale(${excelState.zoomLevel})`;
    document.getElementById('zoom-level').textContent = Math.round(excelState.zoomLevel * 100) + '%';
}

document.getElementById('zoom-in-btn').addEventListener('click', () => {
    excelState.zoomLevel = Math.min(ZOOM_MAX, +(excelState.zoomLevel + ZOOM_STEP).toFixed(2));
    applyZoom();
});
document.getElementById('zoom-out-btn').addEventListener('click', () => {
    excelState.zoomLevel = Math.max(ZOOM_MIN, +(excelState.zoomLevel - ZOOM_STEP).toFixed(2));
    applyZoom();
});
document.getElementById('zoom-reset-btn').addEventListener('click', () => {
    excelState.zoomLevel = 1.0;
    applyZoom();
});

// Ctrl+Scroll wheel zoom on the table area
tableWrapper.addEventListener('wheel', (e) => {
    if (!e.ctrlKey) return;
    e.preventDefault();
    if (e.deltaY < 0) {
        excelState.zoomLevel = Math.min(ZOOM_MAX, +(excelState.zoomLevel + ZOOM_STEP).toFixed(2));
    } else {
        excelState.zoomLevel = Math.max(ZOOM_MIN, +(excelState.zoomLevel - ZOOM_STEP).toFixed(2));
    }
    applyZoom();
}, { passive: false });

// ── Merge Tables ─────────────────────────────────────────────
mergeBtn.addEventListener('click', () => {
    if (excelState.selections.length < 2) {
        return showToast('Need at least 2 tables to merge', 'error');
    }

    const primary = excelState.selections[0];
    const mergedRawData = primary.raw_data.map(row => [...row]); // deep copy primary rows
    const headerCount = primary.headers.length;

    // Append data rows (skip header row) from all other tables
    for (let i = 1; i < excelState.selections.length; i++) {
        const other = excelState.selections[i];
        // other.raw_data[0] is header — skip it
        for (let r = 1; r < other.raw_data.length; r++) {
            const row = [];
            for (let c = 0; c < headerCount; c++) {
                row.push(other.raw_data[r] && other.raw_data[r][c] !== undefined ? other.raw_data[r][c] : '');
            }
            mergedRawData.push(row);
        }
    }

    // Open the editor modal to preview/edit the merged table
    currentEditorData = mergedRawData;
    excelState.isMerging = true;
    excelState.activeBounds = primary.bounds; // retain bounds from primary table
    excelState.mergeSourceSheet = primary.sheetName; // remember primary's sheet for save
    
    document.getElementById('selected-range-display').textContent = 'Merged Table';
    document.getElementById('table-name').value = primary.tableName;
    
    renderEditorModal();
    tableModal.classList.remove('hidden');
});

excelUploadZone.addEventListener('click', () => excelFileInput.click());
excelFileInput.addEventListener('change', async (e) => {
    if(e.target.files.length) {
        showLoading("Processing Excel...");
        const fd = new FormData(); fd.append("file", e.target.files[0]);
        try {
            const res = await authFetch(`${API_URL}/upload_excel`, {method: "POST", body: fd});
            if(!res.ok) throw new Error(await res.text());
            const data = await res.json();
            excelState.fileData = data;
            excelState.selections = [];
            updateExcelCounter();
            renderSheetsList(Object.keys(data.sheets));
            showToast("File loaded successfully", "success");
        } catch(err) {
            showToast("Error loading file", "error");
        } finally {
            hideLoading();
        }
    }
});

function renderSheetsList(sheets) {
    sheetsList.innerHTML = '';
    sheetsPanel.classList.remove('hidden');
    sheets.forEach((sh, i) => {
        const li = document.createElement('li');
        li.className = 'sheet-item'; li.textContent = sh;
        li.addEventListener('click', () => loadSheet(sh));
        sheetsList.appendChild(li);
        if(i===0) loadSheet(sh);
    });
}
function loadSheet(sh) {
    document.querySelectorAll('.sheet-item').forEach(i => i.classList.toggle('active', i.textContent===sh));
    excelState.currentSheet = sh;
    document.getElementById('grid-header').classList.remove('hidden');
    emptyState.classList.add('hidden');
    document.getElementById('current-sheet-name').textContent = sh;
    renderGrid(excelState.fileData.sheets[sh]);
}
function renderGrid(data) {
    if(!data || !data.length) return tableWrapper.innerHTML = '<div class="empty-state">Empty Sheet</div>';
    
    // Performance optimization for large tables
    const table = document.createElement('table'); table.className = 'data-table';
    let maxCols = 0; data.forEach(r => { if(r.length > maxCols) maxCols = r.length; });
    
    const thead = document.createElement('thead'); const hRow = document.createElement('tr');
    hRow.appendChild(document.createElement('th'));
    for(let c=0; c<maxCols; c++) { let th=document.createElement('th'); th.textContent=getColLetter(c); hRow.appendChild(th); }
    thead.appendChild(hRow); table.appendChild(thead);
    
    const tbody = document.createElement('tbody');
    let tbodyHtml = '';
    data.forEach((row, ri) => {
        tbodyHtml += `<tr><th>${ri+1}</th>`;
        for(let c=0; c<maxCols; c++) {
            tbodyHtml += `<td data-row="${ri}" data-col="${c}"><div class="cell-content">${row[c] !== undefined ? row[c]:''}</div></td>`;
        }
        tbodyHtml += '</tr>';
    });
    tbody.innerHTML = tbodyHtml;
    
    // Switch to Event Delegation for massive DOM speed improvement
    tbody.addEventListener('mousedown', (e) => {
        const td = e.target.closest('td');
        if(td) handleMouseDown.call(td, e);
    });
    tbody.addEventListener('mouseover', (e) => { // mouseover bubbles (unlike mouseenter)
        const td = e.target.closest('td');
        if(td) handleMouseEnter.call(td, e);
    });
    tbody.addEventListener('mouseup', (e) => {
        const td = e.target.closest('td');
        if(td) handleMouseUp.call(td, e);
    });
    
    table.appendChild(tbody);
    table.draggable = false;
    
    const innerContainer = document.createElement('div');
    innerContainer.style.position = 'relative';
    innerContainer.style.display = 'inline-block';
    // Let table layout decide width/height naturally
    innerContainer.appendChild(table);
    
    tableWrapper.innerHTML = ''; tableWrapper.appendChild(innerContainer);
    applyConfirmedSelections();
    applyZoom();  // preserve zoom when switching sheets
}

// Drag Logic Optimized
function handleMouseDown(e) { if(e.button!==0) return; excelState.isDragging = true;
    const r=parseInt(this.dataset.row), c=parseInt(this.dataset.col);
    excelState.startCell = {row:r, col:c}; excelState.endCell = {row:r, col:c};
    clearCurrentSelection(); updateSelectionVisuals();
}
function handleMouseEnter(e) { if(!excelState.isDragging) return;
    excelState.endCell = {row:parseInt(this.dataset.row), col:parseInt(this.dataset.col)};
    updateSelectionVisuals();
}
function handleMouseUp(e) { if(!excelState.isDragging) return;
    excelState.isDragging = false;
    _stopAutoScroll();
    const b = getBounds(excelState.startCell, excelState.endCell);
    if(b.maxRow - b.minRow >= 1) showTableModal(b);
    else clearCurrentSelection();
}
document.addEventListener('mouseup', () => { if(excelState.isDragging){ excelState.isDragging=false; _stopAutoScroll(); clearCurrentSelection();} });

// --- Shared autoscroll with rAF and distance-based acceleration ---
// Works for BOTH Excel tab and Doctor List tab
let _scrollRAF = null;
let _lastMouseX = 0, _lastMouseY = 0;

function _getActiveScrollCtx() {
    if (excelState.isDragging) return { wrapper: tableWrapper, mode: 'excel' };
    if (dlState.isDragging) return { wrapper: document.getElementById('dl-table-wrapper'), mode: 'doclist' };
    return null;
}

function _startAutoScroll() {
    if(_scrollRAF) return;
    function step() {
        const ctx = _getActiveScrollCtx();
        if (!ctx) { _scrollRAF = null; return; }
        const rect = ctx.wrapper.getBoundingClientRect();
        const edgeZone = 60;
        let sX = 0, sY = 0;
        // Distance-based speed: the further past the edge, the faster
        if(_lastMouseY < rect.top + edgeZone) {
            const dist = Math.max(0, (rect.top + edgeZone) - _lastMouseY);
            sY = -(4 + Math.pow(dist / 8, 1.5));
        } else if(_lastMouseY > rect.bottom - edgeZone) {
            const dist = Math.max(0, _lastMouseY - (rect.bottom - edgeZone));
            sY = 4 + Math.pow(dist / 8, 1.5);
        }
        if(_lastMouseX < rect.left + edgeZone) {
            const dist = Math.max(0, (rect.left + edgeZone) - _lastMouseX);
            sX = -(4 + Math.pow(dist / 8, 1.5));
        } else if(_lastMouseX > rect.right - edgeZone) {
            const dist = Math.max(0, _lastMouseX - (rect.right - edgeZone));
            sX = 4 + Math.pow(dist / 8, 1.5);
        }
        if(sX || sY) {
            ctx.wrapper.scrollBy(sX, sY);
            // Update end cell under cursor after scroll
            const el = document.elementFromPoint(_lastMouseX, _lastMouseY);
            if(el) {
                const td = el.closest('td[data-row]');
                if(td) {
                    if (ctx.mode === 'excel') {
                        excelState.endCell = { row: parseInt(td.dataset.row), col: parseInt(td.dataset.col) };
                        updateSelectionVisuals();
                    } else {
                        dlState.endCell = { r: +td.dataset.row, c: +td.dataset.col };
                        const tbody = td.closest('tbody');
                        if (tbody) dlHighlightSelection(tbody);
                    }
                }
            }
        }
        _scrollRAF = requestAnimationFrame(step);
    }
    _scrollRAF = requestAnimationFrame(step);
}

function _stopAutoScroll() {
    if(_scrollRAF) { cancelAnimationFrame(_scrollRAF); _scrollRAF = null; }
}

document.addEventListener('mousemove', (e) => {
    _lastMouseX = e.clientX;
    _lastMouseY = e.clientY;
    if (excelState.isDragging || dlState.isDragging) _startAutoScroll();
});

function getBounds(c1, c2) { return { minRow: Math.min(c1.row, c2.row), maxRow: Math.max(c1.row, c2.row), minCol: Math.min(c1.col, c2.col), maxCol: Math.max(c1.col, c2.col) }; }
function getColLetter(i) { let l=''; let t=i; while(t>=0) { l=String.fromCharCode(65+(t%26))+l; t=Math.floor(t/26)-1;} return l; }

function clearCurrentSelection() { 
    const overbox = document.getElementById('selection-overlay');
    if(overbox) overbox.style.display = 'none';
}

function updateSelectionVisuals() {
    const b = getBounds(excelState.startCell, excelState.endCell);
    if(!b) return clearCurrentSelection();
    
    const tbody = document.querySelector('.data-table tbody');
    if(!tbody) return;
    
    const innerContainer = document.querySelector('#table-wrapper > div');
    
    let overbox = document.getElementById('selection-overlay');
    if(!overbox) {
        overbox = document.createElement('div');
        overbox.id = 'selection-overlay';
        overbox.style.position = 'absolute';
        overbox.style.backgroundColor = 'var(--selection-bg)';
        overbox.style.border = '2px solid var(--selection-border)';
        overbox.style.pointerEvents = 'none';
        overbox.style.zIndex = '5';
        if(innerContainer) innerContainer.appendChild(overbox);
    }
    
    const startRow = tbody.children[b.minRow];
    const endRow = tbody.children[b.maxRow];
    if(!startRow || !endRow) return;
    
    const startTd = startRow.children[b.minCol+1];
    const endTd = endRow.children[b.maxCol+1];
    if(startTd && endTd) {
        overbox.style.top = startTd.offsetTop + 'px';
        overbox.style.left = startTd.offsetLeft + 'px';
        overbox.style.width = ((endTd.offsetLeft + endTd.offsetWidth) - startTd.offsetLeft) + 'px';
        overbox.style.height = ((endTd.offsetTop + endTd.offsetHeight) - startTd.offsetTop) + 'px';
        overbox.style.display = 'block';
    }
}

function applyConfirmedSelections() {
    document.querySelectorAll('.confirmed-overlay').forEach(el=> el.remove());
    const tbody = document.querySelector('.data-table tbody');
    const innerContainer = document.querySelector('#table-wrapper > div');
    if(!tbody || !innerContainer) return;
    
    excelState.selections.forEach(sel => {
        if(sel.sheetName !== excelState.currentSheet) return;
        const b = sel.bounds;
        const startRow = tbody.children[b.minRow];
        const endRow = tbody.children[b.maxRow];
        if(!startRow || !endRow) return;
        
        const startTd = startRow.children[b.minCol+1];
        const endTd = endRow.children[b.maxCol+1];
        
        if(startTd && endTd) {
            const overbox = document.createElement('div');
            overbox.className = 'confirmed-overlay';
            overbox.style.position = 'absolute';
            overbox.style.backgroundColor = 'rgba(107, 142, 58, 0.15)'; 
            overbox.style.border = '2px solid var(--accent-color)';
            overbox.style.pointerEvents = 'none';
            overbox.style.zIndex = '4';
            
            overbox.style.top = startTd.offsetTop + 'px';
            overbox.style.left = startTd.offsetLeft + 'px';
            overbox.style.width = ((endTd.offsetLeft + endTd.offsetWidth) - startTd.offsetLeft) + 'px';
            overbox.style.height = ((endTd.offsetTop + endTd.offsetHeight) - startTd.offsetTop) + 'px';
            
            innerContainer.appendChild(overbox);
        }
    });
}

// Modal logic for Excel (shared with Doctor List tab via currentModalMode)
const tableModal = document.getElementById('table-modal');
let currentEditorData = [];
let currentModalMode = 'excel'; // 'excel' or 'doclist'
function showTableModal(bounds) {
    currentModalMode = 'excel';
    excelState.isMerging = false;
    excelState.activeBounds = bounds;
    document.getElementById('selected-range-display').textContent = `${getColLetter(bounds.minCol)}${bounds.minRow+1}:${getColLetter(bounds.maxCol)}${bounds.maxRow+1}`;
    const tn = document.getElementById('table-name');
    tn.value = `${excelState.currentSheet.replace(/\s+/g,'_').toLowerCase()}_table_${excelState.selections.length+1}`;
    tn.readOnly = false;
    // Restore modal labels for Excel mode
    const editorLabel = document.querySelector('.editor-toolbar label');
    if (editorLabel) editorLabel.innerHTML = 'Data Preview & Edit <small>(First row = headers)</small>';
    document.getElementById('save-table-cfg-btn').textContent = 'Add to Selection';
    const sData = excelState.fileData.sheets[excelState.currentSheet];
    currentEditorData = [];
    for(let r=bounds.minRow; r<=bounds.maxRow; r++){
        const row = [];
        for(let c=bounds.minCol; c<=bounds.maxCol; c++) row.push(sData[r][c] !== undefined ? sData[r][c] : '');
        currentEditorData.push(row);
    }
    renderEditorModal();
    tableModal.classList.remove('hidden');
}
function renderEditorModal() {
    const cont = document.getElementById('modal-table-container');
    if(!currentEditorData.length) { cont.innerHTML = '<p>No data</p>'; return; }
    let maxCols = currentEditorData[0].length;
    let html = '<table class="edit-table"><thead><tr><th class="row-header"></th>';
    for(let c=0; c<maxCols; c++) html += `<th><button class="del-btn" onclick="delEditorCol(${c})">&times;</button></th>`;
    html += '</tr></thead><tbody>';
    currentEditorData.forEach((row, ri) => {
        html += `<tr><th class="row-header"><button class="del-btn" onclick="delEditorRow(${ri})">&times;</button></th>`;
        for(let c=0; c<maxCols; c++) {
            html += `<td><input type="text" class="edit-cell" value="${row[c]}" onchange="currentEditorData[${ri}][${c}]=this.value" ${ri===0 && currentModalMode==='excel'?'style="font-weight:bold;"':''}></td>`;
        }
        html += '</tr>';
    });
    html += '</tbody></table>';
    cont.innerHTML = html;
}
window.delEditorCol = (c) => { currentEditorData.forEach(r=>r.splice(c,1)); renderEditorModal(); }
window.delEditorRow = (r) => { currentEditorData.splice(r,1); renderEditorModal(); }
document.getElementById('close-modal').onclick = document.getElementById('cancel-table-btn').onclick = () => {
    if (currentModalMode === 'excel') {
        excelState.isMerging = false;
        excelState.mergeSourceSheet = null;
        tableModal.classList.add('hidden'); clearCurrentSelection(); applyConfirmedSelections();
    } else {
        tableModal.classList.add('hidden');
    }
};
document.getElementById('add-row-btn').onclick = () => { currentEditorData.push(new Array(currentEditorData[0].length).fill('')); renderEditorModal(); };
document.getElementById('add-col-btn').onclick = () => { currentEditorData.forEach(r=>r.push('')); renderEditorModal(); };

document.getElementById('save-table-cfg-btn').onclick = () => {
    // --- Doctor List mode: extract all cells as doctor names (no header row) ---
    if (currentModalMode === 'doclist') {
        // All rows are data — no header. Extract first column values as doctor names.
        const doctors = [];
        for (let r = 0; r < currentEditorData.length; r++) {
            const val = String(currentEditorData[r][0] || '').trim();
            if (val) doctors.push(val);
        }
        if (doctors.length === 0) return showToast("No doctor names found in selection", "error");

        // Store the selection for the save button
        dlState.selectedDoctors = doctors;
        const dlSelPanel = document.getElementById('dl-selections-panel');
        const dlSelInfo = document.getElementById('dl-selection-info');
        dlSelPanel.classList.remove('hidden');
        dlSelInfo.innerHTML = `<strong>${doctors.length}</strong> doctors selected<br><small>${doctors.slice(0, 5).join(', ')}${doctors.length > 5 ? '...' : ''}</small>`;
        document.getElementById('dl-save-btn').disabled = !document.getElementById('dl-profile-select').value;
        
        tableModal.classList.add('hidden');
        showToast(`${doctors.length} doctors selected`, "success");
        return;
    }

    // --- Excel mode (original flow) ---
    const tName = document.getElementById('table-name').value.trim();
    if(currentEditorData.length < 2 || !tName) return showToast("Need name and >=2 rows", "error");
    
    // Sanitize headers for MySQL column names
    const headers = [];
    const usedHeaders = new Set();
    currentEditorData[0].forEach((v,i) => {
        let base = String(v || `Col_${i}`).replace(/[^a-zA-Z0-9_]/g, '_');
        // Ensure it doesn't start with a digit (invalid SQL identifier)
        if(/^\d/.test(base)) base = 'Col_' + base;
        let h = base;
        let counter = 1;
        while(usedHeaders.has(h)) {
            h = base + '_' + counter;
            counter++;
        }
        usedHeaders.add(h);
        headers.push(h);
    });
    // Build data dicts for MySQL
    const tData = [];
    for(let r=1; r<currentEditorData.length; r++) {
        let row = {};
        for(let c=0; c<headers.length; c++) row[headers[c]] = currentEditorData[r][c];
        tData.push(row);
    }
    // Save the pristine 2D array (raw_data) for Chroma chunk generation on the server
    const rawData = currentEditorData.map(row => row.map(v => (v !== undefined && v !== null) ? v : ''));
    
    if (excelState.isMerging) {
        excelState.selections = [];
        excelState.isMerging = false;
    }
    
    const sheetName = excelState.mergeSourceSheet || excelState.currentSheet;
    excelState.mergeSourceSheet = null;

    excelState.selections.push({
        id: Date.now(), tableName: tName, sheetName: sheetName,
        bounds: excelState.activeBounds, data: tData, headers: headers, raw_data: rawData
    });
    tableModal.classList.add('hidden');
    updateExcelCounter(); applyConfirmedSelections();
    showToast(`Table ${tName} added`, "success");
};

function updateExcelCounter() {
    selectionCounter.textContent = excelState.selections.length;
    excelConfirmBtn.disabled = excelState.selections.length === 0;
    if(excelState.selections.length) selectionsPanel.classList.remove('hidden');
    else selectionsPanel.classList.add('hidden');
    
    // Show merge button only when 2+ tables selected
    mergeBtn.style.display = excelState.selections.length >= 2 ? '' : 'none';
    
    selectionsList.innerHTML = '';
    excelState.selections.forEach(sel => {
        const li = document.createElement('li'); li.className = 'sheet-item selection-item';
        li.innerHTML = `<div class="selection-info"><span class="selection-name">${sel.tableName}</span><span class="selection-sheet">${sel.sheetName}</span></div>
        <button class="remove-btn" onclick="removeSelection(${sel.id})"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg></button>`;
        selectionsList.appendChild(li);
    });
}
window.removeSelection = (id) => {
    excelState.selections = excelState.selections.filter(s=>s.id !== id);
    updateExcelCounter(); applyConfirmedSelections();
}

excelConfirmBtn.onclick = async () => {
    if(!excelState.selections.length) return;
    excelConfirmBtn.disabled = true;
    showLoading("Saving to MySQL and Embedding to Chroma Excel_store. DO NOT RELOAD THE PAGE!");
    const payload = { tables: excelState.selections.map(s => ({table_name: s.tableName, data: s.data, headers: s.headers, raw_data: s.raw_data})) };
    try {
        const res = await authFetch(`${API_URL}/save_tables`, {
            method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)
        });
        if(!res.ok) throw new Error(await res.text());
        const data = await res.json();
        showToast(`Successfully saved ${data.tables_saved} tables to MySQL & Chroma!`, "success");
        excelState.selections = []; updateExcelCounter(); applyConfirmedSelections();
    } catch(e) {
        showToast("Error: " + e.message, "error");
        excelConfirmBtn.disabled = false;
    } finally { hideLoading(); }
};

// ═══════════════════════════════════════════════════════════════
// Tab 4: Doctor List (full Excel-style selection flow)
// ═══════════════════════════════════════════════════════════════
const dlState = {
    fileData: null, currentSheet: null, isDragging: false,
    startCell: null, endCell: null, selectedDoctors: [],
    zoomLevel: 1.0, activeBounds: null,
};

const dlProfileSelect = document.getElementById('dl-profile-select');
const dlUploadZone = document.getElementById('dl-upload-zone');
const dlFileInput = document.getElementById('dl-file-input');
const dlSaveBtn = document.getElementById('dl-save-btn');

// Profile selector: enable/disable upload zone
dlProfileSelect.addEventListener('change', () => {
    const p = dlProfileSelect.value;
    if (p) {
        dlUploadZone.style.pointerEvents = 'auto';
        dlUploadZone.style.opacity = '1';
        fetchDlInfo(p);
    } else {
        dlUploadZone.style.pointerEvents = 'none';
        dlUploadZone.style.opacity = '0.5';
        document.getElementById('dl-info').classList.add('hidden');
    }
    dlSaveBtn.disabled = true;
    dlState.selectedDoctors = [];
    document.getElementById('dl-selections-panel').classList.add('hidden');
});

async function fetchDlInfo(profile) {
    try {
        const res = await authFetch(`${API_URL}/doctor_list/${profile}`);
        const data = await res.json();
        if (data.count > 0) {
            document.getElementById('dl-count').textContent = data.count;
            document.getElementById('dl-updated').textContent = data.last_updated ? `· Updated ${data.last_updated}` : '';
            document.getElementById('dl-info').classList.remove('hidden');
        } else {
            document.getElementById('dl-info').classList.add('hidden');
        }
    } catch(e) {
        document.getElementById('dl-info').classList.add('hidden');
    }
}

// Zoom controls for doctor list grid
function dlApplyZoom() {
    const inner = document.querySelector('#dl-table-wrapper > div');
    if (!inner) return;
    inner.style.transformOrigin = 'top left';
    inner.style.transform = `scale(${dlState.zoomLevel})`;
    document.getElementById('dl-zoom-level').textContent = Math.round(dlState.zoomLevel * 100) + '%';
}
document.getElementById('dl-zoom-in-btn').addEventListener('click', () => {
    dlState.zoomLevel = Math.min(ZOOM_MAX, +(dlState.zoomLevel + ZOOM_STEP).toFixed(2));
    dlApplyZoom();
});
document.getElementById('dl-zoom-out-btn').addEventListener('click', () => {
    dlState.zoomLevel = Math.max(ZOOM_MIN, +(dlState.zoomLevel - ZOOM_STEP).toFixed(2));
    dlApplyZoom();
});
document.getElementById('dl-zoom-reset-btn').addEventListener('click', () => {
    dlState.zoomLevel = 1.0;
    dlApplyZoom();
});
document.getElementById('dl-table-wrapper').addEventListener('wheel', (e) => {
    if (!e.ctrlKey) return;
    e.preventDefault();
    if (e.deltaY < 0) {
        dlState.zoomLevel = Math.min(ZOOM_MAX, +(dlState.zoomLevel + ZOOM_STEP).toFixed(2));
    } else {
        dlState.zoomLevel = Math.max(ZOOM_MIN, +(dlState.zoomLevel - ZOOM_STEP).toFixed(2));
    }
    dlApplyZoom();
}, { passive: false });

// File upload
dlUploadZone.addEventListener('click', () => dlFileInput.click());
dlFileInput.addEventListener('change', async (e) => {
    if (!e.target.files.length) return;
    showLoading("Processing file...");
    const fd = new FormData(); fd.append("file", e.target.files[0]);
    try {
        const res = await authFetch(`${API_URL}/upload_excel`, {method: "POST", body: fd});
        if (!res.ok) throw new Error(await res.text());
        const data = await res.json();
        dlState.fileData = data;
        dlState.selectedDoctors = [];
        document.getElementById('dl-selections-panel').classList.add('hidden');
        dlSaveBtn.disabled = true;
        dlRenderSheetsList(Object.keys(data.sheets));
        showToast("File loaded — select the doctor name column", "success");
    } catch(err) {
        showToast("Error loading file", "error");
    } finally {
        hideLoading();
    }
});

function dlRenderSheetsList(sheets) {
    const panel = document.getElementById('dl-sheets-panel');
    const list = document.getElementById('dl-sheets-list');
    list.innerHTML = '';
    panel.classList.remove('hidden');
    sheets.forEach((sh, i) => {
        const li = document.createElement('li');
        li.className = 'sheet-item'; li.textContent = sh;
        li.addEventListener('click', () => dlLoadSheet(sh));
        list.appendChild(li);
        if (i === 0) dlLoadSheet(sh);
    });
}

function dlLoadSheet(sheetName) {
    dlState.currentSheet = sheetName;
    document.getElementById('dl-current-sheet-name').textContent = sheetName;
    document.getElementById('dl-grid-header').classList.remove('hidden');
    
    // Highlight active sheet in sidebar
    document.querySelectorAll('#dl-sheets-list .sheet-item').forEach(li => {
        li.classList.toggle('active', li.textContent === sheetName);
    });
    
    dlRenderGrid(dlState.fileData.sheets[sheetName]);
}

function dlRenderGrid(sheetData) {
    const wrapper = document.getElementById('dl-table-wrapper');
    wrapper.innerHTML = '';
    if (!sheetData || !sheetData.length) {
        wrapper.innerHTML = '<div class="empty-state"><p>No data in this sheet</p></div>';
        return;
    }
    
    // Use the same DOM structure as the Excel tab's renderGrid
    const table = document.createElement('table'); table.className = 'data-table';
    let maxCols = 0; sheetData.forEach(r => { if(r.length > maxCols) maxCols = r.length; });
    
    const thead = document.createElement('thead'); const hRow = document.createElement('tr');
    hRow.appendChild(document.createElement('th'));
    for(let c=0; c<maxCols; c++) { let th=document.createElement('th'); th.textContent=getColLetter(c); hRow.appendChild(th); }
    thead.appendChild(hRow); table.appendChild(thead);
    
    const tbody = document.createElement('tbody');
    let tbodyHtml = '';
    sheetData.forEach((row, ri) => {
        tbodyHtml += `<tr><th>${ri+1}</th>`;
        for(let c=0; c<maxCols; c++) {
            tbodyHtml += `<td data-row="${ri}" data-col="${c}"><div class="cell-content">${row[c] !== undefined ? row[c]:''}</div></td>`;
        }
        tbodyHtml += '</tr>';
    });
    tbody.innerHTML = tbodyHtml;
    
    // Drag-to-select via event delegation (same pattern as Excel tab)
    tbody.addEventListener('mousedown', (e) => {
        if(e.button !== 0) return;
        const td = e.target.closest('td[data-row]');
        if (!td) return;
        e.preventDefault(); // Prevent text selection during drag
        dlState.isDragging = true;
        dlState.startCell = { r: +td.dataset.row, c: +td.dataset.col };
        dlState.endCell = { ...dlState.startCell };
        dlHighlightSelection(tbody);
    });
    tbody.addEventListener('mouseover', (e) => {
        if (!dlState.isDragging) return;
        const td = e.target.closest('td[data-row]');
        if (!td) return;
        dlState.endCell = { r: +td.dataset.row, c: +td.dataset.col };
        dlHighlightSelection(tbody);
    });
    tbody.addEventListener('mouseup', (e) => {
        if (!dlState.isDragging) return;
        dlState.isDragging = false;
        _stopAutoScroll();
        const bounds = {
            minRow: Math.min(dlState.startCell.r, dlState.endCell.r),
            maxRow: Math.max(dlState.startCell.r, dlState.endCell.r),
            minCol: Math.min(dlState.startCell.c, dlState.endCell.c),
            maxCol: Math.max(dlState.startCell.c, dlState.endCell.c),
        };
        if (bounds.maxRow - bounds.minRow >= 1) {
            dlShowModal(bounds);
        }
    });
    
    table.appendChild(tbody);
    table.draggable = false;
    // Prevent text selection on the table during drag
    table.style.userSelect = 'none';
    table.style.webkitUserSelect = 'none';
    
    const innerContainer = document.createElement('div');
    innerContainer.style.position = 'relative';
    innerContainer.style.display = 'inline-block';
    innerContainer.appendChild(table);
    
    wrapper.appendChild(innerContainer);
    dlApplyZoom();
}

// Fallback: if mouse is released outside the grid, just clean up
document.addEventListener('mouseup', () => {
    if (dlState.isDragging) {
        dlState.isDragging = false;
        _stopAutoScroll();
    }
});

function dlHighlightSelection(grid) {
    grid.querySelectorAll('td.dl-sel').forEach(td => td.classList.remove('dl-sel'));
    if (!dlState.startCell || !dlState.endCell) return;
    const minR = Math.min(dlState.startCell.r, dlState.endCell.r);
    const maxR = Math.max(dlState.startCell.r, dlState.endCell.r);
    const minC = Math.min(dlState.startCell.c, dlState.endCell.c);
    const maxC = Math.max(dlState.startCell.c, dlState.endCell.c);
    grid.querySelectorAll('td[data-row]').forEach(td => {
        const r = +td.dataset.row, c = +td.dataset.col;
        if (r >= minR && r <= maxR && c >= minC && c <= maxC) td.classList.add('dl-sel');
    });
}

function dlShowModal(bounds) {
    currentModalMode = 'doclist';
    dlState.activeBounds = bounds;
    document.getElementById('selected-range-display').textContent =
        `${getColLetter(bounds.minCol)}${bounds.minRow+1}:${getColLetter(bounds.maxCol)}${bounds.maxRow+1}`;
    // Auto-set table name (user doesn't need to edit this for doctor lists)
    const profile = dlProfileSelect.value || 'unknown';
    const tn = document.getElementById('table-name');
    tn.value = `${profile.toLowerCase().replace(/\s+/g,'_')}_working_list`;
    tn.readOnly = true;
    
    // Update modal labels for doctor list context
    const editorLabel = document.querySelector('.editor-toolbar label');
    if (editorLabel) editorLabel.innerHTML = 'Data Preview & Edit <small>(All rows = doctor names, first column extracted)</small>';
    document.getElementById('save-table-cfg-btn').textContent = 'Confirm Selection';
    
    const sData = dlState.fileData.sheets[dlState.currentSheet];
    currentEditorData = [];
    for (let r = bounds.minRow; r <= bounds.maxRow; r++) {
        const row = [];
        for (let c = bounds.minCol; c <= bounds.maxCol; c++) {
            row.push(sData[r][c] !== undefined ? sData[r][c] : '');
        }
        currentEditorData.push(row);
    }
    renderEditorModal();
    tableModal.classList.remove('hidden');
}

// Save doctor list to backend
dlSaveBtn.onclick = async () => {
    const profile = dlProfileSelect.value;
    if (!profile || !dlState.selectedDoctors.length) return;
    
    dlSaveBtn.disabled = true;
    showLoading("Saving doctor list...");
    try {
        const res = await authFetch(`${API_URL}/save_doctor_list`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ profile, doctors: dlState.selectedDoctors })
        });
        if (!res.ok) {
            const err = await res.json().catch(() => ({detail: 'Save failed'}));
            throw new Error(err.detail || 'Save failed');
        }
        const data = await res.json();
        showToast(`${data.doctors_loaded} doctors saved for ${profile}`, 'success');
        dlState.selectedDoctors = [];
        document.getElementById('dl-selections-panel').classList.add('hidden');
        dlSaveBtn.disabled = true;
        fetchDlInfo(profile);
    } catch(err) {
        showToast(err.message || 'Error saving doctor list', 'error');
        dlSaveBtn.disabled = false;
    } finally {
        hideLoading();
    }
};

// --- Tab 5: Deletion Hub ---
// Inner tabs setup
document.querySelectorAll('.del-tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.del-tab-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.del-pane').forEach(p => p.classList.remove('active'));
        btn.classList.add('active');
        document.getElementById(btn.dataset.target).classList.add('active');
    });
});

async function fetchDelDocs() {
    const res = await authFetch(`${API_URL}/delete_view/docs`); const data = await res.json();
    const ul = document.getElementById('del-docs-list'); ul.innerHTML = '';
    data.items.forEach(i => {
        ul.innerHTML += `<li class="del-item"><div class="del-item-details"><div class="del-item-src">${i.source}</div><div class="del-item-id">${i.id}</div><div class="del-item-preview">${i.chunk}</div></div><button class="del-item-btn" onclick="delDbItem('docs', '${i.id}')" title="Delete">&times;</button></li>`;
    });
}
async function fetchDelImgs() {
    const prof = delProfileSelect.value;
    const grid = document.getElementById('del-imgs-list');
    
    if(!prof) {
        grid.innerHTML = '<div class="empty-state" style="grid-column: 1/-1;">Please select a profile.</div>';
        return;
    }
    
    const url = `${API_URL}/delete_view/images?profile=${encodeURIComponent(prof)}`;
    const res = await authFetch(url); const data = await res.json();
    grid.innerHTML = '';
    
    if(data.items.length === 0) {
        grid.innerHTML = '<div class="empty-state" style="grid-column: 1/-1;">No mapped image data.</div>';
        return;
    }
    // Group by month_year
    const groups = {};
    data.items.forEach(i => {
        const my = i.month_year || '__unsorted__';
        if (!groups[my]) groups[my] = [];
        groups[my].push(i);
    });
    const sortedMonths = Object.keys(groups).sort((a, b) => {
        if (a === '__unsorted__') return 1;
        if (b === '__unsorted__') return -1;
        const [ma, ya] = a.split('_');
        const [mb, yb] = b.split('_');
        return (ya + ma).localeCompare(yb + mb);
    });
    sortedMonths.forEach(my => {
        const label = my === '__unsorted__' ? 'Unsorted' : my.replace('_', ' / ');
        grid.innerHTML += `<div class="month-group-header" style="grid-column:1/-1;">${label}</div>`;
        groups[my].forEach(i => {
            // Build correct image path: if month_year exists, include it in path
            const imgPath = i.month_year ? `${i.month_year}/${i.source}` : i.source;
            const imgSrc = `${API_URL}/static_profiles/${i.person}/${imgPath}`;
            grid.innerHTML += `
            <div class="del-img-card">
                <img src="${imgSrc}" alt="${i.source}" onerror="this.src=''; this.alt='Image not found'">
                <div class="del-img-card-body">
                    <div class="del-item-src">${i.person}</div>
                    <div class="del-item-id">${i.id}</div>
                    <div class="del-item-preview">${i.data}</div>
                    <button class="btn btn-danger btn-sm w-full mt-2" onclick="delDbItem('images', '${i.id}')">Delete Embedded Item</button>
                </div>
            </div>`;
        });
    });
}
async function fetchDelTables() {
    const res = await authFetch(`${API_URL}/delete_view/tables`); const data = await res.json();
    const ul = document.getElementById('del-tables-list'); ul.innerHTML = '';
    data.items.forEach(i => {
        ul.innerHTML += `<li class="del-item"><div class="del-item-details"><div class="del-item-src">${i.source}</div><div class="del-item-id">${i.id}</div><div class="del-item-preview">${i.data}</div></div><button class="del-item-btn" onclick="delDbItem('tables', '${i.id}')" title="Delete">&times;</button></li>`;
    });
}
window.delDbItem = async (type, id) => {
    if(!confirm("Delete this embedded item?")) return;
    try {
        const res = await authFetch(`${API_URL}/delete_item/${type}/${id}`, {method: "DELETE"});
        if(!res.ok) throw new Error(await res.text());
        showToast("Item deleted", "success");
        if(type==='docs') fetchDelDocs();
        else if(type==='images') fetchDelImgs();
        else if(type==='tables') fetchDelTables();
    } catch(e) { showToast(e.message, "error"); }
};

document.getElementById('fetch-docs-btn').onclick = fetchDelDocs;
document.getElementById('fetch-imgs-btn').onclick = fetchDelImgs;
document.getElementById('fetch-tables-btn').onclick = fetchDelTables;
delProfileSelect.addEventListener('change', fetchDelImgs);
