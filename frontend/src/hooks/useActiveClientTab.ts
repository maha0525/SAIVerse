"use client";

import { useEffect, useState, useRef, useCallback } from "react";

/**
 * アクティブクライアントタブ追跡フック。
 *
 * 「最後にユーザー操作があったタブ」をアクティブクライアントとして扱う。
 * 同一ブラウザ内の複数タブは BroadcastChannel で最終操作時刻を共有し、
 * 最新時刻を持つタブだけが ``isActive: true`` を返す。
 *
 * 異なるブラウザ / 異なる端末は BroadcastChannel で同期できないため、
 * それぞれ独立に「最後に触ったタブ」が存在しうる (Tailscale 越しの
 * スマホと PC Chrome が両方アクティブになりうる。これは意図的な仕様)。
 *
 * 初期状態: タブ生成時刻をそのタブの擬似的な最終操作時刻として用いる。
 * これにより、ユーザーが明示的に何かを触るより前でも、直近に開いた /
 * フォーカスしたタブがアクティブ扱いになる。
 */
const CHANNEL_NAME = "saiverse:active-client-tab";

type InteractionMessage = {
    type: "interaction";
    tabId: string;
    ts: number;
};

function randomTabId(): string {
    return `tab-${Math.random().toString(36).slice(2, 10)}-${Date.now().toString(36)}`;
}

export function useActiveClientTab(): { isActive: boolean; tabId: string } {
    const tabIdRef = useRef<string>("");
    if (tabIdRef.current === "") {
        tabIdRef.current = randomTabId();
    }
    const tabId = tabIdRef.current;

    // 自タブの最終操作時刻
    const myLastTsRef = useRef<number>(Date.now());
    // ブラウザ内の他タブの最新操作時刻（最新者が誰かを判定するため）
    const otherLastTsRef = useRef<number>(0);
    const otherTabIdRef = useRef<string>("");

    const [isActive, setIsActive] = useState<boolean>(true);
    const channelRef = useRef<BroadcastChannel | null>(null);

    const recompute = useCallback(() => {
        // 自タブの時刻が他タブの最新より新しければアクティブ
        const active = myLastTsRef.current >= otherLastTsRef.current;
        setIsActive((prev) => (prev !== active ? active : prev));
    }, []);

    const broadcast = useCallback((ts: number) => {
        const ch = channelRef.current;
        if (!ch) return;
        try {
            const msg: InteractionMessage = { type: "interaction", tabId, ts };
            ch.postMessage(msg);
        } catch {
            // 閉じたチャネル等は無視
        }
    }, [tabId]);

    const markInteraction = useCallback(() => {
        const now = Date.now();
        myLastTsRef.current = now;
        broadcast(now);
        recompute();
    }, [broadcast, recompute]);

    useEffect(() => {
        if (typeof window === "undefined") return;
        if (typeof BroadcastChannel === "undefined") {
            // 未対応ブラウザでは同一ブラウザ内の排他なしで、常に自タブがアクティブ扱い
            setIsActive(true);
            return;
        }

        const ch = new BroadcastChannel(CHANNEL_NAME);
        channelRef.current = ch;

        ch.onmessage = (ev) => {
            const msg = ev.data as InteractionMessage | undefined;
            if (!msg || msg.type !== "interaction" || msg.tabId === tabId) return;
            if (msg.ts > otherLastTsRef.current) {
                otherLastTsRef.current = msg.ts;
                otherTabIdRef.current = msg.tabId;
            }
            recompute();
        };

        // 初期プレゼンス通知（= タブが開かれたこと自体を擬似的な操作として扱う）
        broadcast(myLastTsRef.current);

        return () => {
            ch.close();
            channelRef.current = null;
        };
    }, [tabId, broadcast, recompute]);

    useEffect(() => {
        if (typeof window === "undefined") return;

        const events: (keyof WindowEventMap)[] = [
            "click",
            "keydown",
            "touchstart",
            "pointerdown",
        ];
        const handler = () => markInteraction();
        for (const e of events) {
            window.addEventListener(e, handler, { passive: true });
        }
        const visHandler = () => {
            if (document.visibilityState === "visible") {
                markInteraction();
            }
        };
        document.addEventListener("visibilitychange", visHandler);

        return () => {
            for (const e of events) {
                window.removeEventListener(e, handler);
            }
            document.removeEventListener("visibilitychange", visHandler);
        };
    }, [markInteraction]);

    return { isActive, tabId };
}
