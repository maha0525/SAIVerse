"use client";

import { useEffect, useState, useRef } from 'react';
import styles from './Sidebar.module.css';
import { Settings, Zap, BarChart2, UserPlus } from 'lucide-react';
import GlobalSettingsModal from './GlobalSettingsModal';
import UserProfileModal from './UserProfileModal';
import PersonaWizard from './PersonaWizard';

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
    onMove?: () => void;
    isOpen: boolean;
    onOpen: () => void;
    onClose: () => void;
}

export default function Sidebar({ onMove, isOpen, onOpen, onClose }: SidebarProps) {
    const [status, setStatus] = useState<UserStatus | null>(null);
    const [buildings, setBuildings] = useState<Building[]>([]);
    const [isSettingsOpen, setIsSettingsOpen] = useState(false);
    const [isProfileModalOpen, setIsProfileModalOpen] = useState(false);
    const [isWizardOpen, setIsWizardOpen] = useState(false);

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
                    if (onMove) onMove();
                    onClose(); // Close sidebar on nav
                }
            }
        } catch (err) {
            console.error("Move error", err);
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
                {/* Profile Section */}
                <div
                    className={styles.profileSection}
                    onClick={() => setIsProfileModalOpen(true)}
                    style={{ cursor: 'pointer' }}
                >
                    <img
                        src={status?.avatar || "/api/static/icons/user.png"}
                        alt="User"
                        className={styles.avatar}
                        onError={(e) => { e.currentTarget.src = "https://placehold.co/48x48?text=U"; }}
                    />
                    <div className={styles.userInfo}>
                        <span className={styles.userName}>{status?.display_name || "Guest"}</span>
                        <div className={`${styles.userStatus} ${styles[status?.presence_status || 'offline']}`}>
                            <span style={{ fontSize: '1.2em' }}>•</span>
                            {status?.presence_status === 'online' ? 'Online' :
                                status?.presence_status === 'away' ? 'Away' : 'Offline'}
                        </div>
                    </div>
                </div>

                {/* Create Persona Button */}
                <div style={{ padding: '0 1rem', marginBottom: '1rem' }}>
                    <button
                        onClick={() => setIsWizardOpen(true)}
                        className={styles.createPersonaBtn}
                    >
                        <UserPlus size={18} />
                        ペルソナを作成
                    </button>
                </div>

                {/* Navigation */}
                <div className={styles.sectionTitle}>Locations</div>
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
                <div className={styles.sectionTitle}>System</div>
                <div className={styles.buildingList} style={{ flex: 'none', marginBottom: '1rem' }}>
                    <div
                        className={styles.buildingItem}
                        onClick={() => {
                            window.location.href = '/phenomena';
                            if (onClose) onClose();
                        }}
                    >
                        <span style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                            <Zap size={16} /> Phenomenon Rules
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
                            <BarChart2 size={16} /> API Usage
                        </span>
                    </div>
                </div>

                {/* Global Settings Trigger */}
                <div className={styles.settingsFooter}>
                    <button
                        onClick={() => setIsSettingsOpen(true)}
                        className={styles.settingsBtn}
                    >
                        <Settings size={18} /> Settings
                    </button>
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
            </aside>
        </>
    );
}
