"use client";

import { useState, useRef, useEffect, useCallback, ReactNode } from 'react';
import { ChevronDown, Wrench, Cog, Unplug, Hammer } from 'lucide-react';
import styles from './ToolModeSelector.module.css';

interface ToolMode {
    id: string;
    shortLabel: string;
    icon: ReactNode;
    description: string;
}

const ICON_SIZE = 14;

const TOOL_MODES: ToolMode[] = [
    {
        id: 'meta_user',
        shortLabel: '自動',
        icon: <Wrench size={ICON_SIZE} />,
        description: '応答前に自動で一度だけツールを使用できます',
    },
    {
        id: 'meta_agentic',
        shortLabel: '自動(連続)',
        icon: <Cog size={ICON_SIZE} />,
        description: '応答前に自動で10回までツールを連続使用できます',
    },
    {
        id: 'meta_simple_speak',
        shortLabel: 'なし',
        icon: <Unplug size={ICON_SIZE} />,
        description: 'ツールを利用しません',
    },
    {
        id: 'meta_user_manual',
        shortLabel: 'ツール指定',
        icon: <Hammer size={ICON_SIZE} />,
        description: 'ユーザーが選んだツールを必ず使います',
    },
];

interface SubPlaybookOption {
    value: string;
    label: string;
}

interface ToolModeSelectorProps {
    selectedPlaybook: string | null;
    onPlaybookChange: (id: string | null) => void;
    playbookArgs: Record<string, any>;
    onPlaybookArgsChange: (params: Record<string, any>) => void;
}

export default function ToolModeSelector({
    selectedPlaybook,
    onPlaybookChange,
    playbookArgs,
    onPlaybookArgsChange,
}: ToolModeSelectorProps) {
    const [isOpen, setIsOpen] = useState(false);
    const [isSubOpen, setIsSubOpen] = useState(false);
    const [subPlaybooks, setSubPlaybooks] = useState<SubPlaybookOption[]>([]);
    const [subPlaybooksLoaded, setSubPlaybooksLoaded] = useState(false);

    const containerRef = useRef<HTMLDivElement>(null);
    const subContainerRef = useRef<HTMLDivElement>(null);

    // Find current mode (fallback to meta_user)
    const currentMode = TOOL_MODES.find(m => m.id === selectedPlaybook) || TOOL_MODES[0];

    // Click-outside handler for main popover
    useEffect(() => {
        if (!isOpen) return;
        const handler = (e: MouseEvent) => {
            if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
                setIsOpen(false);
            }
        };
        document.addEventListener('mousedown', handler);
        return () => document.removeEventListener('mousedown', handler);
    }, [isOpen]);

    // Click-outside handler for sub-playbook popover
    useEffect(() => {
        if (!isSubOpen) return;
        const handler = (e: MouseEvent) => {
            if (subContainerRef.current && !subContainerRef.current.contains(e.target as Node)) {
                setIsSubOpen(false);
            }
        };
        document.addEventListener('mousedown', handler);
        return () => document.removeEventListener('mousedown', handler);
    }, [isSubOpen]);

    // Fetch sub-playbook options when meta_user_manual is selected
    const fetchSubPlaybooks = useCallback(async () => {
        if (subPlaybooksLoaded) return;
        try {
            const res = await fetch('/api/config/playbooks/meta_user_manual/params');
            if (res.ok) {
                const data = await res.json();
                const selectedParam = data.params?.find((p: any) => p.name === 'selected_playbook');
                if (selectedParam?.resolved_options) {
                    setSubPlaybooks(selectedParam.resolved_options);
                }
            }
        } catch (e) {
            console.error("Failed to fetch sub-playbook options", e);
        }
        setSubPlaybooksLoaded(true);
    }, [subPlaybooksLoaded]);

    useEffect(() => {
        if (selectedPlaybook === 'meta_user_manual') {
            fetchSubPlaybooks();
        }
    }, [selectedPlaybook, fetchSubPlaybooks]);

    const syncToServer = async (playbookId: string | null, params: Record<string, any>) => {
        try {
            await fetch('/api/config/playbook', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ playbook: playbookId, args: params }),
            });
        } catch (e) {
            console.error("Failed to save tool mode", e);
        }
    };

    const handleModeChange = (mode: ToolMode) => {
        onPlaybookChange(mode.id);
        const newParams: Record<string, any> = {};
        onPlaybookArgsChange(newParams);
        setIsOpen(false);
        // Reset sub-playbook cache when switching modes
        if (mode.id !== 'meta_user_manual') {
            setSubPlaybooksLoaded(false);
            setSubPlaybooks([]);
        }
        syncToServer(mode.id, newParams);
    };

    const handleSubPlaybookChange = (value: string | null) => {
        const newParams = { ...playbookArgs, selected_playbook: value || '' };
        onPlaybookArgsChange(newParams);
        setIsSubOpen(false);
        syncToServer(selectedPlaybook, newParams);
    };

    const selectedSubPlaybook = playbookArgs?.selected_playbook || null;
    const selectedSubLabel = selectedSubPlaybook
        ? subPlaybooks.find(s => s.value === selectedSubPlaybook)?.label || selectedSubPlaybook
        : null;

    return (
        <div className={styles.container}>
            {/* Main tool mode button */}
            <div ref={containerRef} style={{ position: 'relative' }}>
                <button
                    className={`${styles.toolModeBtn} ${isOpen ? styles.toolModeBtnActive : ''}`}
                    onClick={() => setIsOpen(!isOpen)}
                    title="ツール利用形式"
                >
                    {currentMode.icon}
                    <span className={styles.modeLabel}>{currentMode.shortLabel}</span>
                    <ChevronDown size={14} style={{ opacity: 0.5 }} />
                </button>

                {isOpen && (
                    <div className={styles.popover}>
                        <div className={styles.popoverHeader}>
                            ツールの利用形式を選べます
                        </div>
                        {TOOL_MODES.map(mode => (
                            <button
                                key={mode.id}
                                className={`${styles.modeOption} ${mode.id === selectedPlaybook ? styles.modeOptionSelected : ''}`}
                                onClick={() => handleModeChange(mode)}
                            >
                                <div className={styles.modeOptionLabel}>
                                    {mode.icon}
                                    <span>{mode.shortLabel}</span>
                                    {mode.id === selectedPlaybook && (
                                        <span className={styles.modeOptionCheck}>&#10003;</span>
                                    )}
                                </div>
                                <div className={styles.modeOptionDesc}>
                                    {mode.description}
                                </div>
                            </button>
                        ))}
                    </div>
                )}
            </div>

            {/* Sub-playbook button (only when meta_user_manual is selected) */}
            {selectedPlaybook === 'meta_user_manual' && (
                <div ref={subContainerRef} style={{ position: 'relative' }}>
                    <button
                        className={styles.subPlaybookBtn}
                        onClick={() => setIsSubOpen(!isSubOpen)}
                        title="使用するツールを選択"
                    >
                        <span>{selectedSubLabel || 'ツール未選択'}</span>
                        <ChevronDown size={12} style={{ opacity: 0.5 }} />
                    </button>

                    {isSubOpen && (
                        <div className={styles.subPopover}>
                            <button
                                className={`${styles.subOption} ${styles.subNone} ${!selectedSubPlaybook ? styles.subOptionSelected : ''}`}
                                onClick={() => handleSubPlaybookChange(null)}
                            >
                                （自動判定）
                            </button>
                            {subPlaybooks.map(opt => (
                                <button
                                    key={opt.value}
                                    className={`${styles.subOption} ${opt.value === selectedSubPlaybook ? styles.subOptionSelected : ''}`}
                                    onClick={() => handleSubPlaybookChange(opt.value)}
                                >
                                    {opt.label}
                                </button>
                            ))}
                        </div>
                    )}
                </div>
            )}
        </div>
    );
}
