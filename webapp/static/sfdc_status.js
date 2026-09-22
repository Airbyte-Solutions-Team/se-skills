(function (root, factory) {
  const sfdcStatus = factory();
  if (root) root.sfdcStatus = sfdcStatus;
  if (typeof module !== "undefined" && module.exports) module.exports = sfdcStatus;
})(typeof window !== "undefined" ? window : globalThis, function () {
  const SEVERITY = { ok: "ok", auth_error: "error", not_installed: "warn", error: "warn" };
  const LABEL = {
    ok: "SFDC connected",
    auth_error: "SFDC needs reauth",
    not_installed: "SFDC CLI missing",
    error: "SFDC error",
  };

  function sfdcStatus(raw) {
    if (!raw || raw.state === "disabled") {
      return {
        visible: false,
        severity: "ok",
        label: "",
        message: "",
        orgAlias: raw && raw.org_alias,
        reauthCommand: null,
      };
    }
    const severity = SEVERITY[raw.state] || "warn";
    return {
      visible: true,
      severity,
      label: LABEL[raw.state] || "SFDC issue",
      message: raw.message || "",
      orgAlias: raw.org_alias || null,
      reauthCommand:
        raw.state === "auth_error" && raw.org_alias ? `sf org login web --alias ${raw.org_alias}` : null,
    };
  }

  return sfdcStatus;
});
