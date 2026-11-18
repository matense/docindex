/* Tables viewer (T2): slides a 50-row window through rows?offset&limit.
 * The server paints the first page; this script replaces the tbody window
 * on scroll/pager instead of ever holding the whole file in the DOM. */
(function () {
  "use strict";

  function init() {
    var mount = document.getElementById("tables-grid");
    if (!mount) return;
    var fileId = mount.getAttribute("data-file-id");
    var total = parseInt(mount.getAttribute("data-total") || "0", 10);
    var pageSize = parseInt(mount.getAttribute("data-page-size") || "50", 10);
    var tbody = document.getElementById("tables-grid-body");
    var status = document.getElementById("tables-status");
    var loading = document.getElementById("tables-loading");
    var prevBtn = document.getElementById("tables-prev");
    var nextBtn = document.getElementById("tables-next");
    var scroller = document.getElementById("tables-scroll");
    if (!tbody || !fileId) return;

    var start = 0;
    var busy = false;

    function baseUrl() {
      return "/m/tables/" + encodeURIComponent(fileId) + "/rows";
    }

    function showLoading(on) {
      busy = on;
      if (loading) loading.classList.toggle("hidden", !on);
    }

    function render(rows) {
      tbody.textContent = "";
      for (var i = 0; i < rows.length; i++) {
        var tr = document.createElement("tr");
        for (var j = 0; j < rows[i].length; j++) {
          var td = document.createElement("td");
          td.textContent = rows[i][j];
          tr.appendChild(td);
        }
        tbody.appendChild(tr);
      }
    }

    function paintStatus() {
      if (!status) return;
      if (total === 0) {
        status.textContent = "No rows";
        return;
      }
      var end = Math.min(start + pageSize, total);
      status.textContent = "Rows " + (start + 1) + "-" + end + " of " + total;
      if (prevBtn) prevBtn.disabled = start <= 0;
      if (nextBtn) nextBtn.disabled = end >= total;
    }

    function loadWindow(next) {
      if (busy) return;
      next = Math.max(0, Math.min(next, Math.max(0, total - 1)));
      showLoading(true);
      fetch(baseUrl() + "?offset=" + next + "&limit=" + pageSize)
        .then(function (resp) {
          if (!resp.ok) throw new Error("rows fetch failed: " + resp.status);
          return resp.json();
        })
        .then(function (data) {
          start = next;
          render(data.rows || []);
          paintStatus();
        })
        .catch(function () {
          if (status) status.textContent = "Could not load rows.";
        })
        .finally(function () {
          showLoading(false);
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
    paintStatus();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
