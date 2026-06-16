// Login screen. Modern, branded replacement for the sparse Tk login. Renders
// the navy/teal palette, the org name + version below the title, and a helpful
// empty state when no admin password is configured. Inline errors only — no
// system message boxes.

import bridge from '../bridge.js';
import * as icons from '../icons.js';

const esc = (s) =>
  String(s).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

export async function renderLogin(root, onAuthenticated) {
  const status = await bridge.auth.status();

  if (!status.ok) {
    root.innerHTML = `
      <div class="login">
        <div class="fatal">
          <div class="fatal__title">Configuration error</div>
          <div class="fatal__body">${esc(status.config_error || 'Unknown error.')}</div>
        </div>
      </div>`;
    return;
  }

  const noGate = !status.password_required;
  const emptyState = noGate
    ? `<div class="login__empty">This installation has no password set —
         proceed to configure.</div>`
    : '';
  const passwordField = noGate
    ? ''
    : `<label class="field">
         <span class="field__label">Password</span>
         <input id="login-pw" class="field__input" type="password"
                autocomplete="off" autofocus />
       </label>`;

  root.innerHTML = `
    <div class="login">
      <form class="login__card" id="login-form" novalidate>
        <div class="login__mark">${icons.shield}</div>
        <h1 class="login__title">${esc(status.app_name)}</h1>
        <p class="login__sub">v${esc(status.app_version)} · ${esc(status.org)}</p>
        ${emptyState}
        <div class="login__form">
          ${passwordField}
          <div class="field__error" id="login-error" role="alert"></div>
          <button class="btn btn--primary btn--block" type="submit" id="login-btn">
            ${noGate ? 'Continue' : 'Log in'}
          </button>
        </div>
      </form>
    </div>`;

  const form = root.querySelector('#login-form');
  const pw = root.querySelector('#login-pw');
  const errorEl = root.querySelector('#login-error');
  const btn = root.querySelector('#login-btn');
  if (pw) pw.focus();

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    errorEl.textContent = '';
    btn.disabled = true;
    try {
      const res = await bridge.auth.login(pw ? pw.value : '');
      if (res.ok) {
        onAuthenticated();
        return;
      }
      errorEl.textContent = res.message || 'Incorrect password.';
      if (pw) { pw.value = ''; pw.focus(); }
    } catch (err) {
      errorEl.textContent = `Bridge error: ${err.message}`;
    } finally {
      btn.disabled = false;
    }
  });
}
