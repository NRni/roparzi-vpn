(function () {
  "use strict";

  var catalog = { nodes: [], routes: [], account: [], group: "" };
  var status = { connected: false, core: "starting", coreError: "", endpoint: "127.0.0.1:7890", selected: "", warning: "" };
  var selected = "";
  var delays = {};
  var busy = false;

  var banner = document.getElementById("banner");
  var list = document.getElementById("node-list");
  var search = document.getElementById("search");
  var region = document.getElementById("region");
  var protocol = document.getElementById("protocol");
  var sort = document.getElementById("sort");
  var power = document.getElementById("power");
  var browserBtn = document.getElementById("browser");

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function showError(message) {
    if (!message) {
      banner.hidden = true;
      banner.textContent = "";
      return;
    }
    banner.hidden = false;
    banner.textContent = message;
  }

  function delayText(name) {
    if (!Object.prototype.hasOwnProperty.call(delays, name)) return "—";
    var ms = delays[name];
    if (ms == null || ms < 0) return "timeout";
    return ms + " ms";
  }

  function itemByName(name) {
    var pools = catalog.routes.concat(catalog.nodes);
    for (var i = 0; i < pools.length; i++) {
      if (pools[i].name === name) return pools[i];
    }
    return null;
  }

  function visibleNodes() {
    var q = search.value.trim().toLowerCase();
    var reg = region.value;
    var proto = protocol.value;
    var items = catalog.nodes.filter(function (node) {
      if (reg !== "all" && node.region !== reg) return false;
      if (proto !== "all" && node.type !== proto) return false;
      if (!q) return true;
      return (node.name + " " + node.region).toLowerCase().indexOf(q) !== -1;
    });
    if (sort.value === "latency") {
      items = items.slice().sort(function (a, b) {
        var da = delays[a.name];
        var db = delays[b.name];
        var aMissing = da == null || da < 0;
        var bMissing = db == null || db < 0;
        if (aMissing && bMissing) return 0;
        if (aMissing) return 1;
        if (bMissing) return -1;
        return da - db;
      });
    }
    return items;
  }

  function fillRegions() {
    var current = region.value || "all";
    var counts = {};
    catalog.nodes.forEach(function (node) {
      if (!node.region) return;
      counts[node.region] = (counts[node.region] || 0) + 1;
    });
    var names = Object.keys(counts).sort(function (a, b) {
      return counts[b] - counts[a] || a.localeCompare(b);
    });
    region.innerHTML = '<option value="all">All regions (' + catalog.nodes.length + ")</option>" +
      names.map(function (name) {
        return '<option value="' + esc(name) + '">' + esc(name) + " (" + counts[name] + ")</option>";
      }).join("");
    region.value = counts[current] || current === "all" ? current : "all";
  }

  function renderStatus() {
    var on = Boolean(status.connected);
    var item = itemByName(selected);
    document.getElementById("lamp").setAttribute("data-state", status.core === "error" ? "bad" : on ? "on" : status.core === "running" ? "wait" : "off");
    document.getElementById("power-kicker").textContent = on ? "Connected" : status.core === "running" ? "Ready" : "Off";
    document.getElementById("power-title").textContent = on ? "Connected" : "Not connected";
    document.getElementById("selected-name").textContent = item ? item.name : "Pick a node";
    document.getElementById("selected-meta").textContent = item
      ? [item.region, item.type, delayText(item.name)].filter(Boolean).join(" · ")
      : "";
    document.getElementById("power-label").textContent = busy ? "Working…" : on ? "Disconnect" : "Connect";
    power.setAttribute("aria-pressed", on ? "true" : "false");
    power.disabled = busy || !selected || status.core === "starting";
    browserBtn.disabled = busy || !selected || status.browserReady === false;
    document.getElementById("endpoint").textContent = status.endpoint || "127.0.0.1:7890";
    var core = document.getElementById("core-line");
    core.textContent = status.core === "running" ? "Running" : status.core === "error" ? "Not running" : "Starting…";
    var account = document.getElementById("account");
    if (catalog.account && catalog.account.length) {
      account.hidden = false;
      account.innerHTML = catalog.account.map(function (name) {
        return '<p class="pill">' + esc(name) + "</p>";
      }).join("");
    } else {
      account.hidden = true;
      account.innerHTML = "";
    }
    var meta = catalog.nodes.length + " nodes";
    if (catalog.updatedAt) meta += " · updated " + catalog.updatedAt;
    document.getElementById("sub-meta").textContent = meta;
    if (status.warning) showError(status.warning);
  }

  function row(item, live) {
    var latencyClass = delays[item.name] > 0 ? "latency good" : "latency";
    var sub = item.region ? '<span class="node-sub">' + esc(item.region) + "</span>" : "";
    return '<button class="node' + (item.name === selected ? " is-selected" : "") + (live && item.name === selected ? " is-live" : "") +
      '" type="button" role="option" aria-selected="' + (item.name === selected ? "true" : "false") +
      '" data-name="' + esc(item.name) + '"><span><span class="node-name">' + esc(item.name) +
      "</span>" + sub + '</span><span class="tag">' + esc(item.type || "") +
      '</span><span class="' + latencyClass + '" data-delay-for="' + esc(item.name) + '">' + esc(delayText(item.name)) + "</span></button>";
  }

  function renderList() {
    var scroll = list.scrollTop;
    var routes = catalog.routes;
    var nodes = visibleNodes();
    var html = "";
    if (routes.length && region.value === "all" && protocol.value === "all" && !search.value.trim()) {
      html += '<p class="group-label">Routes</p>';
      html += routes.map(function (item) { return row(item, status.connected); }).join("");
    }
    html += '<p class="group-label">Nodes</p>';
    html += nodes.length
      ? nodes.map(function (item) { return row(item, status.connected); }).join("")
      : '<p class="empty">No nodes match this filter.</p>';
    list.innerHTML = html;
    list.scrollTop = scroll;
    var shown = nodes.length;
    document.getElementById("count").textContent = shown + " shown";
    document.getElementById("test-listed").textContent = "Test listed (" + shown + ")";
  }

  function render() {
    renderStatus();
    renderList();
  }

  async function request(path, options) {
    var response = await fetch(path, options);
    var data = {};
    try { data = await response.json(); } catch (error) { data = {}; }
    if (!response.ok) throw new Error(data.error || "Request failed");
    return data;
  }

  function post(path, body) {
    return request(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    });
  }

  async function load() {
    var settings = await request("/api/settings");
    document.getElementById("sub-url").value = settings.subscription || "";
    var nodes = await request("/api/nodes");
    catalog = nodes;
    delays = {};
    (nodes.nodes || []).concat(nodes.routes || []).forEach(function (node) {
      if (node.delay != null) delays[node.name] = node.delay;
    });
    var state = await request("/api/state");
    status = state;
    selected = state.selected || (catalog.routes[0] && catalog.routes[0].name) || (catalog.nodes[0] && catalog.nodes[0].name) || "";
    fillRegions();
    render();
    showError(state.coreError || state.warning || "");
  }

  async function choose(name) {
    selected = name;
    render();
    try {
      var state = await post("/api/select", { name: name });
      status = state;
      selected = state.selected || name;
      render();
      showError(state.coreError || "");
    } catch (error) {
      showError(error.message);
    }
  }

  async function togglePower() {
    if (busy) return;
    busy = true;
    renderStatus();
    try {
      var state = status.connected
        ? await post("/api/disconnect", {})
        : await post("/api/connect", { name: selected });
      status = state;
      selected = state.selected || selected;
      showError(state.coreError || state.proxyError || "");
    } catch (error) {
      showError(error.message);
    } finally {
      busy = false;
      render();
    }
  }

  async function testNames(names) {
    if (!names.length) return;
    document.getElementById("test-listed").disabled = true;
    try {
      for (var i = 0; i < names.length; i += 12) {
        var chunk = names.slice(i, i + 12);
        var result = await post("/api/delay", { names: chunk });
        Object.keys(result.delays || {}).forEach(function (name) {
          delays[name] = result.delays[name];
        });
        renderList();
        renderStatus();
      }
    } catch (error) {
      showError(error.message);
    } finally {
      document.getElementById("test-listed").disabled = false;
      render();
    }
  }

  list.addEventListener("click", function (event) {
    var button = event.target.closest(".node");
    if (!button) return;
    choose(button.getAttribute("data-name"));
  });

  search.addEventListener("input", renderList);
  region.addEventListener("change", renderList);
  protocol.addEventListener("change", renderList);
  sort.addEventListener("change", renderList);
  power.addEventListener("click", togglePower);
  browserBtn.addEventListener("click", async function () {
    if (busy) return;
    busy = true;
    renderStatus();
    try {
      var state = await post("/api/browser", { name: selected });
      status = state;
      selected = state.selected || selected;
      showError("");
    } catch (error) {
      showError(error.message);
    } finally {
      busy = false;
      render();
    }
  });
  document.getElementById("refresh").addEventListener("click", async function () {
    this.disabled = true;
    try {
      await post("/api/refresh", {});
      await load();
    } catch (error) {
      showError(error.message);
    } finally {
      this.disabled = false;
    }
  });
  document.getElementById("test-listed").addEventListener("click", function () {
    testNames(visibleNodes().map(function (node) { return node.name; }));
  });
  document.getElementById("copy").addEventListener("click", function () {
    var text = status.endpoint || "127.0.0.1:7890";
    navigator.clipboard.writeText(text).then(function () {
      document.getElementById("copy").textContent = "Copied";
      setTimeout(function () { document.getElementById("copy").textContent = "Copy"; }, 1200);
    }).catch(function () {
      showError("Could not copy the proxy address.");
    });
  });
  document.getElementById("save-sub").addEventListener("click", async function () {
    this.disabled = true;
    try {
      await post("/api/subscription", { url: document.getElementById("sub-url").value.trim() });
      await load();
    } catch (error) {
      showError(error.message);
    } finally {
      this.disabled = false;
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "/" && document.activeElement !== search && document.activeElement !== document.getElementById("sub-url")) {
      event.preventDefault();
      search.focus();
    }
  });

  load().catch(function (error) { showError(error.message); });

  setInterval(function () {
    request("/api/state").then(function (state) {
      var previousError = status.coreError || "";
      var changed = state.connected !== status.connected || state.core !== status.core || state.selected !== status.selected;
      status = state;
      if (state.selected) selected = state.selected;
      renderStatus();
      if (changed) renderList();
      if (state.coreError) showError(state.coreError);
      else if (!state.warning && banner.textContent === previousError) showError("");
    }).catch(function () {});
  }, 4000);
})();
