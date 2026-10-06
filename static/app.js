/* Dashboard logic: state, API client, rendering, filters, ingest polling. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const PAGE = 50;

  const state = {
    repos: [],
    repoId: null,
    repo: null,
    info: null,
    authors: null,
    kind: "root",
    path: "",
    since: null,
    until: null,
    authorIds: [],
    commitHashes: [],
    childMetric: "churn",
    childRows: [],
    commitPage: 0,
    commitQuery: "",
    poll: null,
    loading: false,
  };

  // ------------------------------------------------------------------ api

  async function api(url, opts) {
    const res = await fetch(url, opts);
    if (!res.ok) {
      let msg = res.statusText;
      try {
        const body = await res.json();
        if (body && body.detail) {
          msg = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
        }
      } catch (e) { /* not json */ }
      throw new Error(msg || "Request failed");
    }
    return res.json();
  }

  const postJSON = (url, body) => api(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  // -------------------------------------------------------------- helpers

  function fmt(n) {
    return n == null ? "–" : Number(n).toLocaleString("en-US");
  }
  function signed(n) {
    if (n == null) return "–";
    return (n > 0 ? "+" : "") + fmt(n);
  }
  function signCls(n) {
    return n > 0 ? "pos" : n < 0 ? "neg" : "";
  }
  function fmtTs(ts) {
    if (!ts) return "–";
    return new Date(ts * 1000).toLocaleString("en-GB", {
      year: "numeric", month: "short", day: "2-digit",
      hour: "2-digit", minute: "2-digit",
    });
  }
  function localInputValue(ts) {
    const d = new Date(ts * 1000);
    const pad = (x) => String(x).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
           `T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  }
  function toast(msg, type) {
    const box = document.createElement("div");
    box.className = "toast" + (type ? " " + type : "");
    box.textContent = msg;
    $("toasts").appendChild(box);
    setTimeout(() => box.remove(), 4200);
  }
  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text != null) node.textContent = text;
    return node;
  }

  function filterParams(extra) {
    const p = new URLSearchParams();
    const add = (k, v) => { if (v !== null && v !== undefined && v !== "") p.set(k, v); };
    add("since", state.since);
    add("until", state.until);
    if (state.commitHashes.length) add("commits", state.commitHashes.join(","));
    if (state.authorIds.length) add("authors", state.authorIds.join(","));
    if (extra) Object.keys(extra).forEach((k) => add(k, extra[k]));
    return p.toString();
  }

  // ------------------------------------------------------- repos / ingest

  async function refreshRepos() {
    state.repos = await api("/api/repos");
    renderRepoSelect();
    if (!state.repos.length) {
      $("emptyState").classList.remove("hidden");
      $("dash").classList.add("hidden");
      hideBanners();
      return;
    }
    $("emptyState").classList.add("hidden");
    const last = Number(localStorage.getItem("rat.repoId"));
    const pick =
      state.repos.find((r) => r.id === last) ||
      state.repos.filter((r) => r.status === "ready").pop() ||
      state.repos[state.repos.length - 1];
    selectRepo(pick.id);
  }

  function renderRepoSelect() {
    const sel = $("repoSelect");
    sel.innerHTML = "";
    for (const r of state.repos) {
      const label = `${r.name}  ·  ${r.status}`;
      sel.appendChild(new Option(label, String(r.id)));
    }
    if (state.repoId != null) sel.value = String(state.repoId);
  }

  function selectRepo(id) {
    if (state.repoId !== id) {
      state.kind = "root";
      state.path = "";
      state.since = null;
      state.until = null;
      state.authorIds = [];
      state.commitHashes = [];
      state.commitPage = 0;
      state.commitQuery = "";
      syncFilterInputs();
    }
    state.repoId = id;
    state.repo = state.repos.find((r) => r.id === id) || null;
    localStorage.setItem("rat.repoId", String(id));
    $("repoSelect").value = String(id);

    if (state.repo && state.repo.status === "ready") {
      hideBanners();
      loadAll().catch((e) => toast(e.message, "error"));
    } else {
      $("dash").classList.add("hidden");
      updateIngestUI(state.repo);
      startPolling();
    }
  }

  function updateIngestUI(repo) {
    if (!repo) return;
    const busy = ["pending", "ingesting", "analyzing"].includes(repo.status);
    if (busy) {
      $("ingestBanner").classList.remove("hidden");
      $("errorBanner").classList.add("hidden");
      $("ingestLabel").textContent = repo.progress_label || "Working…";
      $("ingestBar").style.width = (repo.progress || 0) + "%";
    } else {
      $("ingestBanner").classList.add("hidden");
      if (repo.status === "error") {
        $("errorBanner").classList.remove("hidden");
        $("errorText").textContent = `Ingestion failed: ${repo.error || "unknown error"}`;
      } else {
        $("errorBanner").classList.add("hidden");
      }
    }
  }

  function hideBanners() {
    $("ingestBanner").classList.add("hidden");
    $("errorBanner").classList.add("hidden");
  }

  function startPolling() {
    stopPolling();
    state.poll = setInterval(async () => {
      try {
        state.repos = await api("/api/repos");
        const repo = state.repos.find((r) => r.id === state.repoId);
        if (!repo) { stopPolling(); return; }
        state.repo = repo;
        renderRepoSelect();
        updateIngestUI(repo);
        if (repo.status === "ready") {
          stopPolling();
          await loadAll();
        } else if (repo.status === "error" || repo.status === "cancelled") {
          stopPolling();
        }
      } catch (e) { /* transient network error while polling */ }
    }, 700);
  }

  function stopPolling() {
    if (state.poll) { clearInterval(state.poll); state.poll = null; }
  }

  // -------------------------------------------------------------- loading

  async function loadAll() {
    const id = state.repoId;
    if (id == null) return;
    const [info, metrics, childrenRows, timeline, authors, commits, breakdown] =
      await Promise.all([
        api(`/api/repos/${id}/info`),
        api(`/api/repos/${id}/metrics?${filterParams({ kind: state.kind, path: state.path })}`),
        api(`/api/repos/${id}/children?${filterParams({ kind: state.kind, path: state.path, metric: state.childMetric })}`),
        api(`/api/repos/${id}/timeline?${filterParams()}`),
        api(`/api/repos/${id}/authors`),
        api(`/api/repos/${id}/commits?${filterParams({ limit: PAGE, offset: state.commitPage * PAGE, q: state.commitQuery })}`),
        api(`/api/repos/${id}/author_breakdown?${filterParams({ kind: state.kind, path: state.path })}`),
      ]);
    state.info = info;
    state.authors = authors;
    $("dash").classList.remove("hidden");
    renderBreadcrumb();
    renderKpis(metrics);
    renderChildren(childrenRows);
    Charts.setTimeline(timeline);
    renderOwnership(breakdown);
    renderAuthorFilter();
    renderAuthorsPanel();
    renderCommits(commits);
    renderChips();
  }

  async function reloadMetrics() {
    const id = state.repoId;
    if (id == null || !state.repo || state.repo.status !== "ready") return;
    const [metrics, childrenRows, timeline, commits, breakdown] = await Promise.all([
      api(`/api/repos/${id}/metrics?${filterParams({ kind: state.kind, path: state.path })}`),
      api(`/api/repos/${id}/children?${filterParams({ kind: state.kind, path: state.path, metric: state.childMetric })}`),
      api(`/api/repos/${id}/timeline?${filterParams()}`),
      api(`/api/repos/${id}/commits?${filterParams({ limit: PAGE, offset: state.commitPage * PAGE, q: state.commitQuery })}`),
      api(`/api/repos/${id}/author_breakdown?${filterParams({ kind: state.kind, path: state.path })}`),
    ]);
    renderKpis(metrics);
    renderChildren(childrenRows);
    Charts.setTimeline(timeline);
    renderOwnership(breakdown);
    renderCommits(commits);
    renderChips();
  }

  // ----------------------------------------------------------- rendering

  function renderBreadcrumb() {
    const nav = $("crumbs");
    nav.innerHTML = "";
    const repoName = state.repo ? state.repo.name : "repo";
    const parts = state.path ? state.path.split("/") : [];

    const addSep = () => nav.appendChild(el("span", "sep-a", "/"));
    const addLink = (label, kind, path) => {
      const a = el("a", null, label);
      a.addEventListener("click", () => {
        state.kind = kind;
        state.path = path;
        loadAll().catch((e) => toast(e.message, "error"));
      });
      nav.appendChild(a);
    };
    const addCurrent = (label) => nav.appendChild(el("span", "current", label));

    if (state.kind === "root" || !parts.length) {
      addCurrent(repoName);
      return;
    }
    addLink(repoName, "root", "");
    let acc = "";
    parts.forEach((part, i) => {
      addSep();
      acc = acc ? `${acc}/${part}` : part;
      const last = i === parts.length - 1;
      if (last) addCurrent(part);
      else addLink(part, "dir", acc);
    });
  }

  function renderKpis(m) {
    const box = $("kpis");
    box.innerHTML = "";
    const cards = [
      ["Commits |H|", fmt(m.commit_set_size), "non-merge, in commit set"],
      ["Added", fmt(m.added), "lines added"],
      ["Removed", fmt(m.removed), "lines removed"],
      ["Growth", signed(m.growth), "added − removed", signCls(m.growth)],
      ["Churn", fmt(m.churn), "added + removed"],
      ["Modifications", fmt(m.modifications), "commits with churn > 0"],
      ["Mod. frequency", m.modification_frequency.toFixed(3), "modifications / |H|"],
      ["Churn rate", m.churn_rate.toFixed(2), "churn / |H|"],
    ];
    for (const [label, value, hint, cls] of cards) {
      const card = el("div", "kpi");
      card.appendChild(el("div", "k-label", label));
      const v = el("div", "k-value" + (cls ? " " + cls : ""), value);
      card.appendChild(v);
      card.appendChild(el("div", "k-hint", hint));
      box.appendChild(card);
    }
  }

  function renderChildren(rows) {
    state.childRows = rows || [];
    const tbody = $("childrenTable").querySelector("tbody");
    tbody.innerHTML = "";
    $("childrenTitle").textContent = state.kind === "file"
      ? "File — no child objects" : "Contents (files & directories)";
    document.querySelectorAll("#childrenTable th.sortable").forEach((th) => {
      th.classList.toggle("sorted", th.dataset.metric === state.childMetric);
    });

    if (!state.childRows.length) {
      const tr = el("tr");
      const td = el("td", "muted", state.kind === "file"
        ? "This object is a file; use the breadcrumb to go back up."
        : "No changes match the current filters.");
      td.colSpan = 6;
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }

    for (const row of state.childRows) {
      const tr = el("tr", "clickable");
      const nameTd = el("td", "path-name");
      const icon = el("span", "icon", row.kind === "dir" ? "▸" : "·");
      nameTd.appendChild(icon);
      if (row.kind === "dir") {
        const nm = el("span", "dirname", row.name + "/");
        nameTd.appendChild(nm);
      } else {
        nameTd.appendChild(el("span", null, row.name));
      }
      tr.appendChild(nameTd);
      tr.appendChild(el("td", "num", fmt(row.added)));
      tr.appendChild(el("td", "num", fmt(row.removed)));
      const g = el("td", "num " + signCls(row.growth), signed(row.growth));
      tr.appendChild(g);
      tr.appendChild(el("td", "num", fmt(row.churn)));
      tr.appendChild(el("td", "num", row.modifications == null ? "–" : fmt(row.modifications)));
      tr.addEventListener("click", () => {
        state.kind = row.kind;
        state.path = row.path;
        loadAll().catch((e) => toast(e.message, "error"));
      });
      tbody.appendChild(tr);
    }
  }

  function renderOwnership(rows) {
    Charts.setOwnership(rows || [], (authorId) => {
      const idx = state.authorIds.indexOf(authorId);
      if (idx >= 0) state.authorIds.splice(idx, 1);
      else state.authorIds.push(authorId);
      renderAuthorFilter();
      reloadMetrics().catch((e) => toast(e.message, "error"));
    });
  }

  function renderAuthorFilter() {
    const list = $("authorFilterList");
    list.innerHTML = "";
    if (!state.authors) return;
    for (const a of state.authors.authors) {
      const label = el("label");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = state.authorIds.includes(a.id);
      cb.addEventListener("change", () => {
        const i = state.authorIds.indexOf(a.id);
        if (cb.checked && i < 0) state.authorIds.push(a.id);
        if (!cb.checked && i >= 0) state.authorIds.splice(i, 1);
        updateAuthorSummary();
        reloadMetrics().catch((e) => toast(e.message, "error"));
      });
      label.appendChild(cb);
      label.appendChild(el("span", null, `${a.name} <${a.email}>`));
      label.appendChild(el("span", "muted", `(${fmt(a.commit_count)} commits)`));
      list.appendChild(label);
    }
    updateAuthorSummary();
  }

  function updateAuthorSummary() {
    const n = state.authorIds.length;
    $("authorFilterSummary").textContent = n ? `${n} author${n > 1 ? "s" : ""} selected` : "All authors";
  }

  function renderAuthorsPanel() {
    const box = $("authorsList");
    const target = $("mergeTarget");
    box.innerHTML = "";
    target.innerHTML = "";
    if (!state.authors) return;
    for (const a of state.authors.authors) {
      target.appendChild(new Option(`${a.name} <${a.email}>`, String(a.id)));
      const group = el("div", "author-group");
      const head = el("div", "author-head");
      const all = document.createElement("input");
      all.type = "checkbox";
      all.title = "Select all identities of this author";
      all.addEventListener("change", () => {
        group.querySelectorAll("input[data-ident]").forEach((cb) => { cb.checked = all.checked; });
      });
      head.appendChild(all);
      head.appendChild(el("span", "name", a.name));
      head.appendChild(el("span", "mail", `<${a.email}>`));
      head.appendChild(el("span", "muted", `${fmt(a.commit_count)} commits`));
      if (a.identities.some((i) => i.synthetic) || a.identities.length > 1) {
        head.appendChild(el("span", "tag canon", "merged group"));
      }
      group.appendChild(head);

      if (a.identities.length > 1 || a.identities.some((i) => i.id !== a.id)) {
        const list = el("div", "ident-list");
        for (const ident of a.identities) {
          const row = el("div", "ident");
          const cb = document.createElement("input");
          cb.type = "checkbox";
          cb.dataset.ident = "1";
          cb.value = String(ident.id);
          row.appendChild(cb);
          row.appendChild(el("span", null, `${ident.name} <${ident.email}>`));
          row.appendChild(el("span", "muted", `${fmt(ident.commit_count)} commits`));
          if (ident.synthetic) row.appendChild(el("span", "tag canon", "canonical"));
          if (ident.id !== a.id) {
            const un = el("button", "btn small ghost", "Unmerge");
            un.title = "Detach this identity from the group";
            un.addEventListener("click", async (ev) => {
              ev.stopPropagation();
              try {
                await postJSON(`/api/repos/${state.repoId}/authors/unmerge`, { author_id: ident.id });
                toast("Identity detached", "success");
                await loadAll();
              } catch (e) { toast(e.message, "error"); }
            });
            row.appendChild(un);
          }
          list.appendChild(row);
        }
        group.appendChild(list);
      }
      box.appendChild(group);
    }
  }

  function renderCommits(rows) {
    const tbody = $("commitsTable").querySelector("tbody");
    tbody.innerHTML = "";
    if (!rows || !rows.length) {
      const tr = el("tr");
      const td = el("td", "muted", "No commits match the current filters.");
      td.colSpan = 6;
      tr.appendChild(td);
      tbody.appendChild(tr);
    } else {
      for (const c of rows) {
        const tr = el("tr");
        tr.appendChild(el("td", "muted", fmtTs(c.committer_ts)));
        const hashTd = el("td");
        const code = el("span", "hash", c.hash.slice(0, 8));
        code.title = c.hash;
        hashTd.appendChild(code);
        tr.appendChild(hashTd);
        tr.appendChild(el("td", null, c.author_name));
        const subj = el("td");
        const span = el("span", "subject", c.subject);
        span.title = c.subject;
        subj.appendChild(span);
        tr.appendChild(subj);
        const plus = el("span", "pos", `+${fmt(c.added)}`);
        const minus = el("span", "neg", ` −${fmt(c.removed)}`);
        const stat = el("td", "num");
        stat.appendChild(plus);
        stat.appendChild(minus);
        tr.appendChild(stat);
        const act = el("td");
        const btn = el("button", "btn small ghost", "+");
        btn.title = "Assign this commit as the commit set";
        btn.addEventListener("click", () => {
          state.commitHashes = [c.hash];
          $("commitInput").value = c.hash;
          state.commitPage = 0;
          reloadMetrics().catch((e) => toast(e.message, "error"));
        });
        act.appendChild(btn);
        tr.appendChild(act);
        tbody.appendChild(tr);
      }
    }
    $("pageInfo").textContent = `page ${state.commitPage + 1}`;
    $("btnPrevPage").disabled = state.commitPage === 0;
    $("btnNextPage").disabled = !rows || rows.length < PAGE;
  }

  function renderChips() {
    const chips = $("chips");
    chips.innerHTML = "";
    const add = (label, clear) => {
      const chip = el("span", "chip");
      chip.appendChild(el("span", null, label));
      const x = el("button", null, "×");
      x.addEventListener("click", () => {
        clear();
        renderChips();
        reloadMetrics().catch((e) => toast(e.message, "error"));
      });
      chip.appendChild(x);
      chips.appendChild(chip);
    };
    if (state.since != null) add(`since ${fmtTs(state.since)}`, () => { state.since = null; syncFilterInputs(); });
    if (state.until != null) add(`until ${fmtTs(state.until)}`, () => { state.until = null; syncFilterInputs(); });
    if (state.authorIds.length) add(`${state.authorIds.length} author(s)`, () => { state.authorIds = []; renderAuthorFilter(); });
    if (state.commitHashes.length) {
      add(`commit set (${state.commitHashes.length})`, () => {
        state.commitHashes = [];
        $("commitInput").value = "";
      });
    }
    if (state.commitQuery) add(`search “${state.commitQuery}”`, () => {
      state.commitQuery = "";
      $("commitSearch").value = "";
    });
  }

  function syncFilterInputs() {
    $("since").value = state.since ? localInputValue(state.since) : "";
    $("until").value = state.until ? localInputValue(state.until) : "";
    $("preset").value = "";
  }

  // ----------------------------------------------------------- interaction

  function applyTimeInputs() {
    const s = $("since").value ? Math.floor(new Date($("since").value).getTime() / 1000) : null;
    const u = $("until").value ? Math.floor(new Date($("until").value).getTime() / 1000) : null;
    if (s != null && u != null && s >= u) {
      toast("The since time must be before the until time", "error");
      return;
    }
    state.since = s;
    state.until = u;
    state.commitPage = 0;
    reloadMetrics().catch((e) => toast(e.message, "error"));
  }

  function wire() {
    $("repoSelect").addEventListener("change", (e) => selectRepo(Number(e.target.value)));
    $("btnDelete").addEventListener("click", async () => {
      if (state.repoId == null) return;
      if (!confirm("Delete this repository and all its analysis data?")) return;
      try {
        await api(`/api/repos/${state.repoId}`, { method: "DELETE" });
        state.repoId = null;
        toast("Repository deleted", "success");
        await refreshRepos();
      } catch (e) { toast(e.message, "error"); }
    });
    $("btnCancel").addEventListener("click", async () => {
      try {
        await postJSON(`/api/repos/${state.repoId}/cancel`, {});
        toast("Cancelling…");
      } catch (e) { toast(e.message, "error"); }
    });

    const openModal = () => $("modal").classList.remove("hidden");
    $("btnAdd").addEventListener("click", openModal);
    $("btnEmptyUrl").addEventListener("click", openModal);
    $("btnEmptyZip").addEventListener("click", () => { openModal(); $("fileInput").click(); });
    $("btnCloseModal").addEventListener("click", () => $("modal").classList.add("hidden"));

    $("btnClone").addEventListener("click", async () => {
      const url = $("urlInput").value.trim();
      if (!url) { toast("Enter a repository URL", "error"); return; }
      try {
        const { id } = await postJSON("/api/repos/clone", { url });
        $("modal").classList.add("hidden");
        await refreshRepos();
        selectRepo(id);
      } catch (e) { toast(e.message, "error"); }
    });

    $("btnUpload").addEventListener("click", async () => {
      const file = $("fileInput").files[0];
      if (!file) { toast("Choose a .zip file first", "error"); return; }
      const fd = new FormData();
      fd.append("file", file);
      try {
        const { id } = await api("/api/repos/zip", { method: "POST", body: fd });
        $("modal").classList.add("hidden");
        await refreshRepos();
        selectRepo(id);
      } catch (e) { toast(e.message, "error"); }
    });

    $("btnApplyTime").addEventListener("click", applyTimeInputs);
    $("preset").addEventListener("change", (e) => {
      const days = e.target.value;
      if (!days) {
        state.since = null;
        state.until = null;
        syncFilterInputs();
      } else {
        const now = Math.floor(Date.now() / 1000);
        state.since = now - Number(days) * 86400;
        state.until = null;
        $("since").value = localInputValue(state.since);
        $("until").value = "";
      }
      state.commitPage = 0;
      reloadMetrics().catch((err) => toast(err.message, "error"));
    });

    $("btnApplyCommits").addEventListener("click", () => {
      const text = $("commitInput").value.trim();
      state.commitHashes = text ? text.split(/[\s,]+/).filter(Boolean) : [];
      state.commitPage = 0;
      reloadMetrics().catch((e) => toast(e.message, "error"));
    });

    $("btnClearFilters").addEventListener("click", () => {
      state.since = null;
      state.until = null;
      state.authorIds = [];
      state.commitHashes = [];
      state.commitQuery = "";
      state.commitPage = 0;
      syncFilterInputs();
      $("commitInput").value = "";
      $("commitSearch").value = "";
      renderAuthorFilter();
      reloadMetrics().catch((e) => toast(e.message, "error"));
    });

    $("btnZoomRange").addEventListener("click", () => {
      const range = Charts.visibleRange();
      if (!range) { toast("Zoom into the timeline first, then apply the visible range"); return; }
      state.since = range.since;
      state.until = range.until;
      $("since").value = localInputValue(state.since);
      $("until").value = localInputValue(state.until);
      state.commitPage = 0;
      reloadMetrics().catch((e) => toast(e.message, "error"));
    });

    $("btnSearchCommits").addEventListener("click", runCommitSearch);
    $("commitSearch").addEventListener("keydown", (e) => {
      if (e.key === "Enter") runCommitSearch();
    });
    function runCommitSearch() {
      state.commitQuery = $("commitSearch").value.trim();
      state.commitPage = 0;
      reloadMetrics().catch((e) => toast(e.message, "error"));
    }

    $("btnPrevPage").addEventListener("click", () => {
      if (state.commitPage > 0) {
        state.commitPage -= 1;
        reloadMetrics().catch((e) => toast(e.message, "error"));
      }
    });
    $("btnNextPage").addEventListener("click", () => {
      state.commitPage += 1;
      reloadMetrics().catch((e) => toast(e.message, "error"));
    });

    document.querySelectorAll("#childrenTable th.sortable").forEach((th) => {
      th.addEventListener("click", () => {
        const metric = th.dataset.metric;
        if (metric === "name") {
          state.childRows.sort((a, b) => a.name.localeCompare(b.name));
          renderChildren(state.childRows);
          return;
        }
        state.childMetric = metric;
        api(`/api/repos/${state.repoId}/children?${filterParams({ kind: state.kind, path: state.path, metric })}`)
          .then(renderChildren)
          .catch((e) => toast(e.message, "error"));
      });
    });

    $("btnApplyMerge").addEventListener("click", async () => {
      const targetId = Number($("mergeTarget").value);
      const ids = [];
      document.querySelectorAll("#authorsList input[data-ident]").forEach((cb) => {
        if (cb.checked) ids.push(Number(cb.value));
      });
      const sources = ids.filter((i) => i !== targetId);
      if (!sources.length) {
        toast("Select at least one identity to merge (different from the target)", "error");
        return;
      }
      try {
        await postJSON(`/api/repos/${state.repoId}/authors/merge`,
                       { target_id: targetId, source_ids: sources });
        toast("Authors merged", "success");
        await loadAll();
      } catch (e) { toast(e.message, "error"); }
    });
  }

  wire();
  refreshRepos().catch((e) => toast(e.message, "error"));
})();
