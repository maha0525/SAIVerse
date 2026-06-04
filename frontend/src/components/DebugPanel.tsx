import { useState, useEffect, useCallback } from 'react';

// 自律稼働デバッグコントローラー (設計: docs/intent/persona_cognition/debug_controller.md)
// タイマーを無視してメタ判断 / 自律 Pulse を手動発火し、タイマーを止めて
// 完全手動でペルソナを駆動する。UC-2「割り込みと復帰」等の検証用。

interface DebugPanelProps {
    personaId: string;
}

interface SchedulerStatus {
    subline_running: boolean;
    autonomy_state: string;
    manual_mode: boolean;
}

interface RunningTrack {
    track_id: string;
    title: string | null;
    track_type: string;
    status: string;
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
    const [tracks, setTracks] = useState<RunningTrack[]>([]);
    const [selectedTrack, setSelectedTrack] = useState<string>('');
    const [force, setForce] = useState(false);
    const [busy, setBusy] = useState(false);
    const [msg, setMsg] = useState<string>('');

    const refresh = useCallback(async () => {
        try {
            const [sRes, tRes] = await Promise.all([
                fetch(`/api/people/${personaId}/debug/scheduler`),
                fetch(`/api/people/${personaId}/tracks?status=running`),
            ]);
            if (sRes.ok) setStatus(await sRes.json());
            if (tRes.ok) {
                const data = await tRes.json();
                const auto: RunningTrack[] = (data.items || []).filter(
                    (t: RunningTrack) => t.track_type === 'autonomous'
                );
                setTracks(auto);
                setSelectedTrack((prev) =>
                    prev && auto.some((t) => t.track_id === prev)
                        ? prev
                        : auto[0]?.track_id ?? ''
                );
            }
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
            post('scheduler', { subline: false, autonomy: false, manual_mode: true });
        } else {
            post('scheduler', { subline: true, manual_mode: false });
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
                {/* 発火: メタ判断 */}
                <div style={rowStyle}>
                    <button style={btnStyle} disabled={busy} onClick={() => post('fire-meta-judgment', { force })}>
                        メタ判断を 1 回
                    </button>
                    <label style={{ display: 'flex', alignItems: 'center', gap: '0.3rem', fontSize: '0.8rem' }}>
                        <input type="checkbox" checked={force} onChange={(e) => setForce(e.target.checked)} />
                        force (抑止無視)
                    </label>
                </div>

                {/* 発火: 自律 Pulse */}
                <div style={rowStyle}>
                    <button
                        style={btnStyle}
                        disabled={busy || !selectedTrack}
                        onClick={() => post('fire-subline-pulse', { track_id: selectedTrack })}
                    >
                        自律 Pulse を 1 回
                    </button>
                    <select
                        value={selectedTrack}
                        onChange={(e) => setSelectedTrack(e.target.value)}
                        style={{
                            padding: '2px 6px',
                            borderRadius: '4px',
                            border: '1px solid #444',
                            background: 'transparent',
                            color: 'inherit',
                            maxWidth: '14rem',
                        }}
                    >
                        {tracks.length === 0 ? (
                            <option value="">(running な autonomous Track なし)</option>
                        ) : (
                            tracks.map((t) => (
                                <option key={t.track_id} value={t.track_id}>
                                    {t.title || t.track_id.slice(0, 8)}
                                </option>
                            ))
                        )}
                    </select>
                </div>

                {/* 発火: 会話切り上げ */}
                <div style={rowStyle}>
                    <button style={btnStyle} disabled={busy} onClick={() => post('wrap-up-conversation')}>
                        会話を切り上げ (timeout 相当)
                    </button>
                    <span style={{ fontSize: '0.75rem', color: '#888' }}>
                        running の対話 Track を pause → メタ判断
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
                        SubLine: <strong>{status?.subline_running ? 'ON' : 'OFF'}</strong>
                    </span>
                    <button style={btnStyle} disabled={busy} onClick={() => post('scheduler', { subline: !status?.subline_running })}>
                        切替
                    </button>
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
