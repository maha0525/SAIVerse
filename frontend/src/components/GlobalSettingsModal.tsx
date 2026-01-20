import React, { useState, useEffect } from 'react';
import { X, Settings, Database, Globe, Layers, Save, RefreshCw, Power, Play, Pause } from 'lucide-react';
import styles from './GlobalSettingsModal.module.css';
import WorldEditor from './settings/WorldEditor';
import ModalOverlay from './common/ModalOverlay';

interface GlobalSettingsModalProps {
    isOpen: boolean;
    onClose: () => void;
}

interface EnvVar {
    key: string;
    value: string;
    is_sensitive: boolean;
}

interface TableInfo {
    name: string;
    columns: string[];
    pk_columns: string[];
}

type TabId = 'env' | 'world' | 'db';

export default function GlobalSettingsModal({ isOpen, onClose }: GlobalSettingsModalProps) {
    const [activeTab, setActiveTab] = useState<TabId>('env');
    const [envVars, setEnvVars] = useState<EnvVar[]>([]);
    const [isLoading, setIsLoading] = useState(false);
    const [isSaving, setIsSaving] = useState(false);
    const [editedEnv, setEditedEnv] = useState<Record<string, string>>({});

    // DB State
    const [tables, setTables] = useState<TableInfo[]>([]);
    const [selectedTable, setSelectedTable] = useState<string | null>(null);
    const [tableData, setTableData] = useState<any[]>([]);
    const [dbLoading, setDbLoading] = useState(false);

    // Global Auto Mode
    const [globalAutoEnabled, setGlobalAutoEnabled] = useState(true);

    useEffect(() => {
        if (isOpen && activeTab === 'env') {
            loadEnvVars();
            loadGlobalAutoState();
        }
        if (isOpen && activeTab === 'db') {
            loadTables();
        }
    }, [isOpen, activeTab]);

    const loadGlobalAutoState = async () => {
        try {
            const res = await fetch('/api/config/global-auto');
            if (res.ok) {
                const data = await res.json();
                setGlobalAutoEnabled(data.enabled);
            }
        } catch (e) {
            console.error("Failed to load global auto state", e);
        }
    };

    const toggleGlobalAuto = async () => {
        const newState = !globalAutoEnabled;
        try {
            const res = await fetch('/api/config/global-auto', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled: newState })
            });
            if (res.ok) {
                setGlobalAutoEnabled(newState);
            }
        } catch (e) {
            console.error("Failed to toggle global auto", e);
        }
    };

    const loadTables = async () => {
        try {
            const res = await fetch('/api/db/tables');
            if (res.ok) {
                const data = await res.json();
                setTables(data);
            }
        } catch (e) {
            console.error("Failed to load tables", e);
        }
    };

    const loadTableData = async (tableName: string) => {
        setDbLoading(true);
        setSelectedTable(tableName);
        try {
            const res = await fetch(`/api/db/tables/${tableName}`);
            if (res.ok) {
                const data = await res.json();
                setTableData(data);
            }
        } catch (e) {
            console.error(e);
        } finally {
            setDbLoading(false);
        }
    };

    const loadEnvVars = async () => {
        setIsLoading(true);
        try {
            const res = await fetch('/api/admin/env');
            if (res.ok) {
                const data = await res.json();
                setEnvVars(data);
                // Reset edits
                setEditedEnv({});
            }
        } catch (e) {
            console.error("Failed to load env vars", e);
        } finally {
            setIsLoading(false);
        }
    };

    const handleEnvChange = (key: string, value: string) => {
        setEditedEnv(prev => ({
            ...prev,
            [key]: value
        }));
    };

    const saveEnv = async () => {
        setIsSaving(true);
        try {
            const res = await fetch('/api/admin/env', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ updates: editedEnv })
            });
            if (res.ok) {
                alert("Environment variables saved.");
                loadEnvVars(); // Reload to confirm
            } else {
                alert("Failed to save.");
            }
        } catch (e) {
            console.error("Save error", e);
        } finally {
            setIsSaving(false);
        }
    };

    const restartServer = async () => {
        if (!confirm("Are you sure you want to restart the server? This will temporarily disconnect the UI.")) return;
        try {
            await fetch('/api/admin/restart', { method: 'POST' });
            alert("Server is restarting. Please reload the page in a few seconds.");
        } catch (e) {
            console.error(e);
        }
    };

    if (!isOpen) return null;

    return (
        <ModalOverlay onClose={onClose} className={styles.overlay}>
            <div
                className={styles.modal}
                onClick={e => e.stopPropagation()}
                // No need to stop propagation here if parent overlay already stops it,
                // but for safety in case overlay structure changes:
                onTouchStart={(e) => e.stopPropagation()}
                onTouchMove={(e) => e.stopPropagation()}
            >
                <div className={styles.header}>
                    <h2><Settings /> Global Settings</h2>
                    <button className={styles.closeBtn} onClick={onClose}><X size={24} /></button>
                </div>

                <div className={styles.content}>
                    {/* Sidebar Navigation */}
                    <div className={styles.sidebar}>
                        <div
                            className={`${styles.navItem} ${activeTab === 'env' ? styles.active : ''}`}
                            onClick={() => setActiveTab('env')}
                        >
                            <Settings size={18} /> Environment
                        </div>
                        <div
                            className={`${styles.navItem} ${activeTab === 'world' ? styles.active : ''}`}
                            onClick={() => setActiveTab('world')}
                        >
                            <Globe size={18} /> World Editor
                        </div>
                        <div
                            className={`${styles.navItem} ${activeTab === 'db' ? styles.active : ''}`}
                            onClick={() => setActiveTab('db')}
                        >
                            <Database size={18} /> Database Manager
                        </div>
                    </div>

                    {/* Main Content Panel */}
                    <div className={styles.mainPanel}>
                        {activeTab === 'env' && (
                            <div className={styles.envContainer}>
                                {/* Global Auto Mode Toggle */}
                                <div className={styles.toggleContainer}>
                                    <div>
                                        <div className={styles.toggleLabel}>
                                            {globalAutoEnabled ? <Play size={18} /> : <Pause size={18} />}
                                            自律会話モード
                                        </div>
                                        <div className={styles.toggleDescription}>
                                            OFFにするとConversationManagerのポーリングを停止し、ログ出力を抑制します
                                        </div>
                                    </div>
                                    <div
                                        className={`${styles.toggle} ${globalAutoEnabled ? styles.active : ''}`}
                                        onClick={toggleGlobalAuto}
                                    />
                                </div>

                                <div className={styles.sectionHeader}>
                                    <h3>Server Environment Variables (.env)</h3>
                                    <button className={styles.restartBtn} onClick={restartServer}>
                                        <Power size={16} /> Restart Server
                                    </button>
                                </div>

                                {isLoading ? (
                                    <div>Loading...</div>
                                ) : (
                                    <>
                                        <div className={styles.envList}>
                                            {envVars.map(item => (
                                                <div key={item.key} className={styles.envItem}>
                                                    <div className={styles.envKey}>{item.key}</div>
                                                    <input
                                                        className={styles.envInput}
                                                        type={item.is_sensitive ? "password" : "text"}
                                                        defaultValue={item.is_sensitive ? "" : item.value}
                                                        placeholder={item.is_sensitive ? "(Hidden/Unchanged)" : ""}
                                                        onChange={(e) => handleEnvChange(item.key, e.target.value)}
                                                    />
                                                </div>
                                            ))}
                                        </div>
                                        <div className={styles.actionFooter}>
                                            <button
                                                className={styles.saveBtn}
                                                onClick={saveEnv}
                                                disabled={isSaving || Object.keys(editedEnv).length === 0}
                                            >
                                                {isSaving ? <RefreshCw className="spin" /> : <Save />} Save Changes
                                            </button>
                                        </div>
                                    </>
                                )}
                            </div>
                        )}

                        {activeTab === 'world' && (
                            <WorldEditor />
                        )}

                        {activeTab === 'db' && (
                            <div className={styles.dbContainer}>
                                <div className={styles.sectionHeader}>
                                    <h3>Database Manager</h3>
                                    <div className={styles.selectWrapper}>
                                        <select
                                            className={styles.dbSelect}
                                            onChange={(e) => loadTableData(e.target.value)}
                                            value={selectedTable || ""}
                                        >
                                            <option value="" disabled>Select a table...</option>
                                            {tables.map(t => (
                                                <option key={t.name} value={t.name}>{t.name}</option>
                                            ))}
                                        </select>
                                    </div>
                                </div>

                                {dbLoading && <div>Loading data...</div>}

                                {!dbLoading && selectedTable && tableData.length === 0 && (
                                    <div style={{ padding: '1rem', color: '#888' }}>No records found.</div>
                                )}

                                {!dbLoading && selectedTable && tableData.length > 0 && (
                                    <div className={styles.tableWrapper}>
                                        <table className={styles.dataTable}>
                                            <thead>
                                                <tr>
                                                    {Object.keys(tableData[0] || {}).map(k => (
                                                        <th key={k}>{k}</th>
                                                    ))}
                                                </tr>
                                            </thead>
                                            <tbody>
                                                {tableData.map((row, idx) => (
                                                    <tr key={idx}>
                                                        {Object.values(row).map((val: any, cIdx) => (
                                                            <td key={cIdx} title={String(val)}>
                                                                {val === null ? <span style={{ color: '#ccc' }}>NULL</span> : (
                                                                    String(val).length > 50 ? String(val).substring(0, 50) + '...' : String(val)
                                                                )}
                                                            </td>
                                                        ))}
                                                    </tr>
                                                ))}
                                            </tbody>
                                        </table>
                                    </div>
                                )}
                            </div>
                        )}
                    </div>
                </div>
            </div>
        </ModalOverlay>
    );
}
