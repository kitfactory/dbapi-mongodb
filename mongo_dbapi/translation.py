from __future__ import annotations

import re
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Mapping
from dataclasses import replace

from sqlglot import parse_one, exp

from .errors import raise_error


PLACEHOLDER_PATTERN = re.compile(r"%s")
NAMED_PLACEHOLDER_PATTERN = re.compile(r"%\((?P<name>[^)]+)\)s")
PARAM_TOKEN_TEMPLATE = "__param_{index}__"
PARAM_NAMED_TEMPLATE = "__param_{name}__"

CREATE_INDEX_RE = re.compile(
    r"^create\s+(unique\s+)?index\s+([A-Za-z_][\w-]*)\s+on\s+([A-Za-z_][\w-]*)\s*\(([^)]+)\)",
    re.IGNORECASE,
)
DROP_INDEX_RE = re.compile(
    r"^drop\s+index\s+([A-Za-z_][\w-]*)\s+on\s+([A-Za-z_][\w-]*)", re.IGNORECASE
)


@dataclass
class QueryParts:
    """Mongo query parts / Mongo クエリ部品"""

    operation: str
    collection: str
    filter: Dict[str, Any] | None = None
    projection: List[str] | None = None
    projection_paths: List[tuple[str, str]] | None = None
    sort: List[tuple[str, int]] | None = None
    limit: int | None = None
    skip: int | None = None
    values: Dict[str, Any] | None = None
    update: Dict[str, Any] | None = None
    pipeline: List[Dict[str, Any]] | None = None
    index_keys: List[tuple[str, int]] | None = None
    index_name: str | None = None
    unique: bool = False
    union_parts: List["QueryParts"] | None = None
    subqueries: dict[str, dict[str, Any]] | None = None
    inline_token: str | None = None
    inline_rows: list[dict[str, Any]] | None = None
    inline_aggregates: list[tuple[str, str, str | None]] | None = None
    uses_window: bool = False


def preprocess_sql(sql: str, params: Sequence[Any] | Mapping[str, Any] | None) -> tuple[str, list[Any], list[str]]:
    """Replace placeholders with param tokens and validate / プレースホルダーを置換し検証"""
    params_seq: list[Any] = []
    tokens: list[str] = []
    new_sql = sql
    named_matches = list(NAMED_PLACEHOLDER_PATTERN.finditer(sql))
    if named_matches:
        if not isinstance(params, Mapping):
            raise_error("[mdb][E4]")
        used = []
        for m in named_matches:
            name = m.group("name")
            if name not in params:
                raise_error("[mdb][E4]")
            token = PARAM_NAMED_TEMPLATE.format(name=name)
            new_sql = new_sql.replace(m.group(0), token, 1)
            params_seq.append(params[name])
            tokens.append(token)
            used.append(name)
        if len(used) != len(params):
            raise_error("[mdb][E4]")
        return new_sql, params_seq, tokens
    matches = list(PLACEHOLDER_PATTERN.finditer(sql))
    count = len(matches)
    params_list = list(params or [])
    if count != len(params_list):
        raise_error("[mdb][E4]")
    for idx, _ in enumerate(matches):
        token = PARAM_TOKEN_TEMPLATE.format(index=idx)
        new_sql = new_sql.replace("%s", token, 1)
        params_seq.append(params_list[idx])
        tokens.append(token)
    return new_sql, params_seq, tokens


def _register_subquery(
    sub_expr: exp.Expression, params_map: dict[str, Any], parent_subqueries: dict[str, dict[str, Any]], mode: str
) -> str:
    """Register subquery and return placeholder token / サブクエリを登録しトークンを返す"""
    # Collect nested subqueries separately to keep scopes isolated
    sub_collector: dict[str, dict[str, Any]] = {}
    if isinstance(sub_expr, exp.Subquery):
        sub_expr = sub_expr.this
    inner_select = getattr(sub_expr, "this", None)
    if not isinstance(sub_expr, exp.Select) and isinstance(inner_select, exp.Select):
        sub_expr = inner_select
    if not isinstance(sub_expr, exp.Select):
        raise_error("[mdb][E2]", "Unsupported SQL construct: SUBQUERY")
    if sub_expr.args.get("with_"):
        expanded = _expand_with_clause(sub_expr)
        if not isinstance(expanded, exp.Select):
            raise_error("[mdb][E2]", "Unsupported SQL construct: CTE")
        sub_expr = expanded

    # Reject correlated subqueries (non-correlated only) /
    # 相関サブクエリは未対応（非相関のみ許可）
    defined_tables: set[str] = set()
    for t in sub_expr.find_all(exp.Table):
        if t.name:
            defined_tables.add(t.name)
        alias = t.alias_or_name
        if alias:
            defined_tables.add(alias)
    for j in sub_expr.args.get("joins") or []:
        jt = getattr(getattr(j.this, "this", None), "name", None)
        if jt:
            defined_tables.add(jt)
        jalias = getattr(j.this, "alias_or_name", None)
        if jalias:
            defined_tables.add(jalias)
    # If the subquery references a qualified column whose table/alias is not defined inside,
    # treat it as correlated and reject (skip nested subqueries' scopes). /
    # サブクエリ内で未定義のテーブル/alias修飾カラムを参照している場合は相関とみなし拒否（内側サブクエリのスコープは除外）
    def _iter_scope_columns(n: exp.Expression) -> list[exp.Column]:
        cols: list[exp.Column] = []

        def _walk(x: exp.Expression) -> None:
            if isinstance(x, exp.Subquery):
                return
            if isinstance(x, exp.Column):
                cols.append(x)
            for v in x.args.values():
                if isinstance(v, exp.Expression):
                    _walk(v)
                elif isinstance(v, list):
                    for e in v:
                        if isinstance(e, exp.Expression):
                            _walk(e)

        _walk(n)
        return cols

    for col in _iter_scope_columns(sub_expr):
        if col.table and col.table not in defined_tables:
            raise_error("[mdb][E2]", "Unsupported SQL construct: CORRELATED_SUBQUERY")

    sub_parts = _parse_select_like(sub_expr, params_map, sub_collector)
    sub_parts.subqueries = sub_collector or None
    token = f"__subquery_{len(parent_subqueries)}__"
    parent_subqueries[token] = {"parts": sub_parts, "mode": mode}
    return token


def _literal_value(
    node: exp.Expression, params_map: dict[str, Any], subqueries: dict[str, dict[str, Any]]
) -> Any:
    """Extract value from SQLGlot expression / SQLGlot 式から値を取得"""
    if isinstance(node, exp.Literal):
        if node.is_string:
            return node.this
        try:
            return node.to_python()
        except Exception:
            try:
                return int(node.this)
            except Exception:
                try:
                    return float(node.this)
                except Exception:
                    return node.this
    if isinstance(node, exp.Identifier):
        if node.name in params_map:
            return params_map[node.name]
        raise_error("[mdb][E2]", "Unsupported SQL construct: IDENTIFIER_AS_VALUE")
    if isinstance(node, exp.Column):
        name = ".".join(part.name for part in node.parts if hasattr(part, "name"))
        if name in params_map:
            return params_map[name]
        raise_error("[mdb][E2]", "Unsupported SQL construct: COLUMN_AS_VALUE")
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if isinstance(node, (exp.Subquery, exp.Select)):
        return _register_subquery(node, params_map, subqueries, mode="values")
    if isinstance(node, exp.Tuple):
        return [_literal_value(e, params_map, subqueries) for e in node.expressions]
    raise_error("[mdb][E2]")


def _is_subquery_node(node: Any) -> bool:
    return isinstance(node, (exp.Subquery, exp.Select)) or (
        isinstance(node, exp.Expression) and bool(node.find(exp.Select))
    )


def _field_name(node: exp.Expression, params_map: dict[str, Any]) -> str:
    """Extract field name / フィールド名を抽出"""
    if isinstance(node, exp.Column):
        # Prefer column name without table prefix to match Mongo field / Mongo のフィールド名にテーブル接頭辞を付けない
        if node.table:
            return node.name
        return ".".join(part.name for part in node.parts if hasattr(part, "name"))
    if isinstance(node, exp.Identifier):
        return node.name
    if isinstance(node, exp.Literal) and node.is_string:
        return node.this
    if isinstance(node, exp.Column) and node.name in params_map:
        raise_error("[mdb][E2]", "Unsupported SQL construct: PARAM_AS_FIELD")
    if isinstance(node, (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)) and node.alias_or_name:
        return node.alias_or_name
    if isinstance(node, (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)):
        if hasattr(node, "this") and node.this:
            base = _field_name(node.this, params_map)
            return f"{node.__class__.__name__.lower()}_{base}"
        return f"{node.__class__.__name__.lower()}_{len(params_map)}"
    raise_error("[mdb][E2]")


def _column_table_field(node: exp.Expression) -> tuple[str | None, str]:
    """Return (table, field) for Column / カラムのテーブル名とフィールド名を返す"""
    if isinstance(node, exp.Column):
        tbl = node.table
        name = node.name
        return tbl, name
    raise_error("[mdb][E2]")


def _field_with_alias(node: exp.Expression, alias_map: dict[str, str]) -> str:
    if isinstance(node, exp.Column):
        tbl = node.table or ""
        fld = node.name
        if tbl and tbl in alias_map:
            return f"{alias_map[tbl]}{fld}"
        if not tbl and "" in alias_map:
            return f"{alias_map['']}{fld}"
        if not tbl and fld in alias_map:
            return f"{alias_map.get(fld, '')}{fld}"
        raise_error("[mdb][E2]")
    if isinstance(node, (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)):
        alias = node.alias_or_name
        if not alias and hasattr(node, "this") and node.this:
            base = None
            if isinstance(node.this, exp.Column):
                base = node.this.name
            elif isinstance(node.this, exp.Identifier):
                base = node.this.name
            alias = f"{node.__class__.__name__.lower()}_{base or '0'}"
        if alias:
            if alias_map and alias in alias_map:
                return alias
            return alias
    raise_error("[mdb][E2]")


def _like_to_regex(pattern: str) -> str:
    """Convert SQL LIKE pattern to regex / LIKE パターンを正規表現へ"""
    escaped = ""
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "%":
            escaped += ".*"
        elif ch == "_":
            escaped += "."
        elif ch == "\\" and i + 1 < len(pattern):
            escaped += re.escape(pattern[i + 1])
            i += 1
        else:
            escaped += re.escape(ch)
        i += 1
    return f"^{escaped}$"


def _case_to_cond(case_expr: exp.Case, params_map: dict[str, Any], subqueries: dict[str, dict[str, Any]]) -> Any:
    """Convert a simple CASE WHEN ... THEN ... ELSE ... END to $cond / 簡易 CASE を $cond に変換"""
    whens = case_expr.args.get("ifs") or []
    default = case_expr.args.get("default")
    # Support single WHEN branch only
    if not whens:
        raise_error("[mdb][E2]", "Unsupported SQL construct: CASE")
    when = whens[0]
    cond = when.this
    then_expr = when.expression
    else_expr = default or exp.Literal.number(0)
    # Only support equality comparison for condition
    if isinstance(cond, exp.EQ):
        left = _field_name(cond.left, params_map)
        right = _literal_value(cond.right, params_map, subqueries)
        condition = {"$eq": [f"${left}", right]}
    else:
        raise_error("[mdb][E2]", "Unsupported SQL construct: CASE")
    try:
        then_val = _literal_value(then_expr, params_map, subqueries)
    except Exception:
        then_val = getattr(then_expr, "this", None)
    try:
        else_val = _literal_value(else_expr, params_map, subqueries)
    except Exception:
        else_val = getattr(else_expr, "this", None)
    return {"$cond": [condition, then_val, else_val]}


def _condition_to_filter(
    node: exp.Expression, params_map: dict[str, Any], subqueries: dict[str, dict[str, Any]]
) -> Dict[str, Any]:
    """Convert WHERE expression to Mongo filter / WHERE を Mongo フィルタへ変換"""
    if isinstance(node, exp.And):
        parts = []
        if node.expressions:
            parts = [_condition_to_filter(e, params_map, subqueries) for e in node.expressions]
        else:
            parts = [
                _condition_to_filter(node.this, params_map, subqueries),
                _condition_to_filter(node.expression, params_map, subqueries),
            ]
        return {"$and": parts}
    if isinstance(node, exp.Or):
        parts = []
        if node.expressions:
            parts = [_condition_to_filter(e, params_map, subqueries) for e in node.expressions]
        else:
            parts = [
                _condition_to_filter(node.this, params_map, subqueries),
                _condition_to_filter(node.expression, params_map, subqueries),
            ]
        return {"$or": parts}
    if isinstance(node, exp.Not):
        inner = node.this
        if isinstance(inner, exp.Exists):
            sub_expr = inner.this
            token = _register_subquery(sub_expr, params_map, subqueries, mode="exists")
            return {"$expr": {"$not": [{"$literal": token}]}}
        if isinstance(inner, exp.In):
            field = _field_name(inner.this, params_map)
            expr_val = inner.expression or inner.args.get("query") or inner.args.get("expressions")
            if isinstance(expr_val, (exp.Subquery, exp.Select)) or (
                isinstance(expr_val, exp.Expression) and expr_val.find(exp.Select)
            ):
                token = _register_subquery(expr_val, params_map, subqueries, mode="values")
                values = token
            else:
                if isinstance(expr_val, list):
                    values = [_literal_value(v, params_map, subqueries) for v in expr_val]
                else:
                    values = _literal_value(expr_val, params_map, subqueries)
            return {field: {"$nin": values}}
        if isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null):
            field = _field_name(inner.this, params_map)
            return {field: {"$ne": None, "$exists": True}}
        if isinstance(inner, exp.Like):
            field = _field_name(inner.this, params_map)
            value = _literal_value(inner.expression, params_map, subqueries)
            regex = _like_to_regex(str(value))
            return {field: {"$not": {"$regex": regex}}}
        if hasattr(exp, "ILike") and isinstance(inner, getattr(exp, "ILike")):
            field = _field_name(inner.this, params_map)
            value = _literal_value(inner.expression, params_map, subqueries)
            regex = _like_to_regex(str(value))
            return {field: {"$not": {"$regex": regex, "$options": "i"}}}
        if isinstance(inner, exp.EQ):
            field = _field_name(inner.left, params_map)
            value = _literal_value(inner.right, params_map, subqueries)
            return {field: {"$ne": value}}
        raise_error("[mdb][E2]", "Unsupported SQL construct: NOT")
    if isinstance(node, exp.Is) and isinstance(node.expression, exp.Null):
        field = _field_name(node.this, params_map)
        return {field: None}
    if isinstance(node, exp.Between):
        field = _field_name(node.this, params_map)
        low = _literal_value(node.args["low"], params_map, subqueries)
        high = _literal_value(node.args["high"], params_map, subqueries)
        return {field: {"$gte": low, "$lte": high}}
    if isinstance(node, exp.Like):
        field = _field_name(node.this, params_map)
        value = _literal_value(node.expression, params_map, subqueries)
        if not isinstance(value, str):
            raise_error("[mdb][E2]", "Unsupported SQL construct: LIKE")
        regex = _like_to_regex(value)
        return {field: {"$regex": regex}}
    if hasattr(exp, "ILike") and isinstance(node, getattr(exp, "ILike")):
        field = _field_name(node.this, params_map)
        value = _literal_value(node.expression, params_map, subqueries)
        regex = _like_to_regex(str(value))
        return {field: {"$regex": regex, "$options": "i"}}
    def _strip_slashes(val: Any) -> str:
        sval = str(val)
        if sval.startswith("/") and sval.endswith("/") and len(sval) >= 2:
            return sval[1:-1]
        return sval

    if hasattr(exp, "Regex") and isinstance(node, getattr(exp, "Regex")):
        field = _field_name(node.this, params_map)
        pattern = _strip_slashes(_literal_value(node.expression, params_map, subqueries))
        return {field: {"$regex": str(pattern)}}
    if hasattr(exp, "RegexpLike") and isinstance(node, getattr(exp, "RegexpLike")):
        field = _field_name(node.this, params_map)
        pattern = _strip_slashes(_literal_value(node.expression, params_map, subqueries))
        return {field: {"$regex": str(pattern)}}
    if isinstance(node, exp.In):
        field = _field_name(node.this, params_map)
        expr_val = node.expression or node.args.get("query") or node.args.get("expressions")
        if _is_subquery_node(expr_val):
            token = _register_subquery(expr_val, params_map, subqueries, mode="values")
            values = token
        else:
            if isinstance(expr_val, list):
                values = [_literal_value(v, params_map, subqueries) for v in expr_val]
            else:
                values = _literal_value(expr_val, params_map, subqueries)
        return {field: {"$in": values}}
    if isinstance(node, exp.Exists):
        sub_expr = node.this
        token = _register_subquery(sub_expr, params_map, subqueries, mode="exists")
        return {"$expr": {"$literal": token}}
    if isinstance(node, exp.Paren):
        return _condition_to_filter(node.this, params_map, subqueries)
    if isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        left = node.left
        right = node.right
        field = _field_name(left, params_map)
        if _is_subquery_node(right):
            value = _register_subquery(right, params_map, subqueries, mode="scalar")
        else:
            value = _literal_value(right, params_map, subqueries)
        if isinstance(node, exp.EQ):
            return {field: value}
        if isinstance(node, exp.NEQ):
            return {field: {"$ne": value}}
        if isinstance(node, exp.GT):
            return {field: {"$gt": value}}
        if isinstance(node, exp.GTE):
            return {field: {"$gte": value}}
        if isinstance(node, exp.LT):
            return {field: {"$lt": value}}
        if isinstance(node, exp.LTE):
            return {field: {"$lte": value}}
    raise_error("[mdb][E2]")


def _condition_to_filter_join(
    node: exp.Expression, params_map: dict[str, Any], allowed_table: str, subqueries: dict[str, dict[str, Any]]
) -> Dict[str, Any]:
    """WHERE for JOIN: only allow columns from allowed_table / JOIN の WHERE は左テーブルのみ許可"""
    if isinstance(node, exp.And):
        filters = []
        if node.expressions:
            filters = [_condition_to_filter_join(e, params_map, allowed_table, subqueries) for e in node.expressions]
        else:
            filters = [
                _condition_to_filter_join(node.this, params_map, allowed_table, subqueries),
                _condition_to_filter_join(node.expression, params_map, allowed_table, subqueries),
            ]
        return {"$and": filters}
    if isinstance(node, exp.Or):
        filters = []
        if node.expressions:
            filters = [_condition_to_filter_join(e, params_map, allowed_table, subqueries) for e in node.expressions]
        else:
            filters = [
                _condition_to_filter_join(node.this, params_map, allowed_table, subqueries),
                _condition_to_filter_join(node.expression, params_map, allowed_table, subqueries),
            ]
        return {"$or": filters}
    if isinstance(node, exp.Not):
        inner = node.this
        if isinstance(inner, exp.Exists):
            sub_expr = inner.this
            token = _register_subquery(sub_expr, params_map, subqueries, mode="exists")
            return {"$expr": {"$not": [{"$literal": token}]}}
        if isinstance(inner, exp.In):
            tbl, field = _column_table_field(inner.this)
            if tbl and tbl != allowed_table:
                raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_WHERE_RIGHT_TABLE")
            expr_val = inner.expression or inner.args.get("query") or inner.args.get("expressions")
            if isinstance(expr_val, list):
                values = [_literal_value(v, params_map, subqueries) for v in expr_val]
            else:
                values = _literal_value(expr_val, params_map, subqueries)
            return {field: {"$nin": values}}
        if isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null):
            tbl, field = _column_table_field(inner.this)
            if tbl and tbl != allowed_table:
                raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_WHERE_RIGHT_TABLE")
            return {field: {"$ne": None, "$exists": True}}
        if isinstance(inner, exp.Like):
            tbl, field = _column_table_field(inner.this)
            if tbl and tbl != allowed_table:
                raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_WHERE_RIGHT_TABLE")
            value = _literal_value(inner.expression, params_map, subqueries)
            regex = _like_to_regex(str(value))
            return {field: {"$not": {"$regex": regex}}}
        if hasattr(exp, "ILike") and isinstance(inner, getattr(exp, "ILike")):
            tbl, field = _column_table_field(inner.this)
            if tbl and tbl != allowed_table:
                raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_WHERE_RIGHT_TABLE")
            value = _literal_value(inner.expression, params_map, subqueries)
            regex = _like_to_regex(str(value))
            return {field: {"$not": {"$regex": regex, "$options": "i"}}}
        raise_error("[mdb][E2]", "Unsupported SQL construct: NOT")
    if isinstance(node, exp.Is) and isinstance(node.expression, exp.Null):
        tbl, field = _column_table_field(node.this)
        if tbl and tbl != allowed_table:
            raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_WHERE_RIGHT_TABLE")
        return {field: None}
    if isinstance(node, exp.In):
        tbl, field = _column_table_field(node.this)
        if tbl and tbl != allowed_table:
            raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_WHERE_RIGHT_TABLE")
        expr_val = node.expression or node.args.get("query") or node.args.get("expressions")
        if isinstance(expr_val, list):
            values = [_literal_value(v, params_map, subqueries) for v in expr_val]
        else:
            values = _literal_value(expr_val, params_map, subqueries)
        return {field: {"$in": values}}
    if isinstance(node, exp.Exists):
        sub_expr = node.this
        token = _register_subquery(sub_expr, params_map, subqueries, mode="exists")
        return {"$expr": {"$literal": token}}
    if isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        tbl, field = _column_table_field(node.left)
        if tbl and tbl != allowed_table:
            raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_WHERE_RIGHT_TABLE")
        if _is_subquery_node(node.right):
            value = _register_subquery(node.right, params_map, subqueries, mode="scalar")
        else:
            value = _literal_value(node.right, params_map, subqueries)
        if isinstance(node, exp.EQ):
            return {field: value}
        if isinstance(node, exp.NEQ):
            return {field: {"$ne": value}}
        if isinstance(node, exp.GT):
            return {field: {"$gt": value}}
        if isinstance(node, exp.GTE):
            return {field: {"$gte": value}}
        if isinstance(node, exp.LT):
            return {field: {"$lt": value}}
        if isinstance(node, exp.LTE):
            return {field: {"$lte": value}}
    if isinstance(node, exp.Paren):
        return _condition_to_filter_join(node.this, params_map, allowed_table, subqueries)
    if isinstance(node, exp.Between):
        tbl, field = _column_table_field(node.this)
        if tbl and tbl != allowed_table:
            raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_WHERE_RIGHT_TABLE")
        low = _literal_value(node.args["low"], params_map, subqueries)
        high = _literal_value(node.args["high"], params_map, subqueries)
        return {field: {"$gte": low, "$lte": high}}
    if isinstance(node, exp.Like):
        tbl, field = _column_table_field(node.this)
        if tbl and tbl != allowed_table:
            raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_WHERE_RIGHT_TABLE")
        value = _literal_value(node.expression, params_map, subqueries)
        regex = _like_to_regex(str(value))
        return {field: {"$regex": regex}}
    if hasattr(exp, "ILike") and isinstance(node, getattr(exp, "ILike")):
        tbl, field = _column_table_field(node.this)
        if tbl and tbl != allowed_table:
            raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_WHERE_RIGHT_TABLE")
        value = _literal_value(node.expression, params_map, subqueries)
        regex = _like_to_regex(str(value))
        return {field: {"$regex": regex, "$options": "i"}}
    raise_error("[mdb][E2]")


def _condition_to_filter_alias(
    node: exp.Expression, params_map: dict[str, Any], alias_map: dict[str, str], subqueries: dict[str, dict[str, Any]]
) -> Dict[str, Any]:
    """WHERE with alias prefixes / エイリアス付き WHERE を Mongo フィルタへ変換"""
    if isinstance(node, exp.And):
        parts = []
        if node.expressions:
            parts = [_condition_to_filter_alias(e, params_map, alias_map, subqueries) for e in node.expressions]
        else:
            parts = [
                _condition_to_filter_alias(node.this, params_map, alias_map, subqueries),
                _condition_to_filter_alias(node.expression, params_map, alias_map, subqueries),
            ]
        return {"$and": parts}
    if isinstance(node, exp.Or):
        parts = []
        if node.expressions:
            parts = [_condition_to_filter_alias(e, params_map, alias_map, subqueries) for e in node.expressions]
        else:
            parts = [
                _condition_to_filter_alias(node.this, params_map, alias_map, subqueries),
                _condition_to_filter_alias(node.expression, params_map, alias_map, subqueries),
            ]
        return {"$or": parts}
    if isinstance(node, exp.Not):
        inner = node.this
        if isinstance(inner, exp.Exists):
            sub_expr = inner.this
            token = _register_subquery(sub_expr, params_map, subqueries, mode="exists")
            return {"$expr": {"$not": [{"$literal": token}]}}
        if isinstance(inner, exp.In):
            field = _field_with_alias(inner.this, alias_map)
            expr_val = inner.expression or inner.args.get("query") or inner.args.get("expressions")
            if isinstance(expr_val, list):
                values = [_literal_value(v, params_map, subqueries) for v in expr_val]
            else:
                values = _literal_value(expr_val, params_map, subqueries)
            return {field: {"$nin": values}}
        if isinstance(inner, exp.Is) and isinstance(inner.expression, exp.Null):
            field = _field_with_alias(inner.this, alias_map)
            return {field: {"$ne": None, "$exists": True}}
        if isinstance(inner, exp.Like):
            field = _field_with_alias(inner.this, alias_map)
            value = _literal_value(inner.expression, params_map, subqueries)
            regex = _like_to_regex(str(value))
            return {field: {"$not": {"$regex": regex}}}
        if hasattr(exp, "ILike") and isinstance(inner, getattr(exp, "ILike")):
            field = _field_with_alias(inner.this, alias_map)
            value = _literal_value(inner.expression, params_map, subqueries)
            regex = _like_to_regex(str(value))
            return {field: {"$not": {"$regex": regex, "$options": "i"}}}
        if isinstance(inner, exp.EQ):
            field = _field_with_alias(inner.left, alias_map)
            value = _literal_value(inner.right, params_map, subqueries)
            return {field: {"$ne": value}}
        raise_error("[mdb][E2]", "Unsupported SQL construct: NOT")
    if isinstance(node, exp.Is) and isinstance(node.expression, exp.Null):
        field = _field_with_alias(node.this, alias_map)
        return {field: None}
    if isinstance(node, exp.Between):
        field = _field_with_alias(node.this, alias_map)
        low = _literal_value(node.args["low"], params_map, subqueries)
        high = _literal_value(node.args["high"], params_map, subqueries)
        return {field: {"$gte": low, "$lte": high}}
    if isinstance(node, exp.Like):
        field = _field_with_alias(node.this, alias_map)
        value = _literal_value(node.expression, params_map, subqueries)
        regex = _like_to_regex(str(value))
        return {field: {"$regex": regex}}
    if hasattr(exp, "ILike") and isinstance(node, getattr(exp, "ILike")):
        field = _field_with_alias(node.this, alias_map)
        value = _literal_value(node.expression, params_map, subqueries)
        regex = _like_to_regex(str(value))
        return {field: {"$regex": regex, "$options": "i"}}
    if isinstance(node, exp.In):
        field = _field_with_alias(node.this, alias_map)
        expr_val = node.expression or node.args.get("query") or node.args.get("expressions")
        if isinstance(expr_val, list):
            values = [_literal_value(v, params_map, subqueries) for v in expr_val]
        else:
            values = _literal_value(expr_val, params_map, subqueries)
        return {field: {"$in": values}}
    if isinstance(node, exp.Exists):
        sub_expr = node.this
        token = _register_subquery(sub_expr, params_map, subqueries, mode="exists")
        return {"$expr": {"$literal": token}}
    if isinstance(node, exp.Paren):
        return _condition_to_filter_alias(node.this, params_map, alias_map, subqueries)
    if isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        field = _field_with_alias(node.left, alias_map)
        if _is_subquery_node(node.right):
            value = _register_subquery(node.right, params_map, subqueries, mode="scalar")
        else:
            value = _literal_value(node.right, params_map, subqueries)
        if isinstance(node, exp.EQ):
            return {field: value}
        if isinstance(node, exp.NEQ):
            return {field: {"$ne": value}}
        if isinstance(node, exp.GT):
            return {field: {"$gt": value}}
        if isinstance(node, exp.GTE):
            return {field: {"$gte": value}}
        if isinstance(node, exp.LT):
            return {field: {"$lt": value}}
        if isinstance(node, exp.LTE):
            return {field: {"$lte": value}}
    raise_error("[mdb][E2]")


def _ensure_supported(expr: exp.Expression) -> None:
    """Reject unsupported constructs early / 非対応構文を早期に検出"""
    unsupported = (exp.Or, exp.Between, exp.Like, exp.Offset)
    for node in expr.walk():
        if isinstance(node, unsupported):
            keyword = node.key.upper() if hasattr(node, "key") else node.__class__.__name__
            raise_error("[mdb][E2]", f"Unsupported SQL construct: {keyword}")


def _inline_cte_tables(node: exp.Expression, cte_map: dict[str, exp.Select]) -> exp.Expression:
    if not cte_map:
        return node

    def _replace_table(n: exp.Expression) -> exp.Expression:
        if isinstance(n, exp.Table) and n.name in cte_map:
            alias_name = n.alias_or_name or n.name
            return exp.Subquery(
                this=cte_map[n.name].copy(),
                alias=exp.TableAlias(this=exp.to_identifier(alias_name)),
            )
        return n

    return node.transform(_replace_table)


def _expand_with_clause(expr: exp.Expression) -> exp.Expression:
    with_expr = expr.args.get("with_")
    if not with_expr:
        return expr
    if with_expr.args.get("recursive"):
        raise_error("[mdb][E2]", "Unsupported SQL construct: CTE_RECURSIVE")
    ctes = with_expr.expressions or []
    cte_names = [cte.alias_or_name for cte in ctes]
    if any(not name for name in cte_names):
        raise_error("[mdb][E2]", "Unsupported SQL construct: CTE")
    resolved: dict[str, exp.Select] = {}
    for idx, cte in enumerate(ctes):
        cte_name = cte_names[idx]
        cte_body = cte.this
        inner = getattr(cte_body, "this", None)
        if not isinstance(cte_body, exp.Select) and isinstance(inner, exp.Select):
            cte_body = inner
        if not isinstance(cte_body, exp.Select):
            raise_error("[mdb][E2]", "Unsupported SQL construct: CTE")
        expanded_body = _inline_cte_tables(cte_body.copy(), resolved)
        unresolved_names = set(cte_names[idx:])
        for tbl in expanded_body.find_all(exp.Table):
            if tbl.name in unresolved_names:
                raise_error("[mdb][E2]", "Unsupported SQL construct: CTE_RECURSIVE")
        resolved[cte_name] = expanded_body
    root = expr.copy()
    root.set("with_", None)
    return _inline_cte_tables(root, resolved)


def _flatten_union_expression(union_expr: exp.Union) -> tuple[list[exp.Select], bool]:
    nodes: list[exp.Select] = []
    union_modes: list[bool] = []

    def _walk(node: exp.Expression) -> None:
        if isinstance(node, exp.Union):
            union_modes.append(bool(node.args.get("distinct")))
            _walk(node.left)
            _walk(node.right)
            return
        if isinstance(node, exp.Select):
            nodes.append(node)
            return
        raise_error("[mdb][E2]", "Unsupported SQL construct: UNION")

    _walk(union_expr)
    if not union_modes:
        raise_error("[mdb][E2]", "Unsupported SQL construct: UNION")
    first_mode = union_modes[0]
    if any(mode != first_mode for mode in union_modes):
        raise_error("[mdb][E2]", "Unsupported SQL construct: UNION_MIXED")
    return nodes, first_mode


def parse_sql(sql: str, params: Sequence[Any] | Mapping[str, Any] | None = None) -> QueryParts:
    """Parse SQL to QueryParts / SQL を QueryParts に変換"""
    normalized_sql, param_values, tokens = preprocess_sql(sql, params)
    params_map = {tokens[i]: val for i, val in enumerate(param_values)}
    subqueries: dict[str, dict[str, Any]] = {}
    # Handle CREATE/DROP INDEX via simple parser
    ci = _parse_create_index_sql(normalized_sql)
    if ci:
        return ci
    di = _parse_drop_index_sql(normalized_sql)
    if di:
        return di
    try:
        expr = parse_one(normalized_sql)
    except Exception as exc:
        raise_error("[mdb][E5]", cause=exc)
    expr = _expand_with_clause(expr)

    if isinstance(expr, exp.Union):
        select_nodes, is_distinct_union = _flatten_union_expression(expr)
        union_parts = [_parse_select_like(node, params_map, subqueries) for node in select_nodes]
        order = None
        limit = None
        skip = None
        if expr.args.get("order"):
            order = []
            for e in expr.args["order"].expressions:
                field = _field_name(e.this, params_map)
                direction = -1 if e.args.get("desc") else 1
                order.append((field, direction))
        if expr.args.get("limit"):
            limit = int(_literal_value(expr.args["limit"].expression, params_map, subqueries))
        if expr.args.get("offset"):
            skip = int(_literal_value(expr.args["offset"].expression, params_map, subqueries))
        parts = QueryParts(
            operation="union" if is_distinct_union else "union_all",
            collection="",
            union_parts=union_parts,
            sort=order,
            limit=limit,
            skip=skip,
        )
        parts.subqueries = subqueries or None
        return parts

    if isinstance(expr, exp.Select):
        # window function detection
        if expr.find(exp.Window) or expr.find(exp.RowNumber) or expr.find(exp.Rank) or expr.find(exp.DenseRank):
            return _parse_window_select(expr, params_map, subqueries)
        if expr.args.get("joins"):
            parts = _parse_join_select(expr, params_map, subqueries)
        elif expr.args.get("group"):
            parts = _parse_group_select(expr, params_map, subqueries)
        else:
            parts = _parse_select(expr, params_map, subqueries)
        parts.subqueries = subqueries or None
        return parts
    if isinstance(expr, exp.Insert):
        parts = _parse_insert(expr, params_map, subqueries)
        parts.subqueries = subqueries or None
        return parts
    if isinstance(expr, exp.Update):
        parts = _parse_update(expr, params_map, subqueries)
        parts.subqueries = subqueries or None
        return parts
    if isinstance(expr, exp.Delete):
        parts = _parse_delete(expr, params_map, subqueries)
        parts.subqueries = subqueries or None
        return parts
    if isinstance(expr, exp.Create):
        return _parse_create(expr)
    if isinstance(expr, exp.Drop):
        return _parse_drop(expr)
    raise_error("[mdb][E2]", "Unsupported SQL construct: STATEMENT")


def _parse_select(expr: exp.Select, params_map: dict[str, Any], subqueries: dict[str, dict[str, Any]]) -> QueryParts:
    from_expr = expr.args.get("from_")
    collection = None
    base_alias = None
    inline_token = None
    aggregates: list[tuple[str, str, str | None]] = []
    if from_expr:
        if hasattr(from_expr, "this") and isinstance(from_expr.this, exp.Table) and from_expr.this.name:
            collection = from_expr.this.name
            base_alias = from_expr.this.alias_or_name or collection
        elif hasattr(from_expr, "this") and isinstance(from_expr.this, (exp.Subquery, exp.Select)):
            inline_token = _register_subquery(from_expr.this, params_map, subqueries, mode="from")
        else:
            raise_error("[mdb][E5]", "Failed to parse SQL")
    if not collection and not inline_token:
        table = expr.find(exp.Table)
        if table and table.name:
            collection = table.name
            base_alias = table.alias_or_name or collection
        else:
            raise_error("[mdb][E5]", "Failed to parse SQL")
    projection: List[str] | None = None
    projection_paths: list[tuple[str, str]] | None = None
    if not expr.is_star:
        projection_paths = []
        for item in expr.expressions:
            target = item.this if isinstance(item, exp.Alias) else item
            alias = item.alias_or_name
            if isinstance(target, exp.Column):
                projection_paths.append((_field_name(target, params_map), alias))
            else:
                projection_paths.append((alias, alias))
        aggregates = []
        for item in expr.expressions:
            target = item.this if isinstance(item, exp.Alias) else item
            alias = item.alias_or_name
            if isinstance(target, exp.Count):
                if isinstance(target.this, exp.Distinct):
                    distinct_exprs = target.this.expressions or []
                    if len(distinct_exprs) != 1 or not isinstance(distinct_exprs[0], exp.Column):
                        raise_error("[mdb][E2]", "Unsupported SQL construct: COUNT_DISTINCT")
                    aggregates.append((alias, "count_distinct", _field_name(distinct_exprs[0], params_map)))
                else:
                    aggregates.append((alias, "count", None))
            elif isinstance(target, exp.Sum):
                aggregates.append((alias, "sum", _field_name(target.this, params_map)))
            elif isinstance(target, exp.Avg):
                aggregates.append((alias, "avg", _field_name(target.this, params_map)))
            elif isinstance(target, exp.Min):
                aggregates.append((alias, "min", _field_name(target.this, params_map)))
            elif isinstance(target, exp.Max):
                aggregates.append((alias, "max", _field_name(target.this, params_map)))

    def _flatten_and_terms(n: exp.Expression) -> list[exp.Expression]:
        if isinstance(n, exp.And):
            if n.expressions:
                out: list[exp.Expression] = []
                for e in n.expressions:
                    out.extend(_flatten_and_terms(e))
                return out
            return _flatten_and_terms(n.this) + _flatten_and_terms(n.expression)
        return [n]

    def _combine_with_and(items: list[exp.Expression]) -> exp.Expression:
        if not items:
            raise_error("[mdb][E5]", "Failed to parse SQL")
        node = items[0]
        for nxt in items[1:]:
            node = exp.And(this=node, expression=nxt)
        return node

    def _build_correlated_exists_lookup(subquery_expr: exp.Expression, idx: int) -> tuple[dict[str, Any], dict[str, Any], str] | None:
        sub_expr = subquery_expr.this if isinstance(subquery_expr, exp.Subquery) else subquery_expr
        inner_select = getattr(sub_expr, "this", None)
        if not isinstance(sub_expr, exp.Select) and isinstance(inner_select, exp.Select):
            sub_expr = inner_select
        if not isinstance(sub_expr, exp.Select):
            raise_error("[mdb][E2]", "Unsupported SQL construct: CORRELATED_SUBQUERY")
        if (
            sub_expr.args.get("joins")
            or sub_expr.args.get("group")
            or sub_expr.args.get("having")
            or sub_expr.args.get("distinct")
            or sub_expr.args.get("offset")
            or sub_expr.args.get("order")
        ):
            raise_error("[mdb][E2]", "Unsupported SQL construct: CORRELATED_SUBQUERY")
        sub_from = sub_expr.args.get("from_")
        if not sub_from or not hasattr(sub_from, "this") or not isinstance(sub_from.this, exp.Table) or not sub_from.this.name:
            raise_error("[mdb][E2]", "Unsupported SQL construct: CORRELATED_SUBQUERY")
        sub_table = sub_from.this.name
        sub_alias = sub_from.this.alias_or_name or sub_table
        defined_tables = {sub_table, sub_alias}
        if not sub_expr.args.get("where"):
            return None

        outer_alias_map = {base_alias or collection or "": "", collection or "": ""}
        let_by_path: dict[str, str] = {}
        let_vars: dict[str, Any] = {}
        has_correlation = False

        def _operand(node: exp.Expression) -> Any:
            nonlocal has_correlation
            if isinstance(node, exp.Column):
                if node.table in defined_tables:
                    return f"${node.name}"
                if node.table in outer_alias_map:
                    outer_path = _field_with_alias(node, outer_alias_map)
                    if outer_path not in let_by_path:
                        var_name = f"v{len(let_by_path)}"
                        let_by_path[outer_path] = var_name
                        let_vars[var_name] = f"${outer_path}"
                    has_correlation = True
                    return f"$${let_by_path[outer_path]}"
                if not node.table:
                    return f"${node.name}"
                raise_error("[mdb][E2]", "Unsupported SQL construct: CORRELATED_SUBQUERY")
            return _literal_value(node, params_map, subqueries)

        def _to_expr(node: exp.Expression) -> dict[str, Any]:
            if isinstance(node, exp.Paren):
                return _to_expr(node.this)
            if isinstance(node, exp.And):
                parts = node.expressions or [node.this, node.expression]
                return {"$and": [_to_expr(p) for p in parts]}
            if isinstance(node, exp.Or):
                parts = node.expressions or [node.this, node.expression]
                return {"$or": [_to_expr(p) for p in parts]}
            if isinstance(node, exp.Not) and isinstance(node.this, exp.Is) and isinstance(node.this.expression, exp.Null):
                return {"$ne": [_operand(node.this.this), None]}
            if isinstance(node, exp.Is) and isinstance(node.expression, exp.Null):
                return {"$eq": [_operand(node.this), None]}
            if isinstance(node, exp.EQ):
                return {"$eq": [_operand(node.left), _operand(node.right)]}
            if isinstance(node, exp.NEQ):
                return {"$ne": [_operand(node.left), _operand(node.right)]}
            if isinstance(node, exp.GT):
                return {"$gt": [_operand(node.left), _operand(node.right)]}
            if isinstance(node, exp.GTE):
                return {"$gte": [_operand(node.left), _operand(node.right)]}
            if isinstance(node, exp.LT):
                return {"$lt": [_operand(node.left), _operand(node.right)]}
            if isinstance(node, exp.LTE):
                return {"$lte": [_operand(node.left), _operand(node.right)]}
            raise_error("[mdb][E2]", "Unsupported SQL construct: CORRELATED_SUBQUERY")

        expr_doc = _to_expr(sub_expr.args["where"].this)
        if not has_correlation:
            return None
        pipeline: list[dict[str, Any]] = [{"$match": {"$expr": expr_doc}}]
        if sub_expr.args.get("limit"):
            limit_val = int(_literal_value(sub_expr.args["limit"].expression, params_map, subqueries))
            pipeline.append({"$limit": limit_val})
        corr_alias = f"__corr{idx}"
        lookup = {
            "$lookup": {
                "from": sub_table,
                "let": let_vars,
                "pipeline": pipeline,
                "as": corr_alias,
            }
        }
        return lookup, {"$match": {corr_alias: {"$ne": []}}}, corr_alias

    def _try_correlated_exists_pipeline() -> QueryParts | None:
        if inline_token or not collection or not expr.args.get("where"):
            return None
        if expr.args.get("group") or expr.args.get("distinct"):
            return None
        where_expr = expr.args["where"].this
        terms = _flatten_and_terms(where_expr)
        regular_terms: list[exp.Expression] = []
        lookup_stages: list[dict[str, Any]] = []
        match_stages: list[dict[str, Any]] = []
        corr_aliases: list[str] = []
        corr_idx = 0
        has_correlated = False

        for term in terms:
            is_not = False
            exists_expr: exp.Expression | None = None
            if isinstance(term, exp.Exists):
                exists_expr = term.this
            elif isinstance(term, exp.Not) and isinstance(term.this, exp.Exists):
                is_not = True
                exists_expr = term.this.this
            if exists_expr is None:
                regular_terms.append(term)
                continue
            built = _build_correlated_exists_lookup(exists_expr, corr_idx)
            if built is None:
                regular_terms.append(term)
                continue
            has_correlated = True
            lookup_stage, match_stage, corr_alias = built
            if is_not:
                match_stage = {"$match": {corr_alias: {"$eq": []}}}
            lookup_stages.append(lookup_stage)
            match_stages.append(match_stage)
            corr_aliases.append(corr_alias)
            corr_idx += 1

        if not has_correlated:
            return None

        pipeline: list[dict[str, Any]] = []
        if regular_terms:
            alias_map = {base_alias or collection: "", collection: ""}
            where_filter = _condition_to_filter_alias(_combine_with_and(regular_terms), params_map, alias_map, subqueries)
            pipeline.append({"$match": where_filter})
        for lp, mp in zip(lookup_stages, match_stages):
            pipeline.append(lp)
            pipeline.append(mp)

        if not expr.is_star and projection_paths:
            project_doc: dict[str, Any] = {"_id": 0}
            for path, out in projection_paths:
                project_doc[out] = f"${path}"
            pipeline.append({"$project": project_doc})
            out_projection = [(out, out) for _path, out in projection_paths]
        else:
            if corr_aliases:
                pipeline.append({"$project": {alias: 0 for alias in corr_aliases}})
            out_projection = None

        if expr.args.get("order"):
            sort_doc: dict[str, int] = {}
            for e in expr.args["order"].expressions:
                field = _field_name(e.this, params_map)
                direction = -1 if e.args.get("desc") else 1
                sort_doc[field] = direction
            if sort_doc:
                pipeline.append({"$sort": sort_doc})
        if expr.args.get("offset"):
            skip_val = int(_literal_value(expr.args["offset"].expression, params_map, subqueries))
            pipeline.append({"$skip": skip_val})
        if expr.args.get("limit"):
            limit_val = int(_literal_value(expr.args["limit"].expression, params_map, subqueries))
            pipeline.append({"$limit": limit_val})

        return QueryParts(
            operation="aggregate",
            collection=collection,
            pipeline=pipeline,
            projection_paths=out_projection,
        )

    correlated_parts = _try_correlated_exists_pipeline()
    if correlated_parts is not None:
        return correlated_parts

    mongo_filter = None
    if expr.args.get("where"):
        mongo_filter = _condition_to_filter(expr.args["where"].this, params_map, subqueries)

    if expr.args.get("distinct"):
        if expr.is_star:
            raise_error("[mdb][E2]", "Unsupported SQL construct: DISTINCT_STAR")
        distinct_items: list[tuple[str, str]] = []
        for item in expr.expressions:
            target = item.this if isinstance(item, exp.Alias) else item
            if not isinstance(target, exp.Column):
                raise_error("[mdb][E2]", "Unsupported SQL construct: DISTINCT")
            out_name = item.alias_or_name or target.name
            field = _field_name(target, params_map)
            distinct_items.append((field, out_name))
        if len({out for _field, out in distinct_items}) != len(distinct_items):
            raise_error("[mdb][E2]", "Unsupported SQL construct: DISTINCT_DUPLICATE_ALIAS")
        pipeline: list[dict[str, Any]] = []
        if mongo_filter:
            pipeline.append({"$match": mongo_filter})
        if len(distinct_items) == 1:
            field, out_name = distinct_items[0]
            pipeline.append({"$group": {"_id": f"${field}"}})
            pipeline.append({"$project": {out_name: "$_id", "_id": 0}})
        else:
            group_id = {out: f"${field}" for field, out in distinct_items}
            pipeline.append({"$group": {"_id": group_id}})
            project_doc: dict[str, Any] = {"_id": 0}
            for _field, out in distinct_items:
                project_doc[out] = f"$_id.{out}"
            pipeline.append({"$project": project_doc})
        if expr.args.get("order"):
            sort_doc: dict[str, int] = {}
            field_to_out = {field: out for field, out in distinct_items}
            out_names = {out for _field, out in distinct_items}
            for e in expr.args["order"].expressions:
                direction = -1 if e.args.get("desc") else 1
                if isinstance(e.this, exp.Column):
                    ord_field = _field_name(e.this, params_map)
                elif isinstance(e.this, exp.Identifier):
                    ord_field = e.this.name
                else:
                    raise_error("[mdb][E2]", "Unsupported SQL construct: DISTINCT_ORDER_BY")
                if ord_field in out_names:
                    sort_key = ord_field
                elif ord_field in field_to_out:
                    sort_key = field_to_out[ord_field]
                else:
                    raise_error("[mdb][E2]", "Unsupported SQL construct: DISTINCT_ORDER_BY")
                sort_doc[sort_key] = direction
            if sort_doc:
                pipeline.append({"$sort": sort_doc})
        if expr.args.get("offset"):
            skip_val = int(_literal_value(expr.args["offset"].expression, params_map, subqueries))
            pipeline.append({"$skip": skip_val})
        if expr.args.get("limit"):
            limit_val = int(_literal_value(expr.args["limit"].expression, params_map, subqueries))
            pipeline.append({"$limit": limit_val})
        return QueryParts(
            operation="aggregate",
            collection=collection or "",
            pipeline=pipeline,
            projection_paths=[(out, out) for _field, out in distinct_items],
        )

    sort_items = None
    if expr.args.get("order"):
        sort_items = []
        for e in expr.args["order"].expressions:
            field = _field_name(e.this, params_map)
            direction = -1 if e.args.get("desc") else 1
            sort_items.append((field, direction))

    limit_val = None
    if expr.args.get("limit"):
        limit_val = int(_literal_value(expr.args["limit"].expression, params_map, subqueries))
    skip_val = None
    if expr.args.get("offset"):
        skip_val = int(_literal_value(expr.args["offset"].expression, params_map, subqueries))

    if aggregates:
        if inline_token:
            return QueryParts(
                operation="from_subquery",
                collection=collection or "",
                filter=mongo_filter or {},
                projection=[alias for alias, _, _ in aggregates],
                sort=sort_items,
                limit=limit_val,
                skip=skip_val,
                inline_token=inline_token,
                inline_aggregates=aggregates,
                projection_paths=[(alias, alias) for alias, _, _ in aggregates],
            )
        pipeline: list[dict[str, Any]] = []
        if mongo_filter:
            pipeline.append({"$match": mongo_filter})
        group_doc: dict[str, Any] = {"_id": None}
        count_distinct_aliases: set[str] = set()
        for alias, op, field in aggregates:
            if op == "count":
                group_doc[alias] = {"$sum": 1}
            elif op == "count_distinct":
                group_doc[alias] = {"$addToSet": f"${field}"}
                count_distinct_aliases.add(alias)
            elif op == "sum":
                group_doc[alias] = {"$sum": f"${field}"}
            elif op == "avg":
                group_doc[alias] = {"$avg": f"${field}"}
            elif op == "min":
                group_doc[alias] = {"$min": f"${field}"}
            elif op == "max":
                group_doc[alias] = {"$max": f"${field}"}
        pipeline.append({"$group": group_doc})
        if count_distinct_aliases:
            project_doc: dict[str, Any] = {"_id": 0}
            for alias, _op, _field in aggregates:
                if alias in count_distinct_aliases:
                    project_doc[alias] = {"$size": f"${alias}"}
                else:
                    project_doc[alias] = f"${alias}"
            pipeline.append({"$project": project_doc})
        return QueryParts(
            operation="aggregate",
            collection=collection or "",
            pipeline=pipeline,
            projection_paths=[(alias, alias) for alias, _, _ in aggregates],
        )

    return QueryParts(
        operation="from_subquery" if inline_token else "find",
        collection=collection or "",
        filter=mongo_filter or {},
        projection=projection,
        projection_paths=projection_paths,
        sort=sort_items,
        limit=limit_val,
        skip=skip_val,
        inline_token=inline_token,
    )


def _parse_select_like(expr: exp.Select, params_map: dict[str, Any], subqueries: dict[str, dict[str, Any]]) -> QueryParts:
    if expr.args.get("with_"):
        expanded = _expand_with_clause(expr)
        if not isinstance(expanded, exp.Select):
            raise_error("[mdb][E2]", "Unsupported SQL construct: CTE")
        expr = expanded
    if expr.args.get("joins"):
        return _parse_join_select(expr, params_map, subqueries)
    if expr.args.get("group"):
        return _parse_group_select(expr, params_map, subqueries)
    return _parse_select(expr, params_map, subqueries)


def _parse_window_select(expr: exp.Select, params_map: dict[str, Any], subqueries: dict[str, dict[str, Any]]) -> QueryParts:
    from_expr = expr.args.get("from_")
    if not from_expr or not hasattr(from_expr, "this") or not from_expr.this.name:
        raise_error("[mdb][E5]", "Failed to parse SQL")
    if expr.args.get("joins"):
        raise_error("[mdb][E2]", "Unsupported SQL construct: WINDOW_FUNCTION")
    collection = from_expr.this.name
    if expr.args.get("group"):
        raise_error("[mdb][E2]", "Unsupported SQL construct: WINDOW_FUNCTION")
    where_filter = None
    if expr.args.get("where"):
        where_filter = _condition_to_filter(expr.args["where"].this, params_map, subqueries)
    window_expr = None
    output_alias = None
    window_func = None
    base_columns: list[tuple[str, str]] = []
    for item in expr.expressions:
        target = item.this if isinstance(item, exp.Alias) else item
        alias = item.alias_or_name
        if isinstance(target, exp.Window) and isinstance(target.this, (exp.RowNumber, exp.Rank, exp.DenseRank)):
            window_expr = target
            output_alias = alias
            if isinstance(target.this, exp.RowNumber):
                window_func = "$documentNumber"
            elif isinstance(target.this, exp.Rank):
                window_func = "$rank"
            elif isinstance(target.this, exp.DenseRank):
                window_func = "$denseRank"
        elif isinstance(target, exp.Column):
            base_columns.append((_field_name(target, params_map), alias))
        else:
            raise_error("[mdb][E2]", "Unsupported SQL construct: WINDOW_FUNCTION")
    if not window_expr or not output_alias:
        raise_error("[mdb][E2]", "Unsupported SQL construct: WINDOW_FUNCTION")
    partition = window_expr.args.get("partition_by")
    order = window_expr.args.get("order")
    if partition and isinstance(partition, list) and len(partition) > 1:
        raise_error("[mdb][E2]", "Unsupported SQL construct: WINDOW_FUNCTION")
    partition_expr = None
    if partition:
        target = partition[0] if isinstance(partition, list) else partition.expressions[0]
        partition_expr = f"${_field_name(target, params_map)}"
    sort_doc: dict[str, int] = {}
    if order and order.expressions:
        for e in order.expressions:
            fld = _field_name(e.this, params_map)
            direction = -1 if e.args.get("desc") else 1
            sort_doc[fld] = direction
    window_output = {output_alias: {window_func: {}}}
    window_doc: dict[str, Any] = {"output": window_output}
    if partition_expr:
        window_doc["partitionBy"] = partition_expr
    if sort_doc:
        window_doc["sortBy"] = sort_doc
    pipeline: list[dict[str, Any]] = []
    if where_filter:
        pipeline.append({"$match": where_filter})
    pipeline.append({"$setWindowFields": window_doc})
    project_doc: dict[str, Any] = {}
    for path, alias in base_columns:
        project_doc[alias] = f"${path}"
    project_doc[output_alias] = f"${output_alias}"
    if project_doc:
        pipeline.append({"$project": project_doc})
    projection_paths = [(alias, alias) for _, alias in base_columns]
    projection_paths.append((output_alias, output_alias))
    return QueryParts(
        operation="aggregate",
        collection=collection,
        pipeline=pipeline,
        projection_paths=projection_paths,
        uses_window=True,
    )


def _parse_join_select(expr: exp.Select, params_map: dict[str, Any], subqueries: dict[str, dict[str, Any]]) -> QueryParts:
    def _parse_order_limit_offset(select_expr: exp.Select) -> tuple[list[tuple[str, int]] | None, int | None, int | None]:
        order = None
        limit = None
        skip = None
        if select_expr.args.get("order"):
            order = []
            for e in select_expr.args["order"].expressions:
                field = _field_name(e.this, params_map)
                direction = -1 if e.args.get("desc") else 1
                order.append((field, direction))
        if select_expr.args.get("limit"):
            limit = int(_literal_value(select_expr.args["limit"].expression, params_map, subqueries))
        if select_expr.args.get("offset"):
            skip = int(_literal_value(select_expr.args["offset"].expression, params_map, subqueries))
        return order, limit, skip

    def _parse_full_outer_single_eq(select_expr: exp.Select) -> QueryParts | None:
        from_expr_local = select_expr.args.get("from_")
        joins_local = select_expr.args.get("joins") or []
        if len(joins_local) != 1:
            if any(((j.args.get("side") or "").upper() == "FULL") for j in joins_local):
                raise_error("[mdb][E2]", "Unsupported SQL construct: FULL_JOIN_CHAIN")
            return None
        join_local = joins_local[0]
        join_side = (join_local.args.get("side") or "").upper()
        join_kind = (join_local.kind or "").upper()
        if join_side != "FULL":
            return None
        if join_kind not in ("", "OUTER"):
            raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN")
        if not from_expr_local or not isinstance(from_expr_local.this, exp.Table):
            raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN")
        if not isinstance(join_local.this, exp.Table):
            raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_TABLE")
        on_local = join_local.args.get("on")
        if not on_local:
            raise_error("[mdb][E2]", "Unsupported SQL construct: FULL_JOIN")

        def _flatten_on_eq(n: exp.Expression) -> list[exp.Expression]:
            if isinstance(n, exp.Paren):
                return _flatten_on_eq(n.this)
            if isinstance(n, exp.And):
                if n.expressions:
                    out: list[exp.Expression] = []
                    for e in n.expressions:
                        out.extend(_flatten_on_eq(e))
                    return out
                return _flatten_on_eq(n.this) + _flatten_on_eq(n.expression)
            return [n]

        on_terms = _flatten_on_eq(on_local)
        if not on_terms or any(not isinstance(term, exp.EQ) for term in on_terms):
            raise_error("[mdb][E2]", "Unsupported SQL construct: FULL_JOIN_NON_EQ")
        left_tbl_name = from_expr_local.this.name
        left_alias_name = from_expr_local.this.alias_or_name or left_tbl_name
        right_tbl_name = join_local.this.name
        right_alias_name = join_local.this.alias_or_name or right_tbl_name

        def _column_side(col: exp.Expression) -> tuple[str, str] | None:
            tbl, fld = _column_table_field(col)
            if tbl in (left_tbl_name, left_alias_name):
                return "left", fld
            if tbl in (right_tbl_name, right_alias_name):
                return "right", fld
            return None

        left_keys: list[str] = []
        for term in on_terms:
            left_tbl, left_field = _column_table_field(term.left)
            right_tbl, right_field = _column_table_field(term.right)
            if left_tbl in (left_tbl_name, left_alias_name) and right_tbl in (right_tbl_name, right_alias_name):
                left_keys.append(left_field)
            elif right_tbl in (left_tbl_name, left_alias_name) and left_tbl in (right_tbl_name, right_alias_name):
                left_keys.append(right_field)
            else:
                raise_error("[mdb][E2]", "Unsupported SQL construct: FULL_JOIN")
        left_keys = list(dict.fromkeys(left_keys))

        order, limit, skip = _parse_order_limit_offset(select_expr)
        null_terms = [exp.Is(this=exp.column(left_key, table=left_alias_name), expression=exp.Null()) for left_key in left_keys]
        right_only_where = null_terms[0]
        for term in null_terms[1:]:
            right_only_where = exp.And(this=right_only_where, expression=term)

        def _parse_full_outer_group_count() -> QueryParts | None:
            if select_expr.args.get("where"):
                return None
            group_expr = select_expr.args.get("group")
            if not group_expr or len(group_expr.expressions) != 1:
                return None
            group_item = group_expr.expressions[0]
            if not isinstance(group_item, exp.Coalesce):
                return None
            coalesce_terms: list[exp.Expression] = [group_item.this, *(group_item.expressions or [])]
            if len(coalesce_terms) != 2 or any(not isinstance(term, exp.Column) for term in coalesce_terms):
                return None
            left_group_field = None
            right_group_field = None
            for term in coalesce_terms:
                side_field = _column_side(term)
                if side_field is None:
                    return None
                side, fld = side_field
                if side == "left":
                    if left_group_field is not None:
                        return None
                    left_group_field = fld
                else:
                    if right_group_field is not None:
                        return None
                    right_group_field = fld
            if left_group_field is None or right_group_field is None:
                return None

            group_alias = None
            count_alias = None
            projection_order: list[str] = []
            for item in select_expr.expressions:
                target = item.this if isinstance(item, exp.Alias) else item
                alias = item.alias_or_name
                if isinstance(target, exp.Coalesce):
                    if target.sql() != group_item.sql():
                        return None
                    if not alias:
                        return None
                    group_alias = alias
                    projection_order.append(alias)
                elif isinstance(target, exp.Count):
                    if count_alias is not None:
                        return None
                    count_target = target.this
                    if not isinstance(count_target, exp.Star):
                        return None
                    count_alias = alias or "cnt"
                    projection_order.append(count_alias)
                else:
                    return None
            if group_alias is None or count_alias is None:
                return None

            left_select = select_expr.copy()
            left_select.set("order", None)
            left_select.set("limit", None)
            left_select.set("offset", None)
            left_select.set("group", None)
            left_select.set("having", None)
            left_select.set("where", None)
            left_select.set(
                "expressions",
                [
                    exp.alias_(exp.column(left_group_field, table=left_alias_name), "__full_left_key"),
                    exp.alias_(exp.column(right_group_field, table=right_alias_name), "__full_right_key"),
                ],
            )
            left_select.set("joins", [exp.Join(this=join_local.this.copy(), on=on_local.copy(), side="LEFT")])

            right_select = select_expr.copy()
            right_select.set("order", None)
            right_select.set("limit", None)
            right_select.set("offset", None)
            right_select.set("group", None)
            right_select.set("having", None)
            right_select.set("from_", exp.From(this=join_local.this.copy()))
            right_select.set(
                "joins",
                [
                    exp.Join(
                        this=from_expr_local.this.copy(),
                        on=on_local.copy(),
                        side="LEFT",
                    )
                ],
            )
            right_select.set(
                "expressions",
                [
                    exp.alias_(exp.column(left_group_field, table=left_alias_name), "__full_left_key"),
                    exp.alias_(exp.column(right_group_field, table=right_alias_name), "__full_right_key"),
                ],
            )
            right_select.set("where", exp.Where(this=right_only_where.copy()))

            left_parts = _parse_join_select(left_select, params_map, subqueries)
            right_parts = _parse_join_select(right_select, params_map, subqueries)
            if left_parts.operation != "aggregate" or right_parts.operation != "aggregate":
                return None

            left_projection = left_parts.projection_paths or []
            right_projection = right_parts.projection_paths or []
            if not left_projection or not right_projection:
                return None

            left_pipeline = list(left_parts.pipeline or [])
            left_pipeline.append({"$project": {"_id": 0, **{out: f"${path}" for path, out in left_projection}}})
            right_pipeline = list(right_parts.pipeline or [])
            right_pipeline.append({"$project": {"_id": 0, **{out: f"${path}" for path, out in right_projection}}})

            pipeline = left_pipeline
            pipeline.append({"$unionWith": {"coll": right_parts.collection, "pipeline": right_pipeline}})
            pipeline.append(
                {
                    "$group": {
                        "_id": {"$ifNull": ["$__full_left_key", "$__full_right_key"]},
                        count_alias: {"$sum": 1},
                    }
                }
            )
            pipeline.append(
                {
                    "$project": {
                        "_id": 0,
                        group_alias: "$_id",
                        count_alias: f"${count_alias}",
                    }
                }
            )
            if select_expr.args.get("having"):
                having_alias_map = {group_alias: "", count_alias: ""}
                having_filter = _condition_to_filter_alias(
                    select_expr.args["having"].this,
                    params_map,
                    having_alias_map,
                    subqueries,
                )
                pipeline.append({"$match": having_filter})
            if order:
                sort_doc: dict[str, int] = {}
                for field, direction in order:
                    sort_doc[field] = direction
                if sort_doc:
                    pipeline.append({"$sort": sort_doc})
            if skip is not None:
                pipeline.append({"$skip": skip})
            if limit is not None:
                pipeline.append({"$limit": limit})

            return QueryParts(
                operation="aggregate",
                collection=left_parts.collection,
                pipeline=pipeline,
                projection_paths=[(name, name) for name in projection_order],
            )

        if select_expr.args.get("group") or select_expr.args.get("having"):
            group_parts = _parse_full_outer_group_count()
            if group_parts is not None:
                return group_parts
            raise_error("[mdb][E2]", "Unsupported SQL construct: FULL_JOIN_GROUP")
        if select_expr.args.get("where"):
            raise_error("[mdb][E2]", "Unsupported SQL construct: FULL_JOIN")

        left_select = select_expr.copy()
        left_select.set("order", None)
        left_select.set("limit", None)
        left_select.set("offset", None)
        left_select.set("joins", [exp.Join(this=join_local.this.copy(), on=on_local.copy(), side="LEFT")])

        right_select = select_expr.copy()
        right_select.set("order", None)
        right_select.set("limit", None)
        right_select.set("offset", None)
        right_select.set("from_", exp.From(this=join_local.this.copy()))
        right_select.set(
            "joins",
            [
                exp.Join(
                    this=from_expr_local.this.copy(),
                    on=on_local.copy(),
                    side="LEFT",
                )
            ],
        )
        right_select.set("where", exp.Where(this=right_only_where))

        left_parts = _parse_join_select(left_select, params_map, subqueries)
        right_parts = _parse_join_select(right_select, params_map, subqueries)
        return QueryParts(
            operation="union_all",
            collection="",
            union_parts=[left_parts, right_parts],
            sort=order,
            limit=limit,
            skip=skip,
        )

    def _normalize_single_right_join(select_expr: exp.Select) -> exp.Select:
        from_expr_local = select_expr.args.get("from_")
        joins_local = select_expr.args.get("joins") or []
        if len(joins_local) != 1:
            if any(((j.args.get("side") or "").upper() == "RIGHT") for j in joins_local):
                raise_error("[mdb][E2]", "Unsupported SQL construct: RIGHT_JOIN_CHAIN")
            return select_expr
        join_local = joins_local[0]
        side = (join_local.args.get("side") or "").upper()
        if side != "RIGHT":
            return select_expr
        if not from_expr_local or not isinstance(from_expr_local.this, exp.Table):
            raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN")
        if not isinstance(join_local.this, exp.Table):
            raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_TABLE")
        on_local = join_local.args.get("on")
        if not on_local:
            raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_ON")

        def _flatten_on_eq(n: exp.Expression) -> list[exp.Expression]:
            if isinstance(n, exp.Paren):
                return _flatten_on_eq(n.this)
            if isinstance(n, exp.And):
                if n.expressions:
                    out: list[exp.Expression] = []
                    for e in n.expressions:
                        out.extend(_flatten_on_eq(e))
                    return out
                return _flatten_on_eq(n.this) + _flatten_on_eq(n.expression)
            return [n]

        on_terms = _flatten_on_eq(on_local)
        if not on_terms or any(not isinstance(term, exp.EQ) for term in on_terms):
            raise_error("[mdb][E2]", "Unsupported SQL construct: RIGHT_JOIN_NON_EQ")

        normalized = select_expr.copy()
        normalized.set("from_", exp.From(this=join_local.this.copy()))
        normalized.set(
            "joins",
            [
                exp.Join(
                    this=from_expr_local.this.copy(),
                    on=on_local.copy(),
                    side="LEFT",
                )
            ],
        )
        return normalized

    full_parts = _parse_full_outer_single_eq(expr)
    if full_parts is not None:
        return full_parts

    expr = _normalize_single_right_join(expr)

    from_expr = expr.args.get("from_")
    joins = expr.args.get("joins") or []
    if not from_expr or not hasattr(from_expr, "this") or not from_expr.this.name or len(joins) < 1:
        raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN")
    base_collection = from_expr.this.name
    base_alias = from_expr.this.alias_or_name or base_collection

    alias_map = {base_alias: "", base_collection: ""}
    pipeline: list[dict] = []
    join_specs: list[dict[str, Any]] = []
    optimize = os.environ.get("MONGO_DBAPI_OPTIMIZE", "1").lower() not in ("0", "false", "no")
    use_lookup_pipeline = os.environ.get("MONGO_DBAPI_LOOKUP_PIPELINE", "0").lower() in ("1", "true", "yes")

    def _flatten_on(n: exp.Expression) -> list[exp.Expression]:
        if isinstance(n, exp.Paren):
            return _flatten_on(n.this)
        if isinstance(n, exp.And):
            if n.expressions:
                out: list[exp.Expression] = []
                for e in n.expressions:
                    out.extend(_flatten_on(e))
                return out
            return _flatten_on(n.this) + _flatten_on(n.expression)
        return [n]

    # prepare joins (up to 3)
    if len(joins) > 3:
        raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_DEPTH")
    for idx, join_expr in enumerate(joins):
        join_side = (join_expr.args.get("side") or "").upper()
        join_kind = (join_expr.kind or "").upper()
        if join_side == "FULL":
            raise_error("[mdb][E2]", "Unsupported SQL construct: FULL_JOIN_CHAIN")
        if join_side == "RIGHT":
            raise_error("[mdb][E2]", "Unsupported SQL construct: RIGHT_JOIN_CHAIN")
        if join_side in ("LEFT",):
            if join_kind not in ("", "OUTER"):
                raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN")
        elif join_side in ("",):
            if join_kind not in ("", "INNER"):
                raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN")
        else:
            raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN")
        on_expr = join_expr.args.get("on")
        if not on_expr:
            raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_ON")
        on_terms = _flatten_on(on_expr)
        if not on_terms or any(not isinstance(t, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)) for t in on_terms):
            raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_ON")
        join_table = join_expr.this.this.name if hasattr(join_expr.this, "this") and hasattr(join_expr.this.this, "name") else None
        join_alias = join_expr.this.alias_or_name or join_table
        prefix = f"__join{idx}"
        alias_map[join_alias] = f"{prefix}."
        alias_map[join_table] = f"{prefix}."
        if not join_table:
            raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_TABLE")

        on_conditions: list[dict[str, Any]] = []
        eq_pairs: list[tuple[exp.Expression, str]] = []
        for term in on_terms:
            left_tbl, left_field = _column_table_field(term.left)
            right_tbl, right_field = _column_table_field(term.right)

            local_expr: exp.Expression
            join_field: str
            join_on_left: bool
            if left_tbl and left_tbl in (join_table, join_alias):
                if right_tbl and right_tbl not in alias_map:
                    raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_TABLE")
                local_expr = term.right
                join_field = left_field
                join_on_left = True
            elif right_tbl and right_tbl in (join_table, join_alias):
                if left_tbl and left_tbl not in alias_map:
                    raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_TABLE")
                local_expr = term.left
                join_field = right_field
                join_on_left = False
            else:
                raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_TABLE")
            on_conditions.append(
                {
                    "op": term.__class__,
                    "local_expr": local_expr,
                    "join_field": join_field,
                    "join_on_left": join_on_left,
                }
            )
            if isinstance(term, exp.EQ):
                eq_pairs.append((local_expr, join_field))

        join_specs.append(
            {
                "prefix": prefix,
                "join_table": join_table,
                "join_alias": join_alias or join_table or "",
                "join_expr": join_expr,
                "on_conditions": on_conditions,
                "eq_pairs": eq_pairs,
            }
        )

    def _flatten_and(n: exp.Expression) -> list[exp.Expression]:
        if isinstance(n, exp.And):
            if n.expressions:
                out: list[exp.Expression] = []
                for e in n.expressions:
                    out.extend(_flatten_and(e))
                return out
            return _flatten_and(n.this) + _flatten_and(n.expression)
        return [n]

    def _combine_and(items: list[exp.Expression]) -> exp.Expression:
        if not items:
            raise_error("[mdb][E5]", "Failed to parse SQL")
        node = items[0]
        for nxt in items[1:]:
            node = exp.And(this=node, expression=nxt)
        return node

    def _referenced_tables(n: exp.Expression) -> set[str]:
        tables: set[str] = set()
        for col in n.find_all(exp.Column):
            if col.table:
                tables.add(col.table)
        return tables

    where_expr = expr.args["where"].this if expr.args.get("where") else None
    base_terms: list[exp.Expression] = []
    join_terms: dict[int, list[exp.Expression]] = {}
    post_terms: list[exp.Expression] = []
    if where_expr is not None:
        # Always push base-only predicates before $lookup (semantics-preserving) /
        # 左テーブル（基底）だけに依存する条件は常に $lookup 前へ前倒し（セマンティクスは不変）
        all_terms = _flatten_and(where_expr)
        rest_terms: list[exp.Expression] = []
        table_to_join_idx: dict[str, int] = {}
        join_idx_tables: dict[int, set[str]] = {}
        for i, spec in enumerate(join_specs):
            join_table = spec["join_table"]
            join_alias = spec["join_alias"]
            join_idx_tables[i] = {join_table, join_alias}
            table_to_join_idx[join_table] = i
            table_to_join_idx[join_alias] = i
        for term in all_terms:
            tables = _referenced_tables(term)
            join_idxs = {table_to_join_idx[t] for t in tables if t in table_to_join_idx}
            if not join_idxs:
                base_terms.append(term)
            else:
                rest_terms.append(term)

        if optimize and rest_terms:
            for term in rest_terms:
                tables = _referenced_tables(term)
                join_idxs = {table_to_join_idx[t] for t in tables if t in table_to_join_idx}
                if len(join_idxs) == 1:
                    idx0 = next(iter(join_idxs))
                    if tables.issubset(join_idx_tables.get(idx0, set())):
                        join_terms.setdefault(idx0, []).append(term)
                    else:
                        post_terms.append(term)
                    continue
                post_terms.append(term)
        else:
            post_terms = rest_terms

    if base_terms:
        pipeline.append({"$match": _condition_to_filter_alias(_combine_and(base_terms), params_map, alias_map, subqueries)})

    join_group_count_opt = False
    if optimize and expr.args.get("group") and len(join_specs) == 1 and not post_terms and not join_terms.get(0):
        group_fields = expr.args.get("group")
        spec0 = join_specs[0]
        join_tables0 = {spec0["join_table"], spec0["join_alias"]}
        group_ok = True
        for col in (group_fields.expressions if group_fields else []):
            if not isinstance(col, exp.Column):
                group_ok = False
                break
            if col.table and col.table not in (base_alias, base_collection):
                group_ok = False
                break
        select_ok = True
        for exp_item in expr.expressions:
            target = exp_item.this if isinstance(exp_item, exp.Alias) else exp_item
            if isinstance(target, exp.Column):
                if target.table and target.table not in (base_alias, base_collection):
                    select_ok = False
                    break
                continue
            if isinstance(target, exp.Count) and isinstance(target.this, exp.Column):
                tbl, _fld = _column_table_field(target.this)
                if tbl not in join_tables0:
                    select_ok = False
                    break
                continue
            select_ok = False
            break
        join_group_count_opt = group_ok and select_ok

    # Semi-join optimization for DISTINCT (MongoDB 4.4) / DISTINCT のセミジョイン最適化（MongoDB 4.4）
    # Avoid unwind/group by matching joined array with $elemMatch (keeps localField/foreignField lookup) /
    # $lookup（localField/foreignField）を維持しつつ、$elemMatch で存在判定して unwind/group を回避する。
    if optimize and expr.args.get("distinct") and not post_terms and len(join_specs) == 1:
        spec = join_specs[0]
        join_prefix = spec["prefix"]
        join_table = spec["join_table"]
        join_alias = spec["join_alias"]
        join_expr = spec["join_expr"]
        on_conditions = spec["on_conditions"]
        eq_pairs = spec["eq_pairs"]
        join_side = (join_expr.args.get("side") or "").upper()
        preserve_null = bool(join_side == "LEFT" or (join_expr.kind and join_expr.kind.upper() == "LEFT"))
        if not preserve_null and len(expr.expressions) == 1 and not expr.is_star and len(on_conditions) == 1 and len(eq_pairs) == 1:
            left_expr, right_field = eq_pairs[0]
            item = expr.expressions[0]
            target = item.this if isinstance(item, exp.Alias) else item
            if isinstance(target, exp.Column):
                if target.table and target.table not in (base_alias, base_collection):
                    raise_error("[mdb][E2]", "Unsupported SQL construct: DISTINCT")
                out_name = item.alias_or_name or target.name
                base_field = _field_name(target, params_map)
                join_term_exprs = join_terms.get(0) or []
                foreign_alias_map = {"": "", join_table: "", join_alias: ""}
                foreign_filter = (
                    _condition_to_filter_alias(_combine_and(join_term_exprs), params_map, foreign_alias_map, subqueries)
                    if join_term_exprs
                    else None
                )

                pipeline.append(
                    {
                        "$lookup": {
                            "from": join_table,
                            "localField": _field_with_alias(left_expr, alias_map),
                            "foreignField": right_field,
                            "as": join_prefix,
                        }
                    }
                )
                if foreign_filter:
                    pipeline.append({"$match": {join_prefix: {"$elemMatch": foreign_filter}}})
                else:
                    pipeline.append({"$match": {join_prefix: {"$ne": []}}})
                pipeline.append({"$project": {out_name: f"${base_field}", "_id": 0}})

                if expr.args.get("order"):
                    sort_doc: dict[str, int] = {}
                    for e in expr.args["order"].expressions:
                        direction = -1 if e.args.get("desc") else 1
                        if isinstance(e.this, exp.Column):
                            ord_tbl = e.this.table
                            ord_name = e.this.name
                            if ord_tbl and ord_tbl not in (base_alias, base_collection):
                                raise_error("[mdb][E2]", "Unsupported SQL construct: DISTINCT_ORDER_BY")
                            if ord_name != target.name and ord_name != out_name:
                                raise_error("[mdb][E2]", "Unsupported SQL construct: DISTINCT_ORDER_BY")
                            sort_doc[out_name] = direction
                        elif isinstance(e.this, exp.Identifier):
                            if e.this.name != out_name:
                                raise_error("[mdb][E2]", "Unsupported SQL construct: DISTINCT_ORDER_BY")
                            sort_doc[out_name] = direction
                        else:
                            raise_error("[mdb][E2]", "Unsupported SQL construct: DISTINCT_ORDER_BY")
                    if sort_doc:
                        pipeline.append({"$sort": sort_doc})
                if expr.args.get("offset"):
                    skip_val = int(_literal_value(expr.args["offset"].expression, params_map, subqueries))
                    pipeline.append({"$skip": skip_val})
                if expr.args.get("limit"):
                    limit_val = int(_literal_value(expr.args["limit"].expression, params_map, subqueries))
                    pipeline.append({"$limit": limit_val})

                return QueryParts(
                    operation="aggregate",
                    collection=base_collection,
                    pipeline=pipeline,
                    projection_paths=[(out_name, out_name)],
                )

    early_sort_applied = False
    if expr.args.get("order") and not expr.args.get("group"):
        early_sort_doc: dict[str, int] = {}
        can_early_sort = True
        for e in expr.args["order"].expressions:
            if not isinstance(e.this, exp.Column):
                can_early_sort = False
                break
            fld = _field_with_alias(e.this, alias_map)
            direction = -1 if e.args.get("desc") else 1
            early_sort_doc[fld] = direction
        if can_early_sort and early_sort_doc and not any(k.startswith("__join") for k in early_sort_doc):
            pipeline.append({"$sort": early_sort_doc})
            early_sort_applied = True

    for idx, spec in enumerate(join_specs):
        prefix = spec["prefix"]
        join_table = spec["join_table"]
        join_alias = spec["join_alias"]
        join_expr = spec["join_expr"]
        on_conditions = spec["on_conditions"]
        join_side = (join_expr.args.get("side") or "").upper()
        preserve_null = bool(join_side == "LEFT" or (join_expr.kind and join_expr.kind.upper() == "LEFT"))
        join_term_exprs = join_terms.get(idx) if optimize else None
        can_push_lookup_filter = bool(join_term_exprs) and not preserve_null and use_lookup_pipeline

        all_eq = all(cond["op"] is exp.EQ for cond in on_conditions)
        simple_eq = all_eq and len(on_conditions) == 1 and isinstance(on_conditions[0]["local_expr"], exp.Column)

        if not simple_eq:
            # Non-equi or composite ON requires $lookup pipeline (MongoDB 4.4) /
            # 非等価または複合 ON は $lookup pipeline が必須（MongoDB 4.4）
            let_vars: dict[str, Any] = {}
            expr_terms: list[dict[str, Any]] = []
            op_map: dict[type[exp.Expression], str] = {
                exp.EQ: "$eq",
                exp.NEQ: "$ne",
                exp.GT: "$gt",
                exp.GTE: "$gte",
                exp.LT: "$lt",
                exp.LTE: "$lte",
            }
            for j, cond in enumerate(on_conditions):
                local_expr = cond["local_expr"]
                foreign_field = cond["join_field"]
                mongo_op = op_map.get(cond["op"], "$eq")
                if isinstance(local_expr, exp.Column):
                    col_token = ".".join(part.name for part in local_expr.parts if hasattr(part, "name"))
                    if col_token in params_map:
                        local_ref: Any = _literal_value(local_expr, params_map, subqueries)
                    else:
                        v = f"l{len(let_vars)}"
                        let_vars[v] = f"${_field_with_alias(local_expr, alias_map)}"
                        local_ref = f"$${v}"
                else:
                    local_ref = _literal_value(local_expr, params_map, subqueries)
                left_ref = f"${foreign_field}" if cond["join_on_left"] else local_ref
                right_ref = local_ref if cond["join_on_left"] else f"${foreign_field}"
                expr_terms.append({mongo_op: [left_ref, right_ref]})
            base_expr: dict[str, Any] = {"$and": expr_terms} if len(expr_terms) > 1 else expr_terms[0]
            lookup_pipeline: list[dict[str, Any]] = [{"$match": {"$expr": base_expr}}]
            if can_push_lookup_filter:
                foreign_alias_map = {"": "", join_table: "", join_alias: ""}
                foreign_filter = _condition_to_filter_alias(
                    _combine_and(join_term_exprs or []),
                    params_map,
                    foreign_alias_map,
                    subqueries,
                )
                lookup_pipeline.append({"$match": foreign_filter})
            pipeline.append(
                {
                    "$lookup": {
                        "from": join_table,
                        "let": let_vars,
                        "pipeline": lookup_pipeline,
                        "as": prefix,
                    }
                }
            )
        else:
            left_expr = on_conditions[0]["local_expr"]
            right_field = on_conditions[0]["join_field"]
            if can_push_lookup_filter:
                foreign_alias_map = {"": "", join_table: "", join_alias: ""}
                foreign_filter = _condition_to_filter_alias(
                    _combine_and(join_term_exprs or []),
                    params_map,
                    foreign_alias_map,
                    subqueries,
                )
                pipeline.append(
                    {
                        "$lookup": {
                            "from": join_table,
                            "let": {"local": f"${_field_with_alias(left_expr, alias_map)}"},
                            "pipeline": [
                                {"$match": {"$expr": {"$eq": [f"${right_field}", "$$local"]}}},
                                {"$match": foreign_filter},
                            ],
                            "as": prefix,
                        }
                    }
                )
            else:
                pipeline.append(
                    {
                        "$lookup": {
                            "from": join_table,
                            "localField": _field_with_alias(left_expr, alias_map),
                            "foreignField": right_field,
                            "as": prefix,
                        }
                    }
                )
        if not (join_group_count_opt and idx == 0):
            pipeline.append({"$unwind": {"path": f"${prefix}", "preserveNullAndEmptyArrays": preserve_null}})
            if optimize and join_terms.get(idx) and not can_push_lookup_filter:
                pipeline.append({"$match": _condition_to_filter_alias(_combine_and(join_terms[idx]), params_map, alias_map, subqueries)})

    if post_terms:
        pipeline.append({"$match": _condition_to_filter_alias(_combine_and(post_terms), params_map, alias_map, subqueries)})

    if expr.args.get("group"):
        group_fields = expr.args.get("group")
        if not group_fields:
            raise_error("[mdb][E5]", "Failed to parse SQL")

        group_id: dict[str, str] = {}
        group_cols: list[str] = []
        for col in group_fields.expressions:
            name = _field_name(col, params_map)
            path = _field_with_alias(col, alias_map)
            group_id[name] = f"${path}"
            group_cols.append(name)

        agg_fields: dict[str, dict[str, Any]] = {}
        count_distinct_aliases: set[str] = set()
        projection_paths: list[tuple[str, str]] = []
        lookup_count_cache: dict[tuple[str, str], str] = {}
        lookup_count_add_fields: dict[str, Any] = {}
        final_order: list[str] = []
        seen_outputs: list[str] = []
        for exp_item in expr.expressions:
            target = exp_item.this if isinstance(exp_item, exp.Alias) else exp_item
            alias = exp_item.alias_or_name or getattr(target, "alias_or_name", None) or None
            if not alias:
                if isinstance(target, (exp.Sum, exp.Count, exp.Avg, exp.Min, exp.Max)):
                    base_name = None
                    if hasattr(target, "this") and target.this:
                        try:
                            base_name = _field_name(target.this, params_map)
                        except Exception:
                            base_name = target.__class__.__name__.lower()
                    alias = f"{target.__class__.__name__.lower()}_{base_name or len(agg_fields)}"
                elif isinstance(target, exp.Column):
                    alias = _field_name(target, params_map)
                else:
                    alias = f"agg_{len(agg_fields)}"
            if alias in seen_outputs:
                continue
            seen_outputs.append(alias)
            final_order.append(alias)

            if isinstance(target, exp.Column):
                col_path = _field_with_alias(target, alias_map)
                agg_fields[alias] = {"$first": f"${col_path}"}
            elif isinstance(target, exp.Count):
                if isinstance(target.this, exp.Distinct):
                    distinct_exprs = target.this.expressions or []
                    if len(distinct_exprs) != 1 or not isinstance(distinct_exprs[0], exp.Column):
                        raise_error("[mdb][E2]", "Unsupported SQL construct: COUNT_DISTINCT")
                    col_path = _field_with_alias(distinct_exprs[0], alias_map)
                    agg_fields[alias] = {"$addToSet": f"${col_path}"}
                    count_distinct_aliases.add(alias)
                elif isinstance(target.this, exp.Column):
                    col_path = _field_with_alias(target.this, alias_map)
                    if join_group_count_opt:
                        tbl, fld = _column_table_field(target.this)
                        spec0 = join_specs[0]
                        if tbl in (spec0["join_table"], spec0["join_alias"]):
                            count_key = (spec0["prefix"], fld)
                            cached_name = lookup_count_cache.get(count_key)
                            if cached_name is None:
                                cached_name = f"__join_count_{len(lookup_count_cache)}"
                                lookup_count_cache[count_key] = cached_name
                                lookup_count_add_fields[cached_name] = {
                                    "$size": {
                                        "$filter": {
                                            "input": f"${spec0['prefix']}",
                                            "as": "j",
                                            "cond": {
                                                "$and": [
                                                    {"$ne": [{"$type": f"$$j.{fld}"}, "missing"]},
                                                    {"$ne": [f"$$j.{fld}", None]},
                                                ]
                                            },
                                        }
                                    }
                                }
                            agg_fields[alias] = {"$sum": f"${cached_name}"}
                        else:
                            agg_fields[alias] = {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {"$ne": [{"$type": f"${col_path}"}, "missing"]},
                                                {"$ne": [f"${col_path}", None]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            }
                    else:
                        agg_fields[alias] = {
                            "$sum": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$ne": [{"$type": f"${col_path}"}, "missing"]},
                                            {"$ne": [f"${col_path}", None]},
                                        ]
                                    },
                                    1,
                                    0,
                                ]
                            }
                        }
                else:
                    agg_fields[alias] = {"$sum": 1}
            elif isinstance(target, exp.Sum):
                if isinstance(target.this, exp.Case):
                    raise_error("[mdb][E2]", "Unsupported SQL construct: GROUP_SELECT")
                col_path = _field_with_alias(target.this, alias_map)
                agg_fields[alias] = {"$sum": f"${col_path}"}
            elif isinstance(target, exp.Avg):
                col_path = _field_with_alias(target.this, alias_map)
                agg_fields[alias] = {"$avg": f"${col_path}"}
            elif isinstance(target, exp.Min):
                col_path = _field_with_alias(target.this, alias_map)
                agg_fields[alias] = {"$min": f"${col_path}"}
            elif isinstance(target, exp.Max):
                col_path = _field_with_alias(target.this, alias_map)
                agg_fields[alias] = {"$max": f"${col_path}"}
            else:
                raise_error("[mdb][E2]", "Unsupported SQL construct: GROUP_SELECT")

        if lookup_count_add_fields:
            pipeline.append({"$addFields": lookup_count_add_fields})

        group_stage: dict[str, Any] = {"_id": group_id}
        group_stage.update(agg_fields)
        pipeline.append({"$group": group_stage})

        having_filter = None
        if expr.args.get("having"):
            having_alias_map = {k: "" for k in list(group_cols) + list(agg_fields.keys())}
            having_filter = _condition_to_filter_alias(expr.args["having"].this, params_map, having_alias_map, subqueries)

        project_doc: dict[str, Any] = {}
        for key in final_order:
            if key in group_cols:
                project_doc[key] = f"$_id.{key}"
            elif key in count_distinct_aliases:
                project_doc[key] = {"$size": f"${key}"}
            else:
                project_doc[key] = f"${key}"
            projection_paths.append((key, key))
        pipeline.append({"$project": project_doc})
        if having_filter:
            pipeline.append({"$match": having_filter})

        if expr.args.get("order"):
            sort_doc: dict[str, int] = {}
            for e in expr.args["order"].expressions:
                field = _field_name(e.this, params_map)
                direction = -1 if e.args.get("desc") else 1
                sort_doc[field] = direction
            if sort_doc:
                pipeline.append({"$sort": sort_doc})
        if expr.args.get("offset"):
            skip_val = int(_literal_value(expr.args["offset"].expression, params_map, subqueries))
            pipeline.append({"$skip": skip_val})
        if expr.args.get("limit"):
            limit_val = int(_literal_value(expr.args["limit"].expression, params_map, subqueries))
            pipeline.append({"$limit": limit_val})

        return QueryParts(
            operation="aggregate",
            collection=base_collection,
            pipeline=pipeline,
            projection_paths=projection_paths,
        )

    if expr.args.get("distinct"):
        if expr.is_star:
            raise_error("[mdb][E2]", "Unsupported SQL construct: DISTINCT_STAR")
        distinct_items: list[tuple[str, str]] = []
        for item in expr.expressions:
            target = item.this if isinstance(item, exp.Alias) else item
            if not isinstance(target, exp.Column):
                raise_error("[mdb][E2]", "Unsupported SQL construct: DISTINCT")
            out_name = item.alias_or_name or target.name
            path = _field_with_alias(target, alias_map)
            distinct_items.append((path, out_name))
        if len({out for _path, out in distinct_items}) != len(distinct_items):
            raise_error("[mdb][E2]", "Unsupported SQL construct: DISTINCT_DUPLICATE_ALIAS")
        if len(distinct_items) == 1:
            path, out_name = distinct_items[0]
            pipeline.append({"$group": {"_id": f"${path}"}})
            pipeline.append({"$project": {out_name: "$_id", "_id": 0}})
        else:
            group_id = {out: f"${path}" for path, out in distinct_items}
            pipeline.append({"$group": {"_id": group_id}})
            project_doc: dict[str, Any] = {"_id": 0}
            for _path, out in distinct_items:
                project_doc[out] = f"$_id.{out}"
            pipeline.append({"$project": project_doc})
        if expr.args.get("order"):
            sort_doc: dict[str, int] = {}
            path_to_out = {path: out for path, out in distinct_items}
            out_names = {out for _path, out in distinct_items}
            for e in expr.args["order"].expressions:
                direction = -1 if e.args.get("desc") else 1
                if isinstance(e.this, exp.Column):
                    ord_path = _field_with_alias(e.this, alias_map)
                    if ord_path in path_to_out:
                        sort_key = path_to_out[ord_path]
                    elif e.this.name in out_names:
                        sort_key = e.this.name
                    else:
                        raise_error("[mdb][E2]", "Unsupported SQL construct: DISTINCT_ORDER_BY")
                elif isinstance(e.this, exp.Identifier):
                    if e.this.name in out_names:
                        sort_key = e.this.name
                    else:
                        raise_error("[mdb][E2]", "Unsupported SQL construct: DISTINCT_ORDER_BY")
                else:
                    raise_error("[mdb][E2]", "Unsupported SQL construct: DISTINCT_ORDER_BY")
                sort_doc[sort_key] = direction
            if sort_doc:
                pipeline.append({"$sort": sort_doc})
        if expr.args.get("offset"):
            skip_val = int(_literal_value(expr.args["offset"].expression, params_map, subqueries))
            pipeline.append({"$skip": skip_val})
        if expr.args.get("limit"):
            limit_val = int(_literal_value(expr.args["limit"].expression, params_map, subqueries))
            pipeline.append({"$limit": limit_val})
        return QueryParts(
            operation="aggregate",
            collection=base_collection,
            pipeline=pipeline,
            projection_paths=[(out, out) for _path, out in distinct_items],
        )

    if expr.args.get("order"):
        sort_doc: dict[str, int] = {}
        for e in expr.args["order"].expressions:
            if isinstance(e.this, exp.Column):
                field = _field_with_alias(e.this, alias_map)
            else:
                field = _field_name(e.this, params_map)
            direction = -1 if e.args.get("desc") else 1
            sort_doc[field] = direction
    else:
        sort_doc = {}

    skip_val: int | None = None
    if expr.args.get("offset"):
        skip_val = int(_literal_value(expr.args["offset"].expression, params_map, subqueries))

    limit_val: int | None = None
    if expr.args.get("limit"):
        limit_val = int(_literal_value(expr.args["limit"].expression, params_map, subqueries))

    projection_paths: list[tuple[str, str]] | None = None
    if not expr.is_star:
        projection_paths = []
        for c in expr.expressions:
            target = c.this if isinstance(c, exp.Alias) else c
            out_name = c.alias_or_name or (target.alias_or_name if isinstance(target, exp.Column) else None)
            if isinstance(target, exp.Column):
                tbl, fld = _column_table_field(target)
                if tbl and tbl not in alias_map:
                    raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_COLUMN")
                path = _field_with_alias(target, alias_map)
                out = out_name or (f"{tbl}.{fld}" if tbl and tbl != base_collection else fld)
                projection_paths.append((path, out))
            else:
                raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_PROJECTION")

    if sort_doc:
        if not early_sort_applied:
            pipeline.append({"$sort": sort_doc})
    if skip_val is not None:
        pipeline.append({"$skip": skip_val})
    if limit_val is not None:
        pipeline.append({"$limit": limit_val})

    # JOIN projection is not always a win for paginated queries (it can add per-document overhead) /
    # ページング用途では JOIN 投影が常に速いとは限らない（1ドキュメントあたりのオーバーヘッドになり得る）
    # so keep it opt-in or enable it for larger result sets. /
    # そのため明示指定か、大きい結果セットでのみ有効化する。
    join_project_forced = os.environ.get("MONGO_DBAPI_JOIN_PROJECT", "0").lower() in ("1", "true", "yes")
    join_project_auto = limit_val is None or limit_val >= 1000
    if optimize and projection_paths and (join_project_forced or join_project_auto):
        project_doc: dict[str, int] = {"_id": 0}
        for path, _out in projection_paths:
            project_doc[path] = 1
        pipeline.append({"$project": project_doc})

    return QueryParts(
        operation="aggregate",
        collection=base_collection,
        pipeline=pipeline,
        projection_paths=projection_paths,
    )


def _parse_group_select(expr: exp.Select, params_map: dict[str, Any], subqueries: dict[str, dict[str, Any]]) -> QueryParts:
    table = expr.find(exp.Table)
    if not table or not table.name:
        raise_error("[mdb][E5]", "Failed to parse SQL")
    pipeline: list[dict] = []
    if expr.args.get("where"):
        where_filter = _condition_to_filter(expr.args["where"].this, params_map, subqueries)
        pipeline.append({"$match": where_filter})
    group_fields = expr.args.get("group")
    if not group_fields:
        raise_error("[mdb][E5]", "Failed to parse SQL")
    group_id: dict[str, str] = {}
    group_cols: list[str] = []
    for col in group_fields.expressions:
        name = _field_name(col, params_map)
        group_id[name] = f"${name}"
        group_cols.append(name)

    agg_fields: dict[str, dict] = {}
    count_distinct_aliases: set[str] = set()
    projection_paths: list[tuple[str, str]] = []
    final_order: list[str] = []
    seen_outputs: list[str] = []
    for exp_item in expr.expressions:
        target = exp_item.this if isinstance(exp_item, exp.Alias) else exp_item
        alias = exp_item.alias_or_name or getattr(target, "alias_or_name", None) or None
        if not alias:
            if isinstance(target, (exp.Sum, exp.Count, exp.Avg, exp.Min, exp.Max)):
                base_name = None
                if hasattr(target, "this") and target.this:
                    try:
                        base_name = _field_name(target.this, params_map)
                    except Exception:
                        base_name = target.__class__.__name__.lower()
                alias = f"{target.__class__.__name__.lower()}_{base_name or len(agg_fields)}"
            elif isinstance(target, exp.Column):
                alias = _field_name(target, params_map)
            else:
                alias = f"agg_{len(agg_fields)}"
        if alias in seen_outputs:
            continue
        seen_outputs.append(alias)
        final_order.append(alias)
        if isinstance(target, exp.Column):
            col_name = _field_name(target, params_map)
            agg_fields[alias] = {"$first": f"${col_name}"}
        elif isinstance(target, exp.Count):
            if isinstance(target.this, exp.Distinct):
                distinct_exprs = target.this.expressions or []
                if len(distinct_exprs) != 1 or not isinstance(distinct_exprs[0], exp.Column):
                    raise_error("[mdb][E2]", "Unsupported SQL construct: COUNT_DISTINCT")
                col_name = _field_name(distinct_exprs[0], params_map)
                agg_fields[alias] = {"$addToSet": f"${col_name}"}
                count_distinct_aliases.add(alias)
            elif isinstance(target.this, exp.Column):
                col_name = _field_name(target.this, params_map)
                agg_fields[alias] = {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$ne": [{"$type": f"${col_name}"}, "missing"]},
                                    {"$ne": [f"${col_name}", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                }
            else:
                agg_fields[alias] = {"$sum": 1}
        elif isinstance(target, exp.Sum):
            if isinstance(target.this, exp.Case):
                agg_fields[alias] = {"$sum": _case_to_cond(target.this, params_map, subqueries)}
            else:
                col_name = _field_name(target.this, params_map)
                agg_fields[alias] = {"$sum": f"${col_name}"}
        elif isinstance(target, exp.Avg):
            col_name = _field_name(target.this, params_map)
            agg_fields[alias] = {"$avg": f"${col_name}"}
        elif isinstance(target, exp.Min):
            col_name = _field_name(target.this, params_map)
            agg_fields[alias] = {"$min": f"${col_name}"}
        elif isinstance(target, exp.Max):
            col_name = _field_name(target.this, params_map)
            agg_fields[alias] = {"$max": f"${col_name}"}
        else:
            raise_error("[mdb][E2]", "Unsupported SQL construct: GROUP_SELECT")

    group_stage: dict[str, Any] = {"_id": group_id}
    group_stage.update(agg_fields)
    pipeline.append({"$group": group_stage})

    having_filter = None
    if expr.args.get("having"):
        alias_map = {k: "" for k in list(group_cols) + list(agg_fields.keys())}
        having_filter = _condition_to_filter_alias(expr.args["having"].this, params_map, alias_map, subqueries)

    project_doc: dict[str, Any] = {}
    for key in final_order:
        if key in group_cols:
            project_doc[key] = f"$_id.{key}"
        elif key in count_distinct_aliases:
            project_doc[key] = {"$size": f"${key}"}
        else:
            project_doc[key] = f"${key}"
        projection_paths.append((key, key))
    pipeline.append({"$project": project_doc})
    if having_filter:
        pipeline.append({"$match": having_filter})

    if expr.args.get("order"):
        sort_doc: dict[str, int] = {}
        for e in expr.args["order"].expressions:
            field = _field_name(e.this, params_map)
            direction = -1 if e.args.get("desc") else 1
            sort_doc[field] = direction
        if sort_doc:
            pipeline.append({"$sort": sort_doc})
    if expr.args.get("offset"):
        skip_val = int(_literal_value(expr.args["offset"].expression, params_map, subqueries))
        pipeline.append({"$skip": skip_val})
    if expr.args.get("limit"):
        limit_val = int(_literal_value(expr.args["limit"].expression, params_map, subqueries))
        pipeline.append({"$limit": limit_val})

    return QueryParts(
        operation="aggregate",
        collection=table.name,
        pipeline=pipeline,
        projection_paths=projection_paths,
    )


def _parse_insert(expr: exp.Insert, params_map: dict[str, Any], subqueries: dict[str, dict[str, Any]]) -> QueryParts:
    table_expr = expr.this
    columns: List[str] = []
    table_name = None
    if isinstance(table_expr, exp.Schema):
        table_name = table_expr.this.name if table_expr.this else None
        columns = [c.name for c in table_expr.expressions]
    elif table_expr and table_expr.name:
        table_name = table_expr.name
    if not table_name:
        raise_error("[mdb][E5]", "Failed to parse SQL")
    values_exp = expr.expression
    if not isinstance(values_exp, exp.Values):
        raise_error("[mdb][E5]", "Failed to parse SQL")
    if len(values_exp.expressions) != 1:
        raise_error("[mdb][E5]", "Failed to parse SQL")
    row = values_exp.expressions[0]
    values = [_literal_value(v, params_map, subqueries) for v in row.expressions]
    if columns and len(columns) != len(values):
        raise_error("[mdb][E4]")
    doc = dict(zip(columns, values)) if columns else dict(enumerate(values))
    return QueryParts(
        operation="insert",
        collection=table_name,
        values=doc,
    )


def _parse_update(expr: exp.Update, params_map: dict[str, Any], subqueries: dict[str, dict[str, Any]]) -> QueryParts:
    table = expr.this
    if not table or not table.name:
        raise_error("[mdb][E5]", "Failed to parse SQL")
    assignments = {}
    set_exp = expr.args.get("expressions") or []
    for assign in set_exp:
        if not isinstance(assign, exp.EQ):
            raise_error("[mdb][E5]")
        field = _field_name(assign.left, params_map)
        value = _literal_value(assign.right, params_map, subqueries)
        assignments[field] = value
    where_clause = expr.args.get("where")
    if not where_clause:
        raise_error("[mdb][E3]")
    mongo_filter = _condition_to_filter(where_clause.this, params_map, subqueries)
    return QueryParts(
        operation="update",
        collection=table.name,
        filter=mongo_filter,
        update={"$set": assignments},
    )


def _parse_delete(expr: exp.Delete, params_map: dict[str, Any], subqueries: dict[str, dict[str, Any]]) -> QueryParts:
    table = expr.this if hasattr(expr, "this") else None
    if not table or not table.name:
        raise_error("[mdb][E5]", "Failed to parse SQL")
    where_clause = expr.args.get("where")
    if not where_clause:
        raise_error("[mdb][E3]")
    mongo_filter = _condition_to_filter(where_clause.this, params_map, subqueries)
    return QueryParts(
        operation="delete",
        collection=table.name,
        filter=mongo_filter,
    )


def _parse_create(expr: exp.Create) -> QueryParts:
    table = expr.this.this.name if hasattr(expr.this, "this") and hasattr(expr.this.this, "name") else None
    if not table:
        raise_error("[mdb][E5]", "Failed to parse SQL")
    return QueryParts(operation="create", collection=table)


def _parse_drop(expr: exp.Drop) -> QueryParts:
    table = expr.this.this.name if hasattr(expr.this, "this") and hasattr(expr.this.this, "name") else None
    if not table:
        raise_error("[mdb][E5]", "Failed to parse SQL")
    return QueryParts(operation="drop", collection=table)


def _parse_create_index_sql(sql: str) -> QueryParts | None:
    m = CREATE_INDEX_RE.match(sql.strip())
    if not m:
        return None
    unique = bool(m.group(1))
    index_name = m.group(2)
    table = m.group(3)
    cols_raw = m.group(4)
    keys: list[tuple[str, int]] = []
    for col in cols_raw.split(","):
        parts = col.strip().split()
        if not parts:
            continue
        name = parts[0]
        direction = 1
        if len(parts) > 1 and parts[1].lower() == "desc":
            direction = -1
        keys.append((name, direction))
    return QueryParts(operation="create_index", collection=table, index_keys=keys, index_name=index_name, unique=unique)


def _parse_drop_index_sql(sql: str) -> QueryParts | None:
    m = DROP_INDEX_RE.match(sql.strip())
    if not m:
        return None
    index_name = m.group(1)
    table = m.group(2)
    return QueryParts(operation="drop_index", collection=table, index_name=index_name)
