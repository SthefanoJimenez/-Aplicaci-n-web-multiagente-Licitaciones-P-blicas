/* Asistente de postulación flotante (HU-014).
   Envía la consulta al grafo de LangGraph y pinta la respuesta sin recargar la página. */
(function () {
  "use strict";

  var root = document.querySelector("[data-chat]");
  if (!root) return;

  var launcher = root.querySelector("[data-chat-launcher]");
  var panel = root.querySelector("[data-chat-panel]");
  var log = root.querySelector("[data-chat-log]");
  var form = root.querySelector("[data-chat-form]");
  var input = root.querySelector("[data-chat-input]");
  var send = root.querySelector("[data-chat-send]");
  var detail = root.querySelector("[data-chat-detail]");
  var detailBody = root.querySelector("[data-chat-detail-body]");
  var detailCount = root.querySelector("[data-chat-detail-count]");
  var detailClose = root.querySelector("[data-chat-detail-close]");
  if (!launcher || !panel || !log || !form || !input || !send) return;
  var welcome = root.getAttribute("data-welcome") || "Hola. ¿Qué vende su empresa?";

  var endpoint = root.getAttribute("data-endpoint");
  var clearUrl = root.getAttribute("data-clear");
  var csrf = root.getAttribute("data-csrf");
  var sending = false;

  // ------------------------------------------------------------------ posición
  var POSITION_KEY = "licitafacil_chat_pos";
  var dragging = null;

  function isMobile() {
    return window.matchMedia("(max-width: 640px)").matches;
  }

  function readPosition() {
    try {
      var raw = window.localStorage.getItem(POSITION_KEY);
      if (!raw) return null;
      var value = JSON.parse(raw);
      if (typeof value.left === "number" && typeof value.top === "number") return value;
    } catch (error) { /* sin almacenamiento o JSON inválido */ }
    return null;
  }

  function savePosition() {
    try {
      window.localStorage.setItem(POSITION_KEY, JSON.stringify({
        left: parseFloat(panel.style.left),
        top: parseFloat(panel.style.top),
      }));
    } catch (error) { /* modo privado */ }
  }

  function clampPosition(left, top) {
    var rect = panel.getBoundingClientRect();
    var width = rect.width || 480;
    var height = rect.height || 640;
    var maxLeft = Math.max(8, window.innerWidth - width - 8);
    var maxTop = Math.max(8, window.innerHeight - height - 8);
    return {
      left: Math.min(Math.max(8, left), maxLeft),
      top: Math.min(Math.max(8, top), maxTop),
    };
  }

  function moveTo(left, top) {
    var point = clampPosition(left, top);
    panel.style.left = point.left + "px";
    panel.style.top = point.top + "px";
  }

  function clearInlinePosition() {
    ["position", "left", "top", "right", "bottom", "margin"].forEach(function (prop) {
      panel.style.removeProperty(prop);
    });
  }

  // Pasa el panel de su posición anclada (abajo a la derecha) a coordenadas fijas,
  // para poder moverlo y conservar el sitio al cerrarlo y abrirlo.
  function placePanel() {
    if (isMobile()) {
      clearInlinePosition();
      return;
    }
    if (!panel.style.position) {
      var rect = panel.getBoundingClientRect();
      panel.style.position = "fixed";
      panel.style.left = rect.left + "px";
      panel.style.top = rect.top + "px";
      panel.style.right = "auto";
      panel.style.bottom = "auto";
      panel.style.margin = "0";
    }
    var saved = readPosition();
    if (saved) moveTo(saved.left, saved.top);
  }

  function resetPosition() {
    try { window.localStorage.removeItem(POSITION_KEY); } catch (error) { /* ignorar */ }
    clearInlinePosition();
    if (!panel.hidden) placePanel();
  }

  function onPointerDown(event) {
    if (isMobile() || event.button !== 0 || event.target.closest("button, a, input, textarea")) return;
    var rect = panel.getBoundingClientRect();
    dragging = { dx: event.clientX - rect.left, dy: event.clientY - rect.top };
    panel.classList.add("is-dragging");
    event.preventDefault();
  }

  function onPointerMove(event) {
    if (!dragging) return;
    moveTo(event.clientX - dragging.dx, event.clientY - dragging.dy);
  }

  function onPointerUp() {
    if (!dragging) return;
    dragging = null;
    panel.classList.remove("is-dragging");
    savePosition();
  }

  function scrollToEnd() {
    log.scrollTop = log.scrollHeight;
  }

  function bubble(role, html) {
    var row = document.createElement("div");
    row.className = "lf-chat-msg " + (role === "user" ? "is-user" : "is-bot");
    var body = document.createElement("div");
    body.className = "lf-chat-bubble" + (role === "user" ? "" : " markdown-body");
    if (role === "user") body.textContent = html;
    else body.innerHTML = html;
    row.appendChild(body);
    log.appendChild(row);
    scrollToEnd();
    return row;
  }

  function typing() {
    var row = document.createElement("div");
    row.className = "lf-chat-msg is-bot lf-chat-typing";
    row.setAttribute("aria-label", "El asistente está escribiendo");
    row.innerHTML = '<div class="lf-chat-bubble"><span></span><span></span><span></span></div>';
    log.appendChild(row);
    scrollToEnd();
    return row;
  }

  function setSending(value) {
    sending = value;
    send.disabled = value;
    form.classList.toggle("is-sending", value);
    panel.setAttribute("aria-busy", value ? "true" : "false");
  }

  function openDetail(html, count) {
    if (!detail || !detailBody) return;
    detailBody.innerHTML = html || "";
    detailBody.scrollTop = 0;
    if (detailCount) {
      detailCount.textContent = count ? count + (count === 1 ? " convocatoria" : " convocatorias") : "";
    }
    detail.hidden = false;
    panel.classList.add("is-detail");
    if (detailClose) detailClose.focus();
  }

  function closeDetail() {
    if (!detail || detail.hidden) return;
    detail.hidden = true;
    panel.classList.remove("is-detail");
    detailBody.innerHTML = "";
  }

  function open() {
    panel.hidden = false;
    launcher.setAttribute("aria-expanded", "true");
    document.body.classList.add("lf-chat-open");
    placePanel();
    scrollToEnd();
    window.setTimeout(function () { input.focus(); }, 40);
  }

  function close() {
    closeDetail();
    panel.hidden = true;
    launcher.setAttribute("aria-expanded", "false");
    document.body.classList.remove("lf-chat-open");
    launcher.focus();
  }

  function resetLog() {
    log.innerHTML = "";
    bubble("bot", "");
    log.lastElementChild.querySelector(".lf-chat-bubble").textContent = welcome;
  }

  function ask(query) {
    if (sending || !query.trim()) return;
    closeDetail();
    var chips = root.querySelector("[data-chat-chips]");
    if (chips) chips.remove();
    bubble("user", query.trim());
    input.value = "";
    input.style.height = "auto";
    var pending = typing();
    setSending(true);

    var body = new URLSearchParams();
    body.set("query", query.trim());
    body.set("csrf_token", csrf);

    fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8", "X-Requested-With": "fetch" },
      body: body.toString(),
      credentials: "same-origin"
    })
      .then(function (response) {
        if (response.redirected && response.url.indexOf("/ingresar") !== -1) {
          // La sesión venció mientras el panel estaba abierto.
          pending.remove();
          bubble("bot", "<p>Su sesión venció. <a href=\"" + response.url + "\">Ingrese de nuevo</a> para continuar.</p>")
            .querySelector(".lf-chat-bubble").classList.add("is-error");
          return null;
        }
        return response.json().catch(function () { return { ok: false, html: "" }; });
      })
      .then(function (data) {
        if (data === null) return;
        pending.remove();
        if (data && data.html) {
          var row = bubble("bot", data.html);
          if (!data.ok) row.querySelector(".lf-chat-bubble").classList.add("is-error");
          if (data.detail) {
            var more = document.createElement("button");
            more.type = "button";
            more.className = "lf-chat-more";
            more.textContent = "Ver detalle" + (data.detail_count ? " (" + data.detail_count + ")" : "");
            more.addEventListener("click", function () { openDetail(data.detail, data.detail_count); });
            row.querySelector(".lf-chat-bubble").appendChild(more);
          }
        } else {
          bubble("bot", "<p>No se pudo obtener una respuesta. Intente de nuevo.</p>")
            .querySelector(".lf-chat-bubble").classList.add("is-error");
        }
      })
      .catch(function () {
        pending.remove();
        bubble("bot", "<p>No hay conexión con el asistente en este momento. Revise su red e intente otra vez.</p>")
          .querySelector(".lf-chat-bubble").classList.add("is-error");
      })
      .finally(function () {
        setSending(false);
        scrollToEnd();
        input.focus();
      });
  }

  launcher.addEventListener("click", function () {
    if (panel.hidden) open(); else close();
  });
  root.querySelector("[data-chat-close]").addEventListener("click", close);

  // Arrastre del panel desde la cabecera; doble clic devuelve a la esquina.
  var head = root.querySelector(".lf-chat-head");
  if (head) {
    head.addEventListener("pointerdown", onPointerDown);
    head.addEventListener("dblclick", resetPosition);
    document.addEventListener("pointermove", onPointerMove);
    document.addEventListener("pointerup", onPointerUp);
    document.addEventListener("pointercancel", onPointerUp);
  }
  window.addEventListener("resize", function () {
    if (panel.hidden) return;
    if (isMobile()) {
      clearInlinePosition();
    } else if (!panel.style.position) {
      placePanel();
    } else {
      var rect = panel.getBoundingClientRect();
      moveTo(rect.left, rect.top);
    }
  });

  root.querySelector("[data-chat-clear]").addEventListener("click", function () {
    if (sending) return;
    fetch(clearUrl, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" },
      body: new URLSearchParams({ csrf_token: csrf }).toString(),
      credentials: "same-origin"
    }).then(function () {
      resetLog();
      input.focus();
    }).catch(function () {
      bubble("bot", "<p>No se pudo reiniciar la conversación. Intente de nuevo.</p>");
    });
  });

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    ask(input.value);
  });

  input.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      ask(input.value);
    }
  });

  // El campo crece con el texto hasta un máximo razonable.
  input.addEventListener("input", function () {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 120) + "px";
  });

  Array.prototype.forEach.call(root.querySelectorAll("[data-chat-prompt]"), function (chip) {
    chip.addEventListener("click", function () {
      var prompt = chip.getAttribute("data-chat-prompt");
      var chips = root.querySelector("[data-chat-chips]");
      if (chips) chips.remove();
      ask(prompt);
      input.focus();
    });
  });

  if (detailClose) detailClose.addEventListener("click", closeDetail);

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    if (detail && !detail.hidden) {
      closeDetail();
      return;
    }
    if (!panel.hidden) close();
  });

  // En pantallas grandes, un clic fuera del panel lo cierra. Se usa composedPath porque al tocar
  // un ejemplo el botón se elimina del DOM antes de que el evento llegue a document.
  document.addEventListener("click", function (event) {
    if (panel.hidden || window.matchMedia("(max-width: 640px)").matches) return;
    if (event.composedPath && event.composedPath().indexOf(root) !== -1) return;
    if (!root.contains(event.target)) close();
  });

  window.setTimeout(scrollToEnd, 60);
})();
