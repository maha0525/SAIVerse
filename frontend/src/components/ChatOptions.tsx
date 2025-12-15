import React, { useEffect, useState } from 'react';
import styles from './ChatOptions.module.css';
import { X } from 'lucide-react';

interface ModelInfo {
    id: string;
    name: string;
}

interface PlaybookInfo {
    id: string;
    name: string;
}

interface ParamSpec {
    label: string;
    type: 'slider' | 'number' | 'dropdown' | 'text';
    default: any;
    min?: number;
    max?: number;
    step?: number;
    options?: string[];
    description?: string;
}

interface ChatOptionsProps {
    isOpen: boolean;
    onClose: () => void;
    currentPlaybook: string | null;
    onPlaybookChange: (id: string | null) => void;
}

export default function ChatOptions({ isOpen, onClose, currentPlaybook, onPlaybookChange }: ChatOptionsProps) {
    const [models, setModels] = useState<ModelInfo[]>([]);
    const [playbooks, setPlaybooks] = useState<PlaybookInfo[]>([]);
    const [currentModel, setCurrentModel] = useState<string>('');
    const [params, setParams] = useState<Record<string, any>>({});
    const [paramSpecs, setParamSpecs] = useState<Record<string, ParamSpec>>({});
    const [loading, setLoading] = useState(false);

    useEffect(() => {
        if (isOpen) {
            fetchData();
        }
    }, [isOpen]);

    const fetchData = async () => {
        setLoading(true);
        try {
            const [modelsRes, playbooksRes, configRes] = await Promise.all([
                fetch('/api/config/models'),
                fetch('/api/config/playbooks'),
                fetch('/api/config/config')
            ]);

            if (modelsRes.ok) setModels(await modelsRes.json());
            if (playbooksRes.ok) setPlaybooks(await playbooksRes.json());

            if (configRes.ok) {
                const config = await configRes.json();
                setCurrentModel(config.current_model || '');
                setParamSpecs(config.parameters || {});
                setParams(config.current_values || {});
            }
        } catch (e) {
            console.error("Failed to load config", e);
        } finally {
            setLoading(false);
        }
    };

    const handleModelChange = async (modelId: string) => {
        setCurrentModel(modelId);
        // Save immediately
        try {
            await fetch('/api/config/model', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ model: modelId })
            });
            // Refresh config to get new params
            const res = await fetch('/api/config/config');
            if (res.ok) {
                const config = await res.json();
                setParamSpecs(config.parameters || {});
                setParams(config.current_values || {});
            }
        } catch (e) {
            console.error("Failed to set model", e);
        }
    };

    const handleParamChange = (key: string, value: any) => {
        const newParams = { ...params, [key]: value };
        setParams(newParams);
    };

    const saveParams = async () => {
        try {
            await fetch('/api/config/parameters', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ parameters: params })
            });
            onClose();
        } catch (e) {
            console.error("Failed to save params", e);
        }
    };

    if (!isOpen) return null;

    return (
        <div className={styles.overlay}>
            <div className={styles.modal}>
                <div className={styles.header}>
                    <h2>Chat Options</h2>
                    <button className={styles.closeBtn} onClick={onClose}><X size={24} /></button>
                </div>

                <div className={styles.content}>
                    {loading ? (
                        <div>Loading configuration...</div>
                    ) : (
                        <>
                            <div className={styles.section}>
                                <div className={styles.sectionTitle}>General</div>
                                <div className={styles.formGroup}>
                                    <label>Model</label>
                                    <select
                                        className={styles.select}
                                        value={currentModel}
                                        onChange={(e) => handleModelChange(e.target.value)}
                                    >
                                        <option value="">(Default)</option>
                                        {models.map(m => (
                                            <option key={m.id} value={m.id}>{m.name}</option>
                                        ))}
                                    </select>
                                </div>
                                <div className={styles.formGroup}>
                                    <label>Playbook (Active for next message)</label>
                                    <select
                                        className={styles.select}
                                        value={currentPlaybook || ''}
                                        onChange={(e) => onPlaybookChange(e.target.value || null)}
                                    >
                                        <option value="">(Auto Detect)</option>
                                        {playbooks.map(p => (
                                            <option key={p.id} value={p.id}>{p.name}</option>
                                        ))}
                                    </select>
                                </div>
                            </div>

                            {Object.keys(paramSpecs).length > 0 && (
                                <div className={styles.section}>
                                    <div className={styles.sectionTitle}>Parameters</div>
                                    {Object.entries(paramSpecs).map(([key, spec]) => (
                                        <div key={key} className={styles.formGroup}>
                                            <label>
                                                {spec.label}
                                                <span className={styles.value}>{params[key]}</span>
                                            </label>

                                            {spec.type === 'slider' && (
                                                <input
                                                    type="range"
                                                    className={styles.slider}
                                                    min={spec.min} max={spec.max} step={spec.step}
                                                    value={params[key] ?? spec.default}
                                                    onChange={(e) => handleParamChange(key, parseFloat(e.target.value))}
                                                />
                                            )}

                                            {spec.type === 'number' && (
                                                <input
                                                    type="number"
                                                    className={styles.input}
                                                    min={spec.min} max={spec.max} step={spec.step}
                                                    value={params[key] ?? spec.default}
                                                    onChange={(e) => handleParamChange(key, parseFloat(e.target.value))}
                                                />
                                            )}

                                            {spec.type === 'dropdown' && (
                                                <select
                                                    className={styles.select}
                                                    value={params[key] ?? spec.default}
                                                    onChange={(e) => handleParamChange(key, e.target.value)}
                                                >
                                                    {spec.options?.map(opt => (
                                                        <option key={opt} value={opt}>{opt}</option>
                                                    ))}
                                                </select>
                                            )}
                                        </div>
                                    ))}
                                </div>
                            )}
                        </>
                    )}
                </div>

                <div className={styles.footer}>
                    <button className={styles.cancelBtn} onClick={onClose}>Close</button>
                    <button className={styles.saveBtn} onClick={saveParams}>Apply Settings</button>
                </div>
            </div>
        </div>
    );
}
