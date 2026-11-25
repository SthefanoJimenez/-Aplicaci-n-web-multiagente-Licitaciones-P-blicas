// Lista de profesiones de Personal clave (24/11/2026).
// La lista nativa (<datalist>) de Safari mide lo mismo que la caja y corta los nombres largos
// («Ingeniero de…»). Esta lista propia muestra cada nombre completo, en varias líneas si hace falta.
// Sin JavaScript queda la lista nativa.
(function () {
  "use strict";

  function plain(text) {
    return (text || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().trim();
  }

  function setup(input) {
    var source = document.getElementById(input.getAttribute("list"));
    if (!source) return;
    var labels = Array.prototype.map.call(source.querySelectorAll("option"), function (o) { return o.value; });
    if (!labels.length) return;

    var listId = input.id + "-lista";
    var wrapper = document.createElement("div");
    wrapper.className = "combo";
    input.parentNode.insertBefore(wrapper, input);
    wrapper.appendChild(input);

    var toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "combo-toggle";
    toggle.tabIndex = -1;
    toggle.setAttribute("aria-label", "Ver la lista de profesiones");
    toggle.innerHTML = '<svg viewBox="0 0 20 20" width="16" height="16" aria-hidden="true"><path d="M5 7.5l5 5 5-5" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    wrapper.appendChild(toggle);

    var list = document.createElement("ul");
    list.id = listId;
    list.className = "combo-list";
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-label", "Profesiones sugeridas");
    list.hidden = true;
    wrapper.appendChild(list);

    input.removeAttribute("list");
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-controls", listId);
    input.setAttribute("aria-expanded", "false");

    var shown = [];
    var active = -1;

    function render() {
      var query = plain(input.value);
      shown = labels.filter(function (label) { return !query || plain(label).indexOf(query) !== -1; });
      if (shown.length === 1 && plain(shown[0]) === query) shown = labels.slice();   // ya eligió: mostrar todas
      list.innerHTML = "";
      shown.forEach(function (label, i) {
        var item = document.createElement("li");
        item.id = listId + "-" + i;
        item.className = "combo-option";
        item.setAttribute("role", "option");
        item.setAttribute("aria-selected", "false");
        item.textContent = label;
        item.addEventListener("mousedown", function (event) { event.preventDefault(); choose(label); });
        list.appendChild(item);
      });
      active = -1;
      input.removeAttribute("aria-activedescendant");
      return shown.length;
    }

    function open() {
      if (!render()) { close(); return; }
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
    }

    function close() {
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      active = -1;
    }

    function highlight(index) {
      var items = list.children;
      if (!items.length) return;
      if (active >= 0 && items[active]) items[active].setAttribute("aria-selected", "false");
      active = (index + items.length) % items.length;
      items[active].setAttribute("aria-selected", "true");
      input.setAttribute("aria-activedescendant", items[active].id);
      items[active].scrollIntoView({ block: "nearest" });
    }

    function choose(label) {
      input.value = label;
      close();
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }

    input.addEventListener("input", function (event) { if (event.isTrusted) open(); });
    input.addEventListener("click", function () { if (list.hidden) open(); });
    input.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        if (list.hidden) open();
        highlight(active + (event.key === "ArrowDown" ? 1 : -1));
      } else if (event.key === "Enter" && !list.hidden && active >= 0) {
        event.preventDefault();
        choose(shown[active]);
      } else if (event.key === "Escape" && !list.hidden) {
        event.preventDefault();
        close();
      } else if (event.key === "Tab") {
        close();
      }
    });
    input.addEventListener("blur", close);
    toggle.addEventListener("mousedown", function (event) {
      event.preventDefault();
      if (list.hidden) { input.focus(); open(); } else { close(); }
    });
  }

  document.querySelectorAll("input[data-combobox][list]").forEach(setup);
})();