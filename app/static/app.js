// Scripts de la page d'accueil. Chargés depuis un fichier (pas de JS inline)
// pour permettre une CSP stricte (script-src 'self').
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
