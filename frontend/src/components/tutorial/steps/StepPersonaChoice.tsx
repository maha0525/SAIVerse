"use client";
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';


import React, { useState } from 'react';
import { UserPlus, Download } from 'lucide-react';
import styles from './Steps.module.css';
import PersonaWizard from '../../PersonaWizard';

interface StepPersonaChoiceProps {
    choice: 'new' | 'import' | null;
    onChange: (choice: 'new' | 'import') => void;
    onPersonaCreated: (personaId: string, roomId: string) => void;
}

export default function StepPersonaChoice({
    choice,
    onChange,
    onPersonaCreated
}: StepPersonaChoiceProps) {
    useLocale();
    const [showPersonaWizard, setShowPersonaWizard] = useState(false);

    const handleNewClick = () => {
        onChange('new');
        setShowPersonaWizard(true);
    };

    const handleImportClick = () => {
        onChange('import');
        setShowPersonaWizard(true);
    };

    return (
        <div className={styles.personaChoiceContainer}>
            <h3 data-i18n="components.tutorial.steps.StepPersonaChoice.text001" className={styles.title}>{uiText("components.tutorial.steps.StepPersonaChoice.text001")}</h3>
            <p data-i18n="components.tutorial.steps.StepPersonaChoice.text002" className={styles.subtitle}>{uiText("components.tutorial.steps.StepPersonaChoice.text002")}</p>

            <div className={styles.choiceCards}>
                <div
                    className={`${styles.choiceCard} ${choice === 'new' ? styles.selected : ''}`}
                    onClick={handleNewClick}
                >
                    <UserPlus size={32} className={styles.choiceIcon} />
                    <div className={styles.choiceInfo}>
                        <div data-i18n="components.tutorial.steps.StepPersonaChoice.text003" className={styles.choiceTitle}>{uiText("components.tutorial.steps.StepPersonaChoice.text003")}</div>
                        <div data-i18n="components.tutorial.steps.StepPersonaChoice.text004" className={styles.choiceDescription}>{uiText("components.tutorial.steps.StepPersonaChoice.text004")}</div>
                    </div>
                </div>

                <div
                    className={`${styles.choiceCard} ${choice === 'import' ? styles.selected : ''}`}
                    onClick={handleImportClick}
                >
                    <Download size={32} className={styles.choiceIcon} />
                    <div className={styles.choiceInfo}>
                        <div data-i18n="components.tutorial.steps.StepPersonaChoice.text005" className={styles.choiceTitle}>{uiText("components.tutorial.steps.StepPersonaChoice.text005")}</div>
                        <div data-i18n="components.tutorial.steps.StepPersonaChoice.text006" className={styles.choiceDescription}>{uiText("components.tutorial.steps.StepPersonaChoice.text006")}</div>
                    </div>
                </div>
            </div>

            {/* PersonaWizard Modal */}
            <PersonaWizard
                isOpen={showPersonaWizard}
                onClose={() => setShowPersonaWizard(false)}
                onComplete={(personaId, roomId) => {
                    onPersonaCreated(personaId, roomId);
                    setShowPersonaWizard(false);
                }}
                embedded
            />
        </div>
    );
}
