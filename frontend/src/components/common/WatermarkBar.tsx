import React from 'react';
import styles from './WatermarkBar.module.css';

// 記憶の整理の二水位 (整理後に残す量 / 整理をはじめる量) を一本の横棒に目印として
// 並べる、表示専用の部品。値の編集は呼び出し側が数値欄で行い、ここは渡された値を
// 描き直すだけ。(旧「最初に読み込む量」= 低水位は 2026-09-04 廃止 — 初期読み込みは
// 残す量を流用する)
//
// ChatOptions の ContextVolumeBar (現在量 vs 残す量/上限) とは役目が違う —
// あちらは「今どこまで溜まっているか」、こちらは「水位が正しい順に並んで
// いるか」を見せる。共通化せず、見た目 (高さ・角丸・グレー地) だけ揃えてある。

export interface WatermarkBarValues {
    target: number | null;
    high: number | null;
}

interface WatermarkBarProps {
    values: WatermarkBarValues;
    /** 目盛りの右端に含めたい追加の量 (例: 現在の提示文字数)。省略可。 */
    extraMax?: number | null;
    /** 順序が崩れている目印のキー (赤く塗る)。 */
    invalidKeys?: ReadonlySet<keyof WatermarkBarValues>;
    /** 目印の呼び名。省略すると会話の整理 (Metabolism) の言い方。 */
    labels?: Record<keyof WatermarkBarValues, string>;
}

export const WATERMARK_LABELS: Record<keyof WatermarkBarValues, string> = {
    target: '整理後に残す量',
    high: '整理をはじめる量',
};

/** 知覚 (部屋の様子などの記録) の二水位の呼び名。同じ棒を別の量に使うときに渡す。 */
export const PERCEPTION_WATERMARK_LABELS: Record<keyof WatermarkBarValues, string> = {
    target: '省略した後に残す量',
    high: '省略をはじめる量',
};

/** 目標 ≤ 高 を破っている目印を返す (null は比較しない)。 */
export function findWatermarkOrderViolations(values: WatermarkBarValues): Set<keyof WatermarkBarValues> {
    const bad = new Set<keyof WatermarkBarValues>();
    const { target, high } = values;
    if (target != null && high != null && target > high) { bad.add('target'); bad.add('high'); }
    return bad;
}

/** 目盛りの右端 = max(水位, extraMax) × 1.1。全部 null なら描けない (null)。 */
export function watermarkScaleMax(values: WatermarkBarValues, extraMax?: number | null): number | null {
    const candidates = [values.target, values.high, extraMax ?? null]
        .filter((v): v is number => v != null && v > 0);
    if (candidates.length === 0) return null;
    return Math.max(...candidates) * 1.1;
}

export default function WatermarkBar({ values, extraMax, invalidKeys, labels }: WatermarkBarProps) {
    const scaleMax = watermarkScaleMax(values, extraMax);
    if (scaleMax == null) return null;
    const bad = invalidKeys ?? findWatermarkOrderViolations(values);
    const keys: Array<keyof WatermarkBarValues> = ['target', 'high'];
    const names = labels ?? WATERMARK_LABELS;

    return (
        <div className={styles.wrapper}>
            <div className={styles.bar}>
                {/* 残す量までの帯を薄く塗る (順序が正しいときだけ意味を持つ) */}
                {values.target != null && (
                    <div className={styles.bandTarget} style={{ width: `${(values.target / scaleMax) * 100}%` }} />
                )}
                {keys.map(key => {
                    const v = values[key];
                    if (v == null) return null;
                    return (
                        <div
                            key={key}
                            className={`${styles.marker} ${bad.has(key) ? styles.markerBad : ''}`}
                            style={{ left: `${Math.min(100, (v / scaleMax) * 100)}%` }}
                            title={`${names[key]} ${v.toLocaleString()} 字`}
                        />
                    );
                })}
            </div>
            <div className={styles.legend}>
                {keys.map(key => {
                    const v = values[key];
                    return (
                        <span key={key} className={`${styles.legendItem} ${bad.has(key) ? styles.legendBad : ''}`}>
                            <span className={styles.legendDot} />
                            {names[key]} {v != null ? `${v.toLocaleString()} 字` : '—'}
                        </span>
                    );
                })}
                {extraMax != null && extraMax > 0 && (
                    <span className={styles.legendItem}>現在 {extraMax.toLocaleString()} 字</span>
                )}
            </div>
        </div>
    );
}
