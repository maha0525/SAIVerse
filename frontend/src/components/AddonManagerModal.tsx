"use client";

import React, { useState, useEffect, useCallback } from 'react';
import { X, Package, ChevronDown, ChevronRight, Trash2 } from 'lucide-react';
import ModalOverlay from './common/ModalOverlay';
import MCPSection from './MCPSection';
import OAuthFlowSection, { OAuthFlow } from './OAuthFlowSection';
import styles from './AddonManagerModal.module.css';

// ---------------------------------------------------------------------------
// Types (mirroring backend Pydantic models)
// ---------------------------------------------------------------------------

interface AddonParamSchema {
    key: string;
    label: string;
    description?: string;
    type: 'toggle' | 'text' | 'number' | 'dropdown' | 'slider' | 'file';
    default: unknown;
    persona_configurable: boolean;
    placeholder?: string;
    options?: string[];
    options_endpoint?: string;
    min?: number;
    max?: number;
    step?: number;
    value_type?: 'int' | 'float';
    accept?: string;
    max_size_mb?: number;
    preview?: 'audio' | 'image';
}

interface AddonInfo {
    addon_name: string;
    display_name: string;
    description: string;
    version: string;
    is_enabled: boolean;
    params_schema: AddonParamSchema[];
    params: Record<string, unknown>;
    ui_extensions: {
        bubble_buttons: unknown[];
        input_buttons: unknown[];
    };
    oauth_flows?: OAuthFlow[];
}

interface PersonaPersonaConfig {
    persona_id: string;
    persona_name: string;
    params: Record<string, unknown>;
}

interface AddonManagerModalProps {
    isOpen: boolean;
    onClose: () => void;
}

// ---------------------------------------------------------------------------
// ParamControl — renders a single parameter according to its schema type
// ---------------------------------------------------------------------------

function ParamControl({
    schema,
    value,
    onChange,
    addonName,
    personaId,
}: {
    schema: AddonParamSchema;
    value: unknown;
    onChange: (key: string, val: unknown) => void;
    addonName?: string;
    personaId?: string;
}) {
    const current = value !== undefined ? value : schema.default;

    switch (schema.type) {
        case 'toggle':
            return (
                <label className={styles.toggleLabel}>
                    <input
                        type="checkbox"
                        className={styles.toggleInput}
                        checked={!!current}
                        onChange={(e) => onChange(schema.key, e.target.checked)}
                    />
                    <span className={styles.toggleSlider} />
                </label>
            );

        case 'text':
            return (
                <input
                    type="text"
                    className={styles.textInput}
                    value={String(current ?? '')}
                    placeholder={schema.placeholder ?? ''}
                    onChange={(e) => onChange(schema.key, e.target.value)}
                />
            );

        case 'number': {
            const num = typeof current === 'number' ? current : Number(current ?? schema.min ?? 0);
            return (
                <input
                    type="number"
                    className={styles.numberInput}
                    value={num}
                    min={schema.min}
                    max={schema.max}
                    step={schema.step ?? 1}
                    placeholder={schema.placeholder ?? ''}
                    onChange={(e) => {
                        const v = schema.value_type === 'int'
                            ? parseInt(e.target.value, 10)
                            : parseFloat(e.target.value);
                        onChange(schema.key, isNaN(v) ? schema.default : v);
                    }}
                />
            );
        }

        case 'dropdown':
            return (
                <DropdownParamControl
                    schema={schema}
                    value={current}
                    onChange={onChange}
                    addonName={addonName}
                />
            );

        case 'slider': {
            const min = schema.min ?? 0;
            const max = schema.max ?? 100;
            const step = schema.step ?? 1;
            const snum = typeof current === 'number' ? current : Number(current ?? min);
            return (
                <div className={styles.sliderRow}>
                    <input
                        type="range"
                        className={styles.slider}
                        min={min}
                        max={max}
                        step={step}
                        value={snum}
                        onChange={(e) => {
                            const v = schema.value_type === 'int'
                                ? parseInt(e.target.value, 10)
                                : parseFloat(e.target.value);
                            onChange(schema.key, v);
                        }}
                    />
                    <span className={styles.sliderValue}>{snum}</span>
                </div>
            );
        }

        case 'file':
            return (
                <FileParamControl
                    schema={schema}
                    value={current}
                    onChange={onChange}
                    addonName={addonName}
                    personaId={personaId}
                />
            );

        default:
            return <span className={styles.unsupported}>（未対応の型: {schema.type}）</span>;
    }
}

// ---------------------------------------------------------------------------
// DropdownParamControl — static options or dynamically fetched from addon API
// ---------------------------------------------------------------------------

function DropdownParamControl({
    schema,
    value,
    onChange,
    addonName,
}: {
    schema: AddonParamSchema;
    value: unknown;
    onChange: (key: string, val: unknown) => void;
    addonName?: string;
}) {
    const [dynamicOptions, setDynamicOptions] = useState<string[] | null>(null);
    const [loading, setLoading] = useState(false);

    useEffect(() => {
        if (!schema.options_endpoint || !addonName) return;
        const ep = schema.options_endpoint;
        const url = ep.startsWith('/') ? ep : `/api/addon/${addonName}/${ep}`;
        setLoading(true);
        fetch(url)
            .then((r) => r.json())
            .then((data) => {
                // 想定レスポンス: {"options": [...]} または [...]（プレーン配列）
                const opts: unknown = Array.isArray(data) ? data : data?.options;
                if (Array.isArray(opts)) {
                    setDynamicOptions(opts.map((o) => String(o)));
                }
            })
            .catch(() => setDynamicOptions([]))
            .finally(() => setLoading(false));
    }, [schema.options_endpoint, addonName]);

    const options = dynamicOptions ?? schema.options ?? [];
    const current = value !== undefined ? value : schema.default;

    return (
        <select
            className={styles.select}
            value={String(current ?? '')}
            onChange={(e) => onChange(schema.key, e.target.value)}
            disabled={loading}
        >
            {loading && <option value="">読み込み中...</option>}
            {!loading && options.length === 0 && <option value="">（選択肢なし）</option>}
            {!loading && options.map((opt) => (
                <option key={opt} value={opt}>{opt}</option>
            ))}
        </select>
    );
}

// ---------------------------------------------------------------------------
// FileParamControl — file upload / preview / delete
// ---------------------------------------------------------------------------

function FileParamControl({
    schema,
    value,
    onChange,
    addonName,
    personaId,
}: {
    schema: AddonParamSchema;
    value: unknown;
    onChange: (key: string, val: unknown) => void;
    addonName?: string;
    personaId?: string;
}) {
    const [uploading, setUploading] = React.useState(false);
    const [error, setError] = React.useState<string | null>(null);
    const [previewUrl, setPreviewUrl] = React.useState<string | null>(null);
    const fileInputRef = React.useRef<HTMLInputElement>(null);

    const hasFile = !!value;
    const canUpload = !!addonName && !!personaId;

    // Build API URLs
    const fileApiBase = canUpload
        ? `/api/addon/${addonName}/config/persona/${encodeURIComponent(personaId!)}/file/${schema.key}`
        : null;

    // Set preview URL when file exists
    React.useEffect(() => {
        if (hasFile && fileApiBase) {
            setPreviewUrl(fileApiBase);
        } else {
            setPreviewUrl(null);
        }
    }, [hasFile, fileApiBase]);

    const handleUpload = async (file: File) => {
        if (!fileApiBase) return;
        setUploading(true);
        setError(null);
        try {
            const formData = new FormData();
            formData.append('file', file);
            const res = await fetch(fileApiBase, {
                method: 'POST',
                body: formData,
            });
            if (!res.ok) {
                const body = await res.json().catch(() => ({ detail: res.statusText }));
                throw new Error(body.detail || `Upload failed: ${res.status}`);
            }
            const data = await res.json();
            onChange(schema.key, data.path);
        } catch (err) {
            setError(err instanceof Error ? err.message : 'Upload failed');
        } finally {
            setUploading(false);
        }
    };

    const handleDelete = async () => {
        if (!fileApiBase) return;
        setError(null);
        try {
            await fetch(fileApiBase, { method: 'DELETE' });
            onChange(schema.key, undefined);
            setPreviewUrl(null);
        } catch (err) {
            setError(err instanceof Error ? err.message : 'Delete failed');
        }
    };

    const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
        const f = e.target.files?.[0];
        if (f) handleUpload(f);
        if (e.target) e.target.value = '';
    };

    const handleDrop = (e: React.DragEvent) => {
        e.preventDefault();
        const f = e.dataTransfer.files[0];
        if (f) handleUpload(f);
    };

    return (
        <div className={styles.fileControl}>
            {/* Current file info + preview */}
            {hasFile && previewUrl && (
                <div className={styles.filePreviewRow}>
                    {schema.preview === 'audio' && (
                        <audio controls src={previewUrl} className={styles.audioPreview}>
                            <track kind="captions" />
                        </audio>
                    )}
                    {schema.preview === 'image' && (
                        <img src={previewUrl} alt={schema.label} className={styles.imagePreview} />
                    )}
                    {!schema.preview && (
                        <span className={styles.fileNameDisplay}>
                            {String(value).split(/[\\/]/).pop()}
                        </span>
                    )}
                    <button
                        className={styles.fileDeleteBtn}
                        onClick={handleDelete}
                        title="削除（デフォルトに戻す）"
                    >
                        <Trash2 size={13} />
                    </button>
                </div>
            )}

            {/* Upload area */}
            <div
                className={`${styles.fileDropArea} ${uploading ? styles.fileUploading : ''}`}
                onDragOver={(e) => e.preventDefault()}
                onDrop={handleDrop}
                onClick={() => fileInputRef.current?.click()}
            >
                <span className={styles.fileDropText}>
                    {uploading
                        ? 'アップロード中...'
                        : hasFile
                            ? 'ファイルを差し替え'
                            : 'ファイルをドロップ or クリック'}
                </span>
                {schema.description && !hasFile && (
                    <span className={styles.fileHint}>{schema.description}</span>
                )}
                <input
                    ref={fileInputRef}
                    type="file"
                    style={{ display: 'none' }}
                    accept={schema.accept}
                    onChange={handleFileSelect}
                />
            </div>

            {/* Error display */}
            {error && <span className={styles.fileError}>{error}</span>}

            {/* Size limit hint */}
            {!hasFile && schema.max_size_mb && (
                <span className={styles.fileHint}>
                    最大 {schema.max_size_mb >= 1024 ? `${(schema.max_size_mb / 1024).toFixed(1)} GB` : `${schema.max_size_mb} MB`}
                    {schema.accept && ` / ${schema.accept.split(',').map(t => t.split('/')[1]).join(', ')}`}
                </span>
            )}
        </div>
    );
}

// ---------------------------------------------------------------------------
// ParamsSection — list of parameters with global + per-persona sections
// ---------------------------------------------------------------------------

function ParamsSection({
    addon,
    personas,
}: {
    addon: AddonInfo;
    personas: { id: string; name: string }[];
}) {
    const [globalParams, setGlobalParams] = useState<Record<string, unknown>>(addon.params ?? {});
    const [personaConfigs, setPersonaConfigs] = useState<PersonaPersonaConfig[]>([]);
    const [saving, setSaving] = useState(false);
    const [selectedPersonaId, setSelectedPersonaId] = useState<string>('');

    const configurableSchemas = addon.params_schema.filter((s) => !s.persona_configurable);
    const personaConfigurableSchemas = addon.params_schema.filter((s) => s.persona_configurable);

    // 保存（グローバル）
    const saveGlobal = useCallback(async (params: Record<string, unknown>) => {
        setSaving(true);
        try {
            await fetch(`/api/addon/${addon.addon_name}/config`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ params }),
            });
        } finally {
            setSaving(false);
        }
    }, [addon.addon_name]);

    const handleGlobalChange = (key: string, val: unknown) => {
        const next = { ...globalParams, [key]: val };
        setGlobalParams(next);
        saveGlobal(next);
    };

    // ペルソナ設定のロード
    useEffect(() => {
        if (personaConfigurableSchemas.length === 0) return;
        // 全ペルソナの設定を並行取得
        Promise.all(
            personas.map((p) =>
                fetch(`/api/addon/${addon.addon_name}/config/persona/${p.id}`)
                    .then((r) => r.json())
                    .then((data) => ({ persona_id: p.id, persona_name: p.name, params: data.params ?? {} }))
            )
        ).then((configs) => {
            // 設定がある（空でない）ペルソナのみ表示
            setPersonaConfigs(configs.filter((c) => Object.keys(c.params).length > 0));
        });
    }, [addon.addon_name, personas, personaConfigurableSchemas.length]);

    const savePersona = async (personaId: string, params: Record<string, unknown>) => {
        await fetch(`/api/addon/${addon.addon_name}/config/persona/${personaId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ params }),
        });
    };

    const handlePersonaChange = (personaId: string, key: string, val: unknown) => {
        setPersonaConfigs((prev) => {
            const next = prev.map((c) =>
                c.persona_id === personaId ? { ...c, params: { ...c.params, [key]: val } } : c
            );
            const config = next.find((c) => c.persona_id === personaId);
            if (config) savePersona(personaId, config.params);
            return next;
        });
    };

    const addPersonaConfig = (personaId: string, personaName: string) => {
        if (personaConfigs.find((c) => c.persona_id === personaId)) return;
        const defaultParams: Record<string, unknown> = {};
        personaConfigurableSchemas.forEach((s) => {
            defaultParams[s.key] = s.default;
        });
        const newConfig: PersonaPersonaConfig = { persona_id: personaId, persona_name: personaName, params: defaultParams };
        setPersonaConfigs((prev) => [...prev, newConfig]);
        savePersona(personaId, defaultParams);
    };

    if (configurableSchemas.length === 0 && personaConfigurableSchemas.length === 0) {
        return <p className={styles.noParams}>設定項目はありません</p>;
    }

    const selectedConfig = personaConfigs.find((c) => c.persona_id === selectedPersonaId);
    const selectedPersona = personas.find((p) => p.id === selectedPersonaId);

    return (
        <div className={styles.paramsSection}>
            {/* Global params */}
            <div className={styles.paramsGroup}>
                <div className={styles.paramsGroupLabel}>デフォルト（全ペルソナ共通）</div>
                {configurableSchemas.map((schema) => (
                    <div key={schema.key} className={styles.paramRow}>
                        <span className={styles.paramLabel}>{schema.label}</span>
                        <ParamControl
                            schema={schema}
                            value={globalParams[schema.key]}
                            onChange={handleGlobalChange}
                            addonName={addon.addon_name}
                        />
                    </div>
                ))}
                {saving && <span className={styles.savingHint}>保存中...</span>}
            </div>

            {/* Per-persona params */}
            {personaConfigurableSchemas.length > 0 && (
                <div className={styles.personaSection}>
                    <div className={styles.personaSectionHeader}>
                        <span className={styles.paramsGroupLabel}>ペルソナ別設定</span>
                    </div>

                    <select
                        className={styles.personaSelector}
                        value={selectedPersonaId}
                        onChange={(e) => setSelectedPersonaId(e.target.value)}
                    >
                        <option value="">-- ペルソナを選択 --</option>
                        {personas.map((p) => {
                            const isConfigured = personaConfigs.some((c) => c.persona_id === p.id);
                            return (
                                <option key={p.id} value={p.id}>
                                    {p.name}{isConfigured ? '' : '（未設定）'}
                                </option>
                            );
                        })}
                    </select>

                    {selectedPersonaId && !selectedConfig && selectedPersona && (
                        <div className={styles.personaUnconfiguredHint}>
                            <span>このペルソナには個別設定がありません。</span>
                            <button
                                className={styles.createPersonaConfigBtn}
                                onClick={() => addPersonaConfig(selectedPersona.id, selectedPersona.name)}
                            >
                                個別設定を作成
                            </button>
                        </div>
                    )}

                    {selectedConfig && (
                        <div className={styles.personaConfigBlock}>
                            {personaConfigurableSchemas.map((schema) => (
                                <div key={schema.key} className={styles.paramRow}>
                                    <span className={styles.paramLabel}>{schema.label}</span>
                                    <ParamControl
                                        schema={schema}
                                        value={selectedConfig.params[schema.key]}
                                        onChange={(key, val) => handlePersonaChange(selectedConfig.persona_id, key, val)}
                                        addonName={addon.addon_name}
                                        personaId={selectedConfig.persona_id}
                                    />
                                </div>
                            ))}
                        </div>
                    )}
                </div>
            )}
        </div>
    );
}

// ---------------------------------------------------------------------------
// AddonCard — single addon row with expand/collapse
// ---------------------------------------------------------------------------

function AddonCard({
    addon,
    personas,
    onToggleEnabled,
}: {
    addon: AddonInfo;
    personas: { id: string; name: string }[];
    onToggleEnabled: (addonName: string, enabled: boolean) => void;
}) {
    const [expanded, setExpanded] = useState(false);

    const handleToggle = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const enabled = e.target.checked;
        await fetch(`/api/addon/${addon.addon_name}/enabled`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ is_enabled: enabled }),
        });
        onToggleEnabled(addon.addon_name, enabled);
    };

    return (
        <div className={`${styles.addonCard} ${!addon.is_enabled ? styles.disabled : ''}`}>
            <div className={styles.addonCardHeader}>
                <button
                    className={styles.expandBtn}
                    onClick={() => setExpanded((v) => !v)}
                    aria-label={expanded ? '折りたたむ' : '展開する'}
                >
                    {expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
                </button>
                <div className={styles.addonMeta} onClick={() => setExpanded((v) => !v)}>
                    <div className={styles.addonMetaRow}>
                        <span className={styles.addonName}>{addon.display_name || addon.addon_name}</span>
                        {addon.version && (
                            <span className={styles.addonVersion}>v{addon.version}</span>
                        )}
                    </div>
                    {addon.description && (
                        <span className={styles.addonDesc}>{addon.description}</span>
                    )}
                </div>
                <label className={styles.enabledToggle} onClick={(e) => e.stopPropagation()}>
                    <input
                        type="checkbox"
                        className={styles.toggleInput}
                        checked={addon.is_enabled}
                        onChange={handleToggle}
                    />
                    <span className={styles.toggleSlider} />
                </label>
            </div>
            {expanded && addon.is_enabled && (
                <div className={styles.addonCardBody}>
                    <ParamsSection addon={addon} personas={personas} />
                    {addon.oauth_flows && addon.oauth_flows.length > 0 && (
                        <OAuthFlowSection
                            addonName={addon.addon_name}
                            flows={addon.oauth_flows}
                            personas={personas}
                        />
                    )}
                    <MCPSection addonName={addon.addon_name} defaultCollapsed={true} />
                </div>
            )}
            {expanded && !addon.is_enabled && (
                <div className={styles.addonCardBody}>
                    <p className={styles.disabledNote}>アドオンが無効です。有効にすると設定を変更できます。</p>
                </div>
            )}
        </div>
    );
}

// ---------------------------------------------------------------------------
// AddonManagerModal — main modal
// ---------------------------------------------------------------------------

export default function AddonManagerModal({ isOpen, onClose }: AddonManagerModalProps) {
    const [addons, setAddons] = useState<AddonInfo[]>([]);
    const [personas, setPersonas] = useState<{ id: string; name: string }[]>([]);
    const [loading, setLoading] = useState(false);
    const [fetchError, setFetchError] = useState<string | null>(null);

    useEffect(() => {
        if (!isOpen) return;
        setLoading(true);
        setFetchError(null);
        Promise.all([
            fetch('/api/addon/').then((r) => {
                if (!r.ok) throw new Error(`/api/addon/ ${r.status} ${r.statusText}`);
                return r.json();
            }),
            fetch('/api/people/').then((r) => {
                if (!r.ok) throw new Error(`/api/people/ ${r.status} ${r.statusText}`);
                return r.json();
            }),
        ]).then(([addonData, peopleData]) => {
            setAddons(Array.isArray(addonData) ? addonData : []);
            const list = Array.isArray(peopleData)
                ? peopleData.map((p: { id?: string; AIID?: string; name?: string; AINAME?: string }) => ({
                    id: p.id ?? p.AIID ?? '',
                    name: p.name ?? p.AINAME ?? p.id ?? '',
                }))
                : [];
            setPersonas(list);
        }).catch((err: unknown) => {
            const msg = err instanceof Error ? err.message : String(err);
            setFetchError(msg);
            console.error('[AddonManager] fetch failed:', msg);
        }).finally(() => setLoading(false));
    }, [isOpen]);

    const handleToggleEnabled = (addonName: string, enabled: boolean) => {
        setAddons((prev) =>
            prev.map((a) => a.addon_name === addonName ? { ...a, is_enabled: enabled } : a)
        );
    };

    if (!isOpen) return null;

    return (
        <ModalOverlay onClose={onClose}>
            <div className={styles.modal} onClick={(e) => e.stopPropagation()}>
                <div className={styles.header}>
                    <div className={styles.headerTitle}>
                        <Package size={22} />
                        <h2>アドオン管理</h2>
                    </div>
                    <button className={styles.closeButton} onClick={onClose}>
                        <X size={20} />
                    </button>
                </div>

                <div className={styles.content}>
                    {!loading && !fetchError && (
                        <MCPSection defaultCollapsed={true} />
                    )}
                    {loading && <p className={styles.loadingText}>読み込み中...</p>}
                    {!loading && fetchError && (
                        <div className={styles.errorState}>
                            <p>読み込みに失敗しました</p>
                            <p className={styles.errorDetail}>{fetchError}</p>
                        </div>
                    )}
                    {!loading && !fetchError && addons.length === 0 && (
                        <div className={styles.emptyState}>
                            <Package size={40} className={styles.emptyIcon} />
                            <p>インストール済みのアドオンはありません</p>
                            <p className={styles.emptyHint}>
                                expansion_data/ フォルダに addon.json を含むアドオンを配置してください
                            </p>
                        </div>
                    )}
                    {!loading && addons.map((addon) => (
                        <AddonCard
                            key={addon.addon_name}
                            addon={addon}
                            personas={personas}
                            onToggleEnabled={handleToggleEnabled}
                        />
                    ))}
                </div>
            </div>
        </ModalOverlay>
    );
}
