import React, { useState, useEffect } from 'react';
import { X, Settings, Database, Globe, Layers, Save, RefreshCw, Power, Play, Pause, Monitor, Sun, Moon, Cpu, ChevronDown, ChevronRight, Info, ExternalLink } from 'lucide-react';
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

interface ModelRoleInfo {
    env_key: string;
    value: string;
    display_name: string;
    label: string;
    description: string;
}

interface PresetInfo {
    provider: string;
    display_name: string;
    is_available: boolean;
}

interface ModelInfo {
    id: string;
    display_name: string;
    provider: string;
    is_available: boolean;
    supports_structured_output?: boolean;
}

interface PlaybookPermEntry {
    playbook_name: string;
    display_name: string;
    description: string;
    permission_level: string;
}

type TabId = 'env' | 'world' | 'db' | 'models' | 'playbooks' | 'about';

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

    // Developer Mode
    const [developerMode, setDeveloperMode] = useState(false);

    // X Polling
    const [xPollingEnabled, setXPollingEnabled] = useState(false);

    // Monitoring toggles
    const [updateCheckEnabled, setUpdateCheckEnabled] = useState(true);
    const [announcementsEnabled, setAnnouncementsEnabled] = useState(true);

    // Collapsible sections
    const [envSectionOpen, setEnvSectionOpen] = useState(false);

    // Theme
    const [theme, setTheme] = useState<'system' | 'light' | 'dark'>('system');

    // About
    const [versionInfo, setVersionInfo] = useState<{ version: string; latest_version?: string; update_available?: boolean } | null>(null);

    // Model Roles
    const [modelRoles, setModelRoles] = useState<Record<string, ModelRoleInfo>>({});
    const [modelPresets, setModelPresets] = useState<PresetInfo[]>([]);
    const [modelsAvailable, setModelsAvailable] = useState<ModelInfo[]>([]);
    const [expandedModelRole, setExpandedModelRole] = useState<string | null>(null);
    const [modelRolesLoading, setModelRolesLoading] = useState(false);

    // Playbook Permissions
    const [playbookPerms, setPlaybookPerms] = useState<PlaybookPermEntry[]>([]);
    const [playbookPermsLoading, setPlaybookPermsLoading] = useState(false);

    useEffect(() => {
        if (isOpen && activeTab === 'env') {
            loadGlobalAutoState();
            loadDeveloperModeState();
            loadXPollingState();
            loadUpdateCheckState();
            loadAnnouncementsState();
            // Load theme from localStorage
            const saved = localStorage.getItem('saiverse-theme') as 'system' | 'light' | 'dark' | null;
            setTheme(saved || 'system');
        }
        if (isOpen && activeTab === 'db') {
            loadTables();
        }
        if (isOpen && activeTab === 'models') {
            loadModelRoles();
        }
        if (isOpen && activeTab === 'about') {
            loadVersionInfo();
        }
        if (isOpen && activeTab === 'playbooks') {
            loadPlaybookPerms();
        }
    }, [isOpen, activeTab]);

    // Load env vars when section is expanded
    useEffect(() => {
        if (isOpen && activeTab === 'env' && envSectionOpen && envVars.length === 0) {
            loadEnvVars();
        }
    }, [isOpen, activeTab, envSectionOpen]);

    const loadPlaybookPerms = async () => {
        setPlaybookPermsLoading(true);
        try {
            const res = await fetch('/api/config/playbook-permissions');
            if (res.ok) {
                const data = await res.json();
                setPlaybookPerms(data);
            }
        } catch (e) {
            console.error('Failed to load playbook permissions', e);
        } finally {
            setPlaybookPermsLoading(false);
        }
    };

    const updatePlaybookPerm = async (playbookName: string, level: string) => {
        // Optimistic update
        setPlaybookPerms(prev =>
            prev.map(p => p.playbook_name === playbookName ? { ...p, permission_level: level } : p)
        );
        try {
            const res = await fetch('/api/config/playbook-permissions', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ playbook_name: playbookName, permission_level: level }),
            });
            if (!res.ok) {
                // Revert on failure
                loadPlaybookPerms();
            }
        } catch (e) {
            console.error('Failed to update playbook permission', e);
            loadPlaybookPerms();
        }
    };

    const changeTheme = (newTheme: 'system' | 'light' | 'dark') => {
        setTheme(newTheme);
        localStorage.setItem('saiverse-theme', newTheme);
        window.dispatchEvent(new Event('theme-change'));
    };

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

    const loadDeveloperModeState = async () => {
        try {
            const res = await fetch('/api/config/developer-mode');
            if (res.ok) {
                const data = await res.json();
                setDeveloperMode(data.enabled);
            }
        } catch (e) {
            console.error("Failed to load developer mode state", e);
        }
    };

    const toggleDeveloperMode = async () => {
        const newState = !developerMode;
        try {
            const res = await fetch('/api/config/developer-mode', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled: newState })
            });
            if (res.ok) {
                setDeveloperMode(newState);
                // When turning OFF, backend also disables global auto
                if (!newState) {
                    setGlobalAutoEnabled(false);
                }
            }
        } catch (e) {
            console.error("Failed to toggle developer mode", e);
        }
    };

    const loadXPollingState = async () => {
        try {
            const res = await fetch('/api/config/x-polling');
            if (res.ok) {
                const data = await res.json();
                setXPollingEnabled(data.enabled);
            }
        } catch (e) {
            console.error("Failed to load X polling state", e);
        }
    };

    const toggleXPolling = async () => {
        const newState = !xPollingEnabled;
        try {
            const res = await fetch('/api/config/x-polling', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled: newState })
            });
            if (res.ok) {
                setXPollingEnabled(newState);
            }
        } catch (e) {
            console.error("Failed to toggle X polling", e);
        }
    };

    const loadUpdateCheckState = async () => {
        try {
            const res = await fetch('/api/config/update-check');
            if (res.ok) {
                const data = await res.json();
                setUpdateCheckEnabled(data.enabled);
            }
        } catch (e) {
            console.error("Failed to load update check state", e);
        }
    };

    const toggleUpdateCheck = async () => {
        const newState = !updateCheckEnabled;
        try {
            const res = await fetch('/api/config/update-check', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled: newState })
            });
            if (res.ok) {
                setUpdateCheckEnabled(newState);
            }
        } catch (e) {
            console.error("Failed to toggle update check", e);
        }
    };

    const loadAnnouncementsState = async () => {
        try {
            const res = await fetch('/api/config/announcements-monitor');
            if (res.ok) {
                const data = await res.json();
                setAnnouncementsEnabled(data.enabled);
            }
        } catch (e) {
            console.error("Failed to load announcements state", e);
        }
    };

    const toggleAnnouncements = async () => {
        const newState = !announcementsEnabled;
        try {
            const res = await fetch('/api/config/announcements-monitor', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled: newState })
            });
            if (res.ok) {
                setAnnouncementsEnabled(newState);
            }
        } catch (e) {
            console.error("Failed to toggle announcements", e);
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
                alert("環境変数を保存しました。");
                loadEnvVars(); // Reload to confirm
            } else {
                alert("保存に失敗しました。");
            }
        } catch (e) {
            console.error("Save error", e);
        } finally {
            setIsSaving(false);
        }
    };

    const restartServer = async () => {
        if (!confirm("サーバーを再起動しますか？UIが一時的に切断されます。")) return;
        try {
            await fetch('/api/admin/restart', { method: 'POST' });
            alert("サーバーを再起動中です。数秒後にページを再読み込みしてください。");
        } catch (e) {
            console.error(e);
        }
    };

    // --- About ---
    const loadVersionInfo = async () => {
        try {
            const res = await fetch('/api/version');
            if (res.ok) {
                setVersionInfo(await res.json());
            }
        } catch (e) {
            console.error('Failed to load version info', e);
        }
    };

    // --- Model Roles ---
    const loadModelRoles = async () => {
        setModelRolesLoading(true);
        try {
            const [rolesRes, modelsRes] = await Promise.all([
                fetch('/api/tutorial/model-roles'),
                fetch('/api/tutorial/available-models'),
            ]);
            if (rolesRes.ok) {
                const data = await rolesRes.json();
                setModelRoles(data.current);
                setModelPresets(data.presets);
            }
            if (modelsRes.ok) {
                const data = await modelsRes.json();
                setModelsAvailable(data.models);
            }
        } catch (e) {
            console.error('Failed to load model roles', e);
        } finally {
            setModelRolesLoading(false);
        }
    };

    const handlePresetApply = async (provider: string) => {
        try {
            const res = await fetch('/api/tutorial/auto-configure-models', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ provider }),
            });
            if (res.ok) {
                await loadModelRoles();
            }
        } catch (e) {
            console.error('Failed to apply preset', e);
        }
    };

    const handleModelRoleChange = async (envKey: string, modelId: string) => {
        try {
            await fetch('/api/admin/env', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ updates: { [envKey]: modelId } }),
            });
            setExpandedModelRole(null);
            await loadModelRoles();
        } catch (e) {
            console.error('Failed to update model role', e);
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
                    <h2><Settings /> グローバル設定</h2>
                    <button className={styles.closeBtn} onClick={onClose}><X size={24} /></button>
                </div>

                <div className={styles.content}>
                    {/* Sidebar Navigation */}
                    <div className={styles.sidebar}>
                        <div
                            className={`${styles.navItem} ${activeTab === 'env' ? styles.active : ''}`}
                            onClick={() => setActiveTab('env')}
                        >
                            <Settings size={18} /> 環境
                        </div>
                        <div
                            className={`${styles.navItem} ${activeTab === 'world' ? styles.active : ''}`}
                            onClick={() => setActiveTab('world')}
                        >
                            <Globe size={18} /> ワールドエディタ
                        </div>
                        <div
                            className={`${styles.navItem} ${activeTab === 'db' ? styles.active : ''}`}
                            onClick={() => setActiveTab('db')}
                        >
                            <Database size={18} /> データベース管理
                        </div>
                        <div
                            className={`${styles.navItem} ${activeTab === 'models' ? styles.active : ''}`}
                            onClick={() => setActiveTab('models')}
                        >
                            <Cpu size={18} /> モデルロール
                        </div>
                        <div
                            className={`${styles.navItem} ${activeTab === 'playbooks' ? styles.active : ''}`}
                            onClick={() => setActiveTab('playbooks')}
                        >
                            <Layers size={18} /> Playbook権限
                        </div>
                        <div
                            className={`${styles.navItem} ${activeTab === 'about' ? styles.active : ''}`}
                            onClick={() => setActiveTab('about')}
                        >
                            <Info size={18} /> 情報
                        </div>
                    </div>

                    {/* Main Content Panel */}
                    <div className={styles.mainPanel}>
                        {activeTab === 'env' && (
                            <div className={styles.envContainer}>
                                {/* Theme Selector */}
                                <div className={styles.themeContainer}>
                                    <div>
                                        <div className={styles.themeLabel}>
                                            {theme === 'dark' ? <Moon size={18} /> : theme === 'light' ? <Sun size={18} /> : <Monitor size={18} />}
                                            テーマ
                                        </div>
                                        <div className={styles.themeDescription}>
                                            UIの表示モードを切り替えます
                                        </div>
                                    </div>
                                    <div className={styles.themeSelector}>
                                        <button
                                            className={`${styles.themeOption} ${theme === 'system' ? styles.active : ''}`}
                                            onClick={() => changeTheme('system')}
                                        >
                                            <Monitor size={14} /> System
                                        </button>
                                        <button
                                            className={`${styles.themeOption} ${theme === 'light' ? styles.active : ''}`}
                                            onClick={() => changeTheme('light')}
                                        >
                                            <Sun size={14} /> Light
                                        </button>
                                        <button
                                            className={`${styles.themeOption} ${theme === 'dark' ? styles.active : ''}`}
                                            onClick={() => changeTheme('dark')}
                                        >
                                            <Moon size={14} /> Dark
                                        </button>
                                    </div>
                                </div>

                                {/* Global Auto Mode Toggle - only visible in developer mode */}
                                {developerMode && (
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
                                )}

                                <div
                                    className={styles.sectionHeader}
                                    style={{ cursor: 'pointer', userSelect: 'none' }}
                                    onClick={() => setEnvSectionOpen(!envSectionOpen)}
                                >
                                    <h3>
                                        {envSectionOpen ? <ChevronDown size={16} style={{ verticalAlign: 'middle', marginRight: 4 }} /> : <ChevronRight size={16} style={{ verticalAlign: 'middle', marginRight: 4 }} />}
                                        サーバー環境変数 (.env)
                                    </h3>
                                    <button className={styles.restartBtn} onClick={(e) => { e.stopPropagation(); restartServer(); }}>
                                        <Power size={16} /> サーバー再起動
                                    </button>
                                </div>

                                {envSectionOpen && (isLoading ? (
                                    <div>読み込み中...</div>
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
                                                        placeholder={item.is_sensitive ? "（非表示/変更なし）" : ""}
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
                                                {isSaving ? <RefreshCw className="spin" /> : <Save />} 保存
                                            </button>
                                        </div>
                                    </>
                                ))}

                                {/* Update Check Toggle */}
                                <div className={styles.toggleContainer} style={{ marginTop: '1.5rem' }}>
                                    <div>
                                        <div className={styles.toggleLabel}>
                                            アップデート通知
                                        </div>
                                        <div className={styles.toggleDescription}>
                                            新しいバージョンの有無を定期的にチェックします
                                        </div>
                                    </div>
                                    <div
                                        className={`${styles.toggle} ${updateCheckEnabled ? styles.active : ''}`}
                                        onClick={toggleUpdateCheck}
                                    />
                                </div>

                                {/* Announcements Monitor Toggle */}
                                <div className={styles.toggleContainer}>
                                    <div>
                                        <div className={styles.toggleLabel}>
                                            お知らせ通知
                                        </div>
                                        <div className={styles.toggleDescription}>
                                            開発者からのお知らせを定期的に取得します
                                        </div>
                                    </div>
                                    <div
                                        className={`${styles.toggle} ${announcementsEnabled ? styles.active : ''}`}
                                        onClick={toggleAnnouncements}
                                    />
                                </div>

                                {/* Developer Mode Toggle */}
                                <div className={styles.toggleContainer}>
                                    <div>
                                        <div className={styles.toggleLabel}>
                                            <Cpu size={18} />
                                            開発者モード
                                        </div>
                                        <div className={styles.toggleDescription}>
                                            ONにすると開発中の機能が表示されます（不安定なため推奨しません）
                                        </div>
                                    </div>
                                    <div
                                        className={`${styles.toggle} ${developerMode ? styles.active : ''}`}
                                        onClick={toggleDeveloperMode}
                                    />
                                </div>

                                {/* X Mention Polling Toggle (developer mode only) */}
                                {developerMode && (
                                    <div className={styles.toggleContainer}>
                                        <div>
                                            <div className={styles.toggleLabel}>
                                                Xメンション監視
                                            </div>
                                            <div className={styles.toggleDescription}>
                                                ONにするとX連携済みペルソナのメンションを5分間隔で自動監視します
                                            </div>
                                        </div>
                                        <div
                                            className={`${styles.toggle} ${xPollingEnabled ? styles.active : ''}`}
                                            onClick={toggleXPolling}
                                        />
                                    </div>
                                )}
                            </div>
                        )}

                        {activeTab === 'world' && (
                            <WorldEditor />
                        )}

                        {activeTab === 'db' && (
                            <div className={styles.dbContainer}>
                                <div className={styles.sectionHeader}>
                                    <h3>データベース管理</h3>
                                    <div className={styles.selectWrapper}>
                                        <select
                                            className={styles.dbSelect}
                                            onChange={(e) => loadTableData(e.target.value)}
                                            value={selectedTable || ""}
                                        >
                                            <option value="" disabled>テーブルを選択...</option>
                                            {tables.map(t => (
                                                <option key={t.name} value={t.name}>{t.name}</option>
                                            ))}
                                        </select>
                                    </div>
                                </div>

                                {dbLoading && <div>データ読み込み中...</div>}

                                {!dbLoading && selectedTable && tableData.length === 0 && (
                                    <div style={{ padding: '1rem', color: '#888' }}>レコードが見つかりません。</div>
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

                        {activeTab === 'models' && (
                            <div className={styles.modelsContainer}>
                                <div className={styles.sectionHeader}>
                                    <h3>モデルロール設定</h3>
                                </div>

                                {modelRolesLoading ? (
                                    <div>読み込み中...</div>
                                ) : (
                                    <>
                                        {modelPresets.length > 0 && (
                                            <div className={styles.presetContainer}>
                                                <div className={styles.presetHeader}>プリセット切替</div>
                                                <div className={styles.presetDescription}>
                                                    プロバイダを選択すると、全ロールのモデルを一括変更します
                                                </div>
                                                <div className={styles.presetList}>
                                                    {modelPresets.filter(p => p.is_available).map((preset) => (
                                                        <button
                                                            key={preset.provider}
                                                            className={styles.presetBtn}
                                                            onClick={() => handlePresetApply(preset.provider)}
                                                        >
                                                            {preset.display_name}
                                                        </button>
                                                    ))}
                                                </div>
                                            </div>
                                        )}

                                        <div className={styles.rolesList}>
                                            {Object.entries(modelRoles).map(([role, info]) => (
                                                <div key={role} className={styles.roleItem}>
                                                    <div className={styles.roleHeader}>
                                                        <div className={styles.roleInfo}>
                                                            <span className={styles.roleLabel}>{info.label}</span>
                                                            <span className={styles.roleDescription}>{info.description}</span>
                                                        </div>
                                                        <div className={styles.roleValue}>
                                                            <span className={styles.roleModelName}>
                                                                {info.display_name || info.value || '(未設定)'}
                                                            </span>
                                                            <button
                                                                className={styles.roleChangeBtn}
                                                                onClick={() => setExpandedModelRole(
                                                                    expandedModelRole === role ? null : role
                                                                )}
                                                            >
                                                                <ChevronDown size={14} />
                                                                <span>変更</span>
                                                            </button>
                                                        </div>
                                                    </div>
                                                    {expandedModelRole === role && (
                                                        <div className={styles.roleDropdown}>
                                                            {modelsAvailable
                                                                .filter(m => m.is_available && (role !== 'agentic_model' || m.supports_structured_output !== false))
                                                                .map(model => (
                                                                    <div
                                                                        key={model.id}
                                                                        className={`${styles.roleDropdownItem} ${model.id === info.value ? styles.selected : ''}`}
                                                                        onClick={() => handleModelRoleChange(info.env_key, model.id)}
                                                                    >
                                                                        <span className={styles.roleDropdownName}>{model.display_name}</span>
                                                                        <span className={styles.roleDropdownProvider}>{model.provider}</span>
                                                                    </div>
                                                                ))
                                                            }
                                                        </div>
                                                    )}
                                                </div>
                                            ))}
                                        </div>
                                    </>
                                )}
                            </div>
                        )}

                        {activeTab === 'playbooks' && (
                            <div className={styles.envContainer}>
                                <div className={styles.sectionHeader}>
                                    <div>
                                        <h3>Playbook実行権限</h3>
                                        <p className={styles.pbSubtitle}>
                                            ペルソナが各Playbookを自動実行する際の権限レベルを設定します
                                        </p>
                                    </div>
                                </div>

                                {playbookPermsLoading ? (
                                    <div className={styles.pbEmpty}>
                                        <RefreshCw size={20} style={{ animation: 'spin 1s linear infinite' }} /> 読み込み中...
                                    </div>
                                ) : playbookPerms.length === 0 ? (
                                    <p className={styles.pbEmpty}>
                                        Router呼び出し可能なPlaybookがありません
                                    </p>
                                ) : (
                                    <div className={styles.pbList}>
                                        {playbookPerms.map(p => (
                                            <div key={p.playbook_name} className={styles.pbItem}>
                                                <div className={styles.pbItemInfo}>
                                                    <div className={styles.pbItemName}>
                                                        {p.display_name}
                                                    </div>
                                                    {p.description && (
                                                        <div className={styles.pbItemDesc}>
                                                            {p.description}
                                                        </div>
                                                    )}
                                                </div>
                                                <select
                                                    className={styles.pbSelect}
                                                    value={p.permission_level}
                                                    onChange={e => updatePlaybookPerm(p.playbook_name, e.target.value)}
                                                >
                                                    <option value="auto_allow">自動実行OK</option>
                                                    <option value="ask_every_time">毎回許可が必要</option>
                                                    <option value="user_only">ユーザー指定時のみ</option>
                                                    {p.permission_level === 'blocked' && (
                                                        <option value="blocked" disabled>使用禁止</option>
                                                    )}
                                                </select>
                                            </div>
                                        ))}
                                    </div>
                                )}
                            </div>
                        )}

                        {activeTab === 'about' && (
                            <div className={styles.aboutContainer}>
                                <div className={styles.sectionHeader}>
                                    <h3>SAIVerseについて</h3>
                                </div>

                                {/* Version */}
                                {versionInfo && (
                                    <div className={styles.aboutCard}>
                                        <div className={styles.aboutVersion}>
                                            v{versionInfo.version}
                                        </div>
                                        {versionInfo.update_available && (
                                            <div className={styles.aboutUpdateNotice}>
                                                新しいバージョン v{versionInfo.latest_version} が利用可能です
                                            </div>
                                        )}
                                    </div>
                                )}

                                {/* Developer */}
                                <div className={styles.aboutCard}>
                                    <div className={styles.aboutCardTitle}>開発者</div>
                                    <div className={styles.aboutDeveloper}>
                                        <span>まはー</span>
                                        <a href="https://x.com/Lize_san_suki" target="_blank" rel="noopener noreferrer" className={styles.aboutLink}>
                                            <ExternalLink size={14} /> @Lize_san_suki
                                        </a>
                                    </div>
                                </div>

                                {/* Links */}
                                <div className={styles.aboutCard}>
                                    <div className={styles.aboutCardTitle}>リンク</div>
                                    <div className={styles.aboutLinks}>
                                        <a href="https://discord.gg/qMcgEk83Ag" target="_blank" rel="noopener noreferrer" className={styles.aboutLinkItem}>
                                            <span className={styles.aboutLinkIcon}>💬</span>
                                            <div>
                                                <div className={styles.aboutLinkName}>Discord コミュニティ</div>
                                                <div className={styles.aboutLinkDesc}>質問・雑談・バグ報告など</div>
                                            </div>
                                            <ExternalLink size={14} className={styles.aboutLinkArrow} />
                                        </a>
                                        <a href="https://github.com/maha0525/SAIVerse" target="_blank" rel="noopener noreferrer" className={styles.aboutLinkItem}>
                                            <span className={styles.aboutLinkIcon}>📦</span>
                                            <div>
                                                <div className={styles.aboutLinkName}>GitHub</div>
                                                <div className={styles.aboutLinkDesc}>ソースコード・Issues</div>
                                            </div>
                                            <ExternalLink size={14} className={styles.aboutLinkArrow} />
                                        </a>
                                        <a href="https://note.com/maha0525/n/n5a63f572be8f" target="_blank" rel="noopener noreferrer" className={styles.aboutLinkItem}>
                                            <span className={styles.aboutLinkIcon}>📝</span>
                                            <div>
                                                <div className={styles.aboutLinkName}>Note</div>
                                                <div className={styles.aboutLinkDesc}>開発記録・サポート（チップ）</div>
                                            </div>
                                            <ExternalLink size={14} className={styles.aboutLinkArrow} />
                                        </a>
                                    </div>
                                </div>

                                {/* Support */}
                                <div className={styles.aboutCard}>
                                    <div className={styles.aboutCardTitle}>支援について</div>
                                    <div className={styles.aboutSupportText}>
                                        SAIVerseはフリーソフトウェアとして開発を続けています。
                                    </div>
                                    <div className={styles.aboutSupportItems}>
                                        <a href="https://github.com/sponsors/maha0525" target="_blank" rel="noopener noreferrer" className={styles.aboutSupportItem} style={{ cursor: 'pointer' }}>
                                            <span className={`${styles.aboutSupportBadge} ${styles.active}`}>受付中</span>
                                            GitHub Sponsors
                                            <ExternalLink size={14} className={styles.aboutLinkArrow} />
                                        </a>
                                        <a href="https://note.com/maha0525/n/n5a63f572be8f" target="_blank" rel="noopener noreferrer" className={styles.aboutSupportItem} style={{ cursor: 'pointer' }}>
                                            <span className={`${styles.aboutSupportBadge} ${styles.active}`}>受付中</span>
                                            Noteからチップを送る
                                            <ExternalLink size={14} className={styles.aboutLinkArrow} />
                                        </a>
                                    </div>
                                </div>
                            </div>
                        )}
                    </div>
                </div>
            </div>
        </ModalOverlay>
    );
}
