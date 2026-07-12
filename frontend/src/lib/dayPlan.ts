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
