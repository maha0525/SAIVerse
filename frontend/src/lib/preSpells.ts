/**
 * pre_spells エントリ (string[]) と UI 状態 (Playbook 名 + Spell 名のリスト) の
 * 相互変換ヘルパ。バックエンドの ``_SPELL_PATTERN`` / ``_SPELL_PATTERN_NO_ARGS``
 * (sea/runtime_llm.py) と互換な形式を生成する。
 *
 * - Playbook 起動: ``/spell name='run_playbook' args={"name": "<playbook>"}``
 *   (run_playbook は confirmed 値が必要なので args 確定形)
 * - Spell 呼び出し: ``/spell name='<spell>'`` (引数省略形 →
 *   spell_args_decider Playbook で動的引数生成)
 */

const SPELL_NAME_PATTERN = /^\/spell\s+name='([^']+)'(\s+args=(.+))?\s*$/;

export interface PreSpellsState {
    /** UI で選んだ実行 Playbook 名 (run_playbook spell の name 引数)。null なら Playbook 起動なし */
    playbookName: string | null;
    /** UI で選んだ追加 Spell 名のリスト (run_playbook 以外、引数は spell_args_decider に委ねる) */
    spellNames: string[];
}

/**
 * pre_spells エントリ配列を UI 状態にパースする。
 * - ``/spell name='run_playbook' args={...}`` → playbookName = args.name
 * - ``/spell name='<other>'`` (no_args) → spellNames に追加
 * - パース失敗エントリは無視
 */
export function parsePreSpellsForUI(entries: string[] | undefined | null): PreSpellsState {
    const result: PreSpellsState = { playbookName: null, spellNames: [] };
    if (!Array.isArray(entries)) return result;

    for (const entry of entries) {
        if (typeof entry !== 'string') continue;
        const m = entry.match(SPELL_NAME_PATTERN);
        if (!m) continue;
        const name = m[1];
        const argsRaw = m[3];

        if (name === 'run_playbook' && argsRaw) {
            // 確定形: args から name フィールドを取り出す。Python の dict literal と
            // JSON は微妙に違うが、シンプルに 'name': 'X' / "name": "X" を抽出する。
            const argMatch = argsRaw.match(/['"]name['"]\s*:\s*['"]([^'"]+)['"]/);
            if (argMatch) {
                result.playbookName = argMatch[1];
            }
        } else if (!argsRaw) {
            // 引数省略形 → spell_args_decider に動的決定を委ねる Spell
            result.spellNames.push(name);
        }
        // それ以外 (run_playbook 以外で args 指定あり) は現状の UI では作らない
    }
    return result;
}

/**
 * UI 状態を pre_spells エントリ配列に組み立てる。
 * Playbook 起動が指定されていれば run_playbook spell を args 確定形で先頭に、
 * Spell リストは引数省略形で続ける (実行順は配列順)。
 */
export function buildPreSpellsFromUI(
    playbookName: string | null,
    spellNames: string[],
): string[] {
    const entries: string[] = [];
    if (playbookName && playbookName.trim()) {
        // run_playbook の args は JSON 形式で書く (バックエンドは ast.literal_eval / json.loads を試す)。
        // name 値に ' / " / \\ などが入る Playbook 名は現状想定外。
        entries.push(`/spell name='run_playbook' args={"name": "${playbookName.trim()}"}`);
    }
    for (const name of spellNames) {
        if (name && name.trim()) {
            entries.push(`/spell name='${name.trim()}'`);
        }
    }
    return entries;
}
