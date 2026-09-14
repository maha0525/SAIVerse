"""Export/import the wording table as UTF-8 CSV for spreadsheet editing.

Usage: python frontend/scripts/i18n-table.py export wording.csv
       python frontend/scripts/i18n-table.py import wording.csv
The JSON file is canonical. Imports validate all rows before replacing it.
"""
import argparse
import csv
import json
import re
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('export', 'import'))
    parser.add_argument('file', type=Path)
    args = parser.parse_args()
    folder = Path(__file__).resolve().parents[1] / 'src' / 'i18n'
    target = folder / 'messages.json'
    catalog = json.loads(target.read_text(encoding='utf-8'))
    locales = list(json.loads((folder / 'locales.json').read_text(encoding='utf-8')))
    if args.action == 'export':
        with args.file.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=['key', *locales])
            writer.writeheader()
            writer.writerows({'key': key, **row} for key, row in catalog.items())
        print(f'Exported {len(catalog)} rows to {args.file}')
        return
    rows = {}
    with args.file.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ['key', *locales]:
            raise ValueError('CSV columns must match key and locales.json in order')
        for row in reader:
            key = row.pop('key')
            if key in rows or not key:
                raise ValueError(f'Duplicate or empty key: {key}')
            variables = set(re.findall(r'(?<!\$)\{\w+\}', row['ja'] or ''))
            for locale, value in row.items():
                if not value or set(re.findall(r'(?<!\$)\{\w+\}', value)) != variables:
                    raise ValueError(f'{key}: missing text or mismatched variables in {locale}')
            rows[key] = row
    if set(rows) != set(catalog):
        raise ValueError('Import must preserve the exact message keys')
    target.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Imported {len(rows)} validated rows')


if __name__ == '__main__':
    main()
