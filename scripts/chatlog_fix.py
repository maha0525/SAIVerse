#!/usr/bin/env python3
"""
chatlog_fix.py  ― Chrome 拡張でエクスポートした Markdown 会話ログを
1) 重複バグを除去した _fixed.md
2) 読みやすい _readable.txt（## まはー: / ## エリス: などに置換）
3) チャットボット用 _importable.json
に変換して保存します。

使い方（※コマンドは 1 行で）:
python chatlog_fix.py input.md --user_name まはー --ai_name エリス --persona_id eris
"""

import argparse
import json
import pathlib
from typing import List


def parse_blocks(text: str) -> List[str]:
    """'## Prompt:' または '## Response:' 行に続く本文をブロック単位で取り出す"""
    blocks, buf = [], []
    skip_leading_blank = True  # skip blank lines before first block content if file starts without header
    for raw_line in text.splitlines():
        line = raw_line.lstrip('\ufeff')  # remove BOM if present on the first line
        if line in ('## Prompt:', '## Response:'):
            if buf:
                blocks.append('\n'.join(buf).rstrip())
                buf = []
            skip_leading_blank = True
        else:
            if skip_leading_blank and not line.strip():
                continue
            skip_leading_blank = False
            buf.append(line)
    if buf:
        blocks.append('\n'.join(buf).rstrip())
    return blocks


def deduplicate(blocks: List[str]) -> List[str]:
    """同一発言が連続した場合に 1 つへまとめる（先頭・末尾空白は無視）"""
    out = []
    for blk in blocks:
        if not out or out[-1].strip() != blk.strip():
            out.append(blk)
    return out


def realign(blocks: List[str]) -> List[str]:
    """偶数番目をユーザ、奇数番目を AI として再整列し、余りがあれば切り捨て"""
    return blocks[:len(blocks) - (len(blocks) % 2)]  # 奇数個なら最後の孤立 Prompt を除外


def write_fixed_md(blocks: List[str], path: pathlib.Path):
    with path.open('w', encoding='utf-8') as f:
        for i, txt in enumerate(blocks):
            hdr = '## Prompt:' if i % 2 == 0 else '## Response:'
            f.write(f'{hdr}\n{txt}\n\n')


def write_readable_md(blocks: List[str], path: pathlib.Path, user_name: str, ai_name: str):
    with path.open('w', encoding='utf-8') as f:
        for i, txt in enumerate(blocks):
            hdr = f'## {user_name}:' if i % 2 == 0 else f'## {ai_name}:'
            f.write(f'{hdr}\n{txt}\n\n')


def write_json(blocks: List[str], path: pathlib.Path, persona_id: str):
    msgs = []
    for i, txt in enumerate(blocks):
        if i % 2 == 0:
            msgs.append({"role": "user", "content": txt})
        else:
            msgs.append({"role": "assistant", "content": txt, "persona_id": persona_id})
    path.write_text(json.dumps(msgs, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('input_file', help='元の Markdown ログ')
    ap.add_argument('--user_name', default='まはー', help='読みやすい形式で表示するユーザー名')
    ap.add_argument('--ai_name', default='エリス', help='読みやすい形式で表示するAI名')
    ap.add_argument('--persona_id', default='eris', help='JSON に入れる persona_id')
    args = ap.parse_args()

    src = pathlib.Path(args.input_file)
    stem = src.stem
    text = src.read_text(encoding='utf-8')

    blocks = realign(deduplicate(parse_blocks(text)))

    write_fixed_md(blocks, src.with_name(f'{stem}_fixed.md'))
    write_readable_md(blocks, src.with_name(f'{stem}_readable.txt'), args.user_name, args.ai_name)
    write_json(blocks, src.with_name(f'{stem}_importable.json'), args.persona_id)


if __name__ == '__main__':
    main()
