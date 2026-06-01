const DEBOUNCE_MS = 550;
const MIN_LEN = 10;

let debounceTimers = {};
let popup = null;
let popupFor = null;  // the textarea currently showing a popup

function getTextareas() {
  return Array.from(document.querySelectorAll('textarea[name^="pred_"]'));
}

// ── Duplicate detection ────────────────────────────────────────────────────

function checkDuplicates() {
  const tas = getTextareas();
  const counts = {};
  tas.forEach(ta => {
    const v = ta.value.trim().toLowerCase();
    if (v) counts[v] = (counts[v] || 0) + 1;
  });

  let hasDup = false;
  tas.forEach(ta => {
    const v = ta.value.trim().toLowerCase();
    const dup = v && counts[v] > 1;
    ta.closest('.cell').classList.toggle('cell-duplicate', dup);
    if (dup) hasDup = true;
  });
  return hasDup;
}

// ── Popup ──────────────────────────────────────────────────────────────────

function ensurePopup() {
  if (!popup) {
    popup = document.createElement('div');
    popup.id = 'similar-popup';
    document.body.appendChild(popup);
  }
  return popup;
}

function positionPopup(textarea) {
  const rect = textarea.getBoundingClientRect();
  const p = ensurePopup();
  p.style.left = (rect.left + rect.width / 2 + window.scrollX) + 'px';
  p.style.top  = (rect.top + window.scrollY - 10) + 'px';
}

function showPopup(textarea, data) {
  if (data.count === 0) { hidePopup(); return; }

  const p = ensurePopup();
  const noun = data.count === 1 ? 'person has' : 'people have';
  let html = `<span class="pop-icon">💭</span> <strong>${data.count}</strong> ${noun} predicted something similar`;
  if (data.examples.length) {
    html += `<div class="pop-examples">`;
    data.examples.slice(0, 2).forEach(ex => {
      html += `<span>"${ex.length > 48 ? ex.slice(0, 48) + '…' : ex}"</span>`;
    });
    html += `</div>`;
  }
  p.innerHTML = html;
  positionPopup(textarea);
  popupFor = textarea;
  p.classList.add('visible');
}

function hidePopup() {
  if (popup) popup.classList.remove('visible');
  popupFor = null;
}

// ── API call ───────────────────────────────────────────────────────────────

async function fetchSimilar(textarea) {
  const text = textarea.value.trim();
  if (text.length < MIN_LEN) { hidePopup(); return; }
  try {
    const resp = await fetch('/api/similar?q=' + encodeURIComponent(text));
    const data = await resp.json();
    if (document.activeElement === textarea || popupFor === textarea) {
      showPopup(textarea, data);
    }
  } catch (_) { /* network error — fail silently */ }
}

// ── Wire up events ─────────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  getTextareas().forEach(ta => {
    ta.addEventListener('input', () => {
      checkDuplicates();
      hidePopup();
      clearTimeout(debounceTimers[ta.name]);
      debounceTimers[ta.name] = setTimeout(() => fetchSimilar(ta), DEBOUNCE_MS);
    });

    ta.addEventListener('focus', () => {
      // Re-position if popup is already showing for this textarea
      if (popupFor === ta && popup?.classList.contains('visible')) {
        positionPopup(ta);
      }
    });

    ta.addEventListener('blur', () => {
      // Small delay so clicks on the popup itself don't immediately hide it
      setTimeout(() => {
        if (document.activeElement !== ta) hidePopup();
      }, 150);
    });
  });

  // Re-position popup on scroll/resize
  window.addEventListener('scroll', () => {
    if (popupFor && popup?.classList.contains('visible')) positionPopup(popupFor);
  }, { passive: true });

  // Block submission if duplicates present
  document.querySelector('.create-form').addEventListener('submit', e => {
    if (checkDuplicates()) {
      e.preventDefault();
      const first = getTextareas().find(ta =>
        ta.closest('.cell').classList.contains('cell-duplicate')
      );
      first?.scrollIntoView({ behavior: 'smooth', block: 'center' });
      first?.focus();
      // Flash a visible error
      let err = document.getElementById('dup-error');
      if (!err) {
        err = document.createElement('div');
        err.id = 'dup-error';
        err.className = 'flash';
        err.textContent = 'Please remove duplicate predictions (highlighted in red) before submitting.';
        document.querySelector('.create-form').prepend(err);
      }
    }
  });
});
