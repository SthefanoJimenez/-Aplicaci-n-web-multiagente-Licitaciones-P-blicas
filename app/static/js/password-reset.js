/* El fragmento no se transmite al servidor ni a su registro de accesos. */
(() => {
  const field = document.getElementById('reset_token');
  if (field && window.location.hash) {
    const token = window.location.hash.slice(1);
    if (/^[A-Za-z0-9_-]{43}$/.test(token)) field.value = token;
    window.history.replaceState(null, '', window.location.pathname);
  }
})();
