// Hostnames the Next.js dev server (`npm run dev`) accepts its own /_next/*
// requests from. Kept out of next.config.ts as plain CommonJS so that
// scripts/test-dev-origins.cjs can run the real list through Next.js's matcher.
//
// Why this matters: an origin that is not on the list gets its HMR websocket
// refused (403). The dev client retries 12 times (~40s) and then reloads the
// page — forever, on every page. Nothing in the UI says why.

// Next.js matches against the *hostname* of the request origin. `*` covers
// exactly one label and `**` one or more. A MagicDNS name is
// `<machine>.<tailnet>.ts.net`, so `*.ts.net` never matched it.
const BASE_ORIGINS = ["localhost", "127.0.0.1", "**.ts.net"];

/**
 * Reduce one SAIVERSE_ALLOWED_ORIGINS entry to the hostname Next.js compares.
 * The backend reads the same variable as complete origins
 * (`http://192.168.1.100:3000`, see .env.example); a bare host, with or
 * without a port, and wildcard patterns are accepted too.
 * @param {string} entry
 * @returns {string}
 */
function toHostname(entry) {
    const host = entry.replace(/^[a-z][a-z0-9+.-]*:\/\//i, "").replace(/[/?#].*$/, "");
    // Drop a trailing :port. An IPv6 literal keeps its brackets, as in an Origin header.
    return host.replace(/^(\[[^\]]*\]|[^:]*):\d+$/, "$1").toLowerCase();
}

/**
 * @param {string | undefined} extra comma-separated SAIVERSE_ALLOWED_ORIGINS
 * @returns {string[]}
 */
function buildAllowedDevOrigins(extra) {
    const origins = [...BASE_ORIGINS];
    for (const entry of (extra || "").split(",")) {
        const hostname = toHostname(entry.trim());
        if (hostname) origins.push(hostname);
    }
    return origins;
}

module.exports = { buildAllowedDevOrigins, toHostname };
