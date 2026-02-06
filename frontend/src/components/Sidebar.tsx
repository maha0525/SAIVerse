"use client";

import { useEffect, useState, useRef } from 'react';
import styles from './Sidebar.module.css';
import { Settings, Zap, BarChart2, UserPlus, Plus, X, HelpCircle } from 'lucide-react';
import GlobalSettingsModal from './GlobalSettingsModal';
import UserProfileModal from './UserProfileModal';
import PersonaWizard from './PersonaWizard';
import TutorialSelectModal from './tutorial/TutorialSelectModal';

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
}

interface SidebarProps {
    onMove?: (buildingId: string) => void;
    isOpen: boolean;
    onOpen: () => void;
    onClose: () => void;
}

export default function Sidebar({ onMove, isOpen, onOpen, onClose }: SidebarProps) {
    const [status, setStatus] = useState<UserStatus | null>(null);
    const [buildings, setBuildings] = useState<Building[]>([]);
    const [cityId, setCityId] = useState<number | null>(null);
    const [isSettingsOpen, setIsSettingsOpen] = useState(false);
    const [isProfileModalOpen, setIsProfileModalOpen] = useState(false);
    const [isWizardOpen, setIsWizardOpen] = useState(false);
    const [isCreateBuildingOpen, setIsCreateBuildingOpen] = useState(false);
    const [newBuildingName, setNewBuildingName] = useState('');
    const [isCreatingBuilding, setIsCreatingBuilding] = useState(false);
    const [isTutorialSelectOpen, setIsTutorialSelectOpen] = useState(false);

    // Swipe Logic for Control
    const startX = useRef<number | null>(null);
    const startY = useRef<number | null>(null);
    const startTime = useRef<number | null>(null);

    const refreshData = async () => {
        try {
            const [statusRes, buildingsRes] = await Promise.all([
                fetch('/api/user/status'),
                fetch('/api/user/buildings')
            ]);
            if (statusRes.ok) setStatus(await statusRes.json());
            if (buildingsRes.ok) {
                const data = await buildingsRes.json();
                setBuildings(data.buildings || []);
                if (data.city_id != null) setCityId(data.city_id);
            }
        } catch (err) {
            console.error("Sidebar fetch error", err);
        }
    };

    // Fetch data only once on mount
    useEffect(() => {
        refreshData();
    }, []);

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

    const handleMove = async (buildingId: string) => {
        if (!status || status.current_building_id === buildingId) return;

        try {
            const res = await fetch('/api/user/move', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ target_building_id: buildingId })
            });
            if (res.ok) {
                const data = await res.json();
                if (data.success) {
                    refreshData();
                    if (onMove) onMove(buildingId);
                    onClose(); // Close sidebar on nav
                }
            }
        } catch (err) {
            console.error("Move error", err);
        }
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
                {/* Create Persona Button */}
                <div style={{ padding: '0 1rem 1rem 1rem' }}>
                    <button
                        onClick={() => setIsWizardOpen(true)}
                        className={styles.createPersonaBtn}
                    >
                        <UserPlus size={18} />
                        ペルソナを作成
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
                    {buildings.map(b => (
                        <div
                            key={b.id}
                            className={`${styles.buildingItem} ${status?.current_building_id === b.id ? styles.active : ''}`}
                            onClick={() => handleMove(b.id)}
                        >
                            <span>{b.name}</span>
                            {status?.current_building_id === b.id && <span>📍</span>}
                        </div>
                    ))}
                </div>

                {/* System Section */}
                <div className={styles.sectionTitle}>システム</div>
                <div className={styles.buildingList} style={{ flex: 'none', marginBottom: '1rem' }}>
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
            </aside>
        </>
    );
}
