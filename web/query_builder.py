"""帖子高级查询：把「条件树」或「类 SQL 表达式」编译成参数化 SQL 片段。

对外只用两个入口：
- `compile_adv(adv: str) -> tuple[str, list[Any]]`：自动识别输入是条件树 JSON 还是表达式文本，
  返回可直接拼到 WHERE 后的子句与参数（恒为参数化，不接受任何 SQL 文本直通）；
- 出错抛 `QueryError`（由 API 层转 400，把原因明确回给用户）。

安全边界（重要）：
- 字段、操作符、排序一律走白名单，任何不在白名单内的标识符直接拒绝；
- 值全部通过 `?` 占位符传参，不做任何字符串拼接；
- 限制嵌套深度、条件总数、IN 的元素个数，防止超长/恶意请求拖垮查询。

条件树结构（可视化构建器产出，也是表达式解析的产物）：
    {"op": "AND", "rules": [
        {"field": "likes", "op": "gt", "value": "100"},
        {"op": "OR", "rules": [ ... ]}          # 有 rules 即为分组
    ]}
"""
from __future__ import annotations

import json
import re
from typing import Any

import db

# ---- 白名单 ----

# 字段 -> (SQL 列名表达式, 类型)。likes/replies/created_at 在库内是 TEXT，
# 数值比较必须 CAST，否则会按字符串比较（'9' > '100'）。
FIELDS: dict[str, tuple[str, str]] = {
    "fid": ("fid", "text"),
    "title": ("title", "text"),
    "author": ("author", "text"),
    "url": ("url", "text"),
    "likes": (db.numeric_expr("likes"), "number"),
    "replies": (db.numeric_expr("replies"), "number"),
    "date": ("date", "date"),
    "update_date": ("update_date", "date"),
}

# 操作符 -> (SQL 模板, 需要的值个数；-1 表示变长 IN)
OPERATORS: dict[str, tuple[str, int]] = {
    "eq": ("{col} = ?", 1),
    "ne": ("{col} != ?", 1),
    "gt": ("{col} > ?", 1),
    "gte": ("{col} >= ?", 1),
    "lt": ("{col} < ?", 1),
    "lte": ("{col} <= ?", 1),
    "contains": ("{col} LIKE ? ESCAPE '\\'", 1),
    # like：表达式专用——用户显式写的 % _ 视为通配符（SQL 语义），
    # 与 contains（构建器用，转义用户输入、自动补 %）区分开
    "like": ("{col} LIKE ? ESCAPE '\\'", 1),
    "not_contains": ("{col} NOT LIKE ? ESCAPE '\\'", 1),
    "starts_with": ("{col} LIKE ? ESCAPE '\\'", 1),
    "between": ("{col} BETWEEN ? AND ?", 2),
    "in": ("{col} IN ({ph})", -1),
    "not_in": ("{col} NOT IN ({ph})", -1),
    "is_empty": ("({col} IS NULL OR {col} = '')", 0),
    "is_not_empty": ("({col} IS NOT NULL AND {col} != '')", 0),
}

# 各类型可用的操作符
FIELD_OPERATORS: dict[str, set[str]] = {
    "text": {"eq", "ne", "contains", "not_contains", "starts_with", "like", "in", "not_in", "is_empty", "is_not_empty"},
    "number": {"eq", "ne", "gt", "gte", "lt", "lte", "between", "is_empty", "is_not_empty"},
    "date": {"eq", "ne", "gt", "gte", "lt", "lte", "between", "is_empty", "is_not_empty"},
}

# 上限：防超长/恶意请求
MAX_DEPTH = 3
MAX_NODES = 60
MAX_IN_VALUES = 50


class QueryError(ValueError):
    """高级查询条件不合法（字段/操作符越界、结构错误、语法错误等）。"""


def _escape_like(value: str) -> str:
    """转义 LIKE 通配符，避免用户输入的 % _ 被当作通配符。"""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_value(operator: str, value: str) -> str:
    if operator == "contains" or operator == "not_contains":
        return f"%{_escape_like(value)}%"
    if operator == "starts_with":
        return f"{_escape_like(value)}%"
    return value


def _split_values(raw: Any) -> list[str]:
    """把值切成列表：支持数组或逗号分隔字符串（IN/NOT IN 用）。"""
    if isinstance(raw, list):
        items = [str(v).strip() for v in raw]
    else:
        items = [v.strip() for v in str(raw).split(",")]
    return [v for v in items if v != ""]


def _build_condition(field: str, operator: str, raw_value: Any) -> tuple[str, list[Any]]:
    """编译单个条件为 (sql, params)。"""
    if field not in FIELDS:
        raise QueryError(f"不支持的字段：{field}（可用：{', '.join(sorted(FIELDS))}）")
    col, ftype = FIELDS[field]
    if operator not in OPERATORS:
        raise QueryError(f"不支持的操作符：{operator}")
    if operator not in FIELD_OPERATORS[ftype]:
        raise QueryError(f"字段 {field}（{ftype}）不支持操作符 {operator}")

    template, need = OPERATORS[operator]
    if need == 0:
        return template.format(col=col), []

    if operator in ("in", "not_in"):
        items = _split_values(raw_value)
        if not items:
            raise QueryError(f"字段 {field} 的 {operator} 需要至少一个值")
        if len(items) > MAX_IN_VALUES:
            raise QueryError(f"字段 {field} 的 {operator} 最多 {MAX_IN_VALUES} 个值")
        sql = template.format(col=col, ph=",".join("?" * len(items)))
        return sql, items

    if operator == "between":
        if isinstance(raw_value, list) and len(raw_value) == 2:
            a, b = str(raw_value[0]), str(raw_value[1])
        else:
            parts = _split_values(raw_value)
            if len(parts) != 2:
                raise QueryError(f"字段 {field} 的 between 需要两个值（起始, 结束）")
            a, b = parts
        return template.format(col=col), [a, b]

    value = "" if raw_value is None else str(raw_value)
    if ftype == "number":
        # 数值字段：校验可解析为数字，避免 CAST 出意外结果
        try:
            number = int(value)
        except ValueError:
            raise QueryError(f"字段 {field} 需要数字，收到：{value!r}") from None
        # 传真正的整数：SQLite 比较时类型亲和性依赖参数类型，
        # 传字符串 '100' 会让「数字 < 文本」规则生效，导致结果恒为空
        return template.format(col=col), [number]
    if operator in ("contains", "not_contains", "starts_with"):
        return template.format(col=col), [_like_value(operator, value)]
    if operator == "like":
        # 不转义：表达式里的 % _ 由用户自己控制（通配符语义）
        return template.format(col=col), [value]
    return template.format(col=col), [value]


class _Counter:
    """编译期节点计数，超过上限即拒绝。"""

    def __init__(self) -> None:
        self.n = 0

    def add(self) -> None:
        self.n += 1
        if self.n > MAX_NODES:
            raise QueryError(f"条件过多（上限 {MAX_NODES} 个）")


def _build_node(node: Any, depth: int, counter: _Counter) -> tuple[str, list[Any]]:
    """递归编译节点：分组（含 rules）或单个条件（含 field）。"""
    if not isinstance(node, dict):
        raise QueryError("条件节点必须是对象")
    counter.add()

    if "rules" in node:
        if depth >= MAX_DEPTH:
            raise QueryError(f"分组嵌套过深（上限 {MAX_DEPTH} 层）")
        op = str(node.get("op", "AND")).upper()
        if op not in ("AND", "OR", "NOT"):
            raise QueryError(f"分组连接符只能是 AND / OR / NOT，收到：{op}")
        rules = node.get("rules") or []
        if not isinstance(rules, list) or not rules:
            raise QueryError("分组内至少需要一个条件")
        parts: list[str] = []
        params: list[Any] = []
        for child in rules:
            sql, ps = _build_node(child, depth + 1, counter)
            parts.append(sql)
            params.extend(ps)
        if op == "NOT":
            if len(parts) != 1:
                raise QueryError("NOT 分组内只能有一个条件")
            return f"(NOT {parts[0]})", params
        return f"({f' {op} '.join(parts)})", params

    if "field" in node:
        field = str(node.get("field", ""))
        operator = str(node.get("op") or node.get("operator") or "eq")
        return _build_condition(field, operator, node.get("value"))

    raise QueryError("条件节点缺少 field 或 rules")


# ---- 表达式解析（类 SQL / KQL 子集）----

_TOKEN_RE = re.compile(
    r"""\s*(?:
        (?P<str>"[^"]*"|'[^']*')      |
        (?P<num>\d+(?:\.\d+)?)        |
        (?P<op>>=|<=|!=|<>|=|>|<)     |
        (?P<lp>\() | (?P<rp>\))       |
        (?P<comma>,)                  |
        (?P<word>[A-Za-z_][A-Za-z0-9_]*)
    )""",
    re.VERBOSE,
)

# 表达式里的操作符别名 -> 内部操作符
_EXPR_OPS = {
    "=": "eq", "==": "eq", "!=": "ne", "<>": "ne", ">": "gt", ">=": "gte",
    "<": "lt", "<=": "lte", "like": "like", "in": "in", "not_in": "not_in",
    "between": "between",
}


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    pos = 0
    while pos < len(text):
        if text[pos].isspace():
            pos += 1
            continue
        m = _TOKEN_RE.match(text, pos)
        if not m:
            raise QueryError(f"无法解析的字符：{text[pos]!r}（位置 {pos}）")
        pos = m.end()
        kind = m.lastgroup or ""
        value = m.group(kind) or ""
        if kind == "str":
            tokens.append(("value", value[1:-1]))
        elif kind == "num":
            # 数字既可能当值（likes > 100），也可能当字段名的一部分（词法不区分，交给语法层）
            tokens.append(("num", value))
        elif kind == "op":
            tokens.append(("op", value))
        elif kind == "lp":
            tokens.append(("lp", "("))
        elif kind == "rp":
            tokens.append(("rp", ")"))
        elif kind == "comma":
            tokens.append(("comma", ","))
        elif kind == "word":
            tokens.append(("word", value))
    return tokens


class _Parser:
    """递归下降解析：expr := term (('AND'|'OR') term)*；term := '(' expr ')' | condition。"""

    def __init__(self, tokens: list[tuple[str, str]]) -> None:
        self.tokens = tokens
        self.i = 0

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def next(self) -> tuple[str, str]:
        tok = self.peek()
        if tok is None:
            raise QueryError("表达式意外结束")
        self.i += 1
        return tok

    def expect_word(self, expected: str) -> None:
        kind, value = self.next()
        if kind != "word" or value.upper() != expected:
            raise QueryError(f"期望关键字 {expected}，收到：{value!r}")

    def parse(self) -> dict[str, Any]:
        node = self.parse_or()
        if self.peek() is not None:
            raise QueryError(f"表达式末尾有多余内容：{self.peek()[1]!r}")
        return node

    def parse_or(self) -> dict[str, Any]:
        left = self.parse_and()
        rules = [left]
        while True:
            tok = self.peek()
            if tok and tok[0] == "word" and tok[1].upper() == "OR":
                _ = self.next()
                rules.append(self.parse_and())
            else:
                break
        if len(rules) == 1:
            return left
        return {"op": "OR", "rules": rules}

    def parse_and(self) -> dict[str, Any]:
        left = self.parse_term()
        rules = [left]
        while True:
            tok = self.peek()
            if tok and tok[0] == "word" and tok[1].upper() == "AND":
                _ = self.next()
                rules.append(self.parse_term())
            else:
                break
        if len(rules) == 1:
            return left
        return {"op": "AND", "rules": rules}

    def parse_term(self) -> dict[str, Any]:
        tok = self.peek()
        if tok is None:
            raise QueryError("表达式意外结束")
        if tok[0] == "lp":
            _ = self.next()
            node = self.parse_or()
            nxt = self.next()
            if nxt[0] != "rp":
                raise QueryError("缺少右括号 )")
            return node
        if tok[0] == "word" and tok[1].upper() == "NOT":
            _ = self.next()
            inner = self.parse_term()
            # SQLite 支持 NOT，用一元取反包装
            return {"op": "AND", "rules": [{"op": "NOT", "rules": [inner]}]}
        return self.parse_condition()

    def parse_condition(self) -> dict[str, Any]:
        kind, field = self.next()
        if kind != "word":
            raise QueryError(f"期望字段名，收到：{field!r}")
        field = field.lower()

        tok = self.next()
        if tok[0] == "word":
            word = tok[1].lower()
            if word in ("is",):
                nxt = self.next()
                if nxt[0] != "word":
                    raise QueryError("IS 后应为 NULL / EMPTY")
                name = nxt[1].lower()
                if name == "null":
                    return {"field": field, "op": "is_empty", "value": ""}
                if name == "empty":
                    return {"field": field, "op": "is_empty", "value": ""}
                raise QueryError(f"不支持的 IS {nxt[1]}")
            if word in ("like", "in", "between", "not_in"):
                operator = _EXPR_OPS[word]
            else:
                raise QueryError(f"不支持的操作符：{tok[1]}")
        elif tok[0] == "op":
            operator = _EXPR_OPS.get(tok[1])
            if operator is None:
                raise QueryError(f"不支持的操作符：{tok[1]}")
        else:
            raise QueryError(f"期望操作符，收到：{tok[1]!r}")

        if operator in ("in", "not_in"):
            nxt = self.next()
            if nxt[0] != "lp":
                raise QueryError("IN 后应为 (值1, 值2)")
            values: list[str] = []
            while True:
                v = self.next()
                if v[0] == "rp":
                    break
                if v[0] == "comma":
                    continue
                values.append(v[1])
            return {"field": field, "op": operator, "value": values}

        if operator == "between":
            v1 = self.next()
            self.expect_word("AND")
            v2 = self.next()
            return {"field": field, "op": "between", "value": [v1[1], v2[1]]}

        v = self.next()
        if v[0] not in ("value", "word", "num"):
            raise QueryError(f"期望值，收到：{v[1]!r}")
        return {"field": field, "op": operator, "value": v[1]}


def parse_expression(text: str) -> dict[str, Any]:
    """解析类 SQL 表达式为条件树。语法示例：

        fid IN (2,4) AND likes > 100 AND (title LIKE '%教程%' OR author = 'x')
        date BETWEEN '2026-09-01' AND '2026-09-07'
    """
    tokens = _tokenize(text)
    if not tokens:
        raise QueryError("表达式为空")
    return _Parser(tokens).parse()


def compile_adv(adv: str) -> tuple[str, list[Any]]:
    """把高级查询条件编译为 (SQL 子句, 参数)。

    输入既可以是条件树 JSON（可视化构建器），也可以是表达式文本（高级模式）。
    恒返回参数化 SQL，不接受任何 SQL 文本直通。
    """
    text = (adv or "").strip()
    if not text:
        raise QueryError("高级查询条件为空")
    if text.startswith("{"):
        try:
            node = json.loads(text)
        except json.JSONDecodeError as e:
            raise QueryError(f"条件 JSON 解析失败：{e}") from None
    else:
        node = parse_expression(text)
    counter = _Counter()
    sql, params = _build_node(node, 1, counter)
    return sql, params
