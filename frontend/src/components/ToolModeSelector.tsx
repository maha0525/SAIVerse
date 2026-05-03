"use client";

import { useState, useRef, useEffect, useCallback, ReactNode } from 'react';
import { ChevronDown, Wrench, Hammer } from 'lucide-react';
import styles from './ToolModeSelector.module.css';

const ICON_SIZE = 14;

// Sentinel value for "ツール指定" mode. Real Playbook names are sent via
// `pre_spells` in the chat payload; this string is only an internal UI mode
// identifier (not a Playbook name on the server).
//
// See: docs/intent/persona_cognition/nested_subline_spell.md §13
export const TOOL_MODE_SELECTED = 'tool_selected';

interface ToolMode {
    id: string | null;
    shortLabel: string;
    icon: ReactNode;
    description: string;
}

const TOOL_MODES: ToolMode[] = [
    {
        id: null,
        shortLabel: '自動',
        icon: <Wrench size={ICON_SIZE} />,
        description: 'ペルソナがツール利用を自分で判断します',
    },
    {
        id: TOOL_MODE_SELECTED,
        shortLabel: 'ツール指定',
        icon: <Hammer size={ICON_SIZE} />,
        description: '応答前に指定したツールを必ず実行します',
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

    // Treat anything other than the sentinel as "auto" — legacy values
    // (meta_user / meta_user_manual / meta_simple_speak) persisted on the
    // server are silently mapped here so the UI doesn't get stuck.
    const normalizedMode: string | null =
        selectedPlaybook === TOOL_MODE_SELECTED ? TOOL_MODE_SELECTED : null;

    const currentMode = TOOL_MODES.find(m => m.id === normalizedMode) || TOOL_MODES[0];

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

    // Pull the list of router_callable Playbooks dynamically when "ツール指定" is active.
    const fetchSubPlaybooks = useCallback(async () => {
        if (subPlaybooksLoaded) return;
        try {
            const res = await fetch('/api/config/playbooks?router_callable=true');
            if (res.ok) {
                const data = await res.json();
                if (Array.isArray(data)) {
                    setSubPlaybooks(data.map((p: any) => ({
                        value: p.id,
                        label: p.name || p.id,
                    })));
                }
            }
        } catch (e) {
            console.error("Failed to fetch router_callable playbooks", e);
        }
        setSubPlaybooksLoaded(true);
    }, [subPlaybooksLoaded]);

    useEffect(() => {
        if (normalizedMode === TOOL_MODE_SELECTED) {
            fetchSubPlaybooks();
        }
    }, [normalizedMode, fetchSubPlaybooks]);

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
        if (mode.id !== TOOL_MODE_SELECTED) {
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
                                key={mode.id ?? 'auto'}
                                className={`${styles.modeOption} ${mode.id === normalizedMode ? styles.modeOptionSelected : ''}`}
                                onClick={() => handleModeChange(mode)}
                            >
                                <div className={styles.modeOptionLabel}>
                                    {mode.icon}
                                    <span>{mode.shortLabel}</span>
                                    {mode.id === normalizedMode && (
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

            {normalizedMode === TOOL_MODE_SELECTED && (
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
                                （未選択）
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
