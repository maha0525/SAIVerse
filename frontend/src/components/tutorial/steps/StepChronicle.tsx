"use client";
import { apiFetch } from '@/i18n/api';

import { getFormatLocale } from '@/i18n/core';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';


import React, { useState, useEffect } from 'react';
import { BookOpen, AlertTriangle } from 'lucide-react';
import { formatCost } from '@/lib/formatCost';
import styles from './Steps.module.css';

interface ChronicleCostEstimate {
    total_messages: number;
    processed_messages: number;
    unprocessed_messages: number;
    estimated_llm_calls: number;
    estimated_cost_usd: number;
    model_name: string;
    is_free_tier: boolean;
    batch_size: number;
    currency?: string;
}

interface StepChronicleProps {
    enabled: boolean;
    onChange: (enabled: boolean) => void;
    personaId: string | null;
}

export default function StepChronicle({ enabled, onChange, personaId }: StepChronicleProps) {
    useLocale();
    const [costEstimate, setCostEstimate] = useState<ChronicleCostEstimate | null>(null);

    useEffect(() => {
        if (personaId) {
            apiFetch(`/api/people/${personaId}/arasuji/cost-estimate`)
                .then(res => res.ok ? res.json() : null)
                .then(data => { if (data) setCostEstimate(data); })
                .catch(() => {});
        }
    }, [personaId]);

    return (
        <div>
            <h3 data-i18n="components.tutorial.steps.StepChronicle.text001" style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '1rem' }}>
                <BookOpen size={20} />{uiText("components.tutorial.steps.StepChronicle.text001")}</h3>

            <p data-i18n="components.tutorial.steps.StepChronicle.text002" style={{ marginBottom: '1rem', lineHeight: '1.6', fontSize: '0.9rem' }}>{uiText("components.tutorial.steps.StepChronicle.text002")}</p>

            <div style={{
                padding: '0.75rem',
                marginBottom: '1rem',
                background: 'rgba(255, 150, 0, 0.1)',
                borderRadius: '6px',
                fontSize: '0.85rem',
                display: 'flex',
                gap: '0.5rem',
                alignItems: 'flex-start',
            }}>
                <AlertTriangle size={18} style={{ flexShrink: 0, marginTop: '2px' }} />
                <div data-i18n="components.tutorial.steps.StepChronicle.text003 components.tutorial.steps.StepChronicle.text005">{uiText("components.tutorial.steps.StepChronicle.text003")}<strong data-i18n="components.tutorial.steps.StepChronicle.text004">{uiText("components.tutorial.steps.StepChronicle.text004")}</strong>{uiText("components.tutorial.steps.StepChronicle.text005")}</div>
            </div>

            {costEstimate && costEstimate.unprocessed_messages > 0 && (
                <div style={{
                    padding: '0.75rem',
                    marginBottom: '1rem',
                    background: 'rgba(100, 100, 100, 0.1)',
                    borderRadius: '6px',
                    fontSize: '0.85rem',
                    lineHeight: '1.6',
                }}>
                    <div data-i18n="components.tutorial.steps.StepChronicle.text006 components.tutorial.steps.StepChronicle.text007">{uiText("components.tutorial.steps.StepChronicle.text006")}<strong>{costEstimate.unprocessed_messages.toLocaleString(getFormatLocale())}</strong>{uiText("components.tutorial.steps.StepChronicle.text007")}</div>
                    <div data-i18n="components.tutorial.steps.StepChronicle.text008">{uiText("components.tutorial.steps.StepChronicle.text008")}<strong>
                            {costEstimate.is_free_tier ? `${formatCost(0, costEstimate.currency)} (Free tier)` : formatCost(costEstimate.estimated_cost_usd, costEstimate.currency)}
                        </strong>
                        {' '}({costEstimate.model_name})
                    </div>
                    <div data-i18n="components.tutorial.steps.StepChronicle.text009 components.tutorial.steps.StepChronicle.text010">{uiText("components.tutorial.steps.StepChronicle.text009")}{costEstimate.estimated_llm_calls}{uiText("components.tutorial.steps.StepChronicle.text010")}</div>
                    <div data-i18n="components.tutorial.steps.StepChronicle.text011" style={{ marginTop: '0.25rem' }}>{uiText("components.tutorial.steps.StepChronicle.text011")}</div>
                </div>
            )}

            <div style={{
                padding: '1rem',
                border: '1px solid rgba(128, 128, 128, 0.3)',
                borderRadius: '8px',
                marginBottom: '1rem',
            }}>
                <label style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', cursor: 'pointer' }}>
                    <input
                        type="checkbox"
                        checked={enabled}
                        onChange={(e) => onChange(e.target.checked)}
                        style={{ width: '18px', height: '18px' }}
                    />
                    <div>
                        <div data-i18n="components.tutorial.steps.StepChronicle.text012" style={{ fontWeight: 'bold' }}>{uiText("components.tutorial.steps.StepChronicle.text012")}</div>
                        <div data-i18n="components.tutorial.steps.StepChronicle.text013" style={{ fontSize: '0.85rem', color: '#888', marginTop: '0.25rem' }}>{uiText("components.tutorial.steps.StepChronicle.text013")}</div>
                    </div>
                </label>
            </div>

            <p data-i18n="components.tutorial.steps.StepChronicle.text014" style={{ fontSize: '0.8rem', color: '#888' }}>{uiText("components.tutorial.steps.StepChronicle.text014")}</p>
        </div>
    );
}
