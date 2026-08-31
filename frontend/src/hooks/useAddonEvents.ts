"use client";

import { useEffect, useRef, useCallback } from 'react';

export interface AddonEvent {
    type: 'addon_event';
    addon: string;
    event: string;
    message_id?: string;
    data?: Record<string, unknown>;
}

type AddonEventHandler = (event: AddonEvent) => void;

/**
 * アドオンイベント SSE を購読するフック。
 *
 * /api/addon/events に EventSource で接続し続け、
 * サーバーから配信されるアドオンイベントをハンドラに渡す。
 * コンポーネントのアンマウント時に自動切断する。
 */
export function useAddonEvents(onEvent: AddonEventHandler): void {
    const onEventRef = useRef(onEvent);
    onEventRef.current = onEvent;

    // 接続中のEventSourceを保持（再接続制御用）
    const esRef = useRef<EventSource | null>(null);
    const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    const connect = useCallback(() => {
        if (esRef.current) {
            esRef.current.close();
        }

        const es = new EventSource('/api/addon/events');
        esRef.current = es;

        es.onmessage = (e) => {
            try {
                const data = JSON.parse(e.data) as AddonEvent;
                if (data.type === 'addon_event') {
                    onEventRef.current(data);
                }
            } catch {
                // コメント行（keep-alive）は JSON.parse で失敗するが正常
            }
        };

        es.onerror = () => {
            console.warn('[addon-events] SSE connection error, reconnecting in 5s');
            // エラー時は一定時間後に再接続
            es.close();
            esRef.current = null;
            reconnectTimerRef.current = setTimeout(connect, 5000);
        };
    }, []);

    useEffect(() => {
        connect();
        return () => {
            if (esRef.current) {
                esRef.current.close();
                esRef.current = null;
            }
            if (reconnectTimerRef.current) {
                clearTimeout(reconnectTimerRef.current);
            }
        };
    }, [connect]);
}
