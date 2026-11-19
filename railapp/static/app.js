(() => {
  "use strict";

  const form = document.getElementById("preview-form");
  if (!form) return;

  const UI = window.PreviewUI;
  const statusEl = document.getElementById("preview-status");
  const confirmStatus = document.getElementById("confirm-status");
  const root = document.getElementById("preview-root");
  const summary = document.getElementById("preview-summary");
  const stack = document.getElementById("preview-downstreams");
  const confirmButton = document.getElementById("confirm-button");
  const discardButton = document.getElementById("discard-button");
  const tpl = document.getElementById("downstream-template");

  let currentPreview = null;

  const setStatus = (el, text, kind) => {
    el.textContent = text || "";
    el.className = "status" + (kind ? " " + kind : "");
  };

  function renderPreview(preview) {
    currentPreview = preview;
    UI.renderAll({ summary, stack, tpl }, preview);

    root.hidden = false;
    confirmButton.disabled = !preview.convertible;
    confirmButton.title = preview.convertible
      ? ""
      : "存在不兼容的列或行，修正前不能发布";
    setStatus(confirmStatus, preview.convertible
      ? "请核对预览后确认发布；确认后该日修订号 +1。"
      : "存在不兼容内容，整批不可发布。", preview.convertible ? "" : "bad");
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    setStatus(statusEl, "上传并校验中…", "busy");
    root.hidden = true;
    const data = new FormData(form);
    try {
      const response = await fetch("/api/previews", { method: "POST", body: data });
      if (!response.ok) {
        const { text } = await UI.errorText(response);
        setStatus(statusEl, text, "bad");
        return;
      }
      const preview = await response.json();
      setStatus(statusEl, "", "");
      renderPreview(preview);
    } catch (err) {
      setStatus(statusEl, "网络错误：" + err.message, "bad");
    }
  });

  confirmButton.addEventListener("click", async () => {
    if (!currentPreview) return;
    confirmButton.disabled = true;
    setStatus(confirmStatus, "发布中…", "busy");
    try {
      const response = await fetch(
        `/api/previews/${encodeURIComponent(currentPreview.preview_id)}/confirm`,
        { method: "POST", headers: { Accept: "application/json" } }
      );
      if (!response.ok) {
        const { text } = await UI.errorText(response);
        setStatus(confirmStatus, text, "bad");
        if (response.status === 409 || response.status === 404) {
          root.hidden = true;
          currentPreview = null;
          setStatus(statusEl, "预览已失效，请重新上传并预览。", "bad");
        } else {
          confirmButton.disabled = false;
        }
        return;
      }
      const published = await response.json();
      setStatus(confirmStatus, "已发布，正在跳转…", "ok");
      window.location.href = "/days/" + encodeURIComponent(published.day);
    } catch (err) {
      setStatus(confirmStatus, "网络错误：" + err.message, "bad");
      confirmButton.disabled = false;
    }
  });

  discardButton.addEventListener("click", async () => {
    if (!currentPreview) return;
    const id = currentPreview.preview_id;
    try {
      await fetch(`/api/previews/${encodeURIComponent(id)}`, { method: "DELETE" });
    } catch (_) {
      /* best effort */
    }
    currentPreview = null;
    root.hidden = true;
    setStatus(statusEl, "已放弃预览。", "");
    form.reset();
  });
})();
