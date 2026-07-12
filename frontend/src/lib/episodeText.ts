/**
 * できごとカードの本文テンプレート (画面 A: /events)。
 *
 * kind 別の日常語テンプレを 1 箇所に集約した決定論ユーティリティ。
 * LLM は呼ばず、Episode 行の属性 (kind / 場所 / 参加者 / meta) だけから
 * 文を組み立てる。内部用語 (Track / mark / stage 等) は一切出さない。
 *
 * 設計正典: docs/intent/persona_cognition/life_concept_map.md §8.1
 */

/** /api/episodes の 1 行のうち、文生成に使う属性のサブセット */
export interface EpisodeForText {
    persona_id: string;
    kind: string;
    started_at: number;               // epoch 秒
    ended_at: number | null;          // open (いま) は null
    building_name: string | null;
    participants: string[];
    meta: Record<string, unknown> | null;
}

export interface EpisodeTextOptions {
    /** participants の ID → 表示名。未解決 ID は文から省く */
    resolveName?: (id: string) => string | undefined;
    /** 同じカードに束ねられたペルソナ ID (自分視点の「相手」から除外する) */
    groupPersonaIds?: string[];
    /** true = 「いま」行 (進行形で終える)。false/省略 = 過去形 */
    ongoing?: boolean;
}

/** meta から slot の表題を取り出す (無ければ null) */
export function episodeTitle(meta: Record<string, unknown> | null): string | null {
    if (!meta) return null;
    const direct = meta['title'];
    if (typeof direct === 'string' && direct.trim()) return direct.trim();
    const slot = meta['slot'];
    if (slot && typeof slot === 'object') {
        const t = (slot as Record<string, unknown>)['title'];
        if (typeof t === 'string' && t.trim()) return t.trim();
    }
    return null;
}

/** 作業セッション Pulse が走るコマ種別 (saiverse/day_plan.py の SIX_KINDS と同値)。
    kind バッジの 2 トーン判定に使う: この 6 種はアクセント色、
    暮らし / 休む (と未知の kind) はミュート色。 */
const WORK_SLOT_KINDS = new Set([
    '話す', '聞く', '作る', '知る', '経験する', '自分を更新する',
]);

/** kind が「作業セッション Pulse が走るコマ」かどうか。
    未知の kind は false (= ミュート側の色でそのまま表示する) */
export function isWorkSlotKind(kind: string): boolean {
    return WORK_SLOT_KINDS.has(kind);
}

/** meta からコマの型 (slot_kind) を取り出す (無ければ null)。
    コマ由来の出来事は day_plan._open_slot_episode が meta['slot_kind'] に
    書く。会話由来など slot_kind を持たない出来事は null。 */
export function episodeSlotKind(meta: Record<string, unknown> | null): string | null {
    if (!meta) return null;
    const v = meta['slot_kind'];
    if (typeof v === 'string' && v.trim()) return v.trim();
    return null;
}

/** meta から成果物 (Item ID) のリストを取り出す (無ければ空配列) */
export function episodeArtifacts(meta: Record<string, unknown> | null): string[] {
    if (!meta) return [];
    const pick = (v: unknown): string[] =>
        Array.isArray(v) ? v.filter((a): a is string => typeof a === 'string' && !!a) : [];
    const direct = pick(meta['artifacts']);
    if (direct.length > 0) return direct;
    const ws = meta['work_session'];
    if (ws && typeof ws === 'object') {
        return pick((ws as Record<string, unknown>)['artifacts']);
    }
    return [];
}

/** 所要時間 (分)。ended が無い / 1 分未満は null */
export function episodeDurationMinutes(ep: EpisodeForText): number | null {
    if (ep.ended_at == null) return null;
    const minutes = Math.round((ep.ended_at - ep.started_at) / 60);
    return minutes >= 1 ? minutes : null;
}

/** 「相手」の表示名リスト (自分と束ねられた仲間を除いた participants) */
function partnerNames(ep: EpisodeForText, opts: EpisodeTextOptions): string[] {
    const exclude = new Set([ep.persona_id, ...(opts.groupPersonaIds ?? [])]);
    const names: string[] = [];
    for (const pid of ep.participants ?? []) {
        if (exclude.has(pid)) continue;
        const name = opts.resolveName?.(pid);
        if (name && !names.includes(name)) names.push(name);
    }
    return names;
}

/**
 * できごと 1 件 (またはグループの代表行) の本文を組み立てる。
 *
 * テンプレ一覧 (過去形 / 進行形):
 * - slot:         「『{title}』の時間を過ごした / 過ごしている」
 *                 title 無し → 「{場所}で過ごした / 過ごしている」
 * - work_session: 「{場所}で作業をした / 作業をしている」
 * - conversation: 「{相手}とおしゃべりした / おしゃべりしている」
 * - presence:     「{場所}でのんびり過ごした / のんびりしている」
 * - stroll:       「{場所}を散歩した / 散歩している」
 * - other:        「{場所}で過ごした / 過ごしている」
 */
export function buildEpisodeSentence(ep: EpisodeForText, opts: EpisodeTextOptions = {}): string {
    const ongoing = !!opts.ongoing;
    const place = ep.building_name;

    switch (ep.kind) {
        case 'slot': {
            const title = episodeTitle(ep.meta);
            if (title) {
                return ongoing
                    ? `「${title}」の時間を過ごしている`
                    : `「${title}」の時間を過ごした`;
            }
            if (place) {
                return ongoing ? `${place}で過ごしている` : `${place}で過ごした`;
            }
            return ongoing ? 'じぶんの時間を過ごしている' : 'じぶんの時間を過ごした';
        }
        case 'work_session': {
            if (place) {
                return ongoing ? `${place}で作業をしている` : `${place}で作業をした`;
            }
            return ongoing ? '作業をしている' : '作業をした';
        }
        case 'conversation': {
            const names = partnerNames(ep, opts);
            const withWho = names.length > 0 ? `${names.join('と')}と` : '';
            const base = withWho
                ? (ongoing ? `${withWho}おしゃべりしている` : `${withWho}おしゃべりした`)
                : (ongoing ? 'おしゃべりしている' : 'おしゃべりした');
            if (!ongoing) {
                const minutes = episodeDurationMinutes(ep);
                if (minutes != null) return `${base}（${minutes}分）`;
            }
            return base;
        }
        case 'presence': {
            if (place) {
                return ongoing ? `${place}でのんびりしている` : `${place}でのんびり過ごした`;
            }
            return ongoing ? 'のんびりしている' : 'のんびり過ごした';
        }
        case 'stroll': {
            if (place) {
                return ongoing ? `${place}を散歩している` : `${place}を散歩した`;
            }
            return ongoing ? '散歩をしている' : '散歩をした';
        }
        default: {
            if (place) {
                return ongoing ? `${place}で過ごしている` : `${place}で過ごした`;
            }
            return ongoing ? '静かに過ごしている' : '静かに過ごした';
        }
    }
}
