"use client";

import React, { useState, useEffect } from 'react';
import { X, Loader2, ArrowRight, ArrowLeft } from 'lucide-react';
import styles from './TutorialWizard.module.css';
import ModalOverlay from '../common/ModalOverlay';

// Step Components
import StepWelcome from './steps/StepWelcome';
import StepUserName from './steps/StepUserName';
import StepCityName from './steps/StepCityName';
import StepPersonaChoice from './steps/StepPersonaChoice';
import StepApiKeys from './steps/StepApiKeys';
import StepModelSummary from './steps/StepModelSummary';
import StepChronicle from './steps/StepChronicle';
import StepComplete from './steps/StepComplete';

interface TutorialWizardProps {
    isOpen: boolean;
    onClose: () => void;
    onComplete?: (roomId?: string) => void;
    startAtStep?: number;
}

type Step = 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8;

interface ApiKeyStatus {
    provider: string;
    env_key: string;
    is_set: boolean;
    display_name: string;
    description: string;
    free_label?: string;
    free_note?: string;
}

interface ModelInfo {
    id: string;
    display_name: string;
    provider: string;
    is_available: boolean;
}

interface ModelRoleAssignment {
    role: string;
    label: string;
    description: string;
    env_key: string;
    model_id: string;
    display_name: string;
}

interface TutorialState {
    userName: string;
    cityName: string;
    personaChoice: 'new' | 'import' | null;
    apiKeys: Record<string, string>;
    createdPersonaId: string | null;
    createdRoomId: string | null;
    autoConfiguredProvider: string;
    autoConfiguredAssignments: ModelRoleAssignment[];
    autoConfigureWarnings: string[];
    chronicleEnabled: boolean;
}

const STEP_TITLES = [
    'Welcome',
    'ユーザー名',
    'City名',
    'ペルソナ',
    'APIキー',
    'モデル設定',
    'Chronicle',
    '完了'
];

// Provider to env key mapping
const PROVIDER_ENV_MAPPING: Record<string, string> = {
    'openai': 'OPENAI_API_KEY',
    'gemini_free': 'GEMINI_FREE_API_KEY',
    'gemini': 'GEMINI_API_KEY',
    'anthropic': 'CLAUDE_API_KEY',
    'grok': 'XAI_API_KEY',
    'openrouter': 'OPENROUTER_API_KEY',
    'nvidia': 'NVIDIA_API_KEY'
};

export default function TutorialWizard({
    isOpen,
    onClose,
    onComplete,
    startAtStep = 1
}: TutorialWizardProps) {
    const [step, setStep] = useState<Step>(startAtStep as Step);
    const [isLoading, setIsLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const [state, setState] = useState<TutorialState>({
        userName: '',
        cityName: '',
        personaChoice: null,
        apiKeys: {},
        createdPersonaId: null,
        createdRoomId: null,
        autoConfiguredProvider: '',
        autoConfiguredAssignments: [],
        autoConfigureWarnings: [],
        chronicleEnabled: false,
    });

    const [availableModels, setAvailableModels] = useState<ModelInfo[]>([]);
    const [apiKeyStatus, setApiKeyStatus] = useState<ApiKeyStatus[]>([]);

    // Reset on open
    useEffect(() => {
        if (isOpen) {
            setStep(startAtStep as Step);
            setError(null);
            setState({
                userName: '',
                cityName: '',
                personaChoice: null,
                apiKeys: {},
                createdPersonaId: null,
                createdRoomId: null,
                autoConfiguredProvider: '',
                autoConfiguredAssignments: [],
                autoConfigureWarnings: [],
                chronicleEnabled: false,
            });
            loadInitialData();
        }
    }, [isOpen, startAtStep]);

    const loadInitialData = async () => {
        try {
            const [modelsRes, keysRes, userRes, citiesRes] = await Promise.all([
                fetch('/api/tutorial/available-models'),
                fetch('/api/tutorial/api-keys/status'),
                fetch('/api/user/status'),
                fetch('/api/db/tables/city'),
            ]);

            if (modelsRes.ok) {
                const data = await modelsRes.json();
                setAvailableModels(data.models);
            }
            if (keysRes.ok) {
                setApiKeyStatus(await keysRes.json());
            }

            // 既存のユーザー名・City名をプリフィル
            const updates: Partial<TutorialState> = {};
            if (userRes.ok) {
                const userData = await userRes.json();
                if (userData.display_name) {
                    updates.userName = userData.display_name;
                }
            }
            if (citiesRes.ok) {
                const cities = await citiesRes.json();
                if (cities.length > 0 && cities[0].CITYNAME) {
                    updates.cityName = cities[0].CITYNAME;
                }
            }
            if (Object.keys(updates).length > 0) {
                setState(prev => ({ ...prev, ...updates }));
            }
        } catch (e) {
            console.error('Failed to load tutorial data', e);
        }
    };

    const updateState = (updates: Partial<TutorialState>) => {
        setState(prev => ({ ...prev, ...updates }));
    };

    const handleNext = async () => {
        setError(null);
        setIsLoading(true);

        try {
            switch (step) {
                case 2:
                    await saveUserName();
                    break;
                case 3:
                    await saveCityName();
                    break;
                case 4:
                    // Persona creation is handled in the step component
                    break;
                case 5:
                    await saveApiKeys();
                    await loadInitialData(); // Reload models with updated availability
                    await autoConfigureModels();
                    break;
                case 7:
                    // Save Chronicle setting for the created persona
                    await saveChronicleSettings();
                    break;
            }

            if (step < 8) {
                setStep((step + 1) as Step);
            }
        } catch (e) {
            console.error('Error in step transition', e);
            setError('処理中にエラーが発生しました');
        } finally {
            setIsLoading(false);
        }
    };

    const handleBack = () => {
        if (step > 1) {
            setStep((step - 1) as Step);
        }
    };

    const handleSkip = () => {
        if (step < 9) {
            setStep((step + 1) as Step);
        }
    };

    const handleComplete = async () => {
        try {
            await fetch('/api/tutorial/complete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ version: 1 })
            });

            // Move to the created persona's room if available
            if (state.createdRoomId) {
                await fetch('/api/user/move', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ target_building_id: state.createdRoomId })
                });
            }

            onComplete?.(state.createdRoomId ?? undefined);
            onClose();
        } catch (e) {
            console.error('Failed to complete tutorial', e);
        }
    };

    // API call functions
    const saveUserName = async () => {
        const name = state.userName.trim() || 'ユーザー';
        await fetch('/api/user/me', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ display_name: name })
        });
    };

    const saveCityName = async () => {
        const name = state.cityName.trim();
        if (!name) return; // Skip if empty, will use default

        // Check if city exists and update, or create new
        try {
            const res = await fetch('/api/db/tables/city');
            if (res.ok) {
                const cities = await res.json();
                if (cities.length > 0) {
                    // Update first city
                    await fetch(`/api/world/cities/${cities[0].CITYID}`, {
                        method: 'PUT',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ name })
                    });
                }
            }
        } catch (e) {
            console.error('Failed to save city name', e);
        }
    };

    const saveApiKeys = async () => {
        const updates: Record<string, string> = {};

        for (const [provider, key] of Object.entries(state.apiKeys)) {
            if (key.trim()) {
                const envKey = PROVIDER_ENV_MAPPING[provider];
                if (envKey) {
                    updates[envKey] = key.trim();
                }
            }
        }

        if (Object.keys(updates).length > 0) {
            await fetch('/api/admin/env', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ updates })
            });
        }
    };

    const saveChronicleSettings = async () => {
        if (!state.createdPersonaId) return;
        try {
            await fetch(`/api/people/${state.createdPersonaId}/config`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ chronicle_enabled: state.chronicleEnabled }),
            });
        } catch (e) {
            console.error('Failed to save Chronicle settings', e);
        }
    };

    const autoConfigureModels = async () => {
        try {
            const res = await fetch('/api/tutorial/auto-configure-models', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({})  // Auto-detect provider
            });
            if (res.ok) {
                const data = await res.json();
                updateState({
                    autoConfiguredProvider: data.provider_display,
                    autoConfiguredAssignments: data.assignments,
                    autoConfigureWarnings: data.warnings,
                });
            }
        } catch (e) {
            console.error('Failed to auto-configure models', e);
        }
    };

    const handleModelOverride = async (role: string, envKey: string, modelId: string) => {
        try {
            await fetch('/api/admin/env', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ updates: { [envKey]: modelId } })
            });
            // Update local state
            const updated = state.autoConfiguredAssignments.map(a =>
                a.role === role ? { ...a, model_id: modelId, display_name: modelId } : a
            );
            updateState({ autoConfiguredAssignments: updated });
            // Reload models to get correct display names
            await loadInitialData();
        } catch (e) {
            console.error('Failed to override model', e);
        }
    };

    const renderStepContent = () => {
        switch (step) {
            case 1:
                return <StepWelcome />;
            case 2:
                return (
                    <StepUserName
                        value={state.userName}
                        onChange={(v) => updateState({ userName: v })}
                    />
                );
            case 3:
                return (
                    <StepCityName
                        value={state.cityName}
                        onChange={(v) => updateState({ cityName: v })}
                    />
                );
            case 4:
                return (
                    <StepPersonaChoice
                        choice={state.personaChoice}
                        onChange={(v) => updateState({ personaChoice: v })}
                        onPersonaCreated={(id, roomId) => updateState({ createdPersonaId: id, createdRoomId: roomId })}
                    />
                );
            case 5:
                return (
                    <StepApiKeys
                        apiKeys={state.apiKeys}
                        apiKeyStatus={apiKeyStatus}
                        onChange={(keys) => updateState({ apiKeys: keys })}
                    />
                );
            case 6:
                return (
                    <StepModelSummary
                        provider={state.autoConfiguredProvider}
                        assignments={state.autoConfiguredAssignments}
                        warnings={state.autoConfigureWarnings}
                        availableModels={availableModels}
                        onOverride={handleModelOverride}
                        editMode={startAtStep === 6 && state.autoConfiguredAssignments.length === 0}
                    />
                );
            case 7:
                return (
                    <StepChronicle
                        enabled={state.chronicleEnabled}
                        onChange={(v) => updateState({ chronicleEnabled: v })}
                        personaId={state.createdPersonaId}
                    />
                );
            case 8:
                return <StepComplete onStart={handleComplete} />;
        }
    };

    // Steps that can be skipped
    const canSkip = [2, 3, 5, 6, 7].includes(step);

    if (!isOpen) return null;

    return (
        <ModalOverlay onClose={onClose} className={styles.overlay}>
            <div className={styles.modal} onClick={(e) => e.stopPropagation()}>
                {/* Header */}
                <div className={styles.header}>
                    <h2 className={styles.title}>SAIVerse セットアップ</h2>
                    <button className={styles.closeButton} onClick={onClose}>
                        <X size={20} />
                    </button>
                </div>

                {/* Stepper */}
                <div className={styles.stepper}>
                    {STEP_TITLES.map((title, idx) => (
                        <div
                            key={idx}
                            className={`${styles.step} ${step >= idx + 1 ? styles.active : ''} ${step > idx + 1 ? styles.completed : ''}`}
                        >
                            <span className={styles.stepNumber}>
                                {step > idx + 1 ? '✓' : idx + 1}
                            </span>
                            <span className={styles.stepTitle}>{title}</span>
                        </div>
                    ))}
                </div>

                {/* Content */}
                <div className={styles.content}>
                    {renderStepContent()}
                    {error && <p className={styles.error}>{error}</p>}
                </div>

                {/* Actions */}
                {step !== 8 && (
                    <div className={styles.actions}>
                        <div className={styles.actionsLeft}>
                            {step > 1 && (
                                <button className={styles.backButton} onClick={handleBack}>
                                    <ArrowLeft size={16} /> 戻る
                                </button>
                            )}
                        </div>
                        <div className={styles.actionsRight}>
                            {canSkip && (
                                <button className={styles.skipButton} onClick={handleSkip}>
                                    スキップ
                                </button>
                            )}
                            <button
                                className={styles.nextButton}
                                onClick={handleNext}
                                disabled={isLoading}
                            >
                                {isLoading ? <Loader2 size={16} className={styles.loader} /> : null}
                                次へ <ArrowRight size={16} />
                            </button>
                        </div>
                    </div>
                )}
            </div>
        </ModalOverlay>
    );
}
