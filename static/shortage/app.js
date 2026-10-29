/* ABB Shortage & Vendor Management — Application JavaScript */

/* --- Dismissible notifications --- */
(() => {
  document.querySelectorAll(".notice").forEach((notice) => {
    if (notice.querySelector("[data-dismiss-notice]")) return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "notice-close";
    button.dataset.dismissNotice = "";
    button.setAttribute("aria-label", "Dismiss notification");
    button.title = "Dismiss";
    button.textContent = "×";
    notice.append(button);
  });
  document.addEventListener("click", (event) => {
    const button = event.target.closest?.("[data-dismiss-notice]");
    if (button) button.closest(".notice")?.remove();
  });
})();

/* --- Theme Toggle --- */
(() => {
  const themeKey = "abb-theme";
  const toggle = document.getElementById("theme-toggle");
  if (!toggle) return;

  const setTheme = (theme, persist = false) => {
    document.documentElement.setAttribute("data-theme", theme);
    toggle.setAttribute("aria-label", theme === "dark" ? "Switch to light mode" : "Switch to dark mode");
    toggle.title = theme === "dark" ? "Switch to light mode" : "Switch to dark mode";
    if (persist) {
      try { localStorage.setItem(themeKey, theme); } catch (_) {}
    }
  };

  let saved = "light";
  try { saved = localStorage.getItem(themeKey) || "light"; } catch (_) {}
  setTheme(saved);

  toggle.addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-theme") || "light";
    setTheme(current === "dark" ? "light" : "dark", true);
  });
})();

/* --- Sidebar Toggle --- */
(() => {
  const storageKey = "abb-shortage-sidebar-collapsed";
  const button = document.querySelector(".sidebar-toggle");
  if (!button) return;

  let savedState = null;
  try { savedState = window.localStorage.getItem(storageKey); } catch (_) {}

  const setCollapsed = (collapsed, persist = false) => {
    document.body.classList.toggle("sidebar-collapsed", collapsed);
    button.setAttribute("aria-expanded", String(!collapsed));
    const label = collapsed ? "Expand workspace sidebar" : "Collapse workspace sidebar";
    button.setAttribute("aria-label", label);
    button.title = collapsed ? "Expand sidebar" : "Collapse sidebar";
    button.querySelector("span").textContent = collapsed ? "›" : "‹";
    if (persist) {
      try { window.localStorage.setItem(storageKey, String(collapsed)); } catch (_) {}
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

/* --- Collapsible Panels --- */
(() => {
  document.querySelectorAll("[data-collapsible-panel]").forEach((panel) => {
    const button = panel.querySelector(".panel-toggle");
    const content = panel.querySelector(".panel-content");
    if (!button || !content) return;

    const storageKey = `abb-panel-collapsed-${panel.dataset.panelKey}`;
    let collapsed = false;
    try { collapsed = window.localStorage.getItem(storageKey) === "true"; } catch (_) {}

    const setCollapsed = (value, persist = false) => {
      content.hidden = value;
      button.setAttribute("aria-expanded", String(!value));
      const title = value ? "Expand section" : "Collapse section";
      button.title = title;
      button.setAttribute("aria-label", `${title}: ${panel.dataset.panelKey.replaceAll("-", " ")}`);
      if (persist) {
        try { window.localStorage.setItem(storageKey, String(value)); } catch (_) {}
      }
    };

    setCollapsed(collapsed);
    button.addEventListener("click", () => setCollapsed(!content.hidden, true));
  });
})();

/* --- Filter Combo Boxes --- */
(() => {
  const combos = [...document.querySelectorAll(".filter-combo")];

  const close = (combo) => {
    const menu = combo.querySelector(".filter-combo-menu");
    const toggle = combo.querySelector(".filter-combo-toggle");
    if (menu) menu.hidden = true;
    if (toggle) toggle.setAttribute("aria-expanded", "false");
  };

  const render = (combo, query = "") => {
    const input = combo.querySelector("input");
    const menu = combo.querySelector(".filter-combo-menu");
    const optionsEl = document.getElementById(input.dataset.options);
    if (!optionsEl) return;
    const values = [...optionsEl.options].map((option) => option.value);
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
    if (!input || !toggle) return;
    toggle.addEventListener("click", () => {
      if (toggle.getAttribute("aria-expanded") === "true") close(combo);
      else render(combo);
    });
    input.addEventListener("focus", () => render(combo, input.value === "All" ? "" : input.value));
    input.addEventListener("input", () => render(combo, input.value === "All" ? "" : input.value));
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") close(combo);
      if (event.key === "ArrowDown" && combo.querySelector(".filter-combo-menu")?.hidden) render(combo);
    });
  });

  document.addEventListener("click", (event) => {
    combos.forEach((combo) => {
      if (!combo.contains(event.target)) close(combo);
    });
  });
})();

/* --- Remarks Dialog --- */
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

  const closeBtn = dialog.querySelector("[data-close-remarks]");
  if (closeBtn) closeBtn.addEventListener("click", () => dialog.close());
})();

/* --- Vendor Track Export/Open Button Sync --- */
(() => {
  const vendorInput = document.getElementById("vendor-filter-vendor");
  const exportForm = document.querySelector(".vendor-date-export-form");
  const openForm = document.querySelector(".excel-open-form");
  if (!vendorInput || !exportForm || !openForm) return;

  const exportButton = exportForm.querySelector("button[type='submit']");
  const openButton = openForm.querySelector("button[type='submit']");
  const exportVendor = exportForm.querySelector("input[name='vendor']");
  const openVendor = openForm.querySelector("input[name='vendor']");
  const optionsEl = document.getElementById(vendorInput.dataset.options);
  if (!optionsEl) return;
  const vendorOptions = [...optionsEl.options]
    .map((option) => option.value)
    .filter((value) => value && value.toLocaleLowerCase() !== "all");

  const updateExportButton = () => {
    const selected = vendorOptions.find((value) => value.toLocaleLowerCase() === vendorInput.value.trim().toLocaleLowerCase());
    if (exportVendor) exportVendor.value = selected || "";
    if (openVendor) openVendor.value = selected || "";
    if (exportButton) {
      exportButton.disabled = !selected;
      exportButton.title = selected ? `Export date request for ${selected}` : "Select one vendor in the filters first";
    }
    if (openButton) {
      openButton.disabled = !selected;
      openButton.title = selected ? `Open the latest date request for ${selected}` : "Select one vendor in the filters first";
      openButton.setAttribute("aria-label", selected ? `Open the latest exported vendor date request for ${selected} in Excel` : "Open the latest exported vendor date request in Excel");
    }
  };

  vendorInput.addEventListener("input", updateExportButton);
  vendorInput.addEventListener("change", updateExportButton);
  updateExportButton();
})();

/* --- Proposed Date Dialog --- */
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

  const closeBtn = dialog.querySelector("[data-close-proposed-date]");
  if (closeBtn) closeBtn.addEventListener("click", () => dialog.close());
})();

/* --- Animated counters on dashboard --- */
(() => {
  document.querySelectorAll("[data-animate-count]").forEach((el) => {
    const target = parseFloat(el.textContent.replace(/,/g, ""));
    if (isNaN(target) || target === 0) return;
    const duration = 600;
    const startTime = performance.now();
    const isInt = Number.isInteger(target);

    const step = (now) => {
      const elapsed = now - startTime;
      const progress = Math.min(elapsed / duration, 1);
      const eased = 1 - Math.pow(1 - progress, 3);
      const current = target * eased;
      el.textContent = isInt ? Math.round(current).toLocaleString() : current.toFixed(2);
      if (progress < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  });
})();
