'use client';

import React, { useState } from 'react';
import { X, ChevronDown, ChevronRight } from 'lucide-react';
import ModalOverlay from '@/components/common/ModalOverlay';
import styles from './ContextPreviewModal.module.css';

interface SectionInfo {
    name: string;
    label: string;
    tokens: number;
    message_count: number;
}

interface AnnotatedMessage {
    role: string;
    content: string;
    section: string;
    tokens: number;
}

interface PersonaPreview {
    persona_id: string;
    persona_name: string;
    model: string;
    model_display_name: string;
    provider: string;
    context_length: number;
    sections: SectionInfo[];
    total_input_tokens: number;
    estimated_cost_best_usd: number;
    estimated_cost_worst_usd: number;
    cache_enabled: boolean;
    cache_ttl: string | null;
    cache_type: string | null;
    pricing: Record<string, number>;
    messages: AnnotatedMessage[];
}

export interface ContextPreviewData {
    personas: PersonaPreview[];
}

interface ContextPreviewModalProps {
    isOpen: boolean;
    onClose: () => void;
    data: ContextPreviewData | null;
    isLoading: boolean;
}

function formatCost(cost: number): string {
    if (cost === 0) return '$0';
    if (cost < 0.001) return `~$${cost.toFixed(6)}`;
    if (cost < 0.01) return `~$${cost.toFixed(4)}`;
    return `~$${cost.toFixed(3)}`;
}

function formatTokens(tokens: number): string {
    if (tokens >= 1000000) return `${(tokens / 1000000).toFixed(1)}M`;
    if (tokens >= 1000) return `${(tokens / 1000).toFixed(1)}K`;
    return tokens.toLocaleString();
}

function TokenBar({ used, total }: { used: number; total: number }) {
    const pct = Math.min((used / total) * 100, 100);
    let color = '#34d399'; // green
    if (pct > 85) color = '#f87171'; // red
    else if (pct > 60) color = '#fbbf24'; // yellow

    return (
        <div className={styles.tokenBar}>
            <div className={styles.tokenBarFill} style={{ width: `${pct}%`, background: color }} />
            <span className={styles.tokenBarLabel}>
                {formatTokens(used)} / {formatTokens(total)} ({pct.toFixed(0)}%)
            </span>
        </div>
    );
}

function SectionRow({ section, totalTokens, messages, isExpanded, onToggle }: {
    section: SectionInfo;
    totalTokens: number;
    messages: AnnotatedMessage[];
    isExpanded: boolean;
    onToggle: () => void;
}) {
    const pct = totalTokens > 0 ? (section.tokens / totalTokens) * 100 : 0;
    const sectionMessages = messages.filter(m => m.section === section.name);

    return (
        <div className={styles.sectionRow}>
            <button className={styles.sectionHeader} onClick={onToggle}>
                <span className={styles.sectionToggle}>
                    {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                </span>
                <span className={styles.sectionLabel}>
                    {section.label}
                    {section.name === 'history' && section.message_count > 0 && ` (${section.message_count}件)`}
                </span>
                <span className={styles.sectionTokens}>{formatTokens(section.tokens)}</span>
                <div className={styles.sectionBar}>
                    <div className={styles.sectionBarFill} style={{ width: `${Math.min(pct, 100)}%` }} />
                </div>
                <span className={styles.sectionPct}>{pct.toFixed(1)}%</span>
            </button>
            {isExpanded && sectionMessages.length > 0 && (
                <div className={styles.sectionMessages}>
                    {sectionMessages.map((msg, idx) => (
                        <div key={idx} className={`${styles.messageItem} ${styles[`msg_${msg.role}`] || ''}`}>
                            <div className={styles.messageMeta}>
                                <span className={styles.messageRole}>{msg.role}</span>
                                <span className={styles.messageTokenCount}>{msg.tokens} トークン</span>
                            </div>
                            <pre className={styles.messageContent}>
                                {msg.content}
                            </pre>
                        </div>
                    ))}
                </div>
            )}
            {isExpanded && sectionMessages.length === 0 && section.tokens > 0 && (
                <div className={styles.sectionMessages}>
                    <div className={styles.estimateNote}>
                        推定トークン数（メッセージ内容なし）
                    </div>
                </div>
            )}
        </div>
    );
}

function PersonaPreviewView({ persona }: { persona: PersonaPreview }) {
    const [expandedSections, setExpandedSections] = useState<Set<string>>(new Set());

    const toggleSection = (name: string) => {
        setExpandedSections(prev => {
            const next = new Set(prev);
            if (next.has(name)) next.delete(name);
            else next.add(name);
            return next;
        });
    };

    const outputRate = persona.pricing?.output_per_1m_tokens;

    return (
        <div className={styles.personaPreview}>
            {/* Model Info */}
            <div className={styles.modelInfo}>
                <span className={styles.modelName}>{persona.model_display_name}</span>
                <span className={styles.providerBadge}>{persona.provider}</span>
            </div>

            {/* Token Usage Bar */}
            <div className={styles.usageSummary}>
                <div className={styles.usageLabel}>推定入力トークン数</div>
                <TokenBar used={persona.total_input_tokens} total={persona.context_length} />
            </div>

            {/* Cost Estimate */}
            <div className={styles.costSummary}>
                <div className={styles.costMain}>
                    <span className={styles.costLabel}>推定入力コスト</span>
                    <span className={styles.costValue}>
                        {persona.cache_enabled && persona.estimated_cost_best_usd !== persona.estimated_cost_worst_usd
                            ? `${formatCost(persona.estimated_cost_best_usd)} ~ ${formatCost(persona.estimated_cost_worst_usd)}`
                            : formatCost(persona.estimated_cost_worst_usd)}
                    </span>
                </div>
                {persona.cache_enabled && (
                    <div className={styles.costNote}>
                        {persona.cache_type === 'explicit'
                            ? `キャッシュ${persona.cache_ttl === '1h' ? '(1時間)' : '(5分)'}有効 — 左: 全ヒット時 / 右: 全書き込み時`
                            : 'キャッシュ(暗黙的)有効 — 左: 全ヒット時 / 右: キャッシュなし時'}
                    </div>
                )}
                {outputRate != null && outputRate > 0 && (
                    <div className={styles.costNote}>
                        出力コストは応答長に依存 (${outputRate}/1Mトークン)
                    </div>
                )}
            </div>

            {/* Section Breakdown */}
            <div className={styles.sectionsContainer}>
                <h3 className={styles.sectionsTitle}>トークン内訳</h3>
                {persona.sections.map(section => (
                    <SectionRow
                        key={section.name}
                        section={section}
                        totalTokens={persona.total_input_tokens}
                        messages={persona.messages}
                        isExpanded={expandedSections.has(section.name)}
                        onToggle={() => toggleSection(section.name)}
                    />
                ))}
            </div>
        </div>
    );
}

export default function ContextPreviewModal({ isOpen, onClose, data, isLoading }: ContextPreviewModalProps) {
    const [selectedPersonaIdx, setSelectedPersonaIdx] = useState(0);

    if (!isOpen) return null;

    const personas = data?.personas || [];
    const activePersona = personas[selectedPersonaIdx] || null;

    return (
        <ModalOverlay onClose={onClose}>
            <div className={styles.modal}>
                <div className={styles.header}>
                    <div className={styles.headerInfo}>
                        <h2>コンテキストプレビュー</h2>
                    </div>
                    <button className={styles.closeBtn} onClick={onClose}>
                        <X size={18} />
                    </button>
                </div>

                <div className={styles.body}>
                    {isLoading && (
                        <div className={styles.loading}>コンテキストを読み込み中...</div>
                    )}

                    {!isLoading && personas.length === 0 && (
                        <div className={styles.empty}>このビルディングに応答可能なペルソナがいません。</div>
                    )}

                    {!isLoading && personas.length > 0 && (
                        <>
                            {/* Persona tabs (if multiple) */}
                            {personas.length > 1 && (
                                <div className={styles.personaTabs}>
                                    {personas.map((p, idx) => (
                                        <button
                                            key={p.persona_id}
                                            className={`${styles.personaTab} ${idx === selectedPersonaIdx ? styles.activeTab : ''}`}
                                            onClick={() => setSelectedPersonaIdx(idx)}
                                        >
                                            {p.persona_name}
                                        </button>
                                    ))}
                                </div>
                            )}

                            {activePersona && (
                                <PersonaPreviewView persona={activePersona} />
                            )}
                        </>
                    )}
                </div>
            </div>
        </ModalOverlay>
    );
}
