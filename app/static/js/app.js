// JavaScript mínimo (SP-003): validación del campo vacío e indicador de carga.
// Si el navegador no ejecuta JavaScript, el servidor hace la misma validación.

document.querySelectorAll("form[data-loading]").forEach((form) => {
    form.addEventListener("submit", (event) => {
        const field = form.querySelector("[data-required]");
        const error = form.querySelector(".error");

        if (field && field.value.trim() === "") {
            event.preventDefault();
            field.setAttribute("aria-invalid", "true");
            if (error) error.hidden = false;
            field.focus();
            return;
        }

        const button = form.querySelector("button[type=submit]");
        if (button && button.dataset.loadingText) {
            button.disabled = true;
            button.setAttribute("aria-busy", "true");
            button.textContent = button.dataset.loadingText;
        }
        const status = form.querySelector(".loading-status");
        if (status) status.hidden = false;
    });
});