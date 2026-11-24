// Personal clave (24/11/2026): el tema y las horas solo se piden para los certificados de capacitación.
// Sin JavaScript los campos se ven siempre y el servidor valida igual.
(function () {
  "use strict";
  document.querySelectorAll("[data-staff-kind]").forEach(function (select) {
    var form = select.closest("form");
    var fields = form.querySelectorAll("[data-training]");
    var topic = form.querySelector("[name=training_topic]");
    function sync() {
      var training = select.value === "capacitacion";
      fields.forEach(function (field) { field.hidden = !training; });
      topic.required = training;
    }
    select.addEventListener("change", sync);
    sync();
  });
})();