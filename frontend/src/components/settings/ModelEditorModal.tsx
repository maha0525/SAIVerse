import React, { useState, useEffect } from 'react';
import { X } from 'lucide-react';
import styles from './ModelEditorModal.module.css';
import ModalOverlay from '../common/ModalOverlay';

export type ModelEditorMode = 'create' | 'edit';

interface ProviderChoice {
    id: string;
    display_name: string;
}

export interface ModelCloneSource {
    key: string;
    config: Record<string, unknown>;
}

interface Props {
    isOpen: boolean;
    mode: ModelEditorMode;
    modelKey?: string;  // required when mode='edit'
    cloneSource?: ModelCloneSource;  // pre-fill for create mode (from duplicate)
    onClose: () => void;
    onSaved: () => void;
}

// Fields surfaced as dedicated form inputs. Everything else lives in the JSON editor.
const BASIC_FIELDS = ['model', 'display_name', 'provider_ref', 'context_length'] as const;
const DEFAULT_CONTEXT_LENGTH = 128000;

export default function ModelEditorModal({ isOpen, mode, modelKey, cloneSource, onClose, onSaved }: Props) {
    const [key, setKey] = useState('');
    // Basic fields (dedicated inputs)
    const [model, setModel] = useState('');
    const [displayName, setDisplayName] = useState('');
    const [providerRef, setProviderRef] = useState('');
    const [contextLength, setContextLength] = useState<number>(DEFAULT_CONTEXT_LENGTH);
    // Everything else (JSON editor)
    const [extraJson, setExtraJson] = useState('{}');
    const [providers, setProviders] = useState<ProviderChoice[]>([]);
    const [source, setSource] = useState<string>('user_data');
    const [loading, setLoading] = useState(false);
    const [saving, setSaving] = useState(false);
    const [parseError, setParseError] = useState<string | null>(null);
    const [saveError, setSaveError] = useState<string | null>(null);

    const applyConfig = (k: string, cfg: Record<string, unknown>) => {
        setKey(k);
        setModel(typeof cfg.model === 'string' ? cfg.model : '');
        setDisplayName(typeof cfg.display_name === 'string' ? cfg.display_name : '');
        setProviderRef(typeof cfg.provider_ref === 'string' ? cfg.provider_ref : '');
        setContextLength(
            typeof cfg.context_length === 'number' ? cfg.context_length : DEFAULT_CONTEXT_LENGTH,
        );
        const extra: Record<string, unknown> = {};
        for (const [field, value] of Object.entries(cfg)) {
            if (!(BASIC_FIELDS as readonly string[]).includes(field)) {
                extra[field] = value;
            }
        }
        setExtraJson(JSON.stringify(extra, null, 2));
        setSource('user_data');
    };

    useEffect(() => {
        if (!isOpen) return;
        setSaveError(null);
        loadProviderList();
        if (mode === 'edit' && modelKey) {
            loadModel(modelKey);
        } else if (mode === 'create' && cloneSource) {
            applyConfig(`${cloneSource.key}-copy`, cloneSource.config);
        } else {
            setKey('');
            setModel('');
            setDisplayName('');
            setProviderRef('');
            setContextLength(DEFAULT_CONTEXT_LENGTH);
            setExtraJson('{}');
            setSource('user_data');
        }
    }, [isOpen, mode, modelKey, cloneSource]);

    // Live JSON validation for the extras textarea
    useEffect(() => {
        if (!extraJson.trim()) {
            setParseError(null);
            return;
        }
        try {
            const parsed = JSON.parse(extraJson);
            if (typeof parsed !== 'object' || Array.isArray(parsed) || parsed === null) {
                setParseError('追加設定はオブジェクト ({}) で指定してください');
                return;
            }
            setParseError(null);
        } catch (e) {
            setParseError(`JSON エラー: ${(e as Error).message}`);
        }
    }, [extraJson]);

    const loadProviderList = async () => {
        try {
            const res = await fetch('/api/providers');
            if (!res.ok) return;
            const data = await res.json();
            setProviders(
                (data as Array<{ id: string; display_name: string }>).map(p => ({
                    id: p.id,
                    display_name: p.display_name,
                })),
            );
        } catch (e) {
            console.error('Failed to load provider list', e);
        }
    };

    const loadModel = async (k: string) => {
        setLoading(true);
        try {
            const res = await fetch(`/api/config/models/${k}`);
            if (!res.ok) {
                setSaveError(`読み込み失敗: HTTP ${res.status}`);
                return;
            }
            const data = await res.json();
            const cfg = (data.config ?? {}) as Record<string, unknown>;
            applyConfig(data.key, cfg);
            setSource(data.source);
        } catch (e) {
            setSaveError(`読み込み失敗: ${e}`);
        } finally {
            setLoading(false);
        }
    };

    const handleSave = async () => {
        setSaveError(null);

        if (!key || !key.match(/^[a-zA-Z0-9_.\-]+$/)) {
            setSaveError('キーは英数字・ハイフン・アンダースコア・ドットのみ使用可能です');
            return;
        }
        if (!model.trim()) {
            setSaveError('モデル ID (model) を入力してください');
            return;
        }
        if (!Number.isFinite(contextLength) || contextLength <= 0) {
            setSaveError('context_length は正の整数を入力してください');
            return;
        }

        let extra: Record<string, unknown>;
        try {
            extra = extraJson.trim() ? JSON.parse(extraJson) : {};
            if (typeof extra !== 'object' || Array.isArray(extra) || extra === null) {
                setSaveError('追加設定はオブジェクトで指定してください');
                return;
            }
        } catch (e) {
            setSaveError(`追加設定の JSON が不正です: ${(e as Error).message}`);
            return;
        }

        // Basic fields override extra (so accidental duplicates in JSON don't shadow the form)
        const merged: Record<string, unknown> = {
            ...extra,
            model: model.trim(),
            context_length: contextLength,
        };
        if (displayName.trim()) {
            merged.display_name = displayName.trim();
        } else {
            delete merged.display_name;
        }
        if (providerRef) {
            merged.provider_ref = providerRef;
        } else {
            delete merged.provider_ref;
        }

        setSaving(true);
        try {
            let res: Response;
            if (mode === 'create') {
                res = await fetch('/api/config/models', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ key, config: merged }),
                });
            } else {
                res = await fetch(`/api/config/models/${key}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ config: merged }),
                });
            }
            if (!res.ok) {
                const text = await res.text();
                setSaveError(`保存失敗: HTTP ${res.status} ${text}`);
                return;
            }
            onSaved();
            onClose();
        } catch (e) {
            setSaveError(`保存失敗: ${e}`);
        } finally {
            setSaving(false);
        }
    };

    if (!isOpen) return null;

    const isShadowingNonUser = mode === 'edit' && source !== 'user_data';

    return (
        <ModalOverlay onClose={onClose}>
            <div className={styles.modal}>
                <div className={styles.header}>
                    <h3>{mode === 'create' ? (cloneSource ? 'モデルを複製' : 'モデルを新規作成') : `モデルを編集: ${key}`}</h3>
                    <button className={styles.closeBtn} onClick={onClose}><X size={20} /></button>
                </div>

                <div className={styles.content}>
                    {loading ? (
                        <div>読み込み中...</div>
                    ) : (
                        <>
                            {isShadowingNonUser && (
                                <div className={styles.warningBanner}>
                                    {source} のモデルを編集中です。保存すると user_data に上書きが作成され、{source} は変更されません。リセットしたい場合は user_data 側のファイルを削除してください。
                                </div>
                            )}

                            <div className={styles.field}>
                                <label>キー（ファイル名）</label>
                                <input
                                    className={styles.input}
                                    type="text"
                                    value={key}
                                    onChange={e => setKey(e.target.value)}
                                    placeholder="例: qwen-via-lmstudio"
                                    disabled={mode === 'edit'}
                                />
                            </div>

                            <div className={styles.field}>
                                <label>モデル ID (model)</label>
                                <input
                                    className={styles.input}
                                    type="text"
                                    value={model}
                                    onChange={e => setModel(e.target.value)}
                                    placeholder="例: qwen2.5-72b-instruct"
                                />
                                <span className={styles.hint}>API 呼び出しに使うモデル名（プロバイダ側のモデル ID）</span>
                            </div>

                            <div className={styles.field}>
                                <label>表示名 (display_name)</label>
                                <input
                                    className={styles.input}
                                    type="text"
                                    value={displayName}
                                    onChange={e => setDisplayName(e.target.value)}
                                    placeholder="例: Qwen 2.5 72B (LM Studio)"
                                />
                            </div>

                            <div className={styles.field}>
                                <label>プロバイダ参照 (provider_ref)</label>
                                <select
                                    className={styles.input}
                                    value={providerRef}
                                    onChange={e => setProviderRef(e.target.value)}
                                >
                                    <option value="">（参照なし — provider/base_url を直接指定する場合）</option>
                                    {providers.map(p => (
                                        <option key={p.id} value={p.id}>
                                            {p.display_name} ({p.id})
                                        </option>
                                    ))}
                                </select>
                                <span className={styles.hint}>
                                    プロバイダを選ぶと base_url / api_key_env がプロバイダ側から自動継承されます
                                </span>
                            </div>

                            <div className={styles.field}>
                                <label>コンテキスト長 (context_length)</label>
                                <input
                                    className={styles.input}
                                    type="number"
                                    value={contextLength}
                                    onChange={e => setContextLength(parseInt(e.target.value, 10) || 0)}
                                    min={1}
                                    step={1}
                                />
                            </div>

                            <div className={styles.field}>
                                <label>
                                    <span>追加設定 (JSON)</span>
                                    {parseError && <span className={styles.parseError}>{parseError}</span>}
                                </label>
                                <textarea
                                    className={`${styles.textarea} ${parseError ? styles.textareaError : ''}`}
                                    value={extraJson}
                                    onChange={e => setExtraJson(e.target.value)}
                                    rows={12}
                                    spellCheck={false}
                                />
                                <span className={styles.hint}>
                                    parameters / pricing / cache / supports_images など、上記以外のフィールドを JSON で指定。
                                    空 ({'{}'}) で問題ない場合も多い。詳細スキーマは builtin_data/models/ の例を参照。
                                </span>
                            </div>

                            {saveError && <div className={styles.error}>{saveError}</div>}
                        </>
                    )}
                </div>

                <div className={styles.footer}>
                    <button className={styles.cancelBtn} onClick={onClose}>キャンセル</button>
                    <button
                        className={styles.saveBtn}
                        onClick={handleSave}
                        disabled={saving || loading || !!parseError}
                    >
                        {saving ? '保存中...' : '保存'}
                    </button>
                </div>
            </div>
        </ModalOverlay>
    );
}
