
import { apiFetch } from '@/i18n/api';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import React, { useEffect, useState, useCallback, useRef } from 'react';
import { Plus, Edit2, Trash2, RefreshCw, LogIn, LogOut, KeyRound, ExternalLink } from 'lucide-react';
import styles from './ProviderManagementPanel.module.css';
import ProviderEditorModal, { ProviderEditorMode } from './ProviderEditorModal';
import CodexLoginModal from './CodexLoginModal';

interface ApiKeyEnvInfo {
    name: string;
    configured: boolean;
}

interface ProviderInfo {
    id: string;
    display_name: string;
    protocol: string;
    base_url?: string | null;
    api_key_env?: string | null;
    // Every environment variable this provider's key can be saved as: the
    // primary one first, then the alternates (Gemini has a paid-tier and a
    // free-tier variable). A backend that predates this field omits it.
    api_key_envs?: ApiKeyEnvInfo[];
    builtin: boolean;
    // True when any one of the key variables is set.
    api_key_configured?: boolean | null;
}

// The key variables to show an input for. Falls back to the primary variable
// alone when the backend does not report api_key_envs.
const keyEnvsOf = (p: ProviderInfo): ApiKeyEnvInfo[] => {
    if (p.api_key_envs && p.api_key_envs.length > 0) return p.api_key_envs;
    if (p.api_key_env) return [{ name: p.api_key_env, configured: !!p.api_key_configured }];
    return [];
};

// How-to-get-a-key pages under docs/api-keys/, keyed by the environment variable
// the key is saved as (the same pages the setup tutorial links to). Keying by the
// variable rather than the provider id lets providers that share a key (e.g. the
// OpenRouter variants) all show the link.
const API_KEY_DOCS: Record<string, string> = {
    OPENAI_API_KEY: 'openai',
    GEMINI_FREE_API_KEY: 'gemini-free',
    GEMINI_API_KEY: 'gemini-paid',
    CLAUDE_API_KEY: 'anthropic',
    XAI_API_KEY: 'grok',
    OPENROUTER_API_KEY: 'openrouter',
    NVIDIA_API_KEY: 'nvidia-nim',
    TYPESAFE_API_KEY: 'typesafe',
};

// A name for the tier a key variable belongs to, shown next to its label so
// that two inputs under one provider can be told apart. Keyed by the variable,
// like API_KEY_DOCS. A variable that is not listed gets no extra name.
const API_KEY_TIER_LABELS: Record<string, () => string> = {
    GEMINI_FREE_API_KEY: () => uiText("components.settings.ProviderManagementPanel.text039"),
    GEMINI_API_KEY: () => uiText("components.settings.ProviderManagementPanel.text040"),
};

const openApiKeyDocs = (filename: string) => {
    window.open(
        `https://github.com/maha0525/SAIVerse/blob/main/docs/api-keys/${filename}.md`,
        '_blank',
        'noopener,noreferrer'
    );
};

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
    // The provider whose API key panel is open (one at a time), and what has been
    // typed into it, per environment variable (a provider can have more than one
    // key variable). The saved key itself is never fetched or shown.
    const [keyPanelFor, setKeyPanelFor] = useState<string | null>(null);
    const [keyInputs, setKeyInputs] = useState<Record<string, string>>({});
    const [keySaving, setKeySaving] = useState(false);
    // Synchronous double-submit guard: keySaving (state) updates asynchronously,
    // so Enter + click in the same tick could both pass the state check.
    const keySavingRef = useRef(false);

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

    const toggleKeyPanel = (id: string) => {
        setKeyInputs({});
        setKeyPanelFor(prev => (prev === id ? null : id));
    };

    // Saved through the same endpoint as the environment settings screen. That
    // save drops every persona's cached connection, so the new key is used from
    // each persona's next reply without a restart. Only the one variable named
    // by envKey is saved; what is typed into the provider's other inputs stays.
    const handleSaveApiKey = async (provider: ProviderInfo, envKey: string) => {
        const value = (keyInputs[envKey] ?? '').trim();
        if (!envKey || !value || keySavingRef.current) return;
        keySavingRef.current = true;
        setKeySaving(true);
        try {
            const res = await apiFetch('/api/admin/env', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ updates: { [envKey]: value } }),
            });
            if (!res.ok) {
                const text = await res.text();
                alert(uiText("components.settings.ProviderManagementPanel.text037", { p1: text }));
                return;
            }
            const data = await res.json().catch(() => null);
            const notices: string[] = Array.isArray(data?.notices) ? data.notices : [];
            const rejected: string[] = Array.isArray(data?.rejected_keys) ? data.rejected_keys : [];
            if (rejected.includes(envKey)) {
                alert(uiText("components.settings.ProviderManagementPanel.text037", { p1: notices.join('\n\n') }));
                return;
            }
            if (notices.length > 0) {
                alert(notices.join('\n\n'));
            }
            if (keyEnvsOf(provider).length > 1) {
                // More than one key variable: clear only the input that was
                // saved and keep the panel open, so the other one can be
                // entered right after.
                setKeyInputs(prev => {
                    const next = { ...prev };
                    delete next[envKey];
                    return next;
                });
            } else {
                setKeyInputs({});
                setKeyPanelFor(null);
            }
            setNotice(uiText("components.settings.ProviderManagementPanel.text036"));
            loadProviders();
        } catch (e) {
            alert(uiText("components.settings.ProviderManagementPanel.text037", { p1: e }));
        } finally {
            keySavingRef.current = false;
            setKeySaving(false);
        }
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

    // The inline panel under a provider row for entering its API key: one
    // label + how-to link + input + save button per key variable.
    const renderKeyPanel = (p: ProviderInfo) => {
        const keyEnvs = keyEnvsOf(p);
        if (keyEnvs.length === 0) return null;
        return (
            <div className={styles.keyPanel}>
                {keyEnvs.map((env, index) => {
                    const docsFile = API_KEY_DOCS[env.name];
                    // The tier name exists to tell two inputs apart, so a
                    // provider with a single input (e.g. a user-made one that
                    // happens to read GEMINI_API_KEY) shows none.
                    const tierLabel = keyEnvs.length > 1 ? API_KEY_TIER_LABELS[env.name] : undefined;
                    const inputId = `api-key-${p.id}-${env.name}`;
                    const typed = keyInputs[env.name] ?? '';
                    return (
                        <div key={env.name} className={styles.keyField}>
                            <div className={styles.keyPanelHeader}>
                                <label data-i18n="components.settings.ProviderManagementPanel.text029 components.settings.ProviderManagementPanel.text030 components.settings.ProviderManagementPanel.text039 components.settings.ProviderManagementPanel.text040" htmlFor={inputId} className={styles.keyLabel}>
                                    {uiText("components.settings.ProviderManagementPanel.text029")}
                                    {tierLabel && <span>{tierLabel()}</span>}
                                    <span className={styles.keyEnvName}>
                                        {uiText("components.settings.ProviderManagementPanel.text030", { p1: env.name })}
                                    </span>
                                </label>
                                {docsFile && (
                                    <button data-i18n="components.settings.ProviderManagementPanel.text031 components.settings.ProviderManagementPanel.text032"
                                        type="button"
                                        className={styles.docLink}
                                        onClick={() => openApiKeyDocs(docsFile)}
                                        title={uiText("components.settings.ProviderManagementPanel.text032")}
                                    >
                                        <ExternalLink size={12} />{uiText("components.settings.ProviderManagementPanel.text031")}</button>
                                )}
                            </div>
                            <div className={styles.keyInputRow}>
                                <input data-i18n="components.settings.ProviderManagementPanel.text033 components.settings.ProviderManagementPanel.text034"
                                    id={inputId}
                                    type="password"
                                    autoComplete="new-password"
                                    className={styles.keyInput}
                                    placeholder={env.configured
                                        ? uiText("components.settings.ProviderManagementPanel.text033")
                                        : uiText("components.settings.ProviderManagementPanel.text034")}
                                    value={typed}
                                    onChange={(e) => {
                                        const value = e.target.value;
                                        setKeyInputs(prev => ({ ...prev, [env.name]: value }));
                                    }}
                                    onKeyDown={(e) => {
                                        if (e.key === 'Enter' && !e.nativeEvent.isComposing) {
                                            e.preventDefault();
                                            handleSaveApiKey(p, env.name);
                                        }
                                    }}
                                    autoFocus={index === 0}
                                />
                                <button data-i18n="components.settings.ProviderManagementPanel.text035"
                                    type="button"
                                    className={styles.btnPrimary}
                                    onClick={() => handleSaveApiKey(p, env.name)}
                                    disabled={!typed.trim() || keySaving}
                                >
                                    {uiText("components.settings.ProviderManagementPanel.text035")}
                                </button>
                            </div>
                        </div>
                    );
                })}
                <div data-i18n="components.settings.ProviderManagementPanel.text038" className={styles.keyHint}>
                    {uiText("components.settings.ProviderManagementPanel.text038")}
                </div>
            </div>
        );
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
                    {providers.map(p => {
                        const canSetKey = !!p.api_key_env && p.protocol !== 'openai_codex';
                        const keyPanelOpen = canSetKey && keyPanelFor === p.id;
                        return (
                            <div key={p.id} className={styles.rowGroup}>
                                <div className={styles.row}>
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
                                        {canSetKey && (
                                            <button data-i18n="components.settings.ProviderManagementPanel.text028"
                                                className={`${styles.iconBtn} ${keyPanelOpen ? styles.iconBtnActive : ''}`}
                                                onClick={() => toggleKeyPanel(p.id)}
                                                aria-expanded={keyPanelOpen}
                                            >
                                                <KeyRound size={12} />{uiText("components.settings.ProviderManagementPanel.text028")}</button>
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
                                {keyPanelOpen && renderKeyPanel(p)}
                            </div>
                        );
                    })}
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
