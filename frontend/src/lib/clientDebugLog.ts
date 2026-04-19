/**
 * Client-side debug log relay.
 *
 * `console.log` の代わりにサーバーへ POST してバックエンドログに記録する。
 * モバイルブラウザで DevTools が手軽に使えない状況で、backend.log を
 * tail するだけで挙動を追えるようにするための debug utility。
 *
 * fire-and-forget で POST する (失敗は ignore)。console にも併記する
 * ので、DevTools が使える環境では両方で確認できる。
 */
export type ClientDebugLevel = "debug" | "info" | "warn" | "error";

const ENDPOINT = "/api/client_debug/log";

export function clientDebugLog(
    level: ClientDebugLevel,
    source: string,
    message: string,
    context?: Record<string, unknown>,
): void {
    // Console にも出す (DevTools 利用可の環境向け)
    const prefix = `[${source}]`;
    if (level === "error") {
        console.error(prefix, message, context ?? "");
    } else if (level === "warn") {
        console.warn(prefix, message, context ?? "");
    } else if (level === "debug") {
        console.debug(prefix, message, context ?? "");
    } else {
        console.log(prefix, message, context ?? "");
    }

    // Backend に fire-and-forget
    try {
        void fetch(ENDPOINT, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ level, source, message, context }),
        }).catch(() => {
            // ログ送信失敗は無視 (ログ用途なので再試行しない)
        });
    } catch {
        // no-op
    }
}
