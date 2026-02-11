"use client";

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
            <h3 className={styles.title}>APIキー設定</h3>
            <p className={styles.subtitle}>
                ペルソナが話すためにはAPIキーが必要です。<br />
                利用したいプラットフォームのAPIキーを入力してください。
            </p>

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
                            <button
                                className={styles.docLink}
                                onClick={() => openDocs(status.provider)}
                                title="APIキー取得方法を見る"
                            >
                                <ExternalLink size={14} />
                                取得方法
                            </button>
                        </div>
                        <p className={styles.providerDescription}>{status.description}</p>
                        {status.free_note && (
                            <p className={styles.freeNote}>{status.free_note}</p>
                        )}
                        <div className={styles.inputRow}>
                            <input
                                type="password"
                                placeholder={status.is_set ? '（設定済み - 変更する場合のみ入力）' : 'APIキーを入力'}
                                value={apiKeys[status.provider] || ''}
                                onChange={(e) => handleChange(status.provider, e.target.value)}
                                className={status.is_set ? styles.inputSet : ''}
                            />
                            {status.is_set && (
                                <span className={styles.statusBadge}>設定済</span>
                            )}
                        </div>
                    </div>
                ))}
            </div>
        </div>
    );
}
