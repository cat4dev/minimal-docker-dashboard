(() => {
  const KEY = "cc_auto_refresh";
  let timer = null;
  let logTarget = null;

  const $ = (id) => document.getElementById(id);
  const modal = $("modal");
  const logBody = $("log-body");
  const logName = $("log-name");
  const auto = $("auto-refresh");
  const grid = $("grid");
  const filter = $("filter");
  const count = $("count");
  const empty = $("filter-empty");
  const toast = $("toast");

  const showToast = (msg, isError) => {
    $("toast")?.remove();
    const div = document.createElement("div");
    div.id = "toast";
    div.className = `toast ${isError ? "toast-err" : "toast-ok"}`;
    div.setAttribute("role", "status");
    const span = document.createElement("span");
    span.className = "toast-msg";
    span.textContent = msg;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "toast-close";
    close.setAttribute("aria-label", "Dismiss");
    close.textContent = "✕";
    close.addEventListener("click", () => div.remove());
    div.appendChild(span);
    div.appendChild(close);
    const wrap = document.querySelector(".wrap");
    if (wrap) {
      const header = wrap.querySelector(".header");
      header ? header.after(div) : wrap.prepend(div);
    }
    setTimeout(() => div.remove(), 8000);
  };

  // Auto-refresh (skip while logs open)
  const setAuto = (on) => {
    clearInterval(timer);
    timer = on
      ? setInterval(() => {
          if (modal && !modal.classList.contains("hidden")) return;
          location.reload();
        }, 30000)
      : null;
    try {
      localStorage.setItem(KEY, on ? "1" : "0");
    } catch (_) {}
  };
  if (auto) {
    try {
      auto.checked = localStorage.getItem(KEY) === "1";
    } catch (_) {}
    setAuto(auto.checked);
    auto.addEventListener("change", () => setAuto(auto.checked));
  }
  $("btn-refresh")?.addEventListener("click", () => location.reload());

  // Toast
  $("toast-close")?.addEventListener("click", () => toast?.remove());
  if (toast) setTimeout(() => toast.remove(), 8000);

  // Confirm stop + prevent double submit
  document.querySelectorAll(".act-form").forEach((form) => {
    form.addEventListener("submit", (e) => {
      const act = form.querySelector("[name=action]")?.value;
      if (act === "stop" && !confirm("Stop this container?")) {
        e.preventDefault();
        return;
      }
      const btn = form.querySelector("button");
      if (btn) btn.disabled = true;
    });
  });

  // Client filter (short display name or full Docker name)
  filter?.addEventListener("input", () => {
    if (!grid) return;
    const q = filter.value.trim().toLowerCase();
    let n = 0;
    grid.querySelectorAll(".card").forEach((card) => {
      const hay = (card.dataset.display || "") + " " + (card.dataset.name || "");
      const show = !q || hay.includes(q);
      card.classList.toggle("hidden-filter", !show);
      if (show) n++;
    });
    if (count) count.textContent = n + " container" + (n === 1 ? "" : "s");
    empty?.classList.toggle("hidden", n > 0);
  });

  // Copy name
  document.querySelectorAll(".copy-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const name = btn.dataset.name || "";
      try {
        await navigator.clipboard.writeText(name);
        const prev = btn.textContent;
        btn.textContent = "✓";
        setTimeout(() => {
          btn.textContent = prev;
        }, 1000);
      } catch (_) {
        prompt("Copy:", name);
      }
    });
  });

  // Logs modal
  const closeModal = () => {
    modal?.classList.add("hidden");
    document.body.style.overflow = "";
    logTarget = null;
  };

  const loadLogs = async (name) => {
    if (!modal || !logBody || !logName) return;
    logTarget = name;
    logName.textContent = name;
    logBody.textContent = "Loading…";
    modal.classList.remove("hidden");
    document.body.style.overflow = "hidden";
    try {
      const res = await fetch("/api/logs/" + encodeURIComponent(name));
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || "HTTP " + res.status);
      }
      const data = await res.json();
      logBody.textContent = data.logs || "(no output)";
      logBody.parentElement.scrollTop = logBody.parentElement.scrollHeight;
    } catch (e) {
      logBody.textContent = "Failed: " + e.message;
    }
  };

  document.querySelectorAll(".logs-btn").forEach((b) => {
    b.addEventListener("click", () => loadLogs(b.dataset.name));
  });
  modal?.querySelectorAll("[data-close]").forEach((el) => {
    el.addEventListener("click", closeModal);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && modal && !modal.classList.contains("hidden")) closeModal();
  });
  $("log-reload")?.addEventListener("click", () => logTarget && loadLogs(logTarget));

  // Deploy fallback (button is only rendered when configured)
  document.querySelectorAll(".deploy-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const name = btn.dataset.name || "";
      if (!name) return;
      if (!confirm(`Deploy ${name}? This rebuilds the container and its network.`)) return;
      const prev = btn.textContent;
      btn.disabled = true;
      btn.textContent = "Deploying…";
      try {
        const res = await fetch("/api/deploy/" + encodeURIComponent(name), { method: "POST" });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
          throw new Error(data.detail || `HTTP ${res.status}`);
        }
        showToast(`Deploy requested for ${name}. Redeploy in progress — refresh to see updates.`, false);
      } catch (e) {
        showToast(`Deploy failed: ${e.message}`, true);
        btn.disabled = false;
        btn.textContent = prev;
      }
    });
  });
})();
