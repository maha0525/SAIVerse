"use client";

import React, { useState, useEffect, useCallback } from 'react';
import { X, Package, ChevronDown, ChevronRight, Plus, Trash2 } from 'lucide-react';
import ModalOverlay from './common/ModalOverlay';
import styles from './AddonManagerModal.module.css';

// ---------------------------------------------------------------------------
// Types (mirroring backend Pydantic models)
// ---------------------------------------------------------------------------

interface AddonParamSchema {
    key: string;
    label: string;
    type: 'toggle' | 'dropdown' | 'slider' | 'file';
    default: unknown;
    persona_configurable: boolean;
    options?: string[];
    min?: number;
    max?: number;
    step?: number;
    value_type?: 'int' | 'float';
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
}: {
    schema: AddonParamSchema;
    value: unknown;
    onChange: (key: string, val: unknown) => void;
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

        case 'dropdown':
            return (
                <select
                    className={styles.select}
                    value={String(current ?? '')}
                    onChange={(e) => onChange(schema.key, e.target.value)}
                >
                    {(schema.options ?? []).map((opt) => (
                        <option key={opt} value={opt}>{opt}</option>
                    ))}
                </select>
            );

        case 'slider': {
            const min = schema.min ?? 0;
            const max = schema.max ?? 100;
            const step = schema.step ?? 1;
            const num = typeof current === 'number' ? current : Number(current ?? min);
            return (
                <div className={styles.sliderRow}>
                    <input
                        type="range"
                        className={styles.slider}
                        min={min}
                        max={max}
                        step={step}
                        value={num}
                        onChange={(e) => {
                            const v = schema.value_type === 'int'
                                ? parseInt(e.target.value, 10)
                                : parseFloat(e.target.value);
                            onChange(schema.key, v);
                        }}
                    />
                    <span className={styles.sliderValue}>{num}</span>
                </div>
            );
        }

        case 'file':
            return (
                <div
                    className={styles.fileDropArea}
                    onDragOver={(e) => e.preventDefault()}
                    onDrop={(e) => {
                        e.preventDefault();
                        const file = e.dataTransfer.files[0];
                        if (file) onChange(schema.key, file.name);
                    }}
                >
                    <span className={styles.fileDropText}>
                        {current ? String(current) : 'ファイルをドロップ or クリック'}
                    </span>
                    <input
                        type="file"
                        className={styles.fileInput}
                        onChange={(e) => {
                            const file = e.target.files?.[0];
                            if (file) onChange(schema.key, file.name);
                        }}
                    />
                </div>
            );

        default:
            return <span className={styles.unsupported}>（未対応の型: {schema.type}）</span>;
    }
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
    const [addingPersona, setAddingPersona] = useState(false);

    const configurableSchemas = addon.params_schema;
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

    const deletePersonaConfig = async (personaId: string) => {
        await fetch(`/api/addon/${addon.addon_name}/config/persona/${personaId}`, {
            method: 'DELETE',
        });
        setPersonaConfigs((prev) => prev.filter((c) => c.persona_id !== personaId));
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
        setAddingPersona(false);
    };

    if (configurableSchemas.length === 0) {
        return <p className={styles.noParams}>設定項目はありません</p>;
    }

    const unconfiguredPersonas = personas.filter(
        (p) => !personaConfigs.find((c) => c.persona_id === p.id)
    );

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
                        {unconfiguredPersonas.length > 0 && (
                            <button
                                className={styles.addPersonaBtn}
                                onClick={() => setAddingPersona((v) => !v)}
                                title="ペルソナを追加"
                            >
                                <Plus size={14} />
                            </button>
                        )}
                    </div>

                    {addingPersona && (
                        <div className={styles.personaPickerList}>
                            {unconfiguredPersonas.map((p) => (
                                <div
                                    key={p.id}
                                    className={styles.personaPickerItem}
                                    onClick={() => addPersonaConfig(p.id, p.name)}
                                >
                                    {p.name}
                                </div>
                            ))}
                        </div>
                    )}

                    {personaConfigs.map((pc) => (
                        <div key={pc.persona_id} className={styles.personaConfigBlock}>
                            <div className={styles.personaConfigHeader}>
                                <span className={styles.personaName}>{pc.persona_name}</span>
                                <button
                                    className={styles.deletePersonaBtn}
                                    onClick={() => deletePersonaConfig(pc.persona_id)}
                                    title="削除（デフォルトに戻す）"
                                >
                                    <Trash2 size={13} />
                                </button>
                            </div>
                            {personaConfigurableSchemas.map((schema) => (
                                <div key={schema.key} className={styles.paramRow}>
                                    <span className={styles.paramLabel}>{schema.label}</span>
                                    <ParamControl
                                        schema={schema}
                                        value={pc.params[schema.key]}
                                        onChange={(key, val) => handlePersonaChange(pc.persona_id, key, val)}
                                    />
                                </div>
                            ))}
                        </div>
                    ))}
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
                    <span className={styles.addonName}>{addon.display_name || addon.addon_name}</span>
                    {addon.version && (
                        <span className={styles.addonVersion}>v{addon.version}</span>
                    )}
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

    useEffect(() => {
        if (!isOpen) return;
        setLoading(true);
        Promise.all([
            fetch('/api/addon/').then((r) => r.json()),
            fetch('/api/people/').then((r) => r.json()),
        ]).then(([addonData, peopleData]) => {
            setAddons(Array.isArray(addonData) ? addonData : []);
            const list = Array.isArray(peopleData)
                ? peopleData.map((p: { id?: string; AIID?: string; name?: string; AINAME?: string }) => ({
                    id: p.id ?? p.AIID ?? '',
                    name: p.name ?? p.AINAME ?? p.id ?? '',
                }))
                : [];
            setPersonas(list);
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
                    {loading && <p className={styles.loadingText}>読み込み中...</p>}
                    {!loading && addons.length === 0 && (
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
