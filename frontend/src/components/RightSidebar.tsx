import { useRef, useState, useEffect } from 'react';
import styles from './RightSidebar.module.css';
import {
    Users,
    FileText,
    Image as ImageIcon,
    File,
    Eye,
    EyeOff,
    Settings,
    Package
} from 'lucide-react';
import ItemModal from './ItemModal';
import PersonaMenu from './PersonaMenu';
import MemoryModal from './MemoryModal';
import ScheduleModal from './ScheduleModal';
import TasksModal from './TasksModal';
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
}

interface Occupant {
    id: string;
    name: string;
    avatar?: string;
}

interface Item {
    id: string;
    name: string;
    type: 'document' | 'picture' | 'bag' | 'other';
    description?: string;
    is_open?: boolean;  // Whether item content is included in visual context
    contained_items?: Item[];  // For bag type: items inside this bag
    contained_count?: number;  // Number of items directly inside this bag
}

interface BuildingDetails {
    id: string;
    name: string;
    description: string;
    image_path?: string | null;  // Building interior image
    occupants: Occupant[];
    items: Item[];
}

export default function RightSidebar({ isOpen, onClose, refreshTrigger, currentBuildingId }: RightSidebarProps) {
    const [details, setDetails] = useState<BuildingDetails | null>(null);
    const [selectedItem, setSelectedItem] = useState<Item | null>(null);
    const [selectedPersona, setSelectedPersona] = useState<Occupant | null>(null);

    // Modal States
    const [showMemory, setShowMemory] = useState(false);
    const [showSchedule, setShowSchedule] = useState(false);
    const [showTasks, setShowTasks] = useState(false);
    const [showSettings, setShowSettings] = useState(false);
    const [showInventory, setShowInventory] = useState(false);
    const [showBuildingSettings, setShowBuildingSettings] = useState(false);

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

    const startX = useRef<number | null>(null);
    const startY = useRef<number | null>(null);
    const startTime = useRef<number | null>(null);



    const fetchDetails = async () => {
        // currentBuildingId 未指定だと server-global の user_current_building_id に
        // 汚染される (エリス上書き事故の遠因)。明示指定がない間は fetch しない。
        if (!currentBuildingId) {
            console.warn('[RightSidebar] fetchDetails skipped: currentBuildingId not provided yet');
            return;
        }
        try {
            const res = await fetch(`/api/info/details?building_id=${encodeURIComponent(currentBuildingId)}`);
            if (res.ok) {
                const data = await res.json();
                setDetails(data);
            }
        } catch (err) {
            console.error("Failed to fetch building details", err);
        }
    };

    const handleToggleOpen = async (e: React.MouseEvent, item: Item) => {
        e.stopPropagation(); // Don't open the item modal
        try {
            const res = await fetch(`/api/info/item/${item.id}/toggle-open`, {
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

    useEffect(() => {
        fetchDetails();
        // currentBuildingId が変われば自動で再 fetch (deps に含める)
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [refreshTrigger, isOpen, currentBuildingId]);

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
        setShowTasks(false);
        setShowSettings(false);
        setShowInventory(false);
        setShowBuildingSettings(false);
    }, [details?.id]);

    // Polling for real-time updates when sidebar is open
    useEffect(() => {
        if (!isOpen) return;

        const pollInterval = setInterval(() => {
            fetchDetails();
        }, 10000); // Poll every 10 seconds

        return () => clearInterval(pollInterval);
    }, [isOpen]);

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
    const openModal = (type: 'memory' | 'schedule' | 'tasks' | 'settings' | 'inventory') => {
        if (!selectedPersona) return;
        const newId = selectedPersona.id;
        const newName = selectedPersona.name;

        const anyOpen = showMemory || showSchedule || showTasks || showSettings || showInventory;
        const sameTarget = anyOpen && activeModalPersonaId === newId;

        const applyOpen = () => {
            setActiveModalPersonaId(newId);
            setActiveModalPersonaName(newName);
            if (type === 'memory') setShowMemory(true);
            if (type === 'schedule') setShowSchedule(true);
            if (type === 'tasks') setShowTasks(true);
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
            setShowTasks(false);
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
                            <h3 className={styles.heading}>現在地</h3>
                            <div className={styles.content}>
                                <div className={styles.buildingHeader}>
                                    <div className={styles.buildingName}>{details.name}</div>
                                    <button
                                        className={styles.settingsBtn}
                                        onClick={() => setShowBuildingSettings(true)}
                                        title="Building設定"
                                    >
                                        <Settings size={16} />
                                    </button>
                                </div>
                                <div className={styles.description}>
                                    {details.description || "説明がありません"}
                                </div>
                            </div>
                        </div>

                        {/* Building Interior Image */}
                        {details.image_path && (
                            <div className={styles.section}>
                                <h3 className={styles.heading}>
                                    <ImageIcon size={16} /> インテリア
                                </h3>
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

                        <div className={styles.section}>
                            <h3 className={styles.heading}>
                                <Users size={16} /> 滞在ペルソナ ({details.occupants.length})
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
                                            <span className={styles.occupantName}>{user.name}</span>
                                        </div>
                                    ))
                                ) : (
                                    <div className={styles.empty}>ここには誰もいません</div>
                                )}
                            </div>
                        </div>

                        <div className={styles.section}>
                            <h3 className={styles.heading}>
                                <FileText size={16} /> アイテム ({details.items.length})
                            </h3>
                            <div className={styles.grid}>
                                {details.items.length > 0 ? (
                                    details.items.map(item => (
                                        <div
                                            key={item.id}
                                            className={`${styles.card} ${styles[item.type]} ${item.is_open ? styles.itemOpen : ''}`}
                                            onClick={() => setSelectedItem(item)}
                                        >
                                            <div className={styles.cardIcon}>
                                                {item.type === 'picture' ? <ImageIcon size={20} />
                                                    : item.type === 'bag' ? <Package size={20} />
                                                    : <File size={20} />}
                                            </div>
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
                                            {(item.type === 'picture' || item.type === 'document' || item.type === 'bag') && (
                                                <button
                                                    className={`${styles.toggleOpenBtn} ${item.is_open ? styles.isOpen : ''}`}
                                                    onClick={(e) => handleToggleOpen(e, item)}
                                                    title={item.is_open ? 'AIコンテキストから除外' : 'AIコンテキストに含める'}
                                                >
                                                    {item.is_open ? <Eye size={16} /> : <EyeOff size={16} />}
                                                </button>
                                            )}
                                        </div>
                                    ))
                                ) : (
                                    <div className={styles.empty}>アイテムはありません</div>
                                )}
                            </div>
                        </div>
                    </>
                ) : (
                    <div style={{ padding: '1rem', color: '#6b7280' }}>読み込み中...</div>
                )}
            </aside>

            {/* Modals & Menus */}
            {/* Note: We wrapper these in a div that stops propagation to prevent sidebar gestures from affecting them if they bubble up */}
            <div onTouchStart={(e) => e.stopPropagation()} onTouchMove={(e) => e.stopPropagation()}>
                <ItemModal
                    isOpen={!!selectedItem}
                    onClose={() => setSelectedItem(null)}
                    item={selectedItem}
                    onItemUpdated={() => {
                        fetchDetails();
                        setSelectedItem(null);  // Close modal after update
                    }}
                />

                {selectedPersona && (
                    <PersonaMenu
                        isOpen={!!selectedPersona}
                        onClose={() => setSelectedPersona(null)}
                        personaId={selectedPersona.id}
                        personaName={selectedPersona.name}
                        avatarUrl={selectedPersona.avatar || "/api/static/icons/host.png"}
                        onOpenMemory={() => openModal('memory')}
                        onOpenSchedule={() => openModal('schedule')}
                        onOpenTasks={() => openModal('tasks')}
                        onOpenSettings={() => openModal('settings')}
                        onOpenInventory={() => openModal('inventory')}
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
                        <TasksModal
                            isOpen={showTasks}
                            onClose={() => setShowTasks(false)}
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

                {/* Building Settings Modal */}
                {details && (
                    <BuildingSettingsModal
                        isOpen={showBuildingSettings}
                        onClose={() => setShowBuildingSettings(false)}
                        buildingId={details.id}
                        onSaved={() => fetchDetails()}
                    />
                )}
            </div>
        </>
    );
}
