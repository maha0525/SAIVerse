"use client";

import React, { useState, useEffect, useCallback } from 'react';
import { X, Package, ChevronDown, ChevronRight, Trash2, Plus } from 'lucide-react';
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
    type: 'toggle' | 'text' | 'password' | 'number' | 'dropdown' | 'slider' | 'file' | 'dict';
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
    // 任意: true でこの行をアコーディオン (折り畳み可能) として描画する。
    // 大きな入力 UI (dict など) や上級者向け項目を普段は折り畳んでおきたい用途で使う。
    collapsible?: boolean;
    default_collapsed?: boolean;  // 初期状態 (collapsible=true 時のみ意味がある)。既定 true。
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

        case 'password':
            return (
                <input
                    type="password"
                    className={styles.textInput}
                    value={String(current ?? '')}
                    placeholder={schema.placeholder ?? ''}
                    autoComplete="off"
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

        case 'dict':
            return (
                <DictParamControl
                    schema={schema}
                    value={current}
                    onChange={onChange}
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
// DictParamControl — editable key/value table for dict-type params
//
// addon.json で `type: "dict"` を指定したパラメータを編集するための UI。
// 値は `Record<string, string>` として保存される (現バージョンは値型 string のみ)。
// 既存値に非文字列が含まれていた場合は表示時に String() で文字列化される。
// ---------------------------------------------------------------------------

function DictParamControl({
    schema,
    value,
    onChange,
}: {
    schema: AddonParamSchema;
    value: unknown;
    onChange: (key: string, val: unknown) => void;
}) {
    type Row = { id: number; k: string; v: string };

    const nextIdRef = React.useRef<number>(0);
    const [rows, setRows] = React.useState<Row[]>(() => {
        const dict = (value && typeof value === 'object' && !Array.isArray(value))
            ? (value as Record<string, unknown>)
            : {};
        const initial = Object.entries(dict).map(([k, v]) => ({
            id: nextIdRef.current++,
            k,
            v: typeof v === 'string' ? v : String(v ?? ''),
        }));
        return initial;
    });

    const commit = (next: Row[]) => {
        const dict: Record<string, string> = {};
        for (const r of next) {
            const k = r.k.trim();
            if (!k) continue;
            dict[k] = r.v;
        }
        onChange(schema.key, dict);
    };

    const updateRow = (id: number, patch: Partial<Pick<Row, 'k' | 'v'>>) => {
        setRows((prev) => {
            const next = prev.map((r) => r.id === id ? { ...r, ...patch } : r);
            commit(next);
            return next;
        });
    };

    const addRow = () => {
        setRows((prev) => [...prev, { id: nextIdRef.current++, k: '', v: '' }]);
        // 新規空行は commit しない (空キーはサーバ送信時に除外されるため)
    };

    const deleteRow = (id: number) => {
        setRows((prev) => {
            const next = prev.filter((r) => r.id !== id);
            commit(next);
            return next;
        });
    };

    return (
        <div className={styles.dictControl}>
            {rows.length > 0 && (
                <div className={styles.dictHeader}>
                    <span className={styles.dictHeaderCell}>キー（誤読される語）</span>
                    <span className={styles.dictHeaderCell}>値（読ませたい表記）</span>
                    <span className={styles.dictHeaderSpacer} />
                </div>
            )}
            {rows.length === 0 && (
                <span className={styles.dictEmpty}>(エントリなし)</span>
            )}
            {rows.map((row) => (
                <div key={row.id} className={styles.dictRow}>
                    <input
                        type="text"
                        className={styles.dictKeyInput}
                        value={row.k}
                        placeholder="key"
                        onChange={(e) => updateRow(row.id, { k: e.target.value })}
                    />
                    <input
                        type="text"
                        className={styles.dictValueInput}
                        value={row.v}
                        placeholder="value"
                        onChange={(e) => updateRow(row.id, { v: e.target.value })}
                    />
                    <button
                        className={styles.dictDeleteBtn}
                        onClick={() => deleteRow(row.id)}
                        title="この行を削除"
                        type="button"
                    >
                        <Trash2 size={13} />
                    </button>
                </div>
            ))}
            <button
                className={styles.dictAddBtn}
                onClick={addRow}
                type="button"
            >
                <Plus size={13} /> 追加
            </button>
        </div>
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
// ParamRow — paramLabel + control の 1 行。schema.collapsible が true のとき
// アコーディオン (折り畳み可能なヘッダ + ボディ) として描画する。
// ---------------------------------------------------------------------------

function ParamRow({
    schema,
    children,
}: {
    schema: AddonParamSchema;
    children: React.ReactNode;
}) {
    const isCollapsible = !!schema.collapsible;
    const [collapsed, setCollapsed] = useState<boolean>(
        isCollapsible ? (schema.default_collapsed ?? true) : false
    );

    if (isCollapsible) {
        return (
            <div className={styles.paramRowAccordion}>
                <button
                    type="button"
                    className={styles.paramRowAccordionHeader}
                    onClick={() => setCollapsed((v) => !v)}
                    aria-expanded={!collapsed}
                >
                    {collapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
                    <span className={styles.paramLabel}>{schema.label}</span>
                </button>
                {!collapsed && (
                    <div className={styles.paramRowAccordionBody}>
                        {children}
                    </div>
                )}
            </div>
        );
    }

    return (
        <div className={`${styles.paramRow} ${schema.type === 'dict' ? styles.paramRowStacked : ''}`}>
            <span className={styles.paramLabel}>{schema.label}</span>
            {children}
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
    const oauthFlows = addon.oauth_flows ?? [];
    const hasOAuthFlows = oauthFlows.length > 0;
    const hasPersonaSection = personaConfigurableSchemas.length > 0 || hasOAuthFlows;

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

    // ペルソナ設定のロード（per-persona params がある時だけ取得）
    useEffect(() => {
        if (personaConfigurableSchemas.length === 0) return;
        Promise.all(
            personas.map((p) =>
                fetch(`/api/addon/${addon.addon_name}/config/persona/${p.id}`)
                    .then((r) => r.json())
                    .then((data) => ({ persona_id: p.id, persona_name: p.name, params: data.params ?? {} }))
            )
        ).then((configs) => {
            // 全 persona 分を保持。空なら defaults でレンダーされるので filter しない
            setPersonaConfigs(configs);
        });
    }, [addon.addon_name, personas, personaConfigurableSchemas.length]);

    /**
     * 1キー単位で merge 保存する。API 側は merge セマンティクスなので、
     * 既存の他キー (OAuth トークン等) は破壊されない。
     */
    const savePersona = async (personaId: string, partial: Record<string, unknown>) => {
        await fetch(`/api/addon/${addon.addon_name}/config/persona/${personaId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ params: partial }),
        });
    };

    const handlePersonaChange = (personaId: string, key: string, val: unknown) => {
        setPersonaConfigs((prev) => {
            const idx = prev.findIndex((c) => c.persona_id === personaId);
            if (idx >= 0) {
                return prev.map((c) =>
                    c.persona_id === personaId ? { ...c, params: { ...c.params, [key]: val } } : c
                );
            }
            // まだ personaConfigs に居なければ新規エントリ
            const personaName = personas.find((p) => p.id === personaId)?.name ?? personaId;
            return [...prev, { persona_id: personaId, persona_name: personaName, params: { [key]: val } }];
        });
        // merge 保存: 変更したキーだけ送る
        savePersona(personaId, { [key]: val });
    };

    if (configurableSchemas.length === 0 && !hasPersonaSection) {
        return <p className={styles.noParams}>設定項目はありません</p>;
    }

    // 選択中ペルソナの params。未保存ペルソナでは空 dict を使い、各 ParamControl が default にフォールバックする
    const selectedPersonaParams: Record<string, unknown> =
        personaConfigs.find((c) => c.persona_id === selectedPersonaId)?.params ?? {};

    return (
        <div className={styles.paramsSection}>
            {/* Global params */}
            {configurableSchemas.length > 0 && (
                <div className={styles.paramsGroup}>
                    <div className={styles.paramsGroupLabel}>デフォルト（全ペルソナ共通）</div>
                    {configurableSchemas.map((schema) => (
                        <ParamRow key={schema.key} schema={schema}>
                            <ParamControl
                                schema={schema}
                                value={globalParams[schema.key]}
                                onChange={handleGlobalChange}
                                addonName={addon.addon_name}
                            />
                        </ParamRow>
                    ))}
                    {saving && <span className={styles.savingHint}>保存中...</span>}
                </div>
            )}

            {/* Per-persona section: per-persona params + OAuth flows are aligned to the same selected persona */}
            {hasPersonaSection && (
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
                        {personas.map((p) => (
                            <option key={p.id} value={p.id}>{p.name}</option>
                        ))}
                    </select>

                    {selectedPersonaId && (
                        <>
                            {/* Per-persona params: 未保存でも default 値で編集可能 */}
                            {personaConfigurableSchemas.length > 0 && (
                                <div className={styles.personaConfigBlock}>
                                    {personaConfigurableSchemas.map((schema) => (
                                        <ParamRow key={schema.key} schema={schema}>
                                            <ParamControl
                                                schema={schema}
                                                value={selectedPersonaParams[schema.key]}
                                                onChange={(key, val) => handlePersonaChange(selectedPersonaId, key, val)}
                                                addonName={addon.addon_name}
                                                personaId={selectedPersonaId}
                                            />
                                        </ParamRow>
                                    ))}
                                </div>
                            )}

                            {/* OAuth flows: 選択中ペルソナに紐付く外部サービス連携 */}
                            {hasOAuthFlows && (
                                <OAuthFlowSection
                                    addonName={addon.addon_name}
                                    flows={oauthFlows}
                                    personaId={selectedPersonaId}
                                />
                            )}
                        </>
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
