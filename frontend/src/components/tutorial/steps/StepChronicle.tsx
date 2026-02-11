"use client";

import React, { useState, useEffect } from 'react';
import { BookOpen, AlertTriangle } from 'lucide-react';
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
}

interface StepChronicleProps {
    enabled: boolean;
    onChange: (enabled: boolean) => void;
    personaId: string | null;
}

export default function StepChronicle({ enabled, onChange, personaId }: StepChronicleProps) {
    const [costEstimate, setCostEstimate] = useState<ChronicleCostEstimate | null>(null);

    useEffect(() => {
        if (personaId) {
            fetch(`/api/people/${personaId}/arasuji/cost-estimate`)
                .then(res => res.ok ? res.json() : null)
                .then(data => { if (data) setCostEstimate(data); })
                .catch(() => {});
        }
    }, [personaId]);

    const formatCost = (cost: number): string => {
        if (cost === 0) return '$0.00';
        if (cost < 0.001) return `~$${cost.toFixed(6)}`;
        if (cost < 0.01) return `~$${cost.toFixed(4)}`;
        return `~$${cost.toFixed(3)}`;
    };

    return (
        <div>
            <h3 style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '1rem' }}>
                <BookOpen size={20} />
                Chronicle（あらすじ）設定
            </h3>

            <p style={{ marginBottom: '1rem', lineHeight: '1.6', fontSize: '0.9rem' }}>
                Chronicleは、ペルソナとの会話履歴を自動的に要約・圧縮する機能です。
                長期的な記憶を維持しつつ、コンテキストウィンドウを効率的に使用します。
            </p>

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
                <div>
                    Chronicle生成時にLLM APIが呼び出され、<strong>APIコストが発生</strong>します。
                    特に大量の会話履歴をインポートした場合、初回の生成コストが高くなる可能性があります。
                </div>
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
                    <div>現在の未処理メッセージ: <strong>{costEstimate.unprocessed_messages.toLocaleString()}</strong>件</div>
                    <div>
                        推定コスト: <strong>
                            {costEstimate.is_free_tier ? '$0.00 (Free tier)' : formatCost(costEstimate.estimated_cost_usd)}
                        </strong>
                        {' '}({costEstimate.model_name})
                    </div>
                    <div>推定LLM呼び出し: {costEstimate.estimated_llm_calls}回</div>
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
                        <div style={{ fontWeight: 'bold' }}>Chronicle 自動生成を有効にする</div>
                        <div style={{ fontSize: '0.85rem', color: '#888', marginTop: '0.25rem' }}>
                            会話が一定量を超えると自動的にChronicleが生成されます
                        </div>
                    </div>
                </label>
            </div>

            <p style={{ fontSize: '0.8rem', color: '#888' }}>
                この設定はペルソナ設定からいつでも変更できます。
                無効にしても、Memory Settings から手動でChronicleを生成することが可能です。
            </p>
        </div>
    );
}
