// Server-side connection setting shared by Next.js rewrites and Route Handlers.
// Keep URL joining in the callers: selecting an environment must not change
// existing path, streaming, or media proxy behavior.
const DEFAULT_BACKEND_ORIGIN = "http://127.0.0.1:8000";

/**
 * @param {{ SAIVERSE_BACKEND_ORIGIN?: string, SAIVERSE_BACKEND_URL?: string }} [env]
 * @param {(message: string) => void} [warn]
 * @returns {string}
 */
function resolveBackendOrigin(env = process.env, warn = console.warn) {
    const origin = env.SAIVERSE_BACKEND_ORIGIN?.trim();
    const legacy = env.SAIVERSE_BACKEND_URL?.trim();
    if (origin && legacy && origin !== legacy) {
        // Do not print either value: URLs may contain credentials or tokens.
        warn(
            "[backend-origin] SAIVERSE_BACKEND_ORIGIN and deprecated " +
            "SAIVERSE_BACKEND_URL differ; using SAIVERSE_BACKEND_ORIGIN.",
        );
    }
    return origin || legacy || DEFAULT_BACKEND_ORIGIN;
}

module.exports = { resolveBackendOrigin };
