# -*- coding: utf-8 -*-
"""
tools/e2e_smoke.py — 走 MCP 工具层（FastMCP.call_tool）打真实 PLECS

验证：plecs_connect → plecs_load → plecs_status → plecs_simulate(MatFile+save_mat)
      → plecs_close 的输出文本是否符合预期（含 P0 修复点）。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import build_server  # noqa: E402

MODEL = sys.argv[1] if len(sys.argv) > 1 else \
    r"C:\Users\74281\WorkBuddy\WORKBUDDY_TEST\lvrt\lvrt_grid_source.plecs"
MAT = str(Path(MODEL).parent / "_e2e_out.mat")


def text_of(res):
    """FastMCP call_tool 返回 (content_list, meta) 或 list[ContentBlock]。"""
    blocks = res[0] if isinstance(res, tuple) else res
    out = []
    for b in blocks:
        t = getattr(b, "text", None)
        if t is None and isinstance(b, dict):
            t = b.get("text")
        out.append(t if t is not None else str(b))
    return "\n".join(out)


async def main() -> int:
    mcp = build_server({"host": "localhost", "port": 1080,
                        "timeout": 15.0, "long_timeout": 600.0})

    async def call(name, **kw):
        res = await mcp.call_tool(name, kw)
        return text_of(res)

    print("== plecs_connect ==")
    print(await call("plecs_connect"))

    print("\n== plecs_load ==")
    out = await call("plecs_load", path=MODEL)
    print(out)
    name = None
    for line in out.splitlines():
        if "名称：" in line:
            name = line.split("名称：", 1)[1].strip()
    assert name and not name.isdigit(), f"load 未解析出模型名：{out}"
    assert "✓" in out, "load 失败"

    print("\n== plecs_status（应显示文件名 + state）==")
    print(await call("plecs_status"))

    print("\n== plecs_simulate Plain ==")
    print(await call("plecs_simulate", model_name=name,
                     opts='{"TimeSpan": 0.02}'))

    print("\n== plecs_simulate MatFile + save_mat ==")
    out = await call("plecs_simulate", model_name=name,
                     opts='{"TimeSpan": 0.02, "OutputFormat": "MatFile"}',
                     save_mat=MAT)
    print(out)
    p = Path(MAT)
    assert p.exists() and p.stat().st_size > 100, "MAT 未落盘"
    assert b"MATLAB 5.0 MAT-file" in p.read_bytes()[:80], "不是 MAT v5"

    print("\n== plecs_close ==")
    print(await call("plecs_close", model_name=name))
    print("\n端到端通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
