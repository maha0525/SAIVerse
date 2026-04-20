"use client";

import { useCallback, useEffect, useRef } from "react";

import { playAudioExecutor } from "@/lib/clientActions/playAudio";
import {
    getClientActionExecutor,
    registerClientActionExecutor,
} from "@/lib/clientActionRegistry";
import type { AddonClientAction, AddonInfo } from "@/types/addon";

// モジュールロード時に初期 executor を registry へ登録（他の action を
// 追加するときは registerClientActionExecutor を同じように呼ぶだけでよい）。
registerClientActionExecutor("play_audio", playAudioExecutor);

type AddonEventPayload = {
    addon: string;
    event: string;
    message_id?: string;
    data?: Record<string, unknown>;
};

type ClientActionEntry = {
    addon: AddonInfo;
    action: AddonClientAction;
};

type NotifyFailureFn = (
    addonName: string,
    endpoint: string,
    payload: Record<string, unknown>,
) => void;

const notifyFailure: NotifyFailureFn = (addonName, endpoint, payload) => {
    const path = endpoint.startsWith("/")
        ? endpoint
        : `/api/addon/${addonName}/${endpoint}`;
    try {
        void fetch(path, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify(payload),
        }).catch((err) => {
            console.warn("[client-actions] failure-endpoint POST failed:", err);
        });
    } catch (err) {
        console.warn("[client-actions] failure-endpoint POST threw:", err);
    }
};

/**
 * アドオン `client_actions` をディスパッチするフック。
 *
 * 返り値の `dispatch(event)` を SSE 受信ハンドラから呼ぶと、
 * マッチする client_actions を条件評価して executor を実行する。
 *
 * - `isActiveTab`: アクティブクライアントタブかどうか
 * - `getAddonMetadata`: message_id → addonMetadata を返すルックアップ関数
 *
 * addon 一覧は /api/addon/ から取得してキャッシュし、定期的に再取得する。
 */
export function useClientActions(params: {
    isActiveTab: boolean;
    getAddonMetadata: (
        messageId: string | undefined,
        addonName: string,
    ) => Record<string, unknown>;
}): {
    dispatch: (event: AddonEventPayload) => void;
} {
    const { isActiveTab, getAddonMetadata } = params;

    const addonsRef = useRef<AddonInfo[]>([]);
    const isActiveRef = useRef(isActiveTab);
    isActiveRef.current = isActiveTab;

    // /api/addon/ を定期的に fetch して、有効アドオンと client_actions を
    // 最新化する。params も同時に取れる。
    useEffect(() => {
        let cancelled = false;
        const refresh = async () => {
            try {
                const r = await fetch("/api/addon/");
                if (!r.ok) return;
                const data = (await r.json()) as AddonInfo[];
                if (!cancelled) addonsRef.current = data;
            } catch {
                // 失敗は無視（次回 refresh で拾う）
            }
        };
        void refresh();
        const interval = setInterval(refresh, 30_000);
        return () => {
            cancelled = true;
            clearInterval(interval);
        };
    }, []);

    const dispatch = useCallback(
        (event: AddonEventPayload) => {
            // 当該 addon の宣言から、event 名がマッチする action を抽出
            const matches: ClientActionEntry[] = [];
            for (const addon of addonsRef.current) {
                if (!addon.is_enabled) continue;
                if (addon.addon_name !== event.addon) continue;
                const actions = addon.ui_extensions?.client_actions ?? [];
                for (const action of actions) {
                    if (action.event !== event.event) continue;
                    matches.push({ addon, action });
                }
            }
            if (matches.length === 0) return;

            for (const entry of matches) {
                const { addon, action } = entry;

                // 条件チェック: requires_enabled_param
                if (action.requires_enabled_param) {
                    const enabled = addon.params[action.requires_enabled_param];
                    if (!enabled) continue;
                }

                // 条件チェック: requires_active_tab
                if (action.requires_active_tab && !isActiveRef.current) {
                    continue;
                }

                const executor = getClientActionExecutor(action.action);
                if (!executor) {
                    console.warn(
                        `[client-actions] executor not found for action=${action.action}`,
                    );
                    continue;
                }

                const metadata = getAddonMetadata(event.message_id, addon.addon_name);

                const runAndReport = async () => {
                    try {
                        await executor({
                            addonName: addon.addon_name,
                            event,
                            action,
                            params: addon.params,
                            metadata,
                        });
                    } catch (err) {
                        const reason = err instanceof Error ? err.message : String(err);
                        console.error(
                            `[client-actions] action ${action.id} (${action.action}) failed:`,
                            reason,
                        );
                        if (action.on_failure_endpoint) {
                            notifyFailure(addon.addon_name, action.on_failure_endpoint, {
                                action_id: action.id,
                                event: action.event,
                                error_reason: reason,
                                message_id: event.message_id,
                            });
                        }
                    }
                };
                void runAndReport();
            }
        },
        [getAddonMetadata],
    );

    return { dispatch };
}
