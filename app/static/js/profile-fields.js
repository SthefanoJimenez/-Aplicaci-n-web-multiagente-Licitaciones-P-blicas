function profileFieldError(name, value) {
    const text = value.trim();
    if (name === "ruc") return !text || /^[0-9]{11}$/.test(text) ? "" : "Escriba los 11 dígitos del RUC.";
    if (name === "experience_years") {
        if (!/^[0-9]+$/.test(text)) return "Escriba un número entero entre 0 y 200.";
        if (Number(text) > 200) return "Fuera de rango: escriba un número entre 0 y 200.";
        return text.length > 3 ? "Escriba hasta tres dígitos." : "";
    }
    if (name === "rubros") {
        if (!text) return "Escriba al menos un nombre o código de rubro.";
        if (Array.from(text).length > 500) return "Use como máximo 500 caracteres.";
        if (text.includes(".")) return "No use puntos. Separe los rubros con comas.";
        const valid = text.split(",").map((part) => part.trim()).every((part) =>
            /^[0-9]{4}$/.test(part) ||
            (/\p{L}/u.test(part) && /^[\p{L}\p{M}0-9 \-/&+()'’]+$/u.test(part)));
        return valid ? "" : "Escriba nombres o códigos de cuatro dígitos, separados con comas.";
    }
    const count = Array.from(text).length;
    if (name === "company_name") return count >= 2 && count <= 200 ? "" : "Escriba una razón social de 2 a 200 caracteres.";
    const max = name === "business_activity" ? 300 : 3000;
    return count >= 1 && count <= max ? "" : "Complete este campo (máximo " + max + " caracteres).";
}

if (typeof module !== "undefined" && module.exports) module.exports = {profileFieldError};

if (typeof document !== "undefined") (() => {
    const form = document.querySelector("[data-company-profile]");
    if (!form) return;
    for (const name of ["ruc", "experience_years", "rubros", "company_name", "business_activity", "specialized_staff"]) {
        const field = form.elements.namedItem(name);
        const feedback = document.getElementById(name + "-entry-feedback");
        if (!field || !feedback) continue;

        function show(message) {
            if (feedback.textContent !== message) feedback.textContent = message;
        }

        function validate({reveal = true, edit = false} = {}) {
            const message = field.readOnly ? "" : profileFieldError(name, field.value);
            const serverError = document.getElementById(name === "specialized_staff" ? "staff-error" : name + "-error");
            if (edit) {
                if (serverError) serverError.hidden = true;
                const summary = form.closest(".profile-panel")?.querySelector('[role="alert"]');
                const item = summary?.querySelector('[data-error-field="' + name + '"]');
                if (item) item.hidden = true;
                if (summary && [...summary.querySelectorAll("li")].every((li) => li.hidden)) summary.hidden = true;
            }
            const serverVisible = serverError && !serverError.hidden;
            field.setCustomValidity(message);
            if (serverVisible || (reveal && message)) field.setAttribute("aria-invalid", "true");
            else field.removeAttribute("aria-invalid");
            show(reveal && !serverVisible ? message : "");
        }
        field.addEventListener("blur", () => validate());
        field.addEventListener("invalid", () => validate());
        validate({reveal: Boolean(field.value)});

        if (!field.hasAttribute("data-profile-digits")) {
            field.addEventListener("input", (event) => { if (!event.isComposing) validate({edit: true}); });
            field.addEventListener("compositionend", () => validate({edit: true}));
            continue;
        }
        const editable = (value) => /^[0-9]*$/.test(value) && value.length <= field.maxLength;
        const snapshot = () => ({value: field.value, start: field.selectionStart, end: field.selectionEnd});
        let previous = editable(field.value) ? snapshot() : null;

        function reject(candidate) {
            const range = name === "experience_years" && /^[0-9]+$/.test(candidate) && Number(candidate) > 200;
            show(range ? "No se añadió esa entrada. Fuera de rango: el máximo es 200."
                : /[^0-9]/.test(candidate)
                ? "No se añadió esa entrada. Use solo dígitos del 0 al 9."
                : "No se añadió esa entrada. El máximo es de " + field.maxLength + " dígitos.");
        }

        function checkInsertion(event, text) {
            if (editable(field.value)) previous = snapshot();
            if (text == null || !event.cancelable) return;
            const start = field.selectionStart ?? field.value.length;
            const end = field.selectionEnd ?? start;
            const candidate = field.value.slice(0, start) + text + field.value.slice(end);
            if (!editable(candidate)) {
                event.preventDefault();
                reject(candidate);
            }
        }

        field.addEventListener("beforeinput", (event) => {
            if (event.isComposing) return;
            const text = event.inputType?.startsWith("insert")
                ? event.data ?? event.dataTransfer?.getData("text/plain")
                : null;
            checkInsertion(event, text);
        });
        field.addEventListener("paste", (event) => {
            checkInsertion(event, event.clipboardData?.getData("text/plain"));
        });

        function checkInput(event) {
            if (event.isComposing) return;
            const candidate = field.value;
            if (editable(candidate)) {
                previous = snapshot();
                validate({edit: true});
            } else {
                if (previous !== null) {
                    field.value = previous.value;
                    if (previous.start !== null) field.setSelectionRange(previous.start, previous.end);
                }
                reject(candidate);
            }
        }

        field.addEventListener("input", checkInput);
        field.addEventListener("compositionend", (event) => {
            const committed = editable(field.value);
            checkInput(event);
            if (committed) field.dispatchEvent(new Event("input", {bubbles: true}));
        });
    }
})();
