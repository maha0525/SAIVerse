"use client";

import { useEffect, useState, useRef, type ReactNode } from 'react';
import styles from './Sidebar.module.css';
import { Settings, Zap, BarChart2, UserPlus, Plus, X, HelpCircle, User, Bell, Package, AlertTriangle, ChevronRight, ChevronDown, Newspaper } from 'lucide-react';
import GlobalSettingsModal from './GlobalSettingsModal';
import UserProfileModal from './UserProfileModal';
import PersonaWizard from './PersonaWizard';
import TutorialSelectModal from './tutorial/TutorialSelectModal';
import AddonManagerModal from './AddonManagerModal';
import EventsModal from './EventsModal';

interface UserStatus {
    is_online: boolean;  // Backward compatibility
    presence_status?: string;  // "online", "away", "offline"
    current_building_id: string | null;
    avatar: string | null;
    display_name: string;
    email?: string | null;
}

interface Building {
    id: string;
    name: string;
    // 所属スコープ (Region / SubRegion の ID)。null なら City 直属
    region_id?: string | null;
    // この Building がいずれかの Region の入口なら、その region_id
    entrance_of?: string | null;
}

interface RegionInfo {
    region_id: string;
    name: string;
    parent_region_id?: string | null;
    region_type?: string;
    entrance_building_id?: string | null;
}

interface SidebarProps {
    onMove?: (buildingId?: string) => void;
    isOpen: boolean;
    onOpen: () => void;
    onClose: () => void;
    refreshTrigger?: boolean;
    // C-1 閲覧モード: UI 上で閲覧中の building (= page.tsx の currentBuildingId)。
    // status.current_building_id (= サーバの真の現在地) と分離して active hilight
    // に使う。 両者が乖離している時が「閲覧モード」 (= 別建物を覗いている状態)。
    viewingBuildingId?: string | null;
    // サーバ側 CURRENT_BUILDINGID が変わったタイミングを通知するトリガー。
    // /chat/utter の auto-move 成功時に page.tsx から increment される。
    // 受け取った Sidebar は status を再 fetch して D-1 マーカー位置を更新する。
    serverMoveTrigger?: number;
}

export default function Sidebar({ onMove, isOpen, onOpen, onClose, refreshTrigger, viewingBuildingId, serverMoveTrigger }: SidebarProps) {
    const [status, setStatus] = useState<UserStatus | null>(null);
    const [buildings, setBuildings] = useState<Building[]>([]);
    const [regions, setRegions] = useState<RegionInfo[]>([]);
    // Region 折り畳み (docs/intent/region.md §2.2): 展開中の region_id 集合
    const [expandedRegions, setExpandedRegions] = useState<Set<string>>(new Set());
    const [cityId, setCityId] = useState<number | null>(null);
    const [isSettingsOpen, setIsSettingsOpen] = useState(false);
    const [isProfileModalOpen, setIsProfileModalOpen] = useState(false);
    const [isWizardOpen, setIsWizardOpen] = useState(false);
    const [isCreateBuildingOpen, setIsCreateBuildingOpen] = useState(false);
    const [newBuildingName, setNewBuildingName] = useState('');
    const [isCreatingBuilding, setIsCreatingBuilding] = useState(false);
    const [isTutorialSelectOpen, setIsTutorialSelectOpen] = useState(false);
    const [isAddonManagerOpen, setIsAddonManagerOpen] = useState(false);
    // できごとはページ遷移でなくモーダルで開く (チャットの書きかけメッセージを失わないため)
    const [isEventsOpen, setIsEventsOpen] = useState(false);
    const [developerMode, setDeveloperMode] = useState(false);
    const [quarantinedIds, setQuarantinedIds] = useState<Set<string>>(new Set());

    useEffect(() => {
        let cancelled = false;
        async function fetchQuarantine() {
            try {
                const res = await fetch('/api/system/quarantine');
                if (!res.ok || cancelled) return;
                const data = await res.json();
                const ids = new Set<string>(
                    (data.quarantined || []).map((q: { building_id: string }) => q.building_id)
                );
                setQuarantinedIds(ids);
            } catch {
                // silent
            }
        }
        fetchQuarantine();
        // Listen for quarantine resolution events (restore/reset from modal)
        // so the sidebar warning indicator clears immediately.
        const handleResolved = () => fetchQuarantine();
        window.addEventListener('quarantine-resolved', handleResolved);
        return () => {
            cancelled = true;
            window.removeEventListener('quarantine-resolved', handleResolved);
        };
    }, [refreshTrigger]);

    // Swipe Logic for Control
    const startX = useRef<number | null>(null);
    const startY = useRef<number | null>(null);
    const startTime = useRef<number | null>(null);

    const refreshData = async () => {
        try {
            const [statusRes, buildingsRes, devModeRes] = await Promise.all([
                fetch('/api/user/status'),
                fetch('/api/user/buildings'),
                fetch('/api/config/developer-mode')
            ]);
            if (statusRes.ok) setStatus(await statusRes.json());
            if (buildingsRes.ok) {
                const data = await buildingsRes.json();
                setBuildings(data.buildings || []);
                setRegions(data.regions || []);
                if (data.city_id != null) setCityId(data.city_id);
            }
            if (devModeRes.ok) {
                const data = await devModeRes.json();
                setDeveloperMode(data.enabled);
            }
        } catch (err) {
            console.error("Sidebar fetch error", err);
        }
    };

    // Fetch data on mount, refreshTrigger change (e.g. backend reconnect), and
    // serverMoveTrigger change (= /chat/utter で auto-move 後の D-1 マーカー追従)
    useEffect(() => {
        refreshData();
    }, [refreshTrigger, serverMoveTrigger]);

    // 閲覧中 / 現在地の Building が Region 内部にあるとき、その Region チェーンを
    // 自動展開する (折り畳まれていて自分の居場所が見えない状態を防ぐ)
    useEffect(() => {
        const regionById = new Map(regions.map(r => [r.region_id, r]));
        const buildingById = new Map(buildings.map(b => [b.id, b]));
        const chain = (bid: string | null | undefined): string[] => {
            const out: string[] = [];
            let rid = bid ? buildingById.get(bid)?.region_id ?? null : null;
            while (rid && !out.includes(rid)) {
                out.push(rid);
                rid = regionById.get(rid)?.parent_region_id ?? null;
            }
            return out;
        };
        const toExpand = [...chain(viewingBuildingId), ...chain(status?.current_building_id)];
        if (toExpand.length === 0) return;
        setExpandedRegions(prev => {
            const next = new Set(prev);
            let changed = false;
            for (const rid of toExpand) {
                if (!next.has(rid)) { next.add(rid); changed = true; }
            }
            return changed ? next : prev;
        });
    }, [buildings, regions, viewingBuildingId, status?.current_building_id]);

    const toggleRegion = (regionId: string) => {
        setExpandedRegions(prev => {
            const next = new Set(prev);
            if (next.has(regionId)) next.delete(regionId); else next.add(regionId);
            return next;
        });
    };

    // Global Touch Handlers for swipe-to-open (separate effect to avoid re-fetching on onOpen change)
    useEffect(() => {
        const handleTouchStart = (e: TouchEvent) => {
            startX.current = e.touches[0].clientX;
            startY.current = e.touches[0].clientY;
            startTime.current = Date.now();
        };

        const handleTouchMove = (e: TouchEvent) => {
            if (startX.current === null || startY.current === null || startTime.current === null) return;

            const currentX = e.touches[0].clientX;
            const currentY = e.touches[0].clientY;
            const diffX = currentX - startX.current;
            const diffY = currentY - startY.current;
            const timeDiff = Date.now() - startTime.current;

            // Vertical scroll preference check - if vertical movement is dominant, abort
            if (Math.abs(diffY) > Math.abs(diffX)) {
                startX.current = null;
                startY.current = null;
                startTime.current = null;
                return;
            }

            // Time limit for quick swipe - only detect gesture within 300ms
            if (timeDiff > 300) {
                startX.current = null;
                startY.current = null;
                startTime.current = null;
                return;
            }

            // If swiped right > 100px quickly, open
            if (diffX > 100) {
                onOpen();
                startX.current = null;
                startY.current = null;
                startTime.current = null;
            }
        };

        window.addEventListener('touchstart', handleTouchStart);
        window.addEventListener('touchmove', handleTouchMove);

        return () => {
            window.removeEventListener('touchstart', handleTouchStart);
            window.removeEventListener('touchmove', handleTouchMove);
        };
    }, [onOpen]);

    const handleMove = (buildingId: string) => {
        // C-1 閲覧モード: サイドバーで建物をクリックしても /api/user/move は
        // 呼ばない。 サーバ側の CURRENT_BUILDINGID は据え置きのまま、 UI 上の
        // ビュー (= history / occupants / chat 表示) だけ切り替える。 実際の
        // 入室は発言時に /api/chat/utter が atomic に行う (= C-2)。
        // See: docs/intent/building_memory_unified.md §C
        if (!status || status.current_building_id === buildingId) {
            // 同じ建物のクリックは noop だが、 onClose で sidebar 自体は閉じる
            if (onMove) onMove(buildingId);
            onClose();
            return;
        }
        if (onMove) onMove(buildingId);
        onClose();
    };

    const handleCreateBuilding = async () => {
        if (!newBuildingName.trim() || cityId == null) return;
        setIsCreatingBuilding(true);
        try {
            const res = await fetch('/api/world/buildings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name: newBuildingName.trim(),
                    description: '',
                    capacity: 10,
                    system_instruction: '',
                    city_id: cityId,
                })
            });
            if (res.ok) {
                setNewBuildingName('');
                setIsCreateBuildingOpen(false);
                refreshData();
            }
        } catch (err) {
            console.error("Create building error", err);
        } finally {
            setIsCreatingBuilding(false);
        }
    };

    // Handler for closing swipe (on the sidebar itself)
    const handleSidebarTouchStart = (e: React.TouchEvent) => {
        e.stopPropagation();
        startX.current = e.touches[0].clientX;
        startY.current = e.touches[0].clientY;
        startTime.current = Date.now();
    };

    const handleSidebarTouchMove = (e: React.TouchEvent) => {
        e.stopPropagation();
        if (startX.current === null || startY.current === null || startTime.current === null) return;

        const currentX = e.touches[0].clientX;
        const currentY = e.touches[0].clientY;
        const diffX = currentX - startX.current;
        const diffY = currentY - startY.current;
        const timeDiff = Date.now() - startTime.current;

        // Vertical scroll preference check
        if (Math.abs(diffY) > Math.abs(diffX)) {
            startX.current = null;
            startY.current = null;
            startTime.current = null;
            return;
        }

        // Time limit for quick swipe
        if (timeDiff > 300) {
            startX.current = null;
            startY.current = null;
            startTime.current = null;
            return;
        }

        // Swipe Left <- (-50px)
        if (diffX < -50) {
            onClose();
            startX.current = null;
            startY.current = null;
            startTime.current = null;
        }
    };

    return (
        <>
            {/* Mobile Overlay */}
            <div
                className={`${styles.overlay} ${isOpen ? styles.visible : ''}`}
                onClick={(e) => {
                    e.stopPropagation();
                    onClose();
                }}
                onTouchStart={handleSidebarTouchStart} // Allow swipe-to-close on overlay
                onTouchMove={handleSidebarTouchMove}
            />

            <aside
                className={`${styles.sidebar} ${isOpen ? styles.open : ''}`}
                onTouchStart={handleSidebarTouchStart}
                onTouchMove={handleSidebarTouchMove}
            >
                {/* Brand + Create Persona */}
                <div className={styles.brandRow}>
                    <img
                        src="/icon.jpg"
                        alt="SAIVerse"
                        className={styles.brandIcon}
                    />
                    <button
                        onClick={() => setIsWizardOpen(true)}
                        className={styles.createPersonaBtn}
                    >
                        <UserPlus size={18} />
                        ペルソナ作成
                    </button>
                </div>

                {/* Navigation */}
                <div className={styles.sectionTitleRow}>
                    <div className={styles.sectionTitle}>場所</div>
                    <button
                        className={styles.addBuildingBtn}
                        onClick={() => setIsCreateBuildingOpen(v => !v)}
                        title="Buildingを作成"
                    >
                        {isCreateBuildingOpen ? <X size={14} /> : <Plus size={14} />}
                    </button>
                </div>
                {isCreateBuildingOpen && (
                    <div className={styles.createBuildingForm}>
                        <input
                            type="text"
                            className={styles.createBuildingInput}
                            placeholder="Building名..."
                            value={newBuildingName}
                            onChange={e => setNewBuildingName(e.target.value)}
                            onKeyDown={e => { if (e.key === 'Enter') handleCreateBuilding(); }}
                            autoFocus
                        />
                        <button
                            className={styles.createBuildingSubmit}
                            onClick={handleCreateBuilding}
                            disabled={!newBuildingName.trim() || isCreatingBuilding}
                        >
                            {isCreatingBuilding ? '...' : '作成'}
                        </button>
                    </div>
                )}
                <div className={styles.buildingList}>
                    {(() => {
                        // Region 折り畳み (docs/intent/region.md §2.2):
                        // トップレベル = City 直属の Building (トップ Region の入口を
                        // 自然に含む)。入口 Building は展開トグル付きで描画し、展開時に
                        // その Region 直属の Building (SubRegion の入口を含む) を再帰
                        // 表示する。入口を持たない Region の内部は到達不能にならない
                        // ようトップレベルへフォールバック表示する。
                        const regionsWithEntrance = new Set(
                            regions.filter(r => r.entrance_building_id).map(r => r.region_id)
                        );
                        const renderBuildingItem = (b: Building, depth: number): ReactNode => {
                            const isQuarantined = quarantinedIds.has(b.id);
                            const childRegionId = b.entrance_of ?? null;
                            const isExpanded = !!childRegionId && expandedRegions.has(childRegionId);
                            const children = (childRegionId && isExpanded)
                                ? buildings.filter(cb => cb.region_id === childRegionId)
                                : [];
                            return (
                                <div key={b.id}>
                                    <div
                                        // active hilight は 「閲覧中の building」 (= viewing)。
                                        // 「サーバ上の真の現在地」 は別途 D-1 マーカー (User
                                        // アイコン) で示す。 両者は閲覧モード中に乖離する。
                                        className={`${styles.buildingItem} ${(viewingBuildingId ?? status?.current_building_id) === b.id ? styles.active : ''}`}
                                        onClick={() => isQuarantined ? null : handleMove(b.id)}
                                        style={{
                                            ...(depth > 0 ? { marginLeft: `${depth * 14}px` } : {}),
                                            ...(isQuarantined ? { opacity: 0.6, cursor: 'not-allowed' } : {}),
                                        }}
                                        title={isQuarantined ? 'このビルディングは会話履歴ファイルが破損しているため隔離中です。アラートバナーから対応してください。' : undefined}
                                    >
                                        {childRegionId && (
                                            <button
                                                onClick={(e) => { e.stopPropagation(); toggleRegion(childRegionId); }}
                                                title={isExpanded ? '折り畳む' : '中を見る'}
                                                style={{
                                                    background: 'none', border: 'none', padding: 0,
                                                    cursor: 'pointer', color: 'inherit',
                                                    display: 'flex', alignItems: 'center',
                                                }}
                                            >
                                                {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                                            </button>
                                        )}
                                        <span>{b.name}</span>
                                        {isQuarantined && (
                                            <AlertTriangle size={14} style={{ color: '#ff6666' }} />
                                        )}
                                        {/* D-1 現在地マーカー: サーバ上の真の現在地に常に表示。
                                            閲覧モード中 (= viewing != server-current) は
                                            active hilight と乖離して、 「自分は今そこではなく
                                            別の場所に居る」 が一瞥で分かる。 */}
                                        {status?.current_building_id === b.id && <User size={14} style={{ opacity: 0.8 }} />}
                                    </div>
                                    {children.map(cb => renderBuildingItem(cb, depth + 1))}
                                </div>
                            );
                        };
                        const topLevel = buildings.filter(
                            b => !b.region_id || !regionsWithEntrance.has(b.region_id)
                        );
                        return topLevel.map(b => renderBuildingItem(b, 0));
                    })()}
                </div>

                {/* System Section */}
                <div className={styles.sectionTitle}>システム</div>
                <div className={styles.buildingList} style={{ flex: 'none', marginBottom: '1rem' }}>
                    {developerMode && (
                        <div
                            className={styles.buildingItem}
                            onClick={() => {
                                window.location.href = '/phenomena';
                                if (onClose) onClose();
                            }}
                        >
                            <span style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                                <Zap size={16} /> フェノメノン
                            </span>
                        </div>
                    )}
                    <div
                        className={styles.buildingItem}
                        onClick={() => setIsEventsOpen(true)}
                    >
                        <span style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                            <Newspaper size={16} /> できごと
                        </span>
                    </div>
                    <div
                        className={styles.buildingItem}
                        onClick={() => {
                            window.location.href = '/announcements';
                            if (onClose) onClose();
                        }}
                    >
                        <span style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                            <Bell size={16} /> お知らせ
                        </span>
                    </div>
                    <div
                        className={styles.buildingItem}
                        onClick={() => {
                            window.location.href = '/usage';
                            if (onClose) onClose();
                        }}
                    >
                        <span style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                            <BarChart2 size={16} /> API使用状況
                        </span>
                    </div>
                    <div
                        className={styles.buildingItem}
                        onClick={() => setIsTutorialSelectOpen(true)}
                    >
                        <span style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                            <HelpCircle size={16} /> チュートリアル
                        </span>
                    </div>
                    <div
                        className={styles.buildingItem}
                        onClick={() => setIsAddonManagerOpen(true)}
                    >
                        <span style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                            <Package size={16} /> アドオン
                        </span>
                    </div>
                </div>

                {/* Footer: Settings + Profile */}
                <div className={styles.settingsFooter}>
                    <div className={styles.footerRow}>
                        <button
                            onClick={() => setIsSettingsOpen(true)}
                            className={styles.settingsBtnIcon}
                            title="設定"
                        >
                            <Settings size={20} />
                        </button>
                        <div
                            className={styles.profileCompact}
                            onClick={() => setIsProfileModalOpen(true)}
                        >
                            <img
                                src={status?.avatar || "/api/static/icons/user.png"}
                                alt="User"
                                className={styles.avatarSmall}
                                onError={(e) => { e.currentTarget.src = "https://placehold.co/32x32?text=U"; }}
                            />
                            <span className={styles.userNameSmall}>{status?.display_name || "Guest"}</span>
                            <span className={`${styles.statusDot} ${styles[status?.presence_status || 'offline']}`} />
                        </div>
                    </div>
                </div>

                <GlobalSettingsModal
                    isOpen={isSettingsOpen}
                    onClose={() => { setIsSettingsOpen(false); refreshData(); }}
                />

                <UserProfileModal
                    isOpen={isProfileModalOpen}
                    onClose={() => setIsProfileModalOpen(false)}
                    currentName={status?.display_name || ""}
                    currentAvatar={status?.avatar ?? null}
                    currentEmail={status?.email}
                    onSaveSuccess={refreshData}
                />

                <PersonaWizard
                    isOpen={isWizardOpen}
                    onClose={() => {
                        setIsWizardOpen(false);
                        refreshData();
                    }}
                    onComplete={() => {
                        refreshData();
                        if (onMove) onMove();
                    }}
                />

                <TutorialSelectModal
                    isOpen={isTutorialSelectOpen}
                    onClose={() => setIsTutorialSelectOpen(false)}
                />

                <AddonManagerModal
                    isOpen={isAddonManagerOpen}
                    onClose={() => setIsAddonManagerOpen(false)}
                />

                <EventsModal
                    isOpen={isEventsOpen}
                    onClose={() => setIsEventsOpen(false)}
                />
            </aside>
        </>
    );
}
