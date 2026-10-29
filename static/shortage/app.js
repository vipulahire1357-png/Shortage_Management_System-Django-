(() => {
  const storageKey = "abb-shortage-sidebar-collapsed";
  const button = document.querySelector(".sidebar-toggle");
  if (!button) return;

  let savedState = null;
  try {
    savedState = window.localStorage.getItem(storageKey);
  } catch (_) {
    // The sidebar remains usable if browser storage is unavailable.
  }

  const setCollapsed = (collapsed, persist = false) => {
    document.body.classList.toggle("sidebar-collapsed", collapsed);
    button.setAttribute("aria-expanded", String(!collapsed));
    const label = collapsed ? "Expand workspace sidebar" : "Collapse workspace sidebar";
    button.setAttribute("aria-label", label);
    button.title = collapsed ? "Expand sidebar" : "Collapse sidebar";
    button.querySelector("span").textContent = collapsed ? "›" : "‹";
    if (persist) {
      try {
        window.localStorage.setItem(storageKey, String(collapsed));
      } catch (_) {
        // Keep the current page state even if browser storage is unavailable.
      }
    }
  };

  const initialState = savedState === null
    ? window.matchMedia("(max-width: 680px)").matches
    : savedState === "true";
  setCollapsed(initialState);
  button.addEventListener("click", () => {
    setCollapsed(!document.body.classList.contains("sidebar-collapsed"), true);
  });
})();

(() => {
  document.querySelectorAll("[data-collapsible-panel]").forEach((panel) => {
    const button = panel.querySelector(".panel-toggle");
    const content = panel.querySelector(".panel-content");
    if (!button || !content) return;

    const storageKey = `abb-panel-collapsed-${panel.dataset.panelKey}`;
    let collapsed = false;
    try {
      collapsed = window.localStorage.getItem(storageKey) === "true";
    } catch (_) {
      // Section toggles remain available for this page if storage is blocked.
    }

    const setCollapsed = (value, persist = false) => {
      content.hidden = value;
      button.setAttribute("aria-expanded", String(!value));
      const title = value ? "Expand section" : "Collapse section";
      button.title = title;
      button.setAttribute("aria-label", `${title}: ${panel.dataset.panelKey.replaceAll("-", " ")}`);
      if (persist) {
        try {
          window.localStorage.setItem(storageKey, String(value));
        } catch (_) {
          // Keep the current page state even if browser storage is unavailable.
        }
      }
    };

    setCollapsed(collapsed);
    button.addEventListener("click", () => setCollapsed(!content.hidden, true));
  });
})();

(() => {
  const combos = [...document.querySelectorAll(".filter-combo")];

  const close = (combo) => {
    const menu = combo.querySelector(".filter-combo-menu");
    const toggle = combo.querySelector(".filter-combo-toggle");
    menu.hidden = true;
    toggle.setAttribute("aria-expanded", "false");
  };

  const render = (combo, query = "") => {
    const input = combo.querySelector("input");
    const menu = combo.querySelector(".filter-combo-menu");
    const values = [...document.getElementById(input.dataset.options).options].map((option) => option.value);
    const normalizedQuery = query.trim().toLocaleLowerCase();
    const matching = normalizedQuery
      ? values.filter((value) => value.toLocaleLowerCase().includes(normalizedQuery))
      : values;
    menu.replaceChildren();

    matching.forEach((value) => {
      const option = document.createElement("button");
      option.type = "button";
      option.className = "filter-combo-option";
      option.setAttribute("role", "option");
      option.setAttribute("aria-selected", String(input.value === value));
      option.textContent = value;
      option.addEventListener("click", () => {
        input.value = value;
        input.dispatchEvent(new Event("change", { bubbles: true }));
        close(combo);
      });
      menu.append(option);
    });

    if (!matching.length) {
      const empty = document.createElement("span");
      empty.className = "filter-combo-empty";
      empty.textContent = "No matching options";
      menu.append(empty);
    }
    menu.hidden = false;
    combo.querySelector(".filter-combo-toggle").setAttribute("aria-expanded", "true");
  };

  combos.forEach((combo) => {
    const input = combo.querySelector("input");
    const toggle = combo.querySelector(".filter-combo-toggle");
    toggle.addEventListener("click", () => {
      if (toggle.getAttribute("aria-expanded") === "true") close(combo);
      else render(combo);
    });
    input.addEventListener("focus", () => render(combo, input.value === "All" ? "" : input.value));
    input.addEventListener("input", () => render(combo, input.value === "All" ? "" : input.value));
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") close(combo);
      if (event.key === "ArrowDown" && combo.querySelector(".filter-combo-menu").hidden) render(combo);
    });
  });

  document.addEventListener("click", (event) => {
    combos.forEach((combo) => {
      if (!combo.contains(event.target)) close(combo);
    });
  });
})();

(() => {
  const dialog = document.getElementById("remarks-dialog");
  if (!dialog) return;

  const form = dialog.querySelector("form");
  const fields = {
    project_id: "project",
    material_ref_code: "ref",
    wbs_element: "wbs",
    material_code: "material",
    remarks: "remarks",
  };

  document.querySelectorAll(".remarks-edit-trigger").forEach((trigger) => {
    trigger.addEventListener("click", () => {
      Object.entries(fields).forEach(([fieldName, dataName]) => {
        form.elements[fieldName].value = trigger.dataset[dataName] || "";
      });
      dialog.showModal();
      form.elements.remarks.focus();
    });
  });

  dialog.querySelector("[data-close-remarks]").addEventListener("click", () => dialog.close());
})();

(() => {
  const vendorInput = document.getElementById("vendor-filter-vendor");
  const exportForm = document.querySelector(".vendor-date-export-form");
  const openForm = document.querySelector(".excel-open-form");
  if (!vendorInput || !exportForm || !openForm) return;

  const exportButton = exportForm.querySelector("button[type='submit']");
  const openButton = openForm.querySelector("button[type='submit']");
  const exportVendor = exportForm.querySelector("input[name='vendor']");
  const openVendor = openForm.querySelector("input[name='vendor']");
  const vendorOptions = [...document.getElementById(vendorInput.dataset.options).options]
    .map((option) => option.value)
    .filter((value) => value && value.toLocaleLowerCase() !== "all");

  const updateExportButton = () => {
    const selected = vendorOptions.find((value) => value.toLocaleLowerCase() === vendorInput.value.trim().toLocaleLowerCase());
    exportVendor.value = selected || "";
    openVendor.value = selected || "";
    exportButton.disabled = !selected;
    openButton.disabled = !selected;
    exportButton.title = selected ? `Export date request for ${selected}` : "Select one vendor in the filters first";
    openButton.title = selected ? `Open the latest date request for ${selected}` : "Select one vendor in the filters first";
    openButton.setAttribute("aria-label", selected ? `Open the latest exported vendor date request for ${selected} in Excel` : "Open the latest exported vendor date request in Excel");
  };

  vendorInput.addEventListener("input", updateExportButton);
  vendorInput.addEventListener("change", updateExportButton);
  updateExportButton();
})();

(() => {
  const dialog = document.getElementById("proposed-date-dialog");
  if (!dialog) return;
  const form = dialog.querySelector("form");
  const input = form.elements.proposed_date;

  document.querySelectorAll(".proposed-date-edit").forEach((button) => {
    button.addEventListener("click", () => {
      form.elements.row_number.value = button.dataset.rowNumber;
      input.value = button.dataset.proposedDate || "";
      dialog.showModal();
      input.focus();
    });
  });

  dialog.querySelector("[data-close-proposed-date]").addEventListener("click", () => dialog.close());
})();
