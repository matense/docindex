/* Tables viewer (T2): slides a 50-row window through rows?offset&limit.
 * T3 adds the SQL filter bar (POST query, SELECT/WITH only — the filtered
 * view replaces the grid and the pager resets to page 1) and versioned
 * inline cell editing on .ptable files (POST cell; csv/tsv/xlsx stay
 * read-only until "Save as .ptable").
 * T4 adds the pin-as-dashboard-card handoff (GET pin-targets picker, POST
 * pin — no new file, only a card) and the build-automation handoff (POST
 * build-automation — a prefilled workflow, then navigate to its editor).
 * Organize previews the per-drive "DocIndex Tables" plan in a modal
 * (GET organize/plan, POST organize/apply) with a revert affordance.
 * Honesty pass: History entry point (server-rendered versions panel reusing
 * the drive per-version routes), keepalive unload flush for the focused
 * cell, a single FETCH_TIMEOUT_MS for every fetch with busy reset + error
 * toast in all catch paths, filtered-view notice + kept stats row, pager
 * loading state, no-match Reset nudge, failed saves preserving drafts. */
(function () {
  "use strict";

  // Single module constant for every tables fetch: hung requests abort
  // after this long instead of wedging the grid on "Loading…" forever.
  var FETCH_TIMEOUT_MS = 30000;

  // fetch() with a timeout. Manual AbortController (not AbortSignal.timeout)
  // so older browsers get the same behavior; timeouts surface as AbortError.
  function timedFetch(url, options) {
    options = options || {};
    var controller = new AbortController();
    var timer = setTimeout(function () { controller.abort(); },
                           FETCH_TIMEOUT_MS);
    options.signal = controller.signal;
    return fetch(url, options).then(function (resp) {
      clearTimeout(timer);
      return resp;
    }, function (err) {
      clearTimeout(timer);
      throw err;
    });
  }

  // Timeout-aware message for a fetch failure: offline and aborted
  // requests say so, everything else falls back to the caller's copy.
  function failMessage(err, fallback) {
    if (err && err.name === "AbortError") {
      return "Request timed out — try again.";
    }
    if (typeof navigator !== "undefined" && navigator &&
        navigator.onLine === false) {
      return "You appear to be offline — try again.";
    }
    return fallback;
  }

  function init() {
    var mount = document.getElementById("tables-grid");
    if (!mount) return;
    var fileId = mount.getAttribute("data-file-id");
    var total = parseInt(mount.getAttribute("data-total") || "0", 10);
    var pageSize = parseInt(mount.getAttribute("data-page-size") || "50", 10);
    var editable = mount.getAttribute("data-editable") === "1";
    var tbody = document.getElementById("tables-grid-body");
    var theadRow = document.querySelector("#tables-table thead tr");
    var status = document.getElementById("tables-status");
    var loading = document.getElementById("tables-loading");
    var prevBtn = document.getElementById("tables-prev");
    var nextBtn = document.getElementById("tables-next");
    var scroller = document.getElementById("tables-scroll");
    var sqlInput = document.getElementById("tables-sql");
    var runBtn = document.getElementById("tables-run");
    var resetBtn = document.getElementById("tables-reset");
    var saveAsBtn = document.getElementById("tables-saveas");
    var pinBtn = document.getElementById("tables-pin");
    var buildBtn = document.getElementById("tables-buildauto");
    var pinPanel = document.getElementById("tables-pin-panel");
    var pinTarget = document.getElementById("tables-pin-target");
    var pinTitle = document.getElementById("tables-pin-title");
    var pinConfirm = document.getElementById("tables-pin-confirm");
    var pinCancel = document.getElementById("tables-pin-cancel");
    var errorBox = document.getElementById("tables-error");
    if (!tbody || !fileId) return;

    var start = 0;
    var busy = false;
    var filtered = false;
    var colNames = readColNames();
    var headSnapshot = theadRow ? theadRow.innerHTML : "";

    function baseUrl() {
      return "/m/tables/" + encodeURIComponent(fileId);
    }

    // Pager/Run clicks during a load used to vanish silently
    // (loadWindow early-returns while busy). Disabled buttons plus the
    // visible spinner make the loading state explicit instead.
    // showLoading always re-derives the pager state so turning loading
    // off can never wedge Prev/Next enabled at an endpoint (or disabled
    // mid-list).
    function showLoading(on) {
      busy = on;
      if (loading) loading.classList.toggle("hidden", !on);
      if (runBtn) runBtn.disabled = on;
      paintPagerDisabled();
    }

    function paintPagerDisabled() {
      if (prevBtn) prevBtn.disabled = busy || start <= 0;
      if (nextBtn) {
        nextBtn.disabled = busy || (total !== 0 &&
                                    start + pageSize >= total);
      }
    }

    function showError(msg) {
      if (!errorBox) return;
      if (!msg) {
        errorBox.textContent = "";
        errorBox.classList.add("hidden");
        return;
      }
      errorBox.textContent = msg;
      errorBox.classList.remove("hidden");
    }

    function readColNames() {
      var names = [];
      if (!theadRow) return names;
      var divs = theadRow.querySelectorAll("th > div");
      for (var i = 0; i < divs.length; i++) {
        names.push(divs[i].textContent);
      }
      return names;
    }

    function renderHead(columns) {
      if (!theadRow) return;
      theadRow.textContent = "";
      colNames = [];
      for (var i = 0; i < columns.length; i++) {
        colNames.push(columns[i].name);
        var th = document.createElement("th");
        var name = document.createElement("div");
        name.className = "font-semibold";
        name.textContent = columns[i].name;
        th.appendChild(name);
        if (columns[i].type) {
          var badge = document.createElement("span");
          badge.className = "col-type col-type-" + columns[i].type;
          badge.textContent = columns[i].type;
          th.appendChild(badge);
        }
        // The stats row is server-rendered on first paint and recomputed
        // by the rows/query APIs for every window after that, so the
        // strip survives paging and filtering instead of vanishing.
        // Sparkline SVG is server-generated markup, injected as HTML;
        // every stat value goes through textContent.
        if (columns[i].stats) {
          var stats = document.createElement("div");
          stats.className = "col-stats text-xs font-normal opacity-80";
          var st = columns[i].stats;
          var pairs = [["stat-sum", "sum", st.sum],
                       ["stat-avg", "avg", st.avg],
                       ["stat-min", "min", st.min],
                       ["stat-max", "max", st.max],
                       ["stat-null", "null", st.nulls]];
          for (var s = 0; s < pairs.length; s++) {
            if (pairs[s][2] === undefined || pairs[s][2] === null) continue;
            var span = document.createElement("span");
            span.className = pairs[s][0];
            span.textContent = pairs[s][1] + " " + pairs[s][2];
            stats.appendChild(span);
            stats.appendChild(document.createTextNode(" "));
          }
          if (st.sparkline) {
            var shot = document.createElement("span");
            shot.innerHTML = st.sparkline;
            stats.appendChild(shot);
          }
          th.appendChild(stats);
        }
        theadRow.appendChild(th);
      }
    }

    function render(rows) {
      tbody.textContent = "";
      var canEdit = editable && !filtered;
      for (var i = 0; i < rows.length; i++) {
        var tr = document.createElement("tr");
        for (var j = 0; j < rows[i].length; j++) {
          var td = document.createElement("td");
          td.textContent = rows[i][j];
          if (canEdit && j < colNames.length) {
            td.contentEditable = "true";
            td.setAttribute("data-r", String(start + i));
            td.setAttribute("data-c", colNames[j]);
            td.title = "Click to edit";
          }
          tr.appendChild(td);
        }
        tbody.appendChild(tr);
      }
    }

    function paintStatus() {
      paintAuxBlocks();
      if (!status) return;
      if (total === 0) {
        status.textContent = filtered ? "No matching rows" : "No rows";
        return;
      }
      var end = Math.min(start + pageSize, total);
      var label = filtered ? "Matching rows " : "Rows ";
      status.textContent = label + (start + 1) + "-" + end + " of " + total;
      paintPagerDisabled();
    }

    // Server-rendered honesty blocks: the filtered-view notice (editing is
    // disabled while filtered) and the no-match block pointing at Reset.
    // Hidden inputs — paintStatus drives them so every path stays honest.
    function paintAuxBlocks() {
      var notice = document.getElementById("tables-filtered-notice");
      if (notice) notice.classList.toggle("hidden", !filtered);
      var nomatch = document.getElementById("tables-nomatch");
      if (nomatch) nomatch.classList.toggle("hidden",
                                            !(filtered && total === 0));
    }

    function applyData(data, next) {
      start = next;
      if (data.columns) renderHead(data.columns);
      total = data.total;
      render(data.rows || []);
      paintStatus();
    }

    function loadWindow(next) {
      if (busy) return;
      next = Math.max(0, Math.min(next, Math.max(0, total - 1)));
      showError(null);
      showLoading(true);
      if (filtered) {
        runQuery(next);
        return;
      }
      timedFetch(baseUrl() + "/rows?offset=" + next + "&limit=" + pageSize)
        .then(function (resp) {
          if (!resp.ok) throw new Error("rows fetch failed: " + resp.status);
          return resp.json();
        })
        .then(function (data) {
          applyData(data, next);
        })
        .catch(function (err) {
          showError(failMessage(err, "Could not load rows."));
          if (status) status.textContent = "Could not load rows.";
        })
        .finally(function () {
          showLoading(false);
        });
    }

    function runQuery(next) {
      var sql = sqlInput ? sqlInput.value : "";
      showLoading(true);
      timedFetch(baseUrl() + "/query", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sql: sql, offset: next, limit: pageSize })
      })
        .then(function (resp) {
          return resp.json().then(function (data) {
            return { ok: resp.ok, data: data };
          });
        })
        .then(function (out) {
          if (!out.ok || !out.data.ok) {
            showError((out.data && out.data.error) || "Query failed.");
            return;
          }
          showError(null);
          filtered = true;
          applyData(out.data, next);
        })
        .catch(function (err) {
          showError(failMessage(err, "Query failed unexpectedly."));
        })
        .finally(function () {
          showLoading(false);
        });
    }

    function resetFilter() {
      if (sqlInput) sqlInput.value = "";
      filtered = false;
      showError(null);
      if (theadRow) {
        theadRow.innerHTML = headSnapshot;
        colNames = readColNames();
      }
      paintStatus();
      loadWindow(0);
    }

    // A failed save must NOT re-render the grid: loadWindow(start) would
    // rebuild tbody from the server and wipe every other in-progress
    // draft. Instead the failed cell keeps its text, gets an error
    // outline, and the toast explains — the user can retry by editing
    // and blurring again.
    function flagCellError(td, msg) {
      td.style.outline = "2px solid #f87171";
      td.title = msg;
      showError(msg + " Your edit is kept — fix the problem and blur the cell to retry.");
    }

    function clearCellError(td) {
      td.style.outline = "";
      td.title = "Click to edit";
    }

    function commitEdit(td) {
      var row = parseInt(td.getAttribute("data-r") || "-1", 10);
      var col = td.getAttribute("data-c") || "";
      var value = td.textContent;
      if (row < 0 || !col) return;
      showError(null);
      timedFetch(baseUrl() + "/cell", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ row: row, col: col, value: value })
      })
        .then(function (resp) {
          return resp.json().then(function (data) {
            return { ok: resp.ok, data: data };
          });
        })
        .then(function (out) {
          if (!out.ok || !out.data.ok) {
            flagCellError(td, (out.data && out.data.error) ||
                              "Could not save.");
            return;
          }
          clearCellError(td);
          td.textContent = out.data.value;
          if (status) status.textContent = "Saved.";
          paintStatusSoon();
        })
        .catch(function (err) {
          flagCellError(td, failMessage(err, "Could not save the cell."));
        });
    }

    var statusTimer = null;
    function paintStatusSoon() {
      if (statusTimer) clearTimeout(statusTimer);
      statusTimer = setTimeout(paintStatus, 1500);
    }

    tbody.addEventListener("focusout", function (ev) {
      var td = ev.target;
      if (td && td.getAttribute("contenteditable") === "true") {
        commitEdit(td);
      }
    });
    tbody.addEventListener("keydown", function (ev) {
      var td = ev.target;
      if (td && td.getAttribute("contenteditable") === "true"
          && ev.key === "Enter" && !ev.shiftKey) {
        ev.preventDefault();
        td.blur();
      }
    });

    if (runBtn) runBtn.addEventListener("click", function () {
      if (busy) return;
      showLoading(true);
      runQuery(0);
    });
    if (sqlInput) sqlInput.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" && !busy) {
        ev.preventDefault();
        showLoading(true);
        runQuery(0);
      }
    });
    if (resetBtn) resetBtn.addEventListener("click", resetFilter);
    if (saveAsBtn) saveAsBtn.addEventListener("click", function () {
      showError(null);
      showLoading(true);
      timedFetch(baseUrl() + "/save-as", { method: "POST" })
        .then(function (resp) {
          return resp.json().then(function (data) {
            return { ok: resp.ok, data: data };
          });
        })
        .then(function (out) {
          showLoading(false);
          if (!out.ok || !out.data.ok || !out.data.file_id) {
            showError((out.data && out.data.error) || "Save failed.");
            return;
          }
          window.location.href = "/m/tables/" + out.data.file_id;
        })
        .catch(function (err) {
          showLoading(false);
          showError(failMessage(err, "Save failed unexpectedly."));
        });
    });

    function openPinPanel() {
      if (!pinPanel || !pinTarget) return;
      showError(null);
      timedFetch(baseUrl() + "/pin-targets")
        .then(function (resp) {
          return resp.json().then(function (data) {
            return { ok: resp.ok, data: data };
          });
        })
        .then(function (out) {
          if (!out.ok || !out.data.ok) {
            showError((out.data && out.data.error) ||
                      "Could not load dashboards.");
            return;
          }
          var dashboards = out.data.dashboards || [];
          if (!dashboards.length) {
            showError("No dashboards yet — create one from the Dashboards module first.");
            return;
          }
          pinTarget.textContent = "";
          for (var i = 0; i < dashboards.length; i++) {
            var opt = document.createElement("option");
            opt.value = String(dashboards[i].id);
            opt.textContent = dashboards[i].name;
            pinTarget.appendChild(opt);
          }
          if (out.data.default_id) {
            pinTarget.value = String(out.data.default_id);
          }
          if (pinTitle) pinTitle.value = out.data.suggested_title || "";
          pinPanel.classList.remove("hidden");
        })
        .catch(function (err) {
          showError(failMessage(err, "Could not load dashboards."));
        });
    }

    if (pinBtn) pinBtn.addEventListener("click", openPinPanel);
    if (pinCancel) pinCancel.addEventListener("click", function () {
      if (pinPanel) pinPanel.classList.add("hidden");
    });
    if (pinConfirm) pinConfirm.addEventListener("click", function () {
      if (busy) return;
      showError(null);
      showLoading(true);
      timedFetch(baseUrl() + "/pin", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          dashboard_id: pinTarget ? pinTarget.value : null,
          title: pinTitle ? pinTitle.value : ""
        })
      })
        .then(function (resp) {
          return resp.json().then(function (data) {
            return { ok: resp.ok, data: data };
          });
        })
        .then(function (out) {
          showLoading(false);
          if (!out.ok || !out.data.ok) {
            showError((out.data && out.data.error) || "Pin failed.");
            return;
          }
          if (pinPanel) pinPanel.classList.add("hidden");
          if (status && out.data.url) {
            status.textContent = "";
            var link = document.createElement("a");
            link.href = out.data.url;
            link.className = "link link-primary";
            link.textContent = "Pinned to dashboard — open it";
            status.appendChild(link);
          }
        })
        .catch(function (err) {
          showLoading(false);
          showError(failMessage(err, "Pin failed unexpectedly."));
        });
    });
    if (buildBtn) buildBtn.addEventListener("click", function () {
      if (busy) return;
      showError(null);
      showLoading(true);
      timedFetch(baseUrl() + "/build-automation", { method: "POST" })
        .then(function (resp) {
          return resp.json().then(function (data) {
            return { ok: resp.ok, data: data };
          });
        })
        .then(function (out) {
          showLoading(false);
          if (!out.ok || !out.data.ok || !out.data.url) {
            showError((out.data && out.data.error) ||
                      "Could not build the automation.");
            return;
          }
          window.location.href = out.data.url;
        })
        .catch(function (err) {
          showLoading(false);
          showError(failMessage(err, "Could not build the automation."));
        });
    });

    var organizeBtn = document.getElementById("tables-organize-btn");
    if (organizeBtn) organizeBtn.addEventListener("click", function () {
      var box = document.getElementById("tables-organize-plan");
      var confirmBtn = document.getElementById("tables-organize-confirm");
      var modal = document.getElementById("tables-organize-modal");
      var planUrl = organizeBtn.getAttribute("data-plan-url");
      if (!box || !confirmBtn || !modal || !planUrl) return;
      confirmBtn.disabled = true;
      box.innerHTML = '<p class="text-xs opacity-50 py-4 text-center">'
        + '<i class="fas fa-circle-notch fa-spin"></i> Computing plan…</p>';
      modal.showModal();
      timedFetch(planUrl)
        .then(function (r) { return r.json(); })
        .then(function (data) {
          var moves = data.moves || [];
          if (!moves.length) {
            box.innerHTML = '<p class="text-xs opacity-60 py-4 text-center">'
              + 'Nothing to organize — every table is already in its '
              + "'DocIndex Tables' folder."
              + (data.skipped_synced ? ' (' + data.skipped_synced + ' on read-only drives stay put.)' : '')
              + '</p>';
            return;
          }
          box.innerHTML = moves.map(function (m) {
            var from = m.from_folder_name
              ? esc(m.from_drive_name) + " / " + esc(m.from_folder_name)
              : esc(m.from_drive_name) + " /";
            return '<div class="flex items-center gap-2 rounded-lg border border-base-content/10 px-3 py-2">'
              + '<i class="fas fa-table text-primary flex-shrink-0"></i>'
              + '<span class="font-medium truncate">' + esc(m.name) + '</span>'
              + '<span class="text-[11px] opacity-50 truncate ml-auto">' + from
              + ' <i class="fas fa-arrow-right mx-1"></i> '
              + esc(m.to_drive_name) + ' / ' + esc(m.to_folder_name) + '</span>'
              + '</div>';
          }).join('')
            + (data.skipped_synced ? '<p class="text-[11px] opacity-50 text-center">'
              + data.skipped_synced + ' table(s) on read-only drives stay put.</p>' : '');
          confirmBtn.disabled = false;
        })
        .catch(function (err) {
          box.innerHTML = '<p class="text-xs text-error py-4 text-center">'
            + esc(failMessage(err, "Could not compute the plan.")) + '</p>';
        });
    });

    function esc(s) {
      return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
      });
    }

    var scrollTick = false;
    function onScroll() {
      if (scrollTick || busy) return;
      scrollTick = true;
      requestAnimationFrame(function () {
        scrollTick = false;
        if (!scroller) return;
        var nearBottom =
          scroller.scrollTop + scroller.clientHeight >=
          scroller.scrollHeight - 200;
        var nearTop = scroller.scrollTop <= 200;
        if (nearBottom && start + pageSize < total) {
          loadWindow(start + pageSize);
        } else if (nearTop && start > 0) {
          loadWindow(start - pageSize);
        }
      });
    }

    if (prevBtn) prevBtn.addEventListener("click", function () {
      loadWindow(start - pageSize);
    });
    if (nextBtn) nextBtn.addEventListener("click", function () {
      loadWindow(start + pageSize);
    });
    if (scroller) scroller.addEventListener("scroll", onScroll);

    // History entry point: the panel is server-rendered (per-version
    // download/diff/restore reuse the existing drive routes); the button
    // only toggles it.
    var histBtn = document.getElementById("tables-history-btn");
    var histPanel = document.getElementById("tables-history-panel");
    if (histBtn && histPanel) histBtn.addEventListener("click", function () {
      histPanel.classList.toggle("hidden");
      histBtn.classList.toggle("btn-active");
    });

    // Filtered-notice and no-match blocks both offer Reset as the way out.
    function wireReset(id) {
      var btn = document.getElementById(id);
      if (btn) btn.addEventListener("click", resetFilter);
    }
    wireReset("tables-notice-reset");
    wireReset("tables-nomatch-reset");

    // Dirty guard (mirrors the notebooks keepalive pattern): an edit lives
    // only in the contentEditable <td> until blur commits it, so a tab
    // closed mid-edit would lose it silently. On unload, flush the focused
    // cell best-effort via keepalive — the response can never be read, but
    // the write still lands server-side (with its version snapshot).
    function flushPendingCell() {
      var el = document.activeElement;
      if (!el || !tbody.contains(el) ||
          el.getAttribute("contenteditable") !== "true") return;
      var row = parseInt(el.getAttribute("data-r") || "-1", 10);
      var col = el.getAttribute("data-c") || "";
      if (row < 0 || !col) return;
      try {
        fetch(baseUrl() + "/cell", {
          method: "POST", keepalive: true,
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ row: row, col: col, value: el.textContent })
        });
      } catch (err) { /* best effort: unload is fire-and-forget */ }
    }
    window.addEventListener("beforeunload", flushPendingCell);

    paintStatus();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
