"use client";

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';

import React from 'react';
import { BookOpen, AlertTriangle } from 'lucide-react';

interface StepChronicleProps {
    enabled: boolean;
    onChange: (enabled: boolean) => void;
}

/**
 * Tutorial step for the Chronicle auto-generation setting only. The count of
 * unsummarized messages and the cost estimate are shown by the import flow's
 * Chronicle step (ImportFlowModal, reached through the embedded PersonaWizard),
 * so they are not repeated here (docs/intent/import_flow_redesign.md).
 */
export default function StepChronicle({ enabled, onChange }: StepChronicleProps) {
    useLocale();

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
