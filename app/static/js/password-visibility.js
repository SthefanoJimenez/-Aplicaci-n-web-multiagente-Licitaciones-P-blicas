// Solo cambia la presentación del campo; no copia ni conserva la contraseña.
(() => {
    for (const button of document.querySelectorAll("[data-password-toggle]")) {
        const field = document.getElementById(button.dataset.passwordToggle);
        if (!field || field.type !== "password") continue;
        const label = button.dataset.passwordLabel;
        const wrapper = field.closest("[data-password-wrapper]");
        function show(visible) {
            field.type = visible ? "text" : "password";
            const action = `${visible ? "Ocultar" : "Mostrar"} ${label}`;
            button.setAttribute("aria-label", action);
            button.title = action;
            button.classList.toggle("is-visible", visible);
        }
        button.hidden = false;
        button.addEventListener("click", () => show(field.type === "password"));
        wrapper.addEventListener("focusout", (event) => {
            if (!wrapper.contains(event.relatedTarget)) show(false);
        });
        field.form.addEventListener("submit", () => show(false));
        window.addEventListener("pagehide", () => show(false));
        window.addEventListener("pageshow", () => show(false));
    }
})();
