import React, { useState, useEffect } from 'react';
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

    useEffect(() => {
        if (isOpen && buildingId) {
            loadData();
        }
    }, [isOpen, buildingId]);

    const loadData = async () => {
        setLoading(true);
        setError(null);
        try {
            // Load building data, tools, cities, and prompts in parallel
            const [buildingsRes, toolsRes, citiesRes, promptsRes, linksRes] = await Promise.all([
                fetch('/api/db/tables/building'),
                fetch('/api/db/tables/tool'),
                fetch('/api/db/tables/city'),
                fetch('/api/world/prompts/available'),
                fetch('/api/db/tables/building_tool_link')
            ]);

            if (buildingsRes.ok) {
                const buildings = await buildingsRes.json();
                const building = buildings.find((b: any) => b.BUILDINGID === buildingId);
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
                        } catch {
                            setExtraPromptFiles([]);
                        }
                    } else {
                        setExtraPromptFiles([]);
                    }
                }
            }

            if (toolsRes.ok) {
                setTools(await toolsRes.json());
            }

            if (citiesRes.ok) {
                setCities(await citiesRes.json());
            }

            if (promptsRes.ok) {
                setAvailablePrompts(await promptsRes.json());
            }

            if (linksRes.ok) {
                const links = await linksRes.json();
                const ids = links
                    .filter((l: any) => l.BUILDINGID === buildingId)
                    .map((l: any) => l.TOOLID);
                setToolIds(ids);
            }

        } catch (err) {
            setError('Failed to load building data');
            console.error(err);
        } finally {
            setLoading(false);
        }
    };

    const handleSave = async () => {
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
                setError(data.detail || 'Failed to save');
            }
        } catch (err) {
            setError('Failed to save building settings');
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
                    <h2>Building Settings</h2>
                    <button className={styles.closeBtn} onClick={onClose}>
                        <X size={20} />
                    </button>
                </div>

                {loading ? (
                    <div className={styles.loading}>
                        <Loader2 size={24} className={styles.spinner} />
                        <span>Loading...</span>
                    </div>
                ) : (
                    <div className={styles.content}>
                        {error && <div className={styles.error}>{error}</div>}

                        <div className={styles.field}>
                            <label>Name</label>
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
                            <label>City</label>
                            <select value={cityId} onChange={e => setCityId(parseInt(e.target.value))}>
                                {cities.map(c => (
                                    <option key={c.CITYID} value={c.CITYID}>{c.CITYNAME}</option>
                                ))}
                            </select>
                        </div>

                        <div className={styles.row}>
                            <div className={styles.field}>
                                <label>Capacity</label>
                                <input
                                    type="number"
                                    value={capacity}
                                    onChange={e => setCapacity(parseInt(e.target.value) || 1)}
                                    min={1}
                                />
                            </div>
                            <div className={styles.field}>
                                <label>Auto Interval (sec)</label>
                                <input
                                    type="number"
                                    value={autoInterval}
                                    onChange={e => setAutoInterval(parseInt(e.target.value) || 10)}
                                    min={1}
                                />
                            </div>
                        </div>

                        <div className={styles.field}>
                            <label>Description</label>
                            <textarea
                                value={description}
                                onChange={e => setDescription(e.target.value)}
                                rows={2}
                            />
                        </div>

                        <div className={styles.field}>
                            <label>System Instruction</label>
                            <textarea
                                value={systemInstruction}
                                onChange={e => setSystemInstruction(e.target.value)}
                                rows={6}
                                className={styles.monospace}
                            />
                        </div>

                        <div className={styles.field}>
                            <label>Interior Image</label>
                            <ImageUpload
                                value={imagePath}
                                onChange={setImagePath}
                            />
                            <small className={styles.hint}>Building interior image for LLM visual context</small>
                        </div>

                        <div className={styles.field}>
                            <label>Extra Prompt Files</label>
                            <div className={styles.promptList}>
                                {extraPromptFiles.map((file, idx) => (
                                    <div key={idx} className={styles.promptItem}>
                                        <select
                                            value={file}
                                            onChange={e => handlePromptFileChange(idx, e.target.value)}
                                        >
                                            <option value="">Select prompt file...</option>
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
                                    + Add Prompt File
                                </button>
                            </div>
                            <small className={styles.hint}>Additional system prompts for personas in this building</small>
                        </div>

                        <div className={styles.field}>
                            <label>Available Tools</label>
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

                        <div className={styles.actions}>
                            <button
                                className={styles.saveBtn}
                                onClick={handleSave}
                                disabled={saving}
                            >
                                {saving ? (
                                    <>
                                        <Loader2 size={16} className={styles.spinner} />
                                        Saving...
                                    </>
                                ) : (
                                    <>
                                        <Save size={16} />
                                        Save Changes
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
