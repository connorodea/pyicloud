(() => {
  "use strict";

  const state = {
    config: null,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
    eventType: "video",
    slotsByDay: new Map(),
    selectedDay: null,
    selectedSlot: null,
    idempotencyKey: null,
    turnstileToken: "",
    turnstileWidget: null,
  };

  const elements = {
    ownerName: document.querySelector("#owner-name"),
    timezoneBadge: document.querySelector("#timezone-badge"),
    eventTypes: document.querySelector("#event-types"),
    monthLabel: document.querySelector("#month-label"),
    dateStrip: document.querySelector("#date-strip"),
    slotStatus: document.querySelector("#slot-status"),
    timeGrid: document.querySelector("#time-grid"),
    refresh: document.querySelector("#refresh-button"),
    timePanel: document.querySelector("#time-panel"),
    detailsPanel: document.querySelector("#details-panel"),
    confirmationPanel: document.querySelector("#confirmation-panel"),
    back: document.querySelector("#back-button"),
    summary: document.querySelector("#selection-summary"),
    form: document.querySelector("#booking-form"),
    phoneField: document.querySelector("#phone-field"),
    formError: document.querySelector("#form-error"),
    confirm: document.querySelector("#confirm-button"),
    confirmationCopy: document.querySelector("#confirmation-copy"),
    calendarLink: document.querySelector("#calendar-link"),
  };

  const api = async (path, options = {}) => {
    const response = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(payload.detail || "Something went wrong. Please try again.");
      error.status = response.status;
      throw error;
    }
    return payload;
  };

  const dateKey = (value, timezone = state.timezone) => {
    const parts = new Intl.DateTimeFormat("en-US", {
      timeZone: timezone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    }).formatToParts(value instanceof Date ? value : new Date(value));
    const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    return `${values.year}-${values.month}-${values.day}`;
  };

  const ownerToday = () => dateKey(new Date(), state.config.owner_timezone);

  const eventDefinition = () =>
    state.config.event_types.find((item) => item.id === state.eventType);

  const renderEventTypes = () => {
    elements.eventTypes.innerHTML = "";
    for (const item of state.config.event_types) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "event-type";
      button.setAttribute("role", "radio");
      button.setAttribute("aria-checked", String(item.id === state.eventType));
      button.dataset.eventType = item.id;
      button.innerHTML = `<strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(item.description)}</span><em>${item.duration_minutes} minutes</em>`;
      button.addEventListener("click", () => selectEventType(item.id));
      elements.eventTypes.appendChild(button);
    }
  };

  const selectEventType = async (eventType) => {
    if (state.eventType === eventType) return;
    state.eventType = eventType;
    state.selectedSlot = null;
    state.idempotencyKey = null;
    renderEventTypes();
    showTimePanel();
    await loadAvailability();
  };

  const loadAvailability = async () => {
    elements.refresh.classList.add("loading");
    elements.refresh.disabled = true;
    elements.slotStatus.textContent = "Checking every connected calendar…";
    elements.timeGrid.innerHTML = "";
    elements.dateStrip.innerHTML = "";
    try {
      const params = new URLSearchParams({
        event_type: state.eventType,
        from_date: ownerToday(),
        days: "14",
        timezone: state.timezone,
      });
      const payload = await api(`/api/v1/availability?${params}`);
      state.slotsByDay = new Map();
      for (const slot of payload.slots) {
        const key = dateKey(slot.start);
        if (!state.slotsByDay.has(key)) state.slotsByDay.set(key, []);
        state.slotsByDay.get(key).push(slot);
      }
      const days = [...state.slotsByDay.keys()];
      if (!days.length) {
        state.selectedDay = null;
        elements.monthLabel.textContent = "No openings yet";
        elements.slotStatus.textContent = "There are no open times in the next two weeks. Please check back soon.";
        return;
      }
      state.selectedDay = state.slotsByDay.has(state.selectedDay) ? state.selectedDay : days[0];
      renderDates();
      renderTimes();
    } catch (error) {
      elements.monthLabel.textContent = "Availability paused";
      elements.slotStatus.textContent = error.message;
    } finally {
      elements.refresh.classList.remove("loading");
      elements.refresh.disabled = false;
    }
  };

  const renderDates = () => {
    elements.dateStrip.innerHTML = "";
    for (const key of state.slotsByDay.keys()) {
      const date = new Date(`${key}T12:00:00`);
      const weekday = new Intl.DateTimeFormat("en-US", { weekday: "short" }).format(date);
      const day = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric" }).format(date);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "date-button";
      button.setAttribute("role", "tab");
      button.setAttribute("aria-selected", String(key === state.selectedDay));
      button.innerHTML = `<span>${escapeHtml(weekday)}</span><strong>${escapeHtml(day)}</strong>`;
      button.addEventListener("click", () => {
        state.selectedDay = key;
        renderDates();
        renderTimes();
      });
      elements.dateStrip.appendChild(button);
    }
  };

  const renderTimes = () => {
    const slots = state.slotsByDay.get(state.selectedDay) || [];
    const selectedDate = new Date(`${state.selectedDay}T12:00:00`);
    elements.monthLabel.textContent = new Intl.DateTimeFormat("en-US", {
      month: "long",
      year: "numeric",
    }).format(selectedDate);
    elements.slotStatus.textContent = `${slots.length} available ${slots.length === 1 ? "time" : "times"}`;
    elements.timeGrid.innerHTML = "";
    for (const slot of slots) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "time-button";
      button.setAttribute("role", "listitem");
      button.textContent = new Intl.DateTimeFormat("en-US", {
        hour: "numeric",
        minute: "2-digit",
      }).format(new Date(slot.start));
      button.addEventListener("click", () => selectSlot(slot));
      elements.timeGrid.appendChild(button);
    }
  };

  const selectSlot = (slot) => {
    state.selectedSlot = slot;
    state.idempotencyKey = window.crypto?.randomUUID?.() || `${Date.now()}-${Math.random()}`;
    const definition = eventDefinition();
    const formatted = new Intl.DateTimeFormat("en-US", {
      weekday: "long",
      month: "long",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      timeZoneName: "short",
    }).format(new Date(slot.start));
    elements.summary.innerHTML = `<strong>${escapeHtml(definition.name)}</strong><br>${escapeHtml(formatted)} · ${definition.duration_minutes} minutes`;
    const phoneInput = elements.form.elements.phone;
    const isPhone = state.eventType === "phone";
    elements.phoneField.hidden = !isPhone;
    phoneInput.required = isPhone;
    elements.timePanel.hidden = true;
    elements.detailsPanel.hidden = false;
    elements.formError.hidden = true;
    window.scrollTo({ top: document.querySelector("#booking-shell").offsetTop - 24, behavior: "smooth" });
    setTimeout(() => elements.form.elements.name.focus(), 50);
    renderTurnstile();
  };

  const showTimePanel = () => {
    elements.detailsPanel.hidden = true;
    elements.confirmationPanel.hidden = true;
    elements.timePanel.hidden = false;
  };

  const renderTurnstile = () => {
    if (!state.config.turnstile_site_key || state.turnstileWidget !== null) return;
    const attempt = () => {
      if (!window.turnstile) {
        window.setTimeout(attempt, 150);
        return;
      }
      state.turnstileWidget = window.turnstile.render("#turnstile-widget", {
        sitekey: state.config.turnstile_site_key,
        theme: "dark",
        callback: (token) => { state.turnstileToken = token; },
        "expired-callback": () => { state.turnstileToken = ""; },
      });
    };
    attempt();
  };

  const submitBooking = async (event) => {
    event.preventDefault();
    if (!elements.form.reportValidity() || !state.selectedSlot) return;
    elements.confirm.disabled = true;
    elements.confirm.querySelector("span").textContent = "Confirming…";
    elements.formError.hidden = true;
    const form = new FormData(elements.form);
    try {
      const payload = await api("/api/v1/bookings", {
        method: "POST",
        body: JSON.stringify({
          event_type: state.eventType,
          start: state.selectedSlot.start,
          timezone: state.timezone,
          name: form.get("name"),
          email: form.get("email"),
          phone: form.get("phone") || "",
          notes: form.get("notes") || "",
          idempotency_key: state.idempotencyKey,
          turnstile_token: state.turnstileToken,
        }),
      });
      showConfirmation(payload);
    } catch (error) {
      elements.formError.textContent = error.message;
      elements.formError.hidden = false;
      if (error.status === 409) await loadAvailability();
      if (window.turnstile && state.turnstileWidget !== null) {
        window.turnstile.reset(state.turnstileWidget);
        state.turnstileToken = "";
      }
    } finally {
      elements.confirm.disabled = false;
      elements.confirm.querySelector("span").textContent = "Confirm appointment";
    }
  };

  const showConfirmation = (booking) => {
    elements.detailsPanel.hidden = true;
    elements.timePanel.hidden = true;
    elements.confirmationPanel.hidden = false;
    const formatted = new Intl.DateTimeFormat("en-US", {
      weekday: "long",
      month: "long",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      timeZoneName: "short",
    }).format(new Date(booking.start));
    elements.confirmationCopy.textContent = `${formatted}. A calendar invitation has been sent to your email.`;
    if (booking.calendar_event_url) {
      elements.calendarLink.href = booking.calendar_event_url;
      elements.calendarLink.hidden = false;
    }
  };

  const escapeHtml = (value) => {
    const element = document.createElement("span");
    element.textContent = String(value);
    return element.innerHTML;
  };

  const initialize = async () => {
    elements.timezoneBadge.textContent = state.timezone.replaceAll("_", " ");
    try {
      state.config = await api("/api/v1/config");
      elements.ownerName.textContent = state.config.owner_name;
      renderEventTypes();
      await loadAvailability();
    } catch (error) {
      elements.eventTypes.innerHTML = "";
      elements.slotStatus.textContent = error.message;
      elements.monthLabel.textContent = "Scheduling unavailable";
    }
  };

  elements.refresh.addEventListener("click", loadAvailability);
  elements.back.addEventListener("click", showTimePanel);
  elements.form.addEventListener("submit", submitBooking);
  initialize();
})();
