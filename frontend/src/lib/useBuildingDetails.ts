import { apiFetch } from '@/i18n/api';
import { useCallback, useEffect, useRef, useState } from 'react';

export interface Occupant {
    id: string;
    name: string;
    avatar?: string;
    /** ペルソナの見た目の画像 (`APPEARANCE_IMAGE_PATH`) の URL。無ければ null。
     * ルームビューの立ち絵に使う (docs/intent/screen_avatar.md 設計 2)。 */
    appearance_image?: string | null;
    // ⚠ 暮らし系の表示 (話しかけやすさ / いま何をしているか / 自律 OFF) は
    // v0.3 で隠した (autonomous_behavior_v3.md §11「運転 UI は隠す」)。供給する
    // 運転そのものが v0.4 なので、動いていない状態を UI に出さない。
}

export interface Item {
    id: string;
    name: string;
    type: 'document' | 'picture' | 'bag' | 'audio' | 'video' | 'other';
    description?: string;
    is_open?: boolean;  // Whether item content is included in visual context
    contained_items?: Item[];  // For bag type: items inside this bag
    contained_count?: number;  // Number of items directly inside this bag
}

export interface Fixture {
    id: string;
    name: string;
    description: string;
    type: string;
    state_json: string | null;
    has_observer: boolean;
}

export interface BuildingDetails {
    id: string;
    name: string;
    description: string;
    image_path?: string | null;  // Building interior image
    occupants: Occupant[];  // AI personas
    users?: Occupant[];  // Users present in the building (intent §D-3)
    items: Item[];
    /** `items` のうちペルソナに見えるアイテムの ID。最近触られた順 (先頭が一番新しい)。
     * どれが見えるかはバックエンドが `_select_displayed_items` で決める。画面の側では
     * 数え直さない (docs/intent/screen_avatar.md 設計 5)。古いバックエンドでは返らない。 */
    visible_item_ids?: string[];
    /** ペルソナに見えない (埋もれた) アイテムの ID。同じく最近触られた順。 */
    buried_item_ids?: string[];
    fixtures?: Fixture[];
}

interface UseBuildingDetailsOptions {
    /** 親 (ChatPage) が把握している現在 Building ID。fetch 時に必須で渡す。
     * これを省略するとバックエンドが server-global の user_current_building_id にフォールバックし、
     * マルチデバイスで他クライアントの操作に汚染される (2026-04-30 エリス上書き事故の遠因)。
     */
    currentBuildingId: string | null;
    refreshTrigger?: number;
    /** 右サイドバーが開いているか。開閉のたびに取り直す (RightSidebar から移す前と同じ)。 */
    sidebarOpen: boolean;
    /** 10 秒ごとの取り直しを動かすか。右サイドバーが開いている、
     * またはルームビューが画面に出ている間だけ true にする。 */
    pollActive: boolean;
}

/** `GET /api/info/details` の取得。RightSidebar とルームビューの両方が使うので、
 * page.tsx で一度だけ呼んで結果を props で渡す。 */
export function useBuildingDetails({ currentBuildingId, refreshTrigger, sidebarOpen, pollActive }: UseBuildingDetailsOptions) {
    // 応答は「どの部屋を要求して得たものか」を添えて保持する。閲覧中の部屋と
    // 一致するときだけ details として使い、一致しない間 (部屋を切り替えてから
    // 新しい応答が届くまで) は「まだ無い」扱いにする。こうしないと前の部屋の
    // 名前・画像・滞在ペルソナ・アイテムが残ったまま描かれ、そこから開く
    // モーダルの宛先も前の部屋になる (2026-04-30 エリス上書き事故と同型)。
    const [loadedDetails, setLoadedDetails] = useState<{ buildingId: string; data: BuildingDetails } | null>(null);
    const details = loadedDetails && loadedDetails.buildingId === currentBuildingId
        ? loadedDetails.data
        : null;

    // 閲覧中の部屋の最新値。fetch の応答が届いた時点でまだ同じ部屋を見ているかを
    // 判定するために使う (応答の追い越し対策)。同期する useEffect は下の fetch 用
    // useEffect より前に置くこと (React は宣言順に effect を走らせるので、
    // fetch が始まる前にこの ref が新しい部屋になっている必要がある)。
    const viewingBuildingIdRef = useRef<string | null>(null);

    // ⚠ この関数は setInterval からも呼ぶので、必ず useCallback で
    // currentBuildingId に紐付けたままにすること。依存から外すと、ポーリングが
    // パネルを開いた時点の building_id を掴み続け、部屋を移動しても 10 秒後に
    // 前の部屋の内容へ戻され続ける (v0.3.13 のユーザー報告)。
    const fetchDetails = useCallback(async () => {
        // currentBuildingId 未指定だと server-global の user_current_building_id に
        // 汚染される (エリス上書き事故の遠因)。明示指定がない間は fetch しない。
        const targetId = currentBuildingId;
        if (!targetId) {
            console.warn('[useBuildingDetails] fetchDetails skipped: currentBuildingId not provided yet');
            return;
        }
        try {
            const res = await apiFetch(`/api/info/details?building_id=${encodeURIComponent(targetId)}`);
            if (res.ok) {
                const data = await res.json();
                // 応答が届くまでに別の部屋へ移っていたら捨てる。先に投げた古い部屋の
                // 応答が後から届いて新しい部屋の内容を上書きするのを防ぐ。
                if (viewingBuildingIdRef.current !== targetId) return;
                setLoadedDetails({ buildingId: targetId, data });
            }
        } catch (err) {
            console.error("Failed to fetch building details", err);
        }
    }, [currentBuildingId]);

    // ⚠ この effect は下の fetch 用 effect より前に置くこと (宣言順の理由は
    // viewingBuildingIdRef の宣言箇所のコメント参照)。
    useEffect(() => {
        viewingBuildingIdRef.current = currentBuildingId ?? null;
    }, [currentBuildingId]);

    useEffect(() => {
        // fetchDetails は currentBuildingId が変わったときだけ作り直されるので、
        // これを deps に置くことが「部屋が変わったら再 fetch」を兼ねる。
        fetchDetails();
    }, [refreshTrigger, sidebarOpen, fetchDetails]);

    // Polling for real-time updates while the sidebar is open or the room view is on screen
    useEffect(() => {
        if (!pollActive) return;

        const pollInterval = setInterval(() => {
            fetchDetails();
        }, 10000); // Poll every 10 seconds

        return () => clearInterval(pollInterval);
        // fetchDetails を deps に含めること。外すとポーリングが古い部屋を
        // 掴み続ける (上の fetchDetails のコメント参照)。
    }, [pollActive, fetchDetails]);

    return { details, refetchDetails: fetchDetails };
}
