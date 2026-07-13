/**
 * 時間割コマ (day-plan slot) の共有型。
 *
 * LifeView と EventsTimeline がどちらも /api/people/{id}/day-plan の slots を
 * 描くため、同じ形の interface を各自で持っていた (実機 §6-4 の重複定義)。
 * ここに一本化する。
 *
 * source of truth は API の `api/routes/people/life.py:DayPlanSlot` (BaseModel)。
 * API はより多くのフィールド (ref / facility / note / budget_rounds 等) を返すが、
 * フロントの表示で使うのはこの部分集合だけ。使うフィールドが増えたらここに足す。
 */
export interface DayPlanSlot {
    index: number;
    start: string;          // "HH:MM"
    kind: string;
    title: string;
    status: string;
    result_label: string;
}

/**
 * ライフ宣言 1 件 (life.md §4.1) の表示用整形。
 * source of truth は `api/routes/people/life.py:LifeItem`。
 * ライフビューの帯描画 (§9.2) で使う。
 */
export interface LifeItem {
    index: number;
    start: string;           // "HH:MM"
    end: string;              // "HH:MM"
    mode: string;              // "even" (均等) / "free" (自由)
    budget_pulses: number;
    used_pulses: number;
    used_rounds: number;
    consumed: number;          // used_pulses + used_rounds × κ
    remaining: number;
    // 判断点 (起床・会話終了・セッション終了・イベント・就寝) の発火回数。
    // 予算 (budget_pulses/consumed) には含めない別枠の観測値 (life.md v0.5 §5.3/§8.2)。
    judgment_pulses: number;
}

/**
 * 「いま」のライフ状態 (life.md §9.1 試金石の判定結果)。
 * 見ている日が「いま」の営業日と一致しないときは day-plan 応答で null。
 */
export interface LifeStatus {
    lives_declared: boolean;
    in_life: boolean;
    life_index: number | null;
}
