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
    { value: 'openai_compat', label: 'OpenAI 互換 (LM Studio / llama.cpp サーバー / Kimi など)' },
    { value: 'ollama_compat', label: 'Ollama 互換' },
];

export default function ProviderEditorModal({ isOpen, mode, providerId, onClose, onSaved }: Props) {
    const [id, setId] = useState('');
    const [displayName, setDisplayName] = useState('');
    const [protocol, setProtocol] = useState('openai_compat');
    const [baseUrl, setBaseUrl] = useState('');
    const [apiKeyEnv, setApiKeyEnv] = useState('');
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
            setIsBuiltin(false);
        }
    }, [isOpen, mode, providerId]);

    const loadProvider = async (pid: string) => {
        setLoading(true);
        try {
            const res = await fetch(`/api/providers/${pid}`);
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
            setTestResult({ success: false, error: 'base_url を入力してください' });
            return;
        }
        setTesting(true);
        setTestResult(null);
        try {
            const res = await fetch('/api/providers/test', {
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
            setError('id は英数字、ハイフン、アンダースコア、ドットのみ使用可能です');
            return;
        }
        if (!displayName.trim()) {
            setError('表示名を入力してください');
            return;
        }

        const payload: Record<string, unknown> = {
            display_name: displayName,
            protocol,
            base_url: baseUrl || null,
            api_key_env: apiKeyEnv || null,
        };

        setSaving(true);
        try {
            let res: Response;
            if (mode === 'create') {
                res = await fetch('/api/providers', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id, ...payload }),
                });
            } else {
                res = await fetch(`/api/providers/${id}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
            }

            if (!res.ok) {
                const text = await res.text();
                setError(`保存に失敗しました: HTTP ${res.status} ${text}`);
                return;
            }

            onSaved();
            onClose();
        } catch (e) {
            setError(`保存に失敗しました: ${e}`);
        } finally {
            setSaving(false);
        }
    };

    if (!isOpen) return null;

    return (
        <ModalOverlay onClose={onClose}>
            <div className={styles.modal}>
                <div className={styles.header}>
                    <h3>{mode === 'create' ? 'プロバイダを新規作成' : `プロバイダを編集: ${id}`}</h3>
                    <button className={styles.closeBtn} onClick={onClose}><X size={20} /></button>
                </div>

                <div className={styles.content}>
                    {loading ? (
                        <div>読み込み中...</div>
                    ) : (
                        <>
                            {isBuiltin && (
                                <div className={styles.hint} style={{ marginBottom: '0.75rem' }}>
                                    builtin プロバイダを編集中です。保存すると user_data に上書きが作成され、次回ロード時に builtin より優先されます。
                                </div>
                            )}

                            <div className={styles.field}>
                                <label>ID（ファイル名になります）</label>
                                <input
                                    className={styles.input}
                                    type="text"
                                    value={id}
                                    onChange={e => setId(e.target.value)}
                                    placeholder="例: lmstudio"
                                    disabled={mode === 'edit'}
                                />
                                {mode === 'create' && (
                                    <span className={styles.hint}>英数字・ハイフン・アンダースコア・ドットのみ</span>
                                )}
                            </div>

                            <div className={styles.field}>
                                <label>表示名</label>
                                <input
                                    className={styles.input}
                                    type="text"
                                    value={displayName}
                                    onChange={e => setDisplayName(e.target.value)}
                                    placeholder="例: LM Studio (local)"
                                />
                            </div>

                            <div className={styles.field}>
                                <label>プロトコル</label>
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
                                        <option value={protocol}>{protocol} (builtin only)</option>
                                    )}
                                </select>
                            </div>

                            <div className={styles.field}>
                                <label>Base URL</label>
                                <input
                                    className={styles.input}
                                    type="text"
                                    value={baseUrl}
                                    onChange={e => setBaseUrl(e.target.value)}
                                    placeholder="例: http://localhost:1234/v1"
                                />
                                {protocol === 'openai_compat' && (
                                    <span className={styles.hint}>末尾に /v1 を含める形式（OpenAI 互換）</span>
                                )}
                                {protocol === 'ollama_compat' && (
                                    <span className={styles.hint}>例: http://127.0.0.1:11434（/v1 不要）</span>
                                )}
                            </div>

                            <div className={styles.field}>
                                <label>API キー環境変数名（任意）</label>
                                <input
                                    className={styles.input}
                                    type="text"
                                    value={apiKeyEnv}
                                    onChange={e => setApiKeyEnv(e.target.value)}
                                    placeholder="例: LMSTUDIO_API_KEY"
                                />
                                <span className={styles.hint}>
                                    キーの値そのものではなく、環境変数の名前を入力します。値はグローバル設定の「環境変数」タブで管理してください。ローカルサーバーで認証不要なら空欄で OK。
                                </span>
                            </div>

                            <div className={styles.testSection}>
                                <button className={styles.testBtn} onClick={handleTest} disabled={testing}>
                                    {testing ? <><Loader size={14} /> テスト中...</> : '接続テスト'}
                                </button>
                                {testResult && testResult.success && (
                                    <div className={styles.testSuccess}>
                                        <CheckCircle size={14} style={{ verticalAlign: 'middle' }} /> 接続成功
                                        {testResult.elapsed_ms != null && ` (${testResult.elapsed_ms}ms)`}
                                        {testResult.models && testResult.models.length > 0 && (
                                            <>
                                                <div style={{ marginTop: 4 }}>
                                                    利用可能なモデル ({testResult.models.length}):
                                                </div>
                                                <div className={styles.modelList}>
                                                    {testResult.models.map(m => <div key={m}>{m}</div>)}
                                                </div>
                                            </>
                                        )}
                                    </div>
                                )}
                                {testResult && !testResult.success && (
                                    <div className={styles.testFail}>
                                        <XCircle size={14} style={{ verticalAlign: 'middle' }} /> 失敗: {testResult.error}
                                    </div>
                                )}
                            </div>


                            {error && <div className={styles.error}>{error}</div>}
                        </>
                    )}
                </div>

                <div className={styles.footer}>
                    <button className={styles.cancelBtn} onClick={onClose}>キャンセル</button>
                    <button className={styles.saveBtn} onClick={handleSave} disabled={saving || loading}>
                        {saving ? '保存中...' : '保存'}
                    </button>
                </div>
            </div>
        </ModalOverlay>
    );
}
