"use client";
import { apiFetch } from '@/i18n/api';

import { getFormatLocale } from '@/i18n/core';

import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';


import { useEffect, useState } from 'react';
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from 'recharts';
import { ArrowLeft, RefreshCw, ChevronDown, ChevronUp } from 'lucide-react';
import { formatCost } from '@/lib/formatCost';
import styles from './page.module.css';

interface CostByCurrency {
    currency: string;
    total_cost: number;
}

interface UsageSummary {
    total_cost_usd: number;
    costs_by_currency?: CostByCurrency[];
    total_input_tokens: number;
    total_output_tokens: number;
    call_count: number;
}

interface DailyUsage {
    date: string;
    model_id: string;
    model_display_name: string;
    cost_usd: number;
    currency?: string;
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
    costs_by_currency?: CostByCurrency[];
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
    useLocale();
    const [summary, setSummary] = useState<UsageSummary | null>(null);
    const [dailyData, setDailyData] = useState<DailyUsage[]>([]);
    const [personas, setPersonas] = useState<Persona[]>([]);
    const [categories, setCategories] = useState<Category[]>([]);
    const [categoryUsage, setCategoryUsage] = useState<CategoryUsage[]>([]);
    const [selectedPersona, setSelectedPersona] = useState<string>('');
    const [selectedCategory, setSelectedCategory] = useState<string>('');
    const [selectedCurrency, setSelectedCurrency] = useState<string>('');
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
            const startDate = new Date();
            startDate.setDate(startDate.getDate() - days);
            const startDateStr = startDate.toISOString().split('T')[0];

            const [summaryRes, dailyRes, personasRes, categoriesRes, categoryUsageRes] = await Promise.all([
                apiFetch(`/api/usage/summary?days=${days}${personaParam}${categoryParam}`),
                apiFetch(`/api/usage/daily?start_date=${startDateStr}${personaParam}${categoryParam}`),
                apiFetch('/api/usage/personas'),
                apiFetch('/api/usage/categories'),
                apiFetch(`/api/usage/by-category?days=${days}${personaParam}`),
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

    // Currencies present in the daily data, ordered by total cost (desc) so the
    // most significant currency becomes the default selection.
    const availableCurrencies = (() => {
        const totals = new Map<string, number>();
        for (const item of dailyData) {
            const cur = item.currency || 'USD';
            totals.set(cur, (totals.get(cur) || 0) + item.cost_usd);
        }
        return Array.from(totals.entries())
            .sort((a, b) => b[1] - a[1])
            .map(([cur]) => cur);
    })();

    // Resolve the currency to display: honor the user's pick if it still exists
    // in the current data, otherwise fall back to the top currency (or USD).
    const effectiveCurrency =
        selectedCurrency && availableCurrencies.includes(selectedCurrency)
            ? selectedCurrency
            : (availableCurrencies[0] || 'USD');

    // Transform daily data for stacked bar chart.
    // Only the selected currency's rows are aggregated — costs in different
    // currencies must never be summed onto the same axis.
    const chartData = (() => {
        const dateMap = new Map<string, Record<string, string | number>>();
        const allModels = new Set<string>();

        for (const item of dailyData) {
            // Register the date before any filtering so days whose usage is
            // all $0 still keep their tick on the X axis (only their bars are
            // empty), instead of the day disappearing from the chart.
            if (!dateMap.has(item.date)) {
                dateMap.set(item.date, { date: item.date });
            }
            if ((item.currency || 'USD') !== effectiveCurrency) continue;
            // $0 rows (free/local models, unpriced models) are real usage but
            // pure noise on a cost chart: skipping them here also drops the
            // model from the legend and from day tooltips. Negative rows
            // (future cache-refund corrections) must still pass.
            if (item.cost_usd === 0) continue;
            allModels.add(item.model_id);
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

    const formatCostsByCurrency = (costs?: CostByCurrency[]): string => {
        if (!costs || costs.length === 0) return formatCost(0);
        return costs.map(c => formatCost(c.total_cost, c.currency)).join(' + ');
    };

    const formatTokens = (value: number) => {
        if (value >= 1000000) return `${(value / 1000000).toFixed(1)}M`;
        if (value >= 1000) return `${(value / 1000).toFixed(1)}K`;
        return value.toString();
    };

    return (
        <div className={styles.container}>
            <header className={styles.header}>
                <button data-i18n="app.usage.page.text001"
                    className={styles.backButton}
                    onClick={() => window.location.href = '/'}
                >
                    <ArrowLeft size={20} />{uiText("app.usage.page.text001")}</button>
                <h1 data-i18n="app.usage.page.text002" className={styles.title}>{uiText("app.usage.page.text002")}</h1>
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
                    {uiText("app.usage.page.label001")}{error}
                </div>
            )}

            {/* Filters */}
            <div className={styles.filters}>
                <div className={styles.filterGroup}>
                    <label data-i18n="app.usage.page.text003">{uiText("app.usage.page.text003")}</label>
                    <select
                        value={days}
                        onChange={(e) => setDays(Number(e.target.value))}
                        className={styles.select}
                    >
                        <option data-i18n="app.usage.page.text004" value={7}>{uiText("app.usage.page.text004")}</option>
                        <option data-i18n="app.usage.page.text005" value={30}>{uiText("app.usage.page.text005")}</option>
                        <option data-i18n="app.usage.page.text006" value={90}>{uiText("app.usage.page.text006")}</option>
                    </select>
                </div>
                <div className={styles.filterGroup}>
                    <label data-i18n="app.usage.page.text007">{uiText("app.usage.page.text007")}</label>
                    <select
                        value={selectedPersona}
                        onChange={(e) => setSelectedPersona(e.target.value)}
                        className={styles.select}
                    >
                        <option data-i18n="app.usage.page.text008" value="">{uiText("app.usage.page.text008")}</option>
                        {personas.map((p) => (
                            <option key={p.persona_id} value={p.persona_id}>
                                {p.persona_name}
                            </option>
                        ))}
                    </select>
                </div>
                <div className={styles.filterGroup}>
                    <label data-i18n="app.usage.page.text009">{uiText("app.usage.page.text009")}</label>
                    <select
                        value={selectedCategory}
                        onChange={(e) => setSelectedCategory(e.target.value)}
                        className={styles.select}
                    >
                        <option data-i18n="app.usage.page.text010" value="">{uiText("app.usage.page.text010")}</option>
                        {categories.map((c) => (
                            <option key={c.category_id} value={c.category_id}>
                                {c.category_name}
                            </option>
                        ))}
                    </select>
                </div>
                {availableCurrencies.length > 1 && (
                    <div className={styles.filterGroup}>
                        <label data-i18n="app.usage.page.text011">{uiText("app.usage.page.text011")}</label>
                        <select
                            value={effectiveCurrency}
                            onChange={(e) => setSelectedCurrency(e.target.value)}
                            className={styles.select}
                        >
                            {availableCurrencies.map((cur) => (
                                <option key={cur} value={cur}>
                                    {cur}
                                </option>
                            ))}
                        </select>
                    </div>
                )}
            </div>

            {/* Summary Cards */}
            {summary && (
                <div className={styles.summaryCards}>
                    <div className={styles.card}>
                        <div data-i18n="app.usage.page.text012" className={styles.cardLabel}>{uiText("app.usage.page.text012")}</div>
                        <div className={styles.cardValue}>{formatCostsByCurrency(summary.costs_by_currency)}</div>
                    </div>
                    <div className={styles.card}>
                        <div data-i18n="app.usage.page.text013" className={styles.cardLabel}>{uiText("app.usage.page.text013")}</div>
                        <div className={styles.cardValue}>{formatTokens(summary.total_input_tokens)}</div>
                    </div>
                    <div className={styles.card}>
                        <div data-i18n="app.usage.page.text014" className={styles.cardLabel}>{uiText("app.usage.page.text014")}</div>
                        <div className={styles.cardValue}>{formatTokens(summary.total_output_tokens)}</div>
                    </div>
                    <div className={styles.card}>
                        <div data-i18n="app.usage.page.text015" className={styles.cardLabel}>{uiText("app.usage.page.text015")}</div>
                        <div className={styles.cardValue}>{summary.call_count.toLocaleString(getFormatLocale())}</div>
                    </div>
                </div>
            )}

            {/* Chart */}
            <div className={styles.chartContainer}>
                <h2 data-i18n="app.usage.page.text016" className={styles.chartTitle}>{uiText("app.usage.page.text016")}</h2>
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
                                tickFormatter={(value) => formatCost(value, effectiveCurrency)}
                            />
                            <Tooltip
                                contentStyle={{ backgroundColor: '#2a2a2a', border: '1px solid #444' }}
                                labelStyle={{ color: '#fff' }}
                                formatter={(value: number, name: string) => [
                                    formatCost(value, effectiveCurrency),
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
                                    <button data-i18n="app.usage.page.text017 app.usage.page.text018 app.usage.page.text019"
                                        className={styles.legendToggle}
                                        onClick={() => setLegendExpanded(!legendExpanded)}
                                    >
                                        {legendExpanded
                                            ? <><ChevronUp size={14} />{uiText("app.usage.page.text017")}</>
                                            : <><ChevronDown size={14} />{uiText("app.usage.page.text018")}{chartData.models.length - COLLAPSE_THRESHOLD}{uiText("app.usage.page.text019")}</>
                                        }
                                    </button>
                                )}
                            </div>
                        );
                    })()}
                    </>
                ) : (
                    <div data-i18n="app.usage.page.text020 app.usage.page.text021" className={styles.noData}>
                        {loading ? uiText("app.usage.page.text020") : uiText("app.usage.page.text021")}
                    </div>
                )}
            </div>

            {/* Category Breakdown */}
            {categoryUsage.length > 0 && (
                <div className={styles.categorySection}>
                    <h2 data-i18n="app.usage.page.text022" className={styles.chartTitle}>{uiText("app.usage.page.text022")}</h2>
                    <div className={styles.categoryGrid}>
                        {categoryUsage.map((cat) => (
                            <div key={cat.category} className={styles.categoryCard}>
                                <div className={styles.categoryName}>{cat.category_name}</div>
                                <div className={styles.categoryStats}>
                                    <div className={styles.categoryStat}>
                                        <span data-i18n="app.usage.page.text023" className={styles.statLabel}>{uiText("app.usage.page.text023")}</span>
                                        <span className={styles.statValue}>{formatCostsByCurrency(cat.costs_by_currency)}</span>
                                    </div>
                                    <div className={styles.categoryStat}>
                                        <span data-i18n="app.usage.page.text024" className={styles.statLabel}>{uiText("app.usage.page.text024")}</span>
                                        <span className={styles.statValue}>{cat.call_count.toLocaleString(getFormatLocale())}</span>
                                    </div>
                                    <div className={styles.categoryStat}>
                                        <span data-i18n="app.usage.page.text025" className={styles.statLabel}>{uiText("app.usage.page.text025")}</span>
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
