"""本番フロントエンド (`npm start` = `next start`) がどのアドレスで待ち受けるかを実測する。

## 何を確かめるものか

`frontend/package.json` の ``start`` は ``next start`` で、``-H`` (ホスト指定) が無い。
``next start`` の CLI は ``-H`` の既定値を commander に登録しておらず
(``frontend/node_modules/next/dist/bin/next`` の ``program.command('start')`` に
``.default(...)`` が無い)、``options.hostname`` は ``undefined`` のまま
``frontend/node_modules/next/dist/server/lib/start-server.js`` の
``server.listen(port, hostname)`` に渡る。

つまり本番フロントの待ち受け範囲は「Node の ``net.Server.listen(port, undefined)``
が何に bind するか」で決まる。このスクリプトはその 1 点を実測する。

SAIVerse のバックエンドもフロントエンドも起動しない。使うのは Node の標準
モジュールだけで、テンポラリなポートを 1 つ開いて閉じる。

## どう実行するか

    C:/Users/shuhe/workspace/SAIVerse/.venv/Scripts/python.exe \
        docs/audits/2026-09-10_normal_behavior_remaining_evidence/repro/b45_next_start_bind_scope.py

Node (v22.17.0 で実測) が PATH に必要。

## 何が観測されたか (2026-09-10、Windows 11 / Node v22.17.0)

- package.json の start は ``next start`` で ``-H`` を含まない: True
- next の CLI に start 用 hostname の既定値登録が無い: True
- ``listen(port, undefined)`` の ``server.address()``: ``{"address": "::",
  "family": "IPv6", "port": <port>}``
  → 未指定アドレス (全インターフェース)。IPv4 マップドも受ける。
- 同じホストから非ループバック IPv4 の全部へ接続: 3 件とも成功
  ``100.114.244.14`` (Tailscale) / ``192.168.0.127`` (自宅 LAN) /
  ``172.22.128.1`` (WSL vEthernet)
  → ソケットは Tailscale だけでなく LAN のアドレスでも受ける。
  ※ **別マシンから実際に届くかは OS のファイアウォール次第で、これは未実測。**
  同一ホストからの接続は多くの場合ファイアウォールを通らない。
"""

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
FRONTEND = REPO_ROOT / "frontend"

NODE_PROBE = r"""
const http = require('http');
const net = require('net');
const os = require('os');

const server = http.createServer((req, res) => { res.end('ok'); });

// next start と同じ呼び方: hostname は undefined のまま listen へ渡る
// (dist/server/lib/start-server.js の `server.listen(port, hostname)`)。
const hostname = undefined;
server.listen(0, hostname, () => {
    const addr = server.address();
    const lan = [];
    for (const [, infos] of Object.entries(os.networkInterfaces())) {
        for (const info of infos || []) {
            if (info.family === 'IPv4' && !info.internal) lan.push(info.address);
        }
    }
    const result = { address: addr, lan_connect: [] };
    if (lan.length === 0) {
        console.log(JSON.stringify(result));
        server.close();
        return;
    }
    let pending = lan.length;
    const done = () => {
        if (--pending === 0) {
            console.log(JSON.stringify(result));
            server.close();
        }
    };
    for (const host of lan) {
        const probe = net.connect({ host, port: addr.port }, () => {
            result.lan_connect.push({ host, ok: true });
            probe.end();
            done();
        });
        probe.on('error', (err) => {
            result.lan_connect.push({ host, ok: false, error: String(err.message) });
            done();
        });
    }
});
"""


def read_start_script() -> str:
    pkg = json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))
    return str(pkg.get("scripts", {}).get("start", ""))


def start_command_has_hostname_default() -> bool:
    """next の CLI 定義で start の -H に既定値が登録されているか。"""
    cli = FRONTEND / "node_modules" / "next" / "dist" / "bin" / "next"
    if not cli.exists():
        return False
    text = cli.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"program\.command\('start'\)(.{0,4000}?)program\.command\(", text, re.S)
    segment = match.group(1) if match else ""
    hostname_part = segment.split("--hostname", 1)[-1][:300]
    return ".default(" in hostname_part


def probe_node_default_bind() -> dict:
    node = shutil.which("node")
    if node is None:
        return {"error": "node not found on PATH"}
    proc = subprocess.run(
        [node, "-e", NODE_PROBE],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        return {"error": proc.stderr.strip()}
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main() -> int:
    start_script = read_start_script()
    print(f"frontend/package.json scripts.start = {start_script!r}")
    print(f"  -H (hostname) 指定を含むか: {'-H' in start_script}")
    print(f"next CLI: start の --hostname に既定値登録があるか: "
          f"{start_command_has_hostname_default()}")

    result = probe_node_default_bind()
    print(f"listen(port, undefined) の観測: {json.dumps(result, ensure_ascii=False)}")

    addr = (result.get("address") or {}).get("address")
    unspecified = addr in {"::", "0.0.0.0"}
    print(f"  → 未指定アドレス (全インターフェース) か: {unspecified}")

    # 参考: このホストのループバック解決も出しておく (比較用)。
    print(f"  参考: socket.gethostname() = {socket.gethostname()!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
