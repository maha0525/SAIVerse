import React, { useState } from 'react';
import { Search, Loader2, AlertCircle, Brain } from 'lucide-react';
import styles from './MemoryRecall.module.css';

interface MemoryRecallProps {
    personaId: string;
}

export default function MemoryRecall({ personaId }: MemoryRecallProps) {
    const [query, setQuery] = useState('');
    const [topk, setTopk] = useState(4);
    const [maxChars, setMaxChars] = useState(1200);
    const [result, setResult] = useState<string | null>(null);
    const [isLoading, setIsLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);

    const handleRecall = async () => {
        if (!query.trim()) {
            setError('検索クエリを入力してね');
            return;
        }

        setIsLoading(true);
        setError(null);
        setResult(null);

        try {
            const res = await fetch(`/api/people/${personaId}/recall`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    query: query.trim(),
                    topk,
                    max_chars: maxChars,
                }),
            });

            if (!res.ok) {
                const data = await res.json();
                throw new Error(data.detail || 'Memory recall failed');
            }

            const data = await res.json();
            setResult(data.result);
        } catch (err: any) {
            setError(err.message || 'An error occurred');
        } finally {
            setIsLoading(false);
        }
    };

    const handleKeyPress = (e: React.KeyboardEvent) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleRecall();
        }
    };

    return (
        <div className={styles.container}>
            <div className={styles.header}>
                <Brain size={24} className={styles.icon} />
                <div>
                    <h3 className={styles.title}>Memory Recall Test</h3>
                    <p className={styles.description}>
                        memory_recall ツールと同じロジックでペルソナの長期記憶を検索できるよ。
                        結果を確認してデバッグに使ってね。
                    </p>
                </div>
            </div>

            <div className={styles.inputSection}>
                <label className={styles.label}>検索クエリ</label>
                <textarea
                    className={styles.queryInput}
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                    onKeyDown={handleKeyPress}
                    placeholder="検索したい内容を入力してね"
                    rows={3}
                />
            </div>

            <div className={styles.paramsRow}>
                <div className={styles.param}>
                    <label className={styles.paramLabel}>topk (取得するシード数)</label>
                    <input
                        type="range"
                        min={1}
                        max={20}
                        value={topk}
                        onChange={(e) => setTopk(Number(e.target.value))}
                        className={styles.slider}
                    />
                    <span className={styles.paramValue}>{topk}</span>
                </div>
                <div className={styles.param}>
                    <label className={styles.paramLabel}>max_chars (出力文字数上限)</label>
                    <input
                        type="range"
                        min={100}
                        max={10000}
                        step={100}
                        value={maxChars}
                        onChange={(e) => setMaxChars(Number(e.target.value))}
                        className={styles.slider}
                    />
                    <span className={styles.paramValue}>{maxChars}</span>
                </div>
            </div>

            <button
                className={styles.executeButton}
                onClick={handleRecall}
                disabled={isLoading || !query.trim()}
            >
                {isLoading ? (
                    <>
                        <Loader2 size={16} className={styles.loader} />
                        実行中...
                    </>
                ) : (
                    <>
                        <Search size={16} />
                        Memory Recall を実行
                    </>
                )}
            </button>

            {error && (
                <div className={styles.error}>
                    <AlertCircle size={16} />
                    <span>{error}</span>
                </div>
            )}

            {result !== null && (
                <div className={styles.resultSection}>
                    <label className={styles.label}>実行結果</label>
                    <pre className={styles.resultBox}>{result}</pre>
                </div>
            )}
        </div>
    );
}
