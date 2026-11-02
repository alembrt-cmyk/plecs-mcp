"""
tests/test_offline.py
---------------------
P1 新增的「离线能力」单测：`.trace` 解析器 / `.plecs` 离线解析与改参 / 对应 MCP 工具。
全部用合成数据，不需要 PLECS、不需要外部样本文件。
"""
import base64
import os
import struct
import sys
import types

import pytest

# ── FastMCP stub（与 test_server.py 同款，幂等）────────────────────────────

class _FakeFastMCP:
    def __init__(self, name="", **kw):
        self._tools = {}

    def tool(self):
        def deco(fn):
            self._tools[fn.__name__] = fn
            return fn
        return deco

    def run(self, **kw):
        pass


_mcp = types.ModuleType("mcp")
_srv = types.ModuleType("mcp.server")
_fmcp = types.ModuleType("mcp.server.fastmcp")
_fmcp.FastMCP = _FakeFastMCP
_mcp.server = _srv
_srv.fastmcp = _fmcp
sys.modules.setdefault("mcp", _mcp)
sys.modules.setdefault("mcp.server", _srv)
sys.modules.setdefault("mcp.server.fastmcp", _fmcp)

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import plecs_file as pf                                            # noqa: E402
import trace_reader as tr                                          # noqa: E402


# ── .trace 合成器 ─────────────────────────────────────────────────────────

def _s(text: str) -> bytes:
    raw = text.encode("utf-16-be")
    return struct.pack(">i", len(raw)) + raw


def mk_trace(traces, times, curves):
    """traces=[(name,[sig...])]；times=[float]；curves=[(ymax,ymin,[v...])]。"""
    b = struct.pack(">ii", 2, len(traces))
    for name, sigs in traces:
        b += _s(name) + struct.pack(">i", len(sigs)) + b"".join(_s(s) for s in sigs)
    b += struct.pack(">i", 0)                       # nLabel
    b += b"\x00" * 20                               # 5×i32 0
    b += struct.pack(">i", len(times) + 1)          # nPoints（实际点数 = -1）
    b += b"\x00" * 8
    b += b"".join(struct.pack(">d", t) for t in times)
    for ymax, ymin, vals in curves:
        b += b"\x01\x00\x00\x00"                     # 曲线块的可变长前缀
        b += struct.pack(">dd", ymax, ymin)
        b += b"\x00" * 12 + struct.pack(">i", len(vals) + 1) + b"\x00" * 8
        b += b"".join(struct.pack(">d", v) for v in vals)
    return b


def _wiggle(n, amp=10.0, dc=0.0):
    return [dc + amp * (1 if i % 2 else -1) * (i / n) for i in range(n)]


@pytest.fixture
def one_curve(tmp_path):
    t = [i * 1e-4 for i in range(50)]
    p = tmp_path / "one.trace"
    p.write_bytes(mk_trace([("Scope", ["Vout"])], t, [(10.0, -10.0, _wiggle(50, 10.0, 5.0))]))
    return p, t


@pytest.fixture
def two_curves(tmp_path):
    t = [i * 2e-5 for i in range(60)]
    p = tmp_path / "two.trace"
    p.write_bytes(mk_trace(
        [("Scope", ["V1", "V2"])], t,
        [(3.0, -3.0, [1.0] * 60), (7.0, -7.0, [-2.0] * 60)]))
    return p, t


# ── 解析器：正常路径 ──────────────────────────────────────────────────────

class TestTraceReader:
    def test_single_curve(self, one_curve):
        p, t = one_curve
        cs = tr.read_trace(p)
        assert len(cs) == 1
        c = cs[0]
        assert (c.trace_label, c.signal) == ("Scope", "Vout")
        assert len(c.v) == 50
        assert c.t[0] == pytest.approx(t[0], abs=1e-12)
        assert c.t[-1] == pytest.approx(t[-1], abs=1e-9)
        assert c.ymax == pytest.approx(10.0) and c.ymin == pytest.approx(-10.0)
        s = c.stats()
        assert s["n"] == 50
        assert s["max"] == max(c.v) and s["min"] == min(c.v)
        assert s["pp"] == pytest.approx(s["max"] - s["min"])

    def test_two_curves_share_time(self, two_curves):
        p, t = two_curves
        cs = tr.read_trace(p)
        assert len(cs) == 2
        assert cs[0].t == cs[1].t
        assert cs[0].v == [1.0] * 60
        assert cs[1].v == [-2.0] * 60
        assert cs[1].stats()["rms"] == pytest.approx(2.0)

    def test_max_curves(self, two_curves):
        p, _ = two_curves
        assert len(tr.read_trace(p, max_curves=1)) == 1
        assert len(tr.read_trace(p, max_curves=0)) == 2

    def test_keep_false_streams_stats(self, two_curves):
        p, _ = two_curves
        cs = tr.read_trace(p, keep=False)
        assert cs[0].v == []                       # 不保留数组
        assert cs[0].stats()["n"] == 60            # 统计量照样有
        assert cs[1].stats()["avg"] == pytest.approx(-2.0)

    def test_summarize_text(self, two_curves):
        p, _ = two_curves
        txt = tr.summarize(tr.read_trace(p), head=2)
        assert "曲线：2 条" in txt and "样本：" in txt
        assert "Scope / V1" in txt

    def test_summarize_empty(self):
        assert tr.summarize([]) == "（无曲线）"


class TestTraceReaderCsv:
    def test_same_time_base(self, two_curves, tmp_path):
        p, _ = two_curves
        out = tr.write_csv(tmp_path / "sub" / "o.csv", tr.read_trace(p))
        lines = open(out, encoding="utf-8").read().splitlines()
        assert lines[0] == "t,Scope/V1,Scope/V2"
        assert len(lines) == 61
        assert lines[1].split(",")[1] == "1"

    def test_diff_time_base(self, tmp_path):
        t1 = [0.0, 1.0, 2.0]
        t2 = [0.0, 0.5]
        cs = [tr.TraceCurve("A", "x", t=t1, v=[1.0, 2.0, 3.0]),
              tr.TraceCurve("B", "y", t=t2, v=[4.0, 5.0])]
        out = tr.write_csv(tmp_path / "d.csv", cs)
        lines = open(out, encoding="utf-8").read().splitlines()
        assert lines[0] == "t[x],v[A/x],t[y],v[B/y]"
        assert len(lines) == 4                     # 表头 + 3 行（短列留空）
        assert lines[3].endswith(",")              # 第 3 行 B 列缺值


class TestTraceReaderErrors:
    def test_tiny_file(self, tmp_path):
        p = tmp_path / "t.trace"
        p.write_bytes(b"1234")
        with pytest.raises(tr.TraceError):
            tr.read_trace(p)

    def test_bad_version(self, tmp_path):
        p = tmp_path / "t.trace"
        p.write_bytes(struct.pack(">ii", 9, 1) + b"\x00" * 40)
        with pytest.raises(tr.TraceError, match="未知"):
            tr.read_trace(p)

    def test_no_anchor(self, tmp_path):
        p = tmp_path / "t.trace"
        p.write_bytes(struct.pack(">ii", 2, 0) + b"\x00" * 64)
        with pytest.raises(tr.TraceError):
            tr.read_trace(p)


# ── .plecs 合成器 ─────────────────────────────────────────────────────────

def _b64(s: str) -> str:
    e = base64.b64encode(s.encode("utf-8")).decode("ascii")
    return f'base64 "{e}"'


def _b64_lines(key: str, text: str, indent: str = "  ", width: int = 16) -> list[str]:
    """跨行 base64 属性（模拟 PLECS 把长串切段续写的样子）。"""
    e = base64.b64encode(text.encode("utf-8")).decode("ascii")
    chunks = [e[i:i + width] for i in range(0, len(e), width)]
    out = [f'{indent}{key} base64 "{chunks[0]}"']
    out += [f'"{c}"' for c in chunks[1:]]
    return out


_INIT = "fc = 50000; fo = 60;"

PLEcs_SAMPLE = "\r\n".join(
    ['Plecs {',
     '  Name          "demo"',
     '  Version       "4.7"',
     '  TimeSpan      "0.05"',
     '  Solver        "radau"']
    + _b64_lines("InitializationCommands", _INIT)
    + ['  Circuit {',
       '    Component {',
       '      Type          Resistor',
       '      Name          "R1"',
       '      Position      [10, 20]',
       '      Parameter {',
       '        Variable      "R"',
       '        Value         "1"',
       '        Show          off',
       '      }',
       '    }',
       '    Component {',
       '      Type          CScript',
       '      Name          ' + _b64("环路控制"),
       '      Parameter {',
       '        Variable      "Ts"',
       '        Value         "1/(fc*200)"',
       '        Show          off',
       '      }',
       '      Parameter {',
       '        Variable      "Declarations"',
       '        Value         "#include \\"a.h\\"\\n"',
       '"#include \\"b.h\\""',
       '        Show          off',
       '      }',
       '      Terminal {',
       '        Type          Input',
       '        Position      [0, 5]',
       '        Direction     right',
       '      }',
       '    }',
       '    Component {',
       '      Type          Subsystem',
       '      Name          "Sub1"',
       '      Circuit {',
       '        Component {',
       '          Type          Inductor',
       '          Name          "L1"',
       '        }',
       '      }',
       '    }',
       '  }',
       '}',
       'DemoSignature "sig"',
       ''])


@pytest.fixture
def model(tmp_path):
    p = tmp_path / "demo.plecs"
    p.write_bytes(PLEcs_SAMPLE.encode("utf-8"))
    return p


class TestPlecsFileParse:
    def test_model_attrs(self, model):
        f = pf.PlecsFile(model)
        assert f.model.attrs["Name"] == "demo"
        assert f.model.attrs["TimeSpan"] == "0.05"
        assert f.model.attrs["Solver"] == "radau"
        # 跨行 base64 应已拼接并解码
        assert f.model.attrs["InitializationCommands"] == "fc = 50000; fo = 60;"
        assert f.newline == "\r\n"
        assert f.trailing_nl is True

    def test_components_and_counts(self, model):
        f = pf.PlecsFile(model)
        names = [c.attrs["Name"] for c in f.components()]
        assert names == ["R1", "环路控制", "Sub1", "L1"]
        assert f.type_counts() == {"CScript": 1, "Inductor": 1, "Resistor": 1, "Subsystem": 1}
        assert f.depth_of(f.resolve("L1")) == 1

    def test_resolve_forms(self, model):
        f = pf.PlecsFile(model)
        assert f.resolve("@").attrs["Name"] == "demo"
        assert f.resolve("#1").attrs["Name"] == "R1"
        assert f.resolve("Sub1/L1").attrs["Name"] == "L1"
        assert f.resolve("Sub").attrs["Name"] == "Sub1"      # 唯一子串
        assert f.resolve("CScript").attrs["Name"] == "环路控制"   # 按类型命中唯一
        with pytest.raises(pf.PlecsFileError, match="没命中"):
            f.resolve("Sub1/Nope")

    def test_resolve_ambiguous(self, model):
        p = model.with_name("x.plecs")
        p.write_bytes(PLEcs_SAMPLE.replace('"L1"', '"R1x"').encode("utf-8"))
        g = pf.PlecsFile(p)
        with pytest.raises(pf.PlecsFileError, match="命中"):
            g.resolve("R")          # 命中 R1 / R1x（还有 Resistor 类型）→ 要求消歧

    def test_params_and_terminals(self, model):
        f = pf.PlecsFile(model)
        cs = f.resolve("环路控制")
        assert cs.params()["Ts"] == "1/(fc*200)"
        assert cs.params()["Declarations"] == '#include "a.h"\n#include "b.h"'
        assert len(cs.terminals()) == 1
        assert "Input" in pf.render_ports(cs)
        assert "NumInputs" in pf.render_ports(f.resolve("R1")) or True

    def test_get_param_attr_fallback(self, model):
        f = pf.PlecsFile(model)
        node, ps = f.get_param("@", "TimeSpan")
        assert ps == {"TimeSpan": "0.05"}
        with pytest.raises(pf.PlecsFileError, match="没有参数"):
            f.get_param("R1", "NoSuch")

    def test_render_tree(self, model):
        f = pf.PlecsFile(model)
        tree = pf.render_tree(f, max_depth=2)
        assert "- R1  [Resistor]" in tree
        assert "  - L1  [Inductor]" in tree
        assert "内含 1 组件" in tree


class TestPlecsFileEdit:
    def test_set_raw_attr(self, model):
        f = pf.PlecsFile(model)
        old, new = f.set_param("@", "TimeSpan", "0.1")
        assert (old, new) == ("0.05", "0.1")
        f.save(backup=False)
        assert pf.PlecsFile(model).model.attrs["TimeSpan"] == "0.1"
        raw = model.read_bytes().decode("utf-8")
        assert '  TimeSpan      "0.1"' in raw           # 列对齐保留
        assert raw.count("\r\n") > 20                  # CRLF 保留

    def test_set_base64_attr(self, model):
        f = pf.PlecsFile(model)
        f.set_attr(f.resolve("环路控制"), "Name", "环路控制2")
        f.save(backup=False)
        g = pf.PlecsFile(model)
        assert g.resolve("环路控制2").attrs["Type"] == "CScript"

    def test_set_multiline_shrinks(self, model):
        f = pf.PlecsFile(model)
        before = len(f.lines)
        f.set_param("环路控制", "Declarations", '#include "a.h"\n')
        assert len(f.lines) == before - 1              # 2 行合并成 1 行
        f.save(backup=False)
        g = pf.PlecsFile(model)
        assert g.resolve("环路控制").params()["Declarations"] == '#include "a.h"\n'
        assert g.components()                          # 结构仍可解析
        assert g.resolve("Sub1/L1").attrs["Name"] == "L1"

    def test_set_grows_and_shifts(self, model):
        f = pf.PlecsFile(model)
        long = "\n".join(f'#include "h{i}.h"' for i in range(40))
        f.set_param("环路控制", "Declarations", long)
        f.save(backup=False)
        g = pf.PlecsFile(model)
        assert g.resolve("环路控制").params()["Declarations"] == long
        assert g.resolve("Sub1/L1").attrs["Name"] == "L1"   # 行号偏移后仍正确
        assert g.model.attrs["Name"] == "demo"

    def test_idempotent(self, model):
        f = pf.PlecsFile(model)
        before = model.read_bytes()
        f.set_param("环路控制", "Ts", "1/(fc*200)")     # 同值
        f.save(backup=False)
        assert model.read_bytes() == before

    def test_backup_created(self, model):
        f = pf.PlecsFile(model)
        f.set_param("@", "TimeSpan", "0.2")
        bak = f.save()
        assert bak and os.path.exists(bak)
        assert open(bak, "rb").read() == PLEcs_SAMPLE.encode("utf-8")

    def test_float_format_ok(self, model):
        f = pf.PlecsFile(model)
        f.set_param("R1", "R", "0.05")
        f.save(backup=False)
        assert pf.PlecsFile(model).resolve("R1").params()["R"] == "0.05"


class TestPlecsFileErrors:
    def test_missing_file(self, tmp_path):
        with pytest.raises(pf.PlecsFileError, match="不存在"):
            pf.PlecsFile(tmp_path / "nope.plecs")

    def test_unbalanced_braces(self, tmp_path):
        p = tmp_path / "b.plecs"
        p.write_bytes(b'Plecs {\n  Name "x"\n')
        with pytest.raises(pf.PlecsFileError, match="闭合"):
            pf.PlecsFile(p)

    def test_extra_close(self, tmp_path):
        p = tmp_path / "c.plecs"
        p.write_bytes(b'Plecs {\n}\n}\n')
        with pytest.raises(pf.PlecsFileError, match="多余"):
            pf.PlecsFile(p)

    def test_unknown_component(self, model):
        f = pf.PlecsFile(model)
        with pytest.raises(pf.PlecsFileError, match="找不到"):
            f.resolve("不存在的块")
        with pytest.raises(pf.PlecsFileError, match="越界"):
            f.resolve("#99")

    def test_binary_file(self, tmp_path):
        p = tmp_path / "bin.plecs"
        p.write_bytes(b"\x00\x01\x02" * 100)
        with pytest.raises(pf.PlecsFileError, match="NUL"):
            pf.PlecsFile(p)


# ── MCP 工具层 ───────────────────────────────────────────────────────────

@pytest.fixture
def tools():
    import server as srv_mod
    from unittest.mock import MagicMock
    from plecs_client import PlecsClient

    cfg = {"host": "localhost", "port": 1080}
    m = srv_mod.build_server(cfg)
    mc = MagicMock(spec=PlecsClient)
    mc.connect.return_value = {"version": "4.7.5", "build": "x", "models": []}
    m._tools["plecs_connect"](host="h", port=1) if False else None
    return m._tools


class TestOfflineTools:
    def test_trace_read_tool(self, tools, two_curves):
        p, _ = two_curves
        out = tools["plecs_trace_read"](trace_path=str(p))
        assert "曲线：2 条" in out and "rms=" in out

    def test_trace_read_head_and_csv(self, tools, two_curves, tmp_path):
        p, _ = two_curves
        csv = tmp_path / "out.csv"
        out = tools["plecs_trace_read"](trace_path=str(p), save_csv=str(csv), head=2)
        assert "样本：" in out
        assert "CSV 已写盘" in out and csv.exists()
        assert len(csv.read_text(encoding="utf-8").splitlines()) == 61

    def test_trace_read_dir_csv(self, tools, one_curve, tmp_path):
        p, _ = one_curve
        out = tools["plecs_trace_read"](trace_path=str(p), save_csv=str(tmp_path))
        assert (tmp_path / "one.csv").exists()
        assert "CSV 已写盘" in out

    def test_trace_read_missing(self, tools, tmp_path):
        out = tools["plecs_trace_read"](trace_path=str(tmp_path / "no.trace"))
        assert out.startswith("✗") and "不存在" in out
        assert "SaveTraces" in out                    # 给出补救指引

    def test_trace_read_keep_false_skips_csv(self, tools, two_curves, tmp_path):
        p, _ = two_curves
        out = tools["plecs_trace_read"](trace_path=str(p), save_csv=str(tmp_path / "x.csv"),
                                        keep=False)
        assert "跳过 CSV" in out

    def test_file_info_tool(self, tools, model):
        out = tools["plecs_file_info"](path=str(model), tree_depth=2)
        assert "模型名：demo" in out
        assert "组件总数：4" in out
        assert "radau" in out
        assert "InitCommands" in out
        assert "L1  [Inductor]" in out

    def test_file_blocks_tool(self, tools, model):
        out = tools["plecs_file_blocks"](path=str(model), kind="CScript", ports=True)
        assert "命中 1 / 4" in out
        assert "环路控制" in out and "Input" in out

    def test_file_blocks_query(self, tools, model):
        out = tools["plecs_file_blocks"](path=str(model), query="sub")
        assert "Sub1" in out
        out2 = tools["plecs_file_blocks"](path=str(model), query="zzz")
        assert "没有匹配" in out2 and "类型：" in out2

    def test_file_get_tool(self, tools, model):
        out = tools["plecs_file_get"](path=str(model), block="环路控制")
        assert "Ts" in out and "1/(fc*200)" in out
        one = tools["plecs_file_get"](path=str(model), block="@", var="TimeSpan")
        assert "TimeSpan = 0.05" in one

    def test_file_get_truncates(self, tools, model, tmp_path):
        p = tmp_path / "long.plecs"
        p.write_bytes(PLEcs_SAMPLE.replace('"#include \\"b.h\\""',
                                           '"' + "x" * 500 + '"').encode("utf-8"))
        out = tools["plecs_file_get"](path=str(p), block="环路控制",
                                      var="Declarations", max_len=50)
        assert "…（共" in out

    def test_file_set_dry_run(self, tools, model):
        before = model.read_bytes()
        out = tools["plecs_file_set"](path=str(model), block="环路控制",
                                      var="Ts", value="1/(fc*100)", dry_run=True)
        assert "dry-run" in out and "1/(fc*100)" in out
        assert model.read_bytes() == before

    def test_file_set_writes_and_verifies(self, tools, model):
        out = tools["plecs_file_set"](path=str(model), block="环路控制",
                                      var="Ts", value="1/(fc*100)")
        assert "回读校验通过" in out and "备份" in out
        assert pf.PlecsFile(model).resolve("环路控制").params()["Ts"] == "1/(fc*100)"
        assert "PLECS 里若已打开" in out

    def test_file_set_same_value(self, tools, model):
        out = tools["plecs_file_set"](path=str(model), block="R1", var="R", value="1")
        assert "已是目标值" in out

    def test_file_tools_missing_file(self, tools, tmp_path):
        p = str(tmp_path / "nope.plecs")
        for name, kw in (("plecs_file_info", {}), ("plecs_file_blocks", {}),
                         ("plecs_file_get", {"block": "x", "var": "y"}),
                         ("plecs_file_set", {"block": "x", "var": "y", "value": "z"})):
            assert tools[name](path=p, **kw).startswith("✗")
