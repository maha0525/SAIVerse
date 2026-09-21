// Validate the editable table and every statically referenced key before shipping.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const table = JSON.parse(fs.readFileSync(path.join(root, 'frontend/src/i18n/messages.json'), 'utf8'));
const locales = JSON.parse(fs.readFileSync(path.join(root, 'frontend/src/i18n/locales.json'), 'utf8'));
const errors = [];
const usedKeys = new Set();
const placeholders = text => [...new Set(text.match(/(?<!\$)\{\w+\}/g) || [])].sort().join(',');
for (const [key, row] of Object.entries(table)) {
    for (const locale of Object.keys(locales)) {
        if (typeof row[locale] !== 'string' || !row[locale]) errors.push(`${key}: missing ${locale}`);
        else if (placeholders(row[locale]) !== placeholders(row.ja)) errors.push(`${key}: ${locale} variables differ`);
    }
}

const cjkRegex = /[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\u3001-\u303f\uff01-\uff5e]/;
function stripComments(code) {
    let clean = code.replace(/\{\/\*[\s\S]*?\*\/\}/g, match => ' '.repeat(match.length));
    clean = clean.replace(/\/\*[\s\S]*?\*\//g, match => match.split('\n').map(l => ' '.repeat(l.length)).join('\n'));
    clean = clean.replace(/\/\/.*$/gm, match => ' '.repeat(match.length));
    return clean;
}

function walk(dir, checkLiterals = false) {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        if (['node_modules', '.next', '__pycache__', 'addon-panels'].includes(entry.name)) continue;
        const file = path.join(dir, entry.name);
        if (entry.isDirectory()) {
            // Do not scan for hardcoded CJK literals inside i18n directory
            walk(file, checkLiterals && entry.name !== 'i18n');
        } else if (/\.(tsx?|py)$/.test(file)) {
            const source = fs.readFileSync(file, 'utf8');
            for (const match of source.matchAll(/\b(?:uiText|ui_message|t)\(\s*['"]([\w.]+)['"]/g)) {
                usedKeys.add(match[1]);
                if (!table[match[1]]) errors.push(`${path.relative(root, file)}: unknown ${match[1]}`);
            }
            if (checkLiterals && /\.(tsx?)$/.test(file)) {
                const clean = stripComments(source);
                const rawLines = source.split('\n');
                const cleanLines = clean.split('\n');
                cleanLines.forEach((line, idx) => {
                    if (cjkRegex.test(line)) {
                        errors.push(`${path.relative(root, file)}:${idx + 1}: unlocalized text: ${rawLines[idx].trim()}`);
                    }
                });
            }
        }
    }
}

walk(path.join(root, 'frontend/src'), true);
walk(path.join(root, 'api'), false);

// Detect unused keys in messages.json (excluding api.* keys which belong to backend)
for (const key of Object.keys(table)) {
    if (!key.startsWith('api.') && !usedKeys.has(key)) {
        errors.push(`unused message key: ${key}`);
    }
}

if (errors.length) {
    console.error(`i18n validation failed with ${errors.length} error(s):\n` + errors.join('\n'));
    process.exitCode = 1;
} else {
    console.log(`Validated ${Object.keys(table).length} messages in ${Object.keys(locales).join(', ')}, verified source references, no unused frontend keys, and zero unlocalized CJK literals.`);
}

