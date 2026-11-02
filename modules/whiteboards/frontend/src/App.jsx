import React, { useCallback, useEffect, useMemo, useRef } from "react";
import {
  Excalidraw,
  exportToBlob,
  restoreElements,
} from "@excalidraw/excalidraw";
import { mergeScenes } from "./merge.js";

function setSaveState(text) {
  const el = document.getElementById("wb-save-state");
  if (el) {
    el.textContent = text;
  }
}

export default function App() {
  const cfg = window.WB_CONFIG || {};
  const readOnly = !!cfg.readOnly;
  const initialScene = cfg.initialScene || {};

  const apiRef = useRef(null);
  // Server scenes carry only the whitelisted fields — restore fills in
  // Excalidraw runtime defaults (seed, versionNonce, text metrics, ...).
  // Without this, hand- or AI-made elements can be invisible or uneditable.
  const restoredInitial = useMemo(() => {
    try {
      return restoreElements(initialScene.elements || [], null, {
        refreshDimensions: true,
        repairBindings: true,
      });
    } catch {
      return initialScene.elements || [];
    }
  }, []);
  // Latest scene snapshot from onChange (elements, appState, files).
  const snapshotRef = useRef({
    elements: initialScene.elements || [],
    appState: initialScene.appState || {},
    files: {},
  });
  const versionRef = useRef(cfg.initialVersion || null);
  // Last mutually-synced scene (what the server had when we last agreed
  // with it). doSave three-way merges local edits against the server
  // instead of blindly overwriting, so concurrent AI edits survive.
  const baseRef = useRef({
    elements: initialScene.elements || [],
    appState: initialScene.appState || {},
  });
  // Dirty rule (see poll effect below): while `pendingRef` is true (local
  // edits not yet saved) or `savingRef` is true (a PUT is in flight), the
  // poller must NOT apply a remote scene, so it can never clobber the
  // user's in-flight drawing. The next poll after the save completes picks
  // the remote version up instead.
  const pendingRef = useRef(false);
  const savingRef = useRef(false);
  const saveTimerRef = useRef(null);
  const firstChangeRef = useRef(true);

  const doSave = useCallback(async () => {
    saveTimerRef.current = null;
    if (readOnly) {
      return;
    }
    if (savingRef.current) {
      // A save is already in flight; re-schedule so no edit is lost.
      saveTimerRef.current = setTimeout(doSave, 2000);
      return;
    }
    // Merge first: the AI may have edited the board since our last
    // sync. PUTting our snapshot blindly would silently delete that work
    // (this is how AI-added text used to "disappear").
    let toSave = snapshotRef.current;
    try {
      const latest = await fetch(cfg.sceneUrl, {
        headers: { Accept: "application/json" },
      });
      if (latest.ok) {
        const data = await latest.json();
        if (data && data.scene && data.version
            && data.version !== versionRef.current) {
          const merged = mergeScenes(
            baseRef.current.elements || [],
            (snapshotRef.current.elements || []),
            (data.scene.elements || []));
          toSave = {
            elements: merged,
            appState: snapshotRef.current.appState
              || data.scene.appState || {},
            files: snapshotRef.current.files || {},
          };
          snapshotRef.current = toSave;
        }
      }
    } catch { /* offline - fall through and try the plain save */ }
    const { elements, appState } = toSave;
    savingRef.current = true;
    setSaveState("Saving…");
    try {
      const res = await fetch(cfg.saveUrl, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": cfg.csrfToken || "",
        },
        body: JSON.stringify({ elements, appState }),
      });
      let data = null;
      try {
        data = await res.json();
      } catch {
        data = null;
      }
      // Any non-ok response counts as "Save failed" per the contract.
      if (!res.ok || !data || data.ok !== true) {
        throw new Error("save failed");
      }
      versionRef.current =
        data.version !== undefined ? data.version : versionRef.current;
      baseRef.current = { elements, appState };
      pendingRef.current = false;
      setSaveState("Saved");
    } catch {
      // Keep pendingRef true: the failed local edits are still unsaved, so
      // the poller keeps skipping remote applies until a retry succeeds.
      setSaveState("Save failed");
      saveTimerRef.current = setTimeout(doSave, 5000);
    } finally {
      savingRef.current = false;
    }
  }, [cfg.saveUrl, cfg.csrfToken, readOnly]);

  const handleChange = useCallback(
    (elements, appState, files) => {
      snapshotRef.current = { elements, appState, files: files || {} };
      if (readOnly) {
        return;
      }
      if (firstChangeRef.current) {
        // Excalidraw fires onChange once on mount; that is not a user edit.
        firstChangeRef.current = false;
        return;
      }
      pendingRef.current = true;
      if (saveTimerRef.current) {
        clearTimeout(saveTimerRef.current);
      }
      saveTimerRef.current = setTimeout(doSave, 2000);
    },
    [doSave, readOnly]
  );

  // Live AI updates: poll the scene endpoint and apply remote versions.
  // NOTE: restore MUST run with repairBindings + refreshDimensions.
  // Without them, container-bound text labels silently fail to render
  // on live updates (they only appeared after a full reload, whose
  // own restore pass repairs bindings).
  useEffect(() => {
    const id = setInterval(async () => {
      if (document.hidden) {
        return;
      }
      if (pendingRef.current || savingRef.current) {
        return;
      }
      let data = null;
      try {
        const res = await fetch(cfg.sceneUrl, {
          headers: { Accept: "application/json" },
        });
        if (!res.ok) {
          return;
        }
        data = await res.json();
      } catch {
        return; // Transient poll failure: try again on the next tick.
      }
      if (!data || data.version === versionRef.current) {
        return;
      }
      // Re-check after the await: a local edit may have landed meanwhile.
      if (pendingRef.current || savingRef.current) {
        return;
      }
      const scene = data.scene || {};
      const api = apiRef.current;
      if (api && typeof api.updateScene === "function") {
        // Server scenes carry only the whitelisted fields; restore fills
        // in Excalidraw runtime defaults (seed, versionNonce, ...).
        // repairBindings + refreshDimensions are REQUIRED here: without
        // them container-bound text labels silently fail to render on
        // live updates (they only appeared after a full reload).
        // The echo onChange this triggers schedules one redundant save of
        // identical content, which is benign (versions converge).
        let elements = scene.elements || [];
        try {
          elements = restoreElements(elements, null, {
            refreshDimensions: true,
            repairBindings: true,
          });
        } catch {
          elements = scene.elements || [];
        }
        api.updateScene({ elements, appState: scene.appState || {} });
        snapshotRef.current = {
          elements,
          appState: scene.appState || {},
          files: snapshotRef.current.files || {},
        };
        baseRef.current = {
          elements,
          appState: scene.appState || {},
        };
      }
      versionRef.current = data.version;
      setSaveState("Updated by AI");
    }, 3000);
    return () => clearInterval(id);
  }, [cfg.sceneUrl]);

  // PNG export button in the board toolbar (guard: button may be absent).
  useEffect(() => {
    const btn = document.getElementById("wb-export");
    if (!btn) {
      return undefined;
    }
    const onClick = async () => {
      try {
        const { elements, appState, files } = snapshotRef.current;
        const blob = await exportToBlob({
          elements: elements || [],
          appState: appState || {},
          files: files || {},
          mimeType: "image/png",
        });
        if (!blob) {
          return;
        }
        const rawTitle =
          initialScene.title || document.title || "whiteboard";
        const title = String(rawTitle).trim() || "whiteboard";
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `${title}.png`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 5000);
      } catch {
        // Export failed (e.g. no elements to render): leave UI untouched.
      }
    };
    btn.addEventListener("click", onClick);
    return () => btn.removeEventListener("click", onClick);
  }, [initialScene.title]);

  const isDark =
    typeof document !== "undefined" &&
    document.documentElement.dataset.theme === "dark";

  return (
    <div style={{ width: "100%", height: "100%" }}>
      <Excalidraw
        excalidrawAPI={(api) => {
          apiRef.current = api;
        }}
        initialData={{
          elements: restoredInitial,
          appState: initialScene.appState || {},
          scrollToContent: true,
        }}
        onChange={handleChange}
        viewModeEnabled={readOnly}
        theme={isDark ? "dark" : "light"}
      />
    </div>
  );
}
