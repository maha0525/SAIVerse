import React, { useEffect, useState } from 'react';
import styles from './ChatOptions.module.css';
import { X } from 'lucide-react';

interface ModelInfo {
    id: string;
    name: string;
}

interface PlaybookParamOption {
    value: string;
    label: string;
}

interface PlaybookParam {
    name: string;
    description: string;
    param_type: string;
    required: boolean;
    default: any;
    enum_values?: string[];
    enum_source?: string;
    user_configurable: boolean;
    ui_widget?: string;
    resolved_options?: PlaybookParamOption[];
}

interface PlaybookInfo {
    id: string;
    name: string;
    description?: string;
    input_schema?: PlaybookParam[];
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
    playbookParams: Record<string, any>;
    onPlaybookParamsChange: (params: Record<string, any>) => void;
}

export default function ChatOptions({ isOpen, onClose, currentPlaybook, onPlaybookChange, playbookParams, onPlaybookParamsChange }: ChatOptionsProps) {
    const [models, setModels] = useState<ModelInfo[]>([]);
    const [playbooks, setPlaybooks] = useState<PlaybookInfo[]>([]);
    const [currentModel, setCurrentModel] = useState<string>('');
    const [params, setParams] = useState<Record<string, any>>({});
    const [paramSpecs, setParamSpecs] = useState<Record<string, ParamSpec>>({});
    const [loading, setLoading] = useState(false);
    const [playbookParamSpecs, setPlaybookParamSpecs] = useState<PlaybookParam[]>([]);

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

            // If a playbook is already selected, fetch its parameters
            if (currentPlaybook) {
                const paramsRes = await fetch(`/api/config/playbooks/${encodeURIComponent(currentPlaybook)}/params`);
                if (paramsRes.ok) {
                    const data = await paramsRes.json();
                    setPlaybookParamSpecs(data.params || []);
                }
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
            const res = await fetch('/api/config/model', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ model: modelId })
            });
            if (res.ok) {
                // Use inline parameters from response (no separate fetch needed)
                const data = await res.json();
                setParamSpecs(data.parameters || {});
                setParams(data.current_values || {});
            }
        } catch (e) {
            console.error("Failed to set model", e);
        }
    };

    const handleParamChange = (key: string, value: any) => {
        const newParams = { ...params, [key]: value };
        setParams(newParams);
    };

    const handlePlaybookChange = async (playbookId: string | null) => {
        onPlaybookChange(playbookId);
        // Reset playbook params when changing playbook
        onPlaybookParamsChange({});
        setPlaybookParamSpecs([]);

        // Save to server immediately (include empty params to reset server-side)
        try {
            await fetch('/api/config/playbook', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ playbook: playbookId, playbook_params: {} })
            });
        } catch (e) {
            console.error("Failed to save playbook", e);
        }

        // Fetch playbook params if a playbook is selected
        if (playbookId) {
            try {
                const res = await fetch(`/api/config/playbooks/${encodeURIComponent(playbookId)}/params`);
                if (res.ok) {
                    const data = await res.json();
                    setPlaybookParamSpecs(data.params || []);
                }
            } catch (e) {
                console.error("Failed to fetch playbook params", e);
            }
        }
    };

    const handlePlaybookParamChange = async (paramName: string, value: any) => {
        const newParams = { ...playbookParams, [paramName]: value };
        onPlaybookParamsChange(newParams);

        // Save to server immediately
        try {
            await fetch('/api/config/playbook', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    playbook: currentPlaybook,
                    playbook_params: newParams
                })
            });
        } catch (e) {
            console.error("Failed to save playbook params", e);
        }
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
                                        onChange={(e) => handlePlaybookChange(e.target.value || null)}
                                    >
                                        <option value="">(Auto Detect)</option>
                                        {playbooks.map(p => (
                                            <option key={p.id} value={p.id}>{p.name}</option>
                                        ))}
                                    </select>
                                </div>
                            </div>

                            {playbookParamSpecs.length > 0 && (
                                <div className={styles.section}>
                                    <div className={styles.sectionTitle}>Playbook Parameters</div>
                                    {playbookParamSpecs.map(param => (
                                        <div key={param.name} className={styles.formGroup}>
                                            <label>
                                                {param.description || param.name}
                                                {!param.required && <span className={styles.optional}> (optional)</span>}
                                            </label>

                                            {param.resolved_options && param.resolved_options.length > 0 ? (
                                                <select
                                                    className={styles.select}
                                                    value={playbookParams[param.name] ?? param.default ?? ''}
                                                    onChange={(e) => handlePlaybookParamChange(param.name, e.target.value || null)}
                                                >
                                                    <option value="">{param.required ? '(Select...)' : '(Auto)'}</option>
                                                    {param.resolved_options.map(opt => (
                                                        <option key={opt.value} value={opt.value}>{opt.label}</option>
                                                    ))}
                                                </select>
                                            ) : param.param_type === 'boolean' ? (
                                                <input
                                                    type="checkbox"
                                                    checked={playbookParams[param.name] ?? param.default ?? false}
                                                    onChange={(e) => handlePlaybookParamChange(param.name, e.target.checked)}
                                                />
                                            ) : param.param_type === 'number' ? (
                                                <input
                                                    type="number"
                                                    className={styles.input}
                                                    value={playbookParams[param.name] ?? param.default ?? ''}
                                                    onChange={(e) => handlePlaybookParamChange(param.name, parseFloat(e.target.value))}
                                                />
                                            ) : (
                                                <input
                                                    type="text"
                                                    className={styles.input}
                                                    value={playbookParams[param.name] ?? param.default ?? ''}
                                                    onChange={(e) => handlePlaybookParamChange(param.name, e.target.value)}
                                                    placeholder={param.description}
                                                />
                                            )}
                                        </div>
                                    ))}
                                </div>
                            )}

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
