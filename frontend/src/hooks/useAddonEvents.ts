"use client";
import { parseUIEvent } from '@/i18n/api';


import { useEffect, useRef, useCallback } from 'react';

export interface AddonEvent {
    type: 'addon_event';
    addon: string;
    event: string;
    message_id?: string;
    data?: Record<string, unknown>;
}

/**
 * サーバーの動作中に見つけた、部屋に結びつかない警告 (docs/intent/runtime_alerts.md)。
 * `title` / `message` は `ui_message` の形で届き、`parseUIEvent` が表示用の文に変える。
 */
export interface RuntimeAlert {
    id: number;
    key: string;
    level: 'warning' | 'error';
    title: string;
    message: string;
    details?: Record<string, unknown> | null;
    count: number;
    /** epoch 秒 */
    first_at: number;
    /** epoch 秒 */
    last_at: number;
}

export type RuntimeAlertEvent =
    | { type: 'runtime_alert'; action: 'upsert'; alert: RuntimeAlert }
    | { type: 'runtime_alert'; action: 'dismiss'; id: number };

type AddonEventHandler = (event: AddonEvent) => void;

export interface AddonEventsOptions {
    /** `type: 'runtime_alert'` の知らせの受け手。アドオンの受け手には流さない。 */
    onRuntimeAlert?: (event: RuntimeAlertEvent) => void;
    /**
     * 接続が開いたとき (初回と、切れて繋ぎ直したとき)。この接続は繋がっていない間の
     * 知らせを保持しないので、取りこぼしてはいけない知らせの一覧はここで取り直す。
     */
    onOpen?: () => void;
}

/**
 * 常設 SSE (/api/addon/events) を購読するフック。
 *
 * /api/addon/events に EventSource で接続し続け、サーバーから配信される知らせを
 * `type` で振り分ける: アドオンイベント (`addon_event`) は `onEvent` へ、
 * 動作中の警告 (`runtime_alert`) は `options.onRuntimeAlert` へ。
 * コンポーネントのアンマウント時に自動切断する。
 */
export function useAddonEvents(onEvent: AddonEventHandler, options: AddonEventsOptions = {}): void {
    const onEventRef = useRef(onEvent);
    onEventRef.current = onEvent;
    const onRuntimeAlertRef = useRef(options.onRuntimeAlert);
    onRuntimeAlertRef.current = options.onRuntimeAlert;
    const onOpenRef = useRef(options.onOpen);
    onOpenRef.current = options.onOpen;

    // 接続中のEventSourceを保持（再接続制御用）
    const esRef = useRef<EventSource | null>(null);
    const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

    const connect = useCallback(() => {
        if (esRef.current) {
            esRef.current.close();
        }

        const es = new EventSource('/api/addon/events');
        esRef.current = es;

        es.onopen = () => {
            onOpenRef.current?.();
        };

        es.onmessage = (e) => {
            let data: AddonEvent | RuntimeAlertEvent;
            try {
                data = parseUIEvent(e.data) as AddonEvent | RuntimeAlertEvent;
            } catch {
                // コメント行（keep-alive）は JSON.parse で失敗するが正常
                return;
            }
            try {
                if (data.type === 'addon_event') {
                    onEventRef.current(data);
                } else if (data.type === 'runtime_alert') {
                    onRuntimeAlertRef.current?.(data);
                }
            } catch (error) {
                console.error('[addon-events] event handler failed', data.type, error);
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
