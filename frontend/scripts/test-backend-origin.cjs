// Run the actual selector, Next config and Route Handlers against a fake fetch.
// No sockets are opened: even fallback/default tests never contact port 8000.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");
const ts = require("typescript");
const { transpileConfig } = require("next/dist/build/next-config-ts/transpile-config");
const { resolveBackendOrigin } = require("../backend-origin.cjs");
const frontendDir = path.resolve(__dirname, "..");
const isolated = "http://127.0.0.1:18000";
const keys = ["SAIVERSE_BACKEND_ORIGIN", "SAIVERSE_BACKEND_URL"];
const originalEnv = keys.map((key) => process.env[key]);
const realFetch = globalThis.fetch;
const realWarn = console.warn;
const realError = console.error;
let calls = [], warnings = [], expectedOrigin, respond;
function setEnv(origin, legacy) {
    for (const [key, value] of [[keys[0], origin], [keys[1], legacy]]) {
        if (value === undefined) delete process.env[key];
        else process.env[key] = value;
    }
}
// Compile real routes; relative imports still load the actual shared resolver.
function loadRoute(relativePath) {
    const filename = path.join(frontendDir, "src/app/api", relativePath, "route.ts");
    const { outputText } = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
        compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 }, fileName: filename,
    });
    const loaded = new Module(filename, module);
    loaded.filename = filename;
    loaded.paths = Module._nodeModulePaths(path.dirname(filename));
    loaded._compile(outputText, filename);
    return loaded.exports;
}
function request(url, method = "GET", headers = {}, body) {
    return new Request(`http://127.0.0.1:18010${url}`, { method, headers, body });
}
async function withTimeout(promise) {
    let timer;
    try {
        return await Promise.race([promise, new Promise((_, reject) => {
            timer = setTimeout(() => reject(new Error("stream buffered")), 1000);
        })]);
    } finally { clearTimeout(timer); }
}
async function main() {
    console.warn = (message) => warnings.push(message);
    globalThis.fetch = async (input, init = {}) => {
        const url = new URL(input);
        assert.equal(url.origin, expectedOrigin, "proxy selected another environment");
        const call = { url, init };
        calls.push(call);
        return respond(call);
    };
    const configModule = await transpileConfig({ nextConfigPath: path.join(frontendDir, "next.config.ts"), configFileName: "next.config.ts", cwd: frontendDir });
    const config = configModule.default || configModule;
    const cases = [
        ["unset", undefined, undefined, "http://127.0.0.1:8000", false],
        ["empty", "", "", "http://127.0.0.1:8000", false],
        ["whitespace", " \t\n", " \r\n", "http://127.0.0.1:8000", false],
        ["canonical only", isolated, undefined, isolated, false],
        ["legacy only", undefined, isolated, isolated, false],
        ["empty canonical", "", isolated, isolated, false],
        ["blank canonical", " \t", ` ${isolated}\n`, isolated, false],
        ["blank legacy", ` ${isolated} `, " \t", isolated, false],
        ["same after trimming", ` ${isolated}\n`, `\t${isolated} `, isolated, false],
        ["conflict", ` ${isolated} `, "http://127.0.0.1:19000", isolated, true],
        // Existing URL-joining behavior is intentionally preserved.
        ["trailing slash", `${isolated}/`, undefined, `${isolated}/`, false],
        ["path prefix", `${isolated}/prefix`, undefined, `${isolated}/prefix`, false],
        ["no implicit URL normalization", `${isolated}/`, isolated, `${isolated}/`, true],
    ];
    for (const [label, origin, legacy, expected, conflict] of cases) {
        setEnv(origin, legacy);
        expectedOrigin = new URL(expected).origin;
        calls = []; warnings = [];
        assert.equal(resolveBackendOrigin(), expected, label);
        const rewrites = await config.rewrites();
        assert.deepEqual(rewrites.beforeFiles, []);
        assert.deepEqual(rewrites.afterFiles, []);
        assert.deepEqual(rewrites.fallback, [{ source: "/api/:path*", destination: `${expected}/api/:path*` }]);
        respond = () => new Response("fake");
        // Synthetic ordinary API write through the real rewrite destination.
        await fetch(rewrites.fallback[0].destination.replace(":path*", "config/fake"), { method: "POST", body: "synthetic settings" });
        assert.equal(calls.at(-1).url.href, `${expected}/api/config/fake`);
        const addon = loadRoute("addon/[...path]"), mcp = loadRoute("mcp/[...path]"), events = loadRoute("addon/events");
        assert.equal(warnings.length, conflict ? 5 : 0, label);
        for (const warning of warnings) {
            assert.match(warning, /SAIVERSE_BACKEND_ORIGIN.*SAIVERSE_BACKEND_URL.*using SAIVERSE_BACKEND_ORIGIN/);
            assert.ok(!warning.includes("http"), "warning must not include endpoint values");
        }
        for (const [prefix, route] of [["addon", addon], ["mcp", mcp]]) {
            for (const method of ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]) {
                const payload = !["GET", "HEAD"].includes(method) ? '{"enabled":false,"fake":true}' : undefined;
                const headers = { "content-type": "application/json", cookie: "fake=session", "x-test-header": "retained", range: "bytes=0-3", host: "strip.invalid", "accept-encoding": "gzip", connection: "close" };
                const req = request(`/api/${prefix}/fake%2Fplugin/settings?tag=a&tag=b+two`, method, headers, payload);
                respond = async ({ init }) => {
                    assert.equal(init.method, method);
                    assert.equal(init.redirect, "manual");
                    assert.equal(init.signal, req.signal);
                    for (const key of ["host", "connection", "accept-encoding", "transfer-encoding", "content-length"]) assert.equal(init.headers.get(key), null);
                    assert.equal(init.headers.get("cookie"), "fake=session");
                    assert.equal(init.headers.get("x-test-header"), "retained");
                    assert.equal(init.headers.get("range"), "bytes=0-3");
                    if (payload !== undefined) {
                        assert.equal(init.duplex, "half");
                        assert.equal(await new Response(init.body).text(), payload);
                    } else assert.equal(init.body, undefined);
                    return new Response(method === "HEAD" ? null : "fake", { status: 206, statusText: "Partial Content", headers: {
                        "content-type": "application/octet-stream", "content-range": "bytes 0-3/10", "accept-ranges": "bytes", "content-length": "4", "x-upstream": "retained", "content-encoding": "identity", connection: "close", "keep-alive": "timeout=5", "transfer-encoding": "chunked",
                    } });
                };
                const response = await route[method](req, { params: Promise.resolve({ path: ["fake/plugin", "settings"] }) });
                assert.equal(calls.at(-1).url.pathname, `/api/${prefix}/fake%2Fplugin/settings`, label);
                assert.equal(calls.at(-1).url.search, "?tag=a&tag=b+two");
                assert.equal(response.status, 206);
                assert.equal(response.statusText, "Partial Content");
                for (const [key, value] of [["content-range", "bytes 0-3/10"], ["accept-ranges", "bytes"], ["content-length", "4"], ["x-upstream", "retained"]]) assert.equal(response.headers.get(key), value);
                for (const key of ["content-encoding", "connection", "keep-alive", "transfer-encoding"]) assert.equal(response.headers.get(key), null);
                assert.equal(await response.text(), method === "HEAD" ? "" : "fake");
            }
        }
        const req = request("/api/addon/events", "GET", { "x-forwarded-for": "192.0.2.1" });
        respond = ({ init }) => {
            assert.equal(init.method, "GET"); assert.equal(init.signal, req.signal);
            assert.equal(init.headers.accept, "text/event-stream");
            assert.equal(init.headers["x-forwarded-for"], "192.0.2.1");
            return new Response("data: fake\n\n", { headers: { "content-type": "text/event-stream" } });
        };
        const response = await events.GET(req);
        assert.equal(calls.at(-1).url.pathname, "/api/addon/events");
        assert.equal(response.headers.get("content-type"), "text/event-stream; charset=utf-8");
        assert.equal(response.headers.get("cache-control"), "no-cache, no-transform");
        assert.equal(response.headers.get("x-accel-buffering"), "no");
        assert.equal(await response.text(), "data: fake\n\n");
        console.log(`OK: shared backend origin and all consumers (${label})`);
    }
    const secretWarnings = [];
    resolveBackendOrigin({ SAIVERSE_BACKEND_ORIGIN: "https://fake-user:fake-password@example.invalid/?token=fake-secret", SAIVERSE_BACKEND_URL: "https://other.invalid/?key=other-secret" }, (message) => secretWarnings.push(message));
    assert.equal(secretWarnings.length, 1);
    assert.doesNotMatch(secretWarnings[0], /fake-user|fake-password|example.invalid|fake-secret|other.invalid|other-secret/);
    setEnv(isolated, undefined); expectedOrigin = isolated;
    const addon = loadRoute("addon/[...path]"), mcp = loadRoute("mcp/[...path]"), events = loadRoute("addon/events");
    for (const mediaPath of [["audio", "fake"], ["video", "fake"], ["fake.mp3"]]) {
        respond = ({ init }) => {
            assert.equal(init.headers.get("range"), null, "existing media GET exception");
            return new Response("fake audio", { headers: { "content-type": "audio/mpeg" } });
        };
        const response = await addon.GET(request(`/api/addon/${mediaPath.join("/")}`, "GET", { range: "bytes=0-3" }), { params: Promise.resolve({ path: mediaPath }) });
        assert.equal(await response.text(), "fake audio");
    }
    // First SSE / media-stream chunks must arrive before the upstream closes.
    for (const streamKind of ["events", "media"]) {
        let controller, canceled = false;
        respond = () => new Response(new ReadableStream({
            start(c) { controller = c; c.enqueue(new TextEncoder().encode("first")); },
            cancel() { canceled = true; },
        }), { headers: { "content-type": streamKind === "events" ? "text/event-stream" : "audio/mpeg" } });
        const response = await withTimeout(streamKind === "events" ? events.GET(request("/api/addon/events")) : addon.GET(request("/api/addon/audio/fake/stream"), { params: Promise.resolve({ path: ["audio", "fake", "stream"] }) }));
        const reader = response.body.getReader();
        assert.equal(new TextDecoder().decode((await withTimeout(reader.read())).value), "first");
        controller.enqueue(new TextEncoder().encode("second"));
        assert.equal(new TextDecoder().decode((await withTimeout(reader.read())).value), "second");
        await reader.cancel(); assert.equal(canceled, true);
    }
    for (const [prefix, route] of [["addon", addon], ["mcp", mcp]]) {
        for (const status of [307, 400, 503]) {
            respond = () => new Response("fake error", { status, headers: { location: "/api/fake/" } });
            const response = await route.GET(request(`/api/${prefix}/fake`), { params: Promise.resolve({ path: ["fake"] }) });
            assert.equal(response.status, status); assert.equal(response.headers.get("location"), "/api/fake/");
            assert.equal(await response.text(), "fake error");
        }
    }
    console.error = () => {};
    respond = () => { throw new Error("synthetic network failure"); };
    assert.equal((await addon.GET(request("/api/addon/fake"), { params: Promise.resolve({ path: ["fake"] }) })).status, 502);
    assert.equal((await mcp.GET(request("/api/mcp/fake"), { params: Promise.resolve({ path: ["fake"] }) })).status, 502);
    assert.equal((await events.GET(request("/api/addon/events"))).status, 502);
    respond = () => new Response("synthetic denied", { status: 403 });
    assert.equal((await events.GET(request("/api/addon/events"))).status, 502);
    console.log("OK: credential-safe warning, Range/media, streaming/cancellation, redirects and errors");
}
main().catch((error) => { console.error = realError; console.error(error); process.exitCode = 1; }).finally(() => {
    setEnv(...originalEnv); globalThis.fetch = realFetch; console.warn = realWarn; console.error = realError;
});
