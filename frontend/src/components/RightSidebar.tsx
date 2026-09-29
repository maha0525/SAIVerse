
import { apiFetch } from '@/i18n/api';

import { getFormatLocale } from '@/i18n/core';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import { useCallback, useRef, useState, useEffect } from 'react';
import styles from './RightSidebar.module.css';
import {
    Users,
    FileText,
    Image as ImageIcon,
    File,
    Eye,
    EyeOff,
    Settings,
    Package,
    Music,
    Video,
    Anchor,
    Activity,
    Plus,
    X,
} from 'lucide-react';
import ItemModal from './ItemModal';
import ItemCreateModal from './ItemCreateModal';
import PersonaMenu from './PersonaMenu';
import ModalOverlay from './common/ModalOverlay';
import fixtureStyles from './FixtureModal.module.css';
import itemModalStyles from './ItemModal.module.css';
import FixtureMetaEditor, { FixtureMetaEditButton } from './FixtureMetaEditor';
import MemoryModal from './MemoryModal';
import ScheduleModal from './ScheduleModal';
import SettingsModal from './SettingsModal';
import InventoryModal from './InventoryModal';
import BuildingSettingsModal from './BuildingSettingsModal';

interface RightSidebarProps {
    isOpen: boolean;
    onClose?: () => void;
    refreshTrigger?: number;
    /** 親 (ChatPage) が把握している現在 Building ID。fetch 時に必須で渡す。
     * これを省略するとバックエンドが server-global の user_current_building_id にフォールバックし、
     * マルチデバイスで他クライアントの操作に汚染される (2026-04-30 エリス上書き事故の遠因)。
     */
    currentBuildingId?: string | null;
    /** PersonaMenu からの dismiss 等で滞在状況が変わったときに親 (ChatPage) へ通知し、
     * moveTrigger 等を bump して Sidebar 側も同期させるための callback。 */
    onPersonaChanged?: () => void;
    /** 通話モードの開始要求を親 (ChatPage) へ渡す。 通話モーダルを ChatPage が
     * 持つのは、 建物を見て回っても通話が切れないようにするため (この
     * サイドバーは building が変わると開いているものを全部閉じる)。 */
    onStartVoiceCall?: (personaId: string, personaName: string, buildingId: string) => void;
}

interface Occupant {
    id: string;
    name: string;
    avatar?: string;
    // ⚠ 暮らし系の表示 (話しかけやすさ / いま何をしているか / 自律 OFF) は
    // v0.3 で隠した (autonomous_behavior_v3.md §11「運転 UI は隠す」)。供給する
    // 運転そのものが v0.4 なので、動いていない状態を UI に出さない。
}

interface Item {
    id: string;
    name: string;
    type: 'document' | 'picture' | 'bag' | 'audio' | 'video' | 'other';
    description?: string;
    is_open?: boolean;  // Whether item content is included in visual context
    contained_items?: Item[];  // For bag type: items inside this bag
    contained_count?: number;  // Number of items directly inside this bag
}

interface Fixture {
    id: string;
    name: string;
    description: string;
    type: string;
    state_json: string | null;
    has_observer: boolean;
}

interface BuildingDetails {
    id: string;
    name: string;
    description: string;
    image_path?: string | null;  // Building interior image
    occupants: Occupant[];  // AI personas
    users?: Occupant[];  // Users present in the building (intent §D-3)
    items: Item[];
    fixtures?: Fixture[];
}

export default function RightSidebar({ isOpen, onClose, refreshTrigger, currentBuildingId, onPersonaChanged, onStartVoiceCall }: RightSidebarProps) {
    useLocale();
    // 応答は「どの部屋を要求して得たものか」を添えて保持する。閲覧中の部屋と
    // 一致するときだけ details として使い、一致しない間 (部屋を切り替えてから
    // 新しい応答が届くまで) は「まだ無い」扱いにする。こうしないと前の部屋の
    // 名前・画像・滞在ペルソナ・アイテムが残ったまま描かれ、そこから開く
    // モーダルの宛先も前の部屋になる (2026-04-30 エリス上書き事故と同型)。
    const [loadedDetails, setLoadedDetails] = useState<{ buildingId: string; data: BuildingDetails } | null>(null);
    const details = loadedDetails && loadedDetails.buildingId === currentBuildingId
        ? loadedDetails.data
        : null;
    const [selectedItem, setSelectedItem] = useState<Item | null>(null);
    const [selectedFixture, setSelectedFixture] = useState<Fixture | null>(null);
    const [selectedPersona, setSelectedPersona] = useState<Occupant | null>(null);
    // Track picture items whose thumbnail failed to load, so we fall back to the icon
    const [thumbErrors, setThumbErrors] = useState<Set<string>>(new Set());

    // Modal States
    const [showMemory, setShowMemory] = useState(false);
    const [showSchedule, setShowSchedule] = useState(false);
    const [showSettings, setShowSettings] = useState(false);
    const [showInventory, setShowInventory] = useState(false);
    const [showBuildingSettings, setShowBuildingSettings] = useState(false);
    const [showItemCreate, setShowItemCreate] = useState(false);

    // Keep track of which persona is active for modals
    // When opening a modal, we use selectedPersona's ID.
    // We need to keep the ID even if selectedPersona is cleared (though typically we might close menu first).
    const [activeModalPersonaId, setActiveModalPersonaId] = useState<string | null>(null);
    const [activeModalPersonaName, setActiveModalPersonaName] = useState<string | null>(null);

    // 2026-04-30 のエリス上書き事故 (feedback_modal_id_integrity.md) の再発防止:
    // サーバ side global の user_current_building_id が他デバイスの操作で変動すると、
    // 当タブの details が別 building の occupants に切り替わる現象が起こりうる。
    // その状態でモーダル / PersonaMenu が開いたままだと、新コンテキストの occupant ID
    // で操作が走ってしまうため、building 変更を検知したらすべて閉じる。
    const previousBuildingIdRef = useRef<string | null>(null);

    // 閲覧中の部屋の最新値。fetch の応答が届いた時点でまだ同じ部屋を見ているかを
    // 判定するために使う (応答の追い越し対策)。同期する useEffect は下の fetch 用
    // useEffect より前に置くこと (React は宣言順に effect を走らせるので、
    // fetch が始まる前にこの ref が新しい部屋になっている必要がある)。
    const viewingBuildingIdRef = useRef<string | null>(null);

    const startX = useRef<number | null>(null);
    const startY = useRef<number | null>(null);
    const startTime = useRef<number | null>(null);



    // ⚠ この関数は setInterval からも呼ぶので、必ず useCallback で
    // currentBuildingId に紐付けたままにすること。依存から外すと、ポーリングが
    // パネルを開いた時点の building_id を掴み続け、部屋を移動しても 10 秒後に
    // 前の部屋の内容へ戻され続ける (v0.3.13 のユーザー報告)。
    const fetchDetails = useCallback(async () => {
        // currentBuildingId 未指定だと server-global の user_current_building_id に
        // 汚染される (エリス上書き事故の遠因)。明示指定がない間は fetch しない。
        const targetId = currentBuildingId;
        if (!targetId) {
            console.warn('[RightSidebar] fetchDetails skipped: currentBuildingId not provided yet');
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

    const handleToggleOpen = async (e: React.MouseEvent, item: Item) => {
        e.stopPropagation(); // Don't open the item modal
        try {
            const res = await apiFetch(`/api/info/item/${item.id}/toggle-open`, {
                method: 'POST'
            });
            if (res.ok) {
                // Refresh to get updated state
                fetchDetails();
            }
        } catch (err) {
            console.error("Failed to toggle item open state", err);
        }
    };

    // ⚠ この effect は下の fetch 用 effect より前に置くこと (宣言順の理由は
    // viewingBuildingIdRef の宣言箇所のコメント参照)。
    useEffect(() => {
        viewingBuildingIdRef.current = currentBuildingId ?? null;
    }, [currentBuildingId]);

    useEffect(() => {
        // fetchDetails は currentBuildingId が変わったときだけ作り直されるので、
        // これを deps に置くことが「部屋が変わったら再 fetch」を兼ねる。
        fetchDetails();
    }, [refreshTrigger, isOpen, fetchDetails]);

    // Building 変化検知: 開いているモーダル / メニューを強制クローズする。
    // - selectedPersona (PersonaMenu の表示元): 旧 building の occupant への参照
    // - 各モーダル: 古い personaId のまま開いていると、ユーザの誤操作で新 context に
    //   引きずられた誤書き込みが起こりうる
    useEffect(() => {
        const newId = details?.id ?? null;
        const prev = previousBuildingIdRef.current;
        previousBuildingIdRef.current = newId;
        if (prev === null || prev === newId) return;
        // Building 変化 → 安全のため全部閉じる
        console.log(`[RightSidebar] Building context changed (${prev} -> ${newId}); closing menus/modals`);
        setSelectedPersona(null);
        setShowMemory(false);
        setShowSchedule(false);
        setShowSettings(false);
        setShowInventory(false);
        setShowBuildingSettings(false);
        setShowItemCreate(false);
    }, [details?.id]);

    // Polling for real-time updates when sidebar is open
    useEffect(() => {
        if (!isOpen) return;

        const pollInterval = setInterval(() => {
            fetchDetails();
        }, 10000); // Poll every 10 seconds

        return () => clearInterval(pollInterval);
        // fetchDetails を deps に含めること。外すとポーリングが古い部屋を
        // 掴み続ける (上の fetchDetails のコメント参照)。
    }, [isOpen, fetchDetails]);

    const handleTouchStart = (e: React.TouchEvent) => {
        e.stopPropagation();
        startX.current = e.touches[0].clientX;
        startY.current = e.touches[0].clientY;
        startTime.current = Date.now();
    };

    const handleTouchMove = (e: React.TouchEvent) => {
        e.stopPropagation();
        if (startX.current === null || startY.current === null || startTime.current === null) return;

        const currentX = e.touches[0].clientX;
        const currentY = e.touches[0].clientY;
        const diffX = currentX - startX.current;
        const diffY = currentY - startY.current;
        const timeDiff = Date.now() - startTime.current;

        // Vertical scroll preference check
        if (Math.abs(diffY) > Math.abs(diffX)) {
            startX.current = null; // Abort
            return;
        }

        // Time limit for quick swipe
        if (timeDiff > 300) {
            startX.current = null; // Abort
            return;
        }

        // Swipe Right (> 60px) -> Close
        if (diffX > 60 && onClose) {
            onClose();
            startX.current = null;
        }
    };

    // Helper to open specific modal
    // 2026-04-30 のエリス上書き事故 (feedback_modal_id_integrity.md) の再発防止:
    // 既にどれかのモーダルが別 personaId で開いている状態で再度 openModal が呼ばれると、
    // activeModalPersonaId だけが上書きされ、対象モーダルは開いたまま personaId プロパティ
    // だけが切り替わる現象が起きる。このとき「フォームの中身は古いまま、保存先 ID だけ新しい」
    // という極めて危険な状態になりうるため、いったんすべてのモーダルを閉じてから開き直す。
    const openModal = (type: 'memory' | 'schedule' | 'settings' | 'inventory') => {
        if (!selectedPersona) return;
        const newId = selectedPersona.id;
        const newName = selectedPersona.name;

        const anyOpen = showMemory || showSchedule || showSettings || showInventory;
        const sameTarget = anyOpen && activeModalPersonaId === newId;

        const applyOpen = () => {
            setActiveModalPersonaId(newId);
            setActiveModalPersonaName(newName);
            if (type === 'memory') setShowMemory(true);
            if (type === 'schedule') setShowSchedule(true);
            if (type === 'settings') setShowSettings(true);
            if (type === 'inventory') setShowInventory(true);
        };

        // Close the menu in either branch.
        setSelectedPersona(null);

        if (anyOpen && !sameTarget) {
            // 別ペルソナでモーダルが開いている → 完全に閉じてから次 tick で開き直す。
            // モーダル内コンポーネントは isOpen=false の間に internal state をリセットし、
            // 再度 isOpen=true になったとき新しい personaId で loadConfig をやり直す。
            setShowMemory(false);
            setShowSchedule(false);
            setShowSettings(false);
            setShowInventory(false);
            // 次の tick で開く: state 反映と useEffect cleanup を間に挟むため
            setTimeout(applyOpen, 0);
            return;
        }

        applyOpen();
    };

    return (
        <>
            {/* Mobile Overlay */}
            <div
                className={`${styles.overlay} ${isOpen ? styles.visible : ''}`}
                onClick={(e) => {
                    e.stopPropagation();
                    if (onClose) onClose();
                }}
                onTouchStart={(e) => e.stopPropagation()}
                onTouchMove={(e) => e.stopPropagation()}
            />

            <aside
                className={`${styles.sidebar} ${isOpen ? styles.open : ''}`}
                onTouchStart={handleTouchStart}
                onTouchMove={handleTouchMove}
            >
                {details ? (
                    <>
                        <div className={styles.section}>
                            <h3 data-i18n="components.RightSidebar.text001" className={styles.heading}>{uiText("components.RightSidebar.text001")}</h3>
                            <div className={styles.content}>
                                <div className={styles.buildingHeader}>
                                    <div className={styles.buildingName}>{details.name}</div>
                                    <button data-i18n="components.RightSidebar.text002"
                                        className={styles.settingsBtn}
                                        onClick={() => setShowBuildingSettings(true)}
                                        title={uiText("components.RightSidebar.text002")}
                                    >
                                        <Settings size={16} />
                                    </button>
                                </div>
                                <div data-i18n="components.RightSidebar.text003" className={styles.description}>
                                    {details.description || uiText("components.RightSidebar.text003")}
                                </div>
                            </div>
                        </div>

                        {/* Building Interior Image */}
                        {details.image_path && (
                            <div className={styles.section}>
                                <h3 data-i18n="components.RightSidebar.text004" className={styles.heading}>
                                    <ImageIcon size={16} />{uiText("components.RightSidebar.text004")}</h3>
                                <div className={styles.buildingImage}>
                                    <img
                                        src={details.image_path}
                                        alt={`${details.name} interior`}
                                        onError={(e) => {
                                            e.currentTarget.style.display = 'none';
                                        }}
                                    />
                                </div>
                            </div>
                        )}

                        {/* Users (intent §D-3): AI とは別配列で表示。
                            「自分も場のメンバー」 を視覚的に示すための独立セクション。
                            ユーザーは modal なし (= プロフィール変更等は別経路)。 */}
                        {details.users && details.users.length > 0 && (
                            <div className={styles.section}>
                                <h3 data-i18n="components.RightSidebar.text005" className={styles.heading}>
                                    <Users size={16} />{uiText("components.RightSidebar.text005")}{details.users.length})
                                </h3>
                                <div className={styles.occupantList}>
                                    {details.users.map(u => (
                                        <div
                                            key={`user-${u.id}`}
                                            className={styles.occupant}
                                        >
                                            <div className={styles.occupantAvatar}>
                                                <img
                                                    src={u.avatar || "/api/static/builtin_icons/user.png"}
                                                    alt={u.name}
                                                    onError={(e) => { e.currentTarget.src = "https://placehold.co/48x48?text=?"; }}
                                                />
                                            </div>
                                            <span className={styles.occupantName}>{u.name}</span>
                                        </div>
                                    ))}
                                </div>
                            </div>
                        )}

                        <div className={styles.section}>
                            <h3 data-i18n="components.RightSidebar.text006" className={styles.heading}>
                                <Users size={16} />{uiText("components.RightSidebar.text006")}{details.occupants.length})
                            </h3>
                            <div className={styles.occupantList}>
                                {details.occupants.length > 0 ? (
                                    details.occupants.map(user => (
                                        <div
                                            key={user.id}
                                            className={`${styles.occupant} ${styles.clickable}`}
                                            onClick={() => setSelectedPersona(user)}
                                        >
                                            <div className={styles.occupantAvatar}>
                                                <img
                                                    src={user.avatar || "/api/static/icons/host.png"}
                                                    alt={user.name}
                                                    onError={(e) => { e.currentTarget.src = "https://placehold.co/48x48?text=?"; }}
                                                />
                                            </div>
                                            <div className={styles.occupantInfo}>
                                                <span className={styles.occupantName}>{user.name}</span>
                                            </div>
                                        </div>
                                    ))
                                ) : (
                                    <div data-i18n="components.RightSidebar.text007" className={styles.empty}>{uiText("components.RightSidebar.text007")}</div>
                                )}
                            </div>
                        </div>

                        <div className={styles.section}>
                            <h3 data-i18n="components.RightSidebar.text008" className={styles.heading}>
                                <FileText size={16} />{uiText("components.RightSidebar.text008")}{details.items.length})
                                {/* この部屋にアイテムを作る一般経路。ワールドエディタを開かずに
                                    今いる部屋へ置ける (docs/issues/bag_item_has_no_creation_path.md)。
                                    id が "unknown" のときは Building を特定できていない
                                    (/api/info/details のフォールバック) ので出さない。 */}
                                {details.id && details.id !== 'unknown' && (
                                    <button
                                        className={styles.addItemBtn}
                                        onClick={() => setShowItemCreate(true)}
                                        title={uiText("components.RightSidebar.text019")}
                                        aria-label={uiText("components.RightSidebar.text019")}
                                    >
                                        <Plus size={16} />
                                    </button>
                                )}
                            </h3>
                            <div className={styles.grid}>
                                {details.items.length > 0 ? (
                                    details.items.map(item => (
                                        <div
                                            key={item.id}
                                            className={`${styles.card} ${styles[item.type]} ${item.is_open ? styles.itemOpen : ''}`}
                                            onClick={() => setSelectedItem(item)}
                                        >
                                            {item.type === 'picture' && !thumbErrors.has(item.id) ? (
                                                <div className={styles.cardThumb}>
                                                    <img
                                                        src={`/api/info/item/${item.id}?thumb=1`}
                                                        alt={item.name}
                                                        loading="lazy"
                                                        onError={() => setThumbErrors(prev => {
                                                            const next = new Set(prev);
                                                            next.add(item.id);
                                                            return next;
                                                        })}
                                                    />
                                                </div>
                                            ) : (
                                                <div className={styles.cardIcon}>
                                                    {item.type === 'picture' ? <ImageIcon size={20} />
                                                        : item.type === 'bag' ? <Package size={20} />
                                                        : item.type === 'audio' ? <Music size={20} />
                                                        : item.type === 'video' ? <Video size={20} />
                                                        : <File size={20} />}
                                                </div>
                                            )}
                                            <div className={styles.cardInfo}>
                                                <div className={styles.cardName}>
                                                    {item.name}
                                                    {item.type === 'bag' && item.contained_count != null && (
                                                        <span className={styles.bagCount}> ({item.contained_count})</span>
                                                    )}
                                                </div>
                                                {item.description && (
                                                    <div className={styles.cardDesc}>{item.description}</div>
                                                )}
                                            </div>
                                            {(item.type === 'picture' || item.type === 'document' || item.type === 'bag' || item.type === 'audio' || item.type === 'video') && (
                                                <button data-i18n="components.RightSidebar.text009 components.RightSidebar.text010"
                                                    className={`${styles.toggleOpenBtn} ${item.is_open ? styles.isOpen : ''}`}
                                                    onClick={(e) => handleToggleOpen(e, item)}
                                                    title={item.is_open ? uiText("components.RightSidebar.text009") : uiText("components.RightSidebar.text010")}
                                                >
                                                    {item.is_open ? <Eye size={16} /> : <EyeOff size={16} />}
                                                </button>
                                            )}
                                        </div>
                                    ))
                                ) : (
                                    <div data-i18n="components.RightSidebar.text011" className={styles.empty}>{uiText("components.RightSidebar.text011")}</div>
                                )}
                            </div>
                        </div>

                        {details.fixtures && details.fixtures.length > 0 && (
                            <div className={styles.section}>
                                <h3 data-i18n="components.RightSidebar.text012" className={styles.heading}>
                                    <Anchor size={16} />{uiText("components.RightSidebar.text012")}{details.fixtures.length})
                                </h3>
                                <div className={styles.grid}>
                                    {details.fixtures.map(fixture => (
                                        <div
                                            key={fixture.id}
                                            className={styles.card}
                                            onClick={() => setSelectedFixture(fixture)}
                                            style={{ cursor: 'pointer' }}
                                        >
                                            <div className={styles.cardIcon}>
                                                {fixture.has_observer ? <Activity size={20} /> : <Anchor size={20} />}
                                            </div>
                                            <div className={styles.cardInfo}>
                                                <div className={styles.cardName}>{fixture.name}</div>
                                                {fixture.description && (
                                                    <div className={styles.cardDesc}>{fixture.description}</div>
                                                )}
                                            </div>
                                        </div>
                                    ))}
                                </div>
                            </div>
                        )}
                    </>
                ) : (
                    <div data-i18n="components.RightSidebar.text013" style={{ padding: '1rem', color: '#6b7280' }}>{uiText("components.RightSidebar.text013")}</div>
                )}
            </aside>

            {/* Modals & Menus */}
            {/* Note: We wrapper these in a div that stops propagation to prevent sidebar gestures from affecting them if they bubble up */}
            <div onTouchStart={(e) => e.stopPropagation()} onTouchMove={(e) => e.stopPropagation()}>
                <ItemModal
                    isOpen={!!selectedItem}
                    onClose={() => setSelectedItem(null)}
                    item={selectedItem}
                    // 操作の宛先は常に「いま閲覧している部屋」= 親から渡る
                    // currentBuildingId。details.id は応答の写しにすぎないので
                    // 宛先には使わない。
                    currentBuildingId={currentBuildingId ?? null}
                    onItemUpdated={() => {
                        fetchDetails();
                        setSelectedItem(null);
                    }}
                    // まとめ収納のように操作を続ける経路。部屋の一覧だけ更新し、モーダルは開いたままにする。
                    onWorldChanged={() => fetchDetails()}
                />

                {selectedFixture && (
                    <FixtureModal
                        key={selectedFixture.id}
                        fixture={selectedFixture}
                        onClose={() => setSelectedFixture(null)}
                        // 名前・説明の保存と削除の後: アイテム (onItemUpdated) と
                        // 同じく部屋の表示を取り直してモーダルを閉じる
                        onFixtureUpdated={() => {
                            fetchDetails();
                            setSelectedFixture(null);
                        }}
                    />
                )}

                {selectedPersona && (
                    <PersonaMenu
                        isOpen={!!selectedPersona}
                        onClose={() => setSelectedPersona(null)}
                        personaId={selectedPersona.id}
                        personaName={selectedPersona.name}
                        avatarUrl={selectedPersona.avatar || "/api/static/icons/host.png"}
                        buildingId={currentBuildingId ?? null}
                        onOpenMemory={() => openModal('memory')}
                        onOpenSchedule={() => openModal('schedule')}
                        onOpenSettings={() => openModal('settings')}
                        onOpenInventory={() => openModal('inventory')}
                        onStartCall={(() => {
                            // 部屋が確定していないときは通話の入口を出さない
                            // (VoiceCallModal 側でも building 無しは弾く)。
                            const callBuildingId = currentBuildingId ?? null;
                            if (!onStartVoiceCall || !callBuildingId) return undefined;
                            const target = selectedPersona;
                            return () => onStartVoiceCall(target.id, target.name, callBuildingId);
                        })()}
                        onDismissed={() => {
                            // dismiss 成功 → details を即時 refetch して滞在ペルソナ表示を更新。
                            // 親にも通知して Sidebar / 召喚可能リストなどを同期させる。
                            fetchDetails();
                            onPersonaChanged?.();
                        }}
                    />
                )}

                {/* Persona Action Modals */}
                {activeModalPersonaId && (
                    <>
                        <MemoryModal
                            isOpen={showMemory}
                            onClose={() => setShowMemory(false)}
                            personaId={activeModalPersonaId}
                            personaName={activeModalPersonaName || undefined}
                        />
                        <ScheduleModal
                            isOpen={showSchedule}
                            onClose={() => setShowSchedule(false)}
                            personaId={activeModalPersonaId}
                        />
                        <SettingsModal
                            isOpen={showSettings}
                            onClose={() => setShowSettings(false)}
                            personaId={activeModalPersonaId}
                        />
                        <InventoryModal
                            isOpen={showInventory}
                            onClose={() => setShowInventory(false)}
                            personaId={activeModalPersonaId}
                        />
                    </>
                )}

                {/* アイテム作成 (この部屋へ置く)。宛先は details.id ではなく
                    閲覧中の部屋 (currentBuildingId)。表示名だけ details から取る。 */}
                {details && currentBuildingId && (
                    <ItemCreateModal
                        isOpen={showItemCreate}
                        onClose={() => setShowItemCreate(false)}
                        buildingId={currentBuildingId}
                        buildingName={details.name}
                        onCreated={() => fetchDetails()}
                    />
                )}

                {/* Building Settings Modal */}
                {details && currentBuildingId && (
                    <BuildingSettingsModal
                        isOpen={showBuildingSettings}
                        onClose={() => setShowBuildingSettings(false)}
                        buildingId={currentBuildingId}
                        onSaved={() => fetchDetails()}
                    />
                )}
            </div>
        </>
    );
}


function parseFixtureState(stateJson: string | null): Record<string, any> | null {
    if (!stateJson) return null;
    try {
        const parsed = JSON.parse(stateJson);
        return parsed && typeof parsed === 'object' ? parsed : null;
    } catch {
        return null;
    }
}

interface FixtureModalProps {
    fixture: Fixture;
    onClose: () => void;
    /** 名前・説明の保存か削除が成功した (ItemModal の onItemUpdated と同じ扱い)。 */
    onFixtureUpdated: () => void;
}

/** 設置物モーダル共通のヘッダー: 名前 + 歯車 (メタ情報を編集) + 閉じる。 */
function FixtureModalHeader({ fixture, isMetaEditing, onStartMetaEdit, onClose }: {
    fixture: Fixture;
    isMetaEditing: boolean;
    onStartMetaEdit: () => void;
    onClose: () => void;
}) {
    return (
        <div className={fixtureStyles.header}>
            <h3 className={fixtureStyles.title}>{fixture.name}</h3>
            <div className={itemModalStyles.headerActions}>
                {!isMetaEditing && <FixtureMetaEditButton onClick={onStartMetaEdit} />}
                <button className={fixtureStyles.closeButton} onClick={onClose}>
                    <X size={18} />
                </button>
            </div>
        </div>
    );
}

function FixtureModal({ fixture, onClose, onFixtureUpdated }: FixtureModalProps) {
    useLocale();
    const state = parseFixtureState(fixture.state_json);
    const [isMetaEditing, setIsMetaEditing] = useState(false);

    if (fixture.type === 'feed_stand') {
        return <FeedStandModal fixture={fixture} state={state} onClose={onClose} onFixtureUpdated={onFixtureUpdated} />;
    }

    return (
        <ModalOverlay onClose={onClose} className={fixtureStyles.overlay}>
            <div className={fixtureStyles.modal} onClick={e => e.stopPropagation()}>
                <FixtureModalHeader
                    fixture={fixture}
                    isMetaEditing={isMetaEditing}
                    onStartMetaEdit={() => setIsMetaEditing(true)}
                    onClose={onClose}
                />
                {isMetaEditing && (
                    <FixtureMetaEditor
                        fixture={fixture}
                        onCancel={() => setIsMetaEditing(false)}
                        onChanged={onFixtureUpdated}
                    />
                )}
                <div className={fixtureStyles.content}>
                    {!isMetaEditing && fixture.description && (
                        <p className={fixtureStyles.description}>{fixture.description}</p>
                    )}
                    <div className={fixtureStyles.fixtureId}>{uiText("components.RightSidebar.label001")}{fixture.id}</div>
                    {state && Object.keys(state).length > 0 ? (
                        <div>
                            <h4 data-i18n="components.RightSidebar.text014" className={fixtureStyles.metricsTitle}>{uiText("components.RightSidebar.text014")}</h4>
                            <table className={fixtureStyles.table}>
                                <thead>
                                    <tr>
                                        <th data-i18n="components.RightSidebar.text015">{uiText("components.RightSidebar.text015")}</th>
                                        <th data-i18n="components.RightSidebar.text016">{uiText("components.RightSidebar.text016")}</th>
                                        <th data-i18n="components.RightSidebar.text017">{uiText("components.RightSidebar.text017")}</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {Object.entries(state).map(([key, entry]: [string, any]) => (
                                        <tr key={key}>
                                            <td>{key}</td>
                                            <td className={fixtureStyles.valueCell}>
                                                {entry?.value_num != null ? entry.value_num : entry?.value_text || '—'}
                                            </td>
                                            <td className={fixtureStyles.timeCell}>
                                                {entry?.recorded_at ? new Date(entry.recorded_at).toLocaleString(getFormatLocale()) : '—'}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    ) : (
                        <div data-i18n="components.RightSidebar.text018" className={fixtureStyles.emptyState}>{uiText("components.RightSidebar.text018")}</div>
                    )}
                </div>
            </div>
        </ModalOverlay>
    );
}


// ── フィードスタンド (Fixture TYPE="feed_stand") のモーダル ──
// 観測値の表は出さない (この用途では無意味 — 2026-09-29 まはー裁定)。
// STATE_JSON の feed_stand キー (書き手は feed_manager.update_fixture_display) を
// 「購読中のフィード」「新着記事」として見せ、スタンドごとの配信設定を編集させる。

type StandSettingKey = 'fetch_interval_sec' | 'summary_max_chars' | 'title_max_chars' | 'max_items_per_push';
type StandSettings = Record<StandSettingKey, number>;
type StandConfig = Record<StandSettingKey, number | null>;

// api/routes/feeds.py の検証範囲 (saiverse/feed_manager.py の STAND_CONFIG_RANGES) と同じ
const STAND_NUMBER_RANGES: Record<Exclude<StandSettingKey, 'fetch_interval_sec'>, [number, number]> = {
    summary_max_chars: [0, 1000],
    title_max_chars: [20, 500],
    max_items_per_push: [0, 10],
};
const STAND_INTERVAL_PRESETS_HOURS = [1, 3, 6, 12, 24];

function formatInterval(seconds: number): string {
    if (seconds % 3600 === 0) {
        return uiText("components.RightSidebar.text029", { p1: seconds / 3600 });
    }
    return uiText("components.RightSidebar.text030", { p1: Math.round(seconds / 60) });
}

function errorDetailText(data: any, status: number): string {
    // FastAPI の 422 (入力検証) は detail が配列で届くので、文字列のときだけそのまま出す
    if (data && typeof data.detail === 'string') return data.detail;
    return `HTTP ${status}`;
}

interface NumberFieldState {
    useDefault: boolean;
    value: string;
}

function FeedStandModal({ fixture, state, onClose, onFixtureUpdated }: {
    fixture: Fixture;
    state: Record<string, any> | null;
    onClose: () => void;
    onFixtureUpdated: () => void;
}) {
    useLocale();
    const [isMetaEditing, setIsMetaEditing] = useState(false);
    const display = state && typeof state.feed_stand === 'object' && state.feed_stand ? state.feed_stand : null;
    const subscriptions: string[] = Array.isArray(display?.subscriptions) ? display.subscriptions : [];
    const latest: string[] = Array.isArray(display?.latest) ? display.latest : [];

    const [loadState, setLoadState] = useState<'loading' | 'ready' | 'error'>('loading');
    const [loadError, setLoadError] = useState<string | null>(null);
    const [defaults, setDefaults] = useState<StandSettings | null>(null);
    // 取得間隔は選択肢 ('' = 既定)、他の 3 欄は「既定値を使う」+ 数値入力
    const [intervalChoice, setIntervalChoice] = useState<string>('');
    const [savedInterval, setSavedInterval] = useState<number | null>(null);
    const [fields, setFields] = useState<Record<Exclude<StandSettingKey, 'fetch_interval_sec'>, NumberFieldState>>({
        summary_max_chars: { useDefault: true, value: '' },
        title_max_chars: { useDefault: true, value: '' },
        max_items_per_push: { useDefault: true, value: '' },
    });
    const [saving, setSaving] = useState(false);
    const [saveMessage, setSaveMessage] = useState<{ kind: 'ok' | 'error'; text: string } | null>(null);

    const applyConfig = useCallback((config: StandConfig, defs: StandSettings) => {
        setDefaults(defs);
        setSavedInterval(config.fetch_interval_sec);
        setIntervalChoice(config.fetch_interval_sec == null ? '' : String(config.fetch_interval_sec));
        setFields({
            summary_max_chars: {
                useDefault: config.summary_max_chars == null,
                value: String(config.summary_max_chars ?? defs.summary_max_chars),
            },
            title_max_chars: {
                useDefault: config.title_max_chars == null,
                value: String(config.title_max_chars ?? defs.title_max_chars),
            },
            max_items_per_push: {
                useDefault: config.max_items_per_push == null,
                value: String(config.max_items_per_push ?? defs.max_items_per_push),
            },
        });
    }, []);

    useEffect(() => {
        let cancelled = false;
        (async () => {
            setLoadState('loading');
            try {
                const res = await apiFetch('/api/feeds/fixtures');
                const data = await res.json().catch(() => null);
                if (cancelled) return;
                if (!res.ok) {
                    setLoadError(errorDetailText(data, res.status));
                    setLoadState('error');
                    return;
                }
                const entry = Array.isArray(data)
                    ? data.find((f: { fixture_id: string }) => f.fixture_id === fixture.id)
                    : null;
                if (!entry || !entry.config || !entry.defaults) {
                    setLoadError(`HTTP ${res.status}`);
                    setLoadState('error');
                    return;
                }
                applyConfig(entry.config, entry.defaults);
                setLoadState('ready');
            } catch (e) {
                if (cancelled) return;
                setLoadError(String(e));
                setLoadState('error');
            }
        })();
        return () => { cancelled = true; };
    }, [fixture.id, applyConfig]);

    const numberLabels: Record<Exclude<StandSettingKey, 'fetch_interval_sec'>, { label: string; help: string; unit: (n: number) => string }> = {
        summary_max_chars: {
            label: uiText("components.RightSidebar.text031"),
            help: uiText("components.RightSidebar.text032"),
            unit: (n) => uiText("components.RightSidebar.text045", { p1: n }),
        },
        title_max_chars: {
            label: uiText("components.RightSidebar.text033"),
            help: uiText("components.RightSidebar.text034"),
            unit: (n) => uiText("components.RightSidebar.text045", { p1: n }),
        },
        max_items_per_push: {
            label: uiText("components.RightSidebar.text035"),
            help: uiText("components.RightSidebar.text036"),
            unit: (n) => uiText("components.RightSidebar.text046", { p1: n }),
        },
    };

    const handleSave = async () => {
        setSaveMessage(null);
        const body: StandConfig = {
            fetch_interval_sec: intervalChoice === '' ? null : Number(intervalChoice),
            summary_max_chars: null,
            title_max_chars: null,
            max_items_per_push: null,
        };
        for (const key of Object.keys(STAND_NUMBER_RANGES) as Array<keyof typeof STAND_NUMBER_RANGES>) {
            const field = fields[key];
            if (field.useDefault) continue;
            const [low, high] = STAND_NUMBER_RANGES[key];
            const trimmed = field.value.trim();
            const parsed = Number(trimmed);
            if (trimmed === '' || !Number.isInteger(parsed) || parsed < low || parsed > high) {
                setSaveMessage({
                    kind: 'error',
                    text: uiText("components.RightSidebar.text044", { p1: numberLabels[key].label, p2: low, p3: high }),
                });
                return;
            }
            body[key] = parsed;
        }
        setSaving(true);
        try {
            const res = await apiFetch(`/api/feeds/fixtures/${encodeURIComponent(fixture.id)}/config`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(body),
            });
            const data = await res.json().catch(() => null);
            if (!res.ok) {
                setSaveMessage({ kind: 'error', text: uiText("components.RightSidebar.text041", { p1: errorDetailText(data, res.status) }) });
                return;
            }
            if (data?.config && data?.defaults) applyConfig(data.config, data.defaults);
            setSaveMessage({ kind: 'ok', text: uiText("components.RightSidebar.text040") });
        } catch (e) {
            setSaveMessage({ kind: 'error', text: uiText("components.RightSidebar.text041", { p1: String(e) }) });
        } finally {
            setSaving(false);
        }
    };

    // 既定の選択肢 + 1/3/6/12/24 時間。保存値が選択肢に無い値 (API で直接
    // 設定された等) なら、その値も選択肢に足して黙って失わない。
    const intervalOptions = STAND_INTERVAL_PRESETS_HOURS.map(h => h * 3600);
    if (savedInterval != null && !intervalOptions.includes(savedInterval)) {
        intervalOptions.push(savedInterval);
        intervalOptions.sort((a, b) => a - b);
    }

    return (
        <ModalOverlay onClose={onClose} className={fixtureStyles.overlay}>
            <div className={fixtureStyles.modal} onClick={e => e.stopPropagation()}>
                <FixtureModalHeader
                    fixture={fixture}
                    isMetaEditing={isMetaEditing}
                    onStartMetaEdit={() => setIsMetaEditing(true)}
                    onClose={onClose}
                />
                {isMetaEditing && (
                    <FixtureMetaEditor
                        fixture={fixture}
                        onCancel={() => setIsMetaEditing(false)}
                        onChanged={onFixtureUpdated}
                    />
                )}
                <div className={fixtureStyles.content}>
                    {!isMetaEditing && fixture.description && (
                        <p className={fixtureStyles.description}>{fixture.description}</p>
                    )}

                    <h4 data-i18n="components.RightSidebar.text020" className={fixtureStyles.sectionTitle}>{uiText("components.RightSidebar.text020")}</h4>
                    {subscriptions.length > 0 ? (
                        <ul className={fixtureStyles.feedList}>
                            {subscriptions.map((title, i) => (
                                <li key={`sub-${i}`} className={fixtureStyles.feedListItem}>{title}</li>
                            ))}
                        </ul>
                    ) : (
                        <div data-i18n="components.RightSidebar.text021" className={fixtureStyles.emptyInline}>{uiText("components.RightSidebar.text021")}</div>
                    )}

                    <h4 data-i18n="components.RightSidebar.text022" className={fixtureStyles.sectionTitle}>{uiText("components.RightSidebar.text022")}</h4>
                    {latest.length > 0 ? (
                        <ul className={fixtureStyles.feedList}>
                            {latest.map((title, i) => (
                                <li key={`latest-${i}`} className={fixtureStyles.feedListItem}>{title}</li>
                            ))}
                        </ul>
                    ) : (
                        <div data-i18n="components.RightSidebar.text023" className={fixtureStyles.emptyInline}>{uiText("components.RightSidebar.text023")}</div>
                    )}

                    <div className={fixtureStyles.settingsSection}>
                        <h4 data-i18n="components.RightSidebar.text024" className={fixtureStyles.sectionTitle}>{uiText("components.RightSidebar.text024")}</h4>
                        <p data-i18n="components.RightSidebar.text025" className={fixtureStyles.settingsLead}>{uiText("components.RightSidebar.text025")}</p>

                        {loadState === 'loading' && (
                            <div data-i18n="components.RightSidebar.text043" className={fixtureStyles.emptyInline}>{uiText("components.RightSidebar.text043")}</div>
                        )}
                        {loadState === 'error' && (
                            <div className={fixtureStyles.errorText}>{uiText("components.RightSidebar.text042", { p1: loadError ?? '' })}</div>
                        )}
                        {loadState === 'ready' && defaults && (
                            <>
                                <div className={fixtureStyles.settingRow}>
                                    <label data-i18n="components.RightSidebar.text026" className={fixtureStyles.settingLabel} htmlFor={`feed-interval-${fixture.id}`}>
                                        {uiText("components.RightSidebar.text026")}
                                    </label>
                                    <select
                                        id={`feed-interval-${fixture.id}`}
                                        className={fixtureStyles.settingSelect}
                                        value={intervalChoice}
                                        onChange={e => { setIntervalChoice(e.target.value); setSaveMessage(null); }}
                                        disabled={saving}
                                    >
                                        <option value="">{uiText("components.RightSidebar.text028", { p1: formatInterval(defaults.fetch_interval_sec) })}</option>
                                        {intervalOptions.map(sec => (
                                            <option key={sec} value={String(sec)}>{formatInterval(sec)}</option>
                                        ))}
                                    </select>
                                    <div data-i18n="components.RightSidebar.text027" className={fixtureStyles.settingHelp}>{uiText("components.RightSidebar.text027")}</div>
                                </div>

                                {(Object.keys(STAND_NUMBER_RANGES) as Array<keyof typeof STAND_NUMBER_RANGES>).map(key => {
                                    const field = fields[key];
                                    const [low, high] = STAND_NUMBER_RANGES[key];
                                    const meta = numberLabels[key];
                                    const inputId = `feed-${key}-${fixture.id}`;
                                    return (
                                        <div key={key} className={fixtureStyles.settingRow}>
                                            <label className={fixtureStyles.settingLabel} htmlFor={inputId}>{meta.label}</label>
                                            <div className={fixtureStyles.settingControls}>
                                                <label className={fixtureStyles.defaultToggle}>
                                                    <input
                                                        type="checkbox"
                                                        checked={field.useDefault}
                                                        disabled={saving}
                                                        onChange={e => {
                                                            const useDefault = e.target.checked;
                                                            setFields(prev => ({
                                                                ...prev,
                                                                [key]: {
                                                                    useDefault,
                                                                    // 既定値へ戻すときは入力欄も既定値の表示に揃える
                                                                    value: useDefault ? String(defaults[key]) : prev[key].value,
                                                                },
                                                            }));
                                                            setSaveMessage(null);
                                                        }}
                                                    />
                                                    {uiText("components.RightSidebar.text037", { p1: meta.unit(defaults[key]) })}
                                                </label>
                                                <input
                                                    id={inputId}
                                                    type="number"
                                                    className={fixtureStyles.settingInput}
                                                    min={low}
                                                    max={high}
                                                    step={1}
                                                    value={field.useDefault ? String(defaults[key]) : field.value}
                                                    disabled={field.useDefault || saving}
                                                    onChange={e => {
                                                        const value = e.target.value;
                                                        setFields(prev => ({ ...prev, [key]: { ...prev[key], value } }));
                                                        setSaveMessage(null);
                                                    }}
                                                />
                                            </div>
                                            <div className={fixtureStyles.settingHelp}>{meta.help}</div>
                                        </div>
                                    );
                                })}

                                <div className={fixtureStyles.settingActions}>
                                    <button
                                        className={fixtureStyles.saveButton}
                                        onClick={handleSave}
                                        disabled={saving}
                                    >
                                        {saving ? uiText("components.RightSidebar.text039") : uiText("components.RightSidebar.text038")}
                                    </button>
                                    {saveMessage && (
                                        <span className={saveMessage.kind === 'ok' ? fixtureStyles.saveOk : fixtureStyles.errorText}>
                                            {saveMessage.text}
                                        </span>
                                    )}
                                </div>
                            </>
                        )}
                    </div>
                </div>
            </div>
        </ModalOverlay>
    );
}
