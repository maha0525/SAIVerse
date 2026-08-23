import React from 'react';
import styles from './ContextVolumeBar.module.css';

// 提示コンテキストの現在量と水位 (GET /api/people/{id}/context-status, read-only)。
// チャットオプションの「データ送信量の管理」と Chronicle 生成の確認窓が共有する。
export interface ContextStatus {
    persona_id: string;
    model: string | null;
    metabolism: boolean;              // 実効モデルが水位を持つか
    low_chars: number | null;         // 最初に読み込む量
    target_chars: number | null;      // 残す量 (整理後にここへ揃える)
    high_chars: number | null;        // 上限 (超えたら整理)
    presented_chars: number | null;   // 現在の提示コンテキスト文字数 (読み戻し後)
    refill_applied: boolean;
    measurement_failed: boolean;      // 計測失敗 (null を「起点なし」と読ませない)
}

interface ContextVolumeBarProps {
    status: ContextStatus;
}

/**
 * 横棒を描けるか (現在量と目盛りの上限が両方そろっているか)。
 *
 * ContextVolumeBar は描けないとき null を返すが、JSX 要素は null を返す
 * コンポーネントでも truthy なので、呼び出し側が「棒か説明文か」を分岐する
 * にはこの述語を使う。
 */
export function canDrawContextVolumeBar(status: ContextStatus): boolean {
    return status.presented_chars != null && scaleMaxOf(status) != null;
}

// バーの右端 = 上限。上限なし (文字数では整理しない) モデルは残す量の 2 倍を目安に描く。
function scaleMaxOf(status: ContextStatus): number | null {
    return status.high_chars ?? (status.target_chars != null ? status.target_chars * 2 : null);
}

/**
 * 今の量 / 残す量 (target) / 上限 (high) の横棒 + 数値行。
 *
 * 現在量か目盛りの上限が決められないとき (起点未確立・計測失敗・水位なし) は
 * null を返す — その場合の説明文は、置き場所ごとに文言が違うので呼び出し側が出す。
 */
export default function ContextVolumeBar({ status }: ContextVolumeBarProps) {
    const presented = status.presented_chars;
    const target = status.target_chars;
    const high = status.high_chars;
    const scaleMax = scaleMaxOf(status);
    if (presented == null || scaleMax == null) return null;

    return (
        <>
            <div className={styles.contextBar}>
                <div
                    className={styles.contextBarFill}
                    style={{
                        width: `${Math.min(100, (presented / scaleMax) * 100)}%`,
                        background: high != null && presented > high ? '#f87171'
                            : target != null && presented > target ? '#fbbf24'
                            : '#34d399',
                    }}
                />
                {target != null && target <= scaleMax && (
                    <div className={styles.contextBarMarker} style={{ left: `${(target / scaleMax) * 100}%` }} />
                )}
            </div>
            <div className={styles.contextStatRow}>
                <span>現在 {presented.toLocaleString()}文字{status.refill_applied ? '（読み戻し後）' : ''}</span>
                <span>
                    残す量 {target != null ? `${target.toLocaleString()}文字` : '—'} ／
                    上限 {high != null ? `${high.toLocaleString()}文字` : 'なし'}
                </span>
            </div>
        </>
    );
}
