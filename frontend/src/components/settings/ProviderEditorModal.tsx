
import { apiFetch } from '@/i18n/api';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import React, { useState, useEffect } from 'react';
import { X, Loader, CheckCircle, XCircle } from 'lucide-react';
import styles from './ProviderEditorModal.module.css';
import ModalOverlay from '../common/ModalOverlay';

export type ProviderEditorMode = 'create' | 'edit';

export interface ProviderConfig {
    id: string;
    display_name: string;
    protocol: string;
    base_url?: string | null;
    api_key_env?: string | null;
    builtin?: boolean;
    api_key_configured?: boolean | null;
    api_key_required?: boolean | null;
}

interface ConnectionTestResult {
    success: boolean;
    status_code?: number | null;
    error?: string | null;
    models?: string[] | null;
    elapsed_ms?: number | null;
}

interface Props {
    isOpen: boolean;
    mode: ProviderEditorMode;
    providerId?: string;  // required when mode='edit'
    onClose: () => void;
    onSaved: () => void;
}

const PROTOCOL_OPTIONS = [
    { value: 'openai_compat', get label() { return uiText("components.settings.ProviderEditorModal.text001"); } },
    { value: 'ollama_compat', get label() { return uiText("components.settings.ProviderEditorModal.text002"); } },
];

export default function ProviderEditorModal({ isOpen, mode, providerId, onClose, onSaved }: Props) {
    useLocale();
    const [id, setId] = useState('');
    const [displayName, setDisplayName] = useState('');
    const [protocol, setProtocol] = useState('openai_compat');
    const [baseUrl, setBaseUrl] = useState('');
    const [apiKeyEnv, setApiKeyEnv] = useState('');
    const [keylessServer, setKeylessServer] = useState(false);
    const [isBuiltin, setIsBuiltin] = useState(false);
    const [loading, setLoading] = useState(false);
    const [saving, setSaving] = useState(false);
    const [testing, setTesting] = useState(false);
    const [testResult, setTestResult] = useState<ConnectionTestResult | null>(null);
    const [error, setError] = useState<string | null>(null);

    useEffect(() => {
        if (!isOpen) return;
        setError(null);
        setTestResult(null);
        if (mode === 'edit' && providerId) {
            loadProvider(providerId);
        } else {
            // Create mode: reset to defaults
            setId('');
            setDisplayName('');
            setProtocol('openai_compat');
            setBaseUrl('http://localhost:1234/v1');
            setApiKeyEnv('');
            setKeylessServer(false);
            setIsBuiltin(false);
        }
    }, [isOpen, mode, providerId]);

    const loadProvider = async (pid: string) => {
        setLoading(true);
        try {
            const res = await apiFetch(`/api/providers/${pid}`);
            if (!res.ok) {
                setError(`Failed to load provider: HTTP ${res.status}`);
                return;
            }
            const data = await res.json();
            setId(data.id);
            setDisplayName(data.display_name || '');
            setProtocol(data.protocol || 'openai_compat');
            setBaseUrl(data.base_url || '');
            setApiKeyEnv(data.api_key_env || '');
            setKeylessServer(data.api_key_required === false);
            setIsBuiltin(!!data.builtin);
        } catch (e) {
            setError(`Load failed: ${e}`);
        } finally {
            setLoading(false);
        }
    };

    const handleTest = async () => {
        // Tests the current form values against the inline test endpoint.
        // Works for both create and edit modes — no save required.
        if (!baseUrl) {
            setTestResult({ success: false, error: uiText("components.settings.ProviderEditorModal.text003") });
            return;
        }
        setTesting(true);
        setTestResult(null);
        try {
            const res = await apiFetch('/api/providers/test', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    protocol,
                    base_url: baseUrl || null,
                    api_key_env: apiKeyEnv || null,
                    provider_id: id || null,
                }),
            });
            const data: ConnectionTestResult = await res.json();
            setTestResult(data);
        } catch (e) {
            setTestResult({ success: false, error: `${e}` });
        } finally {
            setTesting(false);
        }
    };

    const handleSave = async () => {
        setError(null);
        if (!id || !id.match(/^[a-zA-Z0-9_.\-]+$/)) {
            setError(uiText("components.settings.ProviderEditorModal.text004"));
            return;
        }
        if (!displayName.trim()) {
            setError(uiText("components.settings.ProviderEditorModal.text005"));
            return;
        }

        const payload: Record<string, unknown> = {
            display_name: displayName,
            protocol,
            base_url: baseUrl || null,
            api_key_env: apiKeyEnv || null,
            // Send false explicitly (the backend drops null/undefined fields and
            // keeps the stored value, which would make unchecking a no-op).
            api_key_required: keylessServer ? false : true,
        };

        setSaving(true);
        try {
            let res: Response;
            if (mode === 'create') {
                res = await apiFetch('/api/providers', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id, ...payload }),
                });
            } else {
                res = await apiFetch(`/api/providers/${id}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
            }

            if (!res.ok) {
                const text = await res.text();
                setError(uiText("components.settings.ProviderEditorModal.text006", { p1: res.status, p2: text }));
                return;
            }

            // 保存で決め直したときに、新しい設定に切り替えられなかったペルソナの知らせ
            const data = await res.json().catch(() => null);
            const notices: string[] = Array.isArray(data?.notices) ? data.notices : [];
            if (notices.length > 0) {
                alert(notices.join('\n\n'));
            }
            onSaved();
            onClose();
        } catch (e) {
            setError(uiText("components.settings.ProviderEditorModal.text007", { p1: e }));
        } finally {
            setSaving(false);
        }
    };

    if (!isOpen) return null;

    return (
        <ModalOverlay onClose={onClose}>
            <div className={styles.modal}>
                <div className={styles.header}>
                    <h3 data-i18n="components.settings.ProviderEditorModal.text008 components.settings.ProviderEditorModal.text009">{mode === 'create' ? uiText("components.settings.ProviderEditorModal.text008") : uiText("components.settings.ProviderEditorModal.text009", { p1: id })}</h3>
                    <button className={styles.closeBtn} onClick={onClose}><X size={20} /></button>
                </div>

                <div className={styles.content}>
                    {loading ? (
                        <div data-i18n="components.settings.ProviderEditorModal.text010">{uiText("components.settings.ProviderEditorModal.text010")}</div>
                    ) : (
                        <>
                            {isBuiltin && (
                                <div data-i18n="components.settings.ProviderEditorModal.text011" className={styles.hint} style={{ marginBottom: '0.75rem' }}>{uiText("components.settings.ProviderEditorModal.text011")}</div>
                            )}

                            <div className={styles.field}>
                                <label data-i18n="components.settings.ProviderEditorModal.text012">{uiText("components.settings.ProviderEditorModal.text012")}</label>
                                <input data-i18n="components.settings.ProviderEditorModal.text013"
                                    className={styles.input}
                                    type="text"
                                    value={id}
                                    onChange={e => setId(e.target.value)}
                                    placeholder={uiText("components.settings.ProviderEditorModal.text013")}
                                    disabled={mode === 'edit'}
                                />
                                {mode === 'create' && (
                                    <span data-i18n="components.settings.ProviderEditorModal.text014" className={styles.hint}>{uiText("components.settings.ProviderEditorModal.text014")}</span>
                                )}
                            </div>

                            <div className={styles.field}>
                                <label data-i18n="components.settings.ProviderEditorModal.text015">{uiText("components.settings.ProviderEditorModal.text015")}</label>
                                <input data-i18n="components.settings.ProviderEditorModal.text016"
                                    className={styles.input}
                                    type="text"
                                    value={displayName}
                                    onChange={e => setDisplayName(e.target.value)}
                                    placeholder={uiText("components.settings.ProviderEditorModal.text016")}
                                />
                            </div>

                            <div className={styles.field}>
                                <label data-i18n="components.settings.ProviderEditorModal.text017">{uiText("components.settings.ProviderEditorModal.text017")}</label>
                                <select
                                    className={styles.select}
                                    value={protocol}
                                    onChange={e => setProtocol(e.target.value)}
                                    disabled={isBuiltin}
                                >
                                    {PROTOCOL_OPTIONS.map(opt => (
                                        <option key={opt.value} value={opt.value}>{opt.label}</option>
                                    ))}
                                    {isBuiltin && !PROTOCOL_OPTIONS.some(o => o.value === protocol) && (
                                        <option value={protocol}>{protocol} {uiText("components.settings.ProviderEditorModal.label001")}</option>
                                    )}
                                </select>
                            </div>

                            <div className={styles.field}>
                                <label>{uiText("components.settings.ProviderEditorModal.label002")}</label>
                                <input data-i18n="components.settings.ProviderEditorModal.text018"
                                    className={styles.input}
                                    type="text"
                                    value={baseUrl}
                                    onChange={e => setBaseUrl(e.target.value)}
                                    placeholder={uiText("components.settings.ProviderEditorModal.text018")}
                                />
                                {protocol === 'openai_compat' && (
                                    <span data-i18n="components.settings.ProviderEditorModal.text019" className={styles.hint}>{uiText("components.settings.ProviderEditorModal.text019")}</span>
                                )}
                                {protocol === 'ollama_compat' && (
                                    <span data-i18n="components.settings.ProviderEditorModal.text020" className={styles.hint}>{uiText("components.settings.ProviderEditorModal.text020")}</span>
                                )}
                            </div>

                            <div className={styles.field}>
                                <label data-i18n="components.settings.ProviderEditorModal.text021" className={styles.checkboxLabel}>
                                    <input
                                        type="checkbox"
                                        checked={keylessServer}
                                        onChange={e => setKeylessServer(e.target.checked)}
                                    />{uiText("components.settings.ProviderEditorModal.text021")}</label>
                                <span data-i18n="components.settings.ProviderEditorModal.text022" className={styles.hint}>{uiText("components.settings.ProviderEditorModal.text022")}</span>
                            </div>

                            <div className={styles.field}>
                                <label data-i18n="components.settings.ProviderEditorModal.text023">{uiText("components.settings.ProviderEditorModal.text023")}</label>
                                <input data-i18n="components.settings.ProviderEditorModal.text024 components.settings.ProviderEditorModal.text025"
                                    className={styles.input}
                                    type="text"
                                    value={apiKeyEnv}
                                    onChange={e => setApiKeyEnv(e.target.value)}
                                    placeholder={keylessServer ? uiText("components.settings.ProviderEditorModal.text024") : uiText("components.settings.ProviderEditorModal.text025")}
                                />
                                <span data-i18n="components.settings.ProviderEditorModal.text026 components.settings.ProviderEditorModal.text027" className={styles.hint}>{uiText("components.settings.ProviderEditorModal.text026")}{keylessServer && uiText("components.settings.ProviderEditorModal.text027")}
                                </span>
                            </div>

                            <div className={styles.testSection}>
                                <button data-i18n="components.settings.ProviderEditorModal.text028 components.settings.ProviderEditorModal.text029" className={styles.testBtn} onClick={handleTest} disabled={testing}>
                                    {testing ? <><Loader size={14} />{uiText("components.settings.ProviderEditorModal.text028")}</> : uiText("components.settings.ProviderEditorModal.text029")}
                                </button>
                                {testResult && testResult.success && (
                                    <div data-i18n="components.settings.ProviderEditorModal.text030" className={styles.testSuccess}>
                                        <CheckCircle size={14} style={{ verticalAlign: 'middle' }} />{uiText("components.settings.ProviderEditorModal.text030")}{testResult.elapsed_ms != null && ` (${testResult.elapsed_ms}ms)`}
                                        {testResult.models && testResult.models.length > 0 && (
                                            <>
                                                <div data-i18n="components.settings.ProviderEditorModal.text031" style={{ marginTop: 4 }}>{uiText("components.settings.ProviderEditorModal.text031")}{testResult.models.length}):
                                                </div>
                                                <div className={styles.modelList}>
                                                    {testResult.models.map(m => <div key={m}>{m}</div>)}
                                                </div>
                                            </>
                                        )}
                                    </div>
                                )}
                                {testResult && !testResult.success && (
                                    <div data-i18n="components.settings.ProviderEditorModal.text032" className={styles.testFail}>
                                        <XCircle size={14} style={{ verticalAlign: 'middle' }} />{uiText("components.settings.ProviderEditorModal.text032")}{testResult.error}
                                    </div>
                                )}
                            </div>


                            {error && <div className={styles.error}>{error}</div>}
                        </>
                    )}
                </div>

                <div className={styles.footer}>
                    <button data-i18n="components.settings.ProviderEditorModal.text033" className={styles.cancelBtn} onClick={onClose}>{uiText("components.settings.ProviderEditorModal.text033")}</button>
                    <button data-i18n="components.settings.ProviderEditorModal.text034 components.settings.ProviderEditorModal.text035" className={styles.saveBtn} onClick={handleSave} disabled={saving || loading}>
                        {saving ? uiText("components.settings.ProviderEditorModal.text034") : uiText("components.settings.ProviderEditorModal.text035")}
                    </button>
                </div>
            </div>
        </ModalOverlay>
    );
}
