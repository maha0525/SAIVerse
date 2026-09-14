// Validate the editable table and every statically referenced key before shipping.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const table = JSON.parse(fs.readFileSync(path.join(root, 'frontend/src/i18n/messages.json'), 'utf8'));
const locales = JSON.parse(fs.readFileSync(path.join(root, 'frontend/src/i18n/locales.json'), 'utf8'));
const errors = [];
const placeholders = text => [...new Set(text.match(/(?<!\$)\{\w+\}/g) || [])].sort().join(',');
for (const [key, row] of Object.entries(table)) {
    for (const locale of Object.keys(locales)) {
        if (typeof row[locale] !== 'string' || !row[locale]) errors.push(`${key}: missing ${locale}`);
        else if (placeholders(row[locale]) !== placeholders(row.ja)) errors.push(`${key}: ${locale} variables differ`);
    }
}
function walk(dir) {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        if (['node_modules', '.next', '__pycache__', 'addon-panels'].includes(entry.name)) continue;
        const file = path.join(dir, entry.name);
        if (entry.isDirectory()) walk(file);
        else if (/\.(tsx?|py)$/.test(file)) {
            const source = fs.readFileSync(file, 'utf8');
            for (const match of source.matchAll(/\b(?:uiText|ui_message|t)\(\s*['"]([\w.]+)['"]/g)) {
                if (!table[match[1]]) errors.push(`${path.relative(root, file)}: unknown ${match[1]}`);
            }
        }
    }
}
walk(path.join(root, 'frontend/src'));
walk(path.join(root, 'api'));
if (errors.length) { console.error(errors.join('\n')); process.exitCode = 1; }
else console.log(`Validated ${Object.keys(table).length} messages in ${Object.keys(locales).join(', ')} and source references.`);
