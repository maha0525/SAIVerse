const CURRENCY_SYMBOLS: Record<string, string> = {
    USD: '$',
    JPY: '¥',
};

export function formatCost(cost: number, currency: string = 'USD'): string {
    const sym = CURRENCY_SYMBOLS[currency] ?? currency + ' ';
    const isWhole = currency === 'JPY';

    if (cost === 0) return `${sym}0`;
    if (isWhole) {
        if (cost < 1) return `${sym}${cost.toFixed(2)}`;
        return `${sym}${Math.round(cost).toLocaleString()}`;
    }
    if (cost < 0.001) return `${sym}${cost.toFixed(6)}`;
    if (cost < 0.01) return `${sym}${cost.toFixed(4)}`;
    return `${sym}${cost.toFixed(3)}`;
}
