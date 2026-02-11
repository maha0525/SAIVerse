"use client";

import { useEffect, useState } from 'react';
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';
import { ArrowLeft, RefreshCw, ChevronDown, ChevronUp } from 'lucide-react';
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

interface Category {
    category_id: string;
    category_name: string;
}

interface CategoryUsage {
    category: string;
    category_name: string;
    total_cost_usd: number;
    total_input_tokens: number;
    total_output_tokens: number;
    call_count: number;
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
    const [categories, setCategories] = useState<Category[]>([]);
    const [categoryUsage, setCategoryUsage] = useState<CategoryUsage[]>([]);
    const [selectedPersona, setSelectedPersona] = useState<string>('');
    const [selectedCategory, setSelectedCategory] = useState<string>('');
    const [days, setDays] = useState(30);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [legendExpanded, setLegendExpanded] = useState(false);

    const fetchData = async () => {
        setLoading(true);
        setError(null);
        try {
            const personaParam = selectedPersona ? `&persona_id=${selectedPersona}` : '';
            const categoryParam = selectedCategory ? `&category=${selectedCategory}` : '';

            const [summaryRes, dailyRes, personasRes, categoriesRes, categoryUsageRes] = await Promise.all([
                fetch(`/api/usage/summary?days=${days}${personaParam}${categoryParam}`),
                fetch(`/api/usage/daily?${personaParam}${categoryParam}`),
                fetch('/api/usage/personas'),
                fetch('/api/usage/categories'),
                fetch(`/api/usage/by-category?days=${days}${personaParam}`),
            ]);

            if (!summaryRes.ok || !dailyRes.ok) {
                throw new Error('Failed to fetch usage data');
            }

            const summaryData = await summaryRes.json();
            const dailyDataRaw = await dailyRes.json();
            const personasData = personasRes.ok ? await personasRes.json() : [];
            const categoriesData = categoriesRes.ok ? await categoriesRes.json() : [];
            const categoryUsageData = categoryUsageRes.ok ? await categoryUsageRes.json() : [];

            setSummary(summaryData);
            setDailyData(dailyDataRaw);
            setPersonas(personasData);
            setCategories(categoriesData);
            setCategoryUsage(categoryUsageData);
        } catch (err) {
            setError(err instanceof Error ? err.message : 'Unknown error');
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        fetchData();
    }, [selectedPersona, selectedCategory, days]);

    // Transform daily data for stacked bar chart
    const chartData = (() => {
        const dateMap = new Map<string, Record<string, string | number>>();
        const allModels = new Set<string>();

        for (const item of dailyData) {
            allModels.add(item.model_id);
            if (!dateMap.has(item.date)) {
                dateMap.set(item.date, { date: item.date });
            }
            const entry = dateMap.get(item.date)!;
            entry[item.model_id] = ((entry[item.model_id] as number) || 0) + item.cost_usd;
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
                    戻る
                </button>
                <h1 className={styles.title}>API 使用状況モニター</h1>
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
                    <label>期間:</label>
                    <select
                        value={days}
                        onChange={(e) => setDays(Number(e.target.value))}
                        className={styles.select}
                    >
                        <option value={7}>過去7日間</option>
                        <option value={30}>過去30日間</option>
                        <option value={90}>過去90日間</option>
                    </select>
                </div>
                <div className={styles.filterGroup}>
                    <label>ペルソナ:</label>
                    <select
                        value={selectedPersona}
                        onChange={(e) => setSelectedPersona(e.target.value)}
                        className={styles.select}
                    >
                        <option value="">全ペルソナ</option>
                        {personas.map((p) => (
                            <option key={p.persona_id} value={p.persona_id}>
                                {p.persona_name}
                            </option>
                        ))}
                    </select>
                </div>
                <div className={styles.filterGroup}>
                    <label>カテゴリ:</label>
                    <select
                        value={selectedCategory}
                        onChange={(e) => setSelectedCategory(e.target.value)}
                        className={styles.select}
                    >
                        <option value="">全カテゴリ</option>
                        {categories.map((c) => (
                            <option key={c.category_id} value={c.category_id}>
                                {c.category_name}
                            </option>
                        ))}
                    </select>
                </div>
            </div>

            {/* Summary Cards */}
            {summary && (
                <div className={styles.summaryCards}>
                    <div className={styles.card}>
                        <div className={styles.cardLabel}>合計コスト</div>
                        <div className={styles.cardValue}>{formatCurrency(summary.total_cost_usd)}</div>
                    </div>
                    <div className={styles.card}>
                        <div className={styles.cardLabel}>入力トークン</div>
                        <div className={styles.cardValue}>{formatTokens(summary.total_input_tokens)}</div>
                    </div>
                    <div className={styles.card}>
                        <div className={styles.cardLabel}>出力トークン</div>
                        <div className={styles.cardValue}>{formatTokens(summary.total_output_tokens)}</div>
                    </div>
                    <div className={styles.card}>
                        <div className={styles.cardLabel}>API呼び出し</div>
                        <div className={styles.cardValue}>{summary.call_count.toLocaleString()}</div>
                    </div>
                </div>
            )}

            {/* Chart */}
            <div className={styles.chartContainer}>
                <h2 className={styles.chartTitle}>モデル別日次コスト</h2>
                {chartData.data.length > 0 ? (
                    <>
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
                    {/* Custom Legend */}
                    {chartData.models.length > 0 && (() => {
                        const COLLAPSE_THRESHOLD = 5;
                        const needsCollapse = chartData.models.length > COLLAPSE_THRESHOLD;
                        const visibleModels = needsCollapse && !legendExpanded
                            ? chartData.models.slice(0, COLLAPSE_THRESHOLD)
                            : chartData.models;
                        return (
                            <div className={styles.legend}>
                                <div className={styles.legendItems}>
                                    {visibleModels.map((model) => (
                                        <span key={model} className={styles.legendItem}>
                                            <span
                                                className={styles.legendSwatch}
                                                style={{ background: getModelColor(model) }}
                                            />
                                            {model}
                                        </span>
                                    ))}
                                </div>
                                {needsCollapse && (
                                    <button
                                        className={styles.legendToggle}
                                        onClick={() => setLegendExpanded(!legendExpanded)}
                                    >
                                        {legendExpanded
                                            ? <><ChevronUp size={14} /> 折りたたむ</>
                                            : <><ChevronDown size={14} /> 他 {chartData.models.length - COLLAPSE_THRESHOLD} モデルを表示</>
                                        }
                                    </button>
                                )}
                            </div>
                        );
                    })()}
                    </>
                ) : (
                    <div className={styles.noData}>
                        {loading ? '読み込み中...' : '使用データがありません'}
                    </div>
                )}
            </div>

            {/* Category Breakdown */}
            {categoryUsage.length > 0 && (
                <div className={styles.categorySection}>
                    <h2 className={styles.chartTitle}>カテゴリ別使用状況</h2>
                    <div className={styles.categoryGrid}>
                        {categoryUsage.map((cat) => (
                            <div key={cat.category} className={styles.categoryCard}>
                                <div className={styles.categoryName}>{cat.category_name}</div>
                                <div className={styles.categoryStats}>
                                    <div className={styles.categoryStat}>
                                        <span className={styles.statLabel}>コスト</span>
                                        <span className={styles.statValue}>{formatCurrency(cat.total_cost_usd)}</span>
                                    </div>
                                    <div className={styles.categoryStat}>
                                        <span className={styles.statLabel}>呼び出し</span>
                                        <span className={styles.statValue}>{cat.call_count.toLocaleString()}</span>
                                    </div>
                                    <div className={styles.categoryStat}>
                                        <span className={styles.statLabel}>トークン</span>
                                        <span className={styles.statValue}>
                                            {formatTokens(cat.total_input_tokens + cat.total_output_tokens)}
                                        </span>
                                    </div>
                                </div>
                            </div>
                        ))}
                    </div>
                </div>
            )}
        </div>
    );
}
