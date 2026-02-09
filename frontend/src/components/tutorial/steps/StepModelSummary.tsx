"use client";

import React, { useState, useEffect } from 'react';
import { CheckCircle, AlertTriangle, ChevronDown } from 'lucide-react';
import styles from './Steps.module.css';

interface ModelRoleAssignment {
    role: string;
    label: string;
    description: string;
    env_key: string;
    model_id: string;
    display_name: string;
}

interface ModelInfo {
    id: string;
    display_name: string;
    provider: string;
    is_available: boolean;
}

interface StepModelSummaryProps {
    provider: string;
    assignments: ModelRoleAssignment[];
    warnings: string[];
    availableModels: ModelInfo[];
    onOverride: (role: string, envKey: string, modelId: string) => void;
    /** When true, loads current settings from API instead of showing auto-config results */
    editMode?: boolean;
}

interface ModelRoleInfo {
    env_key: string;
    value: string;
    display_name: string;
    label: string;
    description: string;
}

interface PresetInfo {
    provider: string;
    display_name: string;
    is_available: boolean;
}

export default function StepModelSummary({
    provider,
    assignments,
    warnings,
    availableModels,
    onOverride,
    editMode = false,
}: StepModelSummaryProps) {
    const [expandedRole, setExpandedRole] = useState<string | null>(null);
    const [currentRoles, setCurrentRoles] = useState<Record<string, ModelRoleInfo> | null>(null);
    const [presets, setPresets] = useState<PresetInfo[]>([]);

    // In edit mode, fetch current model roles from API
    useEffect(() => {
        if (editMode) {
            loadModelRoles();
        }
    }, [editMode]);

    const loadModelRoles = async () => {
        try {
            const res = await fetch('/api/tutorial/model-roles');
            if (res.ok) {
                const data = await res.json();
                setCurrentRoles(data.current);
                setPresets(data.presets);
            }
        } catch (e) {
            console.error('Failed to load model roles', e);
        }
    };

    const handlePresetChange = async (presetProvider: string) => {
        try {
            const res = await fetch('/api/tutorial/auto-configure-models', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ provider: presetProvider }),
            });
            if (res.ok) {
                await loadModelRoles();
            }
        } catch (e) {
            console.error('Failed to apply preset', e);
        }
    };

    const handleModelSelect = (role: string, envKey: string, modelId: string) => {
        onOverride(role, envKey, modelId);
        setExpandedRole(null);
        // Refresh in edit mode
        if (editMode) {
            loadModelRoles();
        }
    };

    // Determine display data source
    const displayAssignments = editMode && currentRoles
        ? Object.entries(currentRoles).map(([role, info]) => ({
            role,
            label: info.label,
            description: info.description,
            env_key: info.env_key,
            model_id: info.value,
            display_name: info.display_name || info.value || '(未設定)',
        }))
        : assignments;

    const displayProvider = editMode ? '' : provider;

    return (
        <div className={styles.modelSummaryContainer}>
            {!editMode && (
                <>
                    <div className={styles.summaryHeader}>
                        <CheckCircle size={24} className={styles.summaryIcon} />
                        <h3 className={styles.title}>モデルの自動設定が完了しました</h3>
                    </div>
                    {displayProvider && (
                        <p className={styles.summaryProvider}>
                            <strong>{displayProvider}</strong> のモデルプリセットが適用されました
                        </p>
                    )}
                </>
            )}

            {editMode && presets.length > 0 && (
                <div className={styles.presetSelector}>
                    <label className={styles.presetLabel}>プリセット切替:</label>
                    <div className={styles.presetButtons}>
                        {presets.filter(p => p.is_available).map((preset) => (
                            <button
                                key={preset.provider}
                                className={styles.presetButton}
                                onClick={() => handlePresetChange(preset.provider)}
                            >
                                {preset.display_name}
                            </button>
                        ))}
                    </div>
                </div>
            )}

            <div className={styles.roleList}>
                {displayAssignments.map((assignment) => (
                    <div key={assignment.role} className={styles.roleItem}>
                        <div className={styles.roleHeader}>
                            <div className={styles.roleInfo}>
                                <span className={styles.roleLabel}>{assignment.label}</span>
                                <span className={styles.roleDescription}>{assignment.description}</span>
                            </div>
                            <div className={styles.roleModel}>
                                <span className={styles.roleModelName}>{assignment.display_name}</span>
                                <button
                                    className={styles.roleChangeButton}
                                    onClick={() => setExpandedRole(
                                        expandedRole === assignment.role ? null : assignment.role
                                    )}
                                >
                                    <ChevronDown size={14} />
                                    <span>変更</span>
                                </button>
                            </div>
                        </div>
                        {expandedRole === assignment.role && (
                            <div className={styles.roleDropdown}>
                                {availableModels
                                    .filter(m => m.is_available)
                                    .map(model => (
                                        <div
                                            key={model.id}
                                            className={`${styles.roleDropdownItem} ${model.id === assignment.model_id ? styles.roleDropdownSelected : ''}`}
                                            onClick={() => handleModelSelect(assignment.role, assignment.env_key, model.id)}
                                        >
                                            <span className={styles.roleDropdownName}>{model.display_name}</span>
                                            <span className={styles.roleDropdownProvider}>{model.provider}</span>
                                        </div>
                                    ))
                                }
                            </div>
                        )}
                    </div>
                ))}
            </div>

            {warnings.length > 0 && (
                <div className={styles.warningBox}>
                    <AlertTriangle size={16} />
                    <div>
                        {warnings.map((w, i) => (
                            <p key={i}>{w}</p>
                        ))}
                    </div>
                </div>
            )}

            {!editMode && (
                <p className={styles.summaryFooter}>
                    これらの設定は後からサイドバーの設定画面で変更できます。
                </p>
            )}
        </div>
    );
}
