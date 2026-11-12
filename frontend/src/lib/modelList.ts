import { apiFetch } from '@/i18n/api';

/** モデルの 1 日あたりの回数上限 (GET /api/config/models の rate_limit)。 */
export interface ModelRateLimit {
    rpd: number;
    reset_timezone: string;
}

/**
 * GET /api/config/models の 1 行。モデルを選ぶ欄はどの画面もこの一覧を使う
 * (docs/intent/model_picker.md 決まったこと 11)。行はサーバーが表示する順番に
 * 並べて返すので、画面の側では並べ替えない (同 12)。
 */
export interface PickerModel {
    id: string;
    name: string;
    available?: boolean;
    /** 定義が壊れているモデル。選べない状態で「無効」の文言つきで見せる。 */
    config_error?: { path: string; source: string; reason: string } | null;
    provider?: string | null;
    group?: string | null;
    input_price?: number | null;
    output_price?: number | null;
    currency?: string;
    price_period_ends_at?: string | null;
    rate_limit?: ModelRateLimit | null;
    /** 反射判断専用の宛先 (型付きの質問に確率で答えるだけで、文章を書けない)。
     *  会話に使う欄では、呼ぶ側が一覧から外してから渡す。 */
    reflex_only?: boolean;
    /** どのプロバイダのボタンに入るか ("anthropic" "gemini_paid" "custom" など)。 */
    picker_group?: string;
    /** ボタンの表示名の予備。画面が知らないグループのときに使う。 */
    picker_group_label?: string;
    /** ボタンの並び順 (小さい順)。 */
    picker_group_order?: number;
    /** "custom" グループの中でだけ入る、プロバイダの表示名。見出しに使う。 */
    picker_heading?: string | null;
    /** 最初から出すモデルなら true。false のものは「他のモデル」に畳む。 */
    is_current?: boolean;
}

/**
 * モデルの一覧を 1 回取る。取れなかったときは null を返す (呼ぶ側は前の一覧を
 * 残す)。通信の失敗は呼ぶ側へそのまま投げる。
 */
export async function fetchModelList(signal?: AbortSignal): Promise<PickerModel[] | null> {
    const res = await apiFetch('/api/config/models', signal ? { signal } : undefined);
    if (!res.ok) return null;
    const data = await res.json();
    return Array.isArray(data) ? (data as PickerModel[]) : null;
}
