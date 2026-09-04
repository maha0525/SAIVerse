import React from 'react';
import styles from './ContextVolumeBar.module.css';

// 提示コンテキストの現在量と水位 (GET /api/people/{id}/context-status, read-only)。
// チャットオプションの「データ送信量の管理」と Chronicle 生成の確認窓が共有する。
export interface ContextStatus {
    persona_id: string;
    model: string | null;
    metabolism: boolean;              // 実効モデルが水位を持つか
    target_chars: number | null;      // 残す量 (整理後にここへ揃える。会話の起点が無いときの初期読み込み量も兼ねる)
    high_chars: number | null;        // 上限 (超えたら整理)
    presented_chars: number | null;   // 現在の提示コンテキスト文字数 (読み戻し後)
    // presented_chars の内訳の三分割 (2026-09-02 / 2026-09-04): 会話の行 /
    // スペル結果などの機構名義の行 / 送信直前に差し込まれる部屋の様子。
    // 古い backend では undefined。
    stored_chars?: number | null;
    mechanism_chars?: number | null;
    injected_perception_chars?: number | null;
    // 残す量 (target_chars) と比べる量 = 整理が計画を立てる窓の**会話の行だけ**
    // (2026-09-03 / 2026-09-04 裁定: 上限の主語は合計、残す量の主語は会話の行 —
    // スペル結果・部屋の様子は数えない)。古い backend
    // では undefined、新しい backend でも測れなければ null — どちらも色分けは
    // presented_chars で代用するが、内訳の行は数値のときだけ出す。
    window_rows_chars?: number | null;
    // 合計は上限を超えているのに会話の行が残す量以下 = 整理しても畳めるものが
    // 無い (超過の主は部屋の様子の供給)。測れないとき / 古い backend では null。
    perception_over_budget?: boolean | null;
    // 一度に畳む単位 U (整理は残す量より古い側を U 文字ぶんずつ刻んで畳む)。
    // U に達したかは「材料の字数」(スペル結果などの長い機構の行を圧縮した後の
    // 字数) で測るので、生の文字数との比較には使えない (2026-08-29 裁定)。
    fold_unit_chars?: number | null;
    // いま「記憶の整理」で実際に畳みが起きるか。backend が実行時と同じ計画
    // (plan_eviction) を dry に呼んだ結果 — 画面側で算数を再実装しない。
    // 送信量を測れないとき、および古い backend では null/undefined。
    fold_ready?: boolean | null;
    // 畳みが起きないとき、畳める範囲の材料があと何字たまれば畳めるか (畳める
    // ときは 0)。
    fold_shortfall_chars?: number | null;
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
    // 上限と比べるのは合計 (presented)、残す量と比べるのは会話の行だけ
    // (2026-09-03 裁定)。行の量が無いとき (古い backend = undefined / 測れ
    // なかった = null) は色分けだけ合計で代用し、内訳 (うち会話 …) は出さない —
    // 代用値で「うち会話 = 合計」と書くと部屋の様子の字数と矛盾する。
    const rowsField = status.window_rows_chars;
    const rows = rowsField ?? presented;
    const perceptionChars = status.injected_perception_chars ?? null;
    const mechanismChars = status.mechanism_chars ?? null;
    // 内訳 (うち会話 …) — 会話の量が測れているときだけ出し、0 の部分は省く
    // (B=0 なら非表示の流儀)。「会話」は stored_chars (presented の分解) を使う —
    // window_rows_chars は計画窓の量で、正規化で presented と食い違うと
    // 「うち」の足し算が合計と合わなくなる (ローカルレビュー指摘 2026-09-04)。
    const storedChars = status.stored_chars ?? null;
    const breakdownParts: string[] = [];
    if (typeof storedChars === 'number') {
        breakdownParts.push(`会話 ${storedChars.toLocaleString()}`);
        if (mechanismChars != null && mechanismChars > 0) {
            breakdownParts.push(`スペル結果 ${mechanismChars.toLocaleString()}`);
        }
        if (perceptionChars != null && perceptionChars > 0) {
            breakdownParts.push(`部屋の様子 ${perceptionChars.toLocaleString()}`);
        }
    }

    return (
        <>
            <div className={styles.contextBar}>
                <div
                    className={styles.contextBarFill}
                    style={{
                        width: `${Math.min(100, (presented / scaleMax) * 100)}%`,
                        background: high != null && presented > high ? '#f87171'
                            : target != null && rows > target ? '#fbbf24'
                            : '#34d399',
                    }}
                />
                {target != null && target <= scaleMax && (
                    <div className={styles.contextBarMarker} style={{ left: `${(target / scaleMax) * 100}%` }} />
                )}
            </div>
            <div className={styles.contextStatRow}>
                <span>
                    現在 {presented.toLocaleString()}文字{status.refill_applied ? '（読み戻し後）' : ''}
                    {breakdownParts.length > 1
                        ? `（うち${breakdownParts.join('・')}）`
                        : ''}
                </span>
                <span>
                    残す量 {target != null ? `${target.toLocaleString()}文字` : '—'} ／
                    上限 {high != null ? `${high.toLocaleString()}文字` : 'なし'}
                </span>
            </div>
            {status.perception_over_budget && (
                <div className={styles.contextStatRow}>
                    <span>
                        会話は残す量以下ですが、スペル結果や部屋の様子を足した合計が上限を超えています。整理しても畳めるものはありません。
                    </span>
                </div>
            )}
        </>
    );
}
