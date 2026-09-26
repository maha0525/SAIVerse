// 配布チャンネル (安定版 / アーリーアクセス版) の切り替えを画面から頼むための共通部品。
// 仕様の正典は docs/intent/early_access_release.md (§3-2 入るとき / §3-3 戻るとき /
// §3-4 最新が正式版のときは「安定版に戻る」を案内)。
//
// 切り替えの実体はバックエンドの POST /api/system/channel で、受け付けられると
// バックエンドは自分を止めて切り替え → 再起動する (セルフアップデートと同じ流れ)。
// そのため「再起動待ち」の表示は、更新ボタンと同じくトップ画面 (app/page.tsx) が持つ。
// 設定画面から切り替えたときは UPDATE_STARTED_EVENT でトップ画面に知らせる。

import { apiFetch } from '@/i18n/api';

export type ReleaseChannel = 'stable' | 'early_access';

/** GET /api/system/version の応答のうち、更新とチャンネルに関わる部分。 */
export interface VersionInfo {
    version: string;
    latest_version?: string | null;
    update_available?: boolean | null;
    latest_release_url?: string | null;
    channel?: ReleaseChannel;
    branch?: string | null;
    /** アーリーアクセス版のときだけ入る、安定版の最新。 */
    stable_latest_version?: string | null;
    /** 安定版の最新が手元の版以上なら true。GitHub に届かなければ null。 */
    can_return_to_stable?: boolean | null;
}

/** 更新 (またはチャンネルの切り替え) が始まり、バックエンドが再起動に入ったことの知らせ。
 *  detail.version は行き先の版 (分からなければ空文字)。 */
export const UPDATE_STARTED_EVENT = 'saiverse-update-started';

export function announceUpdateStarted(version: string): void {
    window.dispatchEvent(new CustomEvent(UPDATE_STARTED_EVENT, { detail: { version } }));
}

/**
 * アーリーアクセス版の手元で、いちばん新しい版が正式版 (= 正式版が EA を追い越した)
 * かどうか。そのときは通常の更新ではなく「安定版に戻る」を案内する (intent §3-4)。
 * EA のブランチを進めても正式版には届かないため。
 */
export function isStableCaughtUp(info: VersionInfo | null | undefined): boolean {
    if (!info || info.channel !== 'early_access') return false;
    if (!info.latest_version || !info.stable_latest_version) return false;
    return info.latest_version === info.stable_latest_version && info.can_return_to_stable === true;
}

export type ChannelSwitchResult =
    | { ok: true }
    /** detail はサーバーが返した理由の文章 (無ければ null — 呼び出し側が汎用の文言を出す)。 */
    | { ok: false; detail: string | null; unreachable: boolean };

/**
 * チャンネルの切り替えを頼む。アーリーアクセス版に入るときは、利用者が画面で
 * 同意を済ませたあとにだけ consent: true で呼ぶこと (サーバーも無ければ 400 で断る)。
 */
export async function requestChannelSwitch(
    channel: ReleaseChannel,
    consent: boolean,
): Promise<ChannelSwitchResult> {
    let res: Response;
    try {
        res = await apiFetch('/api/system/channel', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ channel, consent }),
        });
    } catch {
        return { ok: false, detail: null, unreachable: true };
    }
    if (res.ok) return { ok: true };
    // 拒否の理由 (手元に変更がある / ブランチが未公開 / 版が古い方向 など) は
    // サーバーの文章をそのまま見せる。
    let detail: string | null = null;
    try {
        const body = await res.json();
        if (body && typeof body.detail === 'string' && body.detail.trim()) {
            detail = body.detail.trim();
        }
    } catch {
        // JSON でない応答: 呼び出し側の汎用の文言に任せる
    }
    return { ok: false, detail, unreachable: false };
}
