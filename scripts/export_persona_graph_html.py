#!/usr/bin/env python3
from __future__ import annotations

"""
ペルソナIDを指定して、Cognee が生成したナレッジグラフを単体HTMLとして出力します。

優先度:
1) ペルソナの Cognee ディレクトリ配下（~/.saiverse/personas/<id>/cognee_system）から
   graph*.json を探索して使用。
2) 見つからない場合は、Cognee のAPIが提供されていれば取得を試行（存在チェックし失敗時はスキップ）。

出力HTMLは D3.js のCDNを利用したシンプルな力学レイアウト。ノード/エッジ情報は埋め込みます。
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from dotenv import load_dotenv
except Exception:  # pragma: no cover
    def load_dotenv(*args, **kwargs):
        return False


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

load_dotenv()

from integrations.cognee_memory import CogneeMemory  # noqa: E402


def _suppress_logs() -> None:
    import logging
    os.environ.setdefault("SAIVERSE_THIRDPARTY_LOG_LEVEL", "ERROR")
    for name in (
        "litellm",
        "httpx",
        "httpcore",
        "urllib3",
        "cognee",
        "cognee.shared.logging_utils",
    ):
        try:
            lg = logging.getLogger(name)
            lg.setLevel(logging.ERROR)
            lg.propagate = False
        except Exception:
            pass


def _persona_cognee_root(persona_id: str) -> Path:
    return Path.home() / ".saiverse" / "personas" / persona_id / "cognee_system"


def _validate_graph(obj: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(obj, dict):
        return None
    nodes = obj.get("nodes")
    edges = obj.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return None
    # Allow flexible field names
    def _norm_node(n: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(n, dict):
            return None
        nid = n.get("id") or n.get("uid") or n.get("key") or n.get("name")
        label = n.get("label") or n.get("title") or n.get("name") or str(nid)
        if nid is None:
            return None
        return {"id": str(nid), "label": str(label)}

    def _norm_edge(e: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(e, dict):
            return None
        src = e.get("source") or e.get("from") or e.get("src")
        tgt = e.get("target") or e.get("to") or e.get("dst")
        lbl = e.get("label") or e.get("relation") or e.get("type") or ""
        if src is None or tgt is None:
            return None
        return {"source": str(src), "target": str(tgt), "label": str(lbl)}

    nn: List[Dict[str, str]] = []
    ee: List[Dict[str, str]] = []
    for n in nodes:
        v = _norm_node(n)
        if v:
            nn.append(v)
    for e in edges:
        v = _norm_edge(e)
        if v:
            ee.append(v)
    if not nn:
        return None
    return {"nodes": nn, "edges": ee}


def _scan_graph_json(base: Path) -> Optional[Dict[str, Any]]:
    if not base.exists():
        return None
    candidates: List[Tuple[float, Path]] = []
    for p in base.rglob("*.json"):
        name = p.name.lower()
        if "graph" in name or "kg" in name:
            try:
                ts = p.stat().st_mtime
            except Exception:
                ts = 0.0
            candidates.append((ts, p))
    # newest first
    candidates.sort(key=lambda x: x[0], reverse=True)
    for _, p in candidates:
        try:
            obj = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        val = _validate_graph(obj)
        if val:
            # attach helper field for debug
            val["__source_path__"] = str(p)
            return val
    return None


def _try_fetch_via_cognee(persona_id: str, debug_api: bool = False) -> Optional[Dict[str, Any]]:
    # Ensure Cognee is initialized with proper persona env
    mem = CogneeMemory(persona_id)
    try:
        import importlib
        api_graph = importlib.import_module("cognee.api.v1.graph")  # type: ignore
    except Exception as e:
        if debug_api:
            print(f"[debug] failed to import cognee.api.v1.graph: {e}", file=sys.stderr)
        return None

    # Try common function names
    funcs = [
        "export_graph_json",
        "get_graph_json",
        "get_graph",
        "export_graph",
    ]
    for fn in funcs:
        try:
            f = getattr(api_graph, fn, None)
            if f is None:
                if debug_api:
                    print(f"[debug] graph API has no function: {fn}", file=sys.stderr)
                continue
            # Try async
            import asyncio

            async def _call():
                try:
                    # prefer explicit datasets kw
                    try:
                        res = await f(datasets=persona_id)
                    except TypeError:
                        res = await f(persona_id)
                except TypeError:
                    # some impls might be sync
                    try:
                        res = f(datasets=persona_id)
                    except TypeError:
                        res = f(persona_id)
                return res

            res = asyncio.run(_call())
            if isinstance(res, str):
                try:
                    res = json.loads(res)
                except Exception:
                    return None
            return _validate_graph(res)
        except Exception as e:
            if debug_api:
                print(f"[debug] calling {fn} failed: {e}", file=sys.stderr)
            continue
    if debug_api:
        # list attributes to help identify export functions in this version
        try:
            names = [n for n in dir(api_graph) if not n.startswith('_')]
            print(f"[debug] available api_graph attrs: {', '.join(names)}", file=sys.stderr)
        except Exception:
            pass
    return None


def _extract_graph_via_kuzu(root: Path, debug: bool = False) -> Optional[Dict[str, Any]]:
    """Fallback: 直接Kùzu DBからノード/エッジを抽出する。
    期待スキーマ（CogneeのKuzuAdapter既定）:
      - NODE TABLE Node(id STRING PRIMARY KEY, name STRING, type STRING, ...)
      - REL  TABLE EDGE(FROM Node TO Node, relationship_name STRING, ...)
    """
    dbdir = root / "databases" / "cognee_graph_kuzu"
    if not dbdir.exists():
        if debug:
            print(f"[debug] kuzu db not found: {dbdir}", file=sys.stderr)
        return None
    try:
        import importlib
        from cognee.infrastructure.databases.graph.kuzu.adapter import KuzuAdapter  # type: ignore
    except Exception as e:
        if debug:
            print(f"[debug] failed to import KuzuAdapter: {e}", file=sys.stderr)
        return None

    ka = KuzuAdapter(str(dbdir))
    import asyncio

    async def _run():
        try:
            # Nodes
            rows_n = await ka.query("MATCH (n:Node) RETURN n.id, n.name")
        except Exception as e:
            if debug:
                print(f"[debug] node query failed: {e}", file=sys.stderr)
            return None
        try:
            # Edges
            rows_e = await ka.query("MATCH (a:Node)-[e:EDGE]->(b:Node) RETURN a.id, b.id, e.relationship_name")
        except Exception as e:
            if debug:
                print(f"[debug] edge query failed: {e}", file=sys.stderr)
            rows_e = []

        # Normalize
        nodes: Dict[str, Dict[str, str]] = {}
        for rid, name in rows_n or []:
            sid = str(rid) if rid is not None else None
            if not sid:
                continue
            lbl = str(name) if name is not None else sid
            nodes[sid] = {"id": sid, "label": lbl}
        edges: List[Dict[str, str]] = []
        for src, dst, lbl in rows_e or []:
            s = str(src) if src is not None else None
            t = str(dst) if dst is not None else None
            if not s or not t:
                continue
            edges.append({"source": s, "target": t, "label": str(lbl or "")})

        if not nodes:
            return None
        return {"nodes": list(nodes.values()), "edges": edges, "__source_path__": str(dbdir)}

    try:
        return asyncio.run(_run())
    except Exception as e:
        if debug:
            print(f"[debug] kuzu extract failed: {e}", file=sys.stderr)
        return None


def _build_html(graph: Dict[str, Any], title: str) -> str:
    data_json = json.dumps(graph, ensure_ascii=False)
    # f-string/format の波括弧衝突を避けるため、プレースホルダを後置換
    template = r"""
<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>__TITLE__</title>
  <style>
    html, body { height: 100%; margin: 0; }
    #graph { width: 100%; height: calc(100% - 56px); background: #0f1117; color: #e6e6e6; }
    header { height: 56px; display: flex; align-items: center; padding: 0 12px; background: #1b1f2a; color: #e6e6e6; font-family: sans-serif; }
    .badge { margin-left: 8px; font-size: 12px; opacity: .8; }
    .label { font-size: 10px; pointer-events: none; fill: #ddd; }
    .link { stroke: #7a88a5; stroke-opacity: .6; }
    .node { stroke: #fff; stroke-width: .5px; }
  </style>
  <script src="https://cdn.jsdelivr.net/npm/d3@7"></script>
</head>
<body>
  <header>
    <div>__TITLE__</div>
    <div id="meta" class="badge"></div>
  </header>
  <svg id="graph"></svg>
  <script>
    const data = __DATA__;
    const svg = d3.select('#graph');
    const width = svg.node().clientWidth;
    const height = svg.node().clientHeight;
    svg.attr('viewBox', [0, 0, width, height]);

    const nodes = data.nodes.map(d => Object.assign({}, d));
    const nodeById = new Map(nodes.map(d => [d.id, d]));
    const links = data.edges.map(d => ({source: nodeById.get(d.source), target: nodeById.get(d.target), label: d.label || ''})).filter(d => d.source && d.target);

    document.getElementById('meta').textContent = `ノード: ${nodes.length} / エッジ: ${links.length}`;

    const color = d3.scaleOrdinal(d3.schemeTableau10);
    const simulation = d3.forceSimulation(nodes)
      .force('link', d3.forceLink(links).id(d => d.id).distance(60).strength(0.08))
      .force('charge', d3.forceManyBody().strength(-60))
      .force('center', d3.forceCenter(width / 2, height / 2));

    const link = svg.append('g')
      .attr('stroke-width', 1)
      .attr('class', 'link')
      .selectAll('line')
      .data(links)
      .join('line');

    const node = svg.append('g')
      .attr('stroke', '#fff')
      .attr('stroke-width', 0.5)
      .selectAll('circle')
      .data(nodes)
      .join('circle')
      .attr('r', 6)
      .attr('fill', d => color(d.id))
      .attr('class', 'node')
      .call(d3.drag()
          .on('start', dragstarted)
          .on('drag', dragged)
          .on('end', dragended));

    const labels = svg.append('g')
      .selectAll('text')
      .data(nodes)
      .join('text')
      .attr('class', 'label')
      .text(d => d.label?.slice(0, 40) || d.id);

    node.append('title').text(d => d.label || d.id);

    simulation.on('tick', () => {
      link.attr('x1', d => d.source.x)
          .attr('y1', d => d.source.y)
          .attr('x2', d => d.target.x)
          .attr('y2', d => d.target.y);

      node.attr('cx', d => d.x)
          .attr('cy', d => d.y);

      labels.attr('x', d => d.x + 8)
            .attr('y', d => d.y + 3);
    });

    function dragstarted(event, d) {
      if (!event.active) simulation.alphaTarget(0.3).restart();
      d.fx = d.x; d.fy = d.y;
    }
    function dragged(event, d) {
      d.fx = event.x; d.fy = event.y;
    }
    function dragended(event, d) {
      if (!event.active) simulation.alphaTarget(0);
      d.fx = null; d.fy = null;
    }
  </script>
</body>
</html>
"""
    return template.replace("__DATA__", data_json).replace("__TITLE__", title)


def main() -> None:
    ap = argparse.ArgumentParser(description="ペルソナのナレッジグラフをHTMLでエクスポート")
    ap.add_argument("--persona-id", required=True, help="ペルソナID")
    ap.add_argument("--out", default=None, help="出力HTMLパス（既定: ./<persona>_graph.html）")
    ap.add_argument("--title", default=None, help="HTMLのタイトル（既定: 'Persona <id> Knowledge Graph'）")
    ap.add_argument("--force-cognify", action="store_true", help="エクスポート前にcognifyを同期実行する")
    ap.add_argument("--quiet", action="store_true", help="サードパーティの冗長ログを抑制する")
    ap.add_argument("--debug-list", action="store_true", help="探索対象ディレクトリ内のファイル一覧を表示する")
    ap.add_argument("--debug-api", action="store_true", help="CogneeのグラフAPI関数を詳細ログ出力する")
    args = ap.parse_args()

    # Geminiが使える場合は既定で選択
    if not os.getenv("LLM_PROVIDER") and (os.getenv("GEMINI_FREE_API_KEY") or os.getenv("GEMINI_API_KEY")):
        os.environ["LLM_PROVIDER"] = "gemini"

    if args.quiet:
        _suppress_logs()

    root = _persona_cognee_root(args.persona_id)
    if args.debug_list:
        print(f"Persona root: {root}")
        dbdir = root / "databases"
        print(f"Databases dir: {dbdir}  (exists={dbdir.exists()})")
        if dbdir.exists():
            for p in sorted(dbdir.rglob('*')):
                try:
                    rel = p.relative_to(root)
                    if p.is_file():
                        print(f" - {rel} size={p.stat().st_size}")
                    else:
                        print(f" - {rel}/")
                except Exception:
                    pass
    if args.force_cognify:
        try:
            CogneeMemory(args.persona_id).finalize(wait=True)
        except Exception:
            # ignore finalize failures and continue scanning
            pass
    graph = _scan_graph_json(root)
    if graph is None:
        graph = _try_fetch_via_cognee(args.persona_id, debug_api=args.debug_api)
    if graph is None:
        graph = _extract_graph_via_kuzu(root, debug=args.debug_api)
    if graph is None:
        print("グラフJSONが見つからず、API取得も失敗しました。cognify実行後に再度お試しください。", file=sys.stderr)
        sys.exit(2)

    out = Path(args.out) if args.out else Path.cwd() / f"{args.persona_id}_graph.html"
    title = args.title or f"Persona {args.persona_id} Knowledge Graph"
    html = _build_html(graph, title)
    out.write_text(html, encoding="utf-8")
    src = graph.get("__source_path__")
    meta = f" source={src}" if src else ""
    print(f"書き出し完了: {out}  (ノード={len(graph['nodes'])}, エッジ={len(graph['edges'])}){meta}")


if __name__ == "__main__":
    main()
