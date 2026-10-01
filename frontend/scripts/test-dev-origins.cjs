// Guards the dev-server origin allowlist (frontend/dev-origins.cjs).
//
// `npm run dev` refuses the HMR websocket from any origin whose hostname is not
// in `allowedDevOrigins`. A refused client retries 12 times (~40s) and then
// reloads the page, so a pattern that silently fails to match shows up as
// "the page reloads itself every 40 seconds" for everyone who opens the dev
// server through that hostname. This runs the list next.config.ts really uses
// through Next.js's own matcher instead of trusting how a wildcard looks.
const path = require("node:path");

const frontendDir = path.resolve(__dirname, "..");
const { buildAllowedDevOrigins } = require(path.join(frontendDir, "dev-origins.cjs"));

const resolveFromFrontend = (id) => require.resolve(id, { paths: [frontendDir] });

try {
    resolveFromFrontend("next/package.json");
} catch {
    console.log("SKIP: next is not installed (run npm ci in frontend/)");
    process.exit(0);
}

// From here on next is installed, so a missing matcher is a failure, not a
// skip: it means Next.js moved its internals and this guard no longer runs.
let isCsrfOriginAllowed;
try {
    ({ isCsrfOriginAllowed } = require(resolveFromFrontend("next/dist/server/app-render/csrf-protection")));
} catch (err) {
    console.error(`FAIL: could not load Next.js's origin matcher: ${err.message}`);
    process.exit(1);
}
if (typeof isCsrfOriginAllowed !== "function") {
    console.error("FAIL: next/dist/server/app-render/csrf-protection no longer exports isCsrfOriginAllowed");
    process.exit(1);
}

const failures = [];
const expect = (origins, host, allowed) => {
    if (isCsrfOriginAllowed(host, origins) !== allowed) {
        failures.push(`${host} should be ${allowed ? "allowed" : "refused"} by ${JSON.stringify(origins)}`);
    }
};

// Base list. Hostnames a user actually types, per README and
// docs/getting-started/tailscale-runbook.md.
const base = buildAllowedDevOrigins(undefined);
expect(base, "localhost", true);
expect(base, "127.0.0.1", true);
expect(base, "my-laptop.my-tailnet.ts.net", true); // MagicDNS: <machine>.<tailnet>.ts.net
expect(base, "desktop-abc.tail1234.ts.net", true);
// The allowlist exists to keep other sites off the dev websocket.
expect(base, "example.com", false);
expect(base, "ts.net.example.com", false);
expect(base, "evil-ts.net", false);
expect(base, "192.168.1.100", false);

// SAIVERSE_ALLOWED_ORIGINS. .env.example documents complete origins; Next.js
// compares hostnames, so every spelling has to come out as the hostname.
const extra = buildAllowedDevOrigins(
    " http://192.168.1.100:3000 , https://My-PC.example,lan-box:3000, bare.example ,*.corp.example,http://[fd7a::1]:3000,,",
);
expect(extra, "192.168.1.100", true);
expect(extra, "my-pc.example", true);
expect(extra, "lan-box", true);
expect(extra, "bare.example", true);
expect(extra, "host.corp.example", true);
expect(extra, "[fd7a::1]", true);
expect(extra, "my-laptop.my-tailnet.ts.net", true); // extras add to the base list
expect(extra, "192.168.1.101", false);
expect(extra, "", false); // empty entries must not become an allow-everything pattern
if (extra.some((origin) => origin === "")) failures.push(`empty entry leaked into ${JSON.stringify(extra)}`);

if (failures.length > 0) {
    console.error("FAIL: dev-server origin allowlist");
    for (const failure of failures) console.error(`  - ${failure}`);
    process.exit(1);
}
console.log(`OK: dev-server origin allowlist ${JSON.stringify(base)}`);
