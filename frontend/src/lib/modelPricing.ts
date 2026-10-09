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
