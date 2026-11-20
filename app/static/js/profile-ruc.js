(() => {
    const button = document.querySelector("[data-ruc-endpoint]");
    if (!button) return;
    const form = button.form;
    const ruc = form.elements.ruc;
    const company = form.elements.company_name;
    const proof = form.elements.ruc_lookup_proof;
    const feedback = document.getElementById("ruc-lookup-feedback");
    const nameHint = document.getElementById("company_name-hint");
    const manualHint = "Escriba el nombre legal de la empresa tal como figura en su RUC.";
    let currentRuc = ruc.value;
    let matchedRuc = company.readOnly ? ruc.value : "";
    let manualName = matchedRuc ? "" : company.value;
    let pending = null;

    function syncButton() {
        button.disabled = Boolean(pending) || !/^[0-9]{11}$/.test(ruc.value);
    }

    function show(message, isError = false) {
        feedback.textContent = message;
        feedback.classList.toggle("field-feedback-error", isError);
    }

    function unlock() {
        matchedRuc = "";
        proof.value = "";
        company.readOnly = false;
        company.value = manualName;
        nameHint.textContent = manualHint;
    }

    ruc.addEventListener("input", (event) => {
        if (event.isComposing || ruc.value === currentRuc) return;
        currentRuc = ruc.value;
        if (pending) pending.abort();
        pending = null;
        if (matchedRuc && ruc.value !== matchedRuc) unlock();
        show("");
        syncButton();
    });
    company.addEventListener("input", () => {
        if (!company.readOnly) manualName = company.value;
    });
    button.addEventListener("click", async () => {
        if (button.disabled) return;
        const number = ruc.value;
        const request = new AbortController();
        pending = request;
        show("Consultando RUC…");
        syncButton();
        try {
            const response = await fetch(button.dataset.rucEndpoint, {
                method: "POST",
                body: new URLSearchParams({ruc: number, csrf_token: form.elements.csrf_token.value}),
                signal: request.signal,
            });
            if (pending !== request || ruc.value !== number) return;
            if (response.redirected) {
                show("La sesión venció. Ingrese de nuevo para consultar el RUC.", true);
                return;
            }
            const result = await response.json();
            if (pending !== request || ruc.value !== number) return;
            if (!response.ok) {
                show(result.message || "No se pudo consultar el RUC.", true);
                return;
            }
            if (result.ruc !== number || !result.company_name || !result.proof) {
                show("No se pudo consultar el RUC.", true);
                return;
            }
            matchedRuc = number;
            company.value = result.company_name;
            company.readOnly = true;
            proof.value = result.proof;
            nameHint.textContent = `Obtenida mediante ${feedback.dataset.source}. Cambie el RUC para editar este campo.`;
            show(`Estado informado: ${result.status} · Domicilio: ${result.condition}.`);
        } catch (_) {
            if (pending === request) show("No se pudo consultar el RUC. Puede completar la razón social a mano.", true);
        } finally {
            if (pending === request) {
                pending = null;
                syncButton();
            }
        }
    });
    button.hidden = false;
    syncButton();
})();
