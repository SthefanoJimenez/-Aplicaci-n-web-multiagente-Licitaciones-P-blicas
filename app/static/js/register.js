// HU-008: ayuda al completar el registro. El servidor conserva la validación definitiva.
(() => {
    const form = document.querySelector("[data-registration-form]");
    if (!form) return;

    const names = ["company_name", "email", "password", "password_confirm"];
    const fields = Object.fromEntries(names.map((name) => [name, form.elements.namedItem(name)]));
    const button = form.querySelector('[type="submit"]');
    const help = document.getElementById("registration-help");
    const announcement = document.getElementById("registration-announcement");
    const summary = form.previousElementSibling?.matches('[role="alert"]') ? form.previousElementSibling : null;
    const touched = new Set();
    const serverErrors = new Map();
    const feedback = new Map();
    let announceTimer;

    // El desplegable nativo sigue funcionando por clic y teclado sin JavaScript.
    // Con JavaScript también se abre al pasar el puntero y al recibir el foco.
    const passwordHelp = form.querySelector("[data-password-help]");
    if (passwordHelp) {
        const trigger = passwordHelp.querySelector("summary");
        let pinned = false;
        let hovered = false;
        const closeHelp = () => {
            pinned = false;
            passwordHelp.open = false;
        };
        passwordHelp.addEventListener("mouseenter", () => {
            // En pantallas táctiles el clic controla la apertura y el cierre.
            if (window.matchMedia("(hover: hover)").matches) {
                hovered = true;
                passwordHelp.open = true;
            }
        });
        passwordHelp.addEventListener("mouseleave", () => {
            hovered = false;
            if (!pinned && !passwordHelp.contains(document.activeElement)) passwordHelp.open = false;
        });
        passwordHelp.addEventListener("focusin", () => { passwordHelp.open = true; });
        passwordHelp.addEventListener("focusout", (event) => {
            if (!pinned && !hovered && !passwordHelp.contains(event.relatedTarget)) passwordHelp.open = false;
        });
        trigger.addEventListener("click", (event) => {
            event.preventDefault();
            pinned = !pinned;
            passwordHelp.open = pinned;
        });
        document.addEventListener("pointerdown", (event) => {
            if (!passwordHelp.contains(event.target)) closeHelp();
        });
        document.addEventListener("keydown", (event) => {
            if (event.key === "Escape" && passwordHelp.open) closeHelp();
        });
    }

    // Se añade sin reemplazar los identificadores ni los mensajes que devuelve Flask.
    for (const name of names) {
        const field = fields[name];
        const original = document.getElementById(`${name}-error`);
        if (original) serverErrors.set(name, original.textContent.trim());
        const message = document.createElement("p");
        message.id = `${name}-feedback`;
        message.className = "field-feedback";
        message.hidden = true;
        field.insertAdjacentElement("afterend", message);
        const descriptions = new Set((field.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean));
        descriptions.add(message.id);
        field.setAttribute("aria-describedby", [...descriptions].join(" "));
        feedback.set(name, message);
    }

    const length = (value) => Array.from(value).length;
    function errorFor(name) {
        if (serverErrors.has(name)) return serverErrors.get(name);
        const value = fields[name].value;
        if (name === "company_name") {
            const count = length(value.trim());
            if (!count) return "Escriba el nombre de su empresa.";
            if (count < 2 || count > 200) return "Use entre 2 y 200 caracteres para el nombre de la empresa.";
        } else if (name === "email") {
            const email = value.trim();
            if (!email) return "Escriba su correo electrónico.";
            if (length(email) > 254 || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/u.test(email)) {
                return "Escriba un correo válido, por ejemplo: empresa@ejemplo.com.";
            }
        } else if (name === "password") {
            const count = length(value);
            const issues = [];
            if (count < 4) {
                const missing = 4 - count;
                issues.push(missing === 1 ? "Falta 1 carácter para llegar al mínimo de 4." : `Faltan ${missing} caracteres para llegar al mínimo de 4.`);
            }
            if (count > 128) issues.push("Use como máximo 128 caracteres.");
            const missing = [];
            if (!/[A-Z]/u.test(value)) missing.push("una mayúscula (A-Z)");
            if (!/[a-z]/u.test(value)) missing.push("una minúscula (a-z)");
            if (!/[0-9]/u.test(value)) missing.push("un número");
            if (!/[^\p{L}\p{N}\s]/u.test(value)) missing.push("un símbolo");
            if (missing.length) {
                const list = missing.length === 1 ? missing[0] : `${missing.slice(0, -1).join(", ")} y ${missing[missing.length - 1]}`;
                issues.push(`${missing.length === 1 ? "Falta" : "Faltan"} ${list}.`);
            }
            return issues.join(" ");
        } else {
            if (!value) return "Repita la contraseña.";
            if (value !== fields.password.value) return "Las contraseñas no coinciden.";
        }
        return "";
    }

    function clearServerError(name) {
        serverErrors.delete(name);
        const original = document.getElementById(`${name}-error`);
        if (original) {
            original.hidden = true;
            const descriptions = (fields[name].getAttribute("aria-describedby") || "").split(/\s+/);
            fields[name].setAttribute("aria-describedby", descriptions.filter((id) => id !== original.id).join(" "));
        }
        if (summary) summary.hidden = true;
    }

    function refresh() {
        const errors = names.map(errorFor);
        for (const [index, name] of names.entries()) {
            const field = fields[name];
            const message = feedback.get(name);
            const show = touched.has(name) || serverErrors.has(name);
            const original = document.getElementById(`${name}-error`);
            // El error del servidor ya está junto al campo; no se duplica.
            if (serverErrors.has(name) && original && !original.hidden) {
                message.hidden = true;
                field.setAttribute("aria-invalid", "true");
                continue;
            }
            const success = !errors[index] && field.value && (name === "password" || name === "password_confirm");
            message.textContent = show ? (errors[index] || (success ? (name === "password" ? "La contraseña cumple los requisitos." : "Las contraseñas coinciden.") : "")) : "";
            message.hidden = !message.textContent;
            message.classList.toggle("field-feedback-error", show && Boolean(errors[index]));
            message.classList.toggle("field-feedback-success", show && Boolean(success));
            if (show && errors[index]) field.setAttribute("aria-invalid", "true");
            else field.removeAttribute("aria-invalid");
        }
        const ready = errors.every((error) => !error);
        // aria-disabled mantiene el botón accesible con Tab y permite explicar qué falta.
        button.setAttribute("aria-disabled", String(!ready));
        help.textContent = ready
            ? "Los campos están completos. Ya puede crear su cuenta."
            : "Complete los cuatro campos para crear su cuenta.";
        return errors;
    }

    function announce(name, immediate = false) {
        clearTimeout(announceTimer);
        announceTimer = setTimeout(() => {
            const message = feedback.get(name);
            announcement.textContent = message.hidden ? "" : message.textContent;
        }, immediate ? 0 : 650);
    }

    for (const name of names) {
        const field = fields[name];
        field.addEventListener("input", () => {
            clearServerError(name);
            // Los campos de texto no se marcan como erróneos antes de salir de ellos.
            if (name === "password" || name === "password_confirm") touched.add(name);
            if (name === "password") clearServerError("password_confirm");
            refresh();
            announce(name === "password" && touched.has("password_confirm") ? "password_confirm" : name);
        });
        field.addEventListener("blur", () => {
            touched.add(name);
            refresh();
            announce(name, true);
        });
        field.addEventListener("change", () => {
            clearServerError(name);
            refresh();
        });
    }

    form.addEventListener("submit", (event) => {
        names.forEach((name) => touched.add(name));
        const errors = refresh();
        const firstError = errors.findIndex(Boolean);
        if (firstError !== -1) {
            event.preventDefault();
            fields[names[firstError]].focus();
            announcement.textContent = errors[firstError];
        }
    });
    // Si JavaScript no carga, permanecen las restricciones HTML y la validación en Flask.
    form.noValidate = true;
    window.addEventListener("pageshow", refresh);
    refresh();
})();
