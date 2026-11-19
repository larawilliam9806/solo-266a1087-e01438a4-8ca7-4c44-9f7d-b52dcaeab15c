(() => {
  "use strict";

  const scanButton = document.getElementById("scan-button");
  if (!scanButton) return;

  const UI = window.PreviewUI;
  const scanStatus = document.getElementById("scan-status");
  const scanSummary = document.getElementById("scan-summary");
  const tableBody = document.querySelector("#intake-table tbody");
  const previewPanel = document.getElementById("preview-panel");
  const previewFor = document.getElementById("preview-for");
  const summary = document.getElementById("preview-summary");
  const stack = document.getElementById("preview-downstreams");
  const confirmButton = document.getElementById("confirm-button");
  const closeButton = document.getElementById("close-button");
  const confirmStatus = document.getElementById("confirm-status");
  const tpl = document.getElementById("downstream-template");

  let currentPreview = null;

  const setStatus = (el, text, kind) => {
    el.textContent = text || "";
    el.className = "status" + (kind ? " " + kind : "");
  };

  const STATUS_LABELS = {
    received: ["待发布", "warn"],
    ack_pending: ["待补写回执", "bad"],
    published: ["已发布", "ok"],
  };

  function badge(text, kind) {
    const span = UI.el("span", "badge " + (kind || ""), text);
    return span;
  }

  async function refreshIntakes() {
    let payload;
    try {
      const response = await fetch("/api/intakes", { headers: { Accept: "application/json" } });
      payload = await response.json();
    } catch (err) {
      tableBody.replaceChildren();
      const tr = UI.el("tr");
      const td = UI.el("td", "empty", "加载失败：" + err.message);
      td.colSpan = 8;
      tr.appendChild(td);
      tableBody.appendChild(tr);
      return;
    }
    renderIntakes(payload.intakes || []);
  }

  function renderIntakes(intakes) {
    tableBody.replaceChildren();
    if (!intakes.length) {
      const tr = UI.el("tr");
      const td = UI.el("td", "empty", "共享目录中还没有已收件的交件，点击“扫描共享目录”。");
      td.colSpan = 8;
      tr.appendChild(td);
      tableBody.appendChild(tr);
      return;
    }
    intakes.forEach((intake) => {
      const tr = UI.el("tr");
      tr.appendChild(UI.el("td", "mono", intake.intake_id));
      tr.appendChild(UI.el("td", "", intake.day));

      const orig = UI.el("td");
      orig.appendChild(UI.el("span", "", intake.original.name));
      orig.appendChild(document.createElement("br"));
      const meta = UI.el("span", "muted mono",
        `${intake.original.sha256.slice(0, 12)}… · ${intake.original.size} B`);
      orig.appendChild(meta);
      tr.appendChild(orig);

      const statusTd = UI.el("td");
      const [label, kind] = STATUS_LABELS[intake.status] || [intake.status, ""];
      statusTd.appendChild(badge(label, kind));
      if (intake.conflict) {
        statusTd.appendChild(document.createTextNode(" "));
        const conflict = badge("冲突", "bad");
        conflict.title = intake.conflict.detail || "";
        statusTd.appendChild(conflict);
      }
      tr.appendChild(statusTd);

      tr.appendChild(UI.el("td", "", intake.published_rev ? "r" + intake.published_rev : "—"));

      const ackTd = UI.el("td");
      if (intake.status === "published" && intake.ack_written_at) {
        ackTd.appendChild(UI.el("span", "mono", "已写 " + intake.ack_written_at));
      } else if (intake.status === "ack_pending") {
        ackTd.appendChild(UI.el("span", "", "待补写"));
      } else {
        ackTd.textContent = "—";
      }
      tr.appendChild(ackTd);

      const files = [];
      files.push(intake.files.csv ? "csv✓" : "csv✗");
      files.push(intake.files.ready ? "ready✓" : "ready✗");
      files.push(intake.files.ack ? "ack✓" : "ack✗");
      tr.appendChild(UI.el("td", "muted mono", files.join(" ")));

      const actions = UI.el("td", "actions");
      if (intake.status === "received") {
        const btn = UI.el("button", "button small", "预览");
        btn.addEventListener("click", () => openPreview(intake.intake_id));
        actions.appendChild(btn);
      }
      if (intake.status === "ack_pending") {
        const btn = UI.el("button", "button small ghost", "补写回执");
        btn.addEventListener("click", () => retryAck(intake.intake_id));
        actions.appendChild(btn);
      }
      tr.appendChild(actions);
      tableBody.appendChild(tr);
    });
  }

  function renderScanSummary(result) {
    scanSummary.hidden = false;
    scanSummary.replaceChildren();
    const lines = [];
    if (result.received.length) lines.push(`新收件：${result.received.join("、")}`);
    if (result.known.length) lines.push(`无变化：${result.known.length} 个交件`);
    if (result.conflicts.length) {
      lines.push("冲突：" + result.conflicts.map((c) => `${c.intake_id}（${c.detail}）`).join("；"));
    }
    if (result.skipped_no_ready.length) {
      lines.push(`跳过（尚无 .ready）：${result.skipped_no_ready.join("、")}`);
    }
    if (result.ack_rewritten.length) lines.push(`已补写回执：${result.ack_rewritten.join("、")}`);
    if (result.ack_failed.length) {
      lines.push("回执补写失败：" + result.ack_failed.map((f) => `${f.intake_id}（${f.error}）`).join("；"));
    }
    if (result.errors.length) {
      lines.push("错误：" + result.errors.map((e) => `${e.intake_id}（${e.error}）`).join("；"));
    }
    if (result.invalid.length) {
      lines.push("无效文件：" + result.invalid.map((e) => `${e.file}（${e.reason}）`).join("；"));
    }
    if (!lines.length) lines.push("共享目录为空。");
    lines.forEach((text) => scanSummary.appendChild(UI.el("p", "scan-line", text)));
  }

  scanButton.addEventListener("click", async () => {
    scanButton.disabled = true;
    setStatus(scanStatus, "扫描中…", "busy");
    try {
      const response = await fetch("/api/inbox/scan", {
        method: "POST",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) {
        const { text } = await UI.errorText(response);
        setStatus(scanStatus, text, "bad");
        return;
      }
      const result = await response.json();
      setStatus(scanStatus, `扫描完成（${result.scanned_at}）`, "ok");
      renderScanSummary(result);
      if (result.intakes) {
        renderIntakes(result.intakes);
      } else {
        await refreshIntakes();
      }
    } catch (err) {
      setStatus(scanStatus, "网络错误：" + err.message, "bad");
    } finally {
      scanButton.disabled = false;
    }
  });

  async function openPreview(intakeId) {
    setStatus(confirmStatus, "", "");
    previewPanel.hidden = true;
    try {
      const response = await fetch(
        `/api/intakes/${encodeURIComponent(intakeId)}/previews`,
        { method: "POST", headers: { Accept: "application/json" } }
      );
      if (!response.ok) {
        const { text } = await UI.errorText(response);
        setStatus(scanStatus, `交件 ${intakeId} 预览失败：${text}`, "bad");
        return;
      }
      const preview = await response.json();
      currentPreview = preview;
      UI.renderAll({ summary, stack, tpl }, preview);
      previewFor.textContent = `交件 ${intakeId}`;
      previewPanel.hidden = false;
      confirmButton.disabled = !preview.convertible;
      setStatus(confirmStatus, preview.convertible
        ? "请核对预览后确认发布；发布后将按收件原件放出回执。"
        : "存在不兼容内容，整批不可发布。", preview.convertible ? "" : "bad");
      previewPanel.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (err) {
      setStatus(scanStatus, "网络错误：" + err.message, "bad");
    }
  }

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
          previewPanel.hidden = true;
          currentPreview = null;
          await refreshIntakes();
        } else {
          confirmButton.disabled = false;
        }
        return;
      }
      const published = await response.json();
      const intake = published.intake || {};
      const ackText = intake.ack_written
        ? `回执 ${intake.ack_file} 已放出`
        : `回执待补写（${intake.ack_error || "稍候扫描自动补写"}）`;
      setStatus(confirmStatus,
        `已发布 ${published.day} 修订 r${published.rev}；${ackText}。`, "ok");
      currentPreview = null;
      await refreshIntakes();
    } catch (err) {
      setStatus(confirmStatus, "网络错误：" + err.message, "bad");
      confirmButton.disabled = false;
    }
  });

  closeButton.addEventListener("click", () => {
    previewPanel.hidden = true;
    currentPreview = null;
  });

  async function retryAck(intakeId) {
    try {
      const response = await fetch(
        `/api/intakes/${encodeURIComponent(intakeId)}/ack`,
        { method: "POST", headers: { Accept: "application/json" } }
      );
      if (!response.ok) {
        const { text } = await UI.errorText(response);
        setStatus(scanStatus, `补写回执失败：${text}`, "bad");
        return;
      }
      setStatus(scanStatus, `交件 ${intakeId} 回执已补写。`, "ok");
      await refreshIntakes();
    } catch (err) {
      setStatus(scanStatus, "网络错误：" + err.message, "bad");
    }
  }

  refreshIntakes();
})();
