// JavaScript mínimo (SP-003): validación del campo vacío e indicador de carga.
// Si el navegador no ejecuta JavaScript, el servidor hace la misma validación.

document.querySelectorAll("form[data-cargando]").forEach((formulario) => {
    formulario.addEventListener("submit", (evento) => {
        const campo = formulario.querySelector("[data-requerido]");
        const error = formulario.querySelector(".error");

        if (campo && campo.value.trim() === "") {
            evento.preventDefault();
            campo.setAttribute("aria-invalid", "true");
            if (error) error.hidden = false;
            campo.focus();
            return;
        }

        const boton = formulario.querySelector("button[type=submit]");
        if (boton && boton.dataset.textoCargando) {
            boton.disabled = true;
            boton.textContent = boton.dataset.textoCargando;
        }
        const estado = formulario.querySelector(".estado-carga");
        if (estado) estado.hidden = false;
    });
});