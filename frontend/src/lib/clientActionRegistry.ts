/**
 * Client action executor registry.
 *
 * アドオンが `ui_extensions.client_actions` で宣言した action 名 → 実行関数の
 * マッピングを保持する。新しい action 型を追加するには、ここに executor を
 * register するだけでよい。
 *
 * 各 executor は失敗時に Error を throw する。呼び出し側 (useClientActions)
 * が catch して on_failure_endpoint への POST 等を担当する。
 */
import type { AddonClientAction } from "@/types/addon";

export type ClientActionContext = {
    /** addon 名 (発火元 addon) */
    addonName: string;
    /** SSE イベント (type=addon_event) */
    event: {
        addon: string;
        event: string;
        message_id?: string;
        data?: Record<string, unknown>;
    };
    /** `client_actions` 宣言本体 (id, source_metadata_key 等) */
    action: AddonClientAction;
    /** addon の現在の params (global のみ) */
    params: Record<string, unknown>;
    /** 当該メッセージに紐づく addon metadata (addonMetadata[addon_name]) */
    metadata: Record<string, unknown>;
};

export type ClientActionExecutor = (ctx: ClientActionContext) => Promise<void>;

const registry = new Map<string, ClientActionExecutor>();

export function registerClientActionExecutor(
    name: string,
    executor: ClientActionExecutor,
): void {
    registry.set(name, executor);
}

export function getClientActionExecutor(
    name: string,
): ClientActionExecutor | undefined {
    return registry.get(name);
}

export function listClientActionExecutors(): string[] {
    return Array.from(registry.keys());
}

/**
 * executor が metadata / event.data のどちらかから値を解決するヘルパ。
 * event.data は action 発火時点のフレッシュな値、addon metadata は
 * 過去メッセージのリハイドレートで使われる永続値。両方試してヒットした
 * 値を返す。
 */
export function resolveActionValue(
    ctx: ClientActionContext,
    key: string | undefined,
): unknown {
    if (!key) return undefined;
    const fromEvent = ctx.event.data?.[key];
    if (fromEvent !== undefined) return fromEvent;
    return ctx.metadata?.[key];
}
