import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import { useEffect, useReducer, useRef, useState, type ReactNode } from 'react';
import styles from './VrmAvatar.module.css';
import type { AvatarFramingMode, VrmAvatarStatus, VrmAvatarView } from '@/lib/vrmAvatarMath';
import type { VrmAvatarLease } from '@/lib/vrmAvatarEngine';

// ⚠ lib/vrmAvatarEngine (three と @pixiv/three-vrm を読むモジュール) は、型だけを
// import type で読み、実物は下の loadEngine の動的 import でだけ読む。この部品の枠が
// 画面に見えるまで loadEngine を呼ばないので、three はそれまで読み込まれない
// (docs/intent/screen_avatar.md 必ず守ること 9)。
type EngineModule = typeof import('@/lib/vrmAvatarEngine');
let enginePromise: Promise<EngineModule> | null = null;
function loadEngine(): Promise<EngineModule> {
    if (!enginePromise) {
        enginePromise = import('@/lib/vrmAvatarEngine').catch(err => {
            enginePromise = null;
            throw err;
        });
    }
    return enginePromise;
}

/** 利用者が閉じた失敗の知らせ (URL と理由の組)。狭い画面ではログに重ねるルームビューが
 * 喋るたびにマウントし直されるので、部品の中ではなくここに持ち、同じ知らせを出し直さない。 */
const dismissedFailures = new Set<string>();

const IDLE_VIEW: VrmAvatarView = { holding: false, status: { phase: 'idle' } };

interface VrmAvatarProps {
    /** VRM を返す API の URL (Occupant.avatar_vrm_url)。 */
    url: string;
    /** ペルソナの名前 (canvas の読み上げ用の名前)。 */
    name: string;
    framing: AvatarFramingMode;
    /** このペルソナがいま喋っているか。喋っている間だけ口を開け閉めする。 */
    speaking: boolean;
    /** このペルソナの声が鳴っているか。鳴っている間は、口の開きを声の音量に合わせる。 */
    voiced: boolean;
    /** 3D が見えていない間 (読み込み中・失敗・別の部品が canvas を持っている) に出す立ち絵。 */
    fallback: ReactNode;
    /** 3D が見えている間に、canvas の上に重ねて出すもの (名前の札)。 */
    overlay?: ReactNode;
}

/** ルームビューの 3D アバター (docs/intent/screen_avatar.md 設計 3)。
 * 読み込んだ VRM は lib/vrmAvatarEngine が一組だけ持ち、この部品はそれを借りて、
 * 自分の枠の中に canvas を置く。この部品の枠は、置かれた親 (ルームビューの枠) の全体を覆う。
 * canvas を持てない間は fallback (立ち絵のカード) を出す。 */
export default function VrmAvatar({ url, name, framing, speaking, voiced, fallback, overlay }: VrmAvatarProps) {
    useLocale();
    const rootRef = useRef<HTMLDivElement>(null);
    const hostRef = useRef<HTMLDivElement>(null);
    const leaseRef = useRef<VrmAvatarLease | null>(null);
    // IntersectionObserver の無いブラウザでは、見えているものとして扱う。
    const [visible, setVisible] = useState(() => typeof IntersectionObserver === 'undefined');
    const [engine, setEngine] = useState<EngineModule | null>(null);
    const [engineError, setEngineError] = useState<string | null>(null);
    const [view, setView] = useState<VrmAvatarView>(IDLE_VIEW);
    const [, rerender] = useReducer((n: number) => n + 1, 0);

    // この部品の枠が画面に見えているか。右サイドバーが引っ込んでいる (display: none か
    // 画面の外へずらしてある) 間や、スクロールで枠が外に出ている間は見えていない。
    useEffect(() => {
        const el = rootRef.current;
        if (!el || typeof IntersectionObserver === 'undefined') return;
        const observer = new IntersectionObserver(entries => {
            const last = entries[entries.length - 1];
            if (last) setVisible(last.isIntersecting);
        });
        observer.observe(el);
        return () => observer.disconnect();
    }, []);

    // 初めて見えたときに、3D のライブラリを読み込む。
    useEffect(() => {
        if (!visible || engine || engineError !== null) return;
        let cancelled = false;
        loadEngine().then(
            mod => { if (!cancelled) setEngine(mod); },
            err => { if (!cancelled) setEngineError(err instanceof Error ? err.message : ''); },
        );
        return () => { cancelled = true; };
    }, [visible, engine, engineError]);

    // VRM を借りる。URL が替わったら、借り直す。
    useEffect(() => {
        if (!engine) return;
        const lease = engine.borrowVrmAvatar(url, setView);
        leaseRef.current = lease;
        return () => {
            lease.release();
            if (leaseRef.current === lease) leaseRef.current = null;
        };
    }, [engine, url]);

    // この部品の様子を知らせる (借りた直後にも一度知らせる — 上の effect の後に走る)。
    useEffect(() => {
        leaseRef.current?.update({ host: hostRef.current, visible, framing, speaking, voiced });
    }, [engine, url, visible, framing, speaking, voiced]);

    const status: VrmAvatarStatus = engineError !== null
        ? { phase: 'failed', reason: 'engine', detail: engineError || undefined }
        : view.status;
    const show3d = engineError === null && view.holding;
    const failureKey = status.phase === 'failed' ? `${url}|${status.reason}` : null;
    const showNotice = failureKey !== null && !dismissedFailures.has(failureKey);

    return (
        <div ref={rootRef} className={styles.stage}>
            <div
                ref={hostRef}
                className={`${styles.canvasHost} ${show3d ? styles.canvasShown : ''}`}
                {...(show3d
                    ? { role: 'img', 'aria-label': uiText("components.RoomView.alt002", { p1: name }) }
                    : { 'aria-hidden': true })}
            />
            {show3d ? overlay : fallback}
            {status.phase === 'loading' && (
                <div className={styles.progress} role="status">
                    {status.progress !== null
                        ? uiText("components.VrmAvatar.loadingPercent", { p1: String(status.progress) })
                        : uiText("components.VrmAvatar.loading")}
                </div>
            )}
            {status.phase === 'failed' && showNotice && (
                <button
                    type="button"
                    className={styles.notice}
                    onClick={() => {
                        if (failureKey) dismissedFailures.add(failureKey);
                        rerender();
                    }}
                    title={uiText("components.VrmAvatar.dismiss")}
                    aria-label={`${uiText("components.VrmAvatar.failed")} ${failureReasonText(status.reason, status.detail)} ${uiText("components.VrmAvatar.dismiss")}`}
                >
                    <span className={styles.noticeTitle}>{uiText("components.VrmAvatar.failed")}</span>
                    <span className={styles.noticeReason}>{failureReasonText(status.reason, status.detail)}</span>
                    <span className={styles.noticeClose} aria-hidden="true">×</span>
                </button>
            )}
        </div>
    );
}

function failureReasonText(reason: Extract<VrmAvatarStatus, { phase: 'failed' }>['reason'], detail?: string): string {
    switch (reason) {
        case 'webgl': return uiText("components.VrmAvatar.reasonWebgl");
        case 'http':
            if (detail === '404') return uiText("components.VrmAvatar.reasonNotFound");
            return uiText("components.VrmAvatar.reasonHttp", { p1: detail ?? '' });
        case 'network': return uiText("components.VrmAvatar.reasonNetwork");
        case 'parse': return uiText("components.VrmAvatar.reasonParse");
        case 'notVrm': return uiText("components.VrmAvatar.reasonNotVrm");
        case 'contextLost': return uiText("components.VrmAvatar.reasonContextLost");
        case 'engine': return uiText("components.VrmAvatar.reasonEngine");
    }
}
