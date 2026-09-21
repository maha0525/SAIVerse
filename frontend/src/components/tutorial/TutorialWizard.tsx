"use client";
import { apiFetch } from '@/i18n/api';

import { t as uiText, setLocale, getLocale, type Locale } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';


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
    /** 反射判断専用の宛先 (型付きの質問に確率で答えるだけで、文章を書けない)。
     *  役割ごとの選択肢を出す StepModelSummary が、反射判断の役割にだけ出す。 */
    reflex_only?: boolean;
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
    timezone: string;
    language: string;
    personaChoice: 'new' | 'import' | null;
    apiKeys: Record<string, string>;
    createdPersonaId: string | null;
    createdRoomId: string | null;
    autoConfiguredProvider: string;
    autoConfiguredAssignments: ModelRoleAssignment[];
    autoConfigureWarnings: string[];
    chronicleEnabled: boolean;
}

const getStepTitles = () => [
    'Welcome',
    uiText("components.tutorial.TutorialWizard.text001"),
    uiText("components.tutorial.TutorialWizard.text002"),
    uiText("components.tutorial.TutorialWizard.text003"),
    uiText("components.tutorial.TutorialWizard.text004"),
    uiText("components.tutorial.TutorialWizard.text005"),
    'Chronicle',
    uiText("components.tutorial.TutorialWizard.text006")
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
    useLocale();
    const [step, setStep] = useState<Step>(startAtStep as Step);
    const [isLoading, setIsLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const [state, setState] = useState<TutorialState>({
        userName: '',
        cityName: '',
        timezone: '',
        language: 'ja',
        personaChoice: null,
        apiKeys: {},
        createdPersonaId: null,
        createdRoomId: null,
        autoConfiguredProvider: '',
        autoConfiguredAssignments: [],
        autoConfigureWarnings: [],
        chronicleEnabled: true,
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
                timezone: '',
                language: getLocale() || 'ja',
                personaChoice: null,
                apiKeys: {},
                createdPersonaId: null,
                createdRoomId: null,
                autoConfiguredProvider: '',
                autoConfiguredAssignments: [],
                autoConfigureWarnings: [],
                chronicleEnabled: true,
            });
            loadInitialData();
        }
    }, [isOpen, startAtStep]);

    const loadInitialData = async () => {
        try {
            const [modelsRes, keysRes, userRes, citiesRes] = await Promise.all([
                apiFetch('/api/tutorial/available-models'),
                apiFetch('/api/tutorial/api-keys/status'),
                apiFetch('/api/user/status'),
                apiFetch('/api/db/tables/city'),
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
                if (cities.length > 0) {
                    // 表示名は CITYNAME。未設定なら識別子 (CITY_SLUG) を初期値にする
                    updates.cityName = cities[0].CITYNAME || cities[0].CITY_SLUG || '';
                    // Auto-detect timezone from browser if city still has default 'UTC'
                    const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
                    const cityTz = cities[0].TIMEZONE || 'UTC';
                    updates.timezone = (cityTz === 'UTC') ? browserTz : cityTz;
                    if (cities[0].LANGUAGE) {
                        updates.language = cities[0].LANGUAGE;
                    }
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
            setError(e instanceof Error ? e.message : uiText("components.tutorial.TutorialWizard.text007"));
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
        setError(null);
        try {
            const completeRes = await apiFetch('/api/tutorial/complete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ version: 1 })
            });
            if (!completeRes.ok) {
                console.error('Failed to mark tutorial complete:', completeRes.status);
            }

            // Move to the created persona's room if available
            if (state.createdRoomId) {
                const moveRes = await apiFetch('/api/user/move', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ target_building_id: state.createdRoomId })
                });
                if (!moveRes.ok) {
                    console.error('Failed to move to room:', moveRes.status);
                }
            }

            onComplete?.(state.createdRoomId ?? undefined);
            onClose();
        } catch (e) {
            console.error('Failed to complete tutorial', e);
            setError(e instanceof Error ? e.message : uiText("components.tutorial.TutorialWizard.text008"));
        }
    };

    // API call functions
    const saveUserName = async () => {
        const name = state.userName.trim() || uiText("components.tutorial.TutorialWizard.text009");
        const res = await apiFetch('/api/user/me', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ display_name: name })
        });
        if (!res.ok) {
            const detail = await res.json().catch(() => ({}));
            throw new Error(detail.detail || uiText("components.tutorial.TutorialWizard.text010"));
        }
    };

    const saveCityName = async () => {
        const res = await apiFetch('/api/db/tables/city');
        if (!res.ok) {
            throw new Error(uiText("components.tutorial.TutorialWizard.text011"));
        }
        const cities = await res.json();
        if (cities.length === 0) {
            throw new Error(uiText("components.tutorial.TutorialWizard.text012"));
        }

        const city = cities[0];
        const name = state.cityName.trim() || city.CITYNAME || city.CITY_SLUG;
        // PUT は全フィールドを要求する。name = 表示名 (CITYNAME)。内部の識別子
        // (CITY_SLUG) は City 作成後は変更できないので送らない。説明文は
        // チュートリアルでは聞かないため既存値をそのまま返す。
        // タイムゾーンは City 名が空でも必ず送る。
        const updateRes = await apiFetch(`/api/world/cities/${city.CITYID}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                name,
                description: city.DESCRIPTION ?? '',
                online_mode: city.START_IN_ONLINE_MODE ?? false,
                ui_port: city.UI_PORT ?? 8000,
                api_port: city.API_PORT ?? 8001,
                timezone: state.timezone || city.TIMEZONE || 'UTC',
                language: state.language || city.LANGUAGE || 'ja',
            })
        });
        if (!updateRes.ok) {
            const detail = await updateRes.json().catch(() => ({}));
            throw new Error(detail.detail || uiText("components.tutorial.TutorialWizard.text013"));
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
            const res = await apiFetch('/api/admin/env', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ updates })
            });
            if (!res.ok) {
                const detail = await res.json().catch(() => ({}));
                throw new Error(detail.detail || uiText("components.tutorial.TutorialWizard.text014"));
            }
        }
    };

    const saveChronicleSettings = async () => {
        if (!state.createdPersonaId) return;

        try {
            await apiFetch(`/api/people/${state.createdPersonaId}/config`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    chronicle_generation_active: state.chronicleEnabled
                })
            });
        } catch (e) {
            console.error('Failed to save Chronicle setting', e);
        }
    };

    const autoConfigureModels = async () => {
        try {
            const res = await apiFetch('/api/admin/models/auto-configure', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            });
            if (res.ok) {
                const data = await res.json();
                setState(prev => ({
                    ...prev,
                    autoConfiguredProvider: data.provider || '',
                    autoConfiguredAssignments: data.assignments || [],
                    autoConfigureWarnings: data.warnings || []
                }));
            }
        } catch (e) {
            console.error('Failed to auto-configure models', e);
        }
    };

    const handleModelOverride = async (role: string, envKey: string, modelId: string) => {
        try {
            const res = await apiFetch('/api/admin/env', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ updates: { [envKey]: modelId } })
            });
            if (res.ok) {
                // 保存しなかったモデル設定や、切り替えられなかったペルソナの知らせ
                const data = await res.json().catch(() => null);
                const notices: string[] = Array.isArray(data?.notices) ? data.notices : [];
                if (notices.length > 0) {
                    alert(notices.join('\n\n'));
                }
                const rejected: string[] = Array.isArray(data?.rejected_keys) ? data.rejected_keys : [];
                if (rejected.includes(envKey)) {
                    return; // 保存されていないので、表示も前の値のままにする
                }
            }
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
                        timezone={state.timezone}
                        onTimezoneChange={(v) => updateState({ timezone: v })}
                        language={state.language}
                        onLanguageChange={(v) => {
                            updateState({ language: v });
                            setLocale(v as Locale);
                        }}
                    />
                );
            case 4:
                return (
                    <StepPersonaChoice
                        choice={state.personaChoice}
                        onChange={(v) => updateState({ personaChoice: v })}
                        onPersonaCreated={(id, roomId) => {
                            updateState({ createdPersonaId: id, createdRoomId: roomId });
                            setStep(5);
                        }}
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
                    <h2 data-i18n="components.tutorial.TutorialWizard.text016" className={styles.title}>{uiText("components.tutorial.TutorialWizard.text016")}</h2>
                    <button className={styles.closeButton} onClick={onClose}>
                        <X size={20} />
                    </button>
                </div>

                {/* Stepper */}
                <div className={styles.stepper}>
                    {getStepTitles().map((title, idx) => (
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
                                <button data-i18n="components.tutorial.TutorialWizard.text017" className={styles.backButton} onClick={handleBack}>
                                    <ArrowLeft size={16} />{uiText("components.tutorial.TutorialWizard.text017")}</button>
                            )}
                        </div>
                        <div className={styles.actionsRight}>
                            {canSkip && (
                                <button data-i18n="components.tutorial.TutorialWizard.text018" className={styles.skipButton} onClick={handleSkip}>{uiText("components.tutorial.TutorialWizard.text018")}</button>
                            )}
                            <button data-i18n="components.tutorial.TutorialWizard.text019"
                                className={styles.nextButton}
                                onClick={handleNext}
                                disabled={isLoading}
                            >
                                {isLoading ? <Loader2 size={16} className={styles.loader} /> : null}{uiText("components.tutorial.TutorialWizard.text019")}<ArrowRight size={16} />
                            </button>
                        </div>
                    </div>
                )}
            </div>
        </ModalOverlay>
    );
}
