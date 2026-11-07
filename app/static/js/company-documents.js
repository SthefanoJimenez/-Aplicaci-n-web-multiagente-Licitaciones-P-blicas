/* HU-015: ayudas de carga. El servidor repite todas las validaciones. */
(() => {
  const form = document.querySelector('[data-document-form]');
  if (!form) return;
  const expiry = form.querySelector('#expires_on');
  const noExpiry = form.querySelector('#no_expiry');
  const validFrom = form.querySelector('#valid_from');
  const file = form.querySelector('#file');
  const fileError = form.querySelector('#file-error');
  const syncDates = () => {
    expiry.required = !noExpiry.checked;
    expiry.disabled = noExpiry.checked;
    if (validFrom.value) expiry.min = validFrom.value;
    else expiry.removeAttribute('min');
  };
  noExpiry.addEventListener('change', syncDates);
  validFrom.addEventListener('input', syncDates);
  syncDates();
  if (!file) return;
  file.addEventListener('change', () => {
    const selected = file.files[0];
    let message = '';
    if (selected && !/\.pdf$/i.test(selected.name)) message = 'Seleccione un archivo con extensión .pdf.';
    else if (selected && !selected.size) message = 'El archivo está vacío. Seleccione un PDF completo.';
    else if (selected && selected.size > 5 * 1024 * 1024) message = 'El PDF supera el máximo de 5 MB.';
    file.setCustomValidity(message);
    fileError.textContent = message;
    if (message) file.setAttribute('aria-invalid', 'true');
    else file.removeAttribute('aria-invalid');
  });
  form.addEventListener('submit', () => {
    if (!form.checkValidity()) return;
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    button.textContent = 'Guardando documento…';
  });
})();
