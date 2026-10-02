// Optional boundary check: real Next.js server -> fake backend on 18000.
// Requires a build with SAIVERSE_BACKEND_ORIGIN=http://127.0.0.1:18000.
// Refuses mismatching builds / occupied ports; never reuses a running backend.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const http = require("node:http");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { once } = require("node:events");
const frontendDir = path.resolve(__dirname, "..");
const origin = "http://127.0.0.1:18000";
const uiOrigin = "http://127.0.0.1:18010";
const seen = [];
let next, logs = "";
const backend = http.createServer(async (req, res) => {
    let body = "";
    for await (const chunk of req) body += chunk;
    seen.push({ method: req.method, url: req.url, headers: req.headers, body });
    res.setHeader("x-fake-backend", "isolated-18000");
    if (req.url === "/api/addon/events" || req.url === "/api/addon/audio/fake/stream") {
        res.setHeader("content-type", req.url.endsWith("events") ? "text/event-stream" : "audio/mpeg");
        res.write("first\n\n");
        const timer = setTimeout(() => res.end("second\n\n"), 1500);
        res.on("close", () => clearTimeout(timer));
    } else if (req.url === "/api/addon/fake.bin") {
        res.writeHead(206, { "content-type": "application/octet-stream", "content-range": "bytes 0-3/10", "accept-ranges": "bytes", "content-length": "4" });
        res.end("fake");
    } else if (req.url === "/api/addon/audio/fake.mp3") {
        res.writeHead(200, { "content-type": "audio/mpeg" });
        res.end("fake audio");
    } else {
        res.writeHead(req.method === "POST" ? 201 : 200, { "content-type": "application/json" });
        res.end(JSON.stringify(seen.at(-1)));
    }
});
async function main() {
    const manifest = JSON.parse(fs.readFileSync(path.join(frontendDir, ".next/routes-manifest.json"), "utf8"));
    assert.deepEqual(manifest.rewrites.beforeFiles, []);
    assert.deepEqual(manifest.rewrites.afterFiles, []);
    assert.equal(manifest.rewrites.fallback.length, 1);
    assert.equal(manifest.rewrites.fallback[0].destination, `${origin}/api/:path*`, "Build must point only at the isolated fake backend");
    // Bind both ports ourselves first. EADDRINUSE fails before any request.
    await new Promise((resolve, reject) => { backend.once("error", reject); backend.listen(18000, "127.0.0.1", resolve); });
    const probe = http.createServer();
    await new Promise((resolve, reject) => { probe.once("error", reject); probe.listen(18010, "127.0.0.1", resolve); });
    await new Promise((resolve) => probe.close(resolve));
    next = spawn(process.execPath, [require.resolve("next/dist/bin/next"), "start", "-H", "127.0.0.1", "-p", "18010"], {
        cwd: frontendDir,
        env: { ...process.env, SAIVERSE_BACKEND_ORIGIN: origin, SAIVERSE_BACKEND_URL: "", NEXT_TELEMETRY_DISABLED: "1" },
        stdio: ["ignore", "pipe", "pipe"],
    });
    await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error(`Next.js did not start: ${logs}`)), 30000);
        const consume = (chunk) => { logs += chunk; if (/Ready in/.test(logs)) { clearTimeout(timer); resolve(); } };
        next.stdout.on("data", consume); next.stderr.on("data", consume);
        next.once("error", (error) => { clearTimeout(timer); reject(error); });
        next.once("exit", (code) => { clearTimeout(timer); reject(new Error(`Next.js exited (${code}): ${logs}`)); });
    });
    for (const prefix of ["config", "addon", "mcp"]) {
        for (const method of ["GET", "POST"]) {
            const body = method === "POST" ? '{"enabled":false,"fake":true}' : undefined;
            const response = await fetch(`${uiOrigin}/api/${prefix}/fake?tag=a&tag=b+two`, { method, body, headers: { "content-type": "application/json", cookie: "fake=session" }, signal: AbortSignal.timeout(10000) });
            assert.equal(response.status, method === "POST" ? 201 : 200);
            assert.equal(response.headers.get("x-fake-backend"), "isolated-18000");
            const recorded = await response.json();
            const upstream = new URL(recorded.url, origin);
            assert.equal(upstream.pathname, `/api/${prefix}/fake`);
            assert.deepEqual(upstream.searchParams.getAll("tag"), ["a", "b two"]);
            // Next rewrites encode the space as %20; custom routes preserve +.
            if (prefix !== "config") assert.equal(upstream.search, "?tag=a&tag=b+two");
            assert.equal(recorded.method, method);
            assert.equal(recorded.headers.cookie, "fake=session");
            assert.equal(recorded.body, body || "");
        }
    }
    const range = await fetch(`${uiOrigin}/api/addon/fake.bin`, { headers: { range: "bytes=0-3" } });
    assert.equal(seen.at(-1).headers.range, "bytes=0-3");
    assert.equal(range.status, 206); assert.equal(range.headers.get("content-range"), "bytes 0-3/10");
    assert.equal(range.headers.get("accept-ranges"), "bytes"); assert.equal(await range.text(), "fake");
    const media = await fetch(`${uiOrigin}/api/addon/audio/fake.mp3`, { headers: { range: "bytes=0-3" } });
    assert.equal(seen.at(-1).headers.range, undefined);
    assert.equal(media.status, 200); assert.equal(await media.text(), "fake audio");
    for (const endpoint of ["/api/addon/events", "/api/addon/audio/fake/stream"]) {
        const response = await fetch(`${uiOrigin}${endpoint}`, { signal: AbortSignal.timeout(10000) });
        const reader = response.body.getReader();
        const first = await reader.read();
        assert.equal(new TextDecoder().decode(first.value), "first\n\n", "first chunk must arrive before completion");
        const second = await reader.read();
        assert.equal(new TextDecoder().decode(second.value), "second\n\n");
        assert.equal((await reader.read()).done, true);
    }
    assert.equal(seen.length, 10);
    console.log("OK: real Next.js -> fake 18000: ordinary/addon/MCP reads+writes, query/cookie, Range/206, media and SSE/stream chunks (10 requests)");
}
main().catch((error) => { console.error(error); process.exitCode = 1; }).finally(async () => {
    if (next && next.exitCode === null && next.signalCode === null) { const exited = once(next, "exit"); next.kill("SIGTERM"); await exited; }
    backend.closeAllConnections();
    if (backend.listening) await new Promise((resolve) => backend.close(resolve));
});
