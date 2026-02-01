"use client";

import { useEffect, useState } from 'react';
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts';
import { ArrowLeft, RefreshCw } from 'lucide-react';
import styles from './page.module.css';

interface UsageSummary {
    total_cost_usd: number;
    total_input_tokens: number;
    total_output_tokens: number;
    call_count: number;
}

interface DailyUsage {
    date: string;
    model_id: string;
    model_display_name: string;
    cost_usd: number;
    input_tokens: number;
    output_tokens: number;
    call_count: number;
}

interface Persona {
    persona_id: string;
    persona_name: string;
}

// Color palette for models
const MODEL_COLORS: Record<string, string> = {
    'gemini-2.5-flash': '#4285F4',
    'gemini-2.5-pro': '#0F9D58',
    'gpt-4o': '#10A37F',
    'chatgpt-4o-latest': '#74AA9C',
    'claude-sonnet-4-5': '#D97757',
    'claude-opus-4': '#CC785C',
};

function getModelColor(modelId: string): string {
    // Check for exact match first
    if (MODEL_COLORS[modelId]) return MODEL_COLORS[modelId];
    // Check for partial match
    for (const [key, color] of Object.entries(MODEL_COLORS)) {
        if (modelId.includes(key) || key.includes(modelId)) return color;
    }
    // Generate a consistent color from model ID
    let hash = 0;
    for (let i = 0; i < modelId.length; i++) {
        hash = modelId.charCodeAt(i) + ((hash << 5) - hash);
    }
    const hue = Math.abs(hash) % 360;
    return `hsl(${hue}, 60%, 50%)`;
}

export default function UsagePage() {
    const [summary, setSummary] = useState<UsageSummary | null>(null);
    const [dailyData, setDailyData] = useState<DailyUsage[]>([]);
    const [personas, setPersonas] = useState<Persona[]>([]);
    const [selectedPersona, setSelectedPersona] = useState<string>('');
    const [days, setDays] = useState(30);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    const fetchData = async () => {
        setLoading(true);
        setError(null);
        try {
            const personaParam = selectedPersona ? `&persona_id=${selectedPersona}` : '';

            const [summaryRes, dailyRes, personasRes] = await Promise.all([
                fetch(`/api/usage/summary?days=${days}${personaParam}`),
                fetch(`/api/usage/daily?${personaParam}`),
                fetch('/api/usage/personas'),
            ]);

            if (!summaryRes.ok || !dailyRes.ok) {
                throw new Error('Failed to fetch usage data');
            }

            const summaryData = await summaryRes.json();
            const dailyDataRaw = await dailyRes.json();
            const personasData = personasRes.ok ? await personasRes.json() : [];

            setSummary(summaryData);
            setDailyData(dailyDataRaw);
            setPersonas(personasData);
        } catch (err) {
            setError(err instanceof Error ? err.message : 'Unknown error');
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        fetchData();
    }, [selectedPersona, days]);

    // Transform daily data for stacked bar chart
    const chartData = (() => {
        const dateMap = new Map<string, Record<string, number>>();
        const allModels = new Set<string>();

        for (const item of dailyData) {
            allModels.add(item.model_id);
            if (!dateMap.has(item.date)) {
                dateMap.set(item.date, { date: item.date } as Record<string, number>);
            }
            const entry = dateMap.get(item.date)!;
            entry[item.model_id] = (entry[item.model_id] || 0) + item.cost_usd;
        }

        return {
            data: Array.from(dateMap.values()).sort((a, b) =>
                (a.date as string).localeCompare(b.date as string)
            ),
            models: Array.from(allModels),
        };
    })();

    const formatCurrency = (value: number) => {
        if (value < 0.01) return `$${value.toFixed(4)}`;
        return `$${value.toFixed(2)}`;
    };

    const formatTokens = (value: number) => {
        if (value >= 1000000) return `${(value / 1000000).toFixed(1)}M`;
        if (value >= 1000) return `${(value / 1000).toFixed(1)}K`;
        return value.toString();
    };

    return (
        <div className={styles.container}>
            <header className={styles.header}>
                <button
                    className={styles.backButton}
                    onClick={() => window.location.href = '/'}
                >
                    <ArrowLeft size={20} />
                    Back
                </button>
                <h1 className={styles.title}>API Usage Monitor</h1>
                <button
                    className={styles.refreshButton}
                    onClick={fetchData}
                    disabled={loading}
                >
                    <RefreshCw size={20} className={loading ? styles.spinning : ''} />
                </button>
            </header>

            {error && (
                <div className={styles.error}>
                    Error: {error}
                </div>
            )}

            {/* Filters */}
            <div className={styles.filters}>
                <div className={styles.filterGroup}>
                    <label>Period:</label>
                    <select
                        value={days}
                        onChange={(e) => setDays(Number(e.target.value))}
                        className={styles.select}
                    >
                        <option value={7}>Last 7 days</option>
                        <option value={30}>Last 30 days</option>
                        <option value={90}>Last 90 days</option>
                    </select>
                </div>
                <div className={styles.filterGroup}>
                    <label>Persona:</label>
                    <select
                        value={selectedPersona}
                        onChange={(e) => setSelectedPersona(e.target.value)}
                        className={styles.select}
                    >
                        <option value="">All Personas</option>
                        {personas.map((p) => (
                            <option key={p.persona_id} value={p.persona_id}>
                                {p.persona_name}
                            </option>
                        ))}
                    </select>
                </div>
            </div>

            {/* Summary Cards */}
            {summary && (
                <div className={styles.summaryCards}>
                    <div className={styles.card}>
                        <div className={styles.cardLabel}>Total Cost</div>
                        <div className={styles.cardValue}>{formatCurrency(summary.total_cost_usd)}</div>
                    </div>
                    <div className={styles.card}>
                        <div className={styles.cardLabel}>Input Tokens</div>
                        <div className={styles.cardValue}>{formatTokens(summary.total_input_tokens)}</div>
                    </div>
                    <div className={styles.card}>
                        <div className={styles.cardLabel}>Output Tokens</div>
                        <div className={styles.cardValue}>{formatTokens(summary.total_output_tokens)}</div>
                    </div>
                    <div className={styles.card}>
                        <div className={styles.cardLabel}>API Calls</div>
                        <div className={styles.cardValue}>{summary.call_count.toLocaleString()}</div>
                    </div>
                </div>
            )}

            {/* Chart */}
            <div className={styles.chartContainer}>
                <h2 className={styles.chartTitle}>Daily Cost by Model</h2>
                {chartData.data.length > 0 ? (
                    <ResponsiveContainer width="100%" height={400}>
                        <BarChart data={chartData.data}>
                            <CartesianGrid strokeDasharray="3 3" stroke="#444" />
                            <XAxis
                                dataKey="date"
                                tick={{ fill: '#ccc', fontSize: 12 }}
                                tickFormatter={(value) => {
                                    const d = new Date(value);
                                    return `${d.getMonth() + 1}/${d.getDate()}`;
                                }}
                            />
                            <YAxis
                                tick={{ fill: '#ccc', fontSize: 12 }}
                                tickFormatter={(value) => `$${value.toFixed(2)}`}
                            />
                            <Tooltip
                                contentStyle={{ backgroundColor: '#2a2a2a', border: '1px solid #444' }}
                                labelStyle={{ color: '#fff' }}
                                formatter={(value: number, name: string) => [
                                    formatCurrency(value),
                                    name,
                                ]}
                            />
                            <Legend />
                            {chartData.models.map((model) => (
                                <Bar
                                    key={model}
                                    dataKey={model}
                                    stackId="a"
                                    fill={getModelColor(model)}
                                    name={model}
                                />
                            ))}
                        </BarChart>
                    </ResponsiveContainer>
                ) : (
                    <div className={styles.noData}>
                        {loading ? 'Loading...' : 'No usage data available'}
                    </div>
                )}
            </div>
        </div>
    );
}
