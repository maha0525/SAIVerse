"use client";
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';


import React from 'react';
import { ExternalLink } from 'lucide-react';
import styles from './Steps.module.css';

interface ApiKeyStatus {
    provider: string;
    env_key: string;
    is_set: boolean;
    display_name: string;
    description: string;
    free_label?: string;
    free_note?: string;
}

interface StepApiKeysProps {
    apiKeys: Record<string, string>;
    apiKeyStatus: ApiKeyStatus[];
    onChange: (keys: Record<string, string>) => void;
}

export default function StepApiKeys({
    apiKeys,
    apiKeyStatus,
    onChange
}: StepApiKeysProps) {
    useLocale();
    const handleChange = (provider: string, value: string) => {
        onChange({ ...apiKeys, [provider]: value });
    };

    const DOCS_FILE_MAP: Record<string, string> = {
        openai: 'openai',
        gemini_free: 'gemini-free',
        gemini: 'gemini-paid',
        anthropic: 'anthropic',
        grok: 'grok',
        openrouter: 'openrouter',
        nvidia: 'nvidia-nim',
    };

    const openDocs = (provider: string) => {
        const filename = DOCS_FILE_MAP[provider] || provider;
        window.open(
            `https://github.com/maha0525/SAIVerse/blob/main/docs/api-keys/${filename}.md`,
            '_blank',
            'noopener,noreferrer'
        );
    };

    return (
        <div className={styles.apiKeysContainer}>
            <h3 data-i18n="components.tutorial.steps.StepApiKeys.text001" className={styles.title}>{uiText("components.tutorial.steps.StepApiKeys.text001")}</h3>
            <p data-i18n="components.tutorial.steps.StepApiKeys.text002 components.tutorial.steps.StepApiKeys.text003" className={styles.subtitle}>{uiText("components.tutorial.steps.StepApiKeys.text002")}<br />{uiText("components.tutorial.steps.StepApiKeys.text003")}</p>

            <div className={styles.apiKeyList}>
                {apiKeyStatus.map((status) => (
                    <div key={status.provider} className={styles.apiKeyItem}>
                        <div className={styles.apiKeyHeader}>
                            <div className={styles.apiKeyHeaderLeft}>
                                <label>{status.display_name}</label>
                                {status.free_label && (
                                    <span className={styles.freeBadge}>{status.free_label}</span>
                                )}
                            </div>
                            <button data-i18n="components.tutorial.steps.StepApiKeys.text004 components.tutorial.steps.StepApiKeys.text005"
                                className={styles.docLink}
                                onClick={() => openDocs(status.provider)}
                                title={uiText("components.tutorial.steps.StepApiKeys.text004")}
                            >
                                <ExternalLink size={14} />{uiText("components.tutorial.steps.StepApiKeys.text005")}</button>
                        </div>
                        <p className={styles.providerDescription}>{status.description}</p>
                        {status.free_note && (
                            <p className={styles.freeNote}>{status.free_note}</p>
                        )}
                        <div className={styles.inputRow}>
                            <input data-i18n="components.tutorial.steps.StepApiKeys.text006 components.tutorial.steps.StepApiKeys.text007"
                                type="password"
                                placeholder={status.is_set ? uiText("components.tutorial.steps.StepApiKeys.text006") : uiText("components.tutorial.steps.StepApiKeys.text007")}
                                value={apiKeys[status.provider] || ''}
                                onChange={(e) => handleChange(status.provider, e.target.value)}
                                className={status.is_set ? styles.inputSet : ''}
                            />
                            {status.is_set && (
                                <span data-i18n="components.tutorial.steps.StepApiKeys.text008" className={styles.statusBadge}>{uiText("components.tutorial.steps.StepApiKeys.text008")}</span>
                            )}
                        </div>
                    </div>
                ))}
            </div>
        </div>
    );
}
