"use strict";

// Read only the already-escaped, current response DOM. No storage or fetch.
(() => {
  const table = document.querySelector("#issued-code-table");
  if (!table) return;
  const rows = () => Array.from(table.querySelectorAll("tr"), row =>
    Array.from(row.querySelectorAll("th, td"), cell => cell.textContent));
  const csvCell = value => {
    // Neutralize the first meaningful character, including after whitespace
    // and control characters. Quote every cell and double embedded quotes.
    if (/^[\s\u0000-\u001f\u007f]*[=+\-@]/u.test(value)) value = "'" + value;
    return '"' + value.replaceAll('"', '""') + '"';
  };
  document.getElementById("download-csv").addEventListener("click", () => {
    const csv = "\ufeff" + rows().map(row => row.map(csvCell).join(",")).join("\r\n") + "\r\n";
    const url = URL.createObjectURL(new Blob([csv], {type: "text/csv;charset=utf-8"}));
    const link = document.createElement("a");
    try {
      link.href = url;
      link.download = "coursmith-codes.csv";
      document.body.appendChild(link);
      link.click();
    } finally {
      link.remove();
      URL.revokeObjectURL(url);
    }
  });
  document.getElementById("copy-codes").addEventListener("click", async () => {
    const status = document.getElementById("copy-status");
    try {
      await navigator.clipboard.writeText(rows().slice(1).map(row => row[1]).join("\n"));
      status.textContent = "已复制，请妥善交付。";
    } catch {
      status.textContent = "无法自动复制，请手动选择表格中的激活码。";
    }
  });
})();
