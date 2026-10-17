// JavaScript mínimo (SP-003): validación del campo vacío e indicador de carga.
// Si el navegador no ejecuta JavaScript, el servidor hace la misma validación.

// Lleva al campo con error también cuando la validación vino del servidor.
document.querySelector('[data-required][aria-invalid="true"]')?.focus();

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
        const message = form.querySelector(".loading-message");
        if (message) message.textContent = "Esto puede tardar unos segundos.";
    });
});
