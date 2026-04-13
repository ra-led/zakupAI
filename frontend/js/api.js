(function () {
  'use strict';

  /**
   * Core fetch wrapper with auth and error handling.
   *
   * @param {string}  path              - API path (e.g. "/users/me"), appended to CONFIG.API_URL
   * @param {Object}  [options]
   * @param {string}  [options.method]  - HTTP method (default GET)
   * @param {*}       [options.body]    - Will be JSON-stringified unless it is FormData
   * @param {string}  [options.token]   - Override token (otherwise pulled from localStorage)
   * @returns {Promise<any>}
   */
  async function apiFetch(path, options) {
    var opts = options || {};
    var method = opts.method || 'GET';
    var token = opts.token || localStorage.getItem(CONFIG.TOKEN_KEY);

    var headers = {};
    if (token) {
      headers['Authorization'] = 'Bearer ' + token;
    }

    var fetchOptions = {
      method: method,
      headers: headers,
    };

    if (opts.body !== undefined) {
      if (opts.body instanceof FormData) {
        fetchOptions.body = opts.body;
      } else {
        headers['Content-Type'] = 'application/json';
        fetchOptions.body = JSON.stringify(opts.body);
      }
    }

    var response = await fetch(CONFIG.API_URL + path, fetchOptions);

    if (response.status === 401) {
      localStorage.removeItem(CONFIG.TOKEN_KEY);
      localStorage.removeItem(CONFIG.USER_KEY);
      window.location.href = '/login.html';
      return;
    }

    if (!response.ok) {
      var errorBody;
      try {
        errorBody = await response.json();
      } catch (_) {
        errorBody = { detail: response.statusText };
      }
      var err = new Error(errorBody.detail || 'API error ' + response.status);
      err.status = response.status;
      err.body = errorBody;
      throw err;
    }

    // 204 No Content
    if (response.status === 204) {
      return null;
    }

    return response.json();
  }

  /**
   * Upload files via multipart/form-data.
   *
   * @param {string}   path      - API path
   * @param {FormData} formData  - FormData with file(s)
   * @param {string}   [token]   - Override token
   * @returns {Promise<any>}
   */
  async function apiUpload(path, formData, token) {
    return apiFetch(path, {
      method: 'POST',
      body: formData,
      token: token,
    });
  }

  /**
   * Convert a tech-task document (docx/pdf) to markdown via doc-to-md service.
   *
   * @param {File} file - File object to convert
   * @returns {Promise<any>}
   */
  async function convertTechTaskFile(file, purchaseId) {
    var formData = new FormData();
    formData.append('file', file);

    var token = localStorage.getItem(CONFIG.TOKEN_KEY);
    var headers = {};
    if (token) {
      headers['Authorization'] = 'Bearer ' + token;
    }

    var response = await fetch(CONFIG.DOC_TO_MD_URL + '/convert', {
      method: 'POST',
      headers: headers,
      body: formData,
    });

    if (response.status === 401) {
      localStorage.removeItem(CONFIG.TOKEN_KEY);
      localStorage.removeItem(CONFIG.USER_KEY);
      window.location.href = '/login.html';
      return;
    }

    if (!response.ok) {
      var errorBody;
      try {
        errorBody = await response.json();
      } catch (_) {
        errorBody = { detail: response.statusText };
      }
      var err = new Error(errorBody.detail || 'Conversion error ' + response.status);
      err.status = response.status;
      err.body = errorBody;
      throw err;
    }

    var data = await response.json();

    // Track Mistral OCR usage if returned by doc-to-md
    if (data && data.usage) {
      var trackBody = { usage: data.usage };
      if (purchaseId) trackBody.purchase_id = purchaseId;
      apiFetch('/admin/track-conversion', {
        method: 'POST',
        body: trackBody,
      }).catch(function () { /* ignore tracking errors */ });
    }

    return data;
  }

  window.API = {
    apiFetch: apiFetch,
    apiUpload: apiUpload,
    convertTechTaskFile: convertTechTaskFile,
  };
})();
