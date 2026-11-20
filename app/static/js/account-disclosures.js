(() => {
    const disclosures = document.querySelectorAll("details[data-account-disclosure]");
    if (!disclosures.length) return;

    function closeOutside(event) {
        disclosures.forEach((disclosure) => {
            if (disclosure.open && !disclosure.contains(event.target)) disclosure.open = false;
        });
    }

    document.addEventListener("click", closeOutside);
    document.addEventListener("focusin", closeOutside);
    document.addEventListener("keydown", (event) => {
        if (event.key !== "Escape" || event.defaultPrevented) return;
        const disclosure = event.target.closest?.("details[data-account-disclosure]");
        if (!disclosure?.open) return;
        event.preventDefault();
        disclosure.open = false;
        disclosure.querySelector("summary").focus();
    });
})();
