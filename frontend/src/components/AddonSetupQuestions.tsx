'use client';
import { t as uiText } from '@/i18n/core';
import { useLocale } from '@/i18n/useLocale';
import { resolveI18nText, I18nTextValue } from '@/i18n/resolve';

import React, { useId } from 'react';
import { Lock } from 'lucide-react';
import styles from './AddonSetupQuestions.module.css';

// ---------------------------------------------------------------------------
// 導入時の質問 (manifest の setup.options) と、答えに応じて実行される step。
// 契約は docs/intent/addon_catalog_management.md の「導入時の質問と、
// アドオン専用の Python 環境」の節。prepare / options の API が返す形を写す。
// ---------------------------------------------------------------------------

export interface SetupChoice {
    id: string;
    label: I18nTextValue;
    default?: boolean;
    /** 保存済みの答えに含まれる選択肢 (導入済みのアドオンで質問を開き直したとき)。外せない。 */
    selected?: boolean;
}

export interface SetupQuestion {
    id: string;
    question: I18nTextValue;
    multiple: boolean;
    choices: SetupChoice[];
}

export interface SetupStep {
    name: I18nTextValue;
    /** {question_id: choice_id}。null / 未指定なら常に実行される。 */
    when?: Record<string, string> | null;
    /** アドオン専用の Python 環境の名前。null / 未指定なら本体の venv。 */
    env?: string | null;
}

export interface SetupPlan {
    questions: SetupQuestion[];
    steps: SetupStep[];
}

/** {question_id: [choice_id, ...]}。一つだけ選ぶ質問も要素 1 の一覧。 */
export type SetupAnswers = Record<string, string[]>;

/**
 * 「どの step を一覧に出すか」の決め方。
 * - all:   答えに当てはまる step を全部 (導入・更新)
 * - added: 前に選んだ答えでは当てはまらず、いまの答えで新しく当てはまった
 *          `when` 付きの step だけ (導入済みのアドオンへ選択肢を足すとき。
 *          `when` の無い step はやり直さない)
 */
export type StepListMode = 'all' | 'added';

/** 前に選んだ答え (`selected: true` の選択肢) */
export function previousAnswers(plan: SetupPlan): SetupAnswers {
    const result: SetupAnswers = {};
    for (const q of plan.questions) {
        result[q.id] = q.choices.filter((c) => c.selected).map((c) => c.id);
    }
    return result;
}

/** ダイアログを開いたときの答え: 前に選んだもの + `default: true` のもの */
export function initialAnswers(plan: SetupPlan): SetupAnswers {
    const result: SetupAnswers = {};
    for (const q of plan.questions) {
        const selected = q.choices.filter((c) => c.selected).map((c) => c.id);
        if (selected.length > 0) {
            // 前の答えがある質問は、その答えから始める (default は足さない —
            // 足すと、利用者が選んでいない選択肢の step が黙って走る)
            result[q.id] = q.multiple ? selected : selected.slice(0, 1);
            continue;
        }
        const defaults = q.choices.filter((c) => c.default).map((c) => c.id);
        result[q.id] = q.multiple ? defaults : defaults.slice(0, 1);
    }
    return result;
}

/** 前の答えがあり、導入後には変えられない「一つだけ選ぶ質問」か */
function isLockedSingle(q: SetupQuestion): boolean {
    return !q.multiple && q.choices.some((c) => c.selected);
}

type StepMatch = 'yes' | 'no' | 'unknown';

/**
 * step の `when` が答えに当てはまるか。複数の条件は全部当てはまるときだけ。
 * `when` が画面に出ていない質問を指しているときは、ここでは判定できない
 * (サーバーが保存済みの答えで決める) ので 'unknown' を返す。
 */
function matchStep(step: SetupStep, answers: SetupAnswers, knownQuestions: Set<string>): StepMatch {
    const when = step.when;
    if (!when) return 'yes';
    let unknown = false;
    for (const [qid, cid] of Object.entries(when)) {
        if (!knownQuestions.has(qid)) {
            unknown = true;
            continue;
        }
        if (!(answers[qid] ?? []).includes(cid)) return 'no';
    }
    return unknown ? 'unknown' : 'yes';
}

export interface VisibleStep {
    step: SetupStep;
    dependsOnPrevious: boolean;
}

export function visibleSteps(plan: SetupPlan, answers: SetupAnswers, mode: StepListMode): VisibleStep[] {
    const known = new Set(plan.questions.map((q) => q.id));
    const before = previousAnswers(plan);
    const result: VisibleStep[] = [];
    for (const step of plan.steps) {
        const now = matchStep(step, answers, known);
        if (now === 'no') continue;
        if (mode === 'added') {
            if (!step.when) continue;
            if (matchStep(step, before, known) !== 'no') continue;
        }
        result.push({ step, dependsOnPrevious: now === 'unknown' });
    }
    return result;
}

/** 承認してよい答えか: 一つだけ選ぶ質問には必ず一つ答えている */
export function answersComplete(plan: SetupPlan, answers: SetupAnswers): boolean {
    return plan.questions.every((q) => q.multiple || (answers[q.id] ?? []).length === 1);
}

/** 前に選んだ答えに、新しい選択肢が一つでも足されているか */
export function hasNewSelection(plan: SetupPlan, answers: SetupAnswers): boolean {
    const before = previousAnswers(plan);
    return plan.questions.some((q) =>
        (answers[q.id] ?? []).some((cid) => !(before[q.id] ?? []).includes(cid)),
    );
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface Props {
    plan: SetupPlan;
    answers: SetupAnswers;
    onChange: (answers: SetupAnswers) => void;
    stepListMode: StepListMode;
}

export default function AddonSetupQuestions({ plan, answers, onChange, stepListMode }: Props) {
    const currentLocale = useLocale();
    const groupId = useId();

    const toggleMultiple = (q: SetupQuestion, choiceId: string, checked: boolean) => {
        const current = answers[q.id] ?? [];
        const next = checked
            ? (current.includes(choiceId) ? current : [...current, choiceId])
            : current.filter((c) => c !== choiceId);
        // 選択肢の並びは manifest の順に揃えて送る
        const ordered = q.choices.map((c) => c.id).filter((id) => next.includes(id));
        onChange({ ...answers, [q.id]: ordered });
    };

    const chooseSingle = (q: SetupQuestion, choiceId: string) => {
        onChange({ ...answers, [q.id]: [choiceId] });
    };

    const steps = visibleSteps(plan, answers, stepListMode);

    return (
        <div className={styles.container}>
            <div data-i18n="components.AddonSetupQuestions.text001" className={styles.sectionLabel}>{uiText("components.AddonSetupQuestions.text001")}</div>
            {plan.questions.map((q) => {
                const lockedSingle = isLockedSingle(q);
                const chosen = answers[q.id] ?? [];
                const unanswered = !q.multiple && chosen.length === 0;
                return (
                    <fieldset key={q.id} className={styles.question}>
                        <legend className={styles.questionText}>
                            {resolveI18nText(q.question, currentLocale, null, null, q.id)}
                        </legend>
                        <div data-i18n="components.AddonSetupQuestions.text002 components.AddonSetupQuestions.text003" className={styles.questionHint}>
                            {q.multiple ? uiText("components.AddonSetupQuestions.text002") : uiText("components.AddonSetupQuestions.text003")}
                        </div>
                        <div className={styles.choices}>
                            {q.choices.map((c) => {
                                const locked = !!c.selected;
                                const disabled = lockedSingle || (q.multiple && locked);
                                const checked = chosen.includes(c.id);
                                return (
                                    <label
                                        key={c.id}
                                        className={`${styles.choice} ${checked ? styles.choiceChecked : ''} ${disabled ? styles.choiceDisabled : ''}`}
                                    >
                                        <input
                                            type={q.multiple ? 'checkbox' : 'radio'}
                                            name={`${groupId}-${q.id}`}
                                            checked={checked}
                                            disabled={disabled}
                                            onChange={(e) => (q.multiple
                                                ? toggleMultiple(q, c.id, e.target.checked)
                                                : chooseSingle(q, c.id))}
                                        />
                                        <span className={styles.choiceLabel}>
                                            {resolveI18nText(c.label, currentLocale, null, null, c.id)}
                                        </span>
                                        {locked && (
                                            <span data-i18n="components.AddonSetupQuestions.text004" className={styles.lockedTag}>
                                                <Lock size={10} />{uiText("components.AddonSetupQuestions.text004")}
                                            </span>
                                        )}
                                    </label>
                                );
                            })}
                        </div>
                        {lockedSingle && (
                            <div data-i18n="components.AddonSetupQuestions.text005" className={styles.questionHint}>{uiText("components.AddonSetupQuestions.text005")}</div>
                        )}
                        {unanswered && (
                            <div data-i18n="components.AddonSetupQuestions.text012" className={styles.warnHint}>{uiText("components.AddonSetupQuestions.text012")}</div>
                        )}
                    </fieldset>
                );
            })}

            <div className={styles.steps}>
                <div data-i18n="components.AddonSetupQuestions.text006 components.AddonSetupQuestions.text007" className={styles.sectionLabel}>
                    {stepListMode === 'added'
                        ? uiText("components.AddonSetupQuestions.text007")
                        : uiText("components.AddonSetupQuestions.text006")}
                </div>
                {steps.length === 0 ? (
                    <div data-i18n="components.AddonSetupQuestions.text008 components.AddonSetupQuestions.text009" className={styles.emptySteps}>
                        {stepListMode === 'added'
                            ? uiText("components.AddonSetupQuestions.text009")
                            : uiText("components.AddonSetupQuestions.text008")}
                    </div>
                ) : (
                    <ol className={styles.stepList}>
                        {steps.map(({ step, dependsOnPrevious }, i) => (
                            <li key={i} className={styles.stepItem}>
                                <span>{resolveI18nText(step.name, currentLocale)}</span>
                                {step.env && (
                                    <span data-i18n="components.AddonSetupQuestions.text010" className={styles.envTag}>
                                        {uiText("components.AddonSetupQuestions.text010", { p1: step.env })}
                                    </span>
                                )}
                                {dependsOnPrevious && (
                                    <span data-i18n="components.AddonSetupQuestions.text011" className={styles.dependsTag}>
                                        {uiText("components.AddonSetupQuestions.text011")}
                                    </span>
                                )}
                            </li>
                        ))}
                    </ol>
                )}
            </div>
        </div>
    );
}
