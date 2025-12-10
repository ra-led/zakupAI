import { useEffect, useMemo, useState } from 'react';

const API_URL =
  import.meta.env.VITE_API_URL ||
  (typeof window !== 'undefined' ? `${window.location.origin}/api` : 'http://localhost:8000');

async function apiFetch(path, { token, method = 'GET', body } = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(`${API_URL}${path}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    let errorText = 'Request failed';
    try {
      const parsed = await response.json();
      errorText = parsed.detail || JSON.stringify(parsed);
    } catch (err) {
      errorText = await response.text();
    }
    throw new Error(errorText || `${response.status}`);
  }
  if (response.status === 204) return null;
  return response.json();
}

function AuthPanel({ onAuth, busy }) {
  const [mode, setMode] = useState('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [showPassword, setShowPassword] = useState(false);

  const handleSubmit = async (evt) => {
    evt.preventDefault();
    setError('');
    const endpoint = mode === 'login' ? '/auth/login' : '/auth/register';
    try {
      if (mode === 'register') {
        await apiFetch(endpoint, { method: 'POST', body: { email, password } });
      }
      const result = await apiFetch('/auth/login', { method: 'POST', body: { email, password } });
      onAuth(result.token, email);
    } catch (err) {
      setError(err.message);
    }
  };

  return (
    <div className="card" style={{ maxWidth: 420, margin: '60px auto' }}>
      <div className="auth-tabs" role="tablist" aria-label="Авторизация">
        <button
          type="button"
          role="tab"
          aria-selected={mode === 'login'}
          className={mode === 'login' ? 'auth-tabs__btn active' : 'auth-tabs__btn'}
          onClick={() => setMode('login')}
          disabled={busy}
        >
          Вход
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={mode === 'register'}
          className={mode === 'register' ? 'auth-tabs__btn active' : 'auth-tabs__btn'}
          onClick={() => setMode('register')}
          disabled={busy}
        >
          Регистрация
        </button>
      </div>
      <div className="auth-mode-body" key={mode}>
        {error && <div className="alert">{error}</div>}
        <form onSubmit={handleSubmit}>
          <label htmlFor="email">Email</label>
          <input id="email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />

          <div className="stack" style={{ alignItems: 'center', justifyContent: 'space-between', gap: 6 }}>
            <label htmlFor="password" style={{ marginBottom: 0 }}>
              Пароль
            </label>
            <button
              type="button"
              className="linkish"
              onClick={() => setShowPassword((v) => !v)}
              disabled={busy}
              style={{ background: 'transparent', color: '#2563eb', padding: 0, width: 'auto' }}
            >
              {showPassword ? 'Скрыть пароль' : 'Показать пароль'}
            </button>
          </div>
          <input
            id="password"
            type={showPassword ? 'text' : 'password'}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            aria-describedby={mode === 'register' ? 'password-help' : undefined}
          />
          {mode === 'register' && (
            <div id="password-help" className="password-hint">
              Пароль от 6 до 72 символов. Используйте буквы и цифры, чтобы обеспечить безопасность.
            </div>
          )}
          <button type="submit" className="primary" disabled={busy} style={{ width: '100%' }}>
            {busy ? 'Пожалуйста, подождите…' : mode === 'login' ? 'Войти' : 'Зарегистрироваться'}
          </button>
        </form>
      </div>
    </div>
  );
}

function PurchaseCard({ purchase, onSelect, isActive }) {
  const SUMMARY_LIMIT = 100;
  const terms = purchase.terms_text || '';
  const preview = terms.length > SUMMARY_LIMIT ? `${terms.slice(0, SUMMARY_LIMIT)}…` : terms;

  return (
    <div
      className="card"
      style={{ border: isActive ? '2px solid #6366f1' : '1px solid #e2e8f0', cursor: 'pointer' }}
      onClick={onSelect}
    >
      <h3 style={{ margin: '0 0 6px 0' }}>{purchase.full_name}</h3>
      {terms && (
        <p className="muted" style={{ marginBottom: 0 }}>
          {preview}
        </p>
      )}
    </div>
  );
}

function SupplierList({ suppliers, contactsBySupplier }) {
  if (!suppliers.length) return <p className="muted">Поставщики пока не добавлены.</p>;
  return (
    <div className="list">
      {suppliers.map((supplier) => (
        <div key={supplier.id} className="card">
          <h4 style={{ margin: '0 0 4px 0' }}>{supplier.company_name || supplier.website_url || 'Без названия'}</h4>
          {supplier.website_url && (
            <a href={supplier.website_url} target="_blank" rel="noreferrer" className="muted">
              {supplier.website_url}
            </a>
          )}
          <div className="muted" style={{ marginTop: 6 }}>Релевантность: {supplier.relevance_score ?? '—'}</div>
          <div style={{ marginTop: 10 }}>
            <div className="section-title">Контакты</div>
            {(contactsBySupplier[supplier.id] || []).length ? (
              <ul style={{ paddingLeft: 18, margin: 0 }}>
                {contactsBySupplier[supplier.id].map((c) => (
                  <li key={c.id}>
                    <strong>{c.email}</strong>
                    {c.source_url && (
                      <a href={c.source_url} target="_blank" rel="noreferrer" style={{ marginLeft: 8 }}>
                        источник
                      </a>
                    )}
                    {c.is_selected_for_request && <span className="tag">Для рассылки</span>}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="muted">Нет контактов.</p>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

function App() {
  const storedToken = useMemo(() => localStorage.getItem('zakupai_token'), []);
  const storedUser = useMemo(() => localStorage.getItem('zakupai_user'), []);
  const [token, setToken] = useState(storedToken || '');
  const [userEmail, setUserEmail] = useState(storedUser || '');
  const [busy, setBusy] = useState(false);
  const [purchases, setPurchases] = useState([]);
  const [selectedId, setSelectedId] = useState(null);
  const [suppliers, setSuppliers] = useState([]);
  const [contactsBySupplier, setContactsBySupplier] = useState({});
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');

  const [purchaseForm, setPurchaseForm] = useState({ custom_name: '', terms_text: '' });
  const [showPurchaseModal, setShowPurchaseModal] = useState(false);
  const [supplierForm, setSupplierForm] = useState({ company_name: '', website_url: '', relevance_score: '' });
  const [contactForm, setContactForm] = useState({ supplier_id: '', email: '', source_url: '', is_selected_for_request: true });
  const [searchHints, setSearchHints] = useState('');
  const [llmQueries, setLlmQueries] = useState(null);
  const [emailDraft, setEmailDraft] = useState(null);
  const [purchaseDetailsExpanded, setPurchaseDetailsExpanded] = useState(false);

  useEffect(() => {
    if (token) {
      loadPurchases();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  useEffect(() => {
    if (!token || !selectedId) {
      setLlmQueries(null);
      return;
    }

    const preloadSearchState = async () => {
      try {
        const selectedPurchase = purchases.find((p) => p.id === selectedId);
        const state = await apiWithToken(`/purchases/${selectedId}/suppliers/search`, {
          method: 'POST',
          body: { terms_text: selectedPurchase?.terms_text || '', hints: [] },
        });
        setLlmQueries(state);
      } catch (err) {
        console.error('Не удалось загрузить состояние поиска', err);
      }
    };

    preloadSearchState();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, selectedId, purchases]);

  const apiWithToken = (path, options) => apiFetch(path, { ...options, token });

  const loadPurchases = async () => {
    setBusy(true);
    setError('');
    try {
      const data = await apiWithToken('/purchases');
      setPurchases(data);
      if (data.length && !selectedId) setSelectedId(data[0].id);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const loadSuppliers = async (purchaseId) => {
    try {
      const data = await apiWithToken(`/purchases/${purchaseId}/suppliers`);
      setSuppliers(data);
      const map = {};
      for (const s of data) {
        map[s.id] = await apiWithToken(`/suppliers/${s.id}/contacts`);
      }
      setContactsBySupplier(map);
    } catch (err) {
      setError(err.message);
    }
  };

  useEffect(() => {
    if (selectedId && token) {
      loadSuppliers(selectedId);
      setEmailDraft(null);
      setLlmQueries(null);
      setPurchaseDetailsExpanded(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  const handleAuth = (newToken, email) => {
    localStorage.setItem('zakupai_token', newToken);
    localStorage.setItem('zakupai_user', email);
    setToken(newToken);
    setUserEmail(email);
  };

  const handleLogout = () => {
    localStorage.removeItem('zakupai_token');
    localStorage.removeItem('zakupai_user');
    setToken('');
    setPurchases([]);
    setSuppliers([]);
    setContactsBySupplier({});
    setSelectedId(null);
  };

  const createPurchase = async (evt) => {
    evt.preventDefault();
    setBusy(true);
    setError('');
    setMessage('');
    try {
      await apiWithToken('/purchases', { method: 'POST', body: purchaseForm });
      setPurchaseForm({ custom_name: '', terms_text: '' });
      setMessage('Закупка создана');
      await loadPurchases();
      setShowPurchaseModal(false);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const createSupplier = async (evt) => {
    evt.preventDefault();
    if (!selectedId) return;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      await apiWithToken(`/purchases/${selectedId}/suppliers`, {
        method: 'POST',
        body: {
          ...supplierForm,
          relevance_score: supplierForm.relevance_score ? Number(supplierForm.relevance_score) : null,
        },
      });
      setSupplierForm({ company_name: '', website_url: '', relevance_score: '' });
      setMessage('Поставщик добавлен');
      await loadSuppliers(selectedId);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const createContact = async (evt) => {
    evt.preventDefault();
    if (!contactForm.supplier_id) return;
    setBusy(true);
    setError('');
    setMessage('');
    try {
      await apiWithToken(`/purchases/${selectedId}/suppliers/${contactForm.supplier_id}/contacts`, {
        method: 'POST',
        body: {
          email: contactForm.email,
          source_url: contactForm.source_url,
          is_selected_for_request: contactForm.is_selected_for_request,
        },
      });
      setContactForm({ supplier_id: '', email: '', source_url: '', is_selected_for_request: true });
      setMessage('Контакт сохранён');
      await loadSuppliers(selectedId);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const runSearch = async (evt) => {
    evt.preventDefault();
    if (!selectedId) return;
    setBusy(true);
    setError('');
    try {
      const selectedPurchase = purchases.find((p) => p.id === selectedId);
      const result = await apiWithToken(`/purchases/${selectedId}/suppliers/search`, {
        method: 'POST',
        body: {
          terms_text: selectedPurchase?.terms_text || '',
          hints: searchHints.split(/[\,\n]/).map((v) => v.trim()).filter(Boolean),
        },
      });
      setLlmQueries(result);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const buildDraft = async () => {
    if (!selectedId) return;
    setBusy(true);
    setError('');
    try {
      const draft = await apiWithToken(`/purchases/${selectedId}/email-draft`, { method: 'POST' });
      setEmailDraft(draft);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  if (!token) {
    return <AuthPanel onAuth={handleAuth} busy={busy} />;
  }

  const selectedPurchase = purchases.find((p) => p.id === selectedId);
  const purchaseHasLongText = (selectedPurchase?.terms_text || '').length > 420;

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <h1>zakupAI</h1>
        <div className="muted" style={{ marginBottom: 20 }}>
          {userEmail}
          <br />
          <span style={{ fontSize: 12 }}>API: {API_URL}</span>
        </div>
        <button className="linkish" onClick={handleLogout} disabled={busy}>
          Выйти
        </button>
      </aside>
      <main className="main">
        <div className="card">
          <h2 style={{ marginTop: 0 }}>Закупки</h2>
          {message && <div className="alert" style={{ background: '#ecfdf3', color: '#166534' }}>{message}</div>}
          {error && <div className="alert">{error}</div>}
          <div className="list">
            {purchases.map((purchase) => (
              <PurchaseCard
                key={purchase.id}
                purchase={purchase}
                onSelect={() => setSelectedId(purchase.id)}
                isActive={purchase.id === selectedId}
              />
            ))}
            <button
              type="button"
              className="card create-card"
              onClick={() => setShowPurchaseModal(true)}
              disabled={busy}
            >
              <div className="create-card__icon">＋</div>
              <div className="create-card__text">Создать новую закупку</div>
            </button>
          </div>
        </div>

        {showPurchaseModal && (
          <div className="modal-overlay" role="dialog" aria-modal="true">
            <div className="modal">
              <div className="stack" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
                <h3 style={{ margin: 0 }}>Новая закупка</h3>
                <button
                  type="button"
                  className="linkish"
                  onClick={() => setShowPurchaseModal(false)}
                  disabled={busy}
                  aria-label="Закрыть"
                >
                  ✕
                </button>
              </div>
              <form onSubmit={createPurchase} className="stack" style={{ flexDirection: 'column', marginTop: 12 }}>
                <label>Название</label>
                <input
                  value={purchaseForm.custom_name}
                  onChange={(e) => setPurchaseForm((f) => ({ ...f, custom_name: e.target.value }))}
                  placeholder="Например, Поставка серверов"
                  required
                />
                <label>Описание / ТЗ</label>
                <textarea
                  rows={4}
                  value={purchaseForm.terms_text}
                  onChange={(e) => setPurchaseForm((f) => ({ ...f, terms_text: e.target.value }))}
                  placeholder="Кратко опишите предмет закупки"
                  required
                />
                <div className="stack" style={{ justifyContent: 'flex-end', marginTop: 6 }}>
                  <button type="button" className="secondary" onClick={() => setShowPurchaseModal(false)} disabled={busy}>
                    Отмена
                  </button>
                  <button type="submit" className="primary" disabled={busy}>
                    Создать закупку
                  </button>
                </div>
              </form>
            </div>
          </div>
        )}

        {selectedPurchase && (
          <>
            <div className="card">
              <div className="stack" style={{ justifyContent: 'space-between', alignItems: 'flex-start' }}>
                <div className="stack" style={{ alignItems: 'center', gap: 12, flex: 1, minWidth: 0 }}>
                  <h2 style={{ marginTop: 0, marginBottom: 6, flex: 1, minWidth: 0 }}>{selectedPurchase.full_name}</h2>
                  <div className="tag">Статус: {selectedPurchase.status}</div>
                </div>
                {selectedPurchase.nmck_value && (
                  <div className="tag" style={{ whiteSpace: 'nowrap' }}>
                    НМЦК: {selectedPurchase.nmck_value} {selectedPurchase.nmck_currency || ''}
                  </div>
                )}
              </div>
              <p className="muted" style={{ marginBottom: purchaseHasLongText ? 8 : undefined }}>
                {purchaseDetailsExpanded || !purchaseHasLongText
                  ? selectedPurchase.terms_text || 'Описание не заполнено'
                  : `${(selectedPurchase.terms_text || '').slice(0, 420)}…`}
              </p>
              {purchaseHasLongText && (
                <button
                  type="button"
                  className="linkish"
                  onClick={() => setPurchaseDetailsExpanded((v) => !v)}
                  style={{ padding: 0 }}
                >
                  {purchaseDetailsExpanded ? 'Свернуть' : 'Показать полностью'}
                </button>
              )}
            </div>

            <div className="card">
              <div className="stack" style={{ alignItems: 'center', justifyContent: 'space-between' }}>
                <h3 style={{ margin: 0 }}>Поставщики</h3>
                <button className="secondary" onClick={() => loadSuppliers(selectedPurchase.id)} disabled={busy}>
                  Обновить
                </button>
              </div>
              <SupplierList suppliers={suppliers} contactsBySupplier={contactsBySupplier} />

              <div className="section-title">Добавить поставщика</div>
              <form onSubmit={createSupplier} className="stack" style={{ flexDirection: 'column' }}>
                <label>Компания</label>
                <input
                  value={supplierForm.company_name}
                  onChange={(e) => setSupplierForm((f) => ({ ...f, company_name: e.target.value }))}
                  placeholder="Название"
                />
                <label>Сайт</label>
                <input
                  value={supplierForm.website_url}
                  onChange={(e) => setSupplierForm((f) => ({ ...f, website_url: e.target.value }))}
                  placeholder="https://example.com"
                />
                <label>Релевантность (0-1)</label>
                <input
                  value={supplierForm.relevance_score}
                  onChange={(e) => setSupplierForm((f) => ({ ...f, relevance_score: e.target.value }))}
                  type="number"
                  min="0"
                  max="1"
                  step="0.1"
                />
                <button type="submit" className="primary" disabled={busy}>
                  Сохранить поставщика
                </button>
              </form>

              <div className="section-title">Добавить контакт</div>
              <form onSubmit={createContact} className="stack" style={{ flexDirection: 'column' }}>
                <label>Поставщик</label>
                <select
                  value={contactForm.supplier_id}
                  onChange={(e) => setContactForm((f) => ({ ...f, supplier_id: e.target.value }))}
                  required
                >
                  <option value="">Выберите поставщика</option>
                  {suppliers.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.company_name || s.website_url || 'Без названия'}
                    </option>
                  ))}
                </select>
                <label>Email</label>
                <input
                  value={contactForm.email}
                  onChange={(e) => setContactForm((f) => ({ ...f, email: e.target.value }))}
                  type="email"
                  required
                  placeholder="sales@example.com"
                />
                <label>Источник</label>
                <input
                  value={contactForm.source_url}
                  onChange={(e) => setContactForm((f) => ({ ...f, source_url: e.target.value }))}
                  placeholder="https://example.com/contact"
                />
                <label style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <input
                    type="checkbox"
                    checked={contactForm.is_selected_for_request}
                    onChange={(e) => setContactForm((f) => ({ ...f, is_selected_for_request: e.target.checked }))}
                    style={{ width: 'auto' }}
                  />
                  Добавить в рассылку
                </label>
                <button type="submit" className="primary" disabled={busy}>
                  Сохранить контакт
                </button>
              </form>
            </div>

            <div className="card">
              <div className="stack" style={{ justifyContent: 'space-between', alignItems: 'center' }}>
                <h3 style={{ margin: 0 }}>Подготовка</h3>
                <button className="secondary" onClick={buildDraft} disabled={busy}>
                  Сгенерировать письмо
                </button>
              </div>

              <form onSubmit={runSearch} style={{ marginBottom: 18 }}>
                <label>Подсказки для поиска поставщиков</label>
                <textarea
                  rows={2}
                  value={searchHints}
                  onChange={(e) => setSearchHints(e.target.value)}
                  placeholder="Введите ключевые слова, бренды или города"
                />
                <button type="submit" className="primary" disabled={busy}>
                  Построить план поиска
                </button>
              </form>

              {llmQueries && (
                <div className="card" style={{ background: '#f8fafc', border: '1px solid #e2e8f0' }}>
                  <h4 style={{ marginTop: 0 }}>Автопоиск поставщиков</h4>
                  <div className="tag" style={{ marginBottom: 8 }}>
                    Задача #{llmQueries.task_id}: {llmQueries.status}
                  </div>
                  {llmQueries.tech_task_excerpt && (
                    <p className="muted">{llmQueries.tech_task_excerpt}</p>
                  )}
                  {llmQueries.queries?.length ? (
                    <ul>
                      {llmQueries.queries.map((q, idx) => (
                        <li key={idx}>{q}</li>
                      ))}
                    </ul>
                  ) : (
                    <p className="muted">Поисковая задача выполняется или ожидает в очереди.</p>
                  )}
                  <p className="muted">{llmQueries.note}</p>
                </div>
              )}

              {emailDraft && (
                <div className="card" style={{ background: '#f0f9ff', border: '1px solid #bae6fd' }}>
                  <h4 style={{ marginTop: 0 }}>Черновик письма</h4>
                  <div className="tag">{emailDraft.subject}</div>
                  <pre style={{ whiteSpace: 'pre-wrap', fontFamily: 'inherit' }}>{emailDraft.body}</pre>
                </div>
              )}
            </div>
          </>
        )}
      </main>
    </div>
  );
}

export default App;
