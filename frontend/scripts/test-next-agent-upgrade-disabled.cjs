// Guards against Next.js's interactive agent-upgrade prompt (`Upgrade now / Skip`)
// interrupting `start.bat` (`next build`) or `start-dev.bat` (`next dev`) when a
// security advisory is published after a SAIVerse release.
//
// Next.js 16.4 defaults `experimental.agentUpgrade` to `'security'`, which opens a
// terminal selection menu aimed at coding agents (Claude Code / Codex) and, if run
// in place, dirties `frontend/package.json` / `package-lock.json` so SAIVerse's
// own updater refuses to run. This test loads `frontend/next.config.ts` through
// Next.js's real `loadConfig` (including Zod schema validation and default merging)
// and verifies both directions against Next.js's `nudge.js`.
const assert = require("node:assert/strict");
const path = require("node:path");

const frontendDir = path.resolve(__dirname, "..");
// Next.js's SWC CJS require-hook resolves relative imports in `next.config.ts`
// (`./dev-origins.cjs`) against `process.cwd()` when `tsconfig.json` has `paths`.
process.chdir(frontendDir);
const resolveFromFrontend = (id) => require.resolve(id, { paths: [frontendDir] });

try {
    resolveFromFrontend("next/package.json");
} catch {
    console.log("SKIP: next is not installed (run npm ci in frontend/)");
    process.exit(0);
}

const loadConfig = require(resolveFromFrontend("next/dist/server/config")).default;
const { defaultConfig } = require(resolveFromFrontend("next/dist/server/config-shared"));
const { PHASE_PRODUCTION_BUILD, PHASE_DEVELOPMENT_SERVER } = require(
    resolveFromFrontend("next/dist/shared/lib/constants"),
);
const {
    assessUpgrade,
    getUpgradeContext,
    nudgeUpgrade,
} = require(resolveFromFrontend("next/dist/lib/upgrade/nudge"));

async function main() {
    const savedForceTerminal = process.env.__NEXT_AGENT_UPGRADE_FORCE_TERMINAL_FOR_TESTING;
    const savedRequestedUpgrade = process.env.__NEXT_AGENT_UPGRADE;
    const realFetch = globalThis.fetch;
    const warnings = [];
    const realWarn = console.warn;

    try {
        delete process.env.__NEXT_AGENT_UPGRADE;
        process.env.__NEXT_AGENT_UPGRADE_FORCE_TERMINAL_FOR_TESTING = "1";
        globalThis.fetch = async (input) => {
            throw new Error(`unexpected network request during upgrade-nudge check: ${String(input)}`);
        };
        console.warn = (...args) => {
            warnings.push(args.map(String).join(" "));
        };

        // Positive control: confirm Next.js still defaults `agentUpgrade` to `'security'`
        // and that `assessUpgrade` still produces a security reminder under the test flag.
        // If Next.js renames or moves this mechanism, fail loudly instead of passing by accident.
        assert.equal(defaultConfig.experimental.agentUpgrade, "security");
        const activeReminder = await assessUpgrade(frontendDir, defaultConfig);
        assert.ok(activeReminder && activeReminder.kind === "security");

        // Actual config check across both `next build` and `next dev` phases.
        for (const phase of [PHASE_PRODUCTION_BUILD, PHASE_DEVELOPMENT_SERVER]) {
            const loaded = await loadConfig(phase, frontendDir);
            assert.equal(loaded.experimental.agentUpgrade, false);

            const context = getUpgradeContext(loaded);
            assert.equal(context.configuredPolicy, false);
            assert.equal(context.experimental.agentUpgrade, false);

            const reminder = await assessUpgrade(frontendDir, loaded);
            assert.equal(reminder, null);

            const controller = new AbortController();
            const action = await nudgeUpgrade(
                frontendDir,
                context,
                phase === PHASE_PRODUCTION_BUILD ? "build" : "dev",
                controller.signal,
                null,
                null,
            );
            assert.equal(action, undefined);
        }

        const invalidConfigWarnings = warnings.filter((line) =>
            /Invalid next\.config|agentUpgrade/i.test(line),
        );
        assert.deepEqual(invalidConfigWarnings, []);
    } finally {
        console.warn = realWarn;
        globalThis.fetch = realFetch;
        if (savedForceTerminal === undefined) {
            delete process.env.__NEXT_AGENT_UPGRADE_FORCE_TERMINAL_FOR_TESTING;
        } else {
            process.env.__NEXT_AGENT_UPGRADE_FORCE_TERMINAL_FOR_TESTING = savedForceTerminal;
        }
        if (savedRequestedUpgrade === undefined) {
            delete process.env.__NEXT_AGENT_UPGRADE;
        } else {
            process.env.__NEXT_AGENT_UPGRADE = savedRequestedUpgrade;
        }
    }

    console.log("OK: next.config.ts disables experimental.agentUpgrade for build and dev");
}

main().catch((err) => {
    console.error("FAIL: test-next-agent-upgrade-disabled.cjs", err);
    process.exit(1);
});
