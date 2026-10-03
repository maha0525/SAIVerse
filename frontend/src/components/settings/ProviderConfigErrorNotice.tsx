import { t as uiText } from '@/i18n/core';
import styles from './ProviderConfigErrorNotice.module.css';

export interface ProviderConfigError {
    path: string;
    source: string;
    reason: string;
}

export default function ProviderConfigErrorNotice({ error }: { error: ProviderConfigError }) {
    return (
        <div role="alert" className={styles.notice}>
            <strong>{uiText("providerConfig.invalid")}</strong>
            <div>{error.path}</div>
            <div>{error.source}: {error.reason}</div>
            <div>{uiText("providerConfig.repair")}</div>
        </div>
    );
}
