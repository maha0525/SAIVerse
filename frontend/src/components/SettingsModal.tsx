import React, { useState, useEffect } from 'react';
import { X, Save, Loader2, Settings } from 'lucide-react';
import styles from './SettingsModal.module.css';
import ImageUpload from './common/ImageUpload';
import ModalOverlay from './common/ModalOverlay';

interface SettingsModalProps {
    isOpen: boolean;
    onClose: () => void;
    personaId: string;
}

interface AIConfig {
    name: string;
    description: string;
    system_prompt: string;
    default_model: string | null;
    lightweight_model: string | null;
    interaction_mode: string;
    avatar_path: string | null;
    appearance_image_path: string | null;  // Visual context appearance image
}

interface ModelChoice {
    id: string;
    name: string;
}

const INTERACTION_MODES = [
    { value: 'auto', label: '🟢 Auto - Speaks autonomously' },
    { value: 'manual', label: '🟡 Manual - Only responds to user' },
    { value: 'sleep', label: '🔴 Sleep - Currently inactive' },
];

interface AutonomousStatus {
    interaction_mode: string;
    system_running: boolean;
    is_active: boolean;
}

export default function SettingsModal({ isOpen, onClose, personaId }: SettingsModalProps) {
    const [config, setConfig] = useState<AIConfig | null>(null);
    const [availableModels, setAvailableModels] = useState<ModelChoice[]>([]);
    const [isLoading, setIsLoading] = useState(false);
    const [isSaving, setIsSaving] = useState(false);
    const [autonomousStatus, setAutonomousStatus] = useState<AutonomousStatus | null>(null);

    // Form state
    const [description, setDescription] = useState('');
    const [systemPrompt, setSystemPrompt] = useState('');
    const [defaultModel, setDefaultModel] = useState<string>('');
    const [lightweightModel, setLightweightModel] = useState<string>('');
    const [interactionMode, setInteractionMode] = useState<string>('auto');
    const [avatarPath, setAvatarPath] = useState('');
    const [appearanceImagePath, setAppearanceImagePath] = useState('');

    useEffect(() => {
        if (isOpen) {
            loadModels();
        }
    }, [isOpen]);

    useEffect(() => {
        if (isOpen && personaId) {
            loadConfig();
        }
    }, [isOpen, personaId, availableModels]); // dependent on availableModels to safely set default

    const loadModels = async () => {
        try {
            const res = await fetch('/api/info/models');
            if (res.ok) {
                const data = await res.json();
                setAvailableModels(data);
            }
        } catch (e) {
            console.error("Failed to load models", e);
        }
    };

    const loadConfig = async () => {
        setIsLoading(true);
        try {
            const res = await fetch(`/api/people/${personaId}/config`);
            if (res.ok) {
                const data = await res.json();
                setConfig(data);
                setDescription(data.description);
                setSystemPrompt(data.system_prompt);
                setDefaultModel(data.default_model || '');
                setLightweightModel(data.lightweight_model || '');
                setInteractionMode(data.interaction_mode || 'auto');
                setAvatarPath(data.avatar_path || '');
                setAppearanceImagePath(data.appearance_image_path || '');
            } else {
                console.error("Failed to load config");
            }

            // Also load autonomous status
            const statusRes = await fetch(`/api/people/${personaId}/autonomous/status`);
            if (statusRes.ok) {
                const statusData = await statusRes.json();
                setAutonomousStatus(statusData);
            }
        } catch (error) {
            console.error(error);
        } finally {
            setIsLoading(false);
        }
    };


    const handleSave = async () => {
        setIsSaving(true);
        try {
            const res = await fetch(`/api/people/${personaId}/config`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    description: description,
                    system_prompt: systemPrompt,
                    default_model: defaultModel,  // empty string = clear to None
                    lightweight_model: lightweightModel,  // empty string = clear to None
                    interaction_mode: interactionMode,
                    avatar_path: avatarPath || null,
                    appearance_image_path: appearanceImagePath || null
                })
            });

            if (res.ok) {
                const data = await res.json();
                // Close on success
                onClose();
            } else {
                const err = await res.json();
                alert(`Failed to save: ${err.detail}`);
            }
        } catch (error) {
            console.error(error);
            alert("Error saving config");
        } finally {
            setIsSaving(false);
        }
    };

    if (!isOpen) return null;

    return (
        <ModalOverlay onClose={onClose} className={styles.overlay}>
            <div className={styles.modal} onClick={e => e.stopPropagation()}>
                <div className={styles.header}>
                    <h2><Settings size={22} /> Persona Settings</h2>
                    <button className={styles.closeBtn} onClick={onClose}><X size={20} /></button>
                </div>

                <div className={styles.content}>
                    {isLoading ? (
                        <div style={{ display: 'flex', justifyContent: 'center', padding: '2rem' }}>
                            <Loader2 className="spin" size={32} />
                        </div>
                    ) : (
                        <>
                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>AI Name</label>
                                <div className={styles.input} style={{ background: 'rgba(0,0,0,0.05)', color: '#888' }}>
                                    {config?.name}
                                </div>
                                <div className={styles.description}>Name cannot be changed here.</div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>Default Model</label>
                                <select
                                    className={styles.select}
                                    value={defaultModel}
                                    onChange={(e) => setDefaultModel(e.target.value)}
                                >
                                    <option value="">Use System Default</option>
                                    {defaultModel && !availableModels.some(m => m.id === defaultModel) && (
                                        <option value={defaultModel}>⚠️ Unknown: {defaultModel}</option>
                                    )}
                                    {availableModels.map(m => (
                                        <option key={m.id} value={m.id}>{m.name}</option>
                                    ))}
                                </select>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>Lightweight Model (Optional)</label>
                                <select
                                    className={styles.select}
                                    value={lightweightModel}
                                    onChange={(e) => setLightweightModel(e.target.value)}
                                >
                                    <option value="">None (Use Default)</option>
                                    {lightweightModel && !availableModels.some(m => m.id === lightweightModel) && (
                                        <option value={lightweightModel}>⚠️ Unknown: {lightweightModel}</option>
                                    )}
                                    {availableModels.map(m => (
                                        <option key={m.id} value={m.id}>{m.name}</option>
                                    ))}
                                </select>
                                <div className={styles.description}>Used for faster/cheaper responses if applicable.</div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>Interaction Mode</label>
                                <select
                                    className={styles.select}
                                    value={interactionMode}
                                    onChange={(e) => setInteractionMode(e.target.value)}
                                >
                                    {INTERACTION_MODES.map(m => (
                                        <option key={m.value} value={m.value}>{m.label}</option>
                                    ))}
                                </select>
                                {autonomousStatus && (
                                    <div className={styles.description} style={{
                                        marginTop: '0.5rem',
                                        padding: '0.5rem',
                                        background: autonomousStatus.is_active ? 'rgba(0, 200, 0, 0.1)' : 'rgba(100, 100, 100, 0.1)',
                                        borderRadius: '4px'
                                    }}>
                                        {autonomousStatus.is_active ? (
                                            <span>✅ <strong>Autonomous mode active</strong> - This persona will speak on its own.</span>
                                        ) : autonomousStatus.system_running ? (
                                            <span>⏸️ Autonomous system is running, but this persona is in {interactionMode} mode.</span>
                                        ) : (
                                            <span>⚠️ Autonomous system is not running.</span>
                                        )}
                                    </div>
                                )}
                            </div>

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>Avatar</label>
                                <ImageUpload
                                    value={avatarPath}
                                    onChange={setAvatarPath}
                                    circle={true}
                                />
                                <div className={styles.description}>
                                    Upload a new avatar image.
                                </div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>Appearance Image (Visual Context)</label>
                                <ImageUpload
                                    value={appearanceImagePath}
                                    onChange={setAppearanceImagePath}
                                />
                                <div className={styles.description}>
                                    Detailed appearance image for LLM visual context. Separate from avatar.
                                </div>
                            </div>

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>Description</label>
                                <input
                                    className={styles.input}
                                    value={description}
                                    onChange={(e) => setDescription(e.target.value)}
                                    placeholder="Short description of the persona"
                                />
                            </div>

                            <div className={styles.fieldGroup}>
                                <label className={styles.label}>System Instructions</label>
                                <textarea
                                    className={styles.textarea}
                                    value={systemPrompt}
                                    onChange={(e) => setSystemPrompt(e.target.value)}
                                    placeholder="You are..."
                                />
                                <div className={styles.description}>
                                    Core instructions defining behavior, personality, and capabilities.
                                </div>
                            </div>
                        </>
                    )}
                </div>

                <div className={styles.footer}>
                    <button className={styles.cancelBtn} onClick={onClose}>Cancel</button>
                    <button
                        className={styles.saveBtn}
                        onClick={handleSave}
                        disabled={isLoading || isSaving}
                    >
                        {isSaving ? <Loader2 size={16} className="spin" /> : <Save size={16} />}
                        Save Changes
                    </button>
                </div>
            </div>
        </ModalOverlay>
    );
}
