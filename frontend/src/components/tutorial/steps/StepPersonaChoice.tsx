"use client";

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
            <h3 className={styles.title}>最初のペルソナを作成</h3>
            <p className={styles.subtitle}>
                この都市にあなたと話す最初のペルソナを呼びましょう
            </p>

            <div className={styles.choiceCards}>
                <div
                    className={`${styles.choiceCard} ${choice === 'new' ? styles.selected : ''}`}
                    onClick={handleNewClick}
                >
                    <UserPlus size={32} className={styles.choiceIcon} />
                    <div className={styles.choiceInfo}>
                        <div className={styles.choiceTitle}>新しく作成する</div>
                        <div className={styles.choiceDescription}>
                            ゼロから新しいペルソナを創造します
                        </div>
                    </div>
                </div>

                <div
                    className={`${styles.choiceCard} ${choice === 'import' ? styles.selected : ''}`}
                    onClick={handleImportClick}
                >
                    <Download size={32} className={styles.choiceIcon} />
                    <div className={styles.choiceInfo}>
                        <div className={styles.choiceTitle}>他のプラットフォームから引き継ぐ</div>
                        <div className={styles.choiceDescription}>
                            ChatGPT等の会話ログをインポートして記憶を引き継ぎます
                        </div>
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
