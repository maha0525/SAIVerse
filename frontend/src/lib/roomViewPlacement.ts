import { useSyncExternalStore } from 'react';

/** ルームビューの置き場所 (docs/intent/screen_avatar.md 設計 1)。
 * - 'column': チャットのすぐ右にくっつけて置く、独立した縦のUI区画 (チャットと合わせて中央に揃える)
 * - 'sidebar': 右サイドバーの、Building の名前と説明のすぐ下
 * - 'overlay': 右サイドバーを出せない幅で、ログの上に重ねてログと切り替える置き方
 */
export type RoomViewPlacement = 'sidebar' | 'column' | 'overlay';

/** この幅以上なら、ルームビューを独立した縦のUI区画 ('column') として置く。 */
export const ROOM_VIEW_COLUMN_MIN_WIDTH = 2000;

/** この幅未満で、右サイドバーがチャットの横に出ていなければ、ログとルームビューを
 * 切り替える ('overlay')。 */
export const ROOM_VIEW_OVERLAY_BELOW_WIDTH = 1100;

/** 右サイドバーがチャットの横に並ぶ形になる最小の幅 (RightSidebar.module.css の
 * `@media (min-width: 769px)` と同じ境目)。これ未満では引き出す形になる。 */
export const RIGHT_SIDEBAR_BESIDE_MIN_WIDTH = 769;

/** `(min-width: Npx)` を useSyncExternalStore で読むための口。
 * serverValue は幅がまだ分からない間 (サーバー側の描画とハイドレーション) の値。 */
function minWidthStore(minWidth: number, serverValue: boolean) {
    const query = `(min-width: ${minWidth}px)`;
    const hasMatchMedia = () => typeof window !== 'undefined' && typeof window.matchMedia === 'function';
    return {
        subscribe(onChange: () => void): () => void {
            if (!hasMatchMedia()) return () => {};
            const mql = window.matchMedia(query);
            mql.addEventListener('change', onChange);
            return () => mql.removeEventListener('change', onChange);
        },
        getSnapshot(): boolean {
            if (!hasMatchMedia()) return serverValue;
            return window.matchMedia(query).matches;
        },
        getServerSnapshot(): boolean {
            return serverValue;
        },
    };
}

// 幅がまだ分からない間は、column は「置けない」、overlay は「切り替えない」として扱う
// (どちらも、今までの画面の形のまま描き始める側)。
const columnStore = minWidthStore(ROOM_VIEW_COLUMN_MIN_WIDTH, false);
const overlayWideStore = minWidthStore(ROOM_VIEW_OVERLAY_BELOW_WIDTH, true);
const sidebarBesideStore = minWidthStore(RIGHT_SIDEBAR_BESIDE_MIN_WIDTH, true);

/** 画面の幅がルームビューのUI区画 ('column') を置ける幅か。幅が変わると追従する。 */
export function useRoomViewColumn(): boolean {
    return useSyncExternalStore(columnStore.subscribe, columnStore.getSnapshot, columnStore.getServerSnapshot);
}

/** 画面の幅が ROOM_VIEW_OVERLAY_BELOW_WIDTH 未満か。幅が変わると追従する。 */
export function useRoomViewOverlayWidth(): boolean {
    return !useSyncExternalStore(overlayWideStore.subscribe, overlayWideStore.getSnapshot, overlayWideStore.getServerSnapshot);
}

/** 画面の幅が、右サイドバーをチャットの横に並べる幅か。幅が変わると追従する。 */
export function useRightSidebarBesideWidth(): boolean {
    return useSyncExternalStore(sidebarBesideStore.subscribe, sidebarBesideStore.getSnapshot, sidebarBesideStore.getServerSnapshot);
}
