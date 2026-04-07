(function () {
  'use strict';

  // ── State ──────────────────────────────────────────────────────────
  var purchases = [];
  var currentPurchase = null;
  var currentLots = [];
  var currentSuppliers = [];
  var currentBids = [];
  var selectedBidId = null;
  var lotsPollingTimer = null;
  var searchPollingTimer = null;
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
      });
    }
  }

  // ── Header: user info, logout ─────────────────────────────────────

  function initHeader() {
    var user = Auth.getUser();
    if (user) {
      $('user-info').textContent = user.email || user.full_name || '';
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
    updateSelectorText();
    renderPurchaseDropdown();
    // Load all data
    loadLots();
    loadSuppliers();
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
      openModal('modal-new-purchase');
    });

    // File upload zone click
    $('purchase-tz-upload').addEventListener('click', function () {
      $('inp-purchase-tz-file').click();
    });
    $('inp-purchase-tz-file').addEventListener('change', function () {
      var file = this.files[0];
      if (file) {
        $('purchase-tz-label').textContent = file.name;
      }
    });

    $('form-new-purchase').addEventListener('submit', async function (e) {
      e.preventDefault();
      var name = $('inp-purchase-name').value.trim();
      var termsText = $('inp-terms-text').value.trim();
      var fileInput = $('inp-purchase-tz-file');
      var file = fileInput.files[0];

      if (!name) { showError('Введите название закупки'); return; }

      try {
        // Convert file if uploaded
        if (file) {
          showMessage('Конвертация документа...');
          var converted = await API.convertTechTaskFile(file);
          if (converted && converted.markdown) {
            termsText = termsText ? termsText + '\n\n' + converted.markdown : converted.markdown;
          }
        }

        var body = { custom_name: name };
        if (termsText) body.terms_text = termsText;
        var newPurchase = await API.apiFetch('/purchases', { method: 'POST', body: body });
        showMessage('Закупка создана');
        closeModal('modal-new-purchase');
        this.reset();
        $('purchase-tz-label').textContent = 'Нажмите для загрузки';
        purchases.unshift(newPurchase);
        selectPurchase(newPurchase);
      } catch (e) {
        showError('Ошибка создания закупки: ' + e.message);
      }
    });
  }

  // ── TZ Upload (on search tab) ─────────────────────────────────────

  function initTzUpload() {
    $('tz-upload-zone').addEventListener('click', function () {
      $('inp-tz-file').click();
    });

    $('inp-tz-file').addEventListener('change', async function () {
      var file = this.files[0];
      if (!file || !currentPurchase) return;

      try {
        showMessage('Конвертация документа...');
        var converted = await API.convertTechTaskFile(file);
        if (converted && converted.markdown) {
          var existingTerms = currentPurchase.terms_text || '';
          var newTerms = existingTerms ? existingTerms + '\n\n' + converted.markdown : converted.markdown;
          await API.apiFetch('/purchases/' + currentPurchase.id, {
            method: 'PATCH',
            body: { terms_text: newTerms },
          });
          currentPurchase.terms_text = newTerms;
          showMessage('ТЗ загружено, обновляем лоты...');
          loadLots();
        }
      } catch (e) {
        showError('Ошибка загрузки ТЗ: ' + e.message);
      }
      this.value = '';
    });
  }

  // ── Lots ───────────────────────────────────────────────────────────

  async function loadLots() {
    if (!currentPurchase) return;
    try {
      var resp = await API.apiFetch('/purchases/' + currentPurchase.id + '/lots');
      var status = resp.status;
      currentLots = resp.lots || [];
      renderLots();
      updateLotsStatus(status);
      // Poll if processing
      if (status === 'queued' || status === 'in_progress') {
        lotsPollingTimer = setTimeout(loadLots, 3000);
      }
    } catch (e) {
      showError('Ошибка загрузки лотов: ' + e.message);
    }
  }

  function updateLotsStatus(status) {
    var statusEl = $('lots-status');
    var textEl = $('lots-status-text');
    statusEl.className = 'status';
    if (status === 'done' || status === 'ready') {
      statusEl.classList.add('status-active');
      textEl.textContent = currentLots.length + ' распознано';
      // Update badge
      var badge = $('badge-search');
      if (currentLots.length > 0) {
        badge.textContent = currentLots.length;
        badge.classList.remove('hidden');
      }
    } else if (status === 'queued' || status === 'in_progress') {
      statusEl.classList.add('status-search');
      textEl.textContent = 'Обработка...';
    } else {
      statusEl.classList.add('status-draft');
      textEl.textContent = '--';
    }
  }

  function renderLots() {
    var container = $('lots-container');
    if (!currentLots.length) {
      container.innerHTML = '<div class="empty-state">Загрузите ТЗ или добавьте лоты вручную</div>';
      return;
    }
    var html = '';
    for (var i = 0; i < currentLots.length; i++) {
      var lot = currentLots[i];
      var paramCount = lot.parameters ? lot.parameters.length : 0;
      html += '<div class="lot-item" data-lot-index="' + i + '">' +
        '<div class="lot-num">' + (i + 1) + '</div>' +
        '<div class="lot-info">' +
        '<div class="lot-name">' + escapeHtml(lot.name) + '</div>' +
        '<div class="lot-meta">' + paramCount + ' параметр' + pluralParams(paramCount) + '</div>' +
        '</div></div>';
    }
    container.innerHTML = html;
    // Click to show detail
    var items = container.querySelectorAll('.lot-item');
    for (var j = 0; j < items.length; j++) {
      items[j].addEventListener('click', function () {
        var idx = parseInt(this.getAttribute('data-lot-index'), 10);
        showLotDetail(currentLots[idx]);
      });
    }
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
      checkSearchStatus();
    } catch (e) {
      showError('Ошибка загрузки поставщиков: ' + e.message);
    }
  }

  async function checkSearchStatus() {
    if (!currentPurchase) return;
    try {
      var state = await API.apiFetch('/purchases/' + currentPurchase.id + '/suppliers/search');
      if (state && state.status) {
        renderSearchStatus(state);
        if (state.status === 'queued' || state.status === 'in_progress') {
          searchPollingTimer = setTimeout(function () {
            checkSearchStatus();
            loadSuppliers();
          }, 3000);
        }
      }
    } catch (_) {
      // No active search — that's fine
      $('search-status').classList.add('hidden');
    }
  }

  function renderSearchStatus(state) {
    var statusEl = $('search-status');
    statusEl.classList.remove('hidden');
    var statusMap = {
      queued: 'В очереди...',
      in_progress: 'Поиск идёт...',
      done: 'Поиск завершён',
      failed: 'Ошибка поиска',
    };
    var text = statusMap[state.status] || state.status;

    if (state.status === 'queued' || state.status === 'in_progress') {
      statusEl.className = 'search-status';
      statusEl.innerHTML = '<div class="spinner"></div><div><strong>' + text + '</strong></div>';
    } else if (state.status === 'done') {
      statusEl.className = 'search-status';
      statusEl.style.background = 'var(--success-bg)';
      statusEl.innerHTML = '<div style="color:var(--success);font-size:16px">&#10003;</div><div><strong style="color:var(--success)">' + text + '</strong></div>';
    } else if (state.status === 'failed') {
      statusEl.className = 'search-status';
      statusEl.style.background = 'var(--danger-bg)';
      statusEl.innerHTML = '<div style="color:var(--danger);font-size:16px">&#10007;</div><div><strong style="color:var(--danger)">' + text + '</strong></div>';
    }

    if (state.queries && state.queries.length) {
      statusEl.innerHTML += '<div style="font-size:12px;color:var(--text-secondary);margin-left:auto">Запросы: ' +
        state.queries.map(function (q) { return escapeHtml(q); }).join(', ') + '</div>';
    }
  }

  function renderOwnSuppliers() {
    var container = $('own-suppliers-container');
    var ownSuppliers = currentSuppliers.filter(function (s) { return s.source === 'manual'; });
    if (!ownSuppliers.length) {
      container.innerHTML = '<div class="info-block">&#128100; Добавьте своих поставщиков — им тоже будет направлен запрос ТКП</div>';
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
      '<th>Поставщик</th><th>Сайт</th><th>Источник</th><th>Причина</th><th>Контакты</th></tr></thead><tbody>';
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
        '<td class="supplier-contacts" id="contacts-' + s.id + '"><button type="button" class="btn btn-sm btn-secondary btn-load-contacts" data-sid="' + s.id + '">Показать</button></td>' +
        '</tr>';
    }
    html += '</tbody></table>';
    container.innerHTML = html;

    // Bind contact loaders
    var contactBtns = container.querySelectorAll('.btn-load-contacts');
    for (var j = 0; j < contactBtns.length; j++) {
      contactBtns[j].addEventListener('click', function () {
        var sid = this.getAttribute('data-sid');
        loadContacts(sid);
      });
    }
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
          searchPollingTimer = setTimeout(function () {
            checkSearchStatus();
            loadSuppliers();
          }, 3000);
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
    if (!currentBids.length) {
      container.innerHTML = '<div class="empty-state">Коммерческие предложения пока не загружены</div>';
      return;
    }
    var html = '<div class="proposals-grid">';
    for (var i = 0; i < currentBids.length; i++) {
      var bid = currentBids[i];
      var lotCount = bid.lots ? bid.lots.length : 0;
      html += '<div class="proposal-card">' +
        '<div class="proposal-supplier">' + escapeHtml(bid.supplier_name || 'Поставщик') + '</div>' +
        '<div class="proposal-date">' + (bid.supplier_contact ? escapeHtml(bid.supplier_contact) : '') + '</div>' +
        '<div class="proposal-items">' + lotCount + ' позици' + (lotCount === 1 ? 'я' : lotCount < 5 ? 'и' : 'й') + '</div>' +
        '</div>';
    }
    html += '</div>';
    container.innerHTML = html;
  }

  function renderBidSelector() {
    var container = $('comparison-bid-selector');
    if (!currentBids.length) {
      container.innerHTML = '<div class="empty-state">Сначала загрузите КП во вкладке «Письма и КП»</div>';
      $('btn-compare').disabled = true;
      return;
    }
    var html = '<div class="comp-suppliers-list">';
    for (var i = 0; i < currentBids.length; i++) {
      var bid = currentBids[i];
      var active = selectedBidId === bid.id ? ' active' : '';
      html += '<div class="comp-supplier-tab' + active + '" data-bid-id="' + bid.id + '">' +
        '<span class="comp-supplier-indicator comp-supplier-indicator--pending"></span>' +
        '<div>' +
        '<div class="comp-supplier-tab-name">' + escapeHtml(bid.supplier_name || 'Поставщик') + '</div>' +
        '<div class="comp-supplier-tab-meta">' + (bid.lots ? bid.lots.length : 0) + ' позиций</div>' +
        '</div></div>';
    }
    html += '</div>';
    container.innerHTML = html;

    var cards = container.querySelectorAll('.comp-supplier-tab');
    for (var j = 0; j < cards.length; j++) {
      cards[j].addEventListener('click', function () {
        selectedBidId = parseInt(this.getAttribute('data-bid-id'), 10);
        $('btn-compare').disabled = false;
        renderBidSelector();
      });
    }
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
          var converted = await API.convertTechTaskFile(file);
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
      if (!currentPurchase || !selectedBidId) return;
      try {
        this.disabled = true;
        $('comparison-results').innerHTML = '<div class="card"><div class="card-body"><div class="empty-state">Сравнение запущено...</div></div></div>';
        await API.apiFetch('/purchases/' + currentPurchase.id + '/bids/' + selectedBidId + '/comparison', {
          method: 'POST',
        });
        pollComparison();
      } catch (e) {
        showError('Ошибка запуска сравнения: ' + e.message);
        this.disabled = false;
      }
    });
  }

  async function pollComparison() {
    if (!currentPurchase || !selectedBidId) return;
    try {
      var result = await API.apiFetch('/purchases/' + currentPurchase.id + '/bids/' + selectedBidId + '/comparison');
      if (result.status === 'queued' || result.status === 'in_progress') {
        $('comparison-results').innerHTML = '<div class="card"><div class="card-body"><div class="search-status"><div class="spinner"></div><div><strong>Сравнение в процессе...</strong></div></div></div></div>';
        comparisonPollingTimer = setTimeout(pollComparison, 3000);
      } else if (result.status === 'done') {
        renderComparison(result.rows || []);
        $('btn-compare').disabled = false;
      } else {
        $('comparison-results').innerHTML = '<div class="card"><div class="card-body"><div class="info-block" style="background:var(--danger-bg);color:var(--danger)">Ошибка сравнения</div></div></div>';
        $('btn-compare').disabled = false;
      }
    } catch (e) {
      showError('Ошибка получения результатов сравнения: ' + e.message);
      $('btn-compare').disabled = false;
    }
  }

  function renderComparison(rows) {
    if (!rows.length) {
      $('comparison-results').innerHTML = '<div class="card"><div class="card-body"><div class="empty-state">Нет данных для сравнения</div></div></div>';
      return;
    }

    var html = '';
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      var totalChars = row.characteristic_rows ? row.characteristic_rows.length : 0;
      var matchedChars = 0;
      var unmatchedChars = 0;

      if (row.characteristic_rows) {
        for (var m = 0; m < row.characteristic_rows.length; m++) {
          if (row.characteristic_rows[m].status === 'matched') matchedChars++;
          else unmatchedChars++;
        }
      }

      var lotIndicator = unmatchedChars > 0 ? 'comp-lot-indicator--warn' : 'comp-lot-indicator--ok';
      var lotBadgeClass = unmatchedChars > 0 ? 'comp-lot-badge--warn' : 'comp-lot-badge--ok';
      var lotBadgeText = unmatchedChars > 0 ? unmatchedChars + ' расхождени' + (unmatchedChars === 1 ? 'е' : unmatchedChars < 5 ? 'я' : 'й') : 'Всё совпадает';

      html += '<div class="card section-gap"><div class="comp-lot" data-expanded="true">' +
        '<div class="comp-lot-header" onclick="this.parentElement.dataset.expanded = this.parentElement.dataset.expanded === \'true\' ? \'false\' : \'true\'; var body = this.nextElementSibling; body.style.display = this.parentElement.dataset.expanded === \'true\' ? \'block\' : \'none\'; this.querySelector(\'.comp-lot-arrow\').style.transform = this.parentElement.dataset.expanded === \'true\' ? \'\' : \'rotate(-90deg)\'">' +
        '<div class="comp-lot-header-left">' +
        '<span class="comp-lot-arrow">&#9660;</span>' +
        '<span class="comp-lot-indicator ' + lotIndicator + '"></span>' +
        '<span class="comp-lot-title">' + escapeHtml(row.lot_name) + '</span>' +
        '</div>' +
        '<div class="comp-lot-header-right">' +
        '<span class="comp-lot-stat">' + matchedChars + ' из ' + totalChars + ' совпадают</span>' +
        '<span class="comp-lot-badge ' + lotBadgeClass + '">' + lotBadgeText + '</span>' +
        '</div></div>';

      if (row.characteristic_rows && row.characteristic_rows.length) {
        html += '<div class="comp-lot-body"><table class="comparison-table">' +
          '<thead><tr><th>Характеристика</th><th>Требование ТЗ</th><th>Предложение КП</th></tr></thead><tbody>';
        for (var j = 0; j < row.characteristic_rows.length; j++) {
          var cr = row.characteristic_rows[j];
          var statusClass = cr.status === 'matched' ? 'match' : cr.status === 'unmatched_tz' ? 'mismatch' : 'partial';
          var statusIcon = cr.status === 'matched' ? '&#10003;' : cr.status === 'unmatched_tz' ? '&#10007;' : '&#9888;';
          html += '<tr>' +
            '<td>' + escapeHtml(cr.left_text || cr.name || '') + '</td>' +
            '<td>' + escapeHtml(cr.left_text || '') + '</td>' +
            '<td class="' + statusClass + '"><span class="check-icon">' + statusIcon + '</span> ' + escapeHtml(cr.right_text || '—') + '</td>' +
            '</tr>';
        }
        html += '</tbody></table></div>';
      }

      html += '</div></div>';
    }
    $('comparison-results').innerHTML = html;
  }

  // ── National Regime ─────────────────────────────────────────────────

  var regimePollingTimer = null;

  function initRegime() {
    var btnCheck = $('btn-regime-check');
    var btnRefresh = $('btn-regime-refresh');
    if (btnCheck) btnCheck.addEventListener('click', startRegimeCheck);
    if (btnRefresh) btnRefresh.addEventListener('click', loadRegimeCheck);
  }

  function startRegimeCheck() {
    if (!selectedPurchaseId) return;
    $('regime-results').innerHTML = '<div class="search-status"><div class="spinner"></div><span>Запуск проверки национального режима...</span></div>';
    API.apiFetch('/regime/purchases/' + selectedPurchaseId + '/check', { method: 'POST' })
      .then(function (data) {
        if (data && (data.status === 'pending' || data.status === 'processing')) {
          pollRegimeCheck();
        } else {
          renderRegimeResults(data);
        }
      })
      .catch(function (err) { showError(err.message); });
  }

  function loadRegimeCheck() {
    if (!selectedPurchaseId) return;
    API.apiFetch('/regime/purchases/' + selectedPurchaseId + '/check')
      .then(function (data) {
        if (!data || !data.id) {
          $('regime-results').innerHTML = '<div class="empty-state">Загрузите лоты и выполните поиск поставщиков для проверки национального режима</div>';
          return;
        }
        if (data.status === 'pending' || data.status === 'processing') {
          $('regime-results').innerHTML = '<div class="search-status"><div class="spinner"></div><span>Проверка выполняется...</span></div>';
          pollRegimeCheck();
        } else {
          renderRegimeResults(data);
        }
      })
      .catch(function () {
        $('regime-results').innerHTML = '<div class="empty-state">Загрузите лоты и выполните поиск поставщиков для проверки национального режима</div>';
      });
  }

  function pollRegimeCheck() {
    if (regimePollingTimer) clearTimeout(regimePollingTimer);
    regimePollingTimer = setTimeout(function () {
      if (!selectedPurchaseId) return;
      API.apiFetch('/regime/purchases/' + selectedPurchaseId + '/check')
        .then(function (data) {
          if (data && (data.status === 'pending' || data.status === 'processing')) {
            pollRegimeCheck();
          } else {
            renderRegimeResults(data);
          }
        })
        .catch(function () { });
    }, 3000);
  }

  function renderRegimeResults(data) {
    if (!data || !data.items || data.items.length === 0) {
      $('regime-results').innerHTML = '<div class="empty-state">Нет результатов проверки</div>';
      return;
    }
    var html = '';
    for (var i = 0; i < data.items.length; i++) {
      var item = data.items[i];
      var statusClass = 'status-draft';
      var statusLabel = 'Проверяется';
      if (item.overall_status === 'ok') { statusClass = 'status-active'; statusLabel = 'Соответствует'; }
      else if (item.overall_status === 'warning') { statusClass = 'status-warning'; statusLabel = 'Внимание'; }
      else if (item.overall_status === 'error') { statusClass = 'status-search'; statusLabel = 'Не соответствует'; }
      else if (item.overall_status === 'not_found') { statusClass = 'status-draft'; statusLabel = 'Не найден'; }

      html += '<div class="regime-card"><div class="regime-card-header"><div>';
      html += '<div class="regime-product">' + escapeHtml(item.product_name || 'Товар') + '</div>';
      if (item.registry_number) html += '<div style="font-size:12px;color:var(--text-secondary);">Реестровый номер: ' + escapeHtml(item.registry_number) + '</div>';
      if (item.okpd2_code) html += '<div style="font-size:12px;color:var(--text-secondary);">ОКПД2: ' + escapeHtml(item.okpd2_code) + '</div>';
      html += '</div>';
      html += '<span class="status ' + statusClass + '"><span class="status-dot"></span> ' + statusLabel + '</span>';
      html += '</div><div class="regime-checks">';

      // Registry PP 719
      html += renderRegimeCheckCell('Реестр ПП №719', item.registry_status, item.registry_actual, item.registry_cert_end_date, item.registry_raw_url);

      // Localization PP 1875
      var locDetail = '';
      if (item.localization_actual_score != null && item.localization_required_score != null) {
        locDetail = item.localization_actual_score + ' из ' + item.localization_required_score + ' (мин.)';
      }
      html += renderRegimeCheckCellSimple('Баллы локализации', item.localization_status, locDetail);

      // GISP
      html += renderRegimeCheckCellSimple('Каталог ГИСП', item.gisp_status, item.gisp_url ? '<a href="' + escapeHtml(item.gisp_url) + '" target="_blank">Открыть</a>' : '');

      html += '</div></div>';
    }
    $('regime-results').innerHTML = html;
  }

  function renderRegimeCheckCell(label, status, actual, certEnd, url) {
    var cls = 'regime-check regime-unknown';
    var icon = '—';
    var detail = '';
    if (status === 'ok') { cls = 'regime-check regime-pass'; icon = '✓'; detail = actual ? 'Найден в реестре' : 'Найден, не актуален'; }
    else if (status === 'not_actual') { cls = 'regime-check regime-fail'; icon = '✗'; detail = 'Запись не актуальна'; }
    else if (status === 'not_found') { cls = 'regime-check regime-fail'; icon = '✗'; detail = 'Не найден в реестре'; }
    if (certEnd) detail += ', до ' + certEnd;
    return '<div class="' + cls + '"><div class="regime-check-label">' + icon + ' ' + escapeHtml(label) + '</div><div class="regime-check-value">' + detail + '</div></div>';
  }

  function renderRegimeCheckCellSimple(label, status, detail) {
    var cls = 'regime-check regime-unknown';
    var icon = '—';
    if (status === 'ok') { cls = 'regime-check regime-pass'; icon = '✓'; }
    else if (status === 'insufficient' || status === 'mismatch' || status === 'error') { cls = 'regime-check regime-fail'; icon = '✗'; }
    else if (status === 'warning' || status === 'wording') { cls = 'regime-check regime-unknown'; icon = '⚠'; }
    else if (status === 'not_found' || status === 'okpd_not_found' || status === 'score_missing') { cls = 'regime-check regime-unknown'; icon = '—'; }
    return '<div class="' + cls + '"><div class="regime-check-label">' + icon + ' ' + escapeHtml(label) + '</div><div class="regime-check-value">' + (detail || '') + '</div></div>';
  }

  // ── Polling cleanup ────────────────────────────────────────────────

  function clearPolling() {
    if (lotsPollingTimer) { clearTimeout(lotsPollingTimer); lotsPollingTimer = null; }
    if (searchPollingTimer) { clearTimeout(searchPollingTimer); searchPollingTimer = null; }
    if (comparisonPollingTimer) { clearTimeout(comparisonPollingTimer); comparisonPollingTimer = null; }
    if (regimePollingTimer) { clearTimeout(regimePollingTimer); regimePollingTimer = null; }
  }

  // ── Init ───────────────────────────────────────────────────────────

  document.addEventListener('DOMContentLoaded', function () {
    initModals();
    initTabs();
    initHeader();
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
    loadPurchases();
  });

})();
