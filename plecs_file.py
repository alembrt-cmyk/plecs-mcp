# -*- coding: utf-8 -*-
"""
plecs_file.py — PLECS `.plecs` 文件的离线解析与改参（不需要 PLECS 在运行）

★ 文件是 PLECS **私有大括号格式**（不是 XML，也不是 JSON），实测 4.7：

    Plecs {
      Name          "5-level-c"
      Version       "4.7"
      TimeSpan      "0.05"
      Solver        "radau"
      InitializationCommands base64 "VmFjID0gMjc3Ow..."
                             "5u455S15Y6L77yI..."
      Component {
        Type          CScript
        Name          base64 "546v6Lev5o6n5Yi2"     ← 非 ASCII 名一律 base64
        Position      [940, 555]
        Parameter {
          Variable      "Ts"
          Value         "1/(fc*200)"
          Show          off
        }
        Terminal {
          Type          Port
          Position      [55, 30]
          Direction     right
        }
      }
      Connection { ... }
    }

要点（踩过的坑）：
  * 语句形式：`Key<空格>value`；value 可以是
      - 裸标量：`[940, 555]`、`radau`、`off`、`-1`
      - 带引号字符串：`"Ts"`，内部 `\\"` 转义；可跨行续写，续行形如 `"..."`，
        语义就是纯拼接（PLECS 自己按 `\\n` 边界切段）
      - base64 字符串：`base64 "..."`，同样可跨行续写；非 ASCII 名/长文本都用它
  * 块：`Key {` ... `}`，可任意嵌套（Component 里套 Parameter / Terminal）
  * 行尾：本仓库实测是 LF，但别的文件可能是 CRLF → 读写都保留原样

本模块只做**文本级精确替换**：只动目标语句所在的那些行，其余字节一字不改，
改完回读校验，写盘前自动生成带时间戳的 `.bak`。
"""
from __future__ import annotations

import base64
import binascii
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

__all__ = ["PlecsFileError", "Node", "PlecsFile", "load",
           "render_tree", "render_ports", "render_params"]


class PlecsFileError(RuntimeError):
    """`.plecs` 解析/修改失败"""


# ── 字符串编解码 ──────────────────────────────────────────────────────────

_ESC = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


def _unescape(s: str) -> str:
    if "\\" not in s:
        return s
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            out.append(_ESC.get(s[i + 1], "\\" + s[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _escape(s: str) -> str:
    return (s.replace("\\", "\\\\").replace('"', '\\"')
             .replace("\r", "\\r").replace("\n", "\\n").replace("\t", "\\t"))


def _b64decode(s: str) -> str:
    try:
        raw = base64.b64decode(s + "=" * (-len(s) % 4))
    except (binascii.Error, ValueError) as e:
        raise PlecsFileError(f"base64 解码失败：{e}") from e
    for enc in ("utf-8", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _quoted(s: str) -> bool:
    """整行是否恰好是一个字符串字面量（`"..."`，含转义引号）。"""
    if len(s) < 2 or s[0] != '"' or s[-1] != '"':
        return False
    i, n = 1, len(s) - 1
    while i < n:
        if s[i] == "\\":
            i += 2
            continue
        if s[i] == '"':
            return False
        i += 1
    return i == n


def _literal_body(s: str) -> str:
    """去掉首尾引号。"""
    return s[1:-1]


def _split_literal(esc: str, width: int = 300) -> list[str]:
    """把已转义的字符串切回多行字面量。优先在 `\\n` 转义之后切，其次按宽度安全切。"""
    segs: list[str] = []
    cur: list[str] = []
    i, n = 0, len(esc)
    while i < n:
        if esc[i] == "\\" and i + 1 < n:
            cur.append(esc[i:i + 2])
            i += 2
            if esc[i - 1] == "n" and len(cur) >= 1:
                segs.append("".join(cur))
                cur = []
            continue
        cur.append(esc[i])
        i += 1
        if len(cur) >= width:
            segs.append("".join(cur))
            cur = []
    if cur:
        segs.append("".join(cur))
    return segs or [""]


# ── 语法树 ────────────────────────────────────────────────────────────────

@dataclass
class Node:
    key: str                              # Component / Parameter / Plecs / Terminal ...
    line: int                             # 起始行（0 基，指向 `Key {` 那行）
    end_line: int = -1                    # `}` 所在行
    attrs: dict[str, str] = field(default_factory=dict)          # 键 → 已解码文本
    attr_fmt: dict[str, str] = field(default_factory=dict)       # 键 → 'raw'|'str'|'b64'
    attr_span: dict[str, tuple[int, int]] = field(default_factory=dict)  # 键 → (首行, 末行)
    children: list["Node"] = field(default_factory=list)
    parent: "Node | None" = None
    path: str = ""                        # 组件路径，如 "T-3LEVEL1/环路控制"

    # ── 语义快捷方式 ──
    @property
    def name(self) -> str:
        return self.attrs.get("Name", "")

    @property
    def type(self) -> str:
        return self.attrs.get("Type", "") if self.key in ("Component", "Connection") else self.key

    def params(self) -> dict[str, str]:
        """Parameter 子块 → {Variable: Value}。"""
        out: dict[str, str] = {}
        for c in self.children:
            if c.key == "Parameter" and "Variable" in c.attrs:
                out[c.attrs["Variable"]] = c.attrs.get("Value", "")
        return out

    def param_node(self, var: str) -> "Node | None":
        for c in self.children:
            if c.key == "Parameter" and c.attrs.get("Variable") == var:
                return c
        return None

    def terminals(self) -> list["Node"]:
        return [c for c in self.children if c.key == "Terminal"]

    def components(self) -> list["Node"]:
        return [c for c in self.children if c.key == "Component"]


# ── 解析 ──────────────────────────────────────────────────────────────────

_KV_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.]*)\s+(\S.*)$")
_B64_RE = re.compile(r'^base64\s+"(.*)"$', re.S)


def _brace_open(stmt: str) -> str | None:
    """`Key {` → Key；不是块开头则 None。引号内的 `{` 不算。"""
    s = stmt.rstrip()
    if not s.endswith("{"):
        return None
    body = s[:-1].rstrip()
    # 引号奇偶校验：`Key "a{b"` 这种不算块
    if body.count('"') % 2:
        return None
    return body


class PlecsFile:
    """一个已载入内存的 .plecs 模型。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.exists():
            raise PlecsFileError(f"文件不存在：{self.path}")
        raw = self.path.read_bytes()
        if b"\x00" in raw[:4096]:
            raise PlecsFileError("文件含 NUL 字节，不是文本格式的 .plecs（旧版/二进制模型？）")
        self.newline = "\r\n" if b"\r\n" in raw[:200000] else "\n"
        text = raw.decode("utf-8", "replace")
        self.trailing_nl = text.endswith(("\n", "\r"))
        self.lines: list[str] = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if self.trailing_nl and self.lines and self.lines[-1] == "":
            self.lines.pop()
        self.root = self._parse()
        self._assign_paths(self.root, "")

    # ── 解析主体 ──
    def _parse(self) -> Node:
        lines = self.lines
        root = Node(key="<root>", line=-1, end_line=len(lines))
        stack: list[Node] = [root]
        i, n = 0, len(lines)
        while i < n:
            s = lines[i].strip()
            i += 1
            if not s or s.startswith("%"):
                continue
            if s == "}":
                if len(stack) == 1:
                    raise PlecsFileError(f"第 {i} 行出现多余的 `}}`")
                stack.pop().end_line = i - 1
                continue
            opened = _brace_open(s)
            if opened is not None:
                m = _KV_RE.match(opened)
                key = m.group(1) if m else opened
                node = Node(key=key, line=i - 1)
                node.parent = stack[-1]
                stack[-1].children.append(node)
                stack.append(node)
                continue
            m = _KV_RE.match(s)
            if not m:
                continue                      # 认不出的行（注释/续行残留）直接跳过
            key, rest = m.group(1), m.group(2).strip()
            start = i - 1
            mb = _B64_RE.match(rest)
            if mb:
                chunks = [mb.group(1)]
                while i < n and _quoted(lines[i].strip()):
                    chunks.append(_literal_body(lines[i].strip()))
                    i += 1
                node = stack[-1]
                node.attrs[key] = _b64decode("".join(chunks))
                node.attr_fmt[key] = "b64"
                node.attr_span[key] = (start, i - 1)
            elif rest.startswith('"'):
                if not _quoted(rest):
                    raise PlecsFileError(
                        f"第 {start + 1} 行字符串未闭合：{rest[:60]}…")
                parts = [_literal_body(rest)]
                while i < n and _quoted(lines[i].strip()):
                    parts.append(_literal_body(lines[i].strip()))
                    i += 1
                node = stack[-1]
                node.attrs[key] = _unescape("".join(parts))
                node.attr_fmt[key] = "str"
                node.attr_span[key] = (start, i - 1)
            else:
                node = stack[-1]
                node.attrs[key] = rest
                node.attr_fmt[key] = "raw"
                node.attr_span[key] = (start, start)
        if len(stack) != 1:
            raise PlecsFileError(f"花括号不平衡：还有 {len(stack) - 1} 个块未闭合")
        for c in root.children:
            if c.end_line < 0:
                c.end_line = n - 1
        return root

    def _assign_paths(self, node: Node, prefix: str) -> None:
        for c in node.children:
            if c.key == "Component":
                nm = c.attrs.get("Name", "")
                p = f"{prefix}/{nm}" if prefix else nm
                c.path = p
                self._assign_paths(c, p)
            else:
                c.path = c.path or prefix
                self._assign_paths(c, prefix)

    # ── 查询 ──
    def walk(self, node: Node | None = None):
        for c in (node or self.root).children:
            yield c
            yield from self.walk(c)

    def components(self) -> list[Node]:
        """全部 Component 节点（文档顺序，含 Circuit/Subsystem 内的嵌套）。"""
        return [n for n in self.walk() if n.key == "Component"]

    def depth_of(self, node: Node) -> int:
        """嵌套层级：顶层组件 = 0。"""
        d, p = 0, node.parent
        while p is not None:
            if p.key == "Component":
                d += 1
            p = p.parent
        return d

    def type_counts(self) -> dict[str, int]:
        d: dict[str, int] = {}
        for c in self.components():
            d[c.attrs.get("Type", "?")] = d.get(c.attrs.get("Type", "?"), 0) + 1
        return dict(sorted(d.items(), key=lambda kv: (-kv[1], kv[0])))

    @property
    def model(self) -> "Node":
        """真正的模型根（`Plecs { ... }` 块；`DemoSignature` 之类是它的兄弟节点）。"""
        for c in self.root.children:
            if c.key == "Plecs":
                return c
        return self.root

    def resolve(self, selector: str) -> Node:
        """`@`/`root` → 模型根；`#12` → 第 12 个组件；`A/B` → 路径；否则按名字（精确→唯一子串）。"""
        sel = (selector or "").strip()
        if not sel or sel in ("@", "root", "Plecs"):
            return self.model
        comps = self.components()
        if sel.startswith("#"):
            try:
                k = int(sel[1:])
            except ValueError:
                raise PlecsFileError(f"选择器格式错误：{sel}（应为 #序号，从 1 开始）")
            if not (1 <= k <= len(comps)):
                raise PlecsFileError(f"序号越界：{k}（共 {len(comps)} 个组件）")
            return comps[k - 1]
        if "/" in sel:
            hit = [c for c in comps if c.path == sel.strip("/")]
            if len(hit) == 1:
                return hit[0]
            tail = sel.strip("/").split("/")[-1].lower()
            near = [c.path for c in comps if tail in c.path.lower()][:6]
            raise PlecsFileError(
                f"路径 `{sel}` 没命中；相近的：" + ("、".join(near) if near else "无"))
        hit = [c for c in comps if c.attrs.get("Name") == sel]
        if len(hit) == 1:
            return hit[0]
        if not hit:
            low = sel.lower()
            hit = [c for c in comps if low in c.attrs.get("Name", "").lower()
                   or low in c.attrs.get("Type", "").lower()]
        if len(hit) == 1:
            return hit[0]
        if not hit:
            raise PlecsFileError(f"找不到组件 `{sel}`（用 plecs_file_blocks 先列清单）")
        names = "、".join(f"#{i + 1} {c.attrs.get('Name')}〔{c.attrs.get('Type')}〕"
                          for i, c in enumerate(comps) if c in hit[:8])
        raise PlecsFileError(f"`{sel}` 命中 {len(hit)} 个组件，请用路径或 #序号区分：{names}")

    def get_param(self, selector: str, var: str = "") -> tuple[Node, dict[str, str]]:
        node = self.resolve(selector)
        if not var:
            return node, node.params()
        pn = node.param_node(var)
        if pn is not None:
            return node, {var: pn.attrs.get("Value", "")}
        if var in node.attrs:                     # 模型级设置：TimeSpan / Solver / ...
            return node, {var: node.attrs[var]}
        avail = list(node.params()) + [k for k in node.attrs if k not in ("Name", "Type")]
        raise PlecsFileError(
            f"`{node.attrs.get('Name') or node.key}` 没有参数 `{var}`；"
            f"可用：{', '.join(avail) or '（无）'}")

    # ── 修改 ──
    def set_attr(self, node: Node, key: str, value: str) -> bool:
        """改写 node 的某个属性（保持原有编码方式与缩进）。返回是否真的有变化。"""
        old = node.attrs.get(key)
        fmt = node.attr_fmt.get(key, "str")
        if old == value:
            return False
        span = node.attr_span.get(key)
        if span is None:
            raise PlecsFileError(f"节点 `{node.key}` 没有属性 `{key}`")
        a, b = span
        first = self.lines[a]
        indent_raw = first[:len(first) - len(first.lstrip())]
        # 首行里 "Key" 之后到 value 之间的原始空白（保留列对齐）
        m = _KV_RE.match(first.strip())
        gap = ""
        if m:
            gap = first.strip()[len(m.group(1)):]
            gap = gap[:len(gap) - len(gap.lstrip())]
        cont_indent = ""
        if b > a:
            nxt = self.lines[a + 1]
            cont_indent = nxt[:len(nxt) - len(nxt.lstrip())]

        if fmt == "raw":
            new_lines = [f"{indent_raw}{key}{gap}{value}"]
        elif fmt == "b64":
            enc = base64.b64encode(value.encode("utf-8")).decode("ascii")
            chunks = [enc[i:i + 76] for i in range(0, len(enc), 76)] or [""]
            new_lines = [f'{indent_raw}{key}{gap}base64 "{chunks[0]}"']
            new_lines += [f'{cont_indent}"{c}"' for c in chunks[1:]]
        else:
            segs = _split_literal(_escape(value))
            new_lines = [f'{indent_raw}{key}{gap}"{segs[0]}"']
            new_lines += [f'{cont_indent}"{s}"' for s in segs[1:]]

        delta = len(new_lines) - (b - a + 1)
        self.lines[a:b + 1] = new_lines
        node.attrs[key] = value
        node.attr_span[key] = (a, a + len(new_lines) - 1)
        if delta:
            self._shift(a + len(new_lines), delta)
            node.end_line += delta
            p = node.parent
            while p is not None:
                if p.end_line > a:
                    p.end_line += delta
                p = p.parent
        return True

    def _shift(self, from_line: int, delta: int) -> None:
        """属性行数变化后，修正其后续所有节点的行号。"""
        for nd in self.walk():
            if nd.line >= from_line:
                nd.line += delta
            if nd.end_line >= from_line:
                nd.end_line += delta
            for k, (a, b) in list(nd.attr_span.items()):
                if a >= from_line:
                    nd.attr_span[k] = (a + delta, b + delta)

    def set_param(self, selector: str, var: str, value: str) -> tuple[str, str]:
        """改 Parameter 里的值（也支持 `@` 根上的模型设置如 TimeSpan）。"""
        node = self.resolve(selector)
        pn = node.param_node(var)
        if pn is not None:
            old = pn.attrs.get("Value", "")
            self.set_attr(pn, "Value", value)
            return old, value
        if var in node.attrs and var not in ("Parameter",):
            old = node.attrs[var]
            self.set_attr(node, var, value)
            return old, value
        raise PlecsFileError(
            f"`{node.attrs.get('Name') or node.key}` 里没有 `{var}`；"
            f"可用：{', '.join(list(node.params()) + [k for k in node.attrs if k not in ('Name', 'Type')])}")

    # ── 落盘 ──
    def text(self) -> str:
        body = self.newline.join(self.lines)
        return body + self.newline if self.trailing_nl else body

    def save(self, backup: bool = True) -> str:
        bak = ""
        if backup and self.path.exists():
            bak = str(self.path) + time.strftime(".%Y%m%d-%H%M%S.bak")
            Path(bak).write_bytes(self.path.read_bytes())
        self.path.write_bytes(self.text().encode("utf-8"))
        return bak


def load(path: str | Path) -> PlecsFile:
    return PlecsFile(path)


# ── 渲染 ──────────────────────────────────────────────────────────────────

def render_tree(f: PlecsFile, max_depth: int = 2) -> str:
    """组件树：按文档顺序列出组件，Subsystem 标出内部组件数。"""
    rows: list[str] = []

    def desc_comps(node: Node) -> int:
        return sum(1 for n in f.walk(node) if n.key == "Component")

    for c in f.components():
        d = f.depth_of(c)
        if d >= max_depth:
            continue
        mark = ""
        if c.attrs.get("Type") == "Subsystem":
            mark = f"  〔内含 {desc_comps(c)} 组件〕"
        rows.append("  " * d + f"- {c.attrs.get('Name')}  [{c.attrs.get('Type', '?')}]{mark}")
    return "\n".join(rows) if rows else "（模型里没有 Component 块）"


def render_ports(node: Node) -> str:
    """组件的端口表。Subsystem 有显式 Terminal；CScript 等由 NumInputs/NumOutputs 决定。"""
    ts = node.terminals()
    if ts:
        rows = ["  序号  类别      方向       位置"]
        for i, t in enumerate(ts, 1):
            rows.append(f"  {i:>3}   {t.attrs.get('Type', '?'):<9} "
                        f"{t.attrs.get('Direction', '?'):<10} {t.attrs.get('Position', '')}")
        return "\n".join(rows)
    ps = node.params()
    if "NumInputs" in ps or "NumOutputs" in ps:
        return (f"  （无显式 Terminal，端口数由参数决定）\n"
                f"  NumInputs  = {ps.get('NumInputs', '?')}\n"
                f"  NumOutputs = {ps.get('NumOutputs', '?')}")
    return "（该块无端口声明）"


def render_params(node: Node, head: int = 0) -> str:
    """参数表；head>0 时对长文本只显示前 head 字（默认全显示）。"""
    ps = node.params()
    if not ps:
        extra = {k: v for k, v in node.attrs.items() if k not in ("Name", "Type")}
        if not extra:
            return "（无参数）"
        return "\n".join(f"  {k:<18} = {v}" for k, v in extra.items())
    rows = []
    for k, v in ps.items():
        vv = v if (not head or len(v) <= head) else v[:head] + f"…（共 {len(v)} 字）"
        if "\n" in vv:
            vv = vv.replace("\n", "\n" + " " * 22)
        rows.append(f"  {k:<18} = {vv}")
    return "\n".join(rows)
