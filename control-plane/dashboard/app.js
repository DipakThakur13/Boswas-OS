/*
 * Boswas Control Plane dashboard: vanilla ES2020, no build step, no dependencies.
 *
 * Security constraints (enforced by the server's Content-Security-Policy and
 * checked by control-plane/tests/test_dashboard.py):
 *   - only same-origin files (index.html, app.js, app.css, favicon.svg); no
 *     inline scripts or styles, no eval or Function constructor, no external
 *     resources, no HTML parsing of data;
 *   - every element is created by h(); text becomes text nodes (the textContent
 *     path, never parsed as markup), and h() refuses event-handler and style
 *     attributes and any link that is not an in-page "#" route;
 *   - the operator API token is kept only in sessionStorage (this tab), sent
 *     as a bearer token to /api/v1 by api(), and cleared on sign-out and on
 *     any 401 answer; it never goes into URLs, cookies or persistent storage;
 *   - one-time secrets (new enrollment or operator tokens) are shown once in
 *     a dialog and dropped when it closes;
 *   - devices are managed through typed commands only: a command type plus a
 *     catalog application ID. The dashboard has no free-text command input.
 */
"use strict";

(function () {
  // ---------------------------------------------------------------------------
  // Constants
  // ---------------------------------------------------------------------------
  const API_PREFIX = "/api/v1/";
  const TOKEN_KEY = "boswas-cp.operator-token";
  const TOKEN_RE = /^bcp_[0-9a-f]{12}_[A-Za-z0-9_-]{43}$/;
  const REFRESH_MS = 15000;
  const REQUEST_TIMEOUT_MS = 30000;
  const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
  const APP_ID_RE = /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$/;
  const AGENT_VERSION_RE = /^[A-Za-z0-9][A-Za-z0-9._~+:-]{0,63}$/;
  const IDEMPOTENCY_RE = /^[A-Za-z0-9._:-]{1,64}$/;
  const POLICY_NAME_RE = /^[a-z0-9][a-z0-9-]{0,31}$/;
  const PROFILE_RE = /^[a-z0-9][a-z0-9._-]{0,63}$/;
  const OPERATOR_RE = /^[a-z][a-z0-9._-]{1,31}$/;
  const SAFE_HREF_RE = /^#[^\s]*$/;
  const FORBIDDEN_ATTR_RE = /^(on|style$|srcdoc$|formaction$|innerhtml$|outerhtml$)/i;
  const CONTROL_CHAR_RE = /[\u0000-\u001f\u007f]/;

  const ROLE_RANK = { viewer: 0, operator: 1, admin: 2 };

  const APPLICATION_COMMANDS = ["INSTALL_APPLICATION", "UPDATE_APPLICATION", "REMOVE_APPLICATION",
    "LAUNCH_APPLICATION", "STOP_APPLICATION", "REPAIR_APPLICATION"];
  const DEVICE_COMMANDS = ["REFRESH_INVENTORY", "APPLY_POLICY", "UPDATE_AGENT"];
  const COMMAND_TYPES = APPLICATION_COMMANDS.concat(DEVICE_COMMANDS);
  const COMMAND_LABELS = {
    INSTALL_APPLICATION: "Install", UPDATE_APPLICATION: "Update", REMOVE_APPLICATION: "Remove",
    LAUNCH_APPLICATION: "Launch", STOP_APPLICATION: "Stop", REPAIR_APPLICATION: "Repair",
    REFRESH_INVENTORY: "Refresh inventory", APPLY_POLICY: "Apply policy", UPDATE_AGENT: "Update agent",
  };
  const COMMAND_STATUSES = ["QUEUED", "SENT", "ACKNOWLEDGED", "RUNNING", "SUCCEEDED", "FAILED", "EXPIRED",
    "CANCELLED"];
  const EVENT_TYPES = ["DEVICE_REGISTERED", "DEVICE_ONLINE", "DEVICE_OFFLINE", "DEVICE_UPDATED", "DEVICE_RETIRED",
    "APPLICATION_INSTALLED", "APPLICATION_REMOVED", "APPLICATION_LAUNCHED", "APPLICATION_BLOCKED",
    "APPLICATION_REPAIRED", "POLICY_UPDATED", "COMMAND_CREATED", "COMMAND_COMPLETED", "COMMAND_FAILED",
    "COMMAND_CANCELLED", "SECURITY_EVENT", "CATALOG_UPDATED", "ARTIFACT_UPLOADED", "ENROLLMENT_TOKEN_CREATED",
    "OPERATOR_CHANGED"];
  const COMPAT_STATUSES = ["unknown", "untested", "experimental", "tested", "approved"];
  const POLICY_DEVICES = ["display", "audio", "gpu"];
  const POLICY_FOLDERS = ["documents", "downloads", "desktop", "pictures", "music", "videos"];
  const DEVICE_LABELS = { display: "Display", audio: "Audio", gpu: "GPU acceleration" };

  // Mirrors boswas_agent.policydoc.default_policy(); used when no policy exists yet.
  const DEFAULT_POLICY_DOC = {
    description: "",
    compat: {
      allowed_statuses: ["approved", "tested"], unlisted_apps: "deny", unlisted_network: false,
      unlisted_devices: ["display"], unlisted_folders: [], require_apparmor: true, max_installer_mb: 4096,
      blocked_applications: [], allowed_applications: null,
    },
    agent: { heartbeat_seconds: 300, inventory_seconds: 3600, allowed_commands: COMMAND_TYPES.slice() },
    updates: { agent_updates: "manual" },
  };

  const TONES = {
    ok: ["online", "READY", "SUCCEEDED", "INSTALLED", "COMPLIANT", "PASS", "SUPPORTED", "active", "current",
      "approved", "tested", "yes", "healthy", "enforce", "valid", "DEVICE_ONLINE", "COMMAND_COMPLETED"],
    warn: ["offline", "DEGRADED", "OFFLINE", "MAINTENANCE", "WARN", "REPAIR_REQUIRED", "EXPIRED", "PENDING",
      "outdated", "MISSING_DEPENDENCIES", "experimental", "deprecated", "expired", "used up", "complain",
      "expiring", "COMMAND_FAILED", "DEVICE_OFFLINE", "DEVICE_RETIRED"],
    bad: ["ERROR", "FAILED", "FAIL", "BLOCKED", "UNSUPPORTED", "UNSUPPORTED_ARCHITECTURE", "NON_COMPLIANT",
      "blocked", "revoked", "invalid", "unhealthy", "unavailable", "SECURITY_EVENT", "APPLICATION_BLOCKED"],
    info: ["QUEUED", "SENT", "ACKNOWLEDGED", "RUNNING", "INSTALLING", "UPDATING", "INITIALIZING", "update",
      "admin"],
  };

  const LABELS = {
    never: "Never connected", "used up": "Used up", "control-plane": "Control Plane", x86_64: "x86_64 / 64-bit",
    x86: "x86 / 32-bit", current: "Current", outdated: "Outdated", unreported: "Not reported yet",
    none: "No published version", enforce: "Enforcing", gpu: "GPU",
  };

  // ---------------------------------------------------------------------------
  // Pure helpers (no DOM; exercised by the Node smoke test)
  // ---------------------------------------------------------------------------
  function parseTime(iso) {
    if (typeof iso !== "string" || !iso) return null;
    const t = Date.parse(iso);
    return Number.isNaN(t) ? null : t;
  }

  /** "2 min ago", "in 3 days"; the absolute UTC time goes into a title attribute. */
  function relativeTime(iso, now) {
    const t = parseTime(iso);
    if (t === null) return "—";
    const ref = typeof now === "number" ? now : Date.now();
    const diff = Math.round((ref - t) / 1000);
    const abs = Math.abs(diff);
    if (abs < 45) return diff >= 0 ? "just now" : "in a few seconds";
    let text;
    if (abs < 120) text = "1 min";
    else if (abs < 3600) text = `${Math.floor(abs / 60)} min`;
    else if (abs < 7200) text = "1 hour";
    else if (abs < 86400) text = `${Math.floor(abs / 3600)} hours`;
    else if (abs < 172800) text = "1 day";
    else if (abs < 30 * 86400) text = `${Math.floor(abs / 86400)} days`;
    else if (abs < 365 * 86400) {
      const months = Math.max(1, Math.floor(abs / (30 * 86400)));
      text = months === 1 ? "1 month" : `${months} months`;
    } else {
      const years = Math.floor(abs / (365 * 86400));
      text = years === 1 ? "1 year" : `${years} years`;
    }
    return diff >= 0 ? `${text} ago` : `in ${text}`;
  }

  function absoluteUtc(iso) {
    const t = parseTime(iso);
    if (t === null) return "";
    return new Date(t).toISOString().replace("T", " ").replace(/\.\d{3}Z$/, " UTC");
  }

  function badgeTone(value) {
    const text = value === null || value === undefined ? "" : String(value);
    for (const tone of Object.keys(TONES)) {
      if (TONES[tone].includes(text)) return tone;
    }
    return "neutral";
  }

  function badgeClass(value) {
    return `badge badge--${badgeTone(value)}`;
  }

  function humanize(value) {
    if (value === null || value === undefined || value === "") return "—";
    const text = String(value);
    if (Object.prototype.hasOwnProperty.call(LABELS, text)) return LABELS[text];
    if (/^[A-Z0-9_]+$/.test(text)) {
      const words = text.toLowerCase().replace(/_/g, " ");
      return words.charAt(0).toUpperCase() + words.slice(1);
    }
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  function shortId(id) {
    return typeof id === "string" && id.length > 8 ? id.slice(0, 8) : String(id || "—");
  }

  function shortHash(sha) {
    return typeof sha === "string" && sha.length > 16 ? `${sha.slice(0, 12)}…` : String(sha || "—");
  }

  function formatBytes(bytes) {
    const n = Number(bytes);
    if (!Number.isFinite(n) || n < 0) return "—";
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let value = n;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return unit === 0 ? `${n} B` : `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`;
  }

  function formatDuration(seconds) {
    const s = Number(seconds);
    if (!Number.isFinite(s) || s < 0) return "—";
    const days = Math.floor(s / 86400);
    const hours = Math.floor((s % 86400) / 3600);
    const minutes = Math.floor((s % 3600) / 60);
    if (days) return `${days} d ${hours} h`;
    if (hours) return `${hours} h ${minutes} min`;
    return `${minutes} min`;
  }

  function queryString(params) {
    if (!params) return "";
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null && value !== "") search.append(key, String(value));
    }
    const text = search.toString();
    return text ? `?${text}` : "";
  }

  /** "#/devices/abc?filter=online" -> {path: "devices/abc", query: {filter: "online"}} */
  function parseRoute(hash) {
    const raw = String(hash || "").replace(/^#\/?/, "");
    const q = raw.indexOf("?");
    const path = (q >= 0 ? raw.slice(0, q) : raw).replace(/\/+$/, "") || "overview";
    const query = {};
    if (q >= 0) {
      for (const [key, value] of new URLSearchParams(raw.slice(q + 1))) query[key] = value;
    }
    return { path, query };
  }

  function roleAllows(role, needed) {
    const have = Object.prototype.hasOwnProperty.call(ROLE_RANK, role) ? ROLE_RANK[role] : -1;
    return have >= ROLE_RANK[needed];
  }

  function isAppId(value) {
    return typeof value === "string" && value.length <= 96 && APP_ID_RE.test(value);
  }

  /** The JSON body of POST /api/v1/devices/{id}/commands: a type and its typed fields, nothing else. */
  function buildCommandBody(type, options) {
    const opts = options || {};
    const body = { type };
    if (APPLICATION_COMMANDS.includes(type)) {
      if (!isAppId(opts.applicationId)) throw new Error(`${humanize(type)} needs a catalog application ID.`);
      body.application_id = opts.applicationId;
    } else if (type === "UPDATE_AGENT") {
      if (typeof opts.version !== "string" || !AGENT_VERSION_RE.test(opts.version)) {
        throw new Error("Update agent needs a version such as 1.0~alpha3.");
      }
      body.version = opts.version;
    } else if (!DEVICE_COMMANDS.includes(type)) {
      throw new Error(`Unknown command type: ${String(type)}`);
    }
    if (opts.ttlSeconds !== undefined) {
      if (!Number.isInteger(opts.ttlSeconds) || opts.ttlSeconds < 60 || opts.ttlSeconds > 604800) {
        throw new Error("The command lifetime must be 60 to 604800 seconds.");
      }
      body.ttl_seconds = opts.ttlSeconds;
    }
    if (opts.idempotencyKey !== undefined) {
      if (typeof opts.idempotencyKey !== "string" || !IDEMPOTENCY_RE.test(opts.idempotencyKey)) {
        throw new Error("Invalid idempotency key.");
      }
      body.idempotency_key = opts.idempotencyKey;
    }
    return body;
  }

  /** Per-row application actions for a device's application, from its reported state. */
  function appActions(app) {
    const s = app && app.app_state ? app.app_state : "INSTALLED";
    if (s === "INSTALLING") return [];
    const out = [];
    const running = s === "RUNNING" || Number(app && app.running) > 0;
    if (running) out.push("STOP_APPLICATION");
    else if (s === "INSTALLED" || s === "STOPPED") out.push("LAUNCH_APPLICATION");
    if (app && app.update_available) out.push("UPDATE_APPLICATION");
    if (s !== "BLOCKED" && s !== "UNSUPPORTED") out.push("REPAIR_APPLICATION");
    out.push("REMOVE_APPLICATION");
    return out;
  }

  /** Why a catalog entry cannot be installed (null when it can). */
  function installBlockReason(app) {
    if (!app || app.installable) return null;
    if (app.architecture !== "x86_64" || app.support === "UNSUPPORTED_ARCHITECTURE") {
      return "x86 / 32-bit — not supported by Boswas OS";
    }
    if (app.deprecated) return "deprecated in the catalog";
    if (app.support === "MISSING_DEPENDENCIES") return "needs extra dependencies — not installable";
    if (app.status === "blocked") return "blocked in the catalog";
    return "not installable";
  }

  /** Confirmation text that names the device and the application. */
  function describeCommand(type, deviceName, appName, version) {
    const d = `“${deviceName}”`;
    const a = appName ? `“${appName}”` : "the application";
    switch (type) {
      case "INSTALL_APPLICATION":
        return `Install ${a}${version ? ` version ${version}` : ""} on ${d}. The device downloads the pinned ` +
          "installer from the Control Plane and verifies its SHA-256 before installing.";
      case "UPDATE_APPLICATION":
        return `Update ${a} on ${d}${version ? ` to catalog version ${version}` : ""}.`;
      case "REMOVE_APPLICATION":
        return `Remove ${a} from ${d}.`;
      case "LAUNCH_APPLICATION":
        return `Launch ${a} on ${d} in the signed-in user's session.`;
      case "STOP_APPLICATION":
        return `Stop ${a} on ${d}. Unsaved work in the application may be lost.`;
      case "REPAIR_APPLICATION":
        return `Repair ${a} on ${d}.`;
      case "REFRESH_INVENTORY":
        return `Ask ${d} to collect and send a fresh inventory.`;
      case "APPLY_POLICY":
        return `Ask ${d} to fetch, verify and apply its assigned policy${version ? ` (${version})` : ""}.`;
      case "UPDATE_AGENT":
        return `Update the device agent on ${d}${version ? ` to version ${version}` : ""}.`;
      default:
        return `${humanize(type)} on ${d}.`;
    }
  }

  function summarizeDetail(detail) {
    if (detail === null || detail === undefined) return "";
    if (typeof detail !== "object") return String(detail);
    const parts = [];
    for (const [key, value] of Object.entries(detail)) {
      if (value === null || value === undefined || value === "") continue;
      const text = typeof value === "object" ? JSON.stringify(value) : String(value);
      parts.push(`${key.replace(/_/g, " ")}: ${text}`);
    }
    const out = parts.join("; ");
    return out.length > 300 ? `${out.slice(0, 299)}…` : out;
  }

  function policyState(device) {
    if (!device || !device.policy_version) return "none";
    if (device.policy_current) return "current";
    if (!device.reported_policy_version) return "unreported";
    return "outdated";
  }

  function tokenState(token, now) {
    if (token.revoked) return "revoked";
    if (Number(token.uses) >= Number(token.max_uses)) return "used up";
    const expires = parseTime(token.expires_at);
    if (expires !== null && expires <= (typeof now === "number" ? now : Date.now())) return "expired";
    return "active";
  }

  function parseIdList(text) {
    const seen = new Set();
    const out = [];
    for (const item of String(text || "").split(/[\s,]+/)) {
      const id = item.trim();
      if (id && !seen.has(id)) {
        seen.add(id);
        out.push(id);
      }
    }
    return out;
  }

  function toInt(value) {
    const text = String(value === null || value === undefined ? "" : value).trim();
    return /^\d{1,9}$/.test(text) ? Number(text) : NaN;
  }

  function rangeCheck(value, low, high, label, problems) {
    if (!Number.isInteger(value) || value < low || value > high) {
      problems.push(`${label}: a whole number from ${low} to ${high}.`);
    }
  }

  /** POST /api/v1/policies body from editor values. AppArmor confinement is always required. */
  function buildPolicyBody(v) {
    const pick = (allowed, chosen) => allowed.filter((item) => (chosen || []).includes(item));
    const allowList = parseIdList(v.allowedApplications);
    return {
      name: String(v.name || "").trim(),
      description: String(v.description || "").trim(),
      compat: {
        allowed_statuses: pick(COMPAT_STATUSES, v.allowedStatuses),
        unlisted_apps: v.unlistedApps === "allow" ? "allow" : "deny",
        unlisted_network: Boolean(v.unlistedNetwork),
        unlisted_devices: pick(POLICY_DEVICES, v.unlistedDevices),
        unlisted_folders: pick(POLICY_FOLDERS, v.unlistedFolders),
        require_apparmor: true,
        max_installer_mb: toInt(v.maxInstallerMb),
        blocked_applications: parseIdList(v.blockedApplications),
        allowed_applications: allowList.length ? allowList : null,
      },
      agent: {
        heartbeat_seconds: toInt(v.heartbeatSeconds),
        inventory_seconds: toInt(v.inventorySeconds),
        allowed_commands: pick(COMMAND_TYPES, v.allowedCommands),
      },
      updates: { agent_updates: v.agentUpdates === "managed" ? "managed" : "manual" },
    };
  }

  function validatePolicyBody(body) {
    const problems = [];
    if (!POLICY_NAME_RE.test(body.name)) {
      problems.push("Name: lower-case letters, digits and '-', up to 32 characters, starting with a letter or digit.");
    }
    if (body.description.length > 500 || CONTROL_CHAR_RE.test(body.description)) {
      problems.push("Description: a single line of at most 500 characters.");
    }
    rangeCheck(body.compat.max_installer_mb, 1, 65536, "Largest installer (MB)", problems);
    rangeCheck(body.agent.heartbeat_seconds, 30, 86400, "Heartbeat interval (seconds)", problems);
    rangeCheck(body.agent.inventory_seconds, 300, 604800, "Inventory interval (seconds)", problems);
    for (const id of body.compat.blocked_applications) {
      if (!isAppId(id)) problems.push(`Blocked applications: “${id}” is not an application ID (e.g. com.example.app).`);
    }
    for (const id of body.compat.allowed_applications || []) {
      if (!isAppId(id)) problems.push(`Allowed applications: “${id}” is not an application ID (e.g. com.example.app).`);
    }
    return problems;
  }

  /** POST /api/v1/enrollment-tokens body: optional fields are left out when empty. */
  function buildTokenBody(v) {
    const body = {
      ttl_hours: toInt(v.ttlHours),
      max_uses: toInt(v.maxUses),
      allow_ephemeral: Boolean(v.allowEphemeral),
    };
    const profile = String(v.profile || "").trim();
    if (profile) body.profile = profile;
    if (v.policyName) body.policy_name = String(v.policyName);
    const description = String(v.description || "").trim();
    if (description) body.description = description;
    const deviceId = String(v.deviceId || "").trim().toLowerCase();
    if (deviceId) body.device_id = deviceId;
    return body;
  }

  function validateTokenBody(body) {
    const problems = [];
    rangeCheck(body.ttl_hours, 1, 720, "Valid for (hours)", problems);
    rangeCheck(body.max_uses, 1, 1000, "Maximum uses", problems);
    if (body.profile !== undefined && !PROFILE_RE.test(body.profile)) {
      problems.push("Profile: a short lower-case identifier such as engineering (letters, digits, '.', '_', '-').");
    }
    if (body.policy_name !== undefined && !POLICY_NAME_RE.test(body.policy_name)) {
      problems.push("Policy: choose a published policy.");
    }
    if (body.device_id !== undefined && !UUID_RE.test(body.device_id)) {
      problems.push("Device ID: a device ID such as 6a2f41a3-c54c-4fc6-9cbb-07f8d7f6a8a1.");
    }
    if (body.description !== undefined && body.description.length > 200) {
      problems.push("Description: at most 200 characters.");
    }
    return problems;
  }

  function parseManifestText(text) {
    const source = String(text || "").trim();
    if (!source) return { error: "Paste a manifest first." };
    let doc;
    try {
      doc = JSON.parse(source);
    } catch (err) {
      return { error: `The manifest is not valid JSON: ${err.message}` };
    }
    if (!doc || typeof doc !== "object" || Array.isArray(doc)) return { error: "The manifest must be a JSON object." };
    return { manifest: doc };
  }

  /** A starting point for a catalog manifest that pins an uploaded installer. */
  function manifestTemplate(artifact) {
    const installer = { type: artifact && artifact.kind === "msi" ? "msi" : "exe" };
    if (artifact && artifact.sha256) installer.sha256 = artifact.sha256;
    if (artifact && artifact.file_name) installer.fileName = artifact.file_name;
    return {
      id: "com.example.application",
      name: "Application name",
      version: "1.0",
      publisher: "Publisher",
      runtime: { type: "wine", wineVersion: "10" },
      architecture: "x86_64",
      launch: "C:\\Program Files\\Publisher\\Application\\application.exe",
      status: "tested",
      installer,
      sandbox: { network: false, display: true },
    };
  }

  function machineLabel(machine) {
    if (machine === "x86_64") return "x86_64 / 64-bit";
    if (machine === "x86") return "x86 / 32-bit";
    return machine ? String(machine) : "Not detected";
  }

  function deviceLabel(device) {
    return device && device.name ? device.name : `Device ${shortId(device && device.device_id)}`;
  }

  const PURE = {
    parseTime, relativeTime, absoluteUtc, badgeTone, badgeClass, humanize, shortId, shortHash, formatBytes,
    formatDuration, queryString, parseRoute, roleAllows, isAppId, buildCommandBody, appActions,
    installBlockReason, describeCommand, summarizeDetail, policyState, tokenState, parseIdList, toInt,
    buildPolicyBody, validatePolicyBody, buildTokenBody, validateTokenBody, parseManifestText, manifestTemplate,
    machineLabel, deviceLabel, COMMAND_TYPES, TOKEN_RE,
  };

  if (typeof module === "object" && module && module.exports) module.exports = PURE;
  if (typeof window === "undefined" || typeof document === "undefined") return;

  // ---------------------------------------------------------------------------
  // Browser state
  // ---------------------------------------------------------------------------
  const state = {
    me: null,            // {actor, role} from GET /api/v1/me
    ticket: 0,           // increases with every render; stale renders are dropped
    rendering: false,
    hold: false,         // the view asked to pause auto-refresh (e.g. older events loaded)
    editing: false,      // a form control in the view was changed
    dialogs: 0,
    timer: null,
    lastUpdated: 0,
    routePath: "",
    routeKey: "",
  };
  const ui = { lastUpload: null, lastVerify: null };   // non-secret view state for this tab
  const openDialogs = new Set();
  let memoryToken = null;                               // only if sessionStorage is unavailable
  let uidCounter = 0;

  const enc = encodeURIComponent;
  const $ = (id) => document.getElementById(id);

  function uid(prefix) {
    uidCounter += 1;
    return `${prefix || "id"}-${uidCounter}`;
  }

  // ---------------------------------------------------------------------------
  // Token storage: sessionStorage only
  // ---------------------------------------------------------------------------
  function getToken() {
    try {
      return window.sessionStorage.getItem(TOKEN_KEY) || memoryToken;
    } catch (err) {
      return memoryToken;
    }
  }

  function setToken(token) {
    try {
      window.sessionStorage.setItem(TOKEN_KEY, token);
      memoryToken = null;
    } catch (err) {
      memoryToken = token;
    }
  }

  function clearToken() {
    memoryToken = null;
    try {
      window.sessionStorage.removeItem(TOKEN_KEY);
    } catch (err) {
      // nothing was stored
    }
  }

  // ---------------------------------------------------------------------------
  // API
  // ---------------------------------------------------------------------------
  class ApiError extends Error {
    constructor(status, code, message, details) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.code = code;
      this.details = Array.isArray(details) ? details.map(String) : [];
    }
  }

  /**
   * The only network access of the dashboard. JSON in and out, the bearer
   * token from sessionStorage, error documents turned into ApiError, and a
   * 401 signs the operator out.
   */
  async function api(method, path, body, options) {
    const opts = options || {};
    if (typeof path !== "string" || !path.startsWith(API_PREFIX)) throw new Error("api(): not a Control Plane API path");
    const headers = { Accept: "application/json" };
    const token = getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
    const init = {
      method, headers, mode: "same-origin", credentials: "same-origin", cache: "no-store",
      redirect: "error", referrerPolicy: "no-referrer",
    };
    if (opts.raw !== undefined) {
      Object.assign(headers, opts.headers || {});
      headers["Content-Type"] = "application/octet-stream";
      init.body = opts.raw;
    } else if (body !== undefined && body !== null) {
      headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(body);
    }
    // Uploads may take long; everything else gives up after REQUEST_TIMEOUT_MS.
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    if (controller) init.signal = controller.signal;
    const timer = controller && opts.raw === undefined
      ? window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS) : null;
    let response;
    let text = "";
    try {
      response = await fetch(path + queryString(opts.query), init);
      text = await response.text().catch(() => "");
    } catch (err) {
      const timedOut = err && err.name === "AbortError";
      throw new ApiError(0, timedOut ? "TIMEOUT" : "NETWORK_ERROR", timedOut
        ? "The Control Plane did not answer in time. Try again."
        : "The Control Plane could not be reached. Check the connection and try again.");
    } finally {
      if (timer) window.clearTimeout(timer);
    }
    let doc = null;
    if (text) {
      try {
        doc = JSON.parse(text);
      } catch (err) {
        doc = null;
      }
    }
    if (response.status === 401) {
      signOut("Your session has ended or the token is no longer accepted. Sign in again.", true);
      throw new ApiError(401, "UNAUTHORIZED", "Sign in again.");
    }
    if (!response.ok) {
      const e = doc && typeof doc === "object" && doc.error && typeof doc.error === "object" ? doc.error : {};
      throw new ApiError(response.status, e.code || `HTTP_${response.status}`,
        e.message || response.statusText || "The request failed.", e.details);
    }
    return doc || {};
  }

  function apiGet(path, query) {
    return api("GET", path, undefined, { query });
  }

  // ---------------------------------------------------------------------------
  // DOM helpers
  // ---------------------------------------------------------------------------
  /**
   * h("a", {href: "#/devices", class: "x"}, "text", child, [more]) creates an
   * element. Strings become text nodes (never markup); event-handler and style
   * attributes are refused, links must be in-page "#" routes. Attach behaviour
   * with listen().
   */
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    let value;
    if (attrs) {
      for (const [name, raw] of Object.entries(attrs)) {
        if (FORBIDDEN_ATTR_RE.test(name)) throw new Error(`h(): the attribute ${name} is not allowed`);
        if (raw === null || raw === undefined || raw === false) continue;
        if ((name === "href" || name === "src" || name === "action") && !SAFE_HREF_RE.test(String(raw))) {
          throw new Error(`h(): ${name} must be an in-page route`);
        }
        if (name === "value") value = String(raw);
        else if (name === "class") el.className = String(raw);
        else el.setAttribute(name, raw === true ? "" : String(raw));
      }
    }
    append(el, children);
    if (value !== undefined) el.value = value;     // after the children: <select> needs its options first
    return el;
  }

  function append(el, children) {
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      if (child instanceof Node) el.appendChild(child);
      else {
        const text = document.createTextNode("");
        text.textContent = String(child);
        el.appendChild(text);
      }
    }
    return el;
  }

  function listen(el, type, handler) {
    el.addEventListener(type, handler);
    return el;
  }

  function button(label, onClick, opts) {
    const o = opts || {};
    const kinds = (o.kind || "").split(" ").filter(Boolean).map((k) => `button--${k}`).join(" ");
    const el = h("button", {
      type: o.type || "button", class: `button ${kinds}`.trim(), disabled: o.disabled, title: o.title,
      "aria-label": o.ariaLabel,
    }, label);
    if (onClick) listen(el, "click", onClick);
    return el;
  }

  function badge(value, label) {
    return h("span", { class: badgeClass(value) }, label !== undefined ? label : humanize(value));
  }

  function mono(text, title) {
    if (text === null || text === undefined || text === "") return "—";
    return h("span", { class: "mono", title }, String(text));
  }

  /** Audit actors: "device:<uuid>" is shortened, the full reference stays in the title. */
  function actorEl(actor) {
    const m = /^device:([0-9a-f-]{36})$/.exec(actor || "");
    return m ? mono(`device:${shortId(m[1])}`, actor) : mono(actor);
  }

  function timeEl(iso, fallback) {
    if (parseTime(iso) === null) return h("span", { class: "muted" }, fallback || "—");
    return h("time", { datetime: iso, title: absoluteUtc(iso) }, relativeTime(iso));
  }

  function cellValue(value) {
    return value === null || value === undefined || value === "" ? "—" : value;
  }

  function facts(pairs) {
    return h("dl", { class: "facts" }, pairs.filter(Boolean).map(([label, value]) =>
      h("div", { class: "fact" }, h("dt", null, label), h("dd", null, cellValue(value)))));
  }

  function dataTable(label, columns, rows, emptyText, rowClass) {
    if (!rows || !rows.length) return h("p", { class: "empty" }, emptyText || "Nothing to show.");
    return h("div", { class: "table-wrap", role: "region", "aria-label": label, tabindex: "0" },
      h("table", { class: "table" },
        h("thead", null, h("tr", null, columns.map((c) => h("th", { scope: "col", class: c.class }, c.label)))),
        h("tbody", null, rows.map((row) => tableRow(columns, row, rowClass)))));
  }

  function tableRow(columns, row, rowClass) {
    return h("tr", { class: rowClass ? rowClass(row) : null },
      columns.map((c) => {
        const value = c.cell(row);
        return h("td", { class: c.class }, c.class === "cell-actions" ? value || "" : cellValue(value));
      }));
  }

  function pageHeader(title, subtitle, ...actions) {
    const acts = actions.filter(Boolean);
    return h("div", { class: "page-header" },
      h("div", { class: "page-heading" }, h("h1", null, title),
        subtitle ? h("p", { class: "page-subtitle" }, subtitle) : null),
      acts.length ? h("div", { class: "page-actions" }, acts) : null);
  }

  function panel(title, opts, ...children) {
    const o = opts || {};
    const headingId = title ? uid("panel") : null;
    const header = title || o.actions
      ? h("div", { class: "panel-header" },
        title ? h("h2", { id: headingId, class: "panel-title" }, title) : null,
        o.actions ? h("div", { class: "panel-actions" }, o.actions) : null)
      : null;
    return h("section", { class: o.wide ? "panel panel--wide" : "panel", id: o.id, "aria-labelledby": headingId },
      header, children);
  }

  function page(...children) {
    return h("div", { class: "page" }, children);
  }

  function alertBox(tone, message, ...extra) {
    return h("div", { class: `alert alert--${tone}`, role: tone === "bad" ? "alert" : null },
      h("p", { class: "alert-title" }, message), extra);
  }

  function errorBox(err) {
    const e = err || {};
    const details = (Array.isArray(e.details) ? e.details : []).filter((d) => String(d) !== e.message);
    return h("div", { class: "alert alert--bad", role: "alert" },
      h("p", { class: "alert-title" }, e.message || String(err)),
      e.code ? h("p", { class: "alert-code mono" }, e.status ? `${e.code} · HTTP ${e.status}` : e.code) : null,
      details.length ? h("ul", { class: "alert-details" }, details.map((d) => h("li", null, String(d)))) : null);
  }

  function empty(text) {
    return h("p", { class: "empty" }, text);
  }

  function chip(label, value, tone) {
    return h("span", { class: `chip chip--${tone || "neutral"}` }, h("strong", null, String(value === undefined ||
      value === null ? 0 : value)), ` ${label}`);
  }

  function field(label, control, hint) {
    if (!control.id) control.id = uid("field");
    let hintNode = null;
    if (hint) {
      hintNode = h("p", { class: "hint", id: uid("hint") }, hint);
      control.setAttribute("aria-describedby", hintNode.id);
    }
    return h("div", { class: "field" }, h("label", { for: control.id }, label), control, hintNode);
  }

  function selectEl(options, selected, attrs) {
    return h("select", Object.assign({ value: selected === undefined || selected === null ? "" : selected }, attrs),
      options.map((o) => h("option", { value: o.value, disabled: o.disabled || null }, o.label)));
  }

  function checkbox(label, checked) {
    const input = h("input", { type: "checkbox", checked: Boolean(checked) });
    return { input, node: h("label", { class: "check" }, input, h("span", null, label)) };
  }

  function checkGroup(legend, options, selected, hint) {
    const chosen = selected || [];
    const boxes = options.map((o) => {
      const input = h("input", { type: "checkbox", value: o.value, checked: chosen.includes(o.value) });
      return { value: o.value, input, node: h("label", { class: "check" }, input, h("span", null, o.label)) };
    });
    const node = h("fieldset", { class: "fieldset" }, h("legend", null, legend),
      h("div", { class: "check-grid" }, boxes.map((b) => b.node)),
      hint ? h("p", { class: "hint" }, hint) : null);
    return { node, values: () => boxes.filter((b) => b.input.checked).map((b) => b.value) };
  }

  function numberInput(value, min, max) {
    return h("input", { type: "number", min: String(min), max: String(max), step: "1", inputmode: "numeric",
      value: value === undefined || value === null ? "" : String(value) });
  }

  function segmented(label, items) {
    return h("nav", { class: "segmented", "aria-label": label },
      items.map((i) => h("a", { href: i.href, class: "segment", "aria-current": i.current ? "page" : null }, i.label)));
  }

  function breadcrumb(...items) {
    const parts = [];
    items.forEach((item, i) => {
      if (i) parts.push(h("span", { class: "breadcrumb-sep", "aria-hidden": "true" }, "/"));
      parts.push(item.href ? h("a", { href: item.href }, item.label) : h("span", { "aria-current": "page" }, item.label));
    });
    return h("nav", { class: "breadcrumb", "aria-label": "Breadcrumb" }, parts);
  }

  function deviceLink(id, names) {
    if (!id) return "—";
    const label = (names && names[id]) || shortId(id);
    return UUID_RE.test(id) ? h("a", { href: `#/devices/${id}`, title: id }, label) : mono(id);
  }

  function policyBadge(device) {
    const s = policyState(device);
    return badge(s, humanize(s));
  }

  function listText(values, none) {
    return Array.isArray(values) && values.length ? values.join(", ") : (none || "None");
  }

  function copyButton(text, label) {
    return button(label || "Copy", async () => {
      try {
        await navigator.clipboard.writeText(text);
        notify("Copied to the clipboard.", "ok");
      } catch (err) {
        notify("Copying is not available here; select the text and copy it manually.", "warn");
      }
    }, { kind: "small" });
  }

  // ---------------------------------------------------------------------------
  // Notifications and dialogs
  // ---------------------------------------------------------------------------
  function notify(message, tone) {
    const box = $("toasts");
    const close = h("button", { type: "button", class: "toast-close", "aria-label": "Dismiss" }, "×");
    const toast = h("div", { class: `toast toast--${tone || "info"}` }, h("p", null, message), close);
    const remove = () => toast.remove();
    listen(close, "click", remove);
    box.appendChild(toast);
    window.setTimeout(remove, tone === "bad" ? 12000 : 6000);
    while (box.children.length > 4) box.firstChild.remove();
  }

  function notifyError(err) {
    if (err && err.status === 401) return;
    const details = err && Array.isArray(err.details) && err.details.length ? ` (${err.details.slice(0, 3).join("; ")})` : "";
    const code = err && err.code ? ` [${err.code}]` : "";
    notify(`${(err && err.message) || String(err)}${details}${code}`, "bad");
  }

  function modal(build) {
    return new Promise((resolve) => {
      const titleId = uid("dialog-title");
      const dialog = h("dialog", { class: "dialog", "aria-labelledby": titleId });
      const previous = document.activeElement;
      let done = false;
      function finish(value) {
        if (done) return;
        done = true;
        openDialogs.delete(finish);
        state.dialogs = openDialogs.size;
        if (built.onClose) built.onClose();
        if (dialog.open) dialog.close();
        dialog.remove();
        if (previous && previous.isConnected && typeof previous.focus === "function") previous.focus();
        resolve(value);
      }
      const built = build(finish, titleId);
      append(dialog, [built.content]);
      listen(dialog, "cancel", (event) => {
        event.preventDefault();
        if (!built.keepOnEscape) finish(false);
      });
      listen(dialog, "close", () => finish(false));
      openDialogs.add(finish);
      state.dialogs = openDialogs.size;
      document.body.appendChild(dialog);
      dialog.showModal();
      if (built.focus) built.focus.focus();
    });
  }

  /**
   * opts: {title, message, facts, confirmLabel, cancelLabel, danger,
   *        requireText: {accept: [strings], label}}
   */
  function confirmDialog(opts) {
    return modal((finish, titleId) => {
      const confirmBtn = h("button", { type: "submit", class: `button ${opts.danger ? "button--danger" : "button--primary"}` },
        opts.confirmLabel || "Confirm");
      const cancelBtn = button(opts.cancelLabel || "Cancel", () => finish(false));
      const accept = opts.requireText ? opts.requireText.accept.filter(Boolean) : null;
      let input = null;
      let inputField = null;
      if (accept) {
        input = h("input", { type: "text", autocomplete: "off", spellcheck: "false", autocapitalize: "off" });
        inputField = field(opts.requireText.label, input);
        confirmBtn.disabled = true;
        listen(input, "input", () => {
          confirmBtn.disabled = !accept.includes(input.value.trim());
        });
      }
      const form = h("form", { class: "dialog-form", novalidate: true },
        h("h2", { id: titleId, class: "dialog-title" }, opts.title),
        h("p", { class: "dialog-message" }, opts.message),
        opts.facts ? facts(opts.facts) : null,
        inputField,
        h("div", { class: "dialog-actions" }, cancelBtn, confirmBtn));
      listen(form, "submit", (event) => {
        event.preventDefault();
        if (accept && !accept.includes(input.value.trim())) return;
        finish(true);
      });
      return { content: form, focus: input || (opts.danger ? cancelBtn : confirmBtn) };
    });
  }

  /** Shows a one-time secret with a copy button. Nothing is stored; the field is cleared on close. */
  function secretDialog(opts) {
    return modal((finish, titleId) => {
      const secretInput = h("input", {
        type: "text", readonly: true, class: "secret-input mono", value: opts.secret, spellcheck: "false",
        autocomplete: "off", "aria-label": opts.secretLabel || "Token",
      });
      const status = h("p", { class: "hint", role: "status" });
      const copy = button("Copy", async () => {
        secretInput.select();
        try {
          await navigator.clipboard.writeText(secretInput.value);
          status.textContent = "Copied to the clipboard.";
        } catch (err) {
          status.textContent = "Copying is not available here. The token is selected: press Ctrl+C to copy it.";
        }
      }, { kind: "primary" });
      const done = button("I have stored it", () => finish(true));
      const content = h("div", { class: "dialog-form" },
        h("h2", { id: titleId, class: "dialog-title" }, opts.title),
        alertBox("warn", opts.warning),
        h("div", { class: "secret-row" }, secretInput, copy),
        status,
        opts.facts ? facts(opts.facts) : null,
        h("div", { class: "dialog-actions" }, done));
      return {
        content, focus: copy, keepOnEscape: true,
        onClose: () => {
          secretInput.value = "";
        },
      };
    });
  }

  function closeAllDialogs() {
    for (const finish of Array.from(openDialogs)) finish(false);
  }

  // ---------------------------------------------------------------------------
  // Session
  // ---------------------------------------------------------------------------
  function showSignIn(message, isError) {
    $("signin").hidden = false;
    const box = $("signin-error");
    box.textContent = message || "";
    box.hidden = !message;
    box.classList.toggle("form-error--info", Boolean(message) && !isError);
    document.title = "Sign in · Boswas Control Plane";
    $("signin-token").focus();
  }

  function signOut(message, isError) {
    clearToken();
    state.me = null;
    state.ticket += 1;
    state.routePath = "";
    state.routeKey = "";
    state.lastUpdated = 0;
    stopTimer();
    closeAllDialogs();
    closeNav();
    ui.lastUpload = null;
    ui.lastVerify = null;
    $("view").replaceChildren();
    $("refresh-status").replaceChildren();
    $("layout").hidden = true;
    $("session").hidden = true;
    $("nav-toggle").hidden = true;
    $("session-actor").textContent = "";
    showSignIn(message, isError);
  }

  async function startSession() {
    const me = await apiGet("/api/v1/me");
    if (!me || typeof me.actor !== "string" || !Object.prototype.hasOwnProperty.call(ROLE_RANK, me.role)) {
      throw new ApiError(0, "UNEXPECTED_ANSWER", "The Control Plane sent an unexpected answer.");
    }
    state.me = me;
    $("signin").hidden = true;
    $("signin-error").hidden = true;
    $("layout").hidden = false;
    $("session").hidden = false;
    $("nav-toggle").hidden = false;
    $("session-actor").textContent = me.actor;
    const role = $("session-role");
    role.textContent = humanize(me.role);
    role.className = badgeClass(me.role);
    for (const el of document.querySelectorAll("[data-role]")) {
      el.hidden = !roleAllows(me.role, el.getAttribute("data-role"));
    }
    startTimer();
    if (!location.hash.startsWith("#/")) history.replaceState(null, "", "#/overview");
    await render();
  }

  async function onSignIn(event) {
    event.preventDefault();
    const input = $("signin-token");
    const submit = $("signin-submit");
    const token = input.value.trim();
    if (!TOKEN_RE.test(token)) {
      showSignIn("This is not an operator API token. Tokens look like bcp_<12 hex characters>_<43 characters>.", true);
      return;
    }
    input.value = "";
    setToken(token);
    submit.disabled = true;
    try {
      await startSession();
    } catch (err) {
      if (err && err.status === 401) {
        showSignIn("The token was not accepted. It may be mistyped, disabled or replaced.", true);
      } else {
        clearToken();
        showSignIn((err && err.message) || "Sign-in failed.", true);
      }
    } finally {
      submit.disabled = false;
    }
  }

  function toggleNav() {
    const layout = $("layout");
    const open = !layout.classList.contains("nav-open");
    layout.classList.toggle("nav-open", open);
    $("nav-toggle").setAttribute("aria-expanded", String(open));
  }

  function closeNav() {
    $("layout").classList.remove("nav-open");
    $("nav-toggle").setAttribute("aria-expanded", "false");
  }

  // ---------------------------------------------------------------------------
  // Router and auto-refresh
  // ---------------------------------------------------------------------------
  const ROUTES = [
    { pattern: /^overview$/, view: viewOverview, nav: "overview" },
    { pattern: /^devices$/, view: viewDevices, nav: "devices" },
    { pattern: /^devices\/([0-9a-f-]{36})$/, view: viewDevice, nav: "devices" },
    { pattern: /^applications$/, view: viewApplications, nav: "applications" },
    { pattern: /^policies$/, view: viewPolicies, nav: "policies" },
    { pattern: /^policies\/([a-z0-9][a-z0-9-]{0,31})$/, view: viewPolicy, nav: "policies" },
    { pattern: /^commands$/, view: viewCommands, nav: "commands" },
    { pattern: /^events$/, view: viewEvents, nav: "events" },
    { pattern: /^enrollment$/, view: viewEnrollment, nav: "enrollment", role: "admin" },
    { pattern: /^operators$/, view: viewOperators, nav: "operators", role: "admin" },
  ];

  function highlightNav(nav) {
    for (const link of document.querySelectorAll("[data-nav]")) {
      if (link.getAttribute("data-nav") === nav) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
  }

  function holdRefresh() {
    state.hold = true;
    updateRefreshStatus();
  }

  /**
   * Render the current route. options.quiet: a refresh in place (no loading
   * placeholder, no scrolling); options.keepState: keep the paused/editing
   * flags (used by the timer).
   */
  async function render(options) {
    const opts = options || {};
    if (!state.me) return;
    const { path, query } = parseRoute(location.hash);
    const route = ROUTES.find((r) => r.pattern.test(path));
    const key = path + queryString(query);
    const ticket = ++state.ticket;
    const view = $("view");
    const newPage = !opts.quiet && path !== state.routePath;
    if (!opts.keepState) {
      state.hold = false;
      state.editing = false;
    }
    highlightNav(route ? route.nav : "");
    if (newPage) view.replaceChildren(h("p", { class: "loading" }, "Loading…"));
    view.setAttribute("aria-busy", "true");
    state.rendering = true;
    let node;
    try {
      if (!route) node = notFoundPage();
      else if (route.role && !roleAllows(state.me.role, route.role)) node = forbiddenPage(route.role);
      else node = await route.view({ params: path.match(route.pattern).slice(1), query, hold: holdRefresh });
    } catch (err) {
      node = err && err.status === 401 ? null : loadErrorPage(err);
    } finally {
      if (ticket === state.ticket) state.rendering = false;
    }
    if (ticket !== state.ticket || !state.me || !node) return;
    view.replaceChildren(node);
    view.setAttribute("aria-busy", "false");
    state.lastUpdated = Date.now();
    if (!opts.quiet && key !== state.routeKey) {
      const heading = view.querySelector("h1");
      document.title = `${heading ? heading.textContent : "Boswas"} · Boswas Control Plane`;
      if (newPage) {
        window.scrollTo(0, 0);
        $("main").focus({ preventScroll: true });
      }
    }
    state.routePath = path;
    state.routeKey = key;
    updateRefreshStatus();
  }

  function refreshView() {
    return render({ quiet: true });
  }

  function focusInForm() {
    const active = document.activeElement;
    return Boolean(active && $("view").contains(active) && /^(INPUT|SELECT|TEXTAREA)$/.test(active.tagName));
  }

  function canAutoRefresh() {
    return Boolean(state.me) && !document.hidden && !state.rendering && !state.hold && !state.editing &&
      state.dialogs === 0 && !focusInForm();
  }

  function tick() {
    if (canAutoRefresh()) render({ quiet: true, keepState: true });
    else updateRefreshStatus();
  }

  function startTimer() {
    stopTimer();
    state.timer = window.setInterval(tick, REFRESH_MS);
  }

  function stopTimer() {
    if (state.timer) window.clearInterval(state.timer);
    state.timer = null;
  }

  function onVisibilityChange() {
    if (!document.hidden && state.me && Date.now() - state.lastUpdated >= REFRESH_MS) tick();
  }

  function markEditing(event) {
    if (event.target && event.target.closest && event.target.closest("[data-no-hold]")) return;
    if (!state.editing) {
      state.editing = true;
      updateRefreshStatus();
    }
  }

  function updateRefreshStatus() {
    const box = $("refresh-status");
    if (!state.me || !state.lastUpdated) {
      box.replaceChildren();
      return;
    }
    const when = `${new Date(state.lastUpdated).toISOString().slice(11, 19)} UTC`;
    const paused = state.hold || state.editing;
    const parts = [h("span", null, paused
      ? `Updated ${when} · auto-refresh is paused while you work on this page`
      : `Updated ${when} · refreshes every 15 s`)];
    if (paused) parts.push(button("Refresh now", () => render(), { kind: "link" }));
    box.replaceChildren(...parts);
  }

  function notFoundPage() {
    return page(pageHeader("Page not found", "There is no such page in the dashboard."),
      h("p", null, h("a", { href: "#/overview" }, "Go to the overview")));
  }

  function forbiddenPage(role) {
    return page(pageHeader("Not available", `This page needs the ${role} role.`),
      h("p", null, h("a", { href: "#/overview" }, "Go to the overview")));
  }

  function loadErrorPage(err) {
    return page(pageHeader(err && err.status === 404 ? "Not found" : "Could not load this page"),
      errorBox(err),
      h("div", { class: "button-row" }, button("Try again", () => render(), { kind: "primary" })));
  }

  /** Disable the button, run the work, refresh the view; errors go to the slot or a notification. */
  async function runAction(btn, work, opts) {
    const o = opts || {};
    if (btn) btn.disabled = true;
    if (o.errorSlot) o.errorSlot.replaceChildren();
    try {
      await work();
      await refreshView();
      return true;
    } catch (err) {
      if (!(err && err.status === 401)) {
        if (o.errorSlot && o.errorSlot.isConnected) o.errorSlot.replaceChildren(errorBox(err));
        else notifyError(err);
      }
      return false;
    } finally {
      if (btn && btn.isConnected) btn.disabled = false;
    }
  }

  function newIdempotencyKey() {
    const c = window.crypto;
    if (c && typeof c.randomUUID === "function") return `dashboard:${c.randomUUID()}`;
    const bytes = new Uint8Array(16);
    c.getRandomValues(bytes);
    return `dashboard:${Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("")}`;
  }

  // ---------------------------------------------------------------------------
  // Shared table columns
  // ---------------------------------------------------------------------------
  function eventColumns(opts) {
    const o = opts || {};
    const cols = [];
    if (o.id) cols.push({ label: "#", class: "num", cell: (e) => String(e.id) });
    cols.push(
      { label: "When", cell: (e) => timeEl(e.occurred_at) },
      { label: "Event", cell: (e) => badge(e.type) },
      { label: "Source", cell: (e) => humanize(e.source) },
      { label: "Actor", cell: (e) => actorEl(e.actor) });
    if (o.device) cols.push({ label: "Device", cell: (e) => deviceLink(e.device_id, o.names) });
    cols.push({ label: "Application", cell: (e) => mono(e.application_id) });
    if (o.command) cols.push({ label: "Command", cell: (e) => (e.command_id ? mono(shortId(e.command_id), e.command_id) : "—") });
    cols.push({ label: "Detail", class: "cell-detail", cell: (e) => summarizeDetail(e.detail) });
    return cols;
  }

  function commandTarget(cmd) {
    const p = cmd.payload || {};
    if (p.application_id) return h("span", null, mono(p.application_id), p.version ? ` ${p.version}` : "");
    if (p.policy_version) return `Policy ${p.policy_version}`;
    if (p.version) return `Agent ${p.version}`;
    return "—";
  }

  function commandColumns(opts) {
    const o = opts || {};
    const cols = [{ label: "Created", cell: (c) => timeEl(c.created_at) }];
    if (o.showDevice) cols.push({ label: "Device", cell: (c) => deviceLink(c.device_id, o.names) });
    cols.push(
      { label: "Command", cell: (c) => humanize(c.type) },
      { label: "Target", cell: (c) => commandTarget(c) },
      { label: "Status", cell: (c) => badge(c.status) },
      { label: "Created by", cell: (c) => mono(c.created_by) },
      { label: "Last change", cell: (c) => timeEl(c.finished_at || c.started_at || c.acknowledged_at || c.sent_at) },
      { label: "Expires", cell: (c) => (COMMAND_STATUSES.indexOf(c.status) < 4 ? timeEl(c.expires_at) : "—") },
      {
        label: "Error", cell: (c) => (c.error
          ? h("span", null, h("span", { class: "mono" }, c.error.code || "ERROR"), c.error.message ? `: ${c.error.message}` : "")
          : "—"),
      });
    if (o.cancel) {
      cols.push({
        label: "Actions", class: "cell-actions",
        cell: (c) => (c.status === "QUEUED"
          ? button("Cancel", (event) => cancelCommand(c, event.currentTarget, o.names), { kind: "small" })
          : ""),
      });
    }
    return cols;
  }

  async function cancelCommand(cmd, btn, names) {
    const deviceName = (names && names[cmd.device_id]) || shortId(cmd.device_id);
    const app = cmd.payload && cmd.payload.application_id ? ` of ${cmd.payload.application_id}` : "";
    const ok = await confirmDialog({
      title: "Cancel this command?",
      message: `Cancel ${humanize(cmd.type)}${app} for “${deviceName}”. Only commands that were not yet delivered to the device can be cancelled.`,
      facts: [["Command ID", cmd.command_id], ["Device", `${deviceName} · ${cmd.device_id}`]],
      confirmLabel: "Cancel command", cancelLabel: "Keep it", danger: true,
    });
    if (!ok) return;
    await runAction(btn, async () => {
      await api("POST", `/api/v1/devices/${enc(cmd.device_id)}/commands/${enc(cmd.command_id)}/cancel`);
      notify("Command cancelled.", "ok");
    });
  }

  function deviceNames(devices) {
    const names = {};
    for (const d of devices || []) names[d.device_id] = deviceLabel(d);
    return names;
  }

  // ---------------------------------------------------------------------------
  // Overview
  // ---------------------------------------------------------------------------
  function statCard(label, value, sub, href, tone) {
    const body = [h("span", { class: "card-label" }, label), h("span", { class: "card-value" }, String(value)),
      sub ? h("span", { class: "card-sub" }, sub) : null];
    const cls = `card card--${tone || "neutral"}`;
    return href ? h("a", { class: cls, href }, body) : h("div", { class: cls }, body);
  }

  async function viewOverview() {
    const s = await apiGet("/api/v1/summary");
    const d = s.devices || {};
    const a = s.applications || {};
    const c = s.commands || {};
    const p = s.policies || {};
    const outdated = Number(p.devices_outdated || 0);
    const unreported = Number(p.devices_unreported || 0);
    const byStatus = c.by_status || {};
    const statuses = COMMAND_STATUSES.filter((st) => byStatus[st]);
    const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
    return page(
      pageHeader("Overview", h("span", null, "Fleet summary · generated ", timeEl(s.generated_at))),
      h("div", { class: "cards" },
        statCard("Devices", d.active || 0, `${d.total || 0} enrolled · ${d.retired || 0} retired`, "#/devices"),
        statCard("Online devices", d.online || 0, `of ${plural(d.active || 0, "active device", "active devices")}`,
          "#/devices?filter=online", d.online ? "ok" : "neutral"),
        statCard("Offline devices", d.offline || 0, `${d.never_connected || 0} never connected`,
          "#/devices?filter=offline", d.offline ? "warn" : "neutral"),
        statCard("Applications", a.catalog || 0, `${a.supported || 0} supported (x86_64 / 64-bit)`, "#/applications"),
        statCard("Running applications", a.running || 0, "Windows applications running now", null,
          a.running ? "info" : "neutral"),
        statCard("Pending commands", c.pending || 0, "queued, sent or in progress", "#/commands",
          c.pending ? "info" : "neutral"),
        statCard("Failed commands (24 h)", c.failed_24h || 0, "failed or expired", "#/commands?status=FAILED",
          c.failed_24h ? "bad" : "ok"),
        statCard("Policy status", outdated ? outdated : "Current",
          (outdated ? `${outdated === 1 ? "device" : "devices"} on an outdated policy`
            : `every reporting device on its latest policy · ${plural(p.count || 0, "policy", "policies")}`)
            + (unreported ? ` · ${unreported} not reported yet` : ""),
          "#/policies", outdated ? "warn" : "ok")),
      panel("Commands by status", null, statuses.length
        ? h("div", { class: "chips" }, statuses.map((st) => chip(humanize(st), byStatus[st], badgeTone(st))))
        : empty("No commands yet.")),
      panel("Recent security events", {
        wide: true,
        actions: [h("a", { href: "#/events?type=SECURITY_EVENT", class: "panel-link" }, "Open the audit trail")],
      }, dataTable("Recent security events", eventColumns({ device: true }), s.security_events || [],
        "No security events recorded.")));
  }

  // ---------------------------------------------------------------------------
  // Devices
  // ---------------------------------------------------------------------------
  const DEVICE_FILTERS = [
    { key: "all", label: "All", query: {} },
    { key: "online", label: "Online", query: { status: "active", connection: "online" } },
    { key: "offline", label: "Offline", query: { status: "active", connection: "offline" } },
    { key: "never", label: "Never connected", query: { status: "active", connection: "never" } },
    { key: "retired", label: "Retired", query: { status: "retired" } },
  ];

  function osLabel(os) {
    if (!os) return "—";
    const title = [os.name, os.version, os.build_id ? `build ${os.build_id}` : null,
      os.debian_version ? `Debian ${os.debian_version}` : null, os.kernel ? `kernel ${os.kernel}` : null]
      .filter(Boolean).join(" · ");
    return h("span", { title }, os.version_id || os.version || "—");
  }

  async function viewDevices(ctx) {
    const filter = DEVICE_FILTERS.find((f) => f.key === ctx.query.filter) || DEVICE_FILTERS[0];
    const data = await apiGet("/api/v1/devices", filter.query);
    const devices = data.devices || [];
    const columns = [
      {
        label: "Device", cell: (d) => h("div", null,
          h("a", { href: `#/devices/${d.device_id}`, class: "strong-link" }, d.name || "Unnamed device"),
          h("div", { class: "cell-sub mono" }, d.device_id)),
      },
      { label: "Connection", cell: (d) => (d.status === "retired" ? badge("retired") : badge(d.connection)) },
      { label: "State", cell: (d) => (d.device_state ? badge(d.device_state) : "—") },
      { label: "OS version", cell: (d) => osLabel(d.os) },
      { label: "Agent", cell: (d) => d.agent_version },
      { label: "Architecture", cell: (d) => d.architecture },
      { label: "Last heartbeat", cell: (d) => timeEl(d.last_heartbeat_at, "never") },
      { label: "Policy", cell: (d) => h("div", { class: "cell-stack" }, h("span", null, d.policy_name || "—"), policyBadge(d)) },
    ];
    const count = `${devices.length} ${devices.length === 1 ? "device" : "devices"}`;
    return page(
      pageHeader("Devices", filter.key === "all" ? count : `${count} · ${filter.label.toLowerCase()}`),
      segmented("Filter devices", DEVICE_FILTERS.map((f) => ({
        label: f.label, href: f.key === "all" ? "#/devices" : `#/devices?filter=${f.key}`, current: f === filter,
      }))),
      panel(null, { wide: true }, dataTable("Devices", columns, devices,
        filter.key === "all" ? "No devices yet. Create an enrollment token to enroll a Boswas OS device."
          : "No devices match this filter.")));
  }

  // ---------------------------------------------------------------------------
  // Device detail
  // ---------------------------------------------------------------------------
  async function viewDevice(ctx) {
    const id = ctx.params[0];
    const canOperate = roleAllows(state.me.role, "operator");
    const isAdmin = roleAllows(state.me.role, "admin");
    const [device, status, inventory, apps, commands, events, catalog, policies] = await Promise.all([
      apiGet(`/api/v1/devices/${enc(id)}`),
      apiGet(`/api/v1/devices/${enc(id)}/status`),
      apiGet(`/api/v1/devices/${enc(id)}/inventory`),
      apiGet(`/api/v1/devices/${enc(id)}/applications`),
      apiGet(`/api/v1/devices/${enc(id)}/commands`, { limit: 50 }),
      apiGet(`/api/v1/devices/${enc(id)}/events`, { limit: 50 }),
      canOperate ? apiGet("/api/v1/applications") : Promise.resolve(null),
      isAdmin ? apiGet("/api/v1/policies") : Promise.resolve(null),
    ]);
    const active = device.status === "active";
    const name = deviceLabel(device);
    const actions = [];
    if (canOperate && active) {
      actions.push(button("Refresh inventory",
        (event) => sendCommand(device, "REFRESH_INVENTORY", null, event.currentTarget)));
      actions.push(button("Apply policy",
        (event) => sendCommand(device, "APPLY_POLICY", null, event.currentTarget, device.policy_version)));
    }
    return page(
      breadcrumb({ label: "Devices", href: "#/devices" }, { label: name }),
      pageHeader(name, h("span", { class: "mono" }, device.device_id), ...actions),
      h("div", { class: "badges" },
        active ? badge(device.connection) : badge("retired"),
        device.device_state ? badge(device.device_state) : null,
        policyBadge(device),
        device.ephemeral ? badge("ephemeral", "Live session") : null),
      active ? null : alertBox("warn", "This device is retired. Its certificate is no longer accepted, so it cannot connect or be managed."),
      h("div", { class: "grid" },
        identityPanel(device),
        systemPanel(device, status),
        policyPanel(device),
        compliancePanel(status)),
      applicationsPanel(device, apps.applications || [], catalog ? catalog.applications || [] : null, canOperate && active),
      inventoryPanel(device, inventory),
      panel("Commands", { wide: true }, dataTable("Commands for this device",
        commandColumns({ cancel: canOperate, names: { [device.device_id]: name } }), commands.commands || [],
        "No commands for this device yet.")),
      panel("Events", {
        wide: true,
        actions: [h("a", { href: `#/events?device=${device.device_id}`, class: "panel-link" }, "Open in the audit trail")],
      }, dataTable("Events for this device", eventColumns({ command: true }), events.events || [],
        "No events for this device yet.")),
      isAdmin ? adminDevicePanel(device, policies ? policies.policies || [] : []) : null);
  }

  function certificateExpiry(iso) {
    const t = parseTime(iso);
    if (t === null) return "—";
    const days = (t - Date.now()) / 86400000;
    let flag = null;
    if (days <= 0) flag = badge("expired", "Expired");
    else if (days < 30) flag = badge("expiring", "Expires soon");
    return h("span", { class: "cell-stack" }, timeEl(iso), flag);
  }

  function identityPanel(device) {
    return panel("Identity", null, facts([
      ["Device ID", h("span", { class: "mono wrap-anywhere" }, device.device_id)],
      ["Name", device.name],
      ["Profile", device.profile],
      ["Status", badge(device.status)],
      ["Enrolled", timeEl(device.enrolled_at)],
      ["Identity", device.ephemeral ? "Live session (disappears at shutdown)" : "Installed system"],
      ["Certificate fingerprint", h("span", { class: "mono wrap-anywhere" }, device.certificate_fingerprint || "—")],
      ["Certificate expires", certificateExpiry(device.certificate_expires_at)],
    ]));
  }

  function systemPanel(device, status) {
    const os = device.os || {};
    const report = status && status.status_report ? status.status_report.document || {} : null;
    const update = report && report.update ? report.update : null;
    return panel("System", null, facts([
      ["Operating system", [os.name, os.version].filter(Boolean).join(" ")],
      ["Version ID", os.version_id],
      ["Build", os.build_id],
      ["Debian base", os.debian_version],
      ["Kernel", os.kernel],
      ["Architecture", device.architecture],
      ["Agent version", device.agent_version],
      ["Connection", device.status === "retired" ? badge("retired") : badge(device.connection)],
      ["Device state", device.device_state ? badge(device.device_state) : null],
      ["Last heartbeat", timeEl(device.last_heartbeat_at, "never")],
      ["Results waiting on the device", device.pending_results],
      report ? ["Uptime", formatDuration(report.uptime_seconds)] : null,
      update ? ["OS updates", `${humanize(update.state)}${update.available_version ? ` · ${update.available_version}` : ""} (${update.channel})`] : null,
    ]));
  }

  function policyPanel(device) {
    const s = policyState(device);
    return panel("Policy", null,
      facts([
        ["Assigned policy", device.policy_name
          ? h("a", { href: `#/policies/${enc(device.policy_name)}` }, device.policy_name) : null],
        ["Latest version", device.policy_version || "No published version"],
        ["Reported by the device", device.reported_policy_version || "Not reported yet"],
        ["Status", policyBadge(device)],
      ]),
      s === "outdated" || s === "unreported"
        ? h("p", { class: "hint" }, "Devices fetch the latest version of their assigned policy after a heartbeat. Apply policy asks the device to do it now.")
        : null);
  }

  function compliancePanel(status) {
    const comp = status ? status.compliance : null;
    if (!comp || !comp.document) return panel("Compliance", null, empty("No compliance report received yet."));
    const doc = comp.document;
    const sum = doc.summary || {};
    return panel("Compliance", null,
      h("div", { class: "compliance-head" }, badge(sum.state),
        h("div", { class: "chips" }, chip("pass", sum.pass, "ok"), chip("warn", sum.warn, "warn"),
          chip("fail", sum.fail, "bad"), chip("unknown", sum.unknown, "neutral"))),
      facts([
        ["Assessed", timeEl(doc.assessed_at)],
        ["Received", timeEl(comp.received_at)],
        ["Policy version", doc.policy_version],
        ["Basis", doc.basis],
      ]),
      dataTable("Compliance checks", [
        { label: "Check", cell: (c) => mono(c.id) },
        { label: "Result", cell: (c) => badge(c.status) },
        { label: "Scored", cell: (c) => (c.scored ? "Yes" : "No (informational)") },
      ], doc.checks || [], "No checks reported."));
  }

  async function sendCommand(device, type, app, btn, version) {
    const deviceName = deviceLabel(device);
    const appName = app ? app.name || app.id : null;
    let body;
    try {
      body = buildCommandBody(type, { applicationId: app ? app.id : undefined, idempotencyKey: newIdempotencyKey() });
    } catch (err) {
      notify(err.message, "bad");
      return;
    }
    const ok = await confirmDialog({
      title: appName ? `${COMMAND_LABELS[type]} ${appName}?` : `${COMMAND_LABELS[type]}?`,
      message: describeCommand(type, deviceName, appName, (app && app.version) || version),
      facts: [
        ["Device", `${deviceName} · ${device.device_id}`],
        app ? ["Application", app.id] : null,
        ["Command", type],
      ],
      confirmLabel: COMMAND_LABELS[type],
      danger: type === "REMOVE_APPLICATION" || type === "STOP_APPLICATION",
    });
    if (!ok) return;
    await runAction(btn, async () => {
      const result = await api("POST", `/api/v1/devices/${enc(device.device_id)}/commands`, body);
      if (result.duplicate) notify(`${humanize(type)} is already pending on ${deviceName}; no new command was created.`, "info");
      else notify(`${humanize(type)} queued for ${deviceName}.`, "ok");
    });
  }

  function applicationsPanel(device, apps, catalog, canAct) {
    const byId = new Map((catalog || []).map((a) => [a.app_id, a]));
    const columns = [
      {
        label: "Application", cell: (a) => h("div", null, byId.has(a.id) ? byId.get(a.id).name : a.id,
          h("div", { class: "cell-sub mono" }, a.id)),
      },
      { label: "Version", cell: (a) => a.version },
      {
        label: "Catalog", cell: (a) => (a.catalog
          ? h("span", { class: "cell-stack" }, h("span", null, a.catalog.version),
            a.update_available ? badge("update", "Update available") : null)
          : h("span", { class: "muted" }, "Not in the catalog")),
      },
      { label: "State", cell: (a) => (a.app_state ? badge(a.app_state) : "—") },
      { label: "Architecture", cell: (a) => (a.architecture === "x86" ? badge("UNSUPPORTED", "x86 / 32-bit") : a.architecture) },
      { label: "Installs", class: "num", cell: (a) => (a.installations === undefined ? "—" : String(a.installations)) },
      { label: "Running", class: "num", cell: (a) => String(a.running || 0) },
    ];
    if (canAct) {
      columns.push({
        label: "Actions", class: "cell-actions", cell: (a) => {
          const entry = byId.get(a.id);
          return h("div", { class: "button-row button-row--tight" }, appActions(a).map((type) => button(
            COMMAND_LABELS[type],
            (event) => sendCommand(device, type, {
              id: a.id, name: entry ? entry.name : null,
              version: type === "UPDATE_APPLICATION" && a.catalog ? a.catalog.version : null,
            }, event.currentTarget),
            { kind: type === "REMOVE_APPLICATION" ? "small danger-ghost" : "small" })));
        },
      });
    }
    const installed = new Set(apps.map((a) => a.id));
    return panel("Windows applications", { wide: true },
      dataTable("Windows applications on this device", columns, apps,
        "No Windows applications reported. The list comes from the device inventory."),
      canAct && catalog ? installControl(device, catalog, installed) : null);
  }

  function installControl(device, catalog, installed) {
    const installable = catalog.filter((a) => a.installable);
    const blocked = catalog.filter((a) => !a.installable);
    const select = h("select", null,
      h("option", { value: "" }, installable.length ? "Choose a catalog application…" : "No installable applications in the catalog"),
      installable.map((a) => h("option", { value: a.app_id },
        `${a.name} ${a.version} (${a.app_id})${installed.has(a.app_id) ? " — installed" : ""}`)),
      blocked.map((a) => h("option", { value: a.app_id, disabled: true },
        `${a.name} ${a.version} — ${installBlockReason(a)}`)));
    const go = button("Install…", (event) => {
      const app = installable.find((a) => a.app_id === select.value);
      if (!app) {
        notify("Choose an application to install.", "warn");
        select.focus();
        return;
      }
      sendCommand(device, "INSTALL_APPLICATION", { id: app.app_id, name: app.name, version: app.version },
        event.currentTarget);
    }, { kind: "primary" });
    return h("div", { class: "subsection" },
      h("h3", null, "Install application"),
      h("div", { class: "form-row" },
        field("Catalog application", select, "Only 64-bit (x86_64) catalog applications with a pinned installer can be installed."),
        h("div", { class: "form-row-action" }, go)),
      blocked.length ? h("div", { class: "not-installable" },
        h("p", { class: "muted" }, "Listed in the catalog but not installable:"),
        h("ul", null, blocked.map((a) => h("li", null, h("span", { class: "muted-strong" }, `${a.name} ${a.version}`),
          " ", mono(a.app_id), ` — ${installBlockReason(a)}`)))) : null);
  }

  function storageFacts(storage) {
    if (!storage || storage.root_total_gib === null || storage.root_total_gib === undefined) {
      return empty("No storage information.");
    }
    const total = Number(storage.root_total_gib);
    const free = Number(storage.root_free_gib);
    const used = Number.isFinite(free) ? Math.max(0, total - free) : null;
    return h("div", null,
      facts([
        ["Root file system", `${total} GiB`],
        ["Free", Number.isFinite(free) ? `${free} GiB` : null],
      ]),
      used !== null && total > 0
        ? h("meter", { class: "meter", min: "0", max: String(total), value: String(used), low: String(total * 0.75),
          high: String(total * 0.9), optimum: "0", "aria-label": `Used: ${used.toFixed(1)} of ${total} GiB` })
        : null);
  }

  function inventoryPanel(device, inventory) {
    const doc = inventory ? inventory.document : null;
    if (!doc) {
      return panel("Inventory", { wide: true },
        empty("No inventory received yet. Refresh inventory asks the device to send one."));
    }
    const hw = doc.hardware || null;
    const enrolled = device.hardware || {};
    const src = hw || enrolled;
    const compat = doc.compatibility || {};
    let layer;
    if (compat.available === false) layer = badge("unavailable", "Not available");
    else layer = compat.healthy ? badge("healthy", "Healthy") : badge("unhealthy", "Needs attention");
    return panel("Inventory", { wide: true },
      facts([
        ["Received", timeEl(inventory.received_at)],
        ["Collected", timeEl(doc.collected_at)],
        ["Revision", doc.revision],
        ["Detail level", humanize(doc.policy)],
      ]),
      h("div", { class: "grid grid--3" },
        h("div", { class: "subsection" }, h("h3", null, "Hardware"), facts([
          ["Vendor", src.vendor],
          ["Model", src.model],
          ["CPU", hw ? hw.cpu_model : enrolled.cpu],
          ["CPU threads", hw ? hw.cpu_count : null],
          ["Memory", src.memory_gib !== null && src.memory_gib !== undefined ? `${src.memory_gib} GiB` : null],
          ["Firmware", src.firmware_version],
          ["Boot mode", src.boot_mode],
          ["TPM", src.tpm_version],
        ])),
        h("div", { class: "subsection" }, h("h3", null, "Storage"), storageFacts(doc.storage)),
        h("div", { class: "subsection" }, h("h3", null, "Windows compatibility"), facts([
          ["Windows applications", "x86_64 / 64-bit only"],
          ["Compatibility layer", layer],
          ["Wine", compat.wine_version],
          ["Sandbox (bubblewrap)", compat.bubblewrap],
          ["AppArmor", compat.apparmor ? badge(compat.apparmor) : null],
          ["Managed by policy", compat.policy_managed ? "Yes" : "No"],
        ]))),
      h("div", { class: "subsection" }, h("h3", null, "Tracked packages"),
        dataTable("Tracked packages", [
          { label: "Package", cell: (p) => mono(p.name) },
          { label: "Version", cell: (p) => p.version },
        ], doc.packages || [], "No package information.")));
  }

  function adminDevicePanel(device, policies) {
    const active = device.status === "active";
    const nameInput = h("input", { type: "text", maxlength: "64", autocomplete: "off", value: device.name || "" });
    const profileInput = h("input", { type: "text", maxlength: "64", autocomplete: "off", spellcheck: "false",
      value: device.profile || "" });
    const names = policies.map((p) => p.name);
    if (device.policy_name && !names.includes(device.policy_name)) names.unshift(device.policy_name);
    const policySelect = selectEl(names.map((n) => ({ value: n, label: n })), device.policy_name);
    const errors = h("div", { class: "form-errors" });
    const save = button("Save changes", null, { kind: "primary", type: "submit" });
    const form = h("form", { class: "form", novalidate: true },
      h("div", { class: "form-grid" },
        field("Name", nameInput, "Letters, digits, spaces, '.', '_' and '-' (up to 64). Leave empty to remove the name."),
        field("Profile", profileInput, "A lower-case identifier such as engineering. Leave empty to remove it."),
        field("Policy", policySelect, "The device fetches the latest version of this policy.")),
      errors,
      h("div", { class: "form-actions" }, save));
    listen(form, "submit", async (event) => {
      event.preventDefault();
      const body = {};
      const newName = nameInput.value.trim();
      if ((device.name || "") !== newName) body.name = newName || null;
      const newProfile = profileInput.value.trim();
      if ((device.profile || "") !== newProfile) body.profile = newProfile || null;
      if (policySelect.value && policySelect.value !== device.policy_name) body.policy_name = policySelect.value;
      if (!Object.keys(body).length) {
        notify("Nothing changed.", "info");
        return;
      }
      await runAction(save, async () => {
        await api("PATCH", `/api/v1/devices/${enc(device.device_id)}`, body);
        notify(`${deviceLabel(device)} updated.`, "ok");
      }, { errorSlot: errors });
    });
    const retire = active ? button("Retire device…", (event) => retireDevice(device, event.currentTarget), { kind: "danger" }) : null;
    return panel("Administration", { wide: true },
      h("h3", null, "Device settings"),
      form,
      h("div", { class: "danger-zone" },
        h("h3", null, "Retire device"),
        h("p", null, active
          ? "Retiring revokes the device certificate: the device can no longer connect, receive commands or enroll again with this device ID. Queued commands are cancelled. This cannot be undone."
          : "This device is retired."),
        retire));
  }

  async function retireDevice(device, btn) {
    const label = deviceLabel(device);
    const accept = [device.device_id];
    if (device.name) accept.push(device.name);
    const ok = await confirmDialog({
      title: `Retire ${label}?`,
      message: `Retiring “${label}” permanently revokes its device certificate. It can no longer connect, receive commands or enroll again with this device ID, and its queued commands are cancelled. This cannot be undone.`,
      facts: [["Device", label], ["Device ID", device.device_id]],
      requireText: {
        accept,
        label: device.name ? "Type the device name or device ID to confirm" : "Type the device ID to confirm",
      },
      confirmLabel: "Retire device",
      danger: true,
    });
    if (!ok) return;
    await runAction(btn, async () => {
      await api("POST", `/api/v1/devices/${enc(device.device_id)}/retire`);
      notify(`${label} was retired.`, "ok");
    });
  }

  // ---------------------------------------------------------------------------
  // Applications (catalog)
  // ---------------------------------------------------------------------------
  async function viewApplications() {
    const isAdmin = roleAllows(state.me.role, "admin");
    const [apps, artifacts] = await Promise.all([apiGet("/api/v1/applications"), apiGet("/api/v1/artifacts")]);
    const list = apps.applications || [];
    const editor = isAdmin ? manifestEditor() : null;
    const columns = [
      {
        label: "Application", cell: (a) => h("div", null, h("span", { class: "strong" }, a.name), " ",
          a.deprecated ? badge("deprecated") : null, h("div", { class: "cell-sub mono" }, a.app_id)),
      },
      { label: "Publisher", cell: (a) => a.publisher },
      { label: "Version", cell: (a) => a.version },
      {
        label: "Architecture", cell: (a) => (a.architecture === "x86_64" ? a.architecture_label
          : badge("UNSUPPORTED_ARCHITECTURE", a.architecture_label)),
      },
      { label: "Support", cell: (a) => badge(a.support) },
      { label: "Catalog status", cell: (a) => badge(a.status) },
      {
        label: "Installable", class: "cell-note", cell: (a) => (a.installable ? badge("yes", "Yes")
          : h("span", { class: "muted" }, installBlockReason(a))),
      },
      {
        label: "Installer", cell: (a) => {
          const inst = a.installer || {};
          return h("div", null, inst.file_name || "—", h("div", { class: "cell-sub mono", title: inst.sha256 }, shortHash(inst.sha256)));
        },
      },
      { label: "Updated", cell: (a) => h("div", null, timeEl(a.updated_at), h("div", { class: "cell-sub" }, a.updated_by || "")) },
    ];
    if (isAdmin) {
      columns.push({
        label: "Actions", class: "cell-actions", cell: (a) => h("div", { class: "button-row button-row--tight" },
          button("Edit manifest", () => editor.load(a.manifest), { kind: "small" }),
          a.deprecated ? null : button("Deprecate…", (event) => deprecateApplication(a, event.currentTarget),
            { kind: "small danger-ghost" })),
      });
    }
    const artifactColumns = [
      { label: "File", cell: (a) => a.file_name },
      { label: "SHA-256", cell: (a) => h("span", { class: "mono", title: a.sha256 }, shortHash(a.sha256)) },
      { label: "Type", cell: (a) => a.kind },
      {
        label: "Windows architecture", cell: (a) => (a.machine === "x86"
          ? badge("UNSUPPORTED_ARCHITECTURE", "x86 / 32-bit") : machineLabel(a.machine)),
      },
      { label: "Size", class: "num", cell: (a) => formatBytes(a.size) },
      { label: "Uploaded", cell: (a) => h("div", null, timeEl(a.uploaded_at), h("div", { class: "cell-sub" }, a.uploaded_by || "")) },
    ];
    if (isAdmin) {
      artifactColumns.push({
        label: "Actions", class: "cell-actions",
        cell: (a) => button("Start a manifest", () => editor.load(manifestTemplate(a)), { kind: "small" }),
      });
    }
    return page(
      pageHeader("Applications", "The catalog of Windows applications that can be managed on Boswas OS devices."),
      alertBox("info", "Boswas OS runs 64-bit (x86_64) Windows applications only. 32-bit (x86) entries stay listed as unsupported and can never be installed."),
      panel("Catalog", { wide: true }, dataTable("Application catalog", columns, list, "The catalog is empty.",
        (a) => (a.deprecated ? "row--muted" : null))),
      isAdmin ? uploadPanel(editor) : null,
      isAdmin ? editor.node : null,
      panel("Stored installers", { wide: true }, dataTable("Stored installers", artifactColumns,
        artifacts.artifacts || [], "No installers uploaded yet.")));
  }

  function uploadResult(artifact, editor) {
    const is32 = artifact.machine === "x86";
    return alertBox(is32 ? "warn" : "ok", `Stored ${artifact.file_name}`,
      facts([
        ["SHA-256", h("span", { class: "mono wrap-anywhere" }, artifact.sha256)],
        ["Size", formatBytes(artifact.size)],
        ["Type", artifact.kind],
        ["Windows architecture", machineLabel(artifact.machine)],
      ]),
      is32 ? h("p", null, "This is a 32-bit (x86) installer. Boswas OS runs 64-bit Windows applications only; a catalog entry for it is listed as unsupported and cannot be installed.") : null,
      h("div", { class: "button-row" }, copyButton(artifact.sha256, "Copy SHA-256"),
        button("Start a manifest for this installer", () => editor.load(manifestTemplate(artifact)), { kind: "small" })));
  }

  function uploadPanel(editor) {
    const input = h("input", { type: "file", accept: ".exe,.msi" });
    const errors = h("div", { class: "form-errors" });
    const go = button("Upload installer", null, { kind: "primary", type: "submit" });
    const form = h("form", { class: "form", novalidate: true },
      field("Installer (.exe or .msi)", input, "Stored under its SHA-256 and served only to enrolled devices, for catalogued applications."),
      errors,
      h("div", { class: "form-actions" }, go));
    listen(form, "submit", async (event) => {
      event.preventDefault();
      errors.replaceChildren();
      const file = input.files && input.files[0];
      if (!file) {
        errors.replaceChildren(alertBox("bad", "Choose an installer file first."));
        return;
      }
      if (!file.size) {
        errors.replaceChildren(alertBox("bad", "The file is empty."));
        return;
      }
      go.textContent = `Uploading ${formatBytes(file.size)}…`;
      await runAction(go, async () => {
        const artifact = await api("POST", "/api/v1/artifacts", undefined, {
          raw: file, headers: { "X-Boswas-File-Name": encodeURIComponent(file.name) },
        });
        ui.lastUpload = artifact;
        notify(`Uploaded ${artifact.file_name}.`, "ok");
      }, { errorSlot: errors });
      if (go.isConnected) go.textContent = "Upload installer";
    });
    return panel("Upload an installer", { wide: true }, form, ui.lastUpload ? uploadResult(ui.lastUpload, editor) : null);
  }

  function manifestEditor() {
    const textarea = h("textarea", { class: "code", rows: "18", spellcheck: "false", autocomplete: "off",
      autocapitalize: "off" });
    const errors = h("div", { class: "form-errors" });
    const save = button("Save to catalog", null, { kind: "primary", type: "submit" });
    const check = button("Check JSON", () => {
      const parsed = parseManifestText(textarea.value);
      errors.replaceChildren(parsed.error ? alertBox("bad", parsed.error)
        : alertBox("ok", "The JSON is well formed. The Control Plane validates the manifest itself when you save."));
    });
    const form = h("form", { class: "form", novalidate: true },
      field("Manifest (JSON)", textarea, "A Boswas compatibility manifest. Upload the installer first and pin it with installer.sha256; saving an existing ID replaces that catalog entry."),
      errors,
      h("div", { class: "form-actions" }, check, save));
    listen(form, "submit", async (event) => {
      event.preventDefault();
      errors.replaceChildren();
      const parsed = parseManifestText(textarea.value);
      if (parsed.error) {
        errors.replaceChildren(alertBox("bad", parsed.error));
        textarea.focus();
        return;
      }
      await runAction(save, async () => {
        const app = await api("POST", "/api/v1/applications", { manifest: parsed.manifest });
        notify(`${app.name} ${app.version} saved to the catalog (${humanize(app.support)}).`,
          app.support === "SUPPORTED" ? "ok" : "warn");
      }, { errorSlot: errors });
    });
    const node = panel("Add or replace a manifest", { wide: true }, form);
    return {
      node,
      load(manifest) {
        textarea.value = JSON.stringify(manifest, null, 2);
        errors.replaceChildren();
        state.editing = true;
        updateRefreshStatus();
        node.scrollIntoView({ block: "start" });
        textarea.focus({ preventScroll: true });
      },
    };
  }

  async function deprecateApplication(app, btn) {
    const ok = await confirmDialog({
      title: `Deprecate ${app.name}?`,
      message: `${app.name} ${app.version} can no longer be installed or updated from the catalog. Devices keep installed copies, which can still be launched, repaired or removed. Saving its manifest again restores the entry.`,
      facts: [["Application", app.app_id]],
      confirmLabel: "Deprecate",
      danger: true,
    });
    if (!ok) return;
    await runAction(btn, async () => {
      await api("DELETE", `/api/v1/applications/${enc(app.app_id)}`);
      notify(`${app.name} is deprecated.`, "ok");
    });
  }

  // ---------------------------------------------------------------------------
  // Policies
  // ---------------------------------------------------------------------------
  function compatFacts(c) {
    const compat = c || {};
    return facts([
      ["Allowed catalog statuses", listText((compat.allowed_statuses || []).map(humanize))],
      ["Unlisted applications", compat.unlisted_apps === "allow" ? "Allowed (confined)" : "Denied"],
      ["Network for unlisted applications", compat.unlisted_network ? "Allowed" : "Blocked"],
      ["Devices for unlisted applications", listText((compat.unlisted_devices || []).map((d) => DEVICE_LABELS[d] || d))],
      ["Folders for unlisted applications", listText((compat.unlisted_folders || []).map(humanize))],
      ["AppArmor confinement", badge("enforce", "Always on")],
      ["Largest installer", compat.max_installer_mb ? `${compat.max_installer_mb} MB` : null],
      ["Blocked applications", listText(compat.blocked_applications)],
      ["Allow list", compat.allowed_applications === null || compat.allowed_applications === undefined
        ? "No allow list" : listText(compat.allowed_applications, "Empty: no application may run")],
    ]);
  }

  function agentFacts(a, u) {
    const agent = a || {};
    const updates = u || {};
    return facts([
      ["Heartbeat interval", agent.heartbeat_seconds ? `${agent.heartbeat_seconds} s` : null],
      ["Inventory interval", agent.inventory_seconds ? `${agent.inventory_seconds} s` : null],
      ["Allowed commands", (agent.allowed_commands || []).length
        ? h("div", { class: "chips" }, agent.allowed_commands.map((t) => h("span", { class: "chip" }, humanize(t))))
        : "None"],
      ["Agent updates", humanize(updates.agent_updates)],
    ]);
  }

  async function viewPolicies(ctx) {
    const isAdmin = roleAllows(state.me.role, "admin");
    const data = await apiGet("/api/v1/policies");
    const policies = data.policies || [];
    const slot = h("div");
    let newButton = null;
    if (isAdmin) {
      newButton = button("New policy", () => {
        ctx.hold();
        state.editing = true;
        const base = policies.find((p) => p.name === "default") || policies[0];
        slot.replaceChildren(policyEditorPanel({ doc: base ? base.document : null, fixedName: null }));
        newButton.disabled = true;
        slot.scrollIntoView({ block: "start" });
      }, { kind: "primary" });
    }
    const columns = [
      {
        label: "Policy", cell: (p) => h("div", null, h("a", { href: `#/policies/${enc(p.name)}`, class: "strong-link" }, p.name),
          p.document && p.document.description ? h("div", { class: "cell-sub" }, p.document.description) : null),
      },
      { label: "Version", cell: (p) => mono(p.version) },
      { label: "Devices", class: "num", cell: (p) => String(p.devices || 0) },
      { label: "Allowed statuses", cell: (p) => listText(((p.document.compat || {}).allowed_statuses || []).map(humanize)) },
      {
        label: "Unlisted applications", cell: (p) => ((p.document.compat || {}).unlisted_apps === "allow"
          ? badge("experimental", "Allowed") : badge("approved", "Denied")),
      },
      { label: "Unlisted network", cell: (p) => ((p.document.compat || {}).unlisted_network ? "Allowed" : "Blocked") },
      {
        label: "Allowed commands", cell: (p) => {
          const cmds = (p.document.agent || {}).allowed_commands || [];
          return h("span", { title: cmds.join(", ") }, `${cmds.length} of ${COMMAND_TYPES.length}`);
        },
      },
      { label: "Agent updates", cell: (p) => humanize((p.document.updates || {}).agent_updates) },
      { label: "Published", cell: (p) => h("div", null, timeEl(p.created_at), h("div", { class: "cell-sub" }, p.created_by || "")) },
    ];
    return page(
      pageHeader("Policies", "Signed, versioned device policies. Devices fetch the latest version of the policy assigned to them.", newButton),
      slot,
      panel(null, { wide: true }, dataTable("Policies", columns, policies, "No policies published yet.")));
  }

  async function viewPolicy(ctx) {
    const isAdmin = roleAllows(state.me.role, "admin");
    const policy = await apiGet(`/api/v1/policies/${enc(ctx.params[0])}`);
    const doc = policy.document || {};
    const slot = h("div");
    let newVersion = null;
    if (isAdmin) {
      newVersion = button("New version", () => {
        ctx.hold();
        state.editing = true;
        slot.replaceChildren(policyEditorPanel({ doc, fixedName: policy.name }));
        newVersion.disabled = true;
        slot.scrollIntoView({ block: "start" });
      }, { kind: "primary" });
    }
    return page(
      breadcrumb({ label: "Policies", href: "#/policies" }, { label: policy.name }),
      pageHeader(policy.name, h("span", null, `Version ${policy.version} · published `, timeEl(policy.created_at),
        policy.created_by ? ` by ${policy.created_by}` : ""), newVersion),
      slot,
      h("div", { class: "grid" },
        panel("Version", null, facts([
          ["Description", doc.description],
          ["Version", mono(policy.version)],
          ["Sequence", policy.sequence],
          ["Issued", timeEl(doc.issued_at)],
          ["Published by", policy.created_by],
        ])),
        panel("Agent and updates", null, agentFacts(doc.agent, doc.updates)),
        panel("Windows applications", { wide: true }, compatFacts(doc.compat))),
      panel("History", { wide: true }, dataTable("Policy history", [
        { label: "Version", cell: (v) => mono(v.version) },
        { label: "Published", cell: (v) => timeEl(v.created_at) },
        { label: "Published by", cell: (v) => mono(v.created_by) },
      ], policy.history || [], "No history.")));
  }

  function policyEditorPanel(opts) {
    const doc = opts.doc || DEFAULT_POLICY_DOC;
    const compat = Object.assign({}, DEFAULT_POLICY_DOC.compat, doc.compat || {});
    const agent = Object.assign({}, DEFAULT_POLICY_DOC.agent, doc.agent || {});
    const updates = Object.assign({}, DEFAULT_POLICY_DOC.updates, doc.updates || {});
    const nameInput = opts.fixedName ? null : h("input", { type: "text", maxlength: "32", autocomplete: "off",
      spellcheck: "false", autocapitalize: "off", required: true });
    const description = h("input", { type: "text", maxlength: "500", autocomplete: "off", value: doc.description || "" });
    const statuses = checkGroup("Allowed catalog statuses",
      COMPAT_STATUSES.map((s) => ({ value: s, label: humanize(s) })), compat.allowed_statuses,
      "Catalogued applications with these statuses may be installed and run.");
    const unlistedApps = selectEl([
      { value: "deny", label: "Deny: catalogued applications only" },
      { value: "allow", label: "Allow, confined with the settings below" },
    ], compat.unlisted_apps);
    const unlistedNetwork = checkbox("Unlisted applications may use the network", compat.unlisted_network);
    const unlistedDevices = checkGroup("Devices available to unlisted applications",
      POLICY_DEVICES.map((d) => ({ value: d, label: DEVICE_LABELS[d] })), compat.unlisted_devices);
    const unlistedFolders = checkGroup("Folders shared with unlisted applications",
      POLICY_FOLDERS.map((f) => ({ value: f, label: humanize(f) })), compat.unlisted_folders);
    const maxMb = numberInput(compat.max_installer_mb, 1, 65536);
    const blocked = h("textarea", { class: "code", rows: "4", spellcheck: "false", autocapitalize: "off",
      value: (compat.blocked_applications || []).join("\n") });
    const allowed = h("textarea", { class: "code", rows: "4", spellcheck: "false", autocapitalize: "off",
      value: (compat.allowed_applications || []).join("\n") });
    const heartbeat = numberInput(agent.heartbeat_seconds, 30, 86400);
    const inventory = numberInput(agent.inventory_seconds, 300, 604800);
    const commands = checkGroup("Allowed commands",
      COMMAND_TYPES.map((t) => ({ value: t, label: humanize(t) })), agent.allowed_commands,
      "The typed commands operators may send to devices with this policy.");
    const agentUpdates = selectEl([
      { value: "manual", label: "Manual (Update agent is refused)" },
      { value: "managed", label: "Managed (Update agent is accepted)" },
    ], updates.agent_updates);
    const errors = h("div", { class: "form-errors" });
    const publish = button("Publish", null, { kind: "primary", type: "submit" });
    const cancel = button("Cancel", () => render({ quiet: true }));
    const form = h("form", { class: "form policy-editor", novalidate: true },
      h("div", { class: "form-grid" },
        nameInput ? field("Policy name", nameInput, "Lower-case letters, digits and '-', up to 32 characters. A new name creates a new policy.") : null,
        field("Description", description, "One line, up to 500 characters.")),
      h("h3", null, "Windows applications"),
      statuses.node,
      h("div", { class: "form-grid" },
        field("Applications that are not in the catalog", unlistedApps),
        field("Largest installer (MB)", maxMb, "1 to 65536.")),
      h("div", { class: "form-grid" },
        field("Blocked applications", blocked, "One application ID per line, e.g. com.example.app."),
        field("Allowed applications", allowed, "One application ID per line. Leave empty for no allow list.")),
      h("fieldset", { class: "fieldset" }, h("legend", null, "Sandbox for applications that are not in the catalog"),
        unlistedNetwork.node, unlistedDevices.node, unlistedFolders.node),
      h("div", { class: "fact-inline" }, h("span", { class: "strong" }, "AppArmor confinement: "), badge("enforce", "Always on"),
        h("span", { class: "muted" }, " A policy cannot switch confinement off.")),
      h("h3", null, "Agent"),
      h("div", { class: "form-grid" },
        field("Heartbeat interval (seconds)", heartbeat, "30 to 86400."),
        field("Inventory interval (seconds)", inventory, "300 to 604800."),
        field("Agent updates", agentUpdates, "Managed also needs UPDATE_POLICY=managed on the device.")),
      commands.node,
      errors,
      h("div", { class: "form-actions" }, cancel, publish));
    listen(form, "submit", async (event) => {
      event.preventDefault();
      errors.replaceChildren();
      const body = buildPolicyBody({
        name: opts.fixedName || nameInput.value,
        description: description.value,
        allowedStatuses: statuses.values(),
        unlistedApps: unlistedApps.value,
        unlistedNetwork: unlistedNetwork.input.checked,
        unlistedDevices: unlistedDevices.values(),
        unlistedFolders: unlistedFolders.values(),
        maxInstallerMb: maxMb.value,
        blockedApplications: blocked.value,
        allowedApplications: allowed.value,
        heartbeatSeconds: heartbeat.value,
        inventorySeconds: inventory.value,
        allowedCommands: commands.values(),
        agentUpdates: agentUpdates.value,
      });
      const problems = validatePolicyBody(body);
      if (problems.length) {
        errors.replaceChildren(errorBox({ message: "Correct the policy before publishing.", details: problems }));
        return;
      }
      const ok = await confirmDialog({
        title: `Publish ${body.name}?`,
        message: `A new signed version of the policy “${body.name}” is published. Devices assigned to it fetch and apply it after their next heartbeat.`,
        confirmLabel: "Publish",
      });
      if (!ok) return;
      publish.disabled = true;
      try {
        const result = await api("POST", "/api/v1/policies", body);
        notify(`Published ${result.version}.`, "ok");
        const target = `#/policies/${enc(result.name)}`;
        if (location.hash === target) render();
        else location.hash = target;
      } catch (err) {
        if (!(err && err.status === 401)) errors.replaceChildren(errorBox(err));
      } finally {
        if (publish.isConnected) publish.disabled = false;
      }
    });
    return panel(opts.fixedName ? `New version of ${opts.fixedName}` : "New policy", { wide: true }, form);
  }

  // ---------------------------------------------------------------------------
  // Commands (fleet)
  // ---------------------------------------------------------------------------
  async function viewCommands(ctx) {
    const status = COMMAND_STATUSES.includes(ctx.query.status) ? ctx.query.status : "";
    const canOperate = roleAllows(state.me.role, "operator");
    const [data, devices] = await Promise.all([
      apiGet("/api/v1/commands", { status: status || undefined, limit: 200 }),
      apiGet("/api/v1/devices"),
    ]);
    const names = deviceNames(devices.devices);
    const select = selectEl([{ value: "", label: "All statuses" }]
      .concat(COMMAND_STATUSES.map((s) => ({ value: s, label: humanize(s) }))), status, { "data-no-hold": "" });
    listen(select, "change", () => {
      location.hash = select.value ? `#/commands?status=${select.value}` : "#/commands";
    });
    return page(
      pageHeader("Commands", "Typed commands across the fleet, newest first (up to 200)."),
      h("div", { class: "toolbar" }, field("Status", select)),
      panel(null, { wide: true }, dataTable("Commands", commandColumns({ showDevice: true, names, cancel: canOperate }),
        data.commands || [], status ? "No commands with this status." : "No commands yet.")));
  }

  // ---------------------------------------------------------------------------
  // Audit trail
  // ---------------------------------------------------------------------------
  function verifyResult(v) {
    const r = v.result || {};
    if (r.valid) {
      return alertBox("ok", `The audit trail is intact: ${r.events} ${r.events === 1 ? "event" : "events"} verified along the hash chain.`,
        h("p", { class: "hint" }, "Checked ", timeEl(v.at), "."));
    }
    return alertBox("bad", `The audit trail is broken at event #${r.first_invalid_event}. Events from that point on may have been altered or removed.`,
      h("p", null, `${r.events} events before it verified. Investigate before relying on later entries.`),
      h("p", { class: "hint" }, "Checked ", timeEl(v.at), "."));
  }

  async function viewEvents(ctx) {
    const PAGE_SIZE = 100;
    const type = EVENT_TYPES.includes(ctx.query.type) ? ctx.query.type : "";
    const deviceId = UUID_RE.test(ctx.query.device || "") ? ctx.query.device : "";
    const isAdmin = roleAllows(state.me.role, "admin");
    const filters = { type: type || undefined, device_id: deviceId || undefined };
    const [data, devices] = await Promise.all([
      apiGet("/api/v1/events", Object.assign({ limit: PAGE_SIZE }, filters)),
      apiGet("/api/v1/devices"),
    ]);
    const names = deviceNames(devices.devices);
    const events = data.events || [];
    const columns = eventColumns({ id: true, device: true, command: true, names });
    const table = dataTable("Audit trail", columns, events, "No events match this filter.");
    let oldest = events.length ? events[events.length - 1].id : null;
    let more = null;
    if (events.length === PAGE_SIZE) {
      more = button("Load older events", async () => {
        ctx.hold();
        more.disabled = true;
        try {
          const older = await apiGet("/api/v1/events", Object.assign({ limit: PAGE_SIZE, before: oldest }, filters));
          const rows = older.events || [];
          const tbody = table.querySelector("tbody");
          for (const e of rows) tbody.appendChild(tableRow(columns, e));
          if (rows.length) oldest = rows[rows.length - 1].id;
          if (rows.length < PAGE_SIZE) more.replaceWith(h("p", { class: "muted" }, "This is the beginning of the audit trail."));
        } catch (err) {
          notifyError(err);
        } finally {
          if (more.isConnected) more.disabled = false;
        }
      });
    }
    const typeSelect = selectEl([{ value: "", label: "All events" }]
      .concat(EVENT_TYPES.map((t) => ({ value: t, label: humanize(t) }))), type, { "data-no-hold": "" });
    listen(typeSelect, "change", () => {
      const q = queryString({ type: typeSelect.value || undefined, device: deviceId || undefined });
      location.hash = `#/events${q}`;
    });
    const verifySlot = h("div", null, ui.lastVerify ? verifyResult(ui.lastVerify) : null);
    let verify = null;
    if (isAdmin) {
      verify = button("Verify audit trail", async () => {
        verify.disabled = true;
        try {
          const result = await apiGet("/api/v1/events/verify");
          ui.lastVerify = { result, at: new Date().toISOString().replace(/\.\d{3}Z$/, "Z") };
          verifySlot.replaceChildren(verifyResult(ui.lastVerify));
        } catch (err) {
          notifyError(err);
        } finally {
          if (verify.isConnected) verify.disabled = false;
        }
      });
    }
    const deviceChip = deviceId ? h("p", { class: "filter-note" }, "Device: ", deviceLink(deviceId, names), " · ",
      h("a", { href: `#/events${queryString({ type: type || undefined })}` }, "show all devices")) : null;
    return page(
      pageHeader("Audit trail", "Every security-relevant action, newest first. Entries are hash-chained.", verify),
      verifySlot,
      h("div", { class: "toolbar" }, field("Event type", typeSelect), deviceChip),
      panel(null, { wide: true }, table, more ? h("div", { class: "button-row" }, more) : null));
  }

  // ---------------------------------------------------------------------------
  // Enrollment tokens
  // ---------------------------------------------------------------------------
  function enrollmentForm(policies) {
    const description = h("input", { type: "text", maxlength: "200", autocomplete: "off" });
    const profile = h("input", { type: "text", maxlength: "64", autocomplete: "off", spellcheck: "false", autocapitalize: "off" });
    const policy = selectEl([{ value: "", label: "Device default" }]
      .concat(policies.map((p) => ({ value: p.name, label: `${p.name} (${p.version})` }))), "");
    const ttl = numberInput(24, 1, 720);
    const uses = numberInput(1, 1, 1000);
    const ephemeral = checkbox("Allow live-session devices (their identity disappears at shutdown)", false);
    const deviceId = h("input", { type: "text", maxlength: "36", autocomplete: "off", spellcheck: "false", autocapitalize: "off" });
    const errors = h("div", { class: "form-errors" });
    const create = button("Create token", null, { kind: "primary", type: "submit" });
    const form = h("form", { class: "form", novalidate: true },
      h("div", { class: "form-grid" },
        field("Description", description, "Optional, up to 200 characters, e.g. the site or batch."),
        field("Profile", profile, "Optional lower-case identifier such as engineering."),
        field("Policy", policy, "Device default: the policy the device had before, otherwise default."),
        field("Valid for (hours)", ttl, "1 to 720."),
        field("Maximum uses", uses, "1 to 1000 devices."),
        field("Device ID", deviceId, "Optional. Only this device may use the token; required to re-enroll an enrolled device.")),
      ephemeral.node,
      errors,
      h("div", { class: "form-actions" }, create));
    listen(form, "submit", async (event) => {
      event.preventDefault();
      errors.replaceChildren();
      const body = buildTokenBody({
        description: description.value, profile: profile.value, policyName: policy.value, ttlHours: ttl.value,
        maxUses: uses.value, allowEphemeral: ephemeral.input.checked, deviceId: deviceId.value,
      });
      const problems = validateTokenBody(body);
      if (problems.length) {
        errors.replaceChildren(errorBox({ message: "Correct the form before creating the token.", details: problems }));
        return;
      }
      create.disabled = true;
      try {
        const result = await api("POST", "/api/v1/enrollment-tokens", body);
        await secretDialog({
          title: "Enrollment token created",
          secret: result.token,
          secretLabel: "Enrollment token",
          warning: "Copy this token now. It is shown only once and cannot be retrieved later. Anyone who has it can enroll a device until it expires, is used up or is revoked.",
          facts: [
            ["Token ID", result.token_id],
            ["Expires", absoluteUtc(result.expires_at)],
            ["Maximum uses", String(result.max_uses)],
            ["Profile", result.profile],
            ["Policy", result.policy_name || "Device default"],
            ["Live-session devices", result.allow_ephemeral ? "Allowed" : "Not allowed"],
            ["Device", result.device_id || "Any new device"],
          ],
        });
        await refreshView();
      } catch (err) {
        if (!(err && err.status === 401)) errors.replaceChildren(errorBox(err));
      } finally {
        if (create.isConnected) create.disabled = false;
      }
    });
    return form;
  }

  async function revokeToken(token, btn) {
    const ok = await confirmDialog({
      title: `Revoke enrollment token ${token.token_id}?`,
      message: "Devices can no longer enroll with this token. Devices that already enrolled with it are not affected.",
      facts: [["Token ID", token.token_id], ["Description", token.description || "—"]],
      confirmLabel: "Revoke token",
      danger: true,
    });
    if (!ok) return;
    await runAction(btn, async () => {
      await api("POST", `/api/v1/enrollment-tokens/${enc(token.token_id)}/revoke`);
      notify(`Token ${token.token_id} revoked.`, "ok");
    });
  }

  async function viewEnrollment() {
    const [tokens, policies] = await Promise.all([apiGet("/api/v1/enrollment-tokens"), apiGet("/api/v1/policies")]);
    const now = Date.now();
    const columns = [
      { label: "Token ID", cell: (t) => mono(t.token_id) },
      { label: "Description", cell: (t) => t.description },
      { label: "Profile", cell: (t) => t.profile },
      { label: "Policy", cell: (t) => t.policy_name || h("span", { class: "muted" }, "Device default") },
      { label: "Uses", class: "num", cell: (t) => `${t.uses} / ${t.max_uses}` },
      { label: "Live sessions", cell: (t) => (t.allow_ephemeral ? "Allowed" : "No") },
      { label: "Device", cell: (t) => (t.device_id ? deviceLink(t.device_id) : h("span", { class: "muted" }, "Any new device")) },
      { label: "Created", cell: (t) => h("div", null, timeEl(t.created_at), h("div", { class: "cell-sub" }, t.created_by || "")) },
      { label: "Expires", cell: (t) => timeEl(t.expires_at) },
      { label: "State", cell: (t) => badge(tokenState(t, now)) },
      {
        label: "Actions", class: "cell-actions",
        cell: (t) => (t.revoked ? "" : button("Revoke", (event) => revokeToken(t, event.currentTarget), { kind: "small danger-ghost" })),
      },
    ];
    return page(
      pageHeader("Enrollment", "One-time tokens that let Boswas OS devices enroll. A token is shown once, when it is created; only its ID is kept here."),
      panel("Create an enrollment token", { wide: true }, enrollmentForm(policies.policies || [])),
      panel("Enrollment tokens", { wide: true }, dataTable("Enrollment tokens", columns, tokens.tokens || [],
        "No enrollment tokens yet.")));
  }

  // ---------------------------------------------------------------------------
  // Operators
  // ---------------------------------------------------------------------------
  function operatorForm() {
    const name = h("input", { type: "text", maxlength: "32", autocomplete: "off", spellcheck: "false", autocapitalize: "off" });
    const role = selectEl([
      { value: "viewer", label: "Viewer: read only" },
      { value: "operator", label: "Operator: read, create and cancel commands" },
      { value: "admin", label: "Admin: everything" },
    ], "viewer");
    const errors = h("div", { class: "form-errors" });
    const add = button("Add operator", null, { kind: "primary", type: "submit" });
    const form = h("form", { class: "form", novalidate: true },
      h("div", { class: "form-grid" },
        field("Name", name, "2 to 32 lower-case letters, digits, '.', '_' or '-', starting with a letter."),
        field("Role", role)),
      errors,
      h("div", { class: "form-actions" }, add));
    listen(form, "submit", async (event) => {
      event.preventDefault();
      errors.replaceChildren();
      const value = name.value.trim();
      if (!OPERATOR_RE.test(value)) {
        errors.replaceChildren(alertBox("bad", "Operator names are 2 to 32 lower-case letters, digits, '.', '_' or '-', starting with a letter."));
        return;
      }
      add.disabled = true;
      try {
        const result = await api("POST", "/api/v1/operators", { name: value, role: role.value });
        await secretDialog({
          title: `API token for ${result.name}`,
          secret: result.token,
          secretLabel: "Operator API token",
          warning: `Give this token to ${result.name} through a secure channel. It is shown only once; if it is lost, disable this operator and add a new one.`,
          facts: [["Operator", result.name], ["Role", humanize(result.role)]],
        });
        await refreshView();
      } catch (err) {
        if (!(err && err.status === 401)) errors.replaceChildren(errorBox(err));
      } finally {
        if (add.isConnected) add.disabled = false;
      }
    });
    return form;
  }

  async function disableOperator(op, btn) {
    const ok = await confirmDialog({
      title: `Disable ${op.name}?`,
      message: `The API token of ${op.name} stops working immediately. A disabled operator cannot be enabled again from the dashboard.`,
      facts: [["Operator", op.name], ["Role", humanize(op.role)]],
      confirmLabel: "Disable operator",
      danger: true,
    });
    if (!ok) return;
    await runAction(btn, async () => {
      await api("POST", `/api/v1/operators/${enc(op.name)}/disable`);
      notify(`${op.name} is disabled.`, "ok");
    });
  }

  async function viewOperators() {
    const data = await apiGet("/api/v1/operators");
    const self = state.me.actor;
    const columns = [
      {
        label: "Name", cell: (o) => h("span", null, mono(o.name),
          self === `operator:${o.name}` ? h("span", { class: "muted" }, " (you)") : null),
      },
      { label: "Role", cell: (o) => badge(o.role, humanize(o.role)) },
      { label: "Created", cell: (o) => h("div", null, timeEl(o.created_at), h("div", { class: "cell-sub" }, o.created_by || "")) },
      { label: "Last used", cell: (o) => timeEl(o.last_used_at, "never") },
      { label: "Status", cell: (o) => (o.disabled ? badge("disabled", "Disabled") : badge("active", "Active")) },
      {
        label: "Actions", class: "cell-actions", cell: (o) => {
          if (o.disabled) return "";
          if (self === `operator:${o.name}`) return h("span", { class: "muted" }, "Your account");
          return button("Disable…", (event) => disableOperator(o, event.currentTarget), { kind: "small danger-ghost" });
        },
      },
    ];
    return page(
      pageHeader("Operators", "People and systems that use the Control Plane API. Tokens are stored only as hashes."),
      panel("Add an operator", { wide: true }, operatorForm()),
      panel("Operators", { wide: true }, dataTable("Operators", columns, data.operators || [], "No operators.")));
  }

  // ---------------------------------------------------------------------------
  // Boot
  // ---------------------------------------------------------------------------
  function boot() {
    listen($("signin-form"), "submit", onSignIn);
    listen($("sign-out"), "click", () => signOut("You have signed out.", false));
    listen($("nav-toggle"), "click", toggleNav);
    listen($("skip-link"), "click", (event) => {
      event.preventDefault();
      $("main").focus();
    });
    listen($("sidebar"), "click", (event) => {
      if (event.target.closest && event.target.closest("a")) closeNav();
    });
    listen(window, "hashchange", () => {
      if (state.me && location.hash.startsWith("#/")) render();
    });
    listen(document, "visibilitychange", onVisibilityChange);
    listen($("view"), "input", markEditing);
    listen($("view"), "change", markEditing);
    if (getToken()) {
      startSession().catch((err) => {
        if (err && err.status === 401) return;              // signOut() already showed the sign-in form
        showSignIn(`The Control Plane could not be reached: ${(err && err.message) || err}. Reload the page to try again.`, true);
      });
    } else {
      showSignIn();
    }
  }

  boot();
})();
