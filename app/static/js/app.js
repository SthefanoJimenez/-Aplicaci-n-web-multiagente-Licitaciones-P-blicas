// JavaScript mínimo: validación del campo vacío, indicador de carga y ejemplos de descripción.
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

document.querySelectorAll("[data-example]").forEach((chip) => {
    chip.addEventListener("click", () => {
        const field = document.getElementById("description");
        if (!field) return;
        field.value = chip.dataset.example;
        field.removeAttribute("aria-invalid");
        const error = document.getElementById("description-error");
        if (error) error.hidden = true;
        field.focus();
    });
});

// Ventana con el valor al pasar el cursor (o el foco) por el gráfico mensual y el mapa.
// Los valores también están en las tablas de cada gráfico: la ventana solo los acerca.
document.querySelectorAll("[data-chart]").forEach((box) => {
    const tip = document.createElement("div");
    tip.className = "chart-tip";
    tip.hidden = true;
    box.appendChild(tip);
    const svg = box.querySelector("svg");
    const line = box.querySelector(".hover-line");
    const dot = box.querySelector(".dot");

    function show(target, clientX, clientY) {
        const strong = document.createElement("strong");
        strong.textContent = target.dataset.value;
        const label = document.createElement("span");
        label.textContent = target.dataset.label + (target.dataset.extra ? " · " + target.dataset.extra : "");
        tip.replaceChildren(strong, label);
        const rect = box.getBoundingClientRect();
        let x = clientX - rect.left;
        let y = clientY - rect.top;
        if (target.dataset.cx && svg) {
            const s = svg.getBoundingClientRect();
            const scale = s.width / svg.viewBox.baseVal.width;
            x = s.left - rect.left + Number(target.dataset.cx) * scale;
            y = s.top - rect.top + Number(target.dataset.cy) * scale;
            if (line) { line.setAttribute("x1", target.dataset.cx); line.setAttribute("x2", target.dataset.cx); line.style.opacity = 1; }
            if (dot) { dot.setAttribute("cx", target.dataset.cx); dot.setAttribute("cy", target.dataset.cy); dot.style.opacity = 1; }
        }
        tip.hidden = false;
        const half = tip.offsetWidth / 2 + 4;
        tip.style.left = Math.min(Math.max(x, half), Math.max(rect.width - half, half)) + "px";
        tip.style.top = y + "px";
    }
    function hide() {
        tip.hidden = true;
        if (line) line.style.opacity = 0;
        if (dot) dot.style.opacity = 0;
    }
    box.addEventListener("pointermove", (e) => {
        const target = e.target.closest("[data-label]");
        if (target) show(target, e.clientX, e.clientY); else hide();
    });
    box.addEventListener("pointerleave", hide);
    box.addEventListener("focusin", (e) => {
        const target = e.target.closest("[data-label]");
        if (!target) return;
        const r = target.getBoundingClientRect();
        show(target, r.left + r.width / 2, r.top);
    });
    box.addEventListener("focusout", hide);
});

// ---------------------------------------------------------------- Costo: solo números
// Acepta dígitos y un separador decimal (punto o coma). El servidor vuelve a validar.
document.querySelectorAll("[data-numeric]").forEach((input) => {
    const clean = (value) => {
        let v = value.replace(/[^0-9.,]/g, "");
        const sep = v.search(/[.,]/);
        if (sep !== -1) v = v.slice(0, sep + 1) + v.slice(sep + 1).replace(/[.,]/g, "");
        return v;
    };
    input.addEventListener("beforeinput", (e) => {
        if (!e.data) return;
        if (/[^0-9.,]/.test(e.data)) { e.preventDefault(); return; }
        // Un solo separador decimal: se ignora un segundo punto o coma
        const kept = input.value.slice(0, input.selectionStart) + input.value.slice(input.selectionEnd);
        if (/[.,]/.test(e.data) && /[.,]/.test(kept)) e.preventDefault();
    });
    input.addEventListener("input", () => {
        const pos = input.selectionStart;
        const before = input.value;
        const after = clean(before);
        if (after !== before) {
            input.value = after;
            const shift = before.length - after.length;
            input.setSelectionRange(Math.max(pos - shift, 0), Math.max(pos - shift, 0));
        }
    });
});

// ---------------------------------------------------------------- Carrusel de los cinco pasos
// Avanza solo cada 5 segundos y vuelve al inicio al llegar al final. Se pausa mientras el
// usuario lo usa (cursor encima, foco o toque) y no se mueve solo si el sistema pide reducir
// el movimiento. Los puntos indican posiciones, no tarjetas: con 3 tarjetas visibles de 5
// hay 3 posiciones, así que al llegar al final se marca el último punto.
document.querySelectorAll("[data-carousel]").forEach((carousel) => {
    const track = carousel.querySelector(".carousel-track");
    const slides = [...track.children];
    const dotsBox = carousel.querySelector(".carousel-dots");
    const prev = carousel.querySelector('[data-dir="-1"]');
    const next = carousel.querySelector('[data-dir="1"]');
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const DELAY = 5000;
    let timer = null;
    let paused = false;
    let stopped = false;
    const toggle = carousel.querySelector("[data-toggle]");
    if (toggle) {
        if (reduce) toggle.hidden = true;
        toggle.addEventListener("click", () => {
            stopped = !stopped;
            toggle.setAttribute("aria-pressed", stopped ? "true" : "false");
            toggle.setAttribute("aria-label", stopped ? "Reanudar el carrusel" : "Pausar el carrusel");
        });
    }

    const step = () => (slides.length > 1 ? slides[1].offsetLeft - slides[0].offsetLeft : track.clientWidth);
    const perView = () => Math.max(1, Math.round(track.clientWidth / step()));
    const positions = () => Math.max(1, slides.length - perView() + 1);
    const atEnd = () => track.scrollLeft + track.clientWidth >= track.scrollWidth - 4;
    const current = () => (atEnd() ? positions() - 1 : Math.min(Math.round(track.scrollLeft / step()), positions() - 1));

    function go(i) {
        const n = positions();
        const index = ((i % n) + n) % n;          // da la vuelta en los extremos
        track.scrollTo({ left: index * step(), behavior: reduce ? "auto" : "smooth" });
    }
    function buildDots() {
        const n = positions();
        if (dotsBox.children.length === n) return;
        dotsBox.replaceChildren(...Array.from({ length: n }, (_, k) => {
            const d = document.createElement("button");
            d.type = "button";
            d.setAttribute("role", "tab");
            d.setAttribute("aria-label", "Posición " + (k + 1) + " de " + n);
            d.addEventListener("click", () => { go(k); restart(); });
            return d;
        }));
    }
    function mark() {
        buildDots();
        const i = current();
        [...dotsBox.children].forEach((d, k) => d.setAttribute("aria-selected", k === i ? "true" : "false"));
        prev.disabled = false;
        next.disabled = false;
    }
    function restart() {
        window.clearInterval(timer);
        if (reduce || positions() < 2) return;
        timer = window.setInterval(() => { if (!paused && !stopped && !document.hidden) go(current() + 1); }, DELAY);
    }

    prev.addEventListener("click", () => { go(current() - 1); restart(); });
    next.addEventListener("click", () => { go(current() + 1); restart(); });
    track.addEventListener("keydown", (e) => {
        if (e.key === "ArrowRight") { e.preventDefault(); go(current() + 1); restart(); }
        if (e.key === "ArrowLeft") { e.preventDefault(); go(current() - 1); restart(); }
    });
    ["pointerenter", "focusin", "touchstart"].forEach((ev) => carousel.addEventListener(ev, () => { paused = true; }, { passive: true }));
    ["pointerleave", "focusout"].forEach((ev) => carousel.addEventListener(ev, () => { paused = false; }));
    track.addEventListener("touchend", () => { window.setTimeout(() => { paused = false; }, DELAY); }, { passive: true });
    track.addEventListener("scroll", () => window.requestAnimationFrame(mark), { passive: true });
    window.addEventListener("resize", () => { mark(); restart(); });
    mark();
    restart();
});

// ---------------------------------------------------------------- Recorrido guiado
// Resalta, en orden, los elementos con data-tour de cada pantalla. El recorrido sigue en la
// pantalla siguiente cuando el usuario hace clic en el botón indicado (data-tour-click).
// El estado vive en localStorage (solo en este navegador); si no está disponible, el
// recorrido funciona igual dentro de la pantalla actual.
(function () {
    const KEY = "licitafacil_tour";
    const store = {
        get() { try { return localStorage.getItem(KEY); } catch (e) { return null; } },
        set(v) { try { v ? localStorage.setItem(KEY, v) : localStorage.removeItem(KEY); } catch (e) { /* sin almacenamiento */ } },
    };
    const steps = [...document.querySelectorAll("[data-tour]")]
        .filter((el) => el.offsetParent !== null || el.getClientRects().length)
        .sort((a, b) => Number(a.dataset.tour) - Number(b.dataset.tour));

    let index = -1;
    let card = null;
    let target = null;

    function clear() {
        if (target) target.classList.remove("tour-target");
        target = null;
    }
    function end(finished) {
        clear();
        if (card) card.remove();
        card = null;
        document.body.classList.remove("tour-on");
        store.set(finished ? null : null);
    }
    function place() {
        if (!card || !target) return;
        const r = target.getBoundingClientRect();
        const c = card.getBoundingClientRect();
        const margin = 14;
        let top = r.bottom + margin;
        if (top + c.height > window.innerHeight - 8) top = Math.max(r.top - c.height - margin, 8);
        let left = Math.min(Math.max(r.left + r.width / 2 - c.width / 2, 12), window.innerWidth - c.width - 12);
        card.style.top = top + "px";
        card.style.left = left + "px";
    }
    function show(i) {
        clear();
        index = i;
        target = steps[i];
        target.classList.add("tour-target");
        target.scrollIntoView({ block: "center", behavior: "smooth" });
        const last = i === steps.length - 1;
        const clickToGo = target.hasAttribute("data-tour-click");
        const title = document.createElement("strong");
        title.textContent = target.dataset.tourTitle || "";
        const text = document.createElement("p");
        text.textContent = target.dataset.tourText || "";
        const count = document.createElement("span");
        count.className = "tour-count";
        count.textContent = (i + 1) + " de " + steps.length;
        const actions = document.createElement("div");
        actions.className = "tour-actions";
        const skip = document.createElement("button");
        skip.type = "button";
        skip.className = "tour-skip";
        skip.textContent = "Salir del recorrido";
        skip.addEventListener("click", () => end(false));
        actions.append(skip);
        if (i > 0) {
            const back = document.createElement("button");
            back.type = "button";
            back.className = "button button-secondary";
            back.textContent = "Atrás";
            back.addEventListener("click", () => show(i - 1));
            actions.append(back);
        }
        if (!clickToGo) {
            const next = document.createElement("button");
            next.type = "button";
            next.className = "button button-primary";
            const finale = last && target.hasAttribute("data-tour-last");
            next.textContent = finale ? "Terminar" : last ? "Entendido" : "Siguiente";
            next.addEventListener("click", () => (last ? end(finale) : show(i + 1)));
            actions.append(next);
        }
        card.replaceChildren(count, title, text, actions);
        window.setTimeout(place, 350);
        place();
    }
    function start() {
        if (!steps.length) { window.location.href = "/"; return; }
        store.set("on");
        document.body.classList.add("tour-on");
        if (!card) {
            card = document.createElement("div");
            card.className = "tour-card";
            card.setAttribute("role", "dialog");
            card.setAttribute("aria-live", "polite");
            document.body.appendChild(card);
        }
        show(0);
    }
    document.querySelectorAll("[data-tour-start]").forEach((b) => b.addEventListener("click", start));
    steps.forEach((el) => {
        if (!el.hasAttribute("data-tour-click")) return;
        el.addEventListener("click", () => { if (card) { clear(); card.remove(); card = null; } });
    });
    window.addEventListener("resize", place);
    window.addEventListener("scroll", () => window.requestAnimationFrame(place), { passive: true });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && card) end(false); });
    // Al llegar a una pantalla con el recorrido activo, continúa solo.
    if (store.get() === "on" && steps.length) window.setTimeout(start, 300);
    // En la pantalla final, al terminar se apaga para no volver a empezar solo.
})();