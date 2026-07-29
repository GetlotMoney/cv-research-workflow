from __future__ import annotations

import ast
import hashlib
import html
import json
from pathlib import Path
from typing import Any

from .io import (
    parse_json_object_bytes,
    read_bounded_json_object,
    read_bounded_regular_file,
)
from .project import is_link_or_reparse


PYTHON_FILE_LIMIT = 64 * 1024
MODULE_LINE_LIMIT = 500
STRUCTURED_FILE_LIMIT = 1024 * 1024
MAX_SYMBOL_NODES = 8


def render_code_asset(directory: Path) -> bytes:
    """从 CodeAsset 的事实文件重算确定性 framework.html，不执行用户代码。"""
    directory = Path(directory)
    asset = read_bounded_json_object(
        directory / "asset.json", STRUCTURED_FILE_LIMIT, "asset.json",
    )
    module, contract, provenance = validated_render_inputs(directory)
    return render_framework_html(asset, module, contract, provenance)


def validated_render_inputs(directory: Path) -> tuple[bytes, bytes, bytes]:
    directory = Path(directory)
    if is_link_or_reparse(directory) or not directory.is_dir():
        raise ValueError(f"CodeAsset 目录不是普通目录：{directory}")
    module = read_bounded_regular_file(
        directory / "module.py", PYTHON_FILE_LIMIT, "module.py",
    )
    contract = read_bounded_regular_file(
        directory / "test_contract.py", PYTHON_FILE_LIMIT, "test_contract.py",
    )
    provenance = read_bounded_regular_file(
        directory / "provenance.json", STRUCTURED_FILE_LIMIT, "provenance.json",
    )
    _validate_python(module, "module.py", module=True)
    _validate_python(contract, "test_contract.py", module=False)
    parse_json_object_bytes(provenance, "provenance.json")
    return module, contract, provenance


def render_framework_html(
    asset: dict[str, Any],
    module_bytes: bytes,
    contract_bytes: bytes,
    provenance_bytes: bytes,
) -> bytes:
    """渲染完全离线、无脚本且由输入字节唯一决定的 HTML。"""
    _validate_python(module_bytes, "module.py", module=True)
    _validate_python(contract_bytes, "test_contract.py", module=False)
    provenance = parse_json_object_bytes(provenance_bytes, "provenance.json")

    digest = _render_input_digest(asset, module_bytes, contract_bytes, provenance_bytes)
    symbols = _module_symbols(module_bytes.decode("utf-8"))
    visible = symbols[:MAX_SYMBOL_NODES]
    omitted = len(symbols) - len(visible)
    node_items = [(kind, name, False) for kind, name in visible]
    if omitted:
        node_items.append(("summary", f"其余 {omitted} 个符号", True))

    rows = max(1, (len(node_items) + 2) // 3)
    verification_y = 235 + rows * 105
    svg_height = verification_y + 135
    symbol_markup: list[str] = []
    containment_edges: list[str] = []
    for index, (kind, name, summary) in enumerate(node_items):
        row, column = divmod(index, 3)
        x = 285 + column * 220
        y = 215 + row * 105
        containment_edges.append(
            f'<path class="edge containment" data-diagram-edge '
            f'data-edge-kind="containment" marker-end="url(#arrow-containment)" '
            f'd="M {x + 95} {y - 12} L {x + 95} {y}" />'
        )
        css_class = "box summary" if summary else "box symbol"
        symbol_markup.append(
            f'<g data-symbol-node="true">'
            f'<rect class="{css_class}" data-diagram-node x="{x}" y="{y}" '
            f'width="190" height="76" rx="13" />'
            f'<text class="kind" x="{x + 95}" y="{y + 27}">{_escape(_display(kind, 22))}</text>'
            f'<text x="{x + 95}" y="{y + 52}">{_escape(_display(name, 24))}</text>'
            f'</g>'
        )

    asset_id = _display(asset.get("id", "unknown"), 24)
    family = _display(
        asset.get("template_family") or asset.get("template_id", "unknown"), 28,
    )
    attachment = _display(asset.get("attachment_point", "unknown"), 28)
    scope = _display(provenance.get("scientific_claim_scope", "unknown"), 28)
    symbol_count = len(symbols)
    document = f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_escape(asset_id)} framework</title>
<!-- render-input-digest: {digest} -->
<style>
:root {{ color-scheme: light; --ink:#172033; --muted:#596579; --panel:#f7f9fc; --line:#4b5f7a; }}
* {{ box-sizing: border-box; }}
html, body {{ margin:0; padding:0; max-width:100%; overflow-x:hidden; background:#eef2f7; color:var(--ink); }}
body {{ font-family:"Microsoft YaHei","Noto Sans CJK SC",system-ui,sans-serif; }}
.page {{ width:min(1180px, calc(100% - 32px)); margin:24px auto; padding:26px; background:white; border-radius:18px; box-shadow:0 8px 32px #26354a18; }}
h1 {{ margin:0 0 8px; font-size:clamp(22px, 3vw, 34px); }}
.lede {{ margin:0 0 18px; color:var(--muted); line-height:1.7; }}
.svg-wrap {{ width:100%; max-width:100%; overflow:hidden; }}
svg {{ display:block; width:100%; max-width:100%; height:auto; }}
.box {{ fill:#fff; stroke:#536985; stroke-width:2; }}
.boundary {{ fill:#edf4ff; stroke:#3768a6; }}
.symbol {{ fill:#f8fbff; }}
.summary {{ fill:#fff7df; stroke:#9b741b; }}
.verify {{ fill:#eefaf3; stroke:#347253; }}
.edge {{ fill:none; stroke-width:2.2; }}
.edge.boundary {{ stroke:#3768a6; }}
.edge.containment {{ stroke:#6a5ca6; }}
.edge.verification {{ stroke:#347253; stroke-dasharray:7 5; }}
text {{ fill:var(--ink); font-size:14px; text-anchor:middle; dominant-baseline:middle; }}
text.kind {{ fill:#526076; font-size:12px; font-weight:700; letter-spacing:.04em; }}
.lane-label {{ text-anchor:start; font-weight:700; font-size:14px; fill:#344258; }}
.legend {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; margin-top:16px; }}
.legend div {{ padding:12px 14px; border:1px solid #dbe2ec; border-radius:12px; background:var(--panel); line-height:1.5; }}
.swatch {{ display:inline-block; width:28px; height:0; margin-right:8px; vertical-align:middle; border-top:3px solid; }}
.swatch.containment {{ border-color:#6a5ca6; }} .swatch.verification {{ border-color:#347253; border-top-style:dashed; }} .swatch.boundary {{ border-color:#3768a6; }}
@media (max-width: 840px) {{ .page {{ width:calc(100% - 16px); margin:8px auto; padding:12px; border-radius:12px; }} .legend {{ grid-template-columns:1fr; }} .lede {{ font-size:14px; }} }}
</style>
</head>
<body>
<main class="page">
<h1>{_escape(asset_id)} 派生框架图</h1>
<p class="lede">输入：CodeAsset 的事实文件与真实 Python AST；输出：边界、包含关系和静态验证关系。此图不声明运行时执行数据流，也不呈现张量形状；图片数量等维度语义须由实现另行验证。</p>
<div class="svg-wrap">
<svg viewBox="0 0 960 {svg_height}" role="img" aria-label="CodeAsset framework boundary diagram">
<defs>
<marker id="arrow-boundary" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#3768a6" /></marker>
<marker id="arrow-containment" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#6a5ca6" /></marker>
<marker id="arrow-verification" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#347253" /></marker>
</defs>
<text class="lane-label" x="25" y="28">事实边界（左 → 右）</text>
<path class="edge boundary" data-diagram-edge data-edge-kind="boundary" marker-end="url(#arrow-boundary)" d="M 225 105 L 265 105" />
<path class="edge boundary" data-diagram-edge data-edge-kind="boundary" marker-end="url(#arrow-boundary)" d="M 455 105 L 495 105" />
<path class="edge boundary" data-diagram-edge data-edge-kind="boundary" marker-end="url(#arrow-boundary)" d="M 685 105 L 725 105" />
<path class="edge containment" data-diagram-edge data-edge-kind="containment" d="M 225 253 L 255 253" />
<path class="edge containment" data-diagram-edge data-edge-kind="containment" d="M 255 203 L 255 {max(253, 203 + (rows - 1) * 105)}" />
{''.join(f'<path class="edge containment" data-diagram-edge data-edge-kind="containment" d="M 255 {203 + row * 105} L 820 {203 + row * 105}" />' for row in range(rows))}
{''.join(containment_edges)}
<path class="edge verification" data-diagram-edge data-edge-kind="verification" marker-end="url(#arrow-verification)" d="M 225 {verification_y + 38} L 285 {verification_y + 38}" />

<g><rect class="box boundary" data-diagram-node x="25" y="60" width="200" height="90" rx="14" /><text class="kind" x="125" y="90">CodeAsset</text><text x="125" y="120">{_escape(asset_id)}</text></g>
<g><rect class="box boundary" data-diagram-node x="265" y="60" width="190" height="90" rx="14" /><text class="kind" x="360" y="90">template</text><text x="360" y="120">{_escape(family)}</text></g>
<g><rect class="box boundary" data-diagram-node x="495" y="60" width="190" height="90" rx="14" /><text class="kind" x="590" y="90">attachment</text><text x="590" y="120">{_escape(attachment)}</text></g>
<g><rect class="box boundary" data-diagram-node x="725" y="60" width="210" height="90" rx="14" /><text class="kind" x="830" y="90">provenance</text><text x="830" y="120">{_escape(scope)}</text></g>

<text class="lane-label" x="25" y="190">module.py AST containment（{symbol_count} 个符号）</text>
<g><rect class="box" data-diagram-node x="25" y="215" width="200" height="76" rx="13" /><text class="kind" x="125" y="242">source</text><text x="125" y="267">module.py AST</text></g>
{''.join(symbol_markup)}

<text class="lane-label" x="25" y="{verification_y - 20}">静态 verification</text>
<g><rect class="box verify" data-diagram-node x="25" y="{verification_y}" width="200" height="76" rx="13" /><text class="kind" x="125" y="{verification_y + 27}">contract input</text><text x="125" y="{verification_y + 52}">test_contract.py</text></g>
<g><rect class="box verify" data-diagram-node x="285" y="{verification_y}" width="250" height="76" rx="13" /><text class="kind" x="410" y="{verification_y + 27}">verification output</text><text x="410" y="{verification_y + 52}">UTF-8 · compile · boundary</text></g>
</svg>
</div>
<section aria-label="图例" class="legend">
<div><span class="swatch containment"></span><strong>containment</strong>：AST 所属关系</div>
<div><span class="swatch verification"></span><strong>verification</strong>：静态检查关系</div>
<div><span class="swatch boundary"></span><strong>boundary</strong>：事实边界，不是执行顺序</div>
</section>
</main>
</body>
</html>
'''
    encoded = document.encode("utf-8")
    if len(encoded) > STRUCTURED_FILE_LIMIT:
        raise ValueError("framework.html 超过 1 MiB 上限")
    return encoded


def _module_symbols(source: str) -> list[tuple[str, str]]:
    tree = ast.parse(source, filename="module.py")
    symbols: list[tuple[str, str]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.append(("function", node.name))
        elif isinstance(node, ast.ClassDef):
            symbols.append(("class", node.name))
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    symbols.append(("method", f"{node.name}.{child.name}"))
    return symbols


def _validate_python(content: bytes, filename: str, *, module: bool) -> None:
    if len(content) > PYTHON_FILE_LIMIT:
        raise ValueError(f"{filename} 超过 64 KiB 上限")
    try:
        source = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError(f"{filename} 必须是 UTF-8 Python 文件") from error
    if module and len(source.splitlines()) > MODULE_LINE_LIMIT:
        raise ValueError("module.py 超过 500 行上限")
    try:
        compile(source, filename, "exec")
    except (SyntaxError, ValueError, OverflowError) as error:
        raise ValueError(f"{filename} Python 语法无效：{error}") from error


def _render_input_digest(
    asset: dict[str, Any], module: bytes, contract: bytes, provenance: bytes,
) -> str:
    asset_bytes = json.dumps(
        asset, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    digest = hashlib.sha256()
    for label, content in (
        (b"asset", asset_bytes), (b"module", module),
        (b"contract", contract), (b"provenance", provenance),
    ):
        digest.update(len(label).to_bytes(2, "big"))
        digest.update(label)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return "sha256:" + digest.hexdigest()


def _display(value: object, limit: int) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: max(1, limit - 1)] + "…"


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)
