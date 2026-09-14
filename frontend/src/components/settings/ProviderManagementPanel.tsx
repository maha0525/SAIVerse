
import { apiFetch } from '@/i18n/api';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import React, { useEffect, useState, useCallback } from 'react';
import { Plus, Edit2, Trash2, RefreshCw, LogIn, LogOut } from 'lucide-react';
import styles from './ProviderManagementPanel.module.css';
import ProviderEditorModal, { ProviderEditorMode } from './ProviderEditorModal';
import CodexLoginModal from './CodexLoginModal';

interface ProviderInfo {
    id: string;
    display_name: string;
    protocol: string;
    base_url?: string | null;
    api_key_env?: string | null;
    builtin: boolean;
    api_key_configured?: boolean | null;
}

interface CodexAuthStatus {
    logged_in: boolean;
    store: 'saiverse' | 'codex_cli' | null;
    cli_available?: boolean;
    account_id?: string | null;
    access_token_expires_at?: string | null;
    error?: string;
}

export default function ProviderManagementPanel() {
    useLocale();
    const [providers, setProviders] = useState<ProviderInfo[]>([]);
    const [loading, setLoading] = useState(false);
    const [editorOpen, setEditorOpen] = useState(false);
    const [editorMode, setEditorMode] = useState<ProviderEditorMode>('create');
    const [editingId, setEditingId] = useState<string | undefined>();
    // A change here reaches every persona from its next reply, without a restart;
    // a reply already being written finishes with the settings it started with.
    // Saying so at the moment of the change keeps it from looking like the setting
    // was ignored. Personas that could not be switched are named in an alert right
    // after the save or delete.
    const [notice, setNotice] = useState<string | null>(null);
    const [codexStatus, setCodexStatus] = useState<CodexAuthStatus | null>(null);
    const [codexLoginOpen, setCodexLoginOpen] = useState(false);

    const loadProviders = useCallback(async () => {
        setLoading(true);
        try {
            const res = await apiFetch('/api/providers');
            if (res.ok) {
                const data = await res.json();
                setProviders(data);
            }
        } catch (e) {
            console.error('Failed to load providers', e);
        } finally {
            setLoading(false);
        }
    }, []);

    const loadCodexStatus = useCallback(async () => {
        try {
            const res = await apiFetch('/api/codex-auth/status');
            if (res.ok) {
                setCodexStatus(await res.json());
            }
        } catch (e) {
            console.error('Failed to load codex auth status', e);
        }
    }, []);

    useEffect(() => {
        loadProviders();
        loadCodexStatus();
    }, [loadProviders, loadCodexStatus]);

    const handleCodexLogout = async () => {
        if (!confirm(uiText("components.settings.ProviderManagementPanel.text001"))) return;
        try {
            const res = await apiFetch('/api/codex-auth/logout', { method: 'POST' });
            if (!res.ok) {
                alert(uiText("components.settings.ProviderManagementPanel.text002", { p1: res.status }));
                return;
            }
            loadCodexStatus();
        } catch (e) {
            alert(uiText("components.settings.ProviderManagementPanel.text003", { p1: e }));
        }
    };

    const codexBadge = (status: CodexAuthStatus | null) => {
        if (!status) return null;
        if (status.logged_in && status.store === 'saiverse') {
            return <span data-i18n="components.settings.ProviderManagementPanel.text004" className={`${styles.badge} ${styles.badgeKeyOk}`}>{uiText("components.settings.ProviderManagementPanel.text004")}</span>;
        }
        if (status.logged_in && status.store === 'codex_cli') {
            return <span data-i18n="components.settings.ProviderManagementPanel.text005" className={styles.badge}>{uiText("components.settings.ProviderManagementPanel.text005")}</span>;
        }
        return <span data-i18n="components.settings.ProviderManagementPanel.text006" className={`${styles.badge} ${styles.badgeKeyMissing}`}>{uiText("components.settings.ProviderManagementPanel.text006")}</span>;
    };

    const openCreate = () => {
        setEditorMode('create');
        setEditingId(undefined);
        setEditorOpen(true);
    };

    const openEdit = (id: string) => {
        setEditorMode('edit');
        setEditingId(id);
        setEditorOpen(true);
    };

    const handleDelete = async (provider: ProviderInfo) => {
        // Check usage first
        let usingModels: string[] = [];
        try {
            const res = await apiFetch(`/api/providers/${provider.id}/models`);
            if (res.ok) {
                usingModels = await res.json();
            }
        } catch (e) {
            console.error('Failed to check provider usage', e);
        }

        const confirmMsg = usingModels.length > 0
            ? uiText("components.settings.ProviderManagementPanel.text007", { p1: provider.display_name, p2: usingModels.length, p3: usingModels.slice(0, 10).join('\n'), p4: usingModels.length > 10 ? '\n...' : '' })
            : uiText("components.settings.ProviderManagementPanel.text008", { p1: provider.display_name });

        if (!confirm(confirmMsg)) return;

        try {
            const res = await apiFetch(`/api/providers/${provider.id}`, { method: 'DELETE' });
            if (!res.ok) {
                const text = await res.text();
                alert(uiText("components.settings.ProviderManagementPanel.text009", { p1: text }));
                return;
            }
            // 削除で決め直したときに、新しい設定に切り替えられなかったペルソナの知らせ
            const data = await res.json().catch(() => null);
            const notices: string[] = Array.isArray(data?.notices) ? data.notices : [];
            if (notices.length > 0) {
                alert(notices.join('\n\n'));
            }
            setNotice(uiText("components.settings.ProviderManagementPanel.text010"));
            loadProviders();
        } catch (e) {
            alert(uiText("components.settings.ProviderManagementPanel.text011", { p1: e }));
        }
    };

    return (
        <div className={styles.container}>
            <div className={styles.header}>
                <h3 data-i18n="components.settings.ProviderManagementPanel.text012">{uiText("components.settings.ProviderManagementPanel.text012")}</h3>
                <div className={styles.actions}>
                    <button data-i18n="components.settings.ProviderManagementPanel.text013" className={styles.btnSecondary} onClick={loadProviders}>
                        <RefreshCw size={14} />{uiText("components.settings.ProviderManagementPanel.text013")}</button>
                    <button data-i18n="components.settings.ProviderManagementPanel.text014" className={styles.btnPrimary} onClick={openCreate}>
                        <Plus size={14} />{uiText("components.settings.ProviderManagementPanel.text014")}</button>
                </div>
            </div>

            {notice && <div className={styles.notice}>{notice}</div>}

            {loading ? (
                <div data-i18n="components.settings.ProviderManagementPanel.text015" className={styles.empty}>{uiText("components.settings.ProviderManagementPanel.text015")}</div>
            ) : providers.length === 0 ? (
                <div data-i18n="components.settings.ProviderManagementPanel.text016" className={styles.empty}>{uiText("components.settings.ProviderManagementPanel.text016")}</div>
            ) : (
                <div className={styles.list}>
                    {providers.map(p => (
                        <div key={p.id} className={styles.row}>
                            <div className={styles.rowLeft}>
                                <div className={styles.rowName}>
                                    {p.display_name}
                                    <span className={`${styles.badge} ${p.builtin ? '' : styles.badgeUser}`}>
                                        {p.builtin ? 'builtin' : 'user_data'}
                                    </span>
                                    {p.api_key_env && (
                                        <span data-i18n="components.settings.ProviderManagementPanel.text017" className={`${styles.badge} ${p.api_key_configured ? styles.badgeKeyOk : styles.badgeKeyMissing}`}>
                                            {p.api_key_configured ? 'KEY OK' : uiText("components.settings.ProviderManagementPanel.text017")}
                                        </span>
                                    )}
                                    {p.protocol === 'openai_codex' && codexBadge(codexStatus)}
                                </div>
                                <div data-i18n="components.settings.ProviderManagementPanel.text018 components.settings.ProviderManagementPanel.text019" className={styles.rowSub}>
                                    {p.id}{uiText("components.settings.ProviderManagementPanel.text018")}{p.protocol}
                                    {p.base_url && uiText("components.settings.ProviderManagementPanel.text019", { p1: p.base_url })}
                                </div>
                            </div>
                            <div className={styles.rowActions}>
                                {p.protocol === 'openai_codex' && (
                                    codexStatus?.logged_in && codexStatus.store === 'saiverse' ? (
                                        <button data-i18n="components.settings.ProviderManagementPanel.text020" className={styles.iconBtn} onClick={handleCodexLogout}>
                                            <LogOut size={12} />{uiText("components.settings.ProviderManagementPanel.text020")}</button>
                                    ) : (
                                        <button data-i18n="components.settings.ProviderManagementPanel.text021" className={styles.iconBtn} onClick={() => setCodexLoginOpen(true)}>
                                            <LogIn size={12} />{uiText("components.settings.ProviderManagementPanel.text021")}</button>
                                    )
                                )}
                                <button data-i18n="components.settings.ProviderManagementPanel.text022 components.settings.ProviderManagementPanel.text023" className={styles.iconBtn} onClick={() => openEdit(p.id)}>
                                    <Edit2 size={12} /> {p.builtin ? uiText("components.settings.ProviderManagementPanel.text022") : uiText("components.settings.ProviderManagementPanel.text023")}
                                </button>
                                <button data-i18n="components.settings.ProviderManagementPanel.text024 components.settings.ProviderManagementPanel.text025 components.settings.ProviderManagementPanel.text026"
                                    className={`${styles.iconBtn} ${styles.deleteBtn}`}
                                    onClick={() => handleDelete(p)}
                                    disabled={p.builtin}
                                    title={p.builtin ? uiText("components.settings.ProviderManagementPanel.text024") : uiText("components.settings.ProviderManagementPanel.text025")}
                                >
                                    <Trash2 size={12} />{uiText("components.settings.ProviderManagementPanel.text026")}</button>
                            </div>
                        </div>
                    ))}
                </div>
            )}

            <CodexLoginModal
                isOpen={codexLoginOpen}
                onClose={() => {
                    setCodexLoginOpen(false);
                    loadCodexStatus();
                }}
                onSuccess={loadCodexStatus}
            />

            <ProviderEditorModal
                isOpen={editorOpen}
                mode={editorMode}
                providerId={editingId}
                onClose={() => setEditorOpen(false)}
                onSaved={() => {
                    setNotice(uiText("components.settings.ProviderManagementPanel.text027"));
                    loadProviders();
                }}
            />
        </div>
    );
}
