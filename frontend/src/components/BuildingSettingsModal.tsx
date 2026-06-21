import React, { useState, useEffect, useRef } from 'react';
import styles from './BuildingSettingsModal.module.css';
import { X, Save, Loader2 } from 'lucide-react';
import ImageUpload from './common/ImageUpload';

interface Tool {
    TOOLID: number;
    TOOLNAME: string;
    DESCRIPTION: string;
}

interface City {
    CITYID: number;
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
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState<string | null>(null);

    // Form data
    const [name, setName] = useState('');
    const [description, setDescription] = useState('');
    const [capacity, setCapacity] = useState(10);
    const [autoInterval, setAutoInterval] = useState(10);
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
            // Load building data, tools, cities, and prompts in parallel
            const [buildingsRes, toolsRes, citiesRes, promptsRes, linksRes] = await Promise.all([
                fetch('/api/db/tables/building'),
                fetch('/api/db/tables/tool'),
                fetch('/api/db/tables/city'),
                fetch('/api/world/prompts/available'),
                fetch('/api/db/tables/building_tool_link')
            ]);
            if (isStale()) {
                console.warn(
                    `[BuildingSettingsModal] loadData stale (${targetBuildingId} -> ${buildingIdRef.current}); discarding`
                );
                return;
            }

            let buildingApplied = false;
            if (buildingsRes.ok) {
                const buildings = await buildingsRes.json();
                if (isStale()) return;
                const building = buildings.find((b: any) => b.BUILDINGID === targetBuildingId);
                if (building) {
                    setName(building.BUILDINGNAME || '');
                    setDescription(building.DESCRIPTION || '');
                    setCapacity(building.CAPACITY || 10);
                    setAutoInterval(building.AUTO_INTERVAL_SEC || 10);
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

            if (toolsRes.ok) {
                const t = await toolsRes.json();
                if (isStale()) return;
                setTools(t);
            }

            if (citiesRes.ok) {
                const c = await citiesRes.json();
                if (isStale()) return;
                setCities(c);
            }

            if (promptsRes.ok) {
                const p = await promptsRes.json();
                if (isStale()) return;
                setAvailablePrompts(p);
            }

            if (linksRes.ok) {
                const links = await linksRes.json();
                if (isStale()) return;
                const ids = links
                    .filter((l: any) => l.BUILDINGID === targetBuildingId)
                    .map((l: any) => l.TOOLID);
                setToolIds(ids);
            }

            // Load realtime spell bindings + catalog
            try {
                const [spellRes, catalogRes] = await Promise.all([
                    fetch(`/api/world/buildings/${targetBuildingId}/realtime-spell`),
                    fetch('/api/people/realtime-spell-catalog'),
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
            setError('Building データの読み込みに失敗しました');
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
            alert('読み込み中のため保存できません。少し待ってから再度お試しください。');
            return;
        }
        if (!loadedBuildingId || loadedBuildingId !== buildingId) {
            alert(
                `安全のため保存を拒否しました。\n` +
                `表示中のフォームは "${loadedBuildingId ?? '(未読み込み)'}" のもので、\n` +
                `現在の保存先は "${buildingId}" です。\n` +
                `モーダルを一度閉じてから開き直してください。`
            );
            console.error(
                `[BuildingSettingsModal] handleSave rejected: loadedBuildingId=${loadedBuildingId} != buildingId=${buildingId}`
            );
            return;
        }

        setSaving(true);
        setError(null);
        try {
            const res = await fetch(`/api/world/buildings/${buildingId}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    name,
                    description,
                    capacity,
                    auto_interval: autoInterval,
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
                setError(data.detail || '保存に失敗しました');
            }
        } catch (err) {
            setError('Building 設定の保存に失敗しました');
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
                    <h2>Building 設定</h2>
                    <button className={styles.closeBtn} onClick={onClose}>
                        <X size={20} />
                    </button>
                </div>

                {loading ? (
                    <div className={styles.loading}>
                        <Loader2 size={24} className={styles.spinner} />
                        <span>読み込み中...</span>
                    </div>
                ) : (
                    <div className={styles.content}>
                        {error && <div className={styles.error}>{error}</div>}

                        <div className={styles.field}>
                            <label>名前</label>
                            <input
                                type="text"
                                value={name}
                                onChange={e => setName(e.target.value)}
                            />
                        </div>

                        <div className={styles.field}>
                            <label>ID</label>
                            <input
                                type="text"
                                value={buildingId}
                                disabled
                                className={styles.disabled}
                            />
                        </div>

                        <div className={styles.field}>
                            <label>都市</label>
                            <select value={cityId} onChange={e => setCityId(parseInt(e.target.value))}>
                                {cities.map(c => (
                                    <option key={c.CITYID} value={c.CITYID}>{c.DESCRIPTION || c.CITYNAME}</option>
                                ))}
                            </select>
                        </div>

                        <div className={styles.row}>
                            <div className={styles.field}>
                                <label>定員</label>
                                <input
                                    type="number"
                                    value={capacity}
                                    onChange={e => setCapacity(parseInt(e.target.value) || 1)}
                                    min={1}
                                />
                            </div>
                            <div className={styles.field}>
                                <label>自動インターバル（秒）</label>
                                <input
                                    type="number"
                                    value={autoInterval}
                                    onChange={e => setAutoInterval(parseInt(e.target.value) || 10)}
                                    min={1}
                                />
                            </div>
                        </div>

                        <div className={styles.field}>
                            <label>説明</label>
                            <textarea
                                value={description}
                                onChange={e => setDescription(e.target.value)}
                                rows={2}
                            />
                        </div>

                        <div className={styles.field}>
                            <label>システムプロンプト</label>
                            <textarea
                                value={systemInstruction}
                                onChange={e => setSystemInstruction(e.target.value)}
                                rows={6}
                                className={styles.monospace}
                            />
                        </div>

                        <div className={styles.field}>
                            <label>インテリア画像</label>
                            <ImageUpload
                                value={imagePath}
                                onChange={setImagePath}
                            />
                            <small className={styles.hint}>LLMのビジュアルコンテキスト用の Building インテリア画像</small>
                        </div>

                        <div className={styles.field}>
                            <label>追加プロンプトファイル</label>
                            <div className={styles.promptList}>
                                {extraPromptFiles.map((file, idx) => (
                                    <div key={idx} className={styles.promptItem}>
                                        <select
                                            value={file}
                                            onChange={e => handlePromptFileChange(idx, e.target.value)}
                                        >
                                            <option value="">プロンプトファイルを選択...</option>
                                            {availablePrompts.map(p => (
                                                <option key={p} value={p}>{p}</option>
                                            ))}
                                        </select>
                                        <button
                                            type="button"
                                            className={styles.removeBtn}
                                            onClick={() => handleRemovePromptFile(idx)}
                                        >
                                            &times;
                                        </button>
                                    </div>
                                ))}
                                <button
                                    type="button"
                                    className={styles.addBtn}
                                    onClick={handleAddPromptFile}
                                >
                                    + プロンプトファイルを追加
                                </button>
                            </div>
                            <small className={styles.hint}>この Building 内のペルソナ用の追加システムプロンプト</small>
                        </div>

                        <div className={styles.field}>
                            <label>利用可能なツール</label>
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
                            <label>リアルタイムスペル</label>
                            <small className={styles.hint} style={{ display: 'block', marginBottom: '0.5rem' }}>
                                この Building にいるペルソナの会話時に自動実行するスペル
                            </small>
                            {realtimeSpells.length > 0 && (
                                <div style={{ marginBottom: '0.75rem' }}>
                                    {realtimeSpells.map((spell) => (
                                        <div key={spell.binding_id} style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.25rem', padding: '0.4rem 0.6rem', background: 'var(--bg-secondary, #f5f5f5)', borderRadius: '4px' }}>
                                            <span style={{ flex: 1, fontSize: '0.85rem' }}>
                                                <strong>{spell.label || spell.spell_name}</strong>
                                                {spell.spell_args_json && <span style={{ opacity: 0.6, marginLeft: '0.5rem', fontSize: '0.8rem' }}>{spell.spell_args_json}</span>}
                                            </span>
                                            <button
                                                type="button"
                                                style={{ padding: '0.15rem 0.4rem', fontSize: '0.75rem', cursor: 'pointer' }}
                                                onClick={async () => {
                                                    await fetch(`/api/world/buildings/${buildingId}/realtime-spell/${spell.binding_id}`, { method: 'DELETE' });
                                                    setRealtimeSpells(prev => prev.filter(s => s.binding_id !== spell.binding_id));
                                                }}
                                            >
                                                削除
                                            </button>
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
                                        <option value="">スペルを選択...</option>
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
                                    <input
                                        type="text"
                                        placeholder="ラベル (表示名)"
                                        value={newSpellLabel}
                                        onChange={(e) => setNewSpellLabel(e.target.value)}
                                        style={{ flex: 1, padding: '0.25rem 0.5rem', fontSize: '0.85rem' }}
                                    />
                                    <button
                                        type="button"
                                        disabled={!newSpellName}
                                        style={{ padding: '0.3rem 0.75rem', fontSize: '0.85rem', cursor: newSpellName ? 'pointer' : 'not-allowed' }}
                                        onClick={async () => {
                                            if (!newSpellName) return;
                                            const argsObj: Record<string, any> = {};
                                            Object.entries(newSpellArgs).forEach(([k, v]) => { if (v.trim()) argsObj[k] = v.trim(); });
                                            const argsJson = Object.keys(argsObj).length > 0 ? JSON.stringify(argsObj) : null;
                                            const res = await fetch(`/api/world/buildings/${buildingId}/realtime-spell`, {
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
                                    >
                                        追加
                                    </button>
                                </div>
                            </div>
                        </div>

                        <div className={styles.actions}>
                            <button
                                className={styles.saveBtn}
                                onClick={handleSave}
                                disabled={saving || loading || !loadedBuildingId || loadedBuildingId !== buildingId}
                                title={
                                    loading ? '読み込み中…'
                                        : !loadedBuildingId ? '読み込み未完了'
                                        : loadedBuildingId !== buildingId ? `表示中 (${loadedBuildingId}) と保存先 (${buildingId}) が不一致のため無効`
                                        : undefined
                                }
                            >
                                {saving ? (
                                    <>
                                        <Loader2 size={16} className={styles.spinner} />
                                        保存中...
                                    </>
                                ) : (
                                    <>
                                        <Save size={16} />
                                        保存
                                    </>
                                )}
                            </button>
                        </div>
                    </div>
                )}
            </div>
        </div>
    );
}
