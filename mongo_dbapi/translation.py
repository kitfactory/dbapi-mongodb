from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Mapping

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


def _literal_value(node: exp.Expression, params_map: dict[str, Any]) -> Any:
    """Extract value from SQLGlot expression / SQLGlot 式から値を取得"""
    if isinstance(node, exp.Literal):
        if node.is_string:
            return node.this
        try:
            return node.to_python()
        except Exception:
            return node.this
    if isinstance(node, exp.Column):
        name = ".".join(part.name for part in node.parts if hasattr(part, "name"))
        if name in params_map:
            return params_map[name]
        raise_error("[mdb][E2]", "Unsupported SQL construct: COLUMN_AS_VALUE")
    if isinstance(node, exp.Tuple):
        return [_literal_value(e, params_map) for e in node.expressions]
    raise_error("[mdb][E2]")


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
        if not tbl:
            # default to base alias (empty prefix)
            return f"{alias_map.get('', '')}{fld}"
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


def _condition_to_filter(node: exp.Expression, params_map: dict[str, Any]) -> Dict[str, Any]:
    """Convert WHERE expression to Mongo filter / WHERE を Mongo フィルタへ変換"""
    if isinstance(node, exp.And):
        parts = []
        if node.expressions:
            parts = [_condition_to_filter(e, params_map) for e in node.expressions]
        else:
            parts = [_condition_to_filter(node.this, params_map), _condition_to_filter(node.expression, params_map)]
        return {"$and": parts}
    if isinstance(node, exp.Or):
        parts = []
        if node.expressions:
            parts = [_condition_to_filter(e, params_map) for e in node.expressions]
        else:
            parts = [_condition_to_filter(node.this, params_map), _condition_to_filter(node.expression, params_map)]
        return {"$or": parts}
    if isinstance(node, exp.Between):
        field = _field_name(node.this, params_map)
        low = _literal_value(node.args["low"], params_map)
        high = _literal_value(node.args["high"], params_map)
        return {field: {"$gte": low, "$lte": high}}
    if isinstance(node, exp.Like):
        field = _field_name(node.this, params_map)
        value = _literal_value(node.expression, params_map)
        if not isinstance(value, str):
            raise_error("[mdb][E2]", "Unsupported SQL construct: LIKE")
        regex = _like_to_regex(value)
        return {field: {"$regex": regex}}
    if hasattr(exp, "ILike") and isinstance(node, getattr(exp, "ILike")):
        field = _field_name(node.this, params_map)
        value = _literal_value(node.expression, params_map)
        regex = _like_to_regex(str(value))
        return {field: {"$regex": regex, "$options": "i"}}
    if hasattr(exp, "Regex") and isinstance(node, getattr(exp, "Regex")):
        field = _field_name(node.this, params_map)
        pattern = _literal_value(node.expression, params_map)
        return {field: {"$regex": str(pattern)}}
    if isinstance(node, exp.In):
        field = _field_name(node.this, params_map)
        values = _literal_value(node.expression, params_map)
        return {field: {"$in": values}}
    if isinstance(node, exp.Paren):
        return _condition_to_filter(node.this, params_map)
    if isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        left = node.left
        right = node.right
        field = _field_name(left, params_map)
        value = _literal_value(right, params_map)
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


def _condition_to_filter_join(node: exp.Expression, params_map: dict[str, Any], allowed_table: str) -> Dict[str, Any]:
    """WHERE for JOIN: only allow columns from allowed_table / JOIN の WHERE は左テーブルのみ許可"""
    if isinstance(node, exp.And):
        filters = []
        if node.expressions:
            filters = [_condition_to_filter_join(e, params_map, allowed_table) for e in node.expressions]
        else:
            filters = [
                _condition_to_filter_join(node.this, params_map, allowed_table),
                _condition_to_filter_join(node.expression, params_map, allowed_table),
            ]
        return {"$and": filters}
    if isinstance(node, exp.Or):
        filters = []
        if node.expressions:
            filters = [_condition_to_filter_join(e, params_map, allowed_table) for e in node.expressions]
        else:
            filters = [
                _condition_to_filter_join(node.this, params_map, allowed_table),
                _condition_to_filter_join(node.expression, params_map, allowed_table),
            ]
        return {"$or": filters}
    if isinstance(node, exp.In):
        tbl, field = _column_table_field(node.this)
        if tbl and tbl != allowed_table:
            raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_WHERE_RIGHT_TABLE")
        values = _literal_value(node.expression, params_map)
        return {field: {"$in": values}}
    if isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        tbl, field = _column_table_field(node.left)
        if tbl and tbl != allowed_table:
            raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_WHERE_RIGHT_TABLE")
        value = _literal_value(node.right, params_map)
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
        return _condition_to_filter_join(node.this, params_map, allowed_table)
    if isinstance(node, exp.Between):
        tbl, field = _column_table_field(node.this)
        if tbl and tbl != allowed_table:
            raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_WHERE_RIGHT_TABLE")
        low = _literal_value(node.args["low"], params_map)
        high = _literal_value(node.args["high"], params_map)
        return {field: {"$gte": low, "$lte": high}}
    if isinstance(node, exp.Like):
        tbl, field = _column_table_field(node.this)
        if tbl and tbl != allowed_table:
            raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_WHERE_RIGHT_TABLE")
        value = _literal_value(node.expression, params_map)
        regex = _like_to_regex(str(value))
        return {field: {"$regex": regex}}
    if hasattr(exp, "ILike") and isinstance(node, getattr(exp, "ILike")):
        tbl, field = _column_table_field(node.this)
        if tbl and tbl != allowed_table:
            raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_WHERE_RIGHT_TABLE")
        value = _literal_value(node.expression, params_map)
        regex = _like_to_regex(str(value))
        return {field: {"$regex": regex, "$options": "i"}}
    if isinstance(node, exp.Or):
        filters = [_condition_to_filter_join(e, params_map, allowed_table) for e in node.expressions]
        return {"$or": filters}
    if isinstance(node, exp.Or) or isinstance(node, exp.Between) or isinstance(node, exp.Like):
        raise_error("[mdb][E2]")
    raise_error("[mdb][E2]")


def _condition_to_filter_alias(node: exp.Expression, params_map: dict[str, Any], alias_map: dict[str, str]) -> Dict[str, Any]:
    """WHERE with alias prefixes / エイリアス付き WHERE を Mongo フィルタへ変換"""
    if isinstance(node, exp.And):
        parts = []
        if node.expressions:
            parts = [_condition_to_filter_alias(e, params_map, alias_map) for e in node.expressions]
        else:
            parts = [
                _condition_to_filter_alias(node.this, params_map, alias_map),
                _condition_to_filter_alias(node.expression, params_map, alias_map),
            ]
        return {"$and": parts}
    if isinstance(node, exp.Or):
        parts = []
        if node.expressions:
            parts = [_condition_to_filter_alias(e, params_map, alias_map) for e in node.expressions]
        else:
            parts = [
                _condition_to_filter_alias(node.this, params_map, alias_map),
                _condition_to_filter_alias(node.expression, params_map, alias_map),
            ]
        return {"$or": parts}
    if isinstance(node, exp.Between):
        field = _field_with_alias(node.this, alias_map)
        low = _literal_value(node.args["low"], params_map)
        high = _literal_value(node.args["high"], params_map)
        return {field: {"$gte": low, "$lte": high}}
    if isinstance(node, exp.Like):
        field = _field_with_alias(node.this, alias_map)
        value = _literal_value(node.expression, params_map)
        regex = _like_to_regex(str(value))
        return {field: {"$regex": regex}}
    if hasattr(exp, "ILike") and isinstance(node, getattr(exp, "ILike")):
        field = _field_with_alias(node.this, alias_map)
        value = _literal_value(node.expression, params_map)
        regex = _like_to_regex(str(value))
        return {field: {"$regex": regex, "$options": "i"}}
    if isinstance(node, exp.In):
        field = _field_with_alias(node.this, alias_map)
        values = _literal_value(node.expression, params_map)
        return {field: {"$in": values}}
    if isinstance(node, exp.Paren):
        return _condition_to_filter_alias(node.this, params_map, alias_map)
    if isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
        field = _field_with_alias(node.left, alias_map)
        value = _literal_value(node.right, params_map)
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
    unsupported = (exp.Or, exp.Between, exp.Like, exp.Offset, exp.Subquery)
    for node in expr.walk():
        if isinstance(node, unsupported):
            keyword = node.key.upper() if hasattr(node, "key") else node.__class__.__name__
            raise_error("[mdb][E2]", f"Unsupported SQL construct: {keyword}")


def parse_sql(sql: str, params: Sequence[Any] | Mapping[str, Any] | None = None) -> QueryParts:
    """Parse SQL to QueryParts / SQL を QueryParts に変換"""
    normalized_sql, param_values, tokens = preprocess_sql(sql, params)
    params_map = {tokens[i]: val for i, val in enumerate(param_values)}
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

    if isinstance(expr, exp.Union):
        if expr.args.get("distinct"):
            raise_error("[mdb][E2]", "Unsupported SQL construct: UNION DISTINCT")
        left = _parse_select_like(expr.left, params_map)
        right = _parse_select_like(expr.right, params_map)
        order = None
        limit = None
        if expr.args.get("order"):
            order = []
            for e in expr.args["order"].expressions:
                field = _field_name(e.this, params_map)
                direction = -1 if e.args.get("desc") else 1
                order.append((field, direction))
        if expr.args.get("limit"):
            try:
                limit = int(expr.args["limit"].expression.name)
            except Exception:
                limit = int(expr.args["limit"].expression.this)
        return QueryParts(operation="union_all", collection="", union_parts=[left, right], sort=order, limit=limit)

    if isinstance(expr, exp.Select):
        # window function detection
        if expr.find(exp.Window) or expr.find(exp.RowNumber) or expr.find(exp.Rank) or expr.find(exp.DenseRank):
            table = expr.find(exp.Table)
            collection = table.name if table and table.name else ""
            return QueryParts(operation="window", collection=collection)
        if expr.args.get("joins"):
            return _parse_join_select(expr, params_map)
        if expr.args.get("group"):
            return _parse_group_select(expr, params_map)
        return _parse_select(expr, params_map)
    if isinstance(expr, exp.Insert):
        return _parse_insert(expr, params_map)
    if isinstance(expr, exp.Update):
        return _parse_update(expr, params_map)
    if isinstance(expr, exp.Delete):
        return _parse_delete(expr, params_map)
    if isinstance(expr, exp.Create):
        return _parse_create(expr)
    if isinstance(expr, exp.Drop):
        return _parse_drop(expr)
    raise_error("[mdb][E2]", "Unsupported SQL construct: STATEMENT")


def _parse_select(expr: exp.Select, params_map: dict[str, Any]) -> QueryParts:
    table = expr.find(exp.Table)
    if not table or not table.name:
        raise_error("[mdb][E5]", "Failed to parse SQL")
    collection = table.name
    projection: List[str] | None = None
    if not expr.is_star:
        projection = [c.alias_or_name for c in expr.expressions]

    mongo_filter = None
    if expr.args.get("where"):
        mongo_filter = _condition_to_filter(expr.args["where"].this, params_map)

    sort_items = None
    if expr.args.get("order"):
        sort_items = []
        for e in expr.args["order"].expressions:
            field = _field_name(e.this, params_map)
            direction = -1 if e.args.get("desc") else 1
            sort_items.append((field, direction))

    limit_val = None
    if expr.args.get("limit"):
        try:
            limit_val = int(expr.args["limit"].expression.name)
        except Exception:
            limit_val = int(expr.args["limit"].expression.this)
    skip_val = None
    if expr.args.get("offset"):
        try:
            skip_val = int(expr.args["offset"].expression.name)
        except Exception:
            skip_val = int(expr.args["offset"].expression.this)

    return QueryParts(
        operation="find",
        collection=collection,
        filter=mongo_filter or {},
        projection=projection,
        sort=sort_items,
        limit=limit_val,
        skip=skip_val,
    )


def _parse_select_like(expr: exp.Select, params_map: dict[str, Any]) -> QueryParts:
    if expr.args.get("joins"):
        return _parse_join_select(expr, params_map)
    if expr.args.get("group"):
        return _parse_group_select(expr, params_map)
    return _parse_select(expr, params_map)


def _parse_join_select(expr: exp.Select, params_map: dict[str, Any]) -> QueryParts:
    from_expr = expr.args.get("from_")
    joins = expr.args.get("joins") or []
    if not from_expr or not hasattr(from_expr, "this") or not from_expr.this.name or len(joins) < 1:
        raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN")
    base_collection = from_expr.this.name
    base_alias = from_expr.this.alias_or_name or base_collection

    alias_map = {base_alias: "", base_collection: ""}
    pipeline: list[dict] = []
    join_prefixes: list[tuple[str, str, str, str, exp.Join]] = []

    # prepare joins (up to 3)
    if len(joins) > 3:
        raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_DEPTH")
    for idx, join_expr in enumerate(joins):
        if join_expr.kind and join_expr.kind.upper() not in ("INNER", "LEFT"):
            raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN")
        on_expr = join_expr.args.get("on")
        if not on_expr or not isinstance(on_expr, exp.EQ):
            raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_ON")
        left_tbl, left_field = _column_table_field(on_expr.left)
        right_tbl, right_field = _column_table_field(on_expr.right)
        join_table = join_expr.this.this.name if hasattr(join_expr.this, "this") and hasattr(join_expr.this.this, "name") else None
        join_alias = join_expr.this.alias_or_name or join_table
        if not join_table or (left_tbl and left_tbl not in alias_map) or (
            right_tbl and right_tbl not in (join_table, join_alias)
        ):
            raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_TABLE")
        prefix = f"__join{idx}"
        alias_map[join_alias] = f"{prefix}."
        alias_map[join_table] = f"{prefix}."
        join_prefixes.append((prefix, join_table, left_field, right_field, join_expr))

    where_filter = None
    if expr.args.get("where"):
        where_filter = _condition_to_filter_alias(expr.args["where"].this, params_map, alias_map)

    for prefix, join_table, left_field, right_field, join_expr in join_prefixes:
        join_side = (join_expr.args.get("side") or "").upper()
        preserve_null = bool(join_side == "LEFT" or (join_expr.kind and join_expr.kind.upper() == "LEFT"))
        pipeline.append(
            {
                "$lookup": {
                    "from": join_table,
                    "localField": left_field,
                    "foreignField": right_field,
                    "as": prefix,
                }
            }
        )
        pipeline.append({"$unwind": {"path": f"${prefix}", "preserveNullAndEmptyArrays": preserve_null}})
        if where_filter:
            pipeline.append({"$match": where_filter})

    if expr.args.get("order"):
        sort_doc: dict[str, int] = {}
        for e in expr.args["order"].expressions:
            field = _field_name(e.this, params_map)
            direction = -1 if e.args.get("desc") else 1
            sort_doc[field] = direction
        if sort_doc:
            pipeline.append({"$sort": sort_doc})

    if expr.args.get("limit"):
        try:
            limit_val = int(expr.args["limit"].expression.name)
        except Exception:
            limit_val = int(expr.args["limit"].expression.this)
        pipeline.append({"$limit": limit_val})
    if expr.args.get("offset"):
        try:
            skip_val = int(expr.args["offset"].expression.name)
        except Exception:
            skip_val = int(expr.args["offset"].expression.this)
        pipeline.append({"$skip": skip_val})

    projection_paths: list[tuple[str, str]] | None = None
    if not expr.is_star:
        projection_paths = []
        for c in expr.expressions:
            if isinstance(c, exp.Column):
                tbl, fld = _column_table_field(c)
                if tbl and tbl not in alias_map:
                    raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_COLUMN")
                path = _field_with_alias(c, alias_map)
                out_name = c.alias_or_name or (f"{tbl}.{fld}" if tbl and tbl != base_collection else fld)
                projection_paths.append((path, out_name))
            else:
                raise_error("[mdb][E2]", "Unsupported SQL construct: JOIN_PROJECTION")
    return QueryParts(
        operation="aggregate",
        collection=base_collection,
        pipeline=pipeline,
        projection_paths=projection_paths,
    )


def _parse_group_select(expr: exp.Select, params_map: dict[str, Any]) -> QueryParts:
    table = expr.find(exp.Table)
    if not table or not table.name:
        raise_error("[mdb][E5]", "Failed to parse SQL")
    pipeline: list[dict] = []
    if expr.args.get("where"):
        where_filter = _condition_to_filter(expr.args["where"].this, params_map)
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
    projection_paths: list[tuple[str, str]] = []
    final_order: list[str] = []
    seen_outputs: list[str] = []
    for exp_item in expr.expressions:
        alias = exp_item.alias_or_name
        if alias in seen_outputs:
            continue
        seen_outputs.append(alias)
        final_order.append(alias)
        if isinstance(exp_item, exp.Column):
            col_name = _field_name(exp_item, params_map)
            agg_fields[alias] = {"$first": f"${col_name}"}
        elif isinstance(exp_item, exp.Count):
            agg_fields[alias] = {"$sum": 1}
        elif isinstance(exp_item, exp.Sum):
            col_name = _field_name(exp_item.this, params_map)
            agg_fields[alias] = {"$sum": f"${col_name}"}
        elif isinstance(exp_item, exp.Avg):
            col_name = _field_name(exp_item.this, params_map)
            agg_fields[alias] = {"$avg": f"${col_name}"}
        elif isinstance(exp_item, exp.Min):
            col_name = _field_name(exp_item.this, params_map)
            agg_fields[alias] = {"$min": f"${col_name}"}
        elif isinstance(exp_item, exp.Max):
            col_name = _field_name(exp_item.this, params_map)
            agg_fields[alias] = {"$max": f"${col_name}"}
        else:
            raise_error("[mdb][E2]", "Unsupported SQL construct: GROUP_SELECT")

    group_stage: dict[str, Any] = {"_id": group_id}
    group_stage.update(agg_fields)
    pipeline.append({"$group": group_stage})

    having_filter = None
    if expr.args.get("having"):
        alias_map = {k: "" for k in list(group_cols) + list(agg_fields.keys())}
        having_filter = _condition_to_filter_alias(expr.args["having"].this, params_map, alias_map)

    project_doc: dict[str, str] = {}
    for key in final_order:
        if key in group_cols:
            project_doc[key] = f"$_id.{key}"
        else:
            project_doc[key] = f"${key}"
        projection_paths.append((key, key))
    if having_filter:
        pipeline.append({"$match": having_filter})
    pipeline.append({"$project": project_doc})

    if expr.args.get("order"):
        sort_doc: dict[str, int] = {}
        for e in expr.args["order"].expressions:
            field = _field_name(e.this, params_map)
            direction = -1 if e.args.get("desc") else 1
            sort_doc[field] = direction
        if sort_doc:
            pipeline.append({"$sort": sort_doc})
    if expr.args.get("offset"):
        try:
            skip_val = int(expr.args["offset"].expression.name)
        except Exception:
            skip_val = int(expr.args["offset"].expression.this)
        pipeline.append({"$skip": skip_val})
    if expr.args.get("limit"):
        try:
            limit_val = int(expr.args["limit"].expression.name)
        except Exception:
            limit_val = int(expr.args["limit"].expression.this)
        pipeline.append({"$limit": limit_val})

    return QueryParts(
        operation="aggregate",
        collection=table.name,
        pipeline=pipeline,
        projection_paths=projection_paths,
    )


def _parse_insert(expr: exp.Insert, params_map: dict[str, Any]) -> QueryParts:
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
    values = [_literal_value(v, params_map) for v in row.expressions]
    if columns and len(columns) != len(values):
        raise_error("[mdb][E4]")
    doc = dict(zip(columns, values)) if columns else dict(enumerate(values))
    return QueryParts(
        operation="insert",
        collection=table_name,
        values=doc,
    )


def _parse_update(expr: exp.Update, params_map: dict[str, Any]) -> QueryParts:
    table = expr.this
    if not table or not table.name:
        raise_error("[mdb][E5]", "Failed to parse SQL")
    assignments = {}
    set_exp = expr.args.get("expressions") or []
    for assign in set_exp:
        if not isinstance(assign, exp.EQ):
            raise_error("[mdb][E5]")
        field = _field_name(assign.left, params_map)
        value = _literal_value(assign.right, params_map)
        assignments[field] = value
    where_clause = expr.args.get("where")
    if not where_clause:
        raise_error("[mdb][E3]")
    mongo_filter = _condition_to_filter(where_clause.this, params_map)
    return QueryParts(
        operation="update",
        collection=table.name,
        filter=mongo_filter,
        update={"$set": assignments},
    )


def _parse_delete(expr: exp.Delete, params_map: dict[str, Any]) -> QueryParts:
    table = expr.this if hasattr(expr, "this") else None
    if not table or not table.name:
        raise_error("[mdb][E5]", "Failed to parse SQL")
    where_clause = expr.args.get("where")
    if not where_clause:
        raise_error("[mdb][E3]")
    mongo_filter = _condition_to_filter(where_clause.this, params_map)
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
