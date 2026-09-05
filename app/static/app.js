// Scripts du site. Chargés depuis un fichier (pas de JS inline) pour permettre
// une CSP stricte (script-src 'self').

// --- Déconnexion dès qu'on quitte le site (fermeture / changement d'onglet) ---
// On ne déconnecte PAS pendant une navigation interne (clic sur un lien/bouton,
// requête HTMX) : un court délai de grâce distingue les deux.
(function () {
  if (!document.querySelector('a[href="/logout"]')) return; // pas connecté

  let internalUntil = 0;
  let loggedOut = false;
  const markInternal = () => { internalUntil = Date.now() + 1800; };

  document.addEventListener('click', (e) => {
    if (e.target.closest && e.target.closest('a, button')) markInternal();
  }, true);
  document.addEventListener('submit', markInternal, true);
  document.body.addEventListener('htmx:beforeRequest', markInternal);

  const leaving = () => {
    if (loggedOut || Date.now() < internalUntil) return;
    loggedOut = true;
    try { navigator.sendBeacon('/logout'); } catch (e) { /* ignore */ }
  };

  window.addEventListener('pagehide', leaving);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') {
      leaving();
    } else if (loggedOut) {
      window.location.href = '/login';   // retour sur l'onglet -> reconnexion
    }
  });
})();

// --- Scripts spécifiques à la page audio ---
(function () {
  const form = document.getElementById('upload-form');
  if (!form) return;

  const dz = document.getElementById('dropzone');
  const input = document.getElementById('file-input');

  // --- drag & drop ---
  ['dragenter', 'dragover'].forEach(evt =>
    dz.addEventListener(evt, e => { e.preventDefault(); dz.classList.add('over'); }));
  ['dragleave', 'drop'].forEach(evt =>
    dz.addEventListener(evt, e => { e.preventDefault(); dz.classList.remove('over'); }));
  dz.addEventListener('drop', e => {
    if (e.dataTransfer.files.length) {
      input.files = e.dataTransfer.files;
      dz.querySelector('strong').textContent = e.dataTransfer.files[0].name;
    }
  });
  input.addEventListener('change', () => {
    if (input.files.length) dz.querySelector('strong').textContent = input.files[0].name;
  });

  // --- après un upload réussi : reset + bascule vers l'onglet Notes ---
  form.addEventListener('htmx:afterRequest', e => {
    if (!e.detail.successful) return;
    form.reset();
    dz.querySelector('strong').textContent = 'Glisse un fichier audio ici';
    const tab = document.getElementById('tab-notes');
    if (tab) tab.checked = true;
  });

  // --- enregistrement direct au micro ---
  const btn = document.getElementById('rec-btn');
  if (!btn) return;
  const label = btn.querySelector('.rec-label');
  const timeEl = document.getElementById('rec-time');
  const cancelEl = document.getElementById('rec-cancel');
  const msg = document.getElementById('rec-msg');
  let rec, chunks, stream, t0, timer, cancelled;

  if (!navigator.mediaDevices || !window.MediaRecorder) {
    btn.disabled = true;
    btn.title = "Enregistrement non pris en charge par ce navigateur";
    return;
  }

  const pickMime = () =>
    ['audio/webm', 'audio/ogg', 'audio/mp4'].find(m => MediaRecorder.isTypeSupported(m)) || '';
  const extOf = m => m.includes('ogg') ? 'ogg' : m.includes('mp4') ? 'm4a' : 'webm';
  const fmt = s => Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');

  async function start() {
    msg.hidden = true;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      msg.textContent = "Micro inaccessible — autorise l'accès au microphone.";
      msg.hidden = false;
      return;
    }
    const mime = pickMime();
    rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    chunks = [];
    cancelled = false;
    rec.ondataavailable = e => { if (e.data && e.data.size) chunks.push(e.data); };
    rec.onstop = () => {
      stream.getTracks().forEach(t => t.stop());
      clearInterval(timer);
      timeEl.hidden = true;
      cancelEl.hidden = true;
      btn.classList.remove('recording');
      label.textContent = 'Enregistrer au micro';
      if (cancelled || !chunks.length) return;
      const type = (rec && rec.mimeType) || mime || 'audio/webm';
      const blob = new Blob(chunks, { type });
      const file = new File([blob], 'enregistrement-' + Date.now() + '.' + extOf(type), { type });
      const dt = new DataTransfer();
      dt.items.add(file);
      input.files = dt.files;
      dz.querySelector('strong').textContent = file.name;
      form.requestSubmit();
    };
    rec.start();
    t0 = Date.now();
    timeEl.textContent = '0:00';
    timeEl.hidden = false;
    cancelEl.hidden = false;
    btn.classList.add('recording');
    label.textContent = 'Arrêter';
    timer = setInterval(() => {
      timeEl.textContent = fmt(Math.floor((Date.now() - t0) / 1000));
    }, 500);
  }

  btn.addEventListener('click', () => {
    if (rec && rec.state === 'recording') rec.stop();
    else start();
  });
  cancelEl.addEventListener('click', () => {
    cancelled = true;
    if (rec && rec.state === 'recording') rec.stop();
  });
})();

// --- reCAPTCHA v3 (invisible) sur le formulaire d'inscription ---
// Génère un jeton juste avant l'envoi et l'attache à un champ caché ; le score
// est vérifié côté serveur. Si le script Google est indisponible, on laisse
// quand même partir le formulaire (le serveur retombe sur son propre filet).
(function () {
  const form = document.getElementById('register-form');
  if (!form) return;
  const siteKey = form.dataset.recaptchaSitekey;
  if (!siteKey) return; // reCAPTCHA pas configuré (clés absentes) : rien à faire

  form.addEventListener('submit', (e) => {
    if (form.dataset.recaptchaDone === '1') return; // jeton déjà attaché, on laisse partir
    e.preventDefault();
    if (typeof grecaptcha === 'undefined') { form.submit(); return; }
    grecaptcha.ready(() => {
      grecaptcha.execute(siteKey, { action: 'register' })
        .then((token) => {
          const field = document.getElementById('recaptcha-token');
          if (field) field.value = token;
          form.dataset.recaptchaDone = '1';
          form.submit();
        })
        .catch(() => { form.dataset.recaptchaDone = '1'; form.submit(); });
    });
  });
})();
