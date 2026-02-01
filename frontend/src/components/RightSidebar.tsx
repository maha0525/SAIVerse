import { useRef, useState, useEffect } from 'react';
import styles from './RightSidebar.module.css';
import {
    Users,
    FileText,
    Image as ImageIcon,
    File,
    Eye,
    EyeOff,
    Settings
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
}

interface Occupant {
    id: string;
    name: string;
    avatar?: string;
}

interface Item {
    id: string;
    name: string;
    type: 'document' | 'picture' | 'other';
    description?: string;
    is_open?: boolean;  // Whether item content is included in visual context
}

interface BuildingDetails {
    id: string;
    name: string;
    description: string;
    image_path?: string | null;  // Building interior image
    occupants: Occupant[];
    items: Item[];
}

export default function RightSidebar({ isOpen, onClose, refreshTrigger }: RightSidebarProps) {
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

    const startX = useRef<number | null>(null);
    const startY = useRef<number | null>(null);
    const startTime = useRef<number | null>(null);



    const fetchDetails = async () => {
        try {
            const res = await fetch('/api/info/details');
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
    }, [refreshTrigger, isOpen]);

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
    const openModal = (type: 'memory' | 'schedule' | 'tasks' | 'settings' | 'inventory') => {
        if (selectedPersona) {
            setActiveModalPersonaId(selectedPersona.id);
            if (type === 'memory') setShowMemory(true);
            if (type === 'schedule') setShowSchedule(true);
            if (type === 'tasks') setShowTasks(true);
            if (type === 'settings') setShowSettings(true);
            if (type === 'inventory') setShowInventory(true);
            // Close the menu
            setSelectedPersona(null);
        }
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
                            <h3 className={styles.heading}>Current Location</h3>
                            <div className={styles.content}>
                                <div className={styles.buildingHeader}>
                                    <div className={styles.buildingName}>{details.name}</div>
                                    <button
                                        className={styles.settingsBtn}
                                        onClick={() => setShowBuildingSettings(true)}
                                        title="Building Settings"
                                    >
                                        <Settings size={16} />
                                    </button>
                                </div>
                                <div className={styles.description}>
                                    {details.description || "No description available."}
                                </div>
                            </div>
                        </div>

                        {/* Building Interior Image */}
                        {details.image_path && (
                            <div className={styles.section}>
                                <h3 className={styles.heading}>
                                    <ImageIcon size={16} /> Interior
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
                                <Users size={16} /> Occupants ({details.occupants.length})
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
                                    <div className={styles.empty}>No one is here.</div>
                                )}
                            </div>
                        </div>

                        <div className={styles.section}>
                            <h3 className={styles.heading}>
                                <FileText size={16} /> Items ({details.items.length})
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
                                                {item.type === 'picture' ? <ImageIcon size={20} /> : <File size={20} />}
                                            </div>
                                            <div className={styles.cardInfo}>
                                                <div className={styles.cardName}>{item.name}</div>
                                                {item.description && (
                                                    <div className={styles.cardDesc}>{item.description}</div>
                                                )}
                                            </div>
                                            {(item.type === 'picture' || item.type === 'document') && (
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
                                    <div className={styles.empty}>No items found.</div>
                                )}
                            </div>
                        </div>
                    </>
                ) : (
                    <div style={{ padding: '1rem', color: '#6b7280' }}>Loading location info...</div>
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
