/* Tables viewer (T2): slides a 50-row window through rows?offset&limit.
 * T3 adds the SQL filter bar (POST query, SELECT/WITH only — the filtered
 * view replaces the grid and the pager resets to page 1) and versioned
 * inline cell editing on .ptable files (POST cell; csv/tsv/xlsx stay
 * read-only until "Save as .ptable"). */
(function () {
  "use strict";

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

    function showLoading(on) {
      busy = on;
      if (loading) loading.classList.toggle("hidden", !on);
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
      if (!status) return;
      if (total === 0) {
        status.textContent = filtered ? "No matching rows" : "No rows";
        return;
      }
      var end = Math.min(start + pageSize, total);
      var label = filtered ? "Matching rows " : "Rows ";
      status.textContent = label + (start + 1) + "-" + end + " of " + total;
      if (prevBtn) prevBtn.disabled = start <= 0;
      if (nextBtn) nextBtn.disabled = end >= total;
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
      fetch(baseUrl() + "/rows?offset=" + next + "&limit=" + pageSize)
        .then(function (resp) {
          if (!resp.ok) throw new Error("rows fetch failed: " + resp.status);
          return resp.json();
        })
        .then(function (data) {
          applyData(data, next);
        })
        .catch(function () {
          if (status) status.textContent = "Could not load rows.";
        })
        .finally(function () {
          showLoading(false);
        });
    }

    function runQuery(next) {
      var sql = sqlInput ? sqlInput.value : "";
      fetch(baseUrl() + "/query", {
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
        .catch(function () {
          showError("Query failed unexpectedly.");
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
      loadWindow(0);
    }

    function commitEdit(td) {
      var row = parseInt(td.getAttribute("data-r") || "-1", 10);
      var col = td.getAttribute("data-c") || "";
      var value = td.textContent;
      if (row < 0 || !col) return;
      showError(null);
      fetch(baseUrl() + "/cell", {
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
            showError((out.data && out.data.error) || "Could not save.");
            loadWindow(start);
            return;
          }
          td.textContent = out.data.value;
          if (status) status.textContent = "Saved.";
          paintStatusSoon();
        })
        .catch(function () {
          showError("Could not save the cell.");
          loadWindow(start);
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
      fetch(baseUrl() + "/save-as", { method: "POST" })
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
        .catch(function () {
          showLoading(false);
          showError("Save failed unexpectedly.");
        });
    });

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
    paintStatus();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
