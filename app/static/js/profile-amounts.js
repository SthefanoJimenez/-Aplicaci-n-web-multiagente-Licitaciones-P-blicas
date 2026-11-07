// HU-008: límites de entrada y guía decimal; no se convierten ni redondean los montos.
(() => {
    const editable = /^(?:[0-9]{0,14}|[0-9]{1,14}[.,][0-9]{0,2})$/;
    const complete = /^[0-9]{1,14}([.,][0-9]{1,2})?$/;
    const refreshers = [];

    function reason(value) {
        if (!value) return "Escriba un monto. Si no tiene, escriba 0.";
        if (/[^0-9.,]/.test(value)) return "Use solo números, sin signos ni separadores de miles.";
        if ((value.match(/[.,]/g) || []).length > 1) return "Use un solo separador decimal: punto o coma.";
        if (/^[.,]/.test(value)) return "Escriba un cero antes del separador, por ejemplo: 0.50.";
        const [integer, fraction] = value.split(/[.,]/);
        if (integer.length > 14) return "Use como máximo 14 cifras antes del separador decimal.";
        if (fraction !== undefined && fraction.length > 2) return "Use como máximo dos cifras decimales.";
        if (fraction === "") return "Complete los decimales o quite el separador para dejar un monto entero.";
        return complete.test(value) ? "" : "Revise el monto: use hasta 14 cifras enteras y dos decimales.";
    }

    document.querySelectorAll("[data-amount-input]").forEach((wrapper) => {
        const field = wrapper.querySelector("[data-amount-field]");
        const guide = wrapper.querySelector(".amount-guide");
        const prefix = wrapper.querySelector("[data-amount-prefix]");
        const suffix = wrapper.querySelector("[data-amount-suffix]");
        if (!field || !guide || !prefix || !suffix) return;

        const feedback = document.createElement("p");
        feedback.id = `${field.id}-format-error`;
        feedback.className = "field-feedback field-feedback-error";
        feedback.setAttribute("role", "status");
        feedback.setAttribute("aria-atomic", "true");
        feedback.hidden = true;
        wrapper.insertAdjacentElement("afterend", feedback);
        const descriptions = new Set((field.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean));
        descriptions.add(feedback.id);
        field.setAttribute("aria-describedby", [...descriptions].join(" "));
        field.removeAttribute("placeholder");
        let touched = false;
        const snapshot = () => ({value: field.value, start: field.selectionStart, end: field.selectionEnd});
        let previous = editable.test(field.value) ? snapshot() : null;

        function clearServerError() {
            const original = document.getElementById(`${field.id}-error`);
            if (original) {
                original.hidden = true;
                const ids = (field.getAttribute("aria-describedby") || "").split(/\s+/);
                field.setAttribute("aria-describedby", ids.filter((id) => id !== original.id).join(" "));
            }
            const summary = field.closest(".profile-panel")?.querySelector('[role="alert"]');
            const item = summary?.querySelector(`[data-error-field="${field.name}"]`);
            if (item) {
                item.hidden = true;
                if ([...summary.querySelectorAll("li")].every((li) => li.hidden)) summary.hidden = true;
            }
        }

        function validate(show = touched) {
            const message = reason(field.value);
            field.setCustomValidity(message);
            feedback.textContent = show ? message : "";
            feedback.hidden = !feedback.textContent;
            const serverError = document.getElementById(`${field.id}-error`);
            if ((show && message) || (serverError && !serverError.hidden)) field.setAttribute("aria-invalid", "true");
            else field.removeAttribute("aria-invalid");
            return !message;
        }

        function refreshGuide() {
            const value = field.value;
            let remaining = "";
            if (!value) remaining = "0.00";
            else if (/^[0-9]{1,14}$/.test(value)) remaining = ".00";
            else {
                const decimal = value.match(/^[0-9]{1,14}[.,]([0-9]{0,2})$/);
                if (decimal) remaining = "0".repeat(2 - decimal[1].length);
            }
            prefix.textContent = value;
            suffix.textContent = remaining;
            guide.hidden = !remaining || field.scrollLeft > 0;
        }

        function blocked(candidate) {
            validate();
            feedback.textContent = `No se añadió esa entrada. ${reason(candidate)}`;
            feedback.hidden = false;
        }

        function proposed(text) {
            const start = field.selectionStart ?? field.value.length;
            const end = field.selectionEnd ?? start;
            return field.value.slice(0, start) + text + field.value.slice(end);
        }

        field.addEventListener("beforeinput", (event) => {
            if (editable.test(field.value)) previous = snapshot();
            if (event.isComposing || !event.inputType.startsWith("insert") || event.data === null) return;
            const candidate = proposed(event.data);
            if (!editable.test(candidate) && event.cancelable) {
                event.preventDefault();
                blocked(candidate);
            }
        });

        field.addEventListener("paste", (event) => {
            const text = event.clipboardData?.getData("text");
            if (text === undefined) return;
            const candidate = proposed(text);
            if (!editable.test(candidate)) {
                event.preventDefault();
                blocked(candidate);
            }
        });

        function entered(event) {
            if (event?.isComposing) { refreshGuide(); return; }
            const candidate = field.value;
            if (!editable.test(candidate) && previous !== null) {
                // Para autocompletado o eventos no cancelables: rechaza la operación entera.
                // Nunca elimina puntos de una cadena para convertirla en otro importe.
                field.value = previous.value;
                if (previous.start !== null) field.setSelectionRange(previous.start, previous.end);
                blocked(candidate);
            } else {
                clearServerError();
                if (editable.test(candidate)) previous = snapshot();
                validate(touched || !editable.test(candidate));
            }
            refreshGuide();
        }

        field.addEventListener("input", entered);
        field.addEventListener("change", entered);
        field.addEventListener("compositionend", entered);
        field.addEventListener("blur", () => { touched = true; validate(true); refreshGuide(); });
        field.addEventListener("focus", refreshGuide);
        field.addEventListener("scroll", refreshGuide);
        field.addEventListener("invalid", () => { touched = true; validate(true); });
        refreshers.push(() => { validate(); refreshGuide(); });
        validate();
        refreshGuide();
    });
    window.addEventListener("pageshow", () => refreshers.forEach((refresh) => refresh()));
})();
