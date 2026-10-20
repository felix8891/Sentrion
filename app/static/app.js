document.addEventListener('DOMContentLoaded', () => {
  // Re-analysis runs as a background task. Poll the server-rendered source of
  // truth briefly while the job is queued/running; do not show a premature
  // success state or leave the detail page stale after processing finishes.
  const processingBanner = document.querySelector('[data-invoice-processing-status]');
  if (processingBanner) {
    const state = (processingBanner.getAttribute('data-invoice-processing-status') || '').toUpperCase();
    const invoiceId = processingBanner.getAttribute('data-invoice-id') || window.location.pathname;
    const pollKey = `sentrion:invoice-processing:${invoiceId}`;
    if (['QUEUED', 'PROCESSING'].includes(state)) {
      const polls = Number(sessionStorage.getItem(pollKey) || '0');
      if (polls < 20) {
        sessionStorage.setItem(pollKey, String(polls + 1));
        window.setTimeout(() => window.location.reload(), 1500);
      } else {
        const note = document.createElement('p');
        note.className = 'muted small-text';
        note.textContent = 'Processing is taking longer than expected. Refresh this page to check the latest server status.';
        processingBanner.appendChild(note);
        sessionStorage.removeItem(pollKey);
      }
    } else {
      sessionStorage.removeItem(pollKey);
    }
  }
  document.querySelectorAll('input[type=file]').forEach(input => {
    input.addEventListener('change', () => {
      const label = input.closest('.dropzone');
      if (!label) return;
      const names = [...input.files].map(f => f.name);
      const strong = label.querySelector('strong');
      if (strong && names.length) strong.textContent = names.length === 1 ? names[0] : `${names.length} file(s) selected`;
      const small = label.querySelector('small');
      if (small && names.length) small.textContent = names.slice(0,3).join(', ') + (names.length > 3 ? ` and ${names.length-3} other file(s)` : '');
    });
  });
  document.querySelectorAll('form').forEach(form => {
    form.addEventListener('submit', () => {
      const button = form.querySelector('button[type=submit],button:not([type])');
      if (!button || form.classList.contains('decision-form')) return;
      if (!form.checkValidity()) return;
      button.dataset.originalText = button.innerHTML;
      button.classList.add('is-busy');
      // Do not disable the button: the request may depend on its submitted value.
      setTimeout(() => button.classList.remove('is-busy'), 3000);
    });
  });

  // Live invoice filtering uses a same-origin HTML request and replaces only the
  // results region. This avoids a full document navigation and keeps the user's
  // current scroll position stable while the server remains the source of truth.
  document.querySelectorAll('form[data-live-invoice-filters]').forEach(form => {
    const search = form.querySelector('input[name="q"]');
    const riskSelect = form.querySelector('select[name="risk"]');
    const statusSelect = form.querySelector('select[name="status"]');
    const resetLink = form.querySelector('[data-filter-reset]');
    const results = document.querySelector('[data-invoice-results]');
    const resultCount = document.querySelector('[data-invoice-result-count]');
    const errorBox = document.querySelector('[data-filter-error]');
    let searchTimer = null;
    let activeController = null;
    let requestSequence = 0;

    if (!results || !resultCount) return;

    const buildFilterUrl = () => {
      const url = new URL(form.action || '/invoices', window.location.href);
      const params = new URLSearchParams();
      const q = search ? search.value.trim() : '';
      const risk = riskSelect ? riskSelect.value : '';
      const status = statusSelect ? statusSelect.value : '';
      if (q) params.set('q', q);
      if (risk) params.set('risk', risk);
      if (status) params.set('status', status);
      // Filter changes always reset pagination to the first page.
      url.search = params.toString();
      return url;
    };

    const applyFilters = async ({ updateAddress = true } = {}) => {
      const requestId = ++requestSequence;
      if (activeController) activeController.abort();
      activeController = new AbortController();
      const controller = activeController;
      const url = buildFilterUrl();

      if (errorBox) {
        errorBox.hidden = true;
        errorBox.textContent = '';
      }
      form.setAttribute('aria-busy', 'true');
      results.setAttribute('aria-busy', 'true');

      try {
        const response = await fetch(url.toString(), {
          method: 'GET',
          credentials: 'same-origin',
          headers: {
            'Accept': 'text/html',
            'X-Requested-With': 'XMLHttpRequest'
          },
          signal: controller.signal
        });
        if (!response.ok) throw new Error(`Filter request failed (${response.status}).`);
        if (response.redirected && new URL(response.url).pathname === '/login') {
          throw new Error('Your session has expired. Reload the page and sign in again.');
        }

        const html = await response.text();
        const parsed = new DOMParser().parseFromString(html, 'text/html');
        const nextResults = parsed.querySelector('[data-invoice-results]');
        const nextCount = parsed.querySelector('[data-invoice-result-count]');
        if (!nextResults || !nextCount) {
          throw new Error('The filter response does not contain valid invoice data.');
        }
        // An older request must never overwrite the latest selection.
        if (requestId !== requestSequence) return;

        results.innerHTML = nextResults.innerHTML;
        resultCount.textContent = nextCount.textContent;
        results.hidden = false;
        if (updateAddress) {
          window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}`);
        }
      } catch (error) {
        if (error && error.name === 'AbortError') return;
        if (requestId !== requestSequence) return;
        // Do not present the previous table as if it matched the new filters.
        results.hidden = true;
        resultCount.textContent = 'The invoice list has not been updated.';
        if (errorBox) {
          errorBox.textContent = error instanceof Error
            ? `${error.message} Please try again.`
            : 'The invoice list could not be updated. Please try again.';
          errorBox.hidden = false;
        }
      } finally {
        if (requestId === requestSequence) {
          form.removeAttribute('aria-busy');
          results.removeAttribute('aria-busy');
          activeController = null;
        }
      }
    };

    form.addEventListener('submit', event => {
      event.preventDefault();
      if (searchTimer) clearTimeout(searchTimer);
      searchTimer = null;
      applyFilters();
    });

    [riskSelect, statusSelect].filter(Boolean).forEach(select => {
      select.addEventListener('change', () => {
        if (searchTimer) clearTimeout(searchTimer);
        searchTimer = null;
        applyFilters();
      });
    });

    if (search) {
      search.addEventListener('input', () => {
        if (searchTimer) clearTimeout(searchTimer);
        searchTimer = setTimeout(() => {
          searchTimer = null;
          applyFilters();
        }, 300);
      });
      search.addEventListener('keydown', event => {
        if (event.key === 'Enter') {
          event.preventDefault();
          if (searchTimer) clearTimeout(searchTimer);
          searchTimer = null;
          applyFilters();
        }
      });
    }

    if (resetLink) {
      resetLink.addEventListener('click', event => {
        event.preventDefault();
        if (searchTimer) clearTimeout(searchTimer);
        searchTimer = null;
        if (search) search.value = '';
        if (riskSelect) riskSelect.value = '';
        if (statusSelect) statusSelect.value = '';
        applyFilters();
      });
    }
  });
});
