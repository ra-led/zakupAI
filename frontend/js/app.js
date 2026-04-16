(function () {
  'use strict';

  // ── State ──────────────────────────────────────────────────────────
  var purchases = [];
  var currentPurchase = null;
  var currentLots = [];
  var currentSuppliers = [];
  var suppliersExpanded = false;
  var currentBids = [];
  var selectedBidId = null;
  var lotsPollingTimer = null;
  var searchPollingTimer = null;
  var searchStartTime = null;
  var searchTimerInterval = null;
  var comparisonPollingTimer = null;

  // ── DOM cache ──────────────────────────────────────────────────────
  var $ = function (id) { return document.getElementById(id); };

  // ── Utilities ──────────────────────────────────────────────────────

  function formatDate(dateStr) {
    if (!dateStr) return '';
    var d = new Date(dateStr);
    var pad = function (n) { return n < 10 ? '0' + n : '' + n; };
    return pad(d.getDate()) + '.' + pad(d.getMonth() + 1) + '.' + d.getFullYear() +
      ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        showMessage('Скопировано в буфер обмена');
      });
    } else {
      var ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand('copy'); showMessage('Скопировано в буфер обмена'); }
      catch (_) { showError('Не удалось скопировать'); }
      document.body.removeChild(ta);
    }
  }

  function escapeHtml(str) {
    if (!str) return '';
    var div = document.createElement('div');
    div.appendChild(document.createTextNode(str));
    return div.innerHTML;
  }

  function pluralParams(n) {
    if (n % 10 === 1 && n % 100 !== 11) return '';
    if (n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 10 || n % 100 >= 20)) return 'а';
    return 'ов';
  }

  // ── Toasts ─────────────────────────────────────────────────────────

  function showError(msg) {
    var toast = $('error-toast');
    var text = $('error-toast-text');
    text.textContent = msg;
    toast.classList.remove('hidden');
    clearTimeout(toast._timer);
    toast._timer = setTimeout(function () { toast.classList.add('hidden'); }, 5000);
  }

  function showMessage(msg) {
    var toast = $('message-toast');
    var text = $('message-toast-text');
    text.textContent = msg;
    toast.classList.remove('hidden');
    clearTimeout(toast._timer);
    toast._timer = setTimeout(function () { toast.classList.add('hidden'); }, 3000);
  }

  // ── Modals ─────────────────────────────────────────────────────────

  function openModal(id) {
    var modal = $(id);
    if (modal) modal.classList.add('open');
  }

  function closeModal(id) {
    var modal = $(id);
    if (modal) modal.classList.remove('open');
  }

  function initModals() {
    // Close buttons (X icon)
    var closeBtns = document.querySelectorAll('.modal-close');
    for (var i = 0; i < closeBtns.length; i++) {
      closeBtns[i].addEventListener('click', function () {
        var overlay = this.closest('.modal-overlay');
        if (overlay) overlay.classList.remove('open');
      });
    }
    // Cancel buttons
    var cancelBtns = document.querySelectorAll('.modal-close-btn');
    for (var j = 0; j < cancelBtns.length; j++) {
      cancelBtns[j].addEventListener('click', function () {
        var overlay = this.closest('.modal-overlay');
        if (overlay) overlay.classList.remove('open');
      });
    }
    // Click overlay backdrop to close
    var overlays = document.querySelectorAll('.modal-overlay');
    for (var k = 0; k < overlays.length; k++) {
      overlays[k].addEventListener('click', function (e) {
        if (e.target === this) this.classList.remove('open');
      });
    }
  }

  // ── Tabs (sidebar) ────────────────────────────────────────────────

  function initTabs() {
    var tabs = document.querySelectorAll('.sidebar .tab[data-tab]');
    for (var i = 0; i < tabs.length; i++) {
      tabs[i].addEventListener('click', function () {
        var tabId = this.getAttribute('data-tab');
        // Toggle tab active state
        var allTabs = document.querySelectorAll('.sidebar .tab[data-tab]');
        for (var j = 0; j < allTabs.length; j++) {
          allTabs[j].classList.remove('active');
        }
        this.classList.add('active');
        // Toggle content panels
        var panels = document.querySelectorAll('.tab-content');
        for (var k = 0; k < panels.length; k++) {
          panels[k].classList.remove('active');
        }
        var panel = $('tab-' + tabId);
        if (panel) panel.classList.add('active');
        if (tabId === 'dashboard') loadDashboard();
        if (tabId === 'llm-trace') _searchTracePurchases($('trace-purchase-search') ? $('trace-purchase-search').value : '');
      });
    }
  }

  // ── Header: user info, logout ─────────────────────────────────────

  function initHeader() {
    var user = Auth.getUser();
    if (user) {
      $('user-info').textContent = user.email || user.full_name || '';
      if (user.is_admin) {
        $('admin-link').classList.remove('hidden');
      }
    }
    $('btn-logout').addEventListener('click', function () { Auth.logout(); });
  }

  // ── Purchase selector dropdown ────────────────────────────────────

  function initPurchaseSelector() {
    var selector = $('procurement-selector');
    var dropdown = $('procurement-dropdown');

    selector.addEventListener('click', function (e) {
      e.stopPropagation();
      dropdown.classList.toggle('hidden');
    });

    // Close dropdown when clicking outside
    document.addEventListener('click', function () {
      dropdown.classList.add('hidden');
    });

    dropdown.addEventListener('click', function (e) {
      e.stopPropagation();
    });
  }

  function renderPurchaseDropdown() {
    var dropdown = $('procurement-dropdown');
    if (!purchases.length) {
      dropdown.innerHTML = '<div class="procurement-dropdown-item" style="color:var(--text-secondary);cursor:default">Нет закупок</div>';
      return;
    }
    var html = '';
    for (var i = 0; i < purchases.length; i++) {
      var p = purchases[i];
      var active = currentPurchase && currentPurchase.id === p.id ? ' active' : '';
      html += '<div class="procurement-dropdown-item' + active + '" data-purchase-id="' + p.id + '">' +
        escapeHtml(p.custom_name || 'Закупка #' + p.id) +
        '</div>';
    }
    dropdown.innerHTML = html;

    // Bind clicks
    var items = dropdown.querySelectorAll('[data-purchase-id]');
    for (var j = 0; j < items.length; j++) {
      items[j].addEventListener('click', function () {
        var id = parseInt(this.getAttribute('data-purchase-id'), 10);
        var p = purchases.find(function (x) { return x.id === id; });
        if (p) {
          selectPurchase(p);
          $('procurement-dropdown').classList.add('hidden');
        }
      });
    }
  }

  function updateSelectorText() {
    var textEl = $('procurement-selector-text');
    if (currentPurchase) {
      textEl.textContent = currentPurchase.custom_name || 'Закупка #' + currentPurchase.id;
    } else {
      textEl.textContent = 'Выберите закупку';
    }
  }

  // ── Purchase list ──────────────────────────────────────────────────

  async function loadPurchases() {
    try {
      purchases = await API.apiFetch('/purchases');
      renderPurchaseDropdown();
      if (purchases.length > 0 && !currentPurchase) {
        selectPurchase(purchases[0]);
      }
      if (!purchases.length) {
        updateSelectorText();
      }
    } catch (e) {
      showError('Не удалось загрузить список закупок: ' + e.message);
    }
  }

  async function selectPurchase(purchase) {
    currentPurchase = purchase;
    clearPolling();
    lastLotsStatus = null;
    lastLotsError = null;
    lotsExpanded = false;
    suppliersExpanded = false;
    currentLots = [];
    // Reset DOM that can leak across purchases
    var searchStatusEl = $('search-status');
    if (searchStatusEl) {
      searchStatusEl.classList.add('hidden');
      searchStatusEl.innerHTML = '';
    }
    stopSearchTimer();
    searchStartTime = null;
    var lotsContainer = $('lots-container');
    if (lotsContainer) lotsContainer.innerHTML = '';
    resetTzUploadZone();
    updateSelectorText();
    renderPurchaseDropdown();
    // Fetch full purchase (dashboard endpoint omits terms_text)
    try {
      var full = await API.apiFetch('/purchases/' + purchase.id);
      if (full && currentPurchase && currentPurchase.id === purchase.id) {
        currentPurchase = Object.assign({}, currentPurchase, full);
      }
    } catch (_) { /* fallback to dashboard data */ }
    // Reflect already-uploaded TZ filename in the upload zone
    try {
      var files = await API.apiFetch('/purchases/' + purchase.id + '/files');
      if (Array.isArray(files)) {
        var tzFile = files.find(function (f) { return f.file_type === 'tz'; });
        if (tzFile) markTzUploadZone(tzFile.filename);
      }
    } catch (_) { /* non-critical */ }
    // Load all data
    loadLots();
    loadSuppliers();
    checkSearchStatus();
    loadBids();
    loadRegimeCheck();
    // Reset comparison
    selectedBidId = null;
    $('comparison-results').innerHTML = '';
    $('btn-compare').disabled = true;
  }

  // ── Create purchase ────────────────────────────────────────────────

  function initCreatePurchase() {
    $('btn-new-purchase').addEventListener('click', function () {
      $('inp-purchase-target-tab').value = 'search';
      openModal('modal-new-purchase');
    });

    $('form-new-purchase').addEventListener('submit', async function (e) {
      e.preventDefault();
      var name = $('inp-purchase-name').value.trim();
      var termsText = $('inp-terms-text').value.trim();
      var targetTab = $('inp-purchase-target-tab').value || 'search';

      if (!name) { showError('Введите название закупки'); return; }

      try {
        var body = { custom_name: name };
        if (termsText) body.terms_text = termsText;
        var newPurchase = await API.apiFetch('/purchases', { method: 'POST', body: body });
        showMessage('Закупка создана');
        closeModal('modal-new-purchase');
        this.reset();
        purchases.unshift(newPurchase);

        var tab = document.querySelector('.sidebar .tab[data-tab="' + targetTab + '"]');
        if (tab) tab.click();

        await selectPurchase(newPurchase);
      } catch (e) {
        showError('Ошибка создания закупки: ' + e.message);
      }
    });
  }

  // Quick action cards on dashboard
  function _renderQuickActions(hasPurchases) {
    var el = $('dashboard-quick-actions');
    if (!el) return;

    var actions = [
      { tab: 'search', icon: 'person_search', title: 'Найти поставщиков', desc: 'Загрузите ТЗ — AI найдёт подходящих' },
      { tab: 'comparison', icon: 'rule_folder', title: 'Сравнить КП с ТЗ', desc: 'ТЗ и КП — AI сравнит характеристики' },
      { tab: 'regime', icon: 'verified_user', title: 'Проверить нацрежим', desc: 'Проверка по ПП №1875 и реестру' },
    ];

    var variant = hasPurchases ? '' : ' quick-actions-empty';
    var html = '<div class="quick-actions' + variant + '">';
    if (!hasPurchases) html += '<div class="quick-actions-title">Что вы хотите проверить?</div>';
    html += '<div class="quick-actions-grid">';
    for (var i = 0; i < actions.length; i++) {
      var a = actions[i];
      html += '<div class="quick-action-card" onclick="window._quickCreatePurchase(\'' + a.tab + '\')">';
      html += '<div class="quick-action-icon"><span class="material-symbols-outlined">' + a.icon + '</span></div>';
      html += '<div class="quick-action-body">';
      html += '<div class="quick-action-title">' + a.title + '</div>';
      html += '<div class="quick-action-desc">' + a.desc + '</div>';
      html += '</div></div>';
    }
    html += '</div></div>';
    el.innerHTML = html;
  }

  window._quickCreatePurchase = function (tab) {
    $('inp-purchase-target-tab').value = tab;
    $('inp-purchase-name').value = '';
    $('inp-terms-text').value = '';
    openModal('modal-new-purchase');
    setTimeout(function () { $('inp-purchase-name').focus(); }, 100);
  };

  function markTzUploadZone(filename) {
    var zone = $('tz-upload-zone');
    if (!zone) return;
    var label = zone.querySelector('.label');
    var hint = zone.querySelector('.hint');
    if (label) {
      label.textContent = filename;
      label.style.color = 'var(--text-primary)';
      label.style.fontWeight = '600';
    }
    if (hint) hint.style.display = 'none';
  }

  function resetTzUploadZone() {
    var zone = $('tz-upload-zone');
    if (!zone) return;
    var label = zone.querySelector('.label');
    var hint = zone.querySelector('.hint');
    if (label) {
      label.textContent = 'Загрузить ТЗ';
      label.style.color = '';
      label.style.fontWeight = '';
    }
    if (hint) hint.style.display = '';
  }

  // ── TZ Upload (on search tab) ─────────────────────────────────────

  function initTzUpload() {
    $('tz-upload-zone').addEventListener('click', function () {
      $('inp-tz-file').click();
    });

    $('inp-tz-file').addEventListener('change', async function () {
      var file = this.files[0];
      if (!file || !currentPurchase) return;

      var zone = $('tz-upload-zone');
      zone.querySelector('.label').textContent = file.name;
      zone.querySelector('.label').style.color = 'var(--text-primary)';
      zone.querySelector('.label').style.fontWeight = '600';
      zone.querySelector('.hint').style.display = 'none';

      try {
        showMessage('Конвертация документа...');
        var converted = await API.convertTechTaskFile(file, currentPurchase ? currentPurchase.id : null);
        if (converted && converted.markdown) {
          var existingTerms = currentPurchase.terms_text || '';
          var newTerms = existingTerms ? existingTerms + '\n\n' + converted.markdown : converted.markdown;
          await API.apiFetch('/purchases/' + currentPurchase.id, {
            method: 'PATCH',
            body: { terms_text: newTerms },
          });
          currentPurchase.terms_text = newTerms;
          trackFile(currentPurchase.id, file.name, 'tz');
          showMessage('ТЗ загружено, обновляем лоты...');
          loadLots();
          updateComparisonZones();
        }
      } catch (e) {
        showError('Ошибка загрузки ТЗ: ' + e.message);
      }
      this.value = '';
    });
  }

  // ── Lots ───────────────────────────────────────────────────────────

  var lastLotsStatus = null;
  var lastLotsError = null;
  var lotsExpanded = false;

  function logDiag(label, payload) {
    try {
      console.log('[zakupAI/' + label + ']', payload);
    } catch (_) {}
  }

  async function loadLots() {
    if (!currentPurchase) return;
    if (lotsPollingTimer) { clearTimeout(lotsPollingTimer); lotsPollingTimer = null; }
    logDiag('loadLots:start', { purchase_id: currentPurchase.id });
    try {
      var resp = await API.apiFetch('/purchases/' + currentPurchase.id + '/lots');
      var status = resp.status;
      currentLots = resp.lots || [];
      lastLotsStatus = status;
      lastLotsError = resp.error_text || null;
      logDiag('loadLots:resp', { status: status, lots_count: currentLots.length, error_text: resp.error_text });
      renderLots();
      updateLotsStatus(status);
      // Poll while task is still running
      if (status === 'queued' || status === 'in_progress') {
        lotsPollingTimer = setTimeout(loadLots, 3000);
      }
    } catch (e) {
      logDiag('loadLots:error', { message: e.message });
      showError('Ошибка загрузки лотов: ' + e.message);
    }
  }

  async function retryLotsExtraction() {
    if (!currentPurchase || !currentPurchase.terms_text) return;
    try {
      // Bump terms_text via PATCH to trigger a new enqueue (terms_text != original_terms is the trigger).
      // We re-send the same content with a trailing newline toggle to force the diff check.
      var bumped = currentPurchase.terms_text.endsWith('\n')
        ? currentPurchase.terms_text.slice(0, -1)
        : currentPurchase.terms_text + '\n';
      await API.apiFetch('/purchases/' + currentPurchase.id, {
        method: 'PATCH',
        body: { terms_text: bumped },
      });
      currentPurchase.terms_text = bumped;
      showMessage('Распознавание запущено повторно');
      loadLots();
    } catch (e) {
      showError('Не удалось перезапустить распознавание: ' + e.message);
    }
  }

  function updateLotsStatus(status) {
    var statusEl = $('lots-status');
    var textEl = $('lots-status-text');
    statusEl.className = 'status';
    if (status === 'completed' || status === 'done' || status === 'ready') {
      if (currentLots.length > 0) {
        statusEl.classList.add('status-active');
        textEl.textContent = currentLots.length + ' распознано';
        var badge = $('badge-search');
        badge.textContent = currentLots.length;
        badge.classList.remove('hidden');
      } else {
        statusEl.classList.add('status-draft');
        textEl.textContent = 'Лоты не найдены';
      }
    } else if (status === 'queued' || status === 'in_progress') {
      statusEl.classList.add('status-search');
      textEl.textContent = 'Обработка...';
    } else if (status === 'failed') {
      statusEl.classList.add('status-draft');
      textEl.textContent = 'Ошибка распознавания';
    } else if (status === 'idle') {
      statusEl.classList.add('status-draft');
      textEl.textContent = '--';
    } else {
      statusEl.classList.add('status-draft');
      textEl.textContent = '--';
    }
  }

  function renderLots() {
    var container = $('lots-container');
    var uploadCard = $('tz-upload-card');

    // Empty + failed → show error block with retry
    if (!currentLots.length && lastLotsStatus === 'failed') {
      var msg = lastLotsError
        ? escapeHtml(lastLotsError)
        : 'Не удалось распознать лоты в ТЗ. Попробуйте ещё раз или проверьте текст.';
      container.innerHTML =
        '<div class="empty-state" style="display:flex;flex-direction:column;gap:12px;align-items:center">' +
          '<div style="color:var(--text-secondary);text-align:center;max-width:480px">' + msg + '</div>' +
          '<button class="btn btn-primary" id="btn-retry-lots">Распознать ещё раз</button>' +
        '</div>';
      if (uploadCard) uploadCard.style.display = '';
      var retryBtn = $('btn-retry-lots');
      if (retryBtn) retryBtn.addEventListener('click', retryLotsExtraction);
      return;
    }

    // Empty + processing → spinner-ish hint
    if (!currentLots.length && (lastLotsStatus === 'queued' || lastLotsStatus === 'in_progress')) {
      container.innerHTML = '<div class="empty-state">Распознаём лоты из ТЗ, это займёт до минуты…</div>';
      if (uploadCard) uploadCard.style.display = '';
      return;
    }

    // Empty + idle (no terms_text yet) or any other terminal status → upload prompt
    if (!currentLots.length) {
      container.innerHTML = '<div class="empty-state">Загрузите ТЗ или добавьте лоты вручную</div>';
      if (uploadCard) uploadCard.style.display = '';
      return;
    }
    if (uploadCard) uploadCard.style.display = 'none';

    // Collapse long lists to first 3 with "show more" toggle.
    var COLLAPSE_THRESHOLD = 3;
    var shouldCollapse = currentLots.length > COLLAPSE_THRESHOLD && !lotsExpanded;
    var visibleCount = shouldCollapse ? COLLAPSE_THRESHOLD : currentLots.length;

    var html = '';
    for (var i = 0; i < visibleCount; i++) {
      var lot = currentLots[i];
      var paramCount = lot.parameters ? lot.parameters.length : 0;
      html += '<div class="lot-item" data-lot-index="' + i + '">' +
        '<div class="lot-num">' + (i + 1) + '</div>' +
        '<div class="lot-info">' +
        '<div class="lot-name">' + escapeHtml(lot.name) + '</div>' +
        '<div class="lot-meta">' + paramCount + ' параметр' + pluralParams(paramCount) + '</div>' +
        '</div></div>';
    }

    // Toggle row at the bottom
    if (currentLots.length > COLLAPSE_THRESHOLD) {
      if (shouldCollapse) {
        var hidden = currentLots.length - COLLAPSE_THRESHOLD;
        html += '<div class="lot-toggle" id="lot-toggle-row" style="text-align:center;padding:10px;cursor:pointer;color:var(--accent);font-weight:500;border-top:1px solid var(--border);margin-top:4px">' +
          'Показать ещё ' + hidden + ' ' + pluralLots(hidden) + ' ▼' +
          '</div>';
      } else {
        html += '<div class="lot-toggle" id="lot-toggle-row" style="text-align:center;padding:10px;cursor:pointer;color:var(--accent);font-weight:500;border-top:1px solid var(--border);margin-top:4px">' +
          'Свернуть ▲' +
          '</div>';
      }
    }

    container.innerHTML = html;

    // Click on lot row → detail modal
    var items = container.querySelectorAll('.lot-item');
    for (var j = 0; j < items.length; j++) {
      items[j].addEventListener('click', function () {
        var idx = parseInt(this.getAttribute('data-lot-index'), 10);
        showLotDetail(currentLots[idx]);
      });
    }

    // Click on toggle row → expand/collapse + re-render
    var toggleRow = $('lot-toggle-row');
    if (toggleRow) {
      toggleRow.addEventListener('click', function () {
        lotsExpanded = !lotsExpanded;
        renderLots();
      });
    }
  }

  function pluralLots(n) {
    var mod10 = n % 10;
    var mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) return 'лот';
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return 'лота';
    return 'лотов';
  }

  function showLotDetail(lot) {
    $('lot-detail-name').textContent = lot.name;
    var tbody = $('lot-detail-params-body');
    if (!lot.parameters || !lot.parameters.length) {
      tbody.innerHTML = '<tr><td colspan="3" style="text-align:center;color:var(--text-secondary)">Нет параметров</td></tr>';
    } else {
      var html = '';
      for (var i = 0; i < lot.parameters.length; i++) {
        var p = lot.parameters[i];
        html += '<tr><td>' + escapeHtml(p.name) + '</td><td>' + escapeHtml(p.value) + '</td><td>' + escapeHtml(p.units || '') + '</td></tr>';
      }
      tbody.innerHTML = html;
    }
    openModal('modal-lot-detail');
  }

  function initAddLot() {
    $('btn-add-lot').addEventListener('click', function () {
      $('form-add-lot').reset();
      $('lot-params-list').innerHTML = '';
      addLotParamRow();
      openModal('modal-add-lot');
    });

    $('btn-add-lot-param').addEventListener('click', addLotParamRow);

    $('form-add-lot').addEventListener('submit', async function (e) {
      e.preventDefault();
      if (!currentPurchase) return;
      var name = $('inp-lot-name').value.trim();
      if (!name) { showError('Введите название лота'); return; }

      var params = [];
      var rows = $('lot-params-list').querySelectorAll('.lot-param-row');
      for (var i = 0; i < rows.length; i++) {
        var inputs = rows[i].querySelectorAll('input');
        var pName = inputs[0].value.trim();
        var pValue = inputs[1].value.trim();
        var pUnits = inputs[2].value.trim();
        if (pName) params.push({ name: pName, value: pValue, units: pUnits });
      }

      try {
        await API.apiFetch('/purchases/' + currentPurchase.id + '/lots', {
          method: 'POST',
          body: { name: name, parameters: params },
        });
        showMessage('Лот добавлен');
        closeModal('modal-add-lot');
        loadLots();
      } catch (e) {
        showError('Ошибка добавления лота: ' + e.message);
      }
    });
  }

  function addLotParamRow() {
    var list = $('lot-params-list');
    var row = document.createElement('div');
    row.className = 'lot-param-row';
    row.innerHTML = '<input type="text" class="form-input" placeholder="Название">' +
      '<input type="text" class="form-input" placeholder="Значение">' +
      '<input type="text" class="form-input" placeholder="Ед. изм.">' +
      '<button type="button" class="btn btn-sm btn-secondary" style="color:var(--danger)" title="Удалить">&times;</button>';
    row.querySelector('button').addEventListener('click', function () { row.remove(); });
    list.appendChild(row);
  }

  // ── Supplier search ────────────────────────────────────────────────

  async function loadSuppliers() {
    if (!currentPurchase) return;
    try {
      currentSuppliers = await API.apiFetch('/purchases/' + currentPurchase.id + '/suppliers');
      renderSuppliers();
      renderOwnSuppliers();
      renderCorrespondenceSuppliers();
    } catch (e) {
      showError('Ошибка загрузки поставщиков: ' + e.message);
    }
  }

  async function checkSearchStatus() {
    if (!currentPurchase) return;
    clearTimeout(searchPollingTimer);
    try {
      var state = await API.apiFetch('/purchases/' + currentPurchase.id + '/suppliers/search');
      if (state && state.status) {
        renderSearchStatus(state);
        if (state.status === 'queued' || state.status === 'in_progress') {
          searchPollingTimer = setTimeout(function () {
            checkSearchStatus();
          }, 5000);
        } else if (state.status === 'completed') {
          loadSuppliers();
        }
      } else {
        // No search task for this purchase — make sure stale status block is hidden
        var el = $('search-status');
        if (el) {
          el.classList.add('hidden');
          el.innerHTML = '';
        }
      }
    } catch (_) {
      // No active search — that's fine
      var el2 = $('search-status');
      if (el2) {
        el2.classList.add('hidden');
        el2.innerHTML = '';
      }
    }
  }

  function formatElapsed(ms) {
    var s = Math.floor(ms / 1000);
    var m = Math.floor(s / 60);
    s = s % 60;
    return (m > 0 ? m + ' мин ' : '') + s + ' сек';
  }

  function startSearchTimer() {
    if (!searchStartTime) searchStartTime = Date.now();
    clearInterval(searchTimerInterval);
    searchTimerInterval = setInterval(function () {
      var el = $('search-elapsed');
      if (el) el.textContent = formatElapsed(Date.now() - searchStartTime);
    }, 1000);
  }

  function stopSearchTimer() {
    clearInterval(searchTimerInterval);
    searchTimerInterval = null;
  }

  function _parseCrawlProgressFromNote(note) {
    if (!note) return null;
    // "Краулинг сайтов: 12/47 (текущий: example.com, осталось ~5м 30с)"
    var m = note.match(/Краулинг сайтов:\s*(\d+)\s*\/\s*(\d+)(?:\s*\(текущий:\s*([^,)]+?)(?:,\s*осталось\s*~?([^)]+))?\))?/);
    if (m) {
      return {
        processed: parseInt(m[1], 10),
        total: parseInt(m[2], 10),
        current: m[3] ? m[3].trim() : null,
        eta: m[4] ? m[4].trim() : null,
      };
    }
    // Pre-crawl: "Найдено сайтов для обхода: 47"
    var m2 = note.match(/Найдено сайтов для обхода:\s*(\d+)/);
    if (m2) {
      return { processed: 0, total: parseInt(m2[1], 10), current: null, eta: null };
    }
    // Done: "Обход сайтов выполнен: 47 шт."
    var m3 = note.match(/Обход сайтов выполнен:\s*(\d+)/);
    if (m3) {
      var n = parseInt(m3[1], 10);
      return { processed: n, total: n, current: null, eta: null };
    }
    return null;
  }

  function renderSearchStatus(state) {
    var statusEl = $('search-status');
    statusEl.classList.remove('hidden');
    statusEl.style.background = '';

    if (state.status === 'queued' || state.status === 'in_progress') {
      startSearchTimer();
      var note = state.note || '';
      var crawlDone = note.indexOf('Обход сайтов выполнен') >= 0;
      var crawlInProgress = note.indexOf('Краулинг сайтов:') >= 0;
      var steps = [];
      steps.push({ label: 'Генерация поисковых запросов', done: !!(state.queries && state.queries.length) });
      steps.push({ label: 'Поиск через Яндекс и Perplexity', done: note.indexOf('Yandex поиск обработан') >= 0 || note.indexOf('Perplexity обработан') >= 0 });
      steps.push({ label: 'Обход сайтов и сбор контактов', done: crawlDone, inProgress: crawlInProgress && !crawlDone });

      var crawl = _parseCrawlProgressFromNote(note);

      var stepsHtml = '<div style="margin-top:8px">';
      for (var i = 0; i < steps.length; i++) {
        var icon = steps[i].done
          ? '<span style="color:var(--success)">&#10003;</span>'
          : '<span class="spinner" style="width:12px;height:12px;border-width:1.5px;display:inline-block;vertical-align:middle"></span>';
        var textStyle = steps[i].done ? 'color:var(--text-secondary)' : 'font-weight:500';
        stepsHtml += '<div style="font-size:13px;margin-bottom:4px;' + textStyle + '">' + icon + ' ' + steps[i].label;
        // Inline crawl progress on the third step
        if (i === 2 && steps[i].inProgress && crawl && crawl.total > 0) {
          var pct = Math.round((crawl.processed / crawl.total) * 100);
          stepsHtml += '<span style="margin-left:8px;font-weight:600;color:var(--accent)">' +
            crawl.processed + ' / ' + crawl.total + ' (' + pct + '%)</span>';
        }
        stepsHtml += '</div>';
      }
      stepsHtml += '</div>';

      // Visual progress bar + current site, only when crawl is active
      var crawlBlock = '';
      if (crawl && crawl.total > 0 && !crawlDone) {
        var pct2 = Math.round((crawl.processed / crawl.total) * 100);
        var currentLine = '';
        if (crawl.current) {
          currentLine = '<div style="font-size:12px;color:var(--text-secondary);margin-top:4px">Текущий сайт: <span style="color:var(--text-primary);font-weight:500">' + escapeHtml(crawl.current) + '</span>' +
            (crawl.eta ? ' · осталось ~' + escapeHtml(crawl.eta) : '') + '</div>';
        }
        crawlBlock =
          '<div style="margin-top:10px">' +
            '<div style="height:6px;background:var(--bg);border-radius:4px;overflow:hidden;border:1px solid var(--border)">' +
              '<div style="height:100%;width:' + pct2 + '%;background:linear-gradient(90deg,var(--accent),var(--success));transition:width .3s"></div>' +
            '</div>' +
            currentLine +
          '</div>';
      }

      statusEl.className = 'search-status';
      statusEl.style.flexDirection = 'column';
      statusEl.style.alignItems = 'stretch';
      statusEl.innerHTML =
        '<div style="display:flex;align-items:center;gap:12px">' +
        '<div class="spinner"></div>' +
        '<div><strong>Поиск идёт...</strong></div>' +
        '<div style="margin-left:auto;font-size:13px;color:var(--text-secondary)" id="search-elapsed">' + formatElapsed(Date.now() - (searchStartTime || Date.now())) + '</div>' +
        '</div>' +
        stepsHtml +
        crawlBlock;
    } else if (state.status === 'completed') {
      stopSearchTimer();
      var elapsed = searchStartTime ? formatElapsed(Date.now() - searchStartTime) : '';
      searchStartTime = null;
      statusEl.className = 'search-status';
      statusEl.style.background = 'var(--success-bg)';
      statusEl.style.flexDirection = '';
      statusEl.style.alignItems = '';
      statusEl.innerHTML = '<div style="color:var(--success);font-size:16px">&#10003;</div><div><strong style="color:var(--success)">Поиск завершён</strong></div>' +
        (elapsed ? '<div style="margin-left:auto;font-size:13px;color:var(--text-secondary)">' + elapsed + '</div>' : '');
    } else if (state.status === 'failed') {
      stopSearchTimer();
      searchStartTime = null;
      statusEl.className = 'search-status';
      statusEl.style.background = 'var(--danger-bg)';
      statusEl.style.flexDirection = 'column';
      statusEl.style.alignItems = 'stretch';
      // Try to extract a human-readable error from the note field
      var errMsg = 'Поиск завершился с ошибкой. Нажмите «Запустить поиск» ещё раз.';
      if (state.note && state.note.length > 0 && state.note !== 'Поиск поставщиков выполняется') {
        errMsg = state.note;
      }
      statusEl.innerHTML =
        '<div style="display:flex;align-items:center;gap:12px">' +
        '<div style="color:var(--danger);font-size:18px">&#10007;</div>' +
        '<div><strong style="color:var(--danger)">Ошибка поиска</strong></div>' +
        '</div>' +
        '<div style="font-size:13px;color:var(--text-secondary);margin-top:8px;line-height:1.4">' + escapeHtml(errMsg) + '</div>';
    }
  }

  function renderOwnSuppliers() {
    var container = $('own-suppliers-container');
    var ownSuppliers = currentSuppliers.filter(function (s) { return s.source === 'manual'; });
    if (!ownSuppliers.length) {
      container.innerHTML = '<div class="info-block"><span class="material-symbols-outlined">person_add</span><span>Добавьте своих поставщиков — им тоже будет направлен запрос ТКП</span></div>';
      return;
    }
    var html = '';
    for (var i = 0; i < ownSuppliers.length; i++) {
      var s = ownSuppliers[i];
      html += '<div class="own-supplier-item">' +
        '<div class="flex-between">' +
        '<div><div class="supplier-name">' + escapeHtml(s.company_name) + '</div>' +
        '<div class="supplier-email">' + escapeHtml(s.website_url || '') + '</div></div>' +
        '<span class="source-tag source-manual">Свой</span>' +
        '</div>' +
        (s.reason ? '<div style="font-size:12px;color:var(--text-secondary);margin-top:4px">' + escapeHtml(s.reason) + '</div>' : '') +
        '</div>';
    }
    container.innerHTML = html;
  }

  function renderSuppliers() {
    var container = $('suppliers-container');
    var exportBtn = $('btn-export');
    if (!currentSuppliers.length) {
      container.innerHTML = '<div class="empty-state">Нажмите «Запустить поиск» для начала</div>';
      exportBtn.classList.add('hidden');
      return;
    }
    exportBtn.classList.remove('hidden');

    var html = '<table class="suppliers-table"><thead><tr>' +
      '<th>Поставщик</th><th>Сайт</th><th>Источник</th><th style="width:35%">Причина</th><th>Контакты</th></tr></thead><tbody>';
    for (var i = 0; i < currentSuppliers.length; i++) {
      var s = currentSuppliers[i];
      var website = s.website_url ? '<a href="' + escapeHtml(s.website_url) + '" target="_blank" rel="noopener" style="color:var(--accent)">' + escapeHtml(s.website_url) + '</a>' : '—';
      var sourceClass = s.source === 'manual' ? 'source-manual' : 'source-ai';
      var sourceLabel = s.source === 'manual' ? 'Свой' : 'AI-поиск';
      html += '<tr>' +
        '<td><div class="supplier-name">' + escapeHtml(s.company_name) + '</div></td>' +
        '<td>' + website + '</td>' +
        '<td><span class="source-tag ' + sourceClass + '">' + sourceLabel + '</span></td>' +
        '<td style="color:var(--text-secondary);font-size:13px">' + escapeHtml(s.reason || '') + '</td>' +
        '<td class="supplier-contacts" id="contacts-' + s.id + '"><span style="color:var(--text-secondary);font-size:12px">Загрузка…</span></td>' +
        '</tr>';
    }
    html += '</tbody></table>';
    container.innerHTML = html;

    // Auto-load contacts for all rows — they're the whole point of the page.
    for (var k = 0; k < currentSuppliers.length; k++) {
      loadContacts(currentSuppliers[k].id);
    }
  }

  function pluralSuppliers(n) {
    var mod10 = n % 10;
    var mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11) return 'поставщик';
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return 'поставщика';
    return 'поставщиков';
  }

  async function loadContacts(supplierId) {
    try {
      var contacts = await API.apiFetch('/suppliers/' + supplierId + '/contacts');
      var cell = $('contacts-' + supplierId);
      if (!contacts || !contacts.length) {
        cell.textContent = 'Нет контактов';
        return;
      }
      var html = '';
      for (var i = 0; i < contacts.length; i++) {
        var c = contacts[i];
        html += '<div style="font-size:12px">' +
          '<a href="mailto:' + escapeHtml(c.email) + '" style="color:var(--accent)">' + escapeHtml(c.email) + '</a>' +
          (c.source ? ' <span style="color:var(--text-secondary)">(' + escapeHtml(c.source) + ')</span>' : '') +
          '</div>';
      }
      cell.innerHTML = html;
    } catch (e) {
      showError('Ошибка загрузки контактов: ' + e.message);
    }
  }

  function initSupplierSearch() {
    $('btn-search-suppliers').addEventListener('click', async function () {
      if (!currentPurchase) { showError('Выберите закупку'); return; }
      try {
        this.disabled = true;
        var result = await API.apiFetch('/purchases/' + currentPurchase.id + '/suppliers/search', {
          method: 'POST',
          body: { terms_text: currentPurchase.terms_text || '', hints: [] },
        });
        renderSearchStatus(result);
        if (result.status === 'queued' || result.status === 'in_progress') {
          clearTimeout(searchPollingTimer);
          searchPollingTimer = setTimeout(function () {
            checkSearchStatus();
          }, 5000);
        }
      } catch (e) {
        showError('Ошибка запуска поиска: ' + e.message);
      } finally {
        this.disabled = false;
      }
    });

    $('btn-refresh-search').addEventListener('click', function () {
      checkSearchStatus();
      loadSuppliers();
    });

    $('btn-export').addEventListener('click', async function () {
      if (!currentPurchase) return;
      try {
        var token = Auth.getToken();
        var resp = await fetch(CONFIG.API_URL + '/purchases/' + currentPurchase.id + '/suppliers/export', {
          headers: { 'Authorization': 'Bearer ' + token },
        });
        if (!resp.ok) throw new Error('Ошибка экспорта');
        var blob = await resp.blob();
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url;
        a.download = 'suppliers_' + currentPurchase.id + '.xlsx';
        a.click();
        URL.revokeObjectURL(url);
        showMessage('Файл скачан');
      } catch (e) {
        showError('Ошибка экспорта: ' + e.message);
      }
    });
  }

  // ── Add supplier manually ──────────────────────────────────────────

  function initAddSupplier() {
    $('btn-add-supplier').addEventListener('click', function () {
      $('form-add-supplier').reset();
      openModal('modal-add-supplier');
    });

    $('form-add-supplier').addEventListener('submit', async function (e) {
      e.preventDefault();
      if (!currentPurchase) return;
      var company = $('inp-supplier-company').value.trim();
      if (!company) { showError('Введите название компании'); return; }

      try {
        var supplier = await API.apiFetch('/purchases/' + currentPurchase.id + '/suppliers', {
          method: 'POST',
          body: {
            company_name: company,
            website_url: $('inp-supplier-website').value.trim() || null,
            reason: $('inp-supplier-reason').value.trim() || null,
          },
        });

        // Add contact if email provided
        var email = $('inp-supplier-email').value.trim();
        if (email) {
          await API.apiFetch('/suppliers/' + supplier.id + '/contacts', {
            method: 'POST',
            body: { email: email, source: 'manual' },
          });
        }

        showMessage('Поставщик добавлен');
        closeModal('modal-add-supplier');
        this.reset();
        loadSuppliers();
      } catch (e) {
        showError('Ошибка добавления поставщика: ' + e.message);
      }
    });
  }

  // ── Email draft ────────────────────────────────────────────────────

  function initEmailDraft() {
    $('btn-generate-email').addEventListener('click', async function () {
      if (!currentPurchase) { showError('Выберите закупку'); return; }
      try {
        this.disabled = true;
        $('email-draft').innerHTML = '<div class="empty-state">Генерация письма...</div>';
        var result = await API.apiFetch('/purchases/' + currentPurchase.id + '/email-draft', {
          method: 'POST',
        });
        var html = '<div class="letter-preview">' + escapeHtml(result.body) + '</div>' +
          '<div style="margin-top:12px;display:flex;gap:8px">' +
          '<button type="button" class="btn btn-sm btn-secondary" id="btn-copy-subject">Копировать тему</button>' +
          '<button type="button" class="btn btn-sm btn-secondary" id="btn-copy-body">Копировать текст</button>' +
          '</div>';
        $('email-draft').innerHTML = html;
        $('btn-copy-subject').addEventListener('click', function () { copyText(result.subject); });
        $('btn-copy-body').addEventListener('click', function () { copyText(result.body); });
      } catch (e) {
        showError('Ошибка генерации письма: ' + e.message);
        $('email-draft').innerHTML = '<div class="empty-state">Не удалось сгенерировать письмо</div>';
      } finally {
        this.disabled = false;
      }
    });
  }

  // ── Bids ───────────────────────────────────────────────────────────

  async function loadBids() {
    if (!currentPurchase) return;
    try {
      currentBids = await API.apiFetch('/purchases/' + currentPurchase.id + '/bids');
      renderBids();
      renderBidSelector();
      renderCorrespondenceSuppliers();
      populateBidSupplierDropdown();
      // Update badge
      var badge = $('badge-correspondence');
      if (currentBids.length > 0) {
        badge.textContent = currentBids.length;
        badge.classList.remove('hidden');
      } else {
        badge.classList.add('hidden');
      }
    } catch (e) {
      showError('Ошибка загрузки КП: ' + e.message);
    }
  }

  function renderBids() {
    var container = $('bids-container');
    var html = '<div class="proposals-grid">';
    for (var i = 0; i < currentBids.length; i++) {
      var bid = currentBids[i];
      var lotCount = bid.lots ? bid.lots.length : 0;
      html += '<div class="proposal-card">' +
        '<div class="proposal-supplier">' + escapeHtml(bid.supplier_name || 'Поставщик') + '</div>' +
        '<div class="proposal-date">' + (bid.supplier_contact ? escapeHtml(bid.supplier_contact) + ' &middot; ' : '') + formatDate(bid.created_at) + '</div>' +
        '<div class="proposal-items">' + lotCount + ' позици' + (lotCount === 1 ? 'я' : lotCount < 5 ? 'и' : 'й') + '</div>' +
        '</div>';
    }
    html += '<div class="proposal-add" id="bid-upload-zone-inner"><div class="plus">+</div><div>Загрузить КП</div><div style="font-size:11px">pdf, xlsx, doc, docx</div></div>';
    html += '</div>';
    container.innerHTML = html;
    // Bind upload zone click
    var uploadZone = $('bid-upload-zone-inner');
    if (uploadZone) {
      uploadZone.addEventListener('click', function () {
        $('form-add-bid').reset();
        $('bid-file-label').textContent = 'Нажмите для загрузки';
        populateBidSupplierDropdown();
        openModal('modal-add-bid');
      });
    }
    // Update comparison zones
    updateComparisonZones();
  }

  function renderCorrespondenceSuppliers() {
    var container = $('correspondence-suppliers');
    if (!currentSuppliers.length) {
      container.innerHTML = '<div class="empty-state" style="padding:20px;font-size:13px;">Сначала найдите поставщиков во вкладке «Поиск»</div>';
      return;
    }

    // Collapse to first 3 since M2 is still in development — full list is noise.
    var CORRESPONDENCE_COLLAPSE_THRESHOLD = 3;
    var shouldCollapse = currentSuppliers.length > CORRESPONDENCE_COLLAPSE_THRESHOLD && !suppliersExpanded;
    var visibleCount = shouldCollapse ? CORRESPONDENCE_COLLAPSE_THRESHOLD : currentSuppliers.length;

    var html = '';
    for (var i = 0; i < visibleCount; i++) {
      var s = currentSuppliers[i];
      var hasBid = currentBids.some(function (b) { return b.supplier_id === s.id; });
      var pillClass = hasBid ? 'pill-success' : 'pill-draft';
      var pillText = hasBid ? '&#10003; КП получено' : '&#9993; Ожидание КП';
      html += '<div class="supplier-card">' +
        '<div class="supplier-card-name">' + escapeHtml(s.company_name || s.website_url || 'Поставщик') + '</div>' +
        '<div class="supplier-card-status"><span class="status-pill ' + pillClass + '">' + pillText + '</span></div>' +
        '</div>';
    }

    if (currentSuppliers.length > CORRESPONDENCE_COLLAPSE_THRESHOLD) {
      if (shouldCollapse) {
        var hidden = currentSuppliers.length - CORRESPONDENCE_COLLAPSE_THRESHOLD;
        html += '<div class="lot-toggle" id="correspondence-toggle-row" style="text-align:center;padding:10px;cursor:pointer;color:var(--accent);font-weight:500;border-top:1px solid var(--border);margin-top:4px">' +
          'Показать ещё ' + hidden + ' ' + pluralSuppliers(hidden) + ' ▼' +
          '</div>';
      } else {
        html += '<div class="lot-toggle" id="correspondence-toggle-row" style="text-align:center;padding:10px;cursor:pointer;color:var(--accent);font-weight:500;border-top:1px solid var(--border);margin-top:4px">' +
          'Свернуть ▲' +
          '</div>';
      }
    }

    container.innerHTML = html;

    var toggleRow = $('correspondence-toggle-row');
    if (toggleRow) {
      toggleRow.addEventListener('click', function () {
        suppliersExpanded = !suppliersExpanded;
        renderCorrespondenceSuppliers();
      });
    }
  }

  function updateComparisonZones() {
    // ТЗ zone
    var tzHint = $('comparison-tz-hint');
    if (tzHint) {
      if (currentPurchase && currentPurchase.terms_text) {
        tzHint.textContent = 'ТЗ загружено';
        tzHint.style.color = 'var(--success)';
        tzHint.style.fontWeight = '500';
      } else {
        tzHint.textContent = 'ТЗ не загружено';
        tzHint.style.color = '';
        tzHint.style.fontWeight = '';
      }
    }
    // КП zone
    var kpHint = $('comparison-kp-hint');
    if (kpHint) {
      if (currentBids.length > 0) {
        var names = currentBids.map(function (b) { return b.supplier_name || 'Поставщик'; }).join(', ');
        kpHint.textContent = 'Загружено: ' + currentBids.length + ' файл (' + names + ')';
      } else {
        kpHint.textContent = 'КП не загружены';
      }
    }
  }

  function renderBidSelector() {
    var container = $('comparison-bid-selector');
    if (!currentBids.length) {
      container.innerHTML = '<div class="empty-state" style="padding:16px;font-size:13px;width:100%;text-align:center;">Загрузите КП для сравнения</div>';
      $('btn-compare').disabled = true;
      return;
    }
    var html = '';
    for (var i = 0; i < currentBids.length; i++) {
      var bid = currentBids[i];
      var lotCount = bid.lots ? bid.lots.length : 0;
      html += '<div class="comp-supplier-tab" data-bid-id="' + bid.id + '">' +
        '<span class="comp-supplier-indicator comp-supplier-indicator--pending"></span>' +
        '<div>' +
        '<div class="comp-supplier-tab-name">' + escapeHtml(bid.supplier_name || 'Поставщик') + '</div>' +
        '<div class="comp-supplier-tab-meta">' + lotCount + ' позиций</div>' +
        '</div></div>';
    }
    container.innerHTML = html;
    $('btn-compare').disabled = false;
    updateComparisonZones();
  }

  function populateBidSupplierDropdown() {
    var select = $('inp-bid-supplier-select');
    var options = '<option value="">-- Выберите поставщика --</option>';
    for (var i = 0; i < currentSuppliers.length; i++) {
      var s = currentSuppliers[i];
      options += '<option value="' + s.id + '">' + escapeHtml(s.company_name) + '</option>';
    }
    select.innerHTML = options;
  }

  function initAddBid() {
    $('btn-add-bid').addEventListener('click', function () {
      $('form-add-bid').reset();
      $('bid-file-label').textContent = 'Нажмите для загрузки';
      populateBidSupplierDropdown();
      openModal('modal-add-bid');
    });

    // File upload zone
    $('bid-file-upload').addEventListener('click', function () {
      $('inp-bid-file').click();
    });
    $('inp-bid-file').addEventListener('change', function () {
      var file = this.files[0];
      if (file) {
        $('bid-file-label').textContent = file.name;
      }
    });

    $('form-add-bid').addEventListener('submit', async function (e) {
      e.preventDefault();
      if (!currentPurchase) return;

      var supplierSelect = $('inp-bid-supplier-select');
      var supplierId = supplierSelect.value ? parseInt(supplierSelect.value, 10) : null;
      var supplierName = $('inp-bid-supplier-name').value.trim();
      var supplierContact = $('inp-bid-supplier-contact').value.trim();
      var bidText = $('inp-bid-text').value.trim();
      var fileInput = $('inp-bid-file');
      var file = fileInput.files[0];

      if (!supplierName && !supplierId) {
        showError('Выберите или введите поставщика');
        return;
      }

      // If supplier selected from dropdown, get name
      if (supplierId && !supplierName) {
        var found = currentSuppliers.find(function (s) { return s.id === supplierId; });
        if (found) supplierName = found.company_name;
      }

      try {
        // Convert file if uploaded
        if (file) {
          showMessage('Конвертация документа...');
          var converted = await API.convertTechTaskFile(file, currentPurchase ? currentPurchase.id : null);
          if (converted && converted.markdown) {
            bidText = bidText ? bidText + '\n\n' + converted.markdown : converted.markdown;
          }
        }

        if (!bidText) { showError('Введите текст КП или загрузите файл'); return; }

        var body = {
          bid_text: bidText,
          supplier_name: supplierName,
          supplier_contact: supplierContact || null,
        };
        if (supplierId) body.supplier_id = supplierId;

        await API.apiFetch('/purchases/' + currentPurchase.id + '/bids', {
          method: 'POST',
          body: body,
        });
        if (file) trackFile(currentPurchase.id, file.name, 'kp');
        showMessage('КП загружено');
        closeModal('modal-add-bid');
        this.reset();
        loadBids();
      } catch (e) {
        showError('Ошибка загрузки КП: ' + e.message);
      }
    });
  }

  // ── Comparison ─────────────────────────────────────────────────────

  function initComparison() {
    $('btn-compare').addEventListener('click', async function () {
      if (!currentPurchase) { showError('Сначала выберите закупку'); return; }
      if (!currentBids.length) { showError('Сначала загрузите КП'); return; }
      try {
        this.disabled = true;
        $('comparison-results').innerHTML = '';
        var stageNames = ['Загрузка данных', 'Эмбеддинги лотов', 'Сопоставление лотов (LLM)', 'Сопоставление характеристик', 'Формирование результата'];
        _comparisonStartTime = Date.now();
        _comparisonBidQueue = currentBids.map(function (b, idx) {
          var stages = stageNames.map(function (name) { return {name: name, status: 'pending', detail: ''}; });
          return { bid_id: b.id, name: b.supplier_name || 'Поставщик',
                   status: 'queued', stages: stages,
                   startTime: null, elapsed: null, expanded: false };
        });
        _startComparisonTimer();
        _renderComparisonAllProgress();
        // Fire all POSTs in parallel
        var postPromises = currentBids.map(function (b) {
          return API.apiFetch('/purchases/' + currentPurchase.id + '/bids/' + b.id + '/comparison', { method: 'POST' });
        });
        await Promise.all(postPromises);
        pollComparisonAll();
      } catch (e) {
        showError('Ошибка запуска сравнения: ' + e.message);
        this.disabled = false;
        $('comparison-progress').innerHTML = '';
      }
    });

    // ── Standalone TZ upload on Comparison tab ──
    $('comparison-tz-zone').addEventListener('click', function () {
      $('inp-comparison-tz-file').click();
    });

    $('inp-comparison-tz-file').addEventListener('change', async function () {
      var file = this.files[0];
      if (!file) return;
      if (!currentPurchase) {
        showError('Сначала выберите или создайте закупку');
        this.value = '';
        return;
      }
      try {
        showMessage('Конвертация ТЗ...');
        var converted = await API.convertTechTaskFile(file, currentPurchase ? currentPurchase.id : null);
        if (converted && converted.markdown) {
          var existingTerms = currentPurchase.terms_text || '';
          var newTerms = existingTerms ? existingTerms + '\n\n' + converted.markdown : converted.markdown;
          await API.apiFetch('/purchases/' + currentPurchase.id, {
            method: 'PATCH',
            body: { terms_text: newTerms },
          });
          currentPurchase.terms_text = newTerms;
          trackFile(currentPurchase.id, file.name, 'tz');
          showMessage('ТЗ загружено');
          updateComparisonZones();
          loadLots();
        }
      } catch (e) {
        showError('Ошибка загрузки ТЗ: ' + e.message);
      }
      this.value = '';
    });

    // ── Standalone KP upload on Comparison tab (multi-file) ──
    $('comparison-kp-zone').addEventListener('click', function () {
      $('inp-comparison-kp-file').click();
    });

    $('inp-comparison-kp-file').addEventListener('change', async function () {
      var files = Array.from(this.files || []);
      if (!files.length) return;
      if (!currentPurchase) {
        showError('Сначала выберите или создайте закупку');
        this.value = '';
        return;
      }
      var statusEl = $('comparison-upload-status');
      for (var fi = 0; fi < files.length; fi++) {
        var file = files[fi];
        var label = file.name + ' (' + (fi + 1) + '/' + files.length + ')';
        if (statusEl) statusEl.innerHTML = '<div class="upload-inline-status"><div class="spinner" style="width:14px;height:14px"></div> Конвертация: ' + escapeHtml(label) + '</div>';
        try {
          var converted = await API.convertTechTaskFile(file, currentPurchase ? currentPurchase.id : null);
          if (converted && converted.markdown) {
            if (statusEl) statusEl.innerHTML = '<div class="upload-inline-status"><div class="spinner" style="width:14px;height:14px"></div> Сохранение: ' + escapeHtml(label) + '</div>';
            var supplierName = file.name.replace(/\.[^.]+$/, '');
            await API.apiFetch('/purchases/' + currentPurchase.id + '/bids', {
              method: 'POST',
              body: {
                bid_text: converted.markdown,
                supplier_name: supplierName,
              },
            });
            trackFile(currentPurchase.id, file.name, 'kp');
          }
        } catch (e) {
          if (statusEl) statusEl.innerHTML = '<div class="upload-inline-status" style="color:var(--danger)">Ошибка: ' + escapeHtml(file.name) + ' — ' + escapeHtml(e.message) + '</div>';
          continue;
        }
      }
      if (statusEl) statusEl.innerHTML = '<div class="upload-inline-status" style="color:var(--success)">Загружено ' + files.length + ' файл(ов)</div>';
      setTimeout(function () { if (statusEl) statusEl.innerHTML = ''; }, 5000);
      loadBids();
      this.value = '';
    });

    // ── Diagnostics ──
    initComparisonDiag();
  }

  var _comparisonBidQueue = [];
  var _comparisonStartTime = null;
  var _comparisonTimerInterval = null;

  function _fmtElapsed(ms) {
    var s = Math.floor(ms / 1000);
    var m = Math.floor(s / 60);
    s = s % 60;
    return (m > 0 ? m + ':' : '') + (s < 10 && m > 0 ? '0' : '') + s + ' сек';
  }

  function _startComparisonTimer() {
    _stopComparisonTimer();
    _comparisonTimerInterval = setInterval(function () { _renderComparisonAllProgress(); }, 1000);
  }

  function _stopComparisonTimer() {
    if (_comparisonTimerInterval) { clearInterval(_comparisonTimerInterval); _comparisonTimerInterval = null; }
  }

  var _comparisonPollActive = false;

  async function pollComparisonAll() {
    if (_comparisonPollActive) return; // prevent concurrent polls
    if (!currentPurchase || !_comparisonBidQueue.length) return;
    _comparisonPollActive = true;
    var allDone = true;
    try {
      for (var i = 0; i < _comparisonBidQueue.length; i++) {
        var entry = _comparisonBidQueue[i];
        if (entry.status === 'done' || entry.status === 'failed') continue;
        try {
          var result = await API.apiFetch('/purchases/' + currentPurchase.id + '/bids/' + entry.bid_id + '/comparison');
          console.log('[M3 poll] bid=' + entry.bid_id + ' status=' + result.status);
          if (result.status === 'queued') {
            entry.status = 'queued';
            allDone = false;
          } else if (result.status === 'in_progress') {
            if (entry.status !== 'in_progress') { entry.startTime = Date.now(); entry.expanded = true; }
            entry.status = 'in_progress';
            if (result.stages && result.stages.length) entry.stages = result.stages;
            allDone = false;
          } else if (result.status === 'done' || result.status === 'completed') {
            if (!entry.startTime) entry.startTime = Date.now();
            entry.status = 'done';
            entry.elapsed = Date.now() - entry.startTime;
            entry.expanded = false;
            if (result.stages && result.stages.length) entry.stages = result.stages;
            entry.rows = result.rows || [];
          } else {
            entry.status = 'failed';
            entry.note = result.note || result.status;
          }
        } catch (e) {
          console.error('[M3 poll] bid=' + entry.bid_id + ' error:', e);
          entry.status = 'failed';
          entry.note = e.message;
        }
      }
    } finally {
      _comparisonPollActive = false;
    }
    _renderComparisonAllProgress();
    if (allDone) {
      _stopComparisonTimer();
      $('btn-compare').disabled = false;
      setTimeout(function () { $('comparison-progress').innerHTML = ''; }, 3000);
      _renderComparisonAllResults();
    } else {
      comparisonPollingTimer = setTimeout(pollComparisonAll, 1500);
    }
  }

  function _renderComparisonAllProgress() {
    var el = $('comparison-progress');
    if (!el) return;
    var now = Date.now();
    var totalElapsed = _comparisonStartTime ? _fmtElapsed(now - _comparisonStartTime) : '';

    // Read current expanded states from DOM before re-render
    var domSections = el.querySelectorAll('[data-comp-bid-stages]');
    for (var ds = 0; ds < domSections.length; ds++) {
      var bidIdx = parseInt(domSections[ds].getAttribute('data-comp-bid-stages'), 10);
      if (_comparisonBidQueue[bidIdx]) {
        _comparisonBidQueue[bidIdx].expanded = domSections[ds].style.display !== 'none';
      }
    }

    var html = '<div class="card" style="margin-bottom:12px"><div class="card-body">';
    html += '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">';
    html += '<span style="font-weight:600">Сравнение КП с ТЗ</span>';
    if (totalElapsed) html += '<span style="font-size:12px;color:var(--text-secondary);font-variant-numeric:tabular-nums">' + totalElapsed + '</span>';
    html += '</div>';

    for (var b = 0; b < _comparisonBidQueue.length; b++) {
      var entry = _comparisonBidQueue[b];
      var isActive = entry.status === 'in_progress';
      var isDone = entry.status === 'done';
      var isQueued = entry.status === 'queued' || entry.status === 'pending';
      var isFailed = entry.status === 'failed';
      // First non-done/non-failed bid is "next" — show it as waiting with spinner
      var isNext = false;
      if (isQueued) {
        var hasActiveAbove = false;
        for (var p = 0; p < b; p++) {
          if (_comparisonBidQueue[p].status === 'in_progress' || _comparisonBidQueue[p].status === 'queued' || _comparisonBidQueue[p].status === 'pending') { hasActiveAbove = true; break; }
        }
        if (!hasActiveAbove) isNext = true;
      }

      var bidIcon = '&#9675;';
      if (isDone) bidIcon = '<span style="color:var(--success)">&#10003;</span>';
      else if (isActive || isNext) bidIcon = '<div class="spinner" style="width:14px;height:14px;display:inline-block"></div>';
      else if (isFailed) bidIcon = '<span style="color:var(--danger)">&#10007;</span>';

      var bidTimer = '';
      if (isDone && entry.elapsed) bidTimer = _fmtElapsed(entry.elapsed);
      else if (isActive && entry.startTime) bidTimer = _fmtElapsed(now - entry.startTime);

      var expanded = entry.expanded || isNext;
      var arrowStyle = 'cursor:pointer;transition:transform .2s;' + (expanded ? '' : 'transform:rotate(-90deg);');

      html += '<div style="margin-bottom:6px;border-bottom:1px solid var(--border);padding-bottom:6px">';
      html += '<div style="display:flex;align-items:center;gap:8px;padding:4px 0;cursor:pointer" onclick="var s=this.nextElementSibling;if(s){var show=s.style.display===\'none\';s.style.display=show?\'block\':\'none\';this.querySelector(\'.comp-prog-arrow\').style.transform=show?\'\':\' rotate(-90deg)\'}">';
      html += '<span class="comp-prog-arrow" style="font-size:10px;color:var(--text-secondary);' + arrowStyle + '">&#9660;</span>';
      html += '<span style="width:20px;text-align:center">' + bidIcon + '</span>';
      html += '<span style="font-weight:500;color:' + ((isActive || isNext) ? 'var(--success)' : isDone ? 'var(--text)' : isFailed ? 'var(--danger)' : 'var(--text-secondary)') + '">' + escapeHtml(entry.name) + '</span>';
      if (isQueued && !isNext) html += '<span style="font-size:12px;color:var(--text-secondary);margin-left:4px">в очереди</span>';
      else if (isNext) html += '<span style="font-size:12px;color:var(--text-secondary);margin-left:4px">ожидание воркера...</span>';
      if (bidTimer) html += '<span style="font-size:12px;color:var(--text-secondary);margin-left:auto;font-variant-numeric:tabular-nums">' + bidTimer + '</span>';
      html += '</div>';

      if (entry.stages && (!isQueued || isNext)) {
        html += '<div data-comp-bid-stages="' + b + '" style="' + (expanded ? '' : 'display:none') + '">';
        for (var si = 0; si < entry.stages.length; si++) {
          var s = entry.stages[si];
          // For "next" bid waiting for worker, show first stage as in_progress
          if (isNext && si === 0 && s.status === 'pending') s = {name: s.name, status: 'in_progress', detail: 'ожидание'};
          var sIcon = '&#9675;', sColor = 'var(--text-secondary)';
          if (s.status === 'done') { sIcon = '<span style="color:var(--success)">&#10003;</span>'; sColor = 'var(--text)'; }
          else if (s.status === 'in_progress') { sIcon = '<div class="spinner" style="width:12px;height:12px;display:inline-block;border-color:var(--success);border-top-color:transparent"></div>'; sColor = 'var(--success)'; }
          html += '<div style="display:flex;align-items:center;gap:8px;padding:2px 0 2px 36px;color:' + sColor + ';font-size:13px">';
          html += '<span style="width:16px;text-align:center">' + sIcon + '</span>';
          html += '<span>' + escapeHtml(s.name) + '</span>';
          if (s.detail) html += '<span style="color:var(--text-secondary);font-size:12px;margin-left:auto">' + escapeHtml(s.detail) + '</span>';
          html += '</div>';
        }
        html += '</div>';
      }
      html += '</div>';
    }
    html += '</div></div>';
    el.innerHTML = html;
  }

  function _renderComparisonAllResults() {
    var completed = _comparisonBidQueue.filter(function (e) { return e.status === 'done' && e.rows && e.rows.length; });
    if (!completed.length) {
      $('comparison-results').innerHTML = '<div class="card"><div class="card-body"><div class="empty-state">Нет данных для сравнения</div></div></div>';
      return;
    }

    // Build tabs with summary for each supplier
    var html = '<div class="comp-suppliers-bar" style="margin-bottom:12px"><div class="comp-suppliers-label">Результаты по поставщикам:</div><div class="comp-suppliers-list">';
    for (var t = 0; t < completed.length; t++) {
      var active = t === 0 ? ' active' : '';
      // Count matched lots
      var totalLots = completed[t].rows.length;
      var okLots = 0;
      for (var rl = 0; rl < completed[t].rows.length; rl++) {
        var chars = completed[t].rows[rl].characteristic_rows || [];
        // Lot is OK if no TZ-relevant issues (unmatched_kp is fine)
        var hasProblems = chars.some(function (c) { return c.status === 'mismatch' || c.status === 'unmatched_tz' || c.status === 'partial'; });
        if (!hasProblems) okLots++;
      }
      var indicatorClass = okLots === totalLots ? 'comp-supplier-indicator--ok' : okLots === 0 ? 'comp-supplier-indicator--fail' : 'comp-supplier-indicator--warn';
      var metaText = okLots + ' из ' + totalLots + ' соответств.';

      html += '<div class="comp-supplier-tab' + active + '" data-comp-tab="' + t + '" onclick="window._switchCompTab(' + t + ')">';
      html += '<span class="comp-supplier-indicator ' + indicatorClass + '"></span>';
      html += '<div><div class="comp-supplier-tab-name">' + escapeHtml(completed[t].name) + '</div>';
      html += '<div class="comp-supplier-tab-meta">' + metaText + '</div></div></div>';
    }
    html += '</div></div>';

    for (var c = 0; c < completed.length; c++) {
      html += '<div class="comp-tab-content' + (c === 0 ? ' active' : '') + '" data-comp-pane="' + c + '">';
      html += _renderComparisonRows(completed[c].rows, completed[c].name);
      html += '</div>';
    }
    $('comparison-results').innerHTML = html;
    selectedBidId = completed[0].bid_id;
  }

  window._switchCompTab = function (idx) {
    var tabs = document.querySelectorAll('.comp-supplier-tab[data-comp-tab]');
    var panes = document.querySelectorAll('.comp-tab-content[data-comp-pane]');
    for (var t = 0; t < tabs.length; t++) tabs[t].classList.toggle('active', t === idx);
    for (var p = 0; p < panes.length; p++) panes[p].classList.toggle('active', p === idx);
  };

  function renderComparison(rows) {
    if (!rows.length) {
      $('comparison-results').innerHTML = '<div class="card"><div class="card-body"><div class="empty-state">Нет данных для сравнения</div></div></div>';
      return;
    }
    $('comparison-results').innerHTML = _renderComparisonRows(rows, null);
  }

  function _splitParamText(text) {
    // Split "Вес: 1.9 кг" → {name: "Вес", value: "1.9 кг"}
    // or "Вес = 1.9 кг" or "Вес - 1.9 кг"
    if (!text) return {name: '', value: ''};
    var m = text.match(/^([^:=–—-]+?)\s*[:=–—-]\s*(.+)$/);
    if (m) return {name: m[1].trim(), value: m[2].trim()};
    return {name: text, value: ''};
  }

  function _renderComparisonRows(rows, supplierName) {
    var totalLots = rows.length;
    var warnLots = 0, warnRows = [];
    for (var li = 0; li < rows.length; li++) {
      var crs = rows[li].characteristic_rows || [];
      // Only TZ-relevant issues count as discrepancies
      if (crs.some(function (c) { return c.status === 'mismatch' || c.status === 'unmatched_tz' || c.status === 'partial'; })) { warnLots++; warnRows.push(rows[li]); }
    }
    var summaryClass = warnLots === 0 ? 'status-active' : 'status-warning';
    var summaryText = warnLots === 0 ? 'Все лоты совпадают' : warnLots + ' из ' + totalLots + ' лотов с расхождениями';
    var uid = 'cst-' + Date.now() + '-' + Math.random().toString(36).substr(2, 4);

    var html = '<div class="card"><div class="card-header"><h3>Результаты сравнения</h3>';
    html += '<span class="status ' + summaryClass + '"><span class="status-dot"></span> ' + summaryText + '</span></div>';
    html += '<div class="card-body" style="padding:0">';

    // Sub-tabs: Расхождения / Все лоты
    html += '<div class="comp-subtabs">';
    html += '<div class="comp-subtab' + (warnLots > 0 ? ' active' : '') + '" data-cst="' + uid + '" data-cst-target="warn" onclick="window._switchCst(this)">';
    html += '<span class="comp-subtab-dot comp-subtab-dot--warn"></span> Расхождения ';
    html += '<span class="comp-subtab-count">' + warnLots + '</span></div>';
    html += '<div class="comp-subtab' + (warnLots === 0 ? ' active' : '') + '" data-cst="' + uid + '" data-cst-target="all" onclick="window._switchCst(this)">';
    html += 'Все лоты <span class="comp-subtab-count comp-subtab-count--muted">' + totalLots + '</span></div>';
    html += '</div>';

    // Pane: Расхождения
    html += '<div class="comp-subtab-pane' + (warnLots > 0 ? ' active' : '') + '" data-cst-group="' + uid + '" data-cst-id="warn">';
    if (warnRows.length) {
      html += _renderCompLots(warnRows, supplierName);
    } else {
      html += '<div style="padding:24px;text-align:center;color:var(--success);font-weight:500">Расхождений нет</div>';
    }
    html += '</div>';

    // Pane: Все лоты
    html += '<div class="comp-subtab-pane' + (warnLots === 0 ? ' active' : '') + '" data-cst-group="' + uid + '" data-cst-id="all">';
    html += _renderCompLots(rows, supplierName);
    html += '</div>';

    html += '</div></div>';
    return html;
  }

  window._switchCst = function (el) {
    var uid = el.getAttribute('data-cst');
    var target = el.getAttribute('data-cst-target');
    var tabs = document.querySelectorAll('.comp-subtab[data-cst="' + uid + '"]');
    var panes = document.querySelectorAll('.comp-subtab-pane[data-cst-group="' + uid + '"]');
    for (var t = 0; t < tabs.length; t++) tabs[t].classList.toggle('active', tabs[t].getAttribute('data-cst-target') === target);
    for (var p = 0; p < panes.length; p++) panes[p].classList.toggle('active', panes[p].getAttribute('data-cst-id') === target);
  };

  function _renderCompLots(rows, supplierName) {
    var html = '';
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      // Count only TZ-relevant discrepancies (mismatch + unmatched_tz + partial)
      // unmatched_kp = extra KP params, NOT a problem
      var tzTotal = 0, tzOk = 0, tzProblems = 0;
      if (row.characteristic_rows) {
        for (var m = 0; m < row.characteristic_rows.length; m++) {
          var st = row.characteristic_rows[m].status;
          if (st === 'unmatched_kp') continue; // not a TZ requirement
          tzTotal++;
          if (st === 'matched') tzOk++;
          else tzProblems++; // mismatch, unmatched_tz, partial
        }
      }
      var lotIndicator = tzProblems > 0 ? 'comp-lot-indicator--warn' : 'comp-lot-indicator--ok';
      var lotBadgeClass = tzProblems > 0 ? 'comp-lot-badge--warn' : 'comp-lot-badge--ok';
      var lotBadgeText = tzProblems > 0 ? tzProblems + ' расхождени' + (tzProblems === 1 ? 'е' : tzProblems < 5 ? 'я' : 'й') : 'Соответствует ТЗ';

      html += '<div class="comp-lot" data-expanded="false">' +
        '<div class="comp-lot-header" onclick="this.parentElement.dataset.expanded = this.parentElement.dataset.expanded === \'true\' ? \'false\' : \'true\'; var body = this.nextElementSibling; body.style.display = this.parentElement.dataset.expanded === \'true\' ? \'block\' : \'none\'; this.querySelector(\'.comp-lot-arrow\').style.transform = this.parentElement.dataset.expanded === \'true\' ? \'\' : \'rotate(-90deg)\'">' +
        '<div class="comp-lot-header-left">' +
        '<span class="comp-lot-arrow" style="transform:rotate(-90deg)">&#9660;</span>' +
        '<span class="comp-lot-indicator ' + lotIndicator + '"></span>' +
        '<span class="comp-lot-title">' + escapeHtml(row.lot_name) + '</span>' +
        '</div>' +
        '<div class="comp-lot-header-right">' +
        '<span class="comp-lot-stat">' + tzOk + ' из ' + tzTotal + ' по ТЗ</span>' +
        '<span class="comp-lot-badge ' + lotBadgeClass + '">' + lotBadgeText + '</span>' +
        '</div></div>';

      if (row.characteristic_rows && row.characteristic_rows.length) {
        var kpHeader = supplierName ? escapeHtml(supplierName) : 'Предложение КП';
        html += '<div class="comp-lot-body" style="display:none"><table class="comparison-table">' +
          '<thead><tr><th>Характеристика</th><th>Требование ТЗ</th><th>' + kpHeader + '</th></tr></thead><tbody>';
        for (var j = 0; j < row.characteristic_rows.length; j++) {
          var cr = row.characteristic_rows[j];
          var statusClass, statusIcon;
          if (cr.status === 'matched') { statusClass = 'match'; statusIcon = '&#10003;'; }
          else if (cr.status === 'mismatch') { statusClass = 'mismatch'; statusIcon = '&#10007;'; }
          else if (cr.status === 'unmatched_tz') { statusClass = 'mismatch'; statusIcon = '&#10007;'; }
          else if (cr.status === 'partial') { statusClass = 'partial'; statusIcon = '&#9888;'; }
          else if (cr.status === 'unmatched_kp') { statusClass = 'extra'; statusIcon = '&#43;'; }
          else { statusClass = 'match'; statusIcon = '&#10003;'; }
          var lp = _splitParamText(cr.left_text);
          var rp = _splitParamText(cr.right_text);
          var paramName = lp.name || rp.name || '';
          var tzVal = lp.value || lp.name || '';
          var kpVal = cr.right_text ? (rp.value || rp.name || '') : '—';
          if (!lp.value && lp.name) { paramName = ''; tzVal = lp.name; }
          html += '<tr class="comp-row-' + cr.status + '"><td>' + escapeHtml(paramName) + '</td><td>' + escapeHtml(tzVal) + '</td>' +
            '<td class="' + statusClass + '"><span class="check-icon">' + statusIcon + '</span> ' + escapeHtml(kpVal) + '</td></tr>';
        }
        html += '</tbody></table></div>';
      }
      html += '</div>';
    }
    return html;
  }

  // ── Comparison Diagnostics ──────────────────────────────────────────

  var _comparisonDiagData = null;

  function initComparisonDiag() {
    var btn = $('btn-comparison-diag');
    var user = (typeof Auth !== 'undefined' && Auth.getUser) ? Auth.getUser() : null;
    var isAdmin = !!(user && user.is_admin);
    if (btn && !isAdmin) { btn.style.display = 'none'; return; }
    if (btn) btn.addEventListener('click', function () {
      openModal('modal-comparison-diag');
      loadComparisonDiagnostics();
    });
    var refreshBtn = $('btn-comparison-diag-refresh');
    if (refreshBtn) refreshBtn.addEventListener('click', loadComparisonDiagnostics);
    var copyBtn = $('btn-comparison-diag-copy');
    if (copyBtn) copyBtn.addEventListener('click', function () {
      if (_comparisonDiagData) {
        navigator.clipboard.writeText(JSON.stringify(_comparisonDiagData, null, 2))
          .then(function () { showMessage('JSON скопирован'); })
          .catch(function () { showError('Не удалось скопировать'); });
      }
    });
  }

  function loadComparisonDiagnostics() {
    var el = $('comparison-diag-content');
    if (!el || !currentPurchase) return;
    el.textContent = 'Загрузка...';
    API.apiFetch('/purchases/' + currentPurchase.id + '/comparison/diagnostics')
      .then(function (data) {
        _comparisonDiagData = data;
        el.textContent = _formatComparisonDiag(data);
      })
      .catch(function (err) {
        el.textContent = 'Ошибка: ' + err.message;
      });
  }

  function _formatComparisonDiag(d) {
    var out = '';

    // Summary
    var taskCount = d.comparison_tasks ? d.comparison_tasks.length : 0;
    var doneCount = 0, failedCount = 0, queuedCount = 0;
    if (d.comparison_tasks) {
      for (var t = 0; t < d.comparison_tasks.length; t++) {
        if (d.comparison_tasks[t].status === 'completed') doneCount++;
        else if (d.comparison_tasks[t].status === 'failed') failedCount++;
        else queuedCount++;
      }
    }
    out += '╔══════════════════════════════════════════╗\n';
    out += '║           СВОДКА М3 СРАВНЕНИЕ            ║\n';
    out += '╠══════════════════════════════════════════╣\n';
    out += '║  ТЗ лотов:    ' + (d.tz ? d.tz.lot_count : 0) + '\n';
    out += '║  КП:          ' + (d.bids ? d.bids.length : 0) + '\n';
    out += '║  Задач:       ' + taskCount + ' (✓ ' + doneCount + '  ✗ ' + failedCount + '  ◌ ' + queuedCount + ')\n';
    out += '╚══════════════════════════════════════════╝\n';

    // TZ lots
    out += '\n=== ТЗ (лоты) ===\n';
    if (d.tz && d.tz.lots && d.tz.lots.length) {
      for (var l = 0; l < d.tz.lots.length; l++) {
        out += '  #' + d.tz.lots[l].id + ' ' + d.tz.lots[l].name + '\n';
      }
    } else {
      out += '  (нет лотов ТЗ)\n';
    }

    // Bids
    out += '\n=== КП (Bids) ===\n';
    if (d.bids && d.bids.length) {
      for (var i = 0; i < d.bids.length; i++) {
        var b = d.bids[i];
        out += '  #' + b.bid_id + ' ' + (b.supplier_name || '(без имени)') + ' → ' + b.lot_count + ' лотов (' + b.created_at + ')\n';
      }
    } else {
      out += '  (нет КП)\n';
    }

    // Comparison tasks
    out += '\n=== Задачи сравнения ===\n';
    if (d.comparison_tasks && d.comparison_tasks.length) {
      for (var j = 0; j < d.comparison_tasks.length; j++) {
        var ct = d.comparison_tasks[j];
        var statusIcon = ct.status === 'completed' ? '✓' : ct.status === 'failed' ? '✗' : ct.status === 'in_progress' ? '⟳' : '◌';
        out += '\n  --- task #' + ct.task_id + ' [' + statusIcon + ' ' + ct.status + '] bid=#' + ct.bid_id + ' ---\n';
        out += '  Результат: ' + ct.row_count + ' лотов';
        if (ct.note) out += ' | ' + ct.note;
        out += '\n';
        out += '  Создан: ' + ct.created_at + '\n';
        if (ct.updated_at && ct.updated_at !== ct.created_at) out += '  Обновлён: ' + ct.updated_at + '\n';
        if (ct.stages && ct.stages.length) {
          out += '  Этапы:\n';
          for (var k = 0; k < ct.stages.length; k++) {
            var s = ct.stages[k];
            var icon = s.status === 'done' ? '[OK]' : s.status === 'in_progress' ? '[..]' : '[  ]';
            out += '    ' + icon + ' ' + s.name + (s.detail ? ' → ' + s.detail : '') + '\n';
          }
        }
      }
    } else {
      out += '  (нет задач)\n';
    }

    out += '\n=== Raw JSON ===\n';
    out += JSON.stringify(d, null, 2);
    return out;
  }

  // ── National Regime ─────────────────────────────────────────────────

  var regimePollingTimer = null;
  var regimeTimerInterval = null;

  function initRegime() {
    var btnCheck = $('btn-regime-check');
    var btnRefresh = $('btn-regime-refresh');
    if (btnCheck) btnCheck.addEventListener('click', startRegimeCheck);
    if (btnRefresh) btnRefresh.addEventListener('click', loadRegimeCheck);
    initRegimeDiag();
  }

  var regimeStartTime = null;

  function startRegimeTimer() {
    if (!regimeStartTime) regimeStartTime = Date.now();
    clearInterval(regimeTimerInterval);
    regimeTimerInterval = setInterval(function () {
      var el = document.getElementById('regime-elapsed');
      if (el) el.textContent = formatElapsed(Date.now() - regimeStartTime);
    }, 1000);
  }

  function stopRegimeTimer() {
    clearInterval(regimeTimerInterval);
    regimeTimerInterval = null;
  }

  function startRegimeCheck() {
    if (!currentPurchase) return;
    regimeStartTime = Date.now();
    startRegimeTimer();
    // Show initial stages immediately — don't wait for poll
    renderRegimeProgress({
      status: 'processing', total: 0, processed: 0, message: '',
      filename: '',
      stages: [
        {name: 'Сбор позиций из КП', status: 'in_progress', detail: ''},
        {name: 'Проверка реестра ПП №719', status: 'pending', detail: ''},
        {name: 'Проверка баллов локализации', status: 'pending', detail: ''},
        {name: 'Сравнение характеристик (ГИСП)', status: 'pending', detail: ''},
        {name: 'Формирование отчёта PDF', status: 'pending', detail: ''},
      ],
    });
    API.apiFetch('/regime/purchases/' + currentPurchase.id + '/check', { method: 'POST' })
      .then(function (data) {
        if (data && (data.status === 'pending' || data.status === 'processing')) {
          // Immediately poll — don't wait
          _pollRegimeCheckNow();
        } else {
          renderRegimeResults(data);
        }
      })
      .catch(function (err) {
        stopRegimeTimer();
        regimeStartTime = null;
        showError(err.message);
      });
  }

  var _regimeBidsPollingTimer = null;

  function renderRegimeBids() {
    var container = $('regime-bids-list');
    if (!container) return;
    if (!currentBids || currentBids.length === 0) {
      container.innerHTML =
        '<div class="upload-zone" id="regime-kp-zone" style="padding:16px">' +
        '<div class="label">Загрузить КП для проверки</div>' +
        '<div class="hint" id="regime-kp-hint">pdf, xlsx, doc, docx — можно несколько файлов</div>' +
        '<input type="file" id="inp-regime-kp-file" accept=".pdf,.xlsx,.doc,.docx" multiple style="display:none">' +
        '</div>' +
        '<div id="regime-upload-status"></div>';
      _bindRegimeUpload();
      return;
    }
    var html = '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:4px">';
    html += '<span style="font-size:12px;font-weight:500;color:var(--text-secondary)">КП (' + currentBids.length + '):</span>';
    var totalLots = 0;
    var hasExtracting = false;
    for (var i = 0; i < currentBids.length; i++) {
      var bid = currentBids[i];
      var lotCount = bid.lots ? bid.lots.length : 0;
      totalLots += lotCount;
      var statusIcon, lotLabel;
      if (lotCount > 0) {
        statusIcon = '<span style="color:var(--success)">&#10003;</span>';
        lotLabel = lotCount + ' поз.';
      } else {
        var ageMs = Date.now() - new Date(bid.created_at).getTime();
        if (ageMs < 3 * 60 * 1000) {
          statusIcon = '<span class="spinner" style="width:10px;height:10px;border-width:1.5px;display:inline-block;vertical-align:middle"></span>';
          lotLabel = '...';
          hasExtracting = true;
        } else {
          statusIcon = '<span style="color:var(--danger)">&#10007;</span>';
          lotLabel = '0 поз.';
        }
      }
      html += '<span class="regime-bid-chip" style="display:inline-flex;align-items:center;gap:6px;padding:4px 6px 4px 10px;background:var(--card);border:1px solid var(--border);border-radius:6px;font-size:12px">' +
        statusIcon + ' <span style="font-weight:500">' + escapeHtml(bid.supplier_name || 'Поставщик') + '</span>' +
        '<span style="color:var(--text-secondary)">' + lotLabel + '</span>' +
        '<button class="regime-bid-delete" data-bid-id="' + bid.id + '" data-bid-name="' + escapeHtml(bid.supplier_name || 'Поставщик') + '" title="Удалить КП" style="background:none;border:none;cursor:pointer;padding:2px 4px;margin-left:2px;color:var(--text-muted);line-height:1;font-size:14px;border-radius:3px" onmouseover="this.style.background=\'var(--danger-bg)\';this.style.color=\'var(--danger)\'" onmouseout="this.style.background=\'none\';this.style.color=\'var(--text-muted)\'">&times;</button>' +
        '</span>';
    }
    // Compact upload button
    html += '<span id="regime-kp-upload-card" style="display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border:1px dashed var(--border);border-radius:6px;font-size:12px;cursor:pointer;color:var(--text-secondary);transition:all 0.15s" onmouseover="this.style.borderColor=\'var(--accent)\';this.style.color=\'var(--accent)\'" onmouseout="this.style.borderColor=\'var(--border)\';this.style.color=\'var(--text-secondary)\'">' +
      '+ КП<input type="file" id="inp-regime-kp-file" accept=".pdf,.xlsx,.doc,.docx" multiple style="display:none"></span>';
    html += '</div>';
    if (totalLots > 0) {
      html += '<div style="font-size:11px;color:var(--text-secondary);margin-top:2px">Всего ' + totalLots + ' товар' + (totalLots === 1 ? '' : totalLots < 5 ? 'а' : 'ов') + ' будет проверено</div>';
    }
    if (hasExtracting) {
      html += '<div style="font-size:11px;color:var(--accent);margin-top:2px;display:flex;align-items:center;gap:4px"><span class="spinner" style="width:10px;height:10px;border-width:1.5px;display:inline-block;vertical-align:middle"></span> Распознавание позиций...</div>';
    }
    html += '<div id="regime-upload-status"></div>';
    container.innerHTML = html;
    _bindRegimeUpload();
    _bindRegimeBidDelete();

    // Auto-poll bids while extraction is in progress
    if (hasExtracting) {
      _startRegimeBidsPolling();
    } else {
      _stopRegimeBidsPolling();
    }
  }

  function _bindRegimeBidDelete() {
    var buttons = document.querySelectorAll('#regime-bids-list .regime-bid-delete');
    buttons.forEach(function (btn) {
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        var bidId = this.getAttribute('data-bid-id');
        var bidName = this.getAttribute('data-bid-name');
        _handleRegimeBidDelete(bidId, bidName);
      });
    });
  }

  async function _handleRegimeBidDelete(bidId, bidName) {
    if (!currentPurchase || !bidId) return;
    if (!confirm('Удалить КП "' + bidName + '"? Это действие нельзя отменить.')) return;
    _setRegimeUploadStatus('Удаление ' + bidName + '...', 'info');
    try {
      await API.apiFetch('/purchases/' + currentPurchase.id + '/bids/' + bidId, { method: 'DELETE' });
      await loadBids();
      renderRegimeBids();
      _setRegimeUploadStatus('Удалено: ' + bidName, 'clear');
    } catch (e) {
      _setRegimeUploadStatus('Ошибка удаления: ' + e.message, 'error');
    }
  }

  function _startRegimeBidsPolling() {
    if (_regimeBidsPollingTimer) return; // already polling
    _regimeBidsPollingTimer = setInterval(async function () {
      if (!currentPurchase) return;
      try {
        currentBids = await API.apiFetch('/purchases/' + currentPurchase.id + '/bids');
        renderRegimeBids();
      } catch (_) {}
    }, 4000);
  }

  function _stopRegimeBidsPolling() {
    if (_regimeBidsPollingTimer) {
      clearInterval(_regimeBidsPollingTimer);
      _regimeBidsPollingTimer = null;
    }
  }

  function _bindRegimeUpload() {
    var zone = $('regime-kp-zone') || $('regime-kp-upload-card');
    var input = $('inp-regime-kp-file');
    if (zone && input) {
      zone.addEventListener('click', function () { input.click(); });
      // Remove first to avoid duplicates on re-render
      input.removeEventListener('change', _handleRegimeKpUpload);
      input.addEventListener('change', _handleRegimeKpUpload);
    }
  }

  function _setRegimeUploadStatus(msg, type) {
    // type: 'info' | 'error' | 'clear'
    var el = document.getElementById('regime-upload-status');
    if (!el) return;
    if (!msg || type === 'clear') { el.innerHTML = ''; return; }
    var color = type === 'error' ? 'var(--danger)' : 'var(--accent)';
    var icon = type === 'error' ? '&#10007;' : '<span class="spinner" style="width:12px;height:12px;border-width:1.5px;display:inline-block;vertical-align:middle"></span>';
    el.innerHTML = '<div style="display:flex;align-items:center;gap:8px;font-size:13px;color:' + color + ';padding:8px 0">' + icon + ' ' + escapeHtml(msg) + '</div>';
  }

  async function _handleRegimeKpUpload() {
    var files = this.files;
    if (!files || files.length === 0) return;
    if (!currentPurchase) {
      showError('Сначала выберите или создайте закупку');
      this.value = '';
      return;
    }
    var total = files.length;
    var uploaded = 0;
    var errors = [];
    for (var i = 0; i < total; i++) {
      var file = files[i];
      try {
        _setRegimeUploadStatus('Конвертация ' + (i + 1) + '/' + total + ': ' + file.name, 'info');
        var converted = await API.convertTechTaskFile(file, currentPurchase ? currentPurchase.id : null);
        if (converted && converted.markdown) {
          _setRegimeUploadStatus('Сохранение ' + (i + 1) + '/' + total + ': ' + file.name, 'info');
          var supplierName = file.name.replace(/\.[^.]+$/, '');
          await API.apiFetch('/purchases/' + currentPurchase.id + '/bids', {
            method: 'POST',
            body: {
              bid_text: converted.markdown,
              supplier_name: supplierName,
            },
          });
          trackFile(currentPurchase.id, file.name, 'regime_kp');
          uploaded++;
          _setRegimeUploadStatus('Загружено ' + uploaded + '/' + total, 'info');
        }
      } catch (e) {
        errors.push(file.name + ': ' + e.message);
      }
    }
    this.value = '';
    _setRegimeUploadStatus('Обновление списка КП...', 'info');
    await loadBids();
    renderRegimeBids();
    if (errors.length > 0) {
      _setRegimeUploadStatus('Ошибки: ' + errors.join('; '), 'error');
    } else {
      _setRegimeUploadStatus('Загружено ' + uploaded + ' КП', 'clear');
      showMessage('Загружено ' + uploaded + ' КП');
    }
  }

  function loadRegimeCheck() {
    if (!currentPurchase) return;
    renderRegimeBids();
    API.apiFetch('/regime/purchases/' + currentPurchase.id + '/check')
      .then(function (data) {
        if (!data || !data.id) {
          $('regime-results').innerHTML = '<div class="empty-state">Загрузите КП или выберите закупку с загруженными предложениями</div>';
          return;
        }
        if (data.status === 'pending' || data.status === 'processing') {
          regimeStartTime = regimeStartTime || Date.now();
          startRegimeTimer();
          renderRegimeProgress({status: 'processing', stages: [], total: 0, processed: 0, message: 'Проверка выполняется...'});
          pollRegimeCheck();
        } else {
          renderRegimeResults(data);
        }
      })
      .catch(function (err) {
        console.error('[regime] loadRegimeCheck failed:', err);
        $('regime-results').innerHTML = '<div class="empty-state">Загрузите КП или выберите закупку с загруженными предложениями</div>';
      });
  }

  function _pollRegimeCheckNow() {
    if (!currentPurchase) return;
    API.apiFetch('/regime/purchases/' + currentPurchase.id + '/check/progress')
      .then(function (progress) {
        if (progress && (progress.status === 'pending' || progress.status === 'processing')) {
          renderRegimeProgress(progress);
          _scheduleRegimePoll();
        } else if (progress && progress.status === 'done') {
          stopRegimeTimer();
          // Load results immediately, pass progress for summary
          API.apiFetch('/regime/purchases/' + currentPurchase.id + '/check')
            .then(function (data) {
              console.log('[regime] check data loaded, items:', data && data.items ? data.items.length : 'none');
              renderRegimeResults(data, progress);
            })
            .catch(function (err) {
              console.error('[regime] failed to load check results:', err);
              renderRegimeError('Ошибка загрузки результатов: ' + (err.message || err));
            });
        } else if (progress && progress.status === 'error') {
          renderRegimeError(progress.message);
        } else {
          _scheduleRegimePoll();
        }
      })
      .catch(function (err) { console.error('[regime] poll error:', err); _scheduleRegimePoll(); });
  }

  function _scheduleRegimePoll() {
    if (regimePollingTimer) clearTimeout(regimePollingTimer);
    regimePollingTimer = setTimeout(_pollRegimeCheckNow, 500);
  }

  // Backward compat: loadRegimeCheck still calls this
  function pollRegimeCheck() {
    _pollRegimeCheckNow();
  }

  function renderRegimeProgress(progress) {
    var stages = progress.stages || [];
    var total = progress.total || 0;
    var processed = progress.processed || 0;

    var elapsed = regimeStartTime ? formatElapsed(Date.now() - regimeStartTime) : '';

    var html = '<div class="search-status" style="flex-direction:column;align-items:stretch">';
    var filename = progress.filename || '';

    html += '<div style="display:flex;align-items:center;gap:12px">';
    html += '<div class="spinner"></div>';
    html += '<div><strong>Проверка национального режима...</strong></div>';
    if (elapsed) html += '<div id="regime-elapsed" style="margin-left:auto;font-size:13px;color:var(--text-secondary)">' + elapsed + '</div>';
    html += '</div>';
    if (filename) {
      html += '<div style="font-size:12px;color:var(--text-secondary);margin-top:4px">' + escapeHtml(filename) + '</div>';
    }

    // Stages with checkmarks
    if (stages.length > 0) {
      html += '<div style="margin-top:8px">';
      for (var i = 0; i < stages.length; i++) {
        var s = stages[i];
        var icon, textStyle;
        if (s.status === 'done') {
          icon = '<span style="color:var(--success)">&#10003;</span>';
          textStyle = 'color:var(--text-secondary)';
        } else if (s.status === 'skipped') {
          icon = '<span style="color:var(--text-secondary)">&#8212;</span>';
          textStyle = 'color:var(--text-secondary);text-decoration:line-through';
        } else if (s.status === 'in_progress') {
          icon = '<span class="spinner" style="width:12px;height:12px;border-width:1.5px;display:inline-block;vertical-align:middle"></span>';
          textStyle = 'font-weight:500';
        } else {
          icon = '<span style="color:var(--text-secondary)">&#9675;</span>';
          textStyle = 'color:var(--text-secondary)';
        }
        html += '<div style="font-size:13px;margin-bottom:4px;' + textStyle + '">' + icon + ' ' + escapeHtml(s.name);
        if (s.detail) {
          html += '<span style="margin-left:8px;font-weight:600;color:var(--accent)">' + escapeHtml(s.detail) + '</span>';
        }
        html += '</div>';
      }
      html += '</div>';
    }

    // Progress bar for items check
    if (total > 0 && processed < total) {
      var pct = Math.round((processed / total) * 100);
      html += '<div style="margin-top:10px">';
      html += '<div style="height:6px;background:var(--bg);border-radius:4px;overflow:hidden;border:1px solid var(--border)">';
      html += '<div style="height:100%;width:' + pct + '%;background:linear-gradient(90deg,var(--accent),var(--success));transition:width .3s"></div>';
      html += '</div>';
      html += '<div style="font-size:12px;color:var(--text-secondary);margin-top:4px">' + processed + ' из ' + total + ' товаров (' + pct + '%)</div>';
      html += '</div>';
    }

    html += '</div>';
    $('regime-results').innerHTML = html;
  }

  function renderRegimeError(message) {
    stopRegimeTimer();
    regimeStartTime = null;
    var html = '<div class="search-status" style="background:var(--danger-bg);flex-direction:column;align-items:stretch">';
    html += '<div style="display:flex;align-items:center;gap:12px">';
    html += '<div style="color:var(--danger);font-size:18px">&#10007;</div>';
    html += '<div><strong style="color:var(--danger)">Ошибка проверки</strong></div>';
    html += '</div>';
    if (message) html += '<div style="font-size:13px;margin-top:6px;color:var(--text-secondary)">' + escapeHtml(message) + '</div>';
    html += '</div>';
    $('regime-results').innerHTML = html;
  }

  function renderRegimeResults(data, progress) {
    stopRegimeTimer();
    regimeStartTime = null;
    if (!data || !data.items || data.items.length === 0) {
      $('regime-results').innerHTML = '<div class="empty-state">Нет результатов проверки</div>';
      return;
    }

    // Group items by source supplier
    var groups = {};
    var groupOrder = [];
    for (var i = 0; i < data.items.length; i++) {
      var item = data.items[i];
      var key = item.source_supplier || item.source_bid_id || 'all';
      if (!groups[key]) {
        groups[key] = { label: item.source_supplier || 'Все товары', items: [], ok: 0, warn: 0, err: 0, nf: 0 };
        groupOrder.push(key);
      }
      groups[key].items.push(item);
      if (item.overall_status === 'ok') groups[key].ok++;
      else if (item.overall_status === 'warning') groups[key].warn++;
      else if (item.overall_status === 'error') groups[key].err++;
      else groups[key].nf++;
    }

    var html = '';

    // Summary banner with completed stages + counts
    var totalOk = 0, totalWarn = 0, totalErr = 0, totalNf = 0;
    for (var _k in groups) { totalOk += groups[_k].ok; totalWarn += groups[_k].warn; totalErr += groups[_k].err; totalNf += groups[_k].nf; }
    var summaryColor = totalErr > 0 ? 'var(--danger)' : totalWarn > 0 ? 'var(--warning)' : 'var(--success)';
    var summaryBg = totalErr > 0 ? 'var(--danger-bg)' : totalWarn > 0 ? 'var(--warning-bg)' : 'var(--success-bg)';
    html += '<div style="background:' + summaryBg + ';border-radius:8px;padding:12px 16px;margin-bottom:16px">';
    html += '<div style="display:flex;align-items:center;gap:12px">';
    html += '<div style="color:' + summaryColor + ';font-size:18px">&#10003;</div>';
    html += '<div><strong style="color:' + summaryColor + '">Проверка завершена</strong>';
    html += '<span style="margin-left:12px;font-size:13px;color:var(--text-secondary)">' + data.items.length + ' товаров</span></div>';
    if (progress && progress.timings && progress.timings.total) {
      html += '<div style="margin-left:auto;font-size:13px;color:var(--text-secondary)">' + progress.timings.total + 'с</div>';
    }
    html += '</div>';
    // Counts row
    html += '<div style="display:flex;gap:16px;margin-top:8px;font-size:13px">';
    if (totalOk > 0) html += '<span style="color:var(--success)">&#10003; ' + totalOk + ' соответствует</span>';
    if (totalWarn > 0) html += '<span style="color:var(--warning)">&#9888; ' + totalWarn + ' внимание</span>';
    if (totalErr > 0) html += '<span style="color:var(--danger)">&#10007; ' + totalErr + ' не соответствует</span>';
    if (totalNf > 0) html += '<span style="color:var(--text-secondary)">&#8212; ' + totalNf + ' не найден</span>';
    html += '</div>';
    // Stages summary (collapsed)
    if (progress && progress.stages && progress.stages.length > 0) {
      html += '<div style="margin-top:8px;font-size:12px;color:var(--text-secondary)">';
      for (var si = 0; si < progress.stages.length; si++) {
        var stg = progress.stages[si];
        var stgIcon = stg.status === 'done' ? '<span style="color:var(--success)">&#10003;</span>' : stg.status === 'skipped' ? '&#8212;' : '&#9675;';
        html += stgIcon + ' ' + escapeHtml(stg.name);
        if (stg.detail) html += ' <span style="color:var(--accent)">' + escapeHtml(stg.detail) + '</span>';
        if (si < progress.stages.length - 1) html += ' &nbsp;|&nbsp; ';
      }
      html += '</div>';
    }
    html += '</div>';

    var hasMultiple = groupOrder.length > 1;

    // Supplier tabs (only if multiple suppliers)
    if (hasMultiple) {
      html += '<div class="comp-suppliers-bar"><div class="comp-suppliers-label">Результаты по КП:</div><div class="comp-suppliers-list">';
      for (var gi = 0; gi < groupOrder.length; gi++) {
        var g = groups[groupOrder[gi]];
        var indicatorCls = g.err > 0 ? 'comp-supplier-indicator--warn' : g.nf > 0 ? 'comp-supplier-indicator--pending' : 'comp-supplier-indicator--ok';
        var meta = g.items.length + ' поз.';
        if (g.err > 0) meta += ', ' + g.err + ' несоотв.';
        else if (g.ok === g.items.length) meta += ', все ОК';
        html += '<div class="comp-supplier-tab' + (gi === 0 ? ' active' : '') + '" data-regime-group="' + gi + '" onclick="window._switchRegimeTab(' + gi + ')">';
        html += '<span class="comp-supplier-indicator ' + indicatorCls + '"></span>';
        html += '<div><div class="comp-supplier-tab-name">' + escapeHtml(g.label) + '</div>';
        html += '<div class="comp-supplier-tab-meta">' + meta + '</div></div></div>';
      }
      html += '</div></div>';
    }

    // Content panes per supplier
    for (var gi2 = 0; gi2 < groupOrder.length; gi2++) {
      var grp = groups[groupOrder[gi2]];
      html += '<div class="regime-group-content' + (gi2 === 0 ? ' active' : '') + '" id="regime-group-' + gi2 + '">';
      html += _renderRegimeItemCards(grp.items);
      html += '</div>';
    }

    $('regime-results').innerHTML = html;

    // Tab switching
    window._switchRegimeTab = function (idx) {
      var tabs = document.querySelectorAll('.comp-supplier-tab[data-regime-group]');
      var panes = document.querySelectorAll('.regime-group-content');
      for (var t = 0; t < tabs.length; t++) tabs[t].classList.toggle('active', t === idx);
      for (var p = 0; p < panes.length; p++) panes[p].classList.toggle('active', p === idx);
    };
  }

  function _renderRegimeItemCards(items) {
    var html = '';
    for (var i = 0; i < items.length; i++) {
      var item = items[i];
      var statusClass = 'status-draft';
      var statusLabel = 'Проверяется';
      if (item.overall_status === 'ok') { statusClass = 'status-active'; statusLabel = 'Соответствует'; }
      else if (item.overall_status === 'warning') { statusClass = 'status-warning'; statusLabel = 'Внимание'; }
      else if (item.overall_status === 'error') { statusClass = 'status-search'; statusLabel = 'Не соответствует'; }
      else if (item.overall_status === 'not_found') { statusClass = 'status-draft'; statusLabel = 'Не найден'; }

      html += '<div class="regime-card"><div class="regime-checks">';

      // Column 1: Product info
      html += '<div class="regime-check" style="background:none;padding:4px 8px">';
      html += '<div class="regime-product" style="font-size:13px">' + escapeHtml(item.product_name || 'Товар') + '</div>';
      if (item.registry_number) html += '<div style="font-size:11px;color:var(--text-secondary)">№ ' + escapeHtml(item.registry_number) + '</div>';
      if (item.okpd2_code) html += '<div style="font-size:11px;color:var(--text-secondary)">' + escapeHtml(item.okpd2_code) + '</div>';
      html += '<span class="status ' + statusClass + '" style="margin-top:4px;font-size:11px"><span class="status-dot"></span> ' + statusLabel + '</span>';
      html += '</div>';

      // Column 2: Registry PP 719
      html += renderRegimeCheckCell('Реестр ПП №719', item.registry_status, item.registry_actual, item.registry_cert_end_date, item.registry_raw_url);

      // Column 3: Localization PP 1875
      var locDetail = _renderLocalizationDetail(item);
      html += renderRegimeCheckCellSimple('Баллы локализации', item.localization_status, locDetail);

      // Column 4: GISP — expandable comparison table
      html += renderGispCheckCell(item, item.id || i);

      html += '</div></div>';
    }
    return html;
  }

  function renderRegimeCheckCell(label, status, actual, certEnd, url) {
    var cls = 'regime-check regime-unknown';
    var icon = '—';
    var detail = '';
    if (status === 'ok') { cls = 'regime-check regime-pass'; icon = '✓'; detail = actual ? 'Найден в реестре' : 'Найден, не актуален'; }
    else if (status === 'not_actual') { cls = 'regime-check regime-fail'; icon = '✗'; detail = 'Запись не актуальна'; }
    else if (status === 'not_found') { cls = 'regime-check regime-fail'; icon = '✗'; detail = 'Не найден в реестре'; }
    else if (status === 'registry_error') { cls = 'regime-check regime-unknown'; icon = '⚠'; detail = 'Проблемы с доступом к реестру'; }
    if (certEnd) detail += ', до ' + certEnd;
    return '<div class="' + cls + '"><div class="regime-check-label">' + icon + ' ' + escapeHtml(label) + '</div><div class="regime-check-value">' + detail + '</div></div>';
  }

  function _renderLocalizationDetail(item) {
    var primary = '';
    if (item.localization_actual_score != null && item.localization_required_score != null) {
      primary = item.localization_actual_score + ' из ' + item.localization_required_score + ' (мин.)';
    } else if (item.localization_status === 'out_of_scope') {
      primary = 'Нацрежим не применяется';
    } else if (item.localization_status === 'advisory_min_share') {
      primary = 'Мин. доля российских (инфо для заказчика)';
    } else if (item.localization_status === 'ok' && item.localization_required_score == null) {
      primary = 'Требования к баллам не установлены';
    } else if (item.localization_status === 'score_missing' && item.localization_required_score != null) {
      primary = 'Балл не указан, требуется ≥ ' + item.localization_required_score;
    }
    var det = null;
    if (item.localization_details) {
      try {
        det = typeof item.localization_details === 'string' ? JSON.parse(item.localization_details) : item.localization_details;
      } catch (e) { det = null; }
    }
    if (!det) return primary;
    var lines = [];
    if (primary) lines.push(primary);
    if (det.pp1875) {
      var label = det.pp1875.regime_label || det.pp1875.regime || '';
      var pos = det.pp1875.position ? ' поз. ' + det.pp1875.position : '';
      var inh = det.pp1875.inherited_from ? ' (наследует от ОКПД2 ' + det.pp1875.inherited_from + ')' : '';
      if (label) lines.push('<small style="color:var(--text-secondary)">' + escapeHtml(label + pos + inh) + '</small>');
      if (det.pp1875.all_regimes && det.pp1875.all_regimes.length > 1) {
        var extras = det.pp1875.all_regimes.slice(1).map(function (r) {
          var s = 'прил. ' + r.appendix;
          if (r.min_share_percent != null) s += ', ' + r.min_share_percent + '%';
          return s;
        }).join('; ');
        lines.push('<small style="color:var(--text-secondary)">+ также: ' + escapeHtml(extras) + '</small>');
      }
    }
    if (det.pp719 && det.pp719.section) {
      lines.push('<small style="color:var(--text-secondary)">' + escapeHtml(det.pp719.section) + '</small>');
    }
    if (det.upcoming_warning) {
      lines.push('<small style="color:#d97706">С ' + escapeHtml(det.upcoming_warning.from) +
                 ' порог вырастет до ' + det.upcoming_warning.min_score + '</small>');
    }
    if (det.rep_level) {
      if (det.rep_level.this_product_level) {
        lines.push('<small style="color:var(--text-secondary)">РЭП: товар — ' + escapeHtml(det.rep_level.this_product_level) + '</small>');
      }
      if (det.rep_level.applicable) {
        var observed = det.rep_level.observed_levels || {};
        var obsList = Object.keys(observed).map(function (k) { return k + ' (' + observed[k] + ')'; }).join(', ');
        if (obsList) lines.push('<small style="color:var(--text-secondary)">РЭП: типично ' + escapeHtml(obsList) + '</small>');
      }
    }
    if (det.reason && lines.length === 0) {
      lines.push('<small style="color:var(--text-secondary)">' + escapeHtml(det.reason) + '</small>');
    }
    return lines.join('<br>');
  }

  function renderRegimeCheckCellSimple(label, status, detail) {
    var cls = 'regime-check regime-unknown';
    var icon = '—';
    if (status === 'ok' || status === 'out_of_scope' || status === 'advisory_min_share') { cls = 'regime-check regime-pass'; icon = '✓'; }
    else if (status === 'insufficient' || status === 'mismatch' || status === 'error') { cls = 'regime-check regime-fail'; icon = '✗'; }
    else if (status === 'warning' || status === 'wording') { cls = 'regime-check regime-unknown'; icon = '⚠'; }
    else if (status === 'not_found' || status === 'okpd_not_found' || status === 'score_missing') { cls = 'regime-check regime-unknown'; icon = '—'; }
    return '<div class="' + cls + '"><div class="regime-check-label">' + icon + ' ' + escapeHtml(label) + '</div><div class="regime-check-value">' + (detail || '') + '</div></div>';
  }

  function renderGispCheckCell(item, idx) {
    var status = item.gisp_status;
    var cls = 'regime-check regime-unknown';
    var icon = '—';
    if (status === 'ok') { cls = 'regime-check regime-pass'; icon = '✓'; }
    else if (status === 'mismatch' || status === 'wrong_registry_suspected' || status === 'error') { cls = 'regime-check regime-fail'; icon = '✗'; }
    else if (status === 'warning' || status === 'wording') { cls = 'regime-check regime-unknown'; icon = '⚠'; }

    var comparison = null;
    if (item.gisp_comparison) {
      try { comparison = typeof item.gisp_comparison === 'string' ? JSON.parse(item.gisp_comparison) : item.gisp_comparison; } catch (e) { comparison = null; }
    }

    var html = '<div class="' + cls + '">';
    html += '<div class="regime-check-label">' + icon + ' Каталог ГИСП';
    if (comparison && comparison.length) {
      html += ' <button class="gisp-toggle" onclick="window._toggleGisp(' + idx + ')" title="Показать детали">▸</button>';
    }
    html += '</div>';
    html += '<div class="regime-check-value">';
    if (status === 'ok') html += 'Характеристики совпадают';
    else if (status === 'wrong_registry_suspected') html += 'Вероятно неверный реестровый номер';
    else if (status === 'mismatch') html += 'Несоответствие характеристик';
    else if (status === 'warning') html += 'Требует внимания';
    else if (status === 'skipped') html += 'Пропущено (нет данных реестра)';
    else if (status === 'not_found') html += 'Не найден в реестре';
    else if (status === 'gisp_unavailable') html += 'Проблемы с доступом к ГИСП';
    if (item.gisp_url) html += ' <a href="' + escapeHtml(item.gisp_url) + '" target="_blank" style="margin-left:6px">Карточка ГИСП</a>';
    html += '</div>';

    if (comparison && comparison.length) {
      html += '<div class="gisp-details" id="gisp-details-' + idx + '">';
      html += '<table class="gisp-table"><thead><tr>';
      html += '<th>Характеристика</th><th>Поставщик</th><th>ГИСП</th><th>Результат</th>';
      html += '</tr></thead><tbody>';
      for (var c = 0; c < comparison.length; c++) {
        var row = comparison[c];
        var rowCls = '';
        var statusLabel = '';
        if (row.status === 'ok') { rowCls = 'gisp-row-ok'; statusLabel = '✓ Совпадает'; }
        else if (row.status === 'mismatch') { rowCls = 'gisp-row-mismatch'; statusLabel = '✗ Не совпадает'; }
        else if (row.status === 'wording') { rowCls = 'gisp-row-wording'; statusLabel = '⚠ Отличие формулировки'; }
        else if (row.status === 'missing_in_gisp') { rowCls = 'gisp-row-missing'; statusLabel = '— Нет в ГИСП'; }
        else { statusLabel = row.status || '—'; }

        html += '<tr class="' + rowCls + '">';
        html += '<td>' + escapeHtml(row.name || '') + '</td>';
        html += '<td>' + escapeHtml(row.supplier_value || '—') + '</td>';
        html += '<td>' + escapeHtml(row.gisp_value != null ? row.gisp_value : '—') + '</td>';
        html += '<td class="gisp-status-cell">' + statusLabel;
        if (row.comment) html += '<div class="gisp-comment">' + escapeHtml(row.comment) + '</div>';
        html += '</td></tr>';
      }
      html += '</tbody></table></div>';
    }

    html += '</div>';
    return html;
  }

  window._toggleGisp = function (idx) {
    var el = document.getElementById('gisp-details-' + idx);
    if (!el) return;
    var open = el.classList.toggle('open');
    var btn = el.parentElement.querySelector('.gisp-toggle');
    if (btn) btn.textContent = open ? '▾' : '▸';
  };

  // ── Regime Diagnostics ─────────────────────────────────────────────

  var _regimeDiagData = null;

  function initRegimeDiag() {
    var btn = $('btn-regime-diag');
    var user = (typeof Auth !== 'undefined' && Auth.getUser) ? Auth.getUser() : null;
    var isAdmin = !!(user && user.is_admin);
    if (btn && !isAdmin) {
      btn.style.display = 'none';
      return;
    }
    if (btn) btn.addEventListener('click', function () {
      openModal('modal-regime-diag');
      loadRegimeDiagnostics();
    });
    var refreshBtn = $('btn-regime-diag-refresh');
    if (refreshBtn) refreshBtn.addEventListener('click', loadRegimeDiagnostics);
    var copyBtn = $('btn-regime-diag-copy');
    if (copyBtn) copyBtn.addEventListener('click', function () {
      if (_regimeDiagData) {
        navigator.clipboard.writeText(JSON.stringify(_regimeDiagData, null, 2))
          .then(function () { showMessage('JSON скопирован'); })
          .catch(function () { showError('Не удалось скопировать'); });
      }
    });
  }

  function loadRegimeDiagnostics() {
    var el = $('regime-diag-content');
    if (!el || !currentPurchase) return;
    el.textContent = 'Загрузка...';
    API.apiFetch('/regime/purchases/' + currentPurchase.id + '/check/diagnostics')
      .then(function (data) {
        _regimeDiagData = data;
        el.textContent = _formatRegimeDiag(data);
      })
      .catch(function (err) {
        el.textContent = 'Ошибка: ' + err.message;
      });
  }

  function _formatRegimeDiag(d) {
    var out = '';
    out += '=== КП (Bids) ===\n';
    if (d.bids && d.bids.length) {
      for (var i = 0; i < d.bids.length; i++) {
        var b = d.bids[i];
        out += '  #' + b.bid_id + ' ' + (b.supplier_name || '(без имени)') + ' -> ' + b.lot_count + ' лотов (' + b.created_at + ')\n';
      }
    } else {
      out += '  (нет КП)\n';
    }

    out += '\n=== Проверки Нацрежим ===\n';
    if (d.checks && d.checks.length) {
      for (var j = 0; j < d.checks.length; j++) {
        var c = d.checks[j];
        out += '\n  --- check #' + c.check_id + ' [' + c.status + '] ---\n';
        if (c.filename) out += '  Источник: ' + c.filename + '\n';
        out += '  Результаты в БД: ' + c.items_in_db + ' позиций\n';
        out += '  ok=' + (c.ok || 0) + ' warning=' + (c.warning || 0) + ' error=' + (c.error || 0) + ' not_found=' + (c.not_found || 0) + '\n';
        out += '  Создан: ' + c.created_at + '\n';
        if (c.progress && c.progress.stages && c.progress.stages.length) {
          out += '  Этапы:\n';
          for (var k = 0; k < c.progress.stages.length; k++) {
            var s = c.progress.stages[k];
            var icon = s.status === 'done' ? '[OK]' : s.status === 'in_progress' ? '[..] ' : s.status === 'skipped' ? '[--]' : '[  ]';
            out += '    ' + icon + ' ' + s.name + (s.detail ? ' -> ' + s.detail : '') + '\n';
          }
        }
        if (c.progress && c.progress.timings) {
          out += '  Тайминги: ' + JSON.stringify(c.progress.timings) + '\n';
        }
      }
    } else {
      out += '  (нет проверок)\n';
    }

    out += '\n=== Raw JSON ===\n';
    out += JSON.stringify(d, null, 2);
    return out;
  }

  // ── Polling cleanup ────────────────────────────────────────────────

  function clearPolling() {
    if (lotsPollingTimer) { clearTimeout(lotsPollingTimer); lotsPollingTimer = null; }
    if (searchPollingTimer) { clearTimeout(searchPollingTimer); searchPollingTimer = null; }
    if (comparisonPollingTimer) { clearTimeout(comparisonPollingTimer); comparisonPollingTimer = null; }
    if (regimePollingTimer) { clearTimeout(regimePollingTimer); regimePollingTimer = null; }
    stopSearchTimer();
    stopRegimeTimer();
    _stopRegimeBidsPolling();
  }

  // ── Init ───────────────────────────────────────────────────────────

  // ── Dashboard ────────────────────────────────────────────────────

  var lastDashboardItems = [];
  var dashboardSearchQuery = '';

  async function loadDashboard() {
    var archivedSel = $('dashboard-filter-archived');
    var sortSel = $('dashboard-sort');
    var params = [];
    if (archivedSel && archivedSel.value !== '') params.push('archived=' + archivedSel.value);
    var sortVal = sortSel ? sortSel.value : 'created_at_desc';
    var parts = sortVal.split('_');
    var sortOrder = parts.pop();
    var sortBy = parts.join('_');
    params.push('sort_by=' + sortBy);
    params.push('sort_order=' + sortOrder);

    try {
      var data = await API.apiFetch('/purchases/dashboard?' + params.join('&'));
      lastDashboardItems = data || [];
      renderDashboardCards(_filterDashboardItems(lastDashboardItems));
    } catch (e) {
      $('dashboard-cards').innerHTML = '<div class="empty-state">Ошибка загрузки: ' + escapeHtml(e.message) + '</div>';
    }
  }

  function _filterDashboardItems(items) {
    if (!dashboardSearchQuery) return items;
    var q = dashboardSearchQuery.toLowerCase();
    return items.filter(function (p) {
      var name = ((p.custom_name || p.full_name || '') + '').toLowerCase();
      return name.indexOf(q) !== -1;
    });
  }

  function renderDashboardCards(items) {
    _renderQuickActions(lastDashboardItems.length > 0);
    var container = $('dashboard-cards');
    if (!items.length) {
      container.innerHTML = '<div class="empty-state">' + (dashboardSearchQuery ? 'Ничего не найдено' : 'Нет закупок') + '</div>';
      return;
    }

    var moduleLabels = {
      search_status: 'Поиск',
      correspondence_status: 'Письма',
      comparison_status: 'Сравнение',
      regime_check_status: 'Нацрежим',
    };

    var fileTypeLabels = { tz: 'ТЗ', kp: 'КП', regime_kp: 'КП (нацрежим)' };
    var fileTypeIcons = { tz: 'description', kp: 'table_chart', regime_kp: 'verified' };

    var html = '';
    for (var i = 0; i < items.length; i++) {
      var p = items[i];
      var name = escapeHtml(p.custom_name || p.full_name || 'Закупка #' + p.auto_number);
      var date = new Date(p.created_at).toLocaleDateString('ru-RU');
      var archivedClass = p.is_archived ? ' archived' : '';
      var badgeLabel = p.is_archived ? 'Архив' : 'Активна';
      var badgeClass = p.is_archived ? ' archived' : '';

      // Module dots
      var progressHtml = '';
      var keys = ['search_status', 'correspondence_status', 'comparison_status', 'regime_check_status'];
      for (var m = 0; m < keys.length; m++) {
        var st = p[keys[m]] || 'not_started';
        var mutedCls = (st === 'not_started') ? ' muted' : '';
        progressHtml += '<div class="dashboard-module' + mutedCls + '"><span class="module-dot ' + st + '"></span>' + moduleLabels[keys[m]] + '</div>';
      }

      // Metrics
      var metricsHtml =
        '<span><b>' + p.lots_count + '</b>лотов</span>' +
        '<span><b>' + p.suppliers_count + '</b>поставщ.</span>' +
        '<span><b>' + p.bids_count + '</b>КП</span>';

      // Files
      var filesHtml = '';
      if (p.files && p.files.length) {
        for (var f = 0; f < p.files.length; f++) {
          var fl = p.files[f];
          var typeLabel = fileTypeLabels[fl.file_type] || fl.file_type;
          var typeIcon = fileTypeIcons[fl.file_type] || 'draft';
          var fname = escapeHtml(fl.filename);
          filesHtml += '<span class="dashboard-file-chip" title="' + typeLabel + ': ' + fname + '">';
          filesHtml += '<span class="material-symbols-outlined">' + typeIcon + '</span>';
          filesHtml += '<b>' + typeLabel + ':</b>' + fname;
          filesHtml += '</span>';
        }
      }

      var archiveBtnLabel = p.is_archived ? 'Восстановить' : 'В архив';
      var archiveBtnNewState = p.is_archived ? 'false' : 'true';

      html += '<div class="dashboard-card' + archivedClass + '">';
      html +=   '<div class="dashboard-card-head">';
      html +=     '<div class="dashboard-card-head-left">';
      html +=       '<div class="dashboard-card-name" title="' + name + '">' + name + '</div>';
      html +=       '<span class="dashboard-status-badge' + badgeClass + '">' + badgeLabel + '</span>';
      html +=       '<div class="dashboard-card-date">' + date + '</div>';
      html +=     '</div>';
      html +=     '<div class="dashboard-card-actions">';
      html +=       '<button class="btn btn-sm btn-secondary btn-archive-purchase" data-pid="' + p.id + '" data-archive="' + archiveBtnNewState + '">' + archiveBtnLabel + '</button>';
      html +=       '<button class="btn btn-sm btn-primary btn-open-purchase" data-pid="' + p.id + '">Открыть</button>';
      html +=     '</div>';
      html +=   '</div>';
      html +=   '<div class="dashboard-card-body">';
      html +=     '<div class="dashboard-card-body-left">';
      html +=       '<div class="dashboard-progress">' + progressHtml + '</div>';
      html +=       '<div class="dashboard-card-divider"></div>';
      html +=       '<div class="dashboard-metrics">' + metricsHtml + '</div>';
      html +=     '</div>';
      if (filesHtml) html += '<div class="dashboard-files">' + filesHtml + '</div>';
      html +=   '</div>';
      html += '</div>';
    }
    container.innerHTML = html;

    // Bind open buttons
    container.querySelectorAll('.btn-open-purchase').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var pid = parseInt(this.getAttribute('data-pid'));
        var found = purchases.find(function (pp) { return pp.id === pid; });
        if (found) {
          selectPurchase(found);
        } else {
          // Purchase might be archived — load it directly
          API.apiFetch('/purchases/' + pid).then(function (pp) {
            if (pp) selectPurchase(pp);
          });
        }
        // Switch to search tab
        var searchTab = document.querySelector('.sidebar .tab[data-tab="search"]');
        if (searchTab) searchTab.click();
      });
    });

    // Bind archive buttons
    container.querySelectorAll('.btn-archive-purchase').forEach(function (btn) {
      btn.addEventListener('click', async function () {
        var pid = this.getAttribute('data-pid');
        var newArchived = this.getAttribute('data-archive') === 'true';
        var action = newArchived ? 'архивировать' : 'восстановить';
        if (!confirm('Вы уверены, что хотите ' + action + ' закупку?')) return;
        try {
          await API.apiFetch('/purchases/' + pid, { method: 'PATCH', body: { is_archived: newArchived } });
          loadDashboard();
          loadPurchases();
        } catch (e) {
          showError('Ошибка: ' + e.message);
        }
      });
    });
  }

  function initDashboard() {
    var filterArchived = $('dashboard-filter-archived');
    var sortSel = $('dashboard-sort');
    var searchInp = $('dashboard-search-input');
    if (filterArchived) filterArchived.addEventListener('change', function () { loadDashboard(); });
    if (sortSel) sortSel.addEventListener('change', function () { loadDashboard(); });
    if (searchInp) searchInp.addEventListener('input', function () {
      dashboardSearchQuery = this.value.trim();
      renderDashboardCards(_filterDashboardItems(lastDashboardItems));
    });
  }

  function trackFile(purchaseId, filename, fileType) {
    API.apiFetch('/purchases/' + purchaseId + '/files', {
      method: 'POST',
      body: { filename: filename, file_type: fileType },
    }).catch(function () { /* non-critical */ });
  }

  // ── Lots diagnostics ──────────────────────────────────────────────

  var lastDiagPayload = null;

  function _fmtAge(s) {
    if (typeof s !== 'number') return '';
    var m = Math.floor(s / 60);
    var sec = s % 60;
    return (m > 0 ? m + 'м ' : '') + sec + 'с';
  }

  function _formatTaskBlock(t) {
    var createdAge = typeof t.age_seconds === 'number' ? ' (создана ' + _fmtAge(t.age_seconds) + ' назад)' : '';
    var updateLine = '';
    if (typeof t.seconds_since_update === 'number') {
      var stuckMarker = '';
      if (t.status === 'in_progress' && t.seconds_since_update > 120) {
        stuckMarker = '  ⚠ ВОЗМОЖНО ЗАВИСЛА (нет обновлений > 2 мин)';
      }
      updateLine = 'Последнее обновление: ' + _fmtAge(t.seconds_since_update) + ' назад' + stuckMarker + '\n';
    }
    return '\n--- Task #' + t.id + ' [' + t.status + '] ' + t.task_type + createdAge + ' ---\n' +
      updateLine +
      'Input (' + t.input_length + ' chars):\n' + (t.input_preview || '') + '\n' +
      'Output (' + t.output_length + ' chars):\n' + (t.output_preview || '') + '\n';
  }

  async function resetTask(taskType) {
    if (!currentPurchase) return;
    if (!confirm('Принудительно сбросить активную задачу "' + taskType + '"? Это пометит её как failed, и можно будет запустить заново.')) return;
    try {
      var resp = await API.apiFetch('/purchases/' + currentPurchase.id + '/tasks/reset?task_type=' + encodeURIComponent(taskType), {
        method: 'POST',
      });
      showMessage('Сброшено задач: ' + (resp.reset || 0));
      loadLotsDiagnostics();
      // Refresh main UI as well so the lots/search panels update
      if (taskType === 'lots_extraction') loadLots();
      if (taskType === 'supplier_search' || taskType === 'supplier_search_perplexity') checkSearchStatus();
    } catch (e) {
      showError('Не удалось сбросить: ' + e.message);
    }
  }

  function _renderSummaryBlock(s) {
    if (!s) return '';
    var lines = [];
    lines.push('╔══════════════════════════════════════════════════════════════════╗');
    lines.push('║                    СВОДКА ПО ПОДСИСТЕМАМ                         ║');
    lines.push('╚══════════════════════════════════════════════════════════════════╝');
    lines.push('');

    if (s.lots) {
      lines.push('▼ РАСПОЗНАВАНИЕ ЛОТОВ');
      lines.push('  ' + s.lots.status);
      lines.push('  Лотов в БД: ' + s.lots.lots_in_db +
        '  |  completed: ' + (s.lots.completed_count || 0) +
        '  |  failed: ' + (s.lots.failed_count || 0));
      if (s.lots.action_hint) {
        lines.push('  → ' + s.lots.action_hint);
      }
      lines.push('');
    }

    if (s.supplier_search) {
      lines.push('▼ ПОИСК ПОСТАВЩИКОВ');
      lines.push('  ' + s.supplier_search.status);
      lines.push('  Поставщиков в БД: ' + s.supplier_search.suppliers_in_db);
      // ASCII progress bar for crawl stage
      var cp = s.supplier_search.crawl_progress;
      if (cp && cp.total > 0) {
        var barWidth = 30;
        var filled = Math.round(barWidth * (cp.processed / cp.total));
        var bar = '';
        for (var i = 0; i < barWidth; i++) bar += i < filled ? '█' : '░';
        lines.push('  Краулинг: [' + bar + '] ' + cp.processed + '/' + cp.total + ' (' + cp.percent + '%)');
      }
      if (s.supplier_search.current_stage) {
        lines.push('  Текущая стадия: ' + s.supplier_search.current_stage);
      }
      if (s.supplier_search.action_hint) {
        lines.push('  → ' + s.supplier_search.action_hint);
      }
      lines.push('');
    }

    if (s.infrastructure) {
      lines.push('▼ ИНФРАСТРУКТУРА');
      lines.push('  ' + s.infrastructure.status);
      lines.push('');
    }

    return lines.join('\n');
  }

  async function loadLotsDiagnostics() {
    var contentEl = $('diag-content');
    if (!currentPurchase) {
      contentEl.textContent = 'Сначала выберите закупку';
      return;
    }
    contentEl.textContent = 'Загрузка...';
    try {
      var data = await API.apiFetch('/purchases/' + currentPurchase.id + '/lots/diagnostics');
      lastDiagPayload = data;

      var lotsTasks = data.lots_tasks || data.tasks || [];
      var supplierTasks = data.supplier_tasks || [];
      var otherTasks = data.other_tasks || [];

      // ── Top: human-readable summary ─────────────────────────
      var topSummary = _renderSummaryBlock(data.summary);

      var contextBlock = [
        '─────────────────────── КОНТЕКСТ ───────────────────────',
        'Закупка ID:           ' + data.purchase_id,
        'Статус закупки:       ' + data.purchase_status,
        'ТЗ загружено:         ' + (data.has_terms_text ? 'да (' + data.terms_text_length + ' символов)' : 'НЕТ'),
        'OpenAI model:         ' + data.openai_model,
        '',
        'Превью ТЗ: ' + (data.terms_text_preview || '(пусто)').slice(0, 200) + '...',
      ].join('\n');

      var lotsSection = '\n\n─────────────────────── РАСПОЗНАВАНИЕ ЛОТОВ (' + lotsTasks.length + ' задач) ───────────────────────';
      if (lotsTasks.length) {
        for (var i = 0; i < lotsTasks.length; i++) lotsSection += _formatTaskBlock(lotsTasks[i]);
      } else {
        lotsSection += '\n(задач нет — extraction не запускалось)\n';
      }

      var supplierSection = '\n\n─────────────────────── ПОИСК ПОСТАВЩИКОВ (' + supplierTasks.length + ' задач) ───────────────────────';
      if (supplierTasks.length) {
        for (var j = 0; j < supplierTasks.length; j++) supplierSection += _formatTaskBlock(supplierTasks[j]);
      } else {
        supplierSection += '\n(задач нет — поиск ещё не запускался)\n';
      }

      var otherSection = '';
      if (otherTasks.length) {
        otherSection = '\n\n─────────────────────── ДРУГИЕ ЗАДАЧИ (' + otherTasks.length + ') ───────────────────────';
        for (var k = 0; k < otherTasks.length; k++) otherSection += _formatTaskBlock(otherTasks[k]);
      }

      contentEl.textContent = topSummary + '\n' + contextBlock + lotsSection + supplierSection + otherSection +
        '\n\n─────────────────────── СЫРОЙ JSON ───────────────────────\n' + JSON.stringify(data, null, 2);
      logDiag('diagnostics', data);
    } catch (e) {
      contentEl.textContent = 'Ошибка загрузки диагностики: ' + e.message;
      logDiag('diagnostics:error', { message: e.message });
    }
  }

  function initLotsDiagnostics() {
    // Diagnostics is admin-only — hide the button entirely for regular users.
    var btn = $('btn-lots-diag');
    var user = (typeof Auth !== 'undefined' && Auth.getUser) ? Auth.getUser() : null;
    var isAdmin = !!(user && user.is_admin);
    if (btn && !isAdmin) {
      btn.style.display = 'none';
      return; // no point wiring listeners on a hidden button
    }
    if (btn) {
      btn.addEventListener('click', function () {
        openModal('modal-lots-diag');
        loadLotsDiagnostics();
      });
    }
    var refreshBtn = $('btn-diag-refresh');
    if (refreshBtn) refreshBtn.addEventListener('click', loadLotsDiagnostics);
    var copyBtn = $('btn-diag-copy');
    if (copyBtn) {
      copyBtn.addEventListener('click', function () {
        if (!lastDiagPayload) return;
        var text = JSON.stringify(lastDiagPayload, null, 2);
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(function () {
            showMessage('JSON скопирован в буфер обмена');
          });
        } else {
          var ta = document.createElement('textarea');
          ta.value = text;
          document.body.appendChild(ta);
          ta.select();
          try { document.execCommand('copy'); showMessage('JSON скопирован'); } catch (_) {}
          document.body.removeChild(ta);
        }
      });
    }
    var resetLotsBtn = $('btn-diag-reset-lots');
    if (resetLotsBtn) resetLotsBtn.addEventListener('click', function () { resetTask('lots_extraction'); });
    var resetSearchBtn = $('btn-diag-reset-search');
    if (resetSearchBtn) resetSearchBtn.addEventListener('click', function () { resetTask('supplier_search'); });
  }

  // ===========================================================================
  // LLM TRACE TAB (admin-only)
  // ===========================================================================

  var OPERATION_LABELS = {
    lots_extraction: 'Извлечение лотов',
    search_queries: 'Поисковые запросы',
    kp_extraction: 'Парсинг КП',
    bid_lots_extraction: 'Парсинг КП',
    perplexity_postprocess: 'Обработка Perplexity',
    compare_characteristics: 'Сравнение характеристик',
    supplier_search_perplexity: 'Поиск поставщиков',
    characteristic_matching: 'Матчинг характеристик',
    value_compliance: 'Проверка значений',
    doc_validation: 'Валидация документа',
    company_validation: 'Валидация компании',
    summarize_tz: 'Сводка ТЗ',
    lot_match_classify: 'Классификация лотов',
    param_match_classify: 'Классификация параметров',
    pdf_conversion: 'Распознавание PDF',
  };

  var _traceCache = {};

  function _fmtTokens(n) { return n != null ? n.toLocaleString('ru-RU') : '—'; }
  function _fmtCost(c) { return c != null ? '$' + c.toFixed(4) : '—'; }
  function _fmtDuration(ms) { return ms != null ? (ms / 1000).toFixed(1) + 's' : '—'; }

  function initLLMTrace() {
    var user = (typeof Auth !== 'undefined' && Auth.getUser) ? Auth.getUser() : null;
    if (!user || !user.is_admin) return;

    var searchBtn = $('btn-trace-search');
    var currentBtn = $('btn-trace-current');
    var searchInput = $('trace-purchase-search');

    if (searchBtn) searchBtn.addEventListener('click', function () {
      _searchTracePurchases(searchInput ? searchInput.value : '');
    });
    if (currentBtn) currentBtn.addEventListener('click', function () {
      if (currentPurchase && currentPurchase.id) {
        _loadPurchaseTrace(currentPurchase.id);
      } else {
        showError('Сначала выберите закупку');
      }
    });
    if (searchInput) searchInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') _searchTracePurchases(this.value);
    });

    _searchTracePurchases('');
  }

  function _searchTracePurchases(q) {
    var url = '/admin/trace/purchases';
    if (q) url += '?q=' + encodeURIComponent(q);
    API.apiFetch(url).then(function (list) {
      var el = $('trace-purchase-list');
      if (!el) return;
      if (!list || !list.length) {
        el.innerHTML = '<div class="empty-state">Нет закупок с LLM-вызовами</div>';
        return;
      }
      var active = list.filter(function (p) { return !p.is_archived; });
      var archived = list.filter(function (p) { return p.is_archived; });

      function _fmtDate(iso) { return iso ? iso.substring(0, 10).split('-').reverse().join('.') : '—'; }

      function _traceTableRows(items) {
        var h = '';
        for (var i = 0; i < items.length; i++) {
          var p = items[i];
          h += '<tr style="cursor:pointer;border-bottom:1px solid var(--border)" onmouseover="this.style.background=\'var(--accent-light)\'" onmouseout="this.style.background=\'\'" onclick="window._loadTraceForPurchase(' + p.id + ')">';
          h += '<td style="padding:6px 8px;font-weight:500">' + escapeHtml(p.name) + '</td>';
          h += '<td style="padding:6px 8px;text-align:center;color:var(--text-secondary);font-size:12px">' + _fmtDate(p.created_at) + '</td>';
          h += '<td style="padding:6px 8px;text-align:center;color:var(--text-secondary);font-size:12px">' + _fmtDate(p.last_call) + '</td>';
          h += '<td style="padding:6px 8px;text-align:right;color:var(--text-secondary)">' + p.call_count + '</td>';
          h += '<td style="padding:6px 8px;text-align:right;color:var(--text-secondary)">' + _fmtTokens(p.total_tokens) + '</td>';
          h += '<td style="padding:6px 8px;text-align:center">' + (p.has_traces ? '<span style="color:var(--success)">&#9679;</span>' : '<span style="color:var(--border)">&#9679;</span>') + '</td>';
          h += '</tr>';
        }
        return h;
      }

      var thStyle = 'font-size:11px;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-secondary);border-bottom:1px solid var(--border)';
      var thPad = 'text-align:center;padding:6px 8px;font-weight:500';
      var html = '';

      // Active purchases
      if (active.length) {
        html += '<table style="width:100%;border-collapse:collapse;font-size:13px">';
        html += '<thead><tr style="' + thStyle + '">';
        html += '<th style="text-align:left;padding:6px 8px;font-weight:500">Закупка</th>';
        html += '<th style="' + thPad + '">Создана</th>';
        html += '<th style="' + thPad + '">Посл. вызов</th>';
        html += '<th style="text-align:right;padding:6px 8px;font-weight:500;white-space:nowrap">Вызовы</th>';
        html += '<th style="text-align:right;padding:6px 8px;font-weight:500;white-space:nowrap">Токены</th>';
        html += '<th style="text-align:center;padding:6px 8px;font-weight:500">Trace</th>';
        html += '</tr></thead><tbody>' + _traceTableRows(active) + '</tbody></table>';
      } else {
        html += '<div style="color:var(--text-secondary);font-size:13px;padding:8px 0">Нет активных закупок с LLM-вызовами</div>';
      }

      // Archived purchases (collapsible)
      if (archived.length) {
        html += '<div style="margin-top:12px">';
        html += '<div style="cursor:pointer;display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text-secondary);font-weight:500;padding:6px 0" onclick="var t=this.nextElementSibling;var a=t.style.display===\'none\';t.style.display=a?\'\':\' none\';this.querySelector(\'span\').textContent=a?\'&#9660;\':\'&#9654;\'">';
        html += '<span>&#9654;</span> Архив (' + archived.length + ')';
        html += '</div>';
        html += '<div style="display:none">';
        html += '<table style="width:100%;border-collapse:collapse;font-size:13px;opacity:0.7">';
        html += '<tbody>' + _traceTableRows(archived) + '</tbody></table>';
        html += '</div></div>';
      }

      el.innerHTML = html;
    }).catch(function (err) {
      showError('Ошибка загрузки: ' + err.message);
    });
  }

  window._loadTraceForPurchase = function (pid) { _loadPurchaseTrace(pid); };

  function _loadPurchaseTrace(purchaseId) {
    API.apiFetch('/admin/trace/purchases/' + purchaseId).then(function (data) {
      _renderTraceSummary(data);
      _renderTraceTimeline(data);
      var summaryEl = $('trace-summary');
      if (summaryEl) summaryEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }).catch(function (err) {
      showError('Ошибка загрузки trace: ' + err.message);
    });
  }

  function _renderTraceSummary(data) {
    var el = $('trace-summary');
    if (!el) return;
    var s = data.summary;
    var ops = {};
    for (var i = 0; i < data.calls.length; i++) ops[data.calls[i].operation] = true;
    var pills = '';
    for (var op in ops) pills += '<span class="trace-op-pill">' + escapeHtml(op) + '</span> ';
    el.style.display = '';
    el.innerHTML =
      '<div class="trace-summary">' +
        '<div class="trace-summary-stat"><div class="trace-summary-val">' + s.total_calls + '</div><div class="trace-summary-label">Вызовов</div></div>' +
        '<div class="trace-summary-sep"></div>' +
        '<div class="trace-summary-stat"><div class="trace-summary-val">' + _fmtTokens(s.total_tokens) + '</div><div class="trace-summary-label">Токенов</div></div>' +
        '<div class="trace-summary-sep"></div>' +
        '<div class="trace-summary-stat"><div class="trace-summary-val">' + _fmtCost(s.total_cost_usd) + '</div><div class="trace-summary-label">Стоимость</div></div>' +
        '<div class="trace-summary-sep"></div>' +
        '<div class="trace-summary-stat"><div class="trace-summary-val">' + _fmtDuration(s.total_duration_ms) + '</div><div class="trace-summary-label">Время</div></div>' +
        '<div class="trace-summary-sep"></div>' +
        '<div style="display:flex;gap:6px;flex-wrap:wrap;flex:1">' + pills + '</div>' +
      '</div>';
  }

  function _renderTraceTimeline(data) {
    var el = $('trace-timeline');
    if (!el) return;
    var groups = {};
    var groupOrder = [];
    for (var i = 0; i < data.calls.length; i++) {
      var c = data.calls[i];
      var key = c.task_id || 'no_task';
      if (!groups[key]) { groups[key] = []; groupOrder.push(key); }
      groups[key].push(c);
    }
    var taskMap = {};
    for (var t = 0; t < data.tasks.length; t++) taskMap[data.tasks[t].id] = data.tasks[t];

    var html = '';
    for (var g = 0; g < groupOrder.length; g++) {
      var gKey = groupOrder[g];
      var calls = groups[gKey];
      var task = taskMap[gKey];
      var taskLabel = task ? 'Task #' + task.id + ' · ' + task.task_type : 'Без задачи';
      var taskStatus = task ? task.status : '';
      html += '<div class="trace-task-group"><div class="trace-task-header"><span>' + escapeHtml(taskLabel) + '</span>';
      if (taskStatus) {
        var sc = taskStatus === 'completed' ? 'status-active' : taskStatus === 'failed' ? 'status-warning' : 'status-search';
        html += '<span class="status ' + sc + '"><span class="status-dot"></span>' + taskStatus + '</span>';
      }
      html += '</div>';

      // Group consecutive calls with same operation+model into collapsible batches
      var ci = 0;
      while (ci < calls.length) {
        var batchOp = calls[ci].operation;
        var batchModel = calls[ci].model;
        var batchStart = ci;
        while (ci < calls.length && calls[ci].operation === batchOp && calls[ci].model === batchModel) ci++;
        var batchCalls = calls.slice(batchStart, ci);

        if (batchCalls.length <= 2) {
          // Few calls — render individually
          for (var bi = 0; bi < batchCalls.length; bi++) html += _renderTraceCard(batchCalls[bi]);
        } else {
          // Many calls — render as collapsible group
          var batchTokens = 0, batchCost = 0;
          for (var bs = 0; bs < batchCalls.length; bs++) {
            batchTokens += batchCalls[bs].total_tokens || 0;
            batchCost += batchCalls[bs].cost_usd || 0;
          }
          var opLabel = OPERATION_LABELS[batchOp] || batchOp;
          var batchId = 'trace-batch-' + gKey + '-' + batchStart;
          html += '<div class="trace-card" style="border-left-color:var(--text-secondary)">';
          html += '<div class="trace-card-header" onclick="var t=document.getElementById(\'' + batchId + '\');t.style.display=t.style.display===\'none\'?\'\':\'none\'">';
          html += '<div><div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">';
          html += '<span style="font-weight:600;font-size:14px">' + escapeHtml(opLabel) + '</span>';
          html += '<span style="font-size:11px;font-family:monospace;color:var(--text-secondary);background:var(--bg);padding:2px 6px;border-radius:4px">' + escapeHtml(batchOp) + '</span>';
          html += '<span style="background:var(--accent-light);color:var(--accent);font-size:11px;font-weight:600;padding:2px 8px;border-radius:10px">' + batchCalls.length + ' вызовов</span>';
          if (batchModel) html += '<span style="font-size:12px;color:var(--text-secondary);font-family:monospace;background:var(--bg);padding:2px 6px;border-radius:4px">' + escapeHtml(batchModel) + '</span>';
          html += '</div><div class="trace-stats">';
          html += '<span class="trace-tokens-in">' + _fmtTokens(batchTokens) + ' tokens</span>';
          html += '<span>' + _fmtCost(batchCost) + '</span>';
          html += '</div></div>';
          html += '<span style="font-size:12px;color:var(--text-secondary)">&#9660;</span></div>';
          html += '<div class="trace-card-body" id="' + batchId + '" style="display:none;border-top:1px solid var(--border)">';
          for (var bj = 0; bj < batchCalls.length; bj++) html += _renderTraceCard(batchCalls[bj]);
          html += '</div></div>';
        }
      }
      html += '</div>';
    }
    el.innerHTML = html || '<div class="empty-state">Нет LLM-вызовов по этой закупке</div>';
  }

  function _renderTraceCard(call) {
    var ec = call.success ? '' : ' trace-error';
    var opLabel = OPERATION_LABELS[call.operation] || call.operation;
    var h = '<div class="trace-card' + ec + '" id="trace-card-' + call.usage_id + '">';
    h += '<div class="trace-card-header" onclick="window._toggleTraceCard(' + call.usage_id + ',' + call.has_trace + ')">';
    h += '<div><div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">';
    h += '<span style="font-weight:600;font-size:14px">' + escapeHtml(opLabel) + '</span>';
    h += '<span style="font-size:11px;font-family:monospace;color:var(--text-secondary);background:var(--bg);padding:2px 6px;border-radius:4px">' + escapeHtml(call.operation) + '</span>';
    if (call.model) h += '<span style="font-size:12px;color:var(--text-secondary);font-family:monospace;background:var(--bg);padding:2px 6px;border-radius:4px">' + escapeHtml(call.model) + '</span>';
    h += '</div><div class="trace-stats">';
    h += '<span>' + call.created_at.substring(11, 19) + '</span>';
    h += '<span class="trace-tokens-in">' + _fmtTokens(call.prompt_tokens) + ' in</span><span style="opacity:0.4">&rarr;</span><span class="trace-tokens-out">' + _fmtTokens(call.completion_tokens) + ' out</span>';
    h += '<span>' + _fmtCost(call.cost_usd) + '</span><span>' + _fmtDuration(call.duration_ms) + '</span>';
    h += call.success ? '<span style="color:var(--success)">&#10003;</span>' : '<span style="color:var(--danger);font-weight:500">&#10007;</span>';
    h += '</div>';
    if (!call.success && call.error_message) h += '<div style="margin-top:6px;padding:6px 10px;background:var(--danger-bg);border-radius:4px;font-size:12px;color:var(--danger);font-family:monospace">' + escapeHtml(call.error_message) + '</div>';
    h += '</div><span style="font-size:12px;color:var(--text-secondary)">&#9654;</span></div>';
    h += '<div class="trace-card-body" id="trace-body-' + call.usage_id + '"></div></div>';
    return h;
  }

  window._toggleTraceCard = function (usageId, hasTrace) {
    var card = $('trace-card-' + usageId);
    if (!card) return;
    if (card.classList.contains('expanded')) { card.classList.remove('expanded'); return; }
    card.classList.add('expanded');
    var body = $('trace-body-' + usageId);
    if (!body || body.dataset.loaded) return;
    if (!hasTrace) {
      body.innerHTML = '<div class="trace-card-section" style="color:var(--text-secondary);font-size:13px;text-align:center;padding:20px">Трейс недоступен (LLM_TRACE_ENABLED был выключен)</div>';
      body.dataset.loaded = '1';
      return;
    }
    if (_traceCache[usageId]) { _renderTraceBody(body, _traceCache[usageId]); body.dataset.loaded = '1'; return; }
    body.innerHTML = '<div class="trace-card-section" style="text-align:center;padding:20px"><div class="spinner"></div> Загрузка...</div>';
    API.apiFetch('/admin/trace/calls/' + usageId).then(function (trace) {
      _traceCache[usageId] = trace;
      _renderTraceBody(body, trace);
      body.dataset.loaded = '1';
    }).catch(function (err) {
      body.innerHTML = '<div class="trace-card-section" style="color:var(--danger)">Ошибка: ' + escapeHtml(err.message) + '</div>';
    });
  };

  function _renderTraceBody(body, trace) {
    var h = '';
    if (trace.request_messages && trace.request_messages.length) {
      h += '<div class="trace-card-section"><div style="font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-secondary);margin-bottom:8px">Request</div>';
      for (var i = 0; i < trace.request_messages.length; i++) {
        var msg = trace.request_messages[i];
        var role = msg.role || 'unknown';
        h += '<div style="margin-bottom:8px"><div class="trace-role trace-role-' + role + '">' + role + '</div>';
        h += '<div class="trace-pre">' + escapeHtml(typeof msg.content === 'string' ? msg.content : JSON.stringify(msg.content, null, 2)) + '</div></div>';
      }
      h += '</div>';
    }
    if (trace.response_content != null) {
      h += '<div class="trace-card-section"><div style="font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-secondary);margin-bottom:8px">Response</div>';
      h += '<div class="trace-role trace-role-assistant">assistant</div>';
      var content = trace.response_content;
      var isJson = false;
      try { JSON.parse(content); isJson = true; } catch (_) {}
      h += isJson ? '<div class="trace-pre trace-pre-json">' + escapeHtml(JSON.stringify(JSON.parse(content), null, 2)) + '</div>'
                  : '<div class="trace-pre">' + escapeHtml(content) + '</div>';
      h += '</div>';
    }
    h += '<div class="trace-card-actions">';
    h += '<button class="btn btn-ghost btn-sm" onclick="window._copyTraceField(' + trace.usage_id + ',\'request\')">Копировать запрос</button>';
    h += '<button class="btn btn-ghost btn-sm" onclick="window._copyTraceField(' + trace.usage_id + ',\'response\')">Копировать ответ</button>';
    h += '</div>';
    body.innerHTML = h;
  }

  window._copyTraceField = function (usageId, field) {
    var trace = _traceCache[usageId];
    if (!trace) return;
    var text = field === 'request' ? JSON.stringify(trace.request_messages, null, 2) : (trace.response_content || '');
    if (navigator.clipboard) navigator.clipboard.writeText(text).then(function () { showMessage('Скопировано'); });
  };


  // ===========================================================================
  // LLM SANDBOX TAB (admin-only)
  // ===========================================================================

  var _sandboxState = { files: [], mode: 'stepwise', selectedStep: null, steps: [], currentStep: 0, results: [], running: false };

  var SANDBOX_STEPS = [
    { id: 'lots_extraction', name: 'Извлечение лотов', module: 'M1', input: 'tz', desc: 'Парсинг ТЗ в лоты' },
    { id: 'search_queries', name: 'Поисковые запросы', module: 'M1', input: 'lots', desc: 'Генерация запросов из лотов' },
    { id: 'kp_extraction', name: 'Парсинг КП', module: 'M3', input: 'kp', desc: 'Парсинг КП в лоты' },
    { id: 'compare_characteristics', name: 'Сравнение с ГИСП', module: 'M4', input: 'json', desc: 'Сравнение характеристик с реестром' },
    { id: 'perplexity_postprocess', name: 'Обработка Perplexity', module: 'M1', input: 'text', desc: 'Извлечение контактов' },
  ];

  function initLLMSandbox() {
    var user = (typeof Auth !== 'undefined' && Auth.getUser) ? Auth.getUser() : null;
    if (!user || !user.is_admin) return;
    var tzZone = $('sandbox-upload-tz');
    var kpZone = $('sandbox-upload-kp');
    if (tzZone) tzZone.addEventListener('click', function () { $('inp-sandbox-tz').click(); });
    if (kpZone) kpZone.addEventListener('click', function () { $('inp-sandbox-kp').click(); });
    var inpTz = $('inp-sandbox-tz');
    if (inpTz) inpTz.addEventListener('change', function () { if (this.files[0]) { _addSandboxFile(this.files[0], 'tz'); this.value = ''; } });
    var inpKp = $('inp-sandbox-kp');
    if (inpKp) inpKp.addEventListener('change', function () {
      var files = Array.from(this.files || []);
      for (var i = 0; i < files.length; i++) _addSandboxFile(files[i], 'kp');
      this.value = '';
    });
    _renderSandboxModes();
  }

  function _addSandboxFile(file, type) {
    var entry = { name: file.name, type: type, text: null, converting: true, size: file.size };
    _sandboxState.files.push(entry);
    _renderSandboxFiles();
    API.convertTechTaskFile(file).then(function (data) {
      entry.text = data.markdown; entry.converting = false; entry.chars = data.markdown.length;
      _renderSandboxFiles(); _updateSandboxSteps();
    }).catch(function (err) {
      entry.converting = false; entry.error = err.message;
      _renderSandboxFiles();
    });
  }

  function _renderSandboxFiles() {
    var el = $('sandbox-files');
    if (!el) return;
    var h = '';
    for (var i = 0; i < _sandboxState.files.length; i++) {
      var f = _sandboxState.files[i];
      h += '<span class="sandbox-file-chip"><span class="sandbox-file-type sandbox-file-type-' + f.type + '">' + f.type.toUpperCase() + '</span>' + escapeHtml(f.name);
      if (f.converting) h += ' <span class="spinner" style="width:12px;height:12px"></span>';
      else if (f.error) h += ' <span style="color:var(--danger);font-size:11px">Ошибка</span>';
      else if (f.chars) h += ' <span style="font-size:11px;color:var(--text-secondary)">(' + f.chars.toLocaleString('ru-RU') + ' сим.)</span>';
      h += ' <span style="cursor:pointer;color:var(--text-secondary);font-size:16px" onclick="window._removeSandboxFile(' + i + ')">&times;</span></span>';
    }
    el.innerHTML = h;
  }

  window._removeSandboxFile = function (idx) { _sandboxState.files.splice(idx, 1); _renderSandboxFiles(); _updateSandboxSteps(); };

  function _renderSandboxModes() {
    var el = $('sandbox-mode-selector');
    if (!el) return;
    var modes = [
      { id: 'pipeline', title: '&#9654; Весь конвейер', desc: 'Все шаги автоматически' },
      { id: 'single', title: '&#127919; Один шаг', desc: 'Выбрать конкретный шаг' },
      { id: 'stepwise', title: '&#128694; Пошагово', desc: 'Шаг за шагом с «Дальше»' },
    ];
    var h = '<div class="sandbox-mode">';
    for (var i = 0; i < modes.length; i++) {
      var m = modes[i];
      h += '<div class="sandbox-mode-option' + (m.id === _sandboxState.mode ? ' active' : '') + '" onclick="window._setSandboxMode(\'' + m.id + '\')">';
      h += '<div class="sandbox-mode-title">' + m.title + '</div><div class="sandbox-mode-desc">' + m.desc + '</div></div>';
    }
    el.innerHTML = h + '</div>';
  }

  window._setSandboxMode = function (mode) {
    _sandboxState.mode = mode;
    _renderSandboxModes();
    var stepSel = $('sandbox-step-selector');
    if (stepSel) stepSel.style.display = mode === 'single' ? '' : 'none';
    _updateSandboxSteps();
  };

  function _updateSandboxSteps() {
    var hasTz = _sandboxState.files.some(function (f) { return f.type === 'tz' && f.text; });
    var hasKp = _sandboxState.files.some(function (f) { return f.type === 'kp' && f.text; });
    var anyReady = _sandboxState.files.some(function (f) { return f.text; });

    var available = [];
    for (var i = 0; i < SANDBOX_STEPS.length; i++) {
      var s = SANDBOX_STEPS[i];
      var ok = s.input === 'tz' ? hasTz : s.input === 'kp' ? hasKp : s.input === 'lots' ? hasTz : true;
      available.push({ step: s, available: ok });
    }

    if (_sandboxState.mode === 'single') {
      var stepEl = $('sandbox-step-selector');
      if (stepEl) {
        var h = '<div class="sandbox-step-grid">';
        for (var j = 0; j < available.length; j++) {
          var a = available[j];
          h += '<div class="sandbox-step-chip' + (_sandboxState.selectedStep === a.step.id ? ' selected' : '') + (a.available ? '' : ' dimmed') + '" onclick="window._selectSandboxStep(\'' + a.step.id + '\')">';
          h += '<div class="sandbox-step-module" style="background:var(--accent-light);color:var(--accent)">' + a.step.module + '</div>';
          h += '<div class="sandbox-step-name">' + escapeHtml(a.step.name) + '</div>';
          h += '<div class="sandbox-step-desc">' + escapeHtml(a.step.desc) + '</div></div>';
        }
        stepEl.innerHTML = h + '</div>';
      }
    }

    var pipeline = [];
    if (hasTz) { pipeline.push('lots_extraction'); pipeline.push('search_queries'); }
    if (hasKp) pipeline.push('kp_extraction');
    _sandboxState.steps = pipeline;

    var runBar = $('sandbox-run-bar');
    if (runBar) {
      if (!anyReady) {
        runBar.innerHTML = '<div style="color:var(--text-secondary);font-size:13px">Загрузите файл для начала</div>';
      } else if (_sandboxState.mode === 'single') {
        var sn = _sandboxState.selectedStep ? (OPERATION_LABELS[_sandboxState.selectedStep] || _sandboxState.selectedStep) : '';
        runBar.innerHTML = '<button class="btn btn-primary" ' + (_sandboxState.selectedStep ? '' : 'disabled') + ' onclick="window._runSandboxSingle()">&#9654; Запустить' + (sn ? ': ' + sn : '') + '</button>';
      } else {
        var lb = _sandboxState.mode === 'pipeline' ? 'Запустить конвейер' : 'Запустить пошагово';
        runBar.innerHTML = '<button class="btn btn-primary" onclick="window._runSandboxPipeline()">&#9654; ' + lb + '</button>' +
          '<span style="font-size:12px;color:var(--text-secondary);margin-left:8px">' + pipeline.length + ' шагов: ' + pipeline.join(' &rarr; ') + '</span>';
      }
    }
  }

  window._selectSandboxStep = function (stepId) { _sandboxState.selectedStep = stepId; _updateSandboxSteps(); };

  window._runSandboxSingle = function () {
    var step = _sandboxState.selectedStep;
    if (!step) return;
    _runSandboxStep(step, _getSandboxInput(step), null, function (res) {
      _sandboxState.results = [res]; _renderSandboxResults();
    });
  };

  window._runSandboxPipeline = function () {
    _sandboxState.results = []; _sandboxState.currentStep = 0; _sandboxState.running = true;
    if (_sandboxState.mode === 'pipeline') _runNextPipelineStep(); else _runStepwiseStep();
  };

  function _runNextPipelineStep() {
    var idx = _sandboxState.currentStep;
    if (idx >= _sandboxState.steps.length) { _sandboxState.running = false; _renderSandboxResults(); _renderSandboxStepper(); return; }
    _renderSandboxStepper();
    _runSandboxStep(_sandboxState.steps[idx], _getStepInput(idx), null, function (res) {
      _sandboxState.results.push(res); _sandboxState.currentStep++; _renderSandboxResults(); _runNextPipelineStep();
    });
  }

  function _runStepwiseStep() {
    var idx = _sandboxState.currentStep;
    if (idx >= _sandboxState.steps.length) { _sandboxState.running = false; _renderSandboxControls(); return; }
    _renderSandboxStepper(); _renderSandboxControls();
    _runSandboxStep(_sandboxState.steps[idx], _getStepInput(idx), null, function (res) {
      _sandboxState.results.push(res); _sandboxState.running = false;
      _renderSandboxResults(); _renderSandboxStepper(); _renderSandboxControls();
    });
  }

  window._sandboxNext = function () { _sandboxState.currentStep++; _sandboxState.running = true; _runStepwiseStep(); };

  window._sandboxEdit = function (idx) {
    var res = _sandboxState.results[idx];
    if (!res) return;
    var el = $('sandbox-results');
    if (!el) return;
    var json = JSON.stringify(res.result, null, 2);
    el.innerHTML += '<div class="sandbox-result" style="border-left-color:var(--warning)">' +
      '<div style="padding:12px 16px"><span style="font-weight:600">' + escapeHtml(res.step) + '</span> — <span style="color:var(--warning)">редактирование</span></div>' +
      '<div style="padding:0 16px 12px"><textarea class="sandbox-edit-area" id="sandbox-edit-textarea">' + escapeHtml(json) + '</textarea>' +
      '<div id="sandbox-edit-validation" style="font-size:11px;margin-top:4px;color:var(--success)">JSON валиден &#10003;</div></div>' +
      '<div style="padding:8px 16px;border-top:1px solid var(--border);background:#fafbfc;display:flex;gap:8px;justify-content:flex-end">' +
      '<button class="btn btn-secondary btn-sm" onclick="window._cancelSandboxEdit()">Отменить</button>' +
      '<button class="btn btn-primary" onclick="window._saveSandboxEdit(' + idx + ')">Сохранить и продолжить &#8594;</button></div></div>';
    var ta = $('sandbox-edit-textarea');
    if (ta) ta.addEventListener('input', function () {
      var val = $('sandbox-edit-validation');
      try { JSON.parse(this.value); if (val) { val.style.color = 'var(--success)'; val.innerHTML = 'JSON валиден &#10003;'; } }
      catch (e) { if (val) { val.style.color = 'var(--danger)'; val.textContent = 'Ошибка: ' + e.message; } }
    });
  };

  window._cancelSandboxEdit = function () { _renderSandboxResults(); _renderSandboxControls(); };

  window._saveSandboxEdit = function (idx) {
    var ta = $('sandbox-edit-textarea');
    if (!ta) return;
    try {
      _sandboxState.results[idx].result = JSON.parse(ta.value);
      _sandboxState.results[idx].edited = true;
      _renderSandboxResults(); window._sandboxNext();
    } catch (e) { showError('Невалидный JSON: ' + e.message); }
  };

  function _getSandboxInput(step) {
    var tzFile = _sandboxState.files.find(function (f) { return f.type === 'tz' && f.text; });
    var kpFile = _sandboxState.files.find(function (f) { return f.type === 'kp' && f.text; });
    if (step === 'lots_extraction' || step === 'search_queries') return tzFile ? tzFile.text : '';
    if (step === 'kp_extraction') return kpFile ? kpFile.text : '';
    return '';
  }

  function _getStepInput(idx) {
    var step = _sandboxState.steps[idx];
    if (idx > 0 && _sandboxState.results[idx - 1] && _sandboxState.results[idx - 1].result) {
      var prev = _sandboxState.results[idx - 1].result;
      if (step === 'search_queries' && prev.lots) {
        return prev.lots.map(function (l) { return l.name + (l.count ? ' (' + l.count + ' ' + (l.units || 'шт') + ')' : ''); }).join('\n');
      }
    }
    return _getSandboxInput(step);
  }

  function _runSandboxStep(step, inputText, inputJson, callback) {
    var fd = new FormData();
    fd.append('step', step);
    if (inputText) fd.append('input_text', inputText);
    if (inputJson) fd.append('input_json', inputJson);
    API.apiFetch('/admin/sandbox/run', { method: 'POST', body: fd }).then(callback).catch(function (err) {
      callback({ step: step, success: false, result: null, usage: {}, trace: null, error: err.message });
    });
  }

  function _renderSandboxStepper() {
    var el = $('sandbox-stepper');
    if (!el) return;
    if (!_sandboxState.steps.length) { el.style.display = 'none'; return; }
    el.style.display = '';
    var h = '<div class="sandbox-stepper">';
    for (var i = 0; i < _sandboxState.steps.length; i++) {
      var st = i < _sandboxState.currentStep ? 'done' : i === _sandboxState.currentStep ? (_sandboxState.running ? 'active' : (_sandboxState.results[i] ? 'done' : 'active')) : 'pending';
      if (i > 0) h += '<span class="stepper-arrow">&rarr;</span>';
      h += '<div class="stepper-step ' + st + '"><div class="stepper-dot">' + (st === 'done' ? '&#10003;' : (i + 1)) + '</div>';
      h += '<span class="stepper-label">' + escapeHtml(OPERATION_LABELS[_sandboxState.steps[i]] || _sandboxState.steps[i]) + '</span></div>';
    }
    el.innerHTML = h + '</div>';
  }

  function _renderSandboxResults() {
    var el = $('sandbox-results');
    if (!el) return;
    var h = '';
    for (var i = 0; i < _sandboxState.results.length; i++) {
      var r = _sandboxState.results[i];
      var opLabel = OPERATION_LABELS[r.step] || r.step;
      h += '<div class="sandbox-result' + (r.success ? '' : ' error') + ' expanded">';
      h += '<div class="sandbox-result-header" onclick="this.parentElement.classList.toggle(\'expanded\')"><div>';
      h += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px"><span style="font-weight:600">' + escapeHtml(opLabel) + '</span>';
      h += '<span style="font-family:monospace;font-size:11px;color:var(--text-secondary)">' + escapeHtml(r.step) + '</span>';
      if (r.edited) h += '<span style="font-size:11px;color:var(--warning);font-weight:500">&#9998; отредактировано</span>';
      h += '</div>';
      if (r.usage) {
        h += '<div class="trace-stats">';
        if (r.usage.model) h += '<span>' + escapeHtml(r.usage.model) + '</span>';
        h += '<span class="trace-tokens-in">' + _fmtTokens(r.usage.prompt_tokens) + ' in</span><span style="opacity:0.4">&rarr;</span>';
        h += '<span class="trace-tokens-out">' + _fmtTokens(r.usage.completion_tokens) + ' out</span>';
        h += '<span>' + _fmtCost(r.usage.cost_usd) + '</span><span>' + _fmtDuration(r.usage.duration_ms) + '</span></div>';
      }
      h += '</div><span style="font-size:12px;color:var(--text-secondary)">&#9654;</span></div>';
      h += '<div class="sandbox-result-body">';
      if (r.trace && r.trace.request_messages) {
        h += '<div class="trace-card-section"><div style="font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-secondary);margin-bottom:8px">Request</div>';
        for (var m = 0; m < r.trace.request_messages.length; m++) {
          var msg = r.trace.request_messages[m];
          h += '<div style="margin-bottom:8px"><div class="trace-role trace-role-' + (msg.role || 'user') + '">' + (msg.role || 'user') + '</div>';
          h += '<div class="trace-pre">' + escapeHtml(typeof msg.content === 'string' ? msg.content : JSON.stringify(msg.content, null, 2)) + '</div></div>';
        }
        h += '</div>';
      }
      h += '<div class="trace-card-section"><div style="font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;color:var(--text-secondary);margin-bottom:8px">Результат</div>';
      if (r.success && r.result != null) {
        h += '<div class="trace-pre trace-pre-json">' + escapeHtml(typeof r.result === 'string' ? r.result : JSON.stringify(r.result, null, 2)) + '</div>';
      } else if (r.error) {
        h += '<div style="color:var(--danger);font-family:monospace;font-size:13px">' + escapeHtml(r.error) + '</div>';
      }
      h += '</div></div></div>';
    }
    el.innerHTML = h;
  }

  function _renderSandboxControls() {
    var el = $('sandbox-controls');
    if (!el) return;
    if (_sandboxState.mode !== 'stepwise' || !_sandboxState.steps.length) { el.style.display = 'none'; return; }
    el.style.display = '';
    var idx = _sandboxState.currentStep, total = _sandboxState.steps.length;
    var isDone = idx >= total;
    var canNext = !_sandboxState.running && _sandboxState.results.length > 0 && _sandboxState.results.length === idx && !isDone;
    var tT = 0, tC = 0, tD = 0;
    for (var i = 0; i < _sandboxState.results.length; i++) {
      var u = _sandboxState.results[i].usage || {};
      tT += u.total_tokens || 0; tC += u.cost_usd || 0; tD += u.duration_ms || 0;
    }
    var h = '<div class="sandbox-sticky-controls"><div style="display:flex;gap:12px;align-items:center;font-size:12px;color:var(--text-secondary)">';
    h += '<span>Шаг ' + Math.min(idx + 1, total) + ' из ' + total + '</span><span>&middot;</span>';
    h += '<span>' + _fmtTokens(tT) + ' токенов</span><span>&middot;</span><span>' + _fmtCost(tC) + '</span><span>&middot;</span><span>' + _fmtDuration(tD) + '</span>';
    h += '</div><div style="display:flex;gap:8px;align-items:center">';
    if (canNext) h += '<button class="btn btn-secondary btn-sm" onclick="window._sandboxEdit(' + (_sandboxState.results.length - 1) + ')">&#9998; Редактировать</button>';
    if (isDone) h += '<button class="btn btn-primary" disabled>&#10003; Завершено</button>';
    else if (_sandboxState.running) h += '<button class="btn btn-primary" disabled><span class="spinner" style="width:14px;height:14px"></span> Выполняется...</button>';
    else if (canNext) h += '<button class="btn btn-primary" onclick="window._sandboxNext()">Дальше &#8594;</button>';
    h += '</div></div>';
    el.innerHTML = h;
  }


  document.addEventListener('DOMContentLoaded', function () {
    initModals();
    initTabs();
    initHeader();
    initDashboard();
    initPurchaseSelector();
    initCreatePurchase();
    initTzUpload();
    initAddLot();
    initSupplierSearch();
    initAddSupplier();
    initEmailDraft();
    initAddBid();
    initComparison();
    initRegime();
    initLotsDiagnostics();
    initLLMTrace();
    initLLMSandbox();
    loadPurchases();
    loadDashboard();
  });

})();
