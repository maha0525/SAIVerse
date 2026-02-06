"use client";

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
        title: '最初からセットアップ',
        description: 'SAIVerseの基本設定を最初から行います',
        icon: <Play size={24} />,
        startStep: 1
    },
    {
        id: 'persona',
        title: 'ペルソナ作成',
        description: '新しいペルソナを作成します',
        icon: <User size={24} />,
        startStep: 4
    },
    {
        id: 'api_keys',
        title: 'APIキー設定',
        description: 'LLMプロバイダーのAPIキーを設定します',
        icon: <Key size={24} />,
        startStep: 5
    },
    {
        id: 'models',
        title: 'モデル選択',
        description: '使用するLLMモデルを変更します',
        icon: <Cpu size={24} />,
        startStep: 6
    }
];

export default function TutorialSelectModal({ isOpen, onClose }: TutorialSelectModalProps) {
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
                                <h2>チュートリアル</h2>
                            </div>
                            <button className={styles.closeButton} onClick={onClose}>
                                <X size={20} />
                            </button>
                        </div>

                        <div className={styles.content}>
                            <p className={styles.description}>
                                実行したいチュートリアルを選択してください
                            </p>

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
