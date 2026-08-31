import { useState } from 'react';

// デバッグコントローラー (設計: docs/intent/persona_cognition/debug_controller.md)
// ペルソナ設定から手で叩ける保守操作の置き場。
//
// v0.5 改修B (life.md §9.2-2, 2026-07-13): 「自律 Pulse を 1 回」
// (sub_line Pulse 手動発火) と SubLine タイマートグルは、自律行動 v2 で
// 旧 SubLineScheduler (running autonomous Track への連続 Pulse) ごと廃止
// された機能だったため削除した。
//
// Track 撤廃 順序① (track_retirement.md §7.4, 2026-08-14): 「メタ判断を 1 回」
// (fire-meta-judgment) も v1 メタ判断の退役で押しても必ず失敗するボタンに
// なったため削除した。
//
// 2026-08-23 (まはーの実機検証): 残っていた 3 つ —— 「会話を切り上げ」
// 「Autonomy 切替」「完全手動モード」—— も撤去した。会話終了判断は v3 で
// 退役し (autonomous_behavior_v3.md §8/§13.3)、会話は沈黙タイマーだけで
// 閉じる。Autonomy 系は v0.3 の止め具
// (saiverse/autonomy_wiring.py の AUTONOMOUS_DRIVING_SHIPPED = False) で
// 判断点・見張り・コマが発火しないため、切り替えても効果が無かった。

interface DebugPanelProps {
    personaId: string;
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
    const [busy, setBusy] = useState(false);
    const [msg, setMsg] = useState<string>('');

    const post = async (path: string) => {
        setBusy(true);
        setMsg('');
        try {
            const res = await fetch(`/api/people/${personaId}/debug/${path}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
            });
            const data = await res.json().catch(() => ({}));
            setMsg(data.message || data.detail || (res.ok ? 'OK' : `エラー (${res.status})`));
        } catch (e) {
            setMsg(`エラー: ${e}`);
        } finally {
            setBusy(false);
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
                {/* Embedding 生成 */}
                <div style={rowStyle}>
                    <button style={btnStyle} disabled={busy} onClick={() => post('generate-embeddings')}>
                        Embedding 一括生成
                    </button>
                    <span style={{ fontSize: '0.75rem', color: '#888' }}>
                        Chronicle / Memopedia / Fragment の未生成分
                    </span>
                </div>

                {msg && (
                    <div style={{ fontSize: '0.78rem', color: '#9ad', wordBreak: 'break-word' }}>{msg}</div>
                )}
            </div>
        </div>
    );
}
