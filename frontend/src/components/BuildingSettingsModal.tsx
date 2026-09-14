
import { apiFetch } from '@/i18n/api';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import React, { useState, useEffect, useRef } from 'react';
import styles from './BuildingSettingsModal.module.css';
import { X, Save, Loader2 } from 'lucide-react';
import ImageUpload from './common/ImageUpload';
import { fetchAllTableRows } from '../lib/dbTable';

interface Tool {
    TOOLID: number;
    TOOLNAME: string;
    DESCRIPTION: string;
}

interface City {
    CITYID: number;
    /** 内部の識別子。表示名が空のときのフォールバック */
    CITY_SLUG: string;
    /** 表示名 */
    CITYNAME: string;
    DESCRIPTION?: string;
}

interface BuildingSettingsModalProps {
    isOpen: boolean;
    onClose: () => void;
    buildingId: string;
    onSaved?: () => void;
}

export default function BuildingSettingsModal({ isOpen, onClose, buildingId, onSaved }: BuildingSettingsModalProps) {
    useLocale();
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // Form data
    const [name, setName] = useState('');
    const [description, setDescription] = useState('');
    const [capacity, setCapacity] = useState(10);
    const [autoInterval, setAutoInterval] = useState(10);
    // 部屋の様子に出すアイテムの個数の上限。null = 設定なし = 既定の 10 個。
    // 0 も有効な値 (アイテムを様子に出さない部屋) — docs/intent/room_item_display_cap.md 設計 4。
    const [itemDisplayLimit, setItemDisplayLimit] = useState<number | null>(null);
    const [systemInstruction, setSystemInstruction] = useState('');
    const [imagePath, setImagePath] = useState('');
    const [extraPromptFiles, setExtraPromptFiles] = useState<string[]>([]);
    const [toolIds, setToolIds] = useState<number[]>([]);
    const [cityId, setCityId] = useState<number>(1);

    // Reference data
    const [tools, setTools] = useState<Tool[]>([]);
    const [cities, setCities] = useState<City[]>([]);
    const [availablePrompts, setAvailablePrompts] = useState<string[]>([]);

    // Realtime spell bindings
    const [realtimeSpells, setRealtimeSpells] = useState<Array<{binding_id: number; spell_name: string; spell_args_json: string | null; label: string | null; enabled: boolean; priority: number}>>([]);
    const [spellCatalog, setSpellCatalog] = useState<Array<{name: string; description: string; parameters: {properties: Record<string, any>; required: string[]}}>>([]);
    const [newSpellName, setNewSpellName] = useState('');
    const [newSpellArgs, setNewSpellArgs] = useState<Record<string, string>>({});
    const [newSpellLabel, setNewSpellLabel] = useState('');

    // 2026-04-30 のエリス上書き事故と同じ脆弱性を持つため、整合性ガードを追加。
    // (feedback_modal_id_integrity.md)
    const [loadedBuildingId, setLoadedBuildingId] = useState<string | null>(null);
    const buildingIdRef = useRef<string>(buildingId);
    buildingIdRef.current = buildingId;

    useEffect(() => {
        if (isOpen && buildingId) {
            setLoadedBuildingId(null);
            loadData();
        }
    }, [isOpen, buildingId]);

    const loadData = async () => {
        setLoading(true);
        setError(null);
        // Race-condition guard: 非同期 fetch 中に buildingId が切り替わったら setter を打ち切る
        const targetBuildingId = buildingIdRef.current;
        const isStale = () => targetBuildingId !== buildingIdRef.current;

        try {
            // Load building data, tools, cities, and prompts in parallel。
            // テーブル系は「この Building を名前で探す」「ツールを全部並べる」の
            // ように全件そろっている前提なので、1 ページ (100 行) では足りない
            const [buildings, tools, cities, promptsRes, links] = await Promise.all([
                fetchAllTableRows<any>('building'),
                fetchAllTableRows<Tool>('tool'),
                fetchAllTableRows<City>('city'),
                apiFetch('/api/world/prompts/available'),
                fetchAllTableRows<any>('building_tool_link')
            ]);
            if (isStale()) {
                console.warn(
                    `[BuildingSettingsModal] loadData stale (${targetBuildingId} -> ${buildingIdRef.current}); discarding`
                );
                return;
            }

            let buildingApplied = false;
            {
                const building = buildings.find((b: any) => b.BUILDINGID === targetBuildingId);
                if (building) {
                    setName(building.BUILDINGNAME || '');
                    setDescription(building.DESCRIPTION || '');
                    setCapacity(building.CAPACITY || 10);
                    setAutoInterval(building.AUTO_INTERVAL_SEC || 10);
                    // 0 も有効な値なので `||` で潰さない (WorldEditor 側と同じ扱い)
                    setItemDisplayLimit(building.ITEM_DISPLAY_LIMIT ?? null);
                    setSystemInstruction(building.SYSTEM_INSTRUCTION || '');
                    setImagePath(building.IMAGE_PATH || '');
                    setCityId(building.CITYID || 1);

                    // Parse extra prompt files
                    if (building.EXTRA_PROMPT_FILES) {
                        try {
                            setExtraPromptFiles(JSON.parse(building.EXTRA_PROMPT_FILES));
                        } catch (e) {
                            console.error('Failed to parse EXTRA_PROMPT_FILES:', e);
                            setExtraPromptFiles([]);
                        }
                    } else {
                        setExtraPromptFiles([]);
                    }
                    buildingApplied = true;
                }
            }

            setTools(tools);
            setCities(cities);

            if (promptsRes.ok) {
                const p = await promptsRes.json();
                if (isStale()) return;
                setAvailablePrompts(p);
            }

            {
                const ids = links
                    .filter((l: any) => l.BUILDINGID === targetBuildingId)
                    .map((l: any) => l.TOOLID);
                setToolIds(ids);
            }

            // Load realtime spell bindings + catalog
            try {
                const [spellRes, catalogRes] = await Promise.all([
                    apiFetch(`/api/world/buildings/${targetBuildingId}/realtime-spell`),
                    apiFetch('/api/people/realtime-spell-catalog'),
                ]);
                if (!isStale()) {
                    if (spellRes.ok) setRealtimeSpells(await spellRes.json());
                    if (catalogRes.ok) setSpellCatalog(await catalogRes.json());
                }
            } catch (e) { /* ignore */ }

            // building レコードが見つかった場合のみロード成功とみなす。
            if (buildingApplied) {
                setLoadedBuildingId(targetBuildingId);
            }

        } catch (err) {
            setError(uiText("components.BuildingSettingsModal.text001"));
            console.error(err);
        } finally {
            if (!isStale()) {
                setLoading(false);
            }
        }
    };

    const handleSave = async () => {
        // 整合性ガード (feedback_modal_id_integrity.md / エリス上書き事故 2026-04-30)
        if (loading) {
            alert(uiText("components.BuildingSettingsModal.text002"));
            return;
        }
        if (!loadedBuildingId || loadedBuildingId !== buildingId) {
            alert(
                uiText("components.BuildingSettingsModal.text003") +
                uiText("components.BuildingSettingsModal.text004", { p1: loadedBuildingId ?? uiText("common.extra004") }) +
                uiText("components.BuildingSettingsModal.text005", { p1: buildingId }) +
                uiText("components.BuildingSettingsModal.text006")
            );
            console.error(
                `[BuildingSettingsModal] handleSave rejected: loadedBuildingId=${loadedBuildingId} != buildingId=${buildingId}`
            );
            return;
        }

        setSaving(true);
        setError(null);
        try {
            const res = await apiFetch(`/api/world/buildings/${buildingId}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name,
                    description,
                    capacity,
                    auto_interval: autoInterval,
                    // null を明示的に送ると「設定なし = 既定の 10 個」に戻る。
                    // このモーダルは値を読み込んで表示しているので、空欄での保存は
                    // ユーザーが見て納得した上での解除になる。
                    item_display_limit: itemDisplayLimit,
                    system_instruction: systemInstruction,
                    image_path: imagePath,
                    extra_prompt_files: extraPromptFiles,
                    tool_ids: toolIds,
                    city_id: cityId
                })
            });

            if (res.ok) {
                if (onSaved) onSaved();
                onClose();
            } else {
                const data = await res.json();
                setError(data.detail || uiText("components.BuildingSettingsModal.text007"));
            }
        } catch (err) {
            setError(uiText("components.BuildingSettingsModal.text008"));
            console.error(err);
        } finally {
            setSaving(false);
        }
    };

    const handleToolToggle = (toolId: number) => {
        if (toolIds.includes(toolId)) {
            setToolIds(toolIds.filter(id => id !== toolId));
        } else {
            setToolIds([...toolIds, toolId]);
        }
    };

    const handleAddPromptFile = () => {
        setExtraPromptFiles([...extraPromptFiles, '']);
    };

    const handleRemovePromptFile = (index: number) => {
        setExtraPromptFiles(extraPromptFiles.filter((_, i) => i !== index));
    };

    const handlePromptFileChange = (index: number, value: string) => {
        const updated = [...extraPromptFiles];
        updated[index] = value;
        setExtraPromptFiles(updated);
    };

    if (!isOpen) return null;

    return (
        <div className={styles.overlay} onClick={onClose}>
            <div className={styles.modal} onClick={e => e.stopPropagation()}>
                <div className={styles.header}>
                    <h2 data-i18n="components.BuildingSettingsModal.text009">{uiText("components.BuildingSettingsModal.text009")}</h2>
                    <button className={styles.closeBtn} onClick={onClose}>
                        <X size={20} />
                    </button>
                </div>

                {loading ? (
                    <div className={styles.loading}>
                        <Loader2 size={24} className={styles.spinner} />
                        <span data-i18n="components.BuildingSettingsModal.text010">{uiText("components.BuildingSettingsModal.text010")}</span>
                    </div>
                ) : (
                    <div className={styles.content}>
                        {error && <div className={styles.error}>{error}</div>}

                        <div className={styles.field}>
                            <label data-i18n="components.BuildingSettingsModal.text011">{uiText("components.BuildingSettingsModal.text011")}</label>
                            <input
                                type="text"
                                value={name}
                                onChange={e => setName(e.target.value)}
                            />
                        </div>

                        <div className={styles.field}>
                            <label>{uiText("components.BuildingSettingsModal.label001")}</label>
                            <input
                                type="text"
                                value={buildingId}
                                disabled
                                className={styles.disabled}
                            />
                        </div>

                        <div className={styles.field}>
                            <label data-i18n="components.BuildingSettingsModal.text012">{uiText("components.BuildingSettingsModal.text012")}</label>
                            <select value={cityId} onChange={e => setCityId(parseInt(e.target.value))}>
                                {cities.map(c => (
                                    <option key={c.CITYID} value={c.CITYID}>{c.CITYNAME || c.CITY_SLUG}</option>
                                ))}
                            </select>
                        </div>

                        <div className={styles.row}>
                            <div className={styles.field}>
                                <label data-i18n="components.BuildingSettingsModal.text013">{uiText("components.BuildingSettingsModal.text013")}</label>
                                <input
                                    type="number"
                                    value={capacity}
                                    onChange={e => setCapacity(parseInt(e.target.value) || 1)}
                                    min={1}
                                />
                            </div>
                            <div className={styles.field}>
                                <label data-i18n="components.BuildingSettingsModal.text014">{uiText("components.BuildingSettingsModal.text014")}</label>
                                <input
                                    type="number"
                                    value={autoInterval}
                                    onChange={e => setAutoInterval(parseInt(e.target.value) || 10)}
                                    min={1}
                                />
                            </div>
                        </div>

                        <div className={styles.field}>
                            <label>部屋の様子に表示するアイテム数（空欄で既定の 10 個）</label>
                            <input
                                type="number"
                                min={0}
                                placeholder="10"
                                value={itemDisplayLimit ?? ''}
                                onChange={e => {
                                    const raw = e.target.value;
                                    const parsed = parseInt(raw, 10);
                                    setItemDisplayLimit(raw === '' || Number.isNaN(parsed) ? null : parsed);
                                }}
                            />
                            <small className={styles.hint}>この数を超えたアイテムは、最近触られていないものから部屋の様子に出なくなります（物は消えません）。0 にするとアイテムを出しません。</small>
                        </div>

                        <div className={styles.field}>
                            <label data-i18n="components.BuildingSettingsModal.text015">{uiText("components.BuildingSettingsModal.text015")}</label>
                            <textarea
                                value={description}
                                onChange={e => setDescription(e.target.value)}
                                rows={2}
                            />
                        </div>

                        <div className={styles.field}>
                            <label data-i18n="components.BuildingSettingsModal.text016">{uiText("components.BuildingSettingsModal.text016")}</label>
                            <textarea
                                value={systemInstruction}
                                onChange={e => setSystemInstruction(e.target.value)}
                                rows={6}
                                className={styles.monospace}
                            />
                        </div>

                        <div className={styles.field}>
                            <label data-i18n="components.BuildingSettingsModal.text017">{uiText("components.BuildingSettingsModal.text017")}</label>
                            <ImageUpload
                                value={imagePath}
                                onChange={setImagePath}
                            />
                            <small data-i18n="components.BuildingSettingsModal.text018" className={styles.hint}>{uiText("components.BuildingSettingsModal.text018")}</small>
                        </div>

                        <div className={styles.field}>
                            <label data-i18n="components.BuildingSettingsModal.text019">{uiText("components.BuildingSettingsModal.text019")}</label>
                            <div className={styles.promptList}>
                                {extraPromptFiles.map((file, idx) => (
                                    <div key={idx} className={styles.promptItem}>
                                        <select
                                            value={file}
                                            onChange={e => handlePromptFileChange(idx, e.target.value)}
                                        >
                                            <option data-i18n="components.BuildingSettingsModal.text020" value="">{uiText("components.BuildingSettingsModal.text020")}</option>
                                            {availablePrompts.map(p => (
                                                <option key={p} value={p}>{p}</option>
                                            ))}
                                        </select>
                                        <button
                                            type="button"
                                            className={styles.removeBtn}
                                            onClick={() => handleRemovePromptFile(idx)}
                                        >
                                            {uiText("components.BuildingSettingsModal.label002")}</button>
                                    </div>
                                ))}
                                <button data-i18n="components.BuildingSettingsModal.text021"
                                    type="button"
                                    className={styles.addBtn}
                                    onClick={handleAddPromptFile}
                                >{uiText("components.BuildingSettingsModal.text021")}</button>
                            </div>
                            <small data-i18n="components.BuildingSettingsModal.text022" className={styles.hint}>{uiText("components.BuildingSettingsModal.text022")}</small>
                        </div>

                        <div className={styles.field}>
                            <label data-i18n="components.BuildingSettingsModal.text023">{uiText("components.BuildingSettingsModal.text023")}</label>
                            <div className={styles.toolGrid}>
                                {tools.map(t => (
                                    <label key={t.TOOLID} className={styles.toolItem}>
                                        <input
                                            type="checkbox"
                                            checked={toolIds.includes(t.TOOLID)}
                                            onChange={() => handleToolToggle(t.TOOLID)}
                                        />
                                        <span>{t.TOOLNAME}</span>
                                    </label>
                                ))}
                            </div>
                        </div>

                        <div className={styles.field}>
                            <label data-i18n="components.BuildingSettingsModal.text024">{uiText("components.BuildingSettingsModal.text024")}</label>
                            <small data-i18n="components.BuildingSettingsModal.text025" className={styles.hint} style={{ display: 'block', marginBottom: '0.5rem' }}>{uiText("components.BuildingSettingsModal.text025")}</small>
                            {realtimeSpells.length > 0 && (
                                <div style={{ marginBottom: '0.75rem' }}>
                                    {realtimeSpells.map((spell) => (
                                        <div key={spell.binding_id} style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.25rem', padding: '0.4rem 0.6rem', background: 'var(--bg-secondary, #f5f5f5)', borderRadius: '4px' }}>
                                            <span style={{ flex: 1, fontSize: '0.85rem' }}>
                                                <strong>{spell.label || spell.spell_name}</strong>
                                                {spell.spell_args_json && <span style={{ opacity: 0.6, marginLeft: '0.5rem', fontSize: '0.8rem' }}>{spell.spell_args_json}</span>}
                                            </span>
                                            <button data-i18n="components.BuildingSettingsModal.text026"
                                                type="button"
                                                style={{ padding: '0.15rem 0.4rem', fontSize: '0.75rem', cursor: 'pointer' }}
                                                onClick={async () => {
                                                    await apiFetch(`/api/world/buildings/${buildingId}/realtime-spell/${spell.binding_id}`, { method: 'DELETE' });
                                                    setRealtimeSpells(prev => prev.filter(s => s.binding_id !== spell.binding_id));
                                                }}
                                            >{uiText("components.BuildingSettingsModal.text026")}</button>
                                        </div>
                                    ))}
                                </div>
                            )}
                            <div style={{ border: '1px solid var(--border-color, #ddd)', borderRadius: '6px', padding: '0.75rem' }}>
                                <div style={{ marginBottom: '0.5rem' }}>
                                    <select
                                        value={newSpellName}
                                        onChange={(e) => { setNewSpellName(e.target.value); setNewSpellArgs({}); }}
                                        style={{ width: '100%', padding: '0.3rem 0.5rem', fontSize: '0.85rem' }}
                                    >
                                        <option data-i18n="components.BuildingSettingsModal.text027" value="">{uiText("components.BuildingSettingsModal.text027")}</option>
                                        {spellCatalog.map(s => (
                                            <option key={s.name} value={s.name}>{s.name} — {s.description.slice(0, 60)}</option>
                                        ))}
                                    </select>
                                </div>
                                {newSpellName && (() => {
                                    const selected = spellCatalog.find(s => s.name === newSpellName);
                                    if (!selected) return null;
                                    const props = selected.parameters.properties;
                                    const required = selected.parameters.required || [];
                                    return (
                                        <div style={{ marginBottom: '0.5rem' }}>
                                            {Object.entries(props).map(([key, spec]: [string, any]) => (
                                                <div key={key} style={{ marginBottom: '0.35rem' }}>
                                                    <label style={{ fontSize: '0.8rem', display: 'block', marginBottom: '0.1rem' }}>
                                                        {key}{required.includes(key) ? ' *' : ''}
                                                        {spec.description && <span style={{ opacity: 0.6, marginLeft: '0.5rem' }}>{spec.description.slice(0, 50)}</span>}
                                                    </label>
                                                    <input
                                                        type="text"
                                                        value={newSpellArgs[key] || ''}
                                                        onChange={(e) => setNewSpellArgs(prev => ({ ...prev, [key]: e.target.value }))}
                                                        placeholder={spec.default != null ? `default: ${spec.default}` : ''}
                                                        style={{ width: '100%', padding: '0.25rem 0.5rem', fontSize: '0.85rem' }}
                                                    />
                                                </div>
                                            ))}
                                        </div>
                                    );
                                })()}
                                <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
                                    <input data-i18n="components.BuildingSettingsModal.text028"
                                        type="text"
                                        placeholder={uiText("components.BuildingSettingsModal.text028")}
                                        value={newSpellLabel}
                                        onChange={(e) => setNewSpellLabel(e.target.value)}
                                        style={{ flex: 1, padding: '0.25rem 0.5rem', fontSize: '0.85rem' }}
                                    />
                                    <button data-i18n="components.BuildingSettingsModal.text029"
                                        type="button"
                                        disabled={!newSpellName}
                                        style={{ padding: '0.3rem 0.75rem', fontSize: '0.85rem', cursor: newSpellName ? 'pointer' : 'not-allowed' }}
                                        onClick={async () => {
                                            if (!newSpellName) return;
                                            const argsObj: Record<string, any> = {};
                                            Object.entries(newSpellArgs).forEach(([k, v]) => {
                                                if (!v.trim()) return;
                                                try { argsObj[k] = JSON.parse(v.trim()); } catch { argsObj[k] = v.trim(); }
                                            });
                                            const argsJson = Object.keys(argsObj).length > 0 ? JSON.stringify(argsObj) : null;
                                            const res = await apiFetch(`/api/world/buildings/${buildingId}/realtime-spell`, {
                                                method: 'POST',
                                                headers: { 'Content-Type': 'application/json' },
                                                body: JSON.stringify({
                                                    spell_name: newSpellName,
                                                    spell_args_json: argsJson,
                                                    label: newSpellLabel.trim() || null,
                                                }),
                                            });
                                            if (res.ok) {
                                                const data = await res.json();
                                                setRealtimeSpells(prev => [...prev, {
                                                    binding_id: data.binding_id,
                                                    spell_name: newSpellName,
                                                    spell_args_json: argsJson,
                                                    label: newSpellLabel.trim() || null,
                                                    enabled: true,
                                                    priority: 0,
                                                }]);
                                                setNewSpellName('');
                                                setNewSpellArgs({});
                                                setNewSpellLabel('');
                                            }
                                        }}
                                    >{uiText("components.BuildingSettingsModal.text029")}</button>
                                </div>
                            </div>
                        </div>

                        <div className={styles.actions}>
                            <button data-i18n="components.BuildingSettingsModal.text030 components.BuildingSettingsModal.text031 components.BuildingSettingsModal.text032 components.BuildingSettingsModal.text033 components.BuildingSettingsModal.text034"
                                className={styles.saveBtn}
                                onClick={handleSave}
                                disabled={saving || loading || !loadedBuildingId || loadedBuildingId !== buildingId}
                                title={
                                    loading ? uiText("components.BuildingSettingsModal.text030")
                                        : !loadedBuildingId ? uiText("components.BuildingSettingsModal.text031")
                                        : loadedBuildingId !== buildingId ? uiText("components.BuildingSettingsModal.text032", { p1: loadedBuildingId, p2: buildingId })
                                        : undefined
                                }
                            >
                                {saving ? (
                                    <>
                                        <Loader2 size={16} className={styles.spinner} />{uiText("components.BuildingSettingsModal.text033")}</>
                                ) : (
                                    <>
                                        <Save size={16} />{uiText("components.BuildingSettingsModal.text034")}</>
                                )}
                            </button>
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}
