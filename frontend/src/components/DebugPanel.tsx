import { useState, useEffect, useCallback } from 'react';

// 自律稼働デバッグコントローラー (設計: docs/intent/persona_cognition/debug_controller.md)
// タイマーを無視して判断を手動発火し、タイマーを止めて完全手動でペルソナを
// 駆動する。UC-2「割り込みと復帰」等の検証用。
//
// v0.5 改修B (life.md §9.2-2, 2026-07-13): 「自律 Pulse を 1 回」
// (sub_line Pulse 手動発火) と SubLine タイマートグルは、自律行動 v2 で
// 旧 SubLineScheduler (running autonomous Track への連続 Pulse) ごと廃止
// された機能だったため削除した。バックエンド (api/routes/people/debug.py)
// は互換のため no-op のまま残っている。
//
// Track 撤廃 順序① (track_retirement.md §7.4, 2026-08-14): 「メタ判断を 1 回」
// (fire-meta-judgment) も v1 メタ判断の退役で押しても必ず失敗するボタンに
// なったため削除した。自律判断の手動検証は時間割のコマ発火と判断点で行う。

interface DebugPanelProps {
    personaId: string;
}

interface SchedulerStatus {
    autonomy_state: string;
    manual_mode: boolean;
}

const btnStyle: React.CSSProperties = {
    padding: '4px 12px',
    borderRadius: '4px',
    border: '1px solid #555',
    background: 'rgba(120, 120, 120, 0.12)',
    color: 'inherit',
    cursor: 'pointer',
    fontSize: '0.85rem',
};

export default function DebugPanel({ personaId }: DebugPanelProps) {
    const [status, setStatus] = useState<SchedulerStatus | null>(null);
    const [busy, setBusy] = useState(false);
    const [msg, setMsg] = useState<string>('');

    const refresh = useCallback(async () => {
        try {
            const sRes = await fetch(`/api/people/${personaId}/debug/scheduler`);
            if (sRes.ok) setStatus(await sRes.json());
        } catch (e) {
            console.error('[DebugPanel] refresh failed', e);
        }
    }, [personaId]);

    useEffect(() => {
        refresh();
    }, [refresh]);

    const post = async (path: string, body?: object) => {
        setBusy(true);
        setMsg('');
        try {
            const res = await fetch(`/api/people/${personaId}/debug/${path}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: body ? JSON.stringify(body) : undefined,
            });
            const data = await res.json().catch(() => ({}));
            setMsg(data.message || data.detail || (res.ok ? 'OK' : `エラー (${res.status})`));
            await refresh();
        } catch (e) {
            setMsg(`エラー: ${e}`);
        } finally {
            setBusy(false);
        }
    };

    const toggleManual = () => {
        const goingManual = !status?.manual_mode;
        if (goingManual) {
            post('scheduler', { autonomy: false, manual_mode: true });
        } else {
            post('scheduler', { manual_mode: false });
        }
    };

    const rowStyle: React.CSSProperties = {
        display: 'flex',
        alignItems: 'center',
        gap: '0.5rem',
        flexWrap: 'wrap',
    };

    return (
        <div style={{ marginBottom: '1rem' }}>
            <label style={{ display: 'block', fontWeight: 600, marginBottom: '0.35rem' }}>
                🛠 デバッグコントローラー
            </label>
            <div
                style={{
                    padding: '0.75rem',
                    background: 'rgba(100, 100, 100, 0.1)',
                    borderRadius: '6px',
                    display: 'flex',
                    flexDirection: 'column',
                    gap: '0.6rem',
                }}
            >
                {/* 発火: 会話切り上げ */}
                <div style={rowStyle}>
                    <button style={btnStyle} disabled={busy} onClick={() => post('wrap-up-conversation')}>
                        会話を切り上げ (timeout 相当)
                    </button>
                    <span style={{ fontSize: '0.75rem', color: '#888' }}>
                        いまの会話を閉じて会話終了判断を撃つ (会話中でなければ何もしない)
                    </span>
                </div>

                {/* Embedding 生成 */}
                <div style={rowStyle}>
                    <button style={btnStyle} disabled={busy} onClick={() => post('generate-embeddings')}>
                        Embedding 一括生成
                    </button>
                    <span style={{ fontSize: '0.75rem', color: '#888' }}>
                        Chronicle / Memopedia / Fragment の未生成分
                    </span>
                </div>

                {/* タイマー制御 */}
                <div style={{ ...rowStyle, borderTop: '1px solid rgba(255,255,255,0.08)', paddingTop: '0.5rem' }}>
                    <span style={{ fontSize: '0.8rem' }}>
                        Autonomy: <strong>{status?.autonomy_state ?? '?'}</strong>
                    </span>
                    <button
                        style={btnStyle}
                        disabled={busy}
                        onClick={() => post('scheduler', { autonomy: status?.autonomy_state === 'stopped' })}
                    >
                        切替
                    </button>
                </div>

                {/* 完全手動モード */}
                <div style={rowStyle}>
                    <span style={{ fontSize: '0.8rem' }}>
                        完全手動モード: <strong>{status?.manual_mode ? 'ON' : 'OFF'}</strong>
                    </span>
                    <button style={btnStyle} disabled={busy} onClick={toggleManual}>
                        {status?.manual_mode ? '解除' : '全タイマー停止して手動へ'}
                    </button>
                </div>

                {msg && (
                    <div style={{ fontSize: '0.78rem', color: '#9ad', wordBreak: 'break-word' }}>{msg}</div>
                )}
            </div>
        </div>
    );
}
