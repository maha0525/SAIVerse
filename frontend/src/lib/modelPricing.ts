/** Select the output unit rate using the same exclusive input threshold as billing. */
export function outputRateForInput(
    pricing: { output_per_1m_tokens?: number; [key: string]: unknown } | undefined,
    inputTokens: number,
): number | undefined {
    const threshold = pricing?.long_context_threshold_tokens;
    const longRate = pricing?.long_context_output_per_1m_tokens;
    if (typeof threshold === 'number' && Number.isInteger(threshold)
        && inputTokens > threshold && typeof longRate === 'number') {
        return longRate;
    }
    return pricing?.output_per_1m_tokens;
}

/** Format the end of a limited-time price in the viewer's locale and time zone. */
export function formatPricePeriodEnd(endsAt: unknown, locale: string): string | null {
    if (typeof endsAt !== 'string') return null;
    const end = new Date(endsAt);
    if (Number.isNaN(end.getTime())) return null;
    return end.toLocaleString(locale, { dateStyle: 'medium', timeStyle: 'short' });
}

/** True when a snapshot's limited-time price has already ended at ``nowMs``. */
export function isPricePeriodOver(endsAt: unknown, nowMs: number): boolean {
    if (typeof endsAt !== 'string') return false;
    const end = Date.parse(endsAt);
    return Number.isFinite(end) && end <= nowMs;
}
