(function (root, factory) {
  const docStatus = factory();
  if (root) root.docStatus = docStatus;
  if (typeof module !== "undefined" && module.exports) module.exports = docStatus;
})(typeof window !== "undefined" ? window : globalThis, function () {
  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (character) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[character]));
  }

  function docStatus(meta) {
    const issues = [];
    let severity = "ok";

    if (!meta) {
      issues.push({ text: "Metadata unavailable. This output could not be checked.", type: "warn" });
      severity = "warn";
    } else {
      const vstatus = meta.validation_status || "unvalidated";
      if (vstatus === "invalid") {
        issues.push({
          text: "Automatic checks found: " + (meta.validation_errors || []).slice(0, 3).join("; "),
          type: "error",
        });
        severity = "error";
      } else if (vstatus === "unvalidated") {
        if (meta.validation_supported === false) {
          issues.push({
            text: "Automatic structure checks are not defined for this output type.",
            type: "info",
          });
        } else {
          issues.push({
            text: "Automatic structure check unavailable for this older output format. Regenerate it to use current automatic checks.",
            type: "info",
          });
          severity = "info";
        }
      }

      const tracked = meta.reference_sources_tracked !== false;
      const atGen = meta.reference_freshness_at_generation;
      const changed = meta.reference_changed_since_generation || [];
      if (tracked && atGen == null) {
        issues.push({
          text: "Reference snapshot unavailable for this output — product-reference claims may predate current reference data.",
          type: "warn",
        });
        if (severity === "ok" || severity === "info") severity = "warn";
      } else if (tracked && atGen != null) {
        const stale = atGen.filter((r) => !r.fresh);
        if (stale.length) {
          issues.push({
            text: "Reference data was stale/missing when generated: " + stale.map((r) => `${r.label}${r.age_days != null ? " (" + r.age_days + " days old)" : r.status === "missing" ? " (missing)" : ""}`).join(", ") + ".",
            type: "warn",
          });
          if (severity === "ok" || severity === "info") severity = "warn";
        }
        if (changed.length) {
          issues.push({
            text: "Reference data has changed since generation: " + changed.map((c) => `${c.label}${c.new_date ? " (now " + c.new_date + ")" : ""}`).join(", ") + ".",
            type: "warn",
          });
          if (severity === "ok" || severity === "info") severity = "warn";
        }
      }
    }

    const labels = {
      ok: "Ready",
      info: "Checks unavailable",
      warn: "Review sources",
      error: "Output incomplete",
    };
    return {
      severity,
      issues,
      label: labels[severity],
      configKey: severity,
    };
  }

  docStatus.escapeHtml = escapeHtml;
  return docStatus;
});
