"use client";
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';


import React, { useState } from 'react';
import { X, Play, Key, Cpu, User, HelpCircle } from 'lucide-react';
import styles from './TutorialSelectModal.module.css';
import ModalOverlay from '../common/ModalOverlay';
import TutorialWizard from './TutorialWizard';

interface TutorialSelectModalProps {
    isOpen: boolean;
    onClose: () => void;
}

interface TutorialOption {
    id: string;
    title: string;
    description: string;
    icon: React.ReactNode;
    startStep: number;
}

const TUTORIAL_OPTIONS: TutorialOption[] = [
    {
        id: 'full',
        get title() { return uiText("components.tutorial.TutorialSelectModal.text001"); },
        get description() { return uiText("components.tutorial.TutorialSelectModal.text002"); },
        icon: <Play size={24} />,
        startStep: 1
    },
    {
        id: 'persona',
        get title() { return uiText("components.tutorial.TutorialSelectModal.text003"); },
        get description() { return uiText("components.tutorial.TutorialSelectModal.text004"); },
        icon: <User size={24} />,
        startStep: 4
    },
    {
        id: 'api_keys',
        get title() { return uiText("components.tutorial.TutorialSelectModal.text005"); },
        get description() { return uiText("components.tutorial.TutorialSelectModal.text006"); },
        icon: <Key size={24} />,
        startStep: 5
    },
    {
        id: 'models',
        get title() { return uiText("components.tutorial.TutorialSelectModal.text007"); },
        get description() { return uiText("components.tutorial.TutorialSelectModal.text008"); },
        icon: <Cpu size={24} />,
        startStep: 6
    }
];

export default function TutorialSelectModal({ isOpen, onClose }: TutorialSelectModalProps) {
    useLocale();
    const [selectedOption, setSelectedOption] = useState<TutorialOption | null>(null);
    const [isTutorialWizardOpen, setIsTutorialWizardOpen] = useState(false);

    const handleSelect = (option: TutorialOption) => {
        setSelectedOption(option);
        setIsTutorialWizardOpen(true);
        onClose();
    };

    const handleWizardClose = () => {
        setIsTutorialWizardOpen(false);
        setSelectedOption(null);
    };

    if (!isOpen && !isTutorialWizardOpen) return null;

    return (
        <>
            {isOpen && (
                <ModalOverlay onClose={onClose} className={styles.overlay}>
                    <div className={styles.modal} onClick={(e) => e.stopPropagation()}>
                        <div className={styles.header}>
                            <div className={styles.headerTitle}>
                                <HelpCircle size={24} />
                                <h2 data-i18n="components.tutorial.TutorialSelectModal.text009">{uiText("components.tutorial.TutorialSelectModal.text009")}</h2>
                            </div>
                            <button className={styles.closeButton} onClick={onClose}>
                                <X size={20} />
                            </button>
                        </div>

                        <div className={styles.content}>
                            <p data-i18n="components.tutorial.TutorialSelectModal.text010" className={styles.description}>{uiText("components.tutorial.TutorialSelectModal.text010")}</p>

                            <div className={styles.optionList}>
                                {TUTORIAL_OPTIONS.map((option) => (
                                    <div
                                        key={option.id}
                                        className={styles.optionCard}
                                        onClick={() => handleSelect(option)}
                                    >
                                        <div className={styles.optionIcon}>{option.icon}</div>
                                        <div className={styles.optionInfo}>
                                            <div className={styles.optionTitle}>{option.title}</div>
                                            <div className={styles.optionDescription}>{option.description}</div>
                                        </div>
                                    </div>
                                ))}
                            </div>
                        </div>
                    </div>
                </ModalOverlay>
            )}

            {selectedOption && (
                <TutorialWizard
                    isOpen={isTutorialWizardOpen}
                    onClose={handleWizardClose}
                    startAtStep={selectedOption.startStep}
                    onComplete={() => {
                        handleWizardClose();
                        window.location.reload();
                    }}
                />
            )}
        </>
    );
}
