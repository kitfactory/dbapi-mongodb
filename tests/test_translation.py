import os

import pytest

from mongo_dbapi.errors import MongoDbApiError
from mongo_dbapi.translation import parse_sql


def _raises(code: str, sql: str) -> None:
    with pytest.raises(MongoDbApiError) as exc:
        parse_sql(sql)
    assert code in str(exc.value)


def test_non_equi_join_supported():
    parts = parse_sql("SELECT u.id, o.id FROM users u JOIN orders o ON u.id > o.user_id")
    assert parts.operation == "aggregate"
    lookups = [stage.get("$lookup") for stage in (parts.pipeline or []) if isinstance(stage, dict) and "$lookup" in stage]
    assert lookups
    assert "pipeline" in lookups[0]


def test_right_outer_join_supported_single_eq():
    parts = parse_sql("SELECT u.id, o.id FROM users u RIGHT OUTER JOIN orders o ON u.id = o.user_id ORDER BY o.id")
    assert parts.operation == "aggregate"
    assert any("$lookup" in stage for stage in parts.pipeline or [])
    unwind = next((stage.get("$unwind") for stage in parts.pipeline or [] if isinstance(stage, dict) and "$unwind" in stage), None)
    assert isinstance(unwind, dict)
    assert unwind.get("preserveNullAndEmptyArrays") is True


def test_right_outer_join_non_equi_on_rejected_explicit():
    with pytest.raises(MongoDbApiError) as exc:
        parse_sql("SELECT u.id, o.id FROM users u RIGHT OUTER JOIN orders o ON u.id >= o.user_id")
    assert "[mdb][E2]" in str(exc.value)
    assert "RIGHT_JOIN_NON_EQ" in str(exc.value)


def test_full_outer_join_single_eq_supported():
    parts = parse_sql(
        "SELECT u.id AS uid, o.id AS oid "
        "FROM users u FULL OUTER JOIN orders o ON u.id = o.user_id "
        "ORDER BY oid"
    )
    assert parts.operation == "union_all"
    assert parts.union_parts and len(parts.union_parts) == 2


def test_full_outer_join_non_equi_on_rejected_explicit():
    with pytest.raises(MongoDbApiError) as exc:
        parse_sql("SELECT u.id, o.id FROM users u FULL OUTER JOIN orders o ON u.id >= o.user_id")
    assert "[mdb][E2]" in str(exc.value)
    assert "FULL_JOIN_NON_EQ" in str(exc.value)


def test_full_outer_join_chain_rejected_explicit():
    with pytest.raises(MongoDbApiError) as exc:
        parse_sql(
            "SELECT u.id, o.id, a.id "
            "FROM users u "
            "FULL OUTER JOIN orders o ON u.id = o.user_id "
            "JOIN addresses a ON o.id = a.order_id"
        )
    assert "[mdb][E2]" in str(exc.value)
    assert "FULL_JOIN_CHAIN" in str(exc.value)


def test_full_outer_join_composite_on_supported():
    parts = parse_sql(
        "SELECT u.id AS uid, o.id AS oid "
        "FROM users u FULL OUTER JOIN orders o ON u.id = o.user_id AND u.tenant_id = o.tenant_id "
        "ORDER BY uid, oid"
    )
    assert parts.operation == "union_all"
    assert parts.union_parts and len(parts.union_parts) == 2
    assert parts.sort == [("uid", 1), ("oid", 1)]


def test_union_distinct_supported():
    parts = parse_sql("SELECT id FROM users UNION SELECT id FROM users ORDER BY id")
    assert parts.operation == "union"


def test_window_rank_rejected():
    _raises("[mdb][E2]", "SELECT id, RANK() OVER (ORDER BY id) FROM users")


def test_correlated_subquery_supported_for_exists():
    parts = parse_sql("SELECT id FROM users u WHERE EXISTS (SELECT 1 FROM users x WHERE x.id = u.id) ORDER BY id")
    assert parts.operation == "aggregate"
    assert any("$lookup" in stage for stage in parts.pipeline or [])


def test_named_param_shortage_rejected():
    with pytest.raises(MongoDbApiError) as exc:
        parse_sql("SELECT * FROM users WHERE id = %(id)s", params={"other": 1})
    assert "[mdb][E4]" in str(exc.value)


def test_named_param_surplus_rejected():
    with pytest.raises(MongoDbApiError) as exc:
        parse_sql("SELECT * FROM users WHERE id = %(id)s", params={"id": 1, "extra": 2})
    assert "[mdb][E4]" in str(exc.value)


def test_unknown_statement_rejected():
    _raises("[mdb][E2]", "MERGE INTO users USING dual ON (1=1) WHEN MATCHED THEN UPDATE SET name = 'x'")


def test_window_row_number_without_partition_parses():
    parts = parse_sql("SELECT id, ROW_NUMBER() OVER (ORDER BY id) AS rn FROM users")
    assert parts.uses_window is True


def test_select_simple():
    parts = parse_sql("SELECT id, name FROM users WHERE id = %s", params=(1,))
    assert parts.operation == "find"
    assert parts.collection == "users"
    assert parts.filter == {"id": 1}
    assert parts.projection_paths == [("id", "id"), ("name", "name")]


def test_insert_simple():
    parts = parse_sql(
        "INSERT INTO users (id, name) VALUES (%(id)s, %(name)s)",
        params={"id": 1, "name": "Alice"},
    )
    assert parts.operation == "insert"
    assert parts.collection == "users"
    assert parts.values == {"id": 1, "name": "Alice"}


def test_update_with_where():
    parts = parse_sql(
        "UPDATE users SET name = %(name)s WHERE id = %(id)s",
        params={"id": 1, "name": "Bob"},
    )
    assert parts.operation == "update"
    assert parts.update == {"$set": {"name": "Bob"}}
    assert parts.filter == {"id": 1}


def test_delete_with_where():
    parts = parse_sql("DELETE FROM users WHERE id = %(id)s", params={"id": 1})
    assert parts.operation == "delete"
    assert parts.filter == {"id": 1}


def test_where_like_ilike_regex():
    parts_like = parse_sql("SELECT * FROM users WHERE name LIKE %(name)s", params={"name": "A%"})
    assert "$regex" in parts_like.filter.get("name", {})
    parts_ilike = parse_sql("SELECT * FROM users WHERE name ILIKE %(name)s", params={"name": "alice%"})
    assert "$regex" in parts_ilike.filter.get("name", {})
    parts_regex = parse_sql("SELECT * FROM users WHERE name REGEXP '/Al.*ce/'")
    assert "$regex" in parts_regex.filter.get("name", {})


def test_join_inner_and_left():
    parts_inner = parse_sql(
        "SELECT u.id, o.id as oid FROM users u JOIN orders o ON u.id = o.user_id"
    )
    assert parts_inner.operation == "aggregate"
    assert any("$lookup" in stage for stage in parts_inner.pipeline or [])
    assert ("__join0.id", "oid") in (parts_inner.projection_paths or [])
    parts_left = parse_sql(
        "SELECT u.id, o.id FROM users u LEFT JOIN orders o ON u.id = o.user_id"
    )
    assert any("$lookup" in stage for stage in parts_left.pipeline or [])


def test_group_by_and_having():
    parts = parse_sql("SELECT user_id, COUNT(*) AS cnt FROM orders GROUP BY user_id HAVING cnt > 1")
    assert parts.operation == "aggregate"
    assert any("$group" in stage for stage in parts.pipeline or [])
    assert any("$match" in stage for stage in parts.pipeline or [])


def test_sum_case_aggregate():
    parts = parse_sql(
        "SELECT user_id, SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END) AS done_count FROM tasks GROUP BY user_id"
    )
    assert parts.operation == "aggregate"
    cond_sum = None
    for stage in parts.pipeline or []:
        if "$group" in stage:
            cond_sum = stage["$group"].get("done_count")
            break
    assert cond_sum and "$cond" in cond_sum.get("$sum", {})


def test_having_sum_without_alias():
    parts = parse_sql("SELECT user_id, SUM(total) FROM orders GROUP BY user_id HAVING SUM(total) >= 100")
    assert parts.operation == "aggregate"
    assert any("$group" in stage for stage in parts.pipeline or [])
    assert any("$match" in stage for stage in parts.pipeline or [])


def test_having_with_aggregate_alias():
    parts = parse_sql("SELECT user_id, SUM(total) AS total_sum FROM orders GROUP BY user_id HAVING total_sum >= 100")
    assert parts.operation == "aggregate"
    assert any("$match" in stage for stage in parts.pipeline or [])


def test_union_all_parts():
    parts = parse_sql("SELECT id FROM users UNION ALL SELECT id FROM archived_users")
    assert parts.operation == "union_all"
    assert parts.union_parts and len(parts.union_parts) == 2


def test_from_subquery_inline_token():
    parts = parse_sql("SELECT * FROM (SELECT id, name FROM users WHERE id > 0) AS t")
    assert parts.operation == "from_subquery"
    assert parts.inline_token is not None
    assert parts.subqueries


def test_with_cte_select_supported():
    parts = parse_sql(
        "WITH active_users AS (SELECT id, tenant_id FROM users WHERE tenant_id = %s) "
        "SELECT id FROM active_users WHERE id >= %s ORDER BY id",
        params=(1, 10),
    )
    assert parts.operation == "from_subquery"
    assert parts.inline_token is not None
    assert parts.subqueries


def test_with_recursive_is_rejected():
    with pytest.raises(MongoDbApiError) as exc:
        parse_sql(
            "WITH RECURSIVE t AS (SELECT 1 AS id UNION ALL SELECT id + 1 FROM t WHERE id < 3) "
            "SELECT id FROM t"
        )
    assert "[mdb][E2]" in str(exc.value)
    assert "CTE_RECURSIVE" in str(exc.value)


def test_with_cte_union_all_supported():
    parts = parse_sql(
        "WITH tenant_users AS (SELECT id FROM users WHERE tenant_id = %s) "
        "SELECT id FROM tenant_users UNION ALL SELECT id FROM tenant_users ORDER BY id",
        params=(1,),
    )
    assert parts.operation == "union_all"
    assert parts.union_parts and len(parts.union_parts) == 2
    assert parts.subqueries


def test_scalar_subquery_comparison_registered_as_scalar():
    parts = parse_sql(
        "SELECT id FROM users WHERE score >= (SELECT AVG(score) FROM users WHERE tenant_id = %s)",
        params=(1,),
    )
    assert parts.operation == "find"
    assert parts.subqueries
    token, sub = next(iter(parts.subqueries.items()))
    assert token.startswith("__subquery_")
    assert sub.get("mode") == "scalar"


def test_union_all_three_way_supported():
    parts = parse_sql(
        "SELECT id FROM users "
        "UNION ALL SELECT id FROM archived_users "
        "UNION ALL SELECT id FROM users "
        "ORDER BY id"
    )
    assert parts.operation == "union_all"
    assert parts.union_parts and len(parts.union_parts) == 3


def test_union_mixed_is_rejected():
    with pytest.raises(MongoDbApiError) as exc:
        parse_sql(
            "SELECT id FROM users "
            "UNION ALL SELECT id FROM archived_users "
            "UNION SELECT id FROM users"
        )
    assert "[mdb][E2]" in str(exc.value)
    assert "UNION_MIXED" in str(exc.value)


def test_where_in_subquery_registered():
    parts = parse_sql("SELECT id FROM users WHERE id IN (SELECT user_id FROM orders)")
    assert parts.subqueries
    token, sub = next(iter(parts.subqueries.items()))
    assert token.startswith("__subquery_")
    assert sub.get("mode") in ("in", "values")


def test_window_row_number_with_partition_and_order():
    parts = parse_sql(
        "SELECT user_id, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY created_at) AS rn FROM events"
    )
    assert parts.uses_window is True
    assert parts.operation == "aggregate"


def test_window_rank_and_dense_rank_parse():
    parts_rank = parse_sql(
        "SELECT user_id, RANK() OVER (PARTITION BY user_id ORDER BY created_at) AS rnk FROM events"
    )
    assert parts_rank.uses_window is True
    assert any("$setWindowFields" in stage for stage in parts_rank.pipeline or [])
    parts_dense = parse_sql(
        "SELECT user_id, DENSE_RANK() OVER (ORDER BY created_at) AS drk FROM events"
    )
    assert parts_dense.uses_window is True
    assert any("$setWindowFields" in stage for stage in parts_dense.pipeline or [])

def test_param_shortage_rejected():
    with pytest.raises(MongoDbApiError) as exc:
        parse_sql("SELECT * FROM users WHERE id = %s", params=None)
    assert "[mdb][E4]" in str(exc.value)


def test_param_surplus_rejected():
    with pytest.raises(MongoDbApiError) as exc:
        parse_sql("SELECT * FROM users WHERE id = %s", params=(1, 2))
    assert "[mdb][E4]" in str(exc.value)


def test_acceptance_a1_detail_orders_for_user_translation():
    sql = (
        "SELECT o.id, o.total, o.created_at "
        "FROM orders o "
        "WHERE o.tenant_id = %s AND o.user_id = %s "
        "ORDER BY o.created_at DESC, o.id ASC LIMIT %s OFFSET %s"
    )
    parts = parse_sql(sql, params=(1, 10, 2, 0))
    assert parts.operation == "find"
    assert parts.filter is not None
    assert parts.sort is not None
    assert parts.skip == 0
    assert parts.limit == 2


def test_acceptance_a2_search_distinct_not_in_translation():
    sql = (
        "SELECT DISTINCT u.id "
        "FROM users u JOIN orders o ON u.id = o.user_id "
        "WHERE u.tenant_id = %s AND u.name ILIKE %s AND o.status NOT IN (%s, %s) "
        "ORDER BY u.id LIMIT %s OFFSET %s"
    )
    parts = parse_sql(sql, params=(1, "a%", "cancelled", "archived", 10, 0))
    assert parts.operation in ("find", "aggregate")


def test_acceptance_a3_group_having_order_alias_translation():
    sql = (
        "SELECT o.status, COUNT(*) AS cnt, SUM(o.total) AS total_sum "
        "FROM orders o "
        "WHERE o.tenant_id = %s AND o.created_at BETWEEN %s AND %s "
        "GROUP BY o.status HAVING cnt >= %s ORDER BY cnt DESC LIMIT %s"
    )
    parts = parse_sql(
        sql,
        params=(1, "2024-01-01", "2024-01-31", 2, 10),
    )
    assert parts.operation == "aggregate"
    assert any("$group" in stage for stage in parts.pipeline or [])
    assert any("$match" in stage for stage in parts.pipeline or [])
    assert any("$sort" in stage for stage in parts.pipeline or [])
    assert any("$limit" in stage for stage in parts.pipeline or [])


def test_acceptance_a4_search_ilike_contains_translation():
    sql = (
        "SELECT u.id "
        "FROM users u "
        "WHERE u.tenant_id = %s AND u.name ILIKE %s "
        "ORDER BY u.id LIMIT %s OFFSET %s"
    )
    parts = parse_sql(sql, params=(1, "%user1%", 10, 0))
    assert parts.operation == "find"
    assert parts.filter is not None
    # ILIKE should become regex with case-insensitive option / ILIKE は大文字小文字無視の正規表現になる
    and_terms = parts.filter.get("$and") if isinstance(parts.filter, dict) else None
    assert isinstance(and_terms, list)
    name_term = next((t for t in and_terms if isinstance(t, dict) and "name" in t), None)
    assert isinstance(name_term, dict)
    assert name_term["name"].get("$regex") is not None
    assert name_term["name"].get("$options") == "i"


def test_acceptance_a5_deep_offset_paging_translation():
    sql = (
        "SELECT o.id, o.created_at "
        "FROM orders o "
        "WHERE o.tenant_id = %s "
        "ORDER BY o.created_at DESC, o.id ASC LIMIT %s OFFSET %s"
    )
    parts = parse_sql(sql, params=(1, 50, 5000))
    assert parts.operation == "find"
    assert parts.sort is not None
    assert parts.skip == 5000
    assert parts.limit == 50


def test_acceptance_a11_exists_not_exists_translation():
    sql_exists = (
        "SELECT u.id "
        "FROM users u "
        "WHERE u.tenant_id = %s AND EXISTS (SELECT id FROM orders o WHERE o.tenant_id = %s AND o.status = %s LIMIT 1) "
        "ORDER BY u.id LIMIT %s"
    )
    parts_exists = parse_sql(sql_exists, params=(1, 1, "new", 10))
    assert parts_exists.operation == "find"
    assert parts_exists.filter is not None
    assert "$and" in parts_exists.filter
    assert any(isinstance(t, dict) and "$expr" in t for t in parts_exists.filter["$and"])

    sql_not_exists = (
        "SELECT u.id "
        "FROM users u "
        "WHERE u.tenant_id = %s AND NOT EXISTS (SELECT id FROM orders o WHERE o.tenant_id = %s AND o.status = %s LIMIT 1) "
        "ORDER BY u.id LIMIT %s"
    )
    parts_not_exists = parse_sql(sql_not_exists, params=(1, 1, "new", 10))
    assert parts_not_exists.operation == "find"
    assert parts_not_exists.filter is not None
    assert "$and" in parts_not_exists.filter
    expr_term = next((t for t in parts_not_exists.filter["$and"] if isinstance(t, dict) and "$expr" in t), None)
    assert isinstance(expr_term, dict)
    assert "$not" in expr_term["$expr"]


def test_correlated_exists_translation():
    sql = (
        "SELECT u.id "
        "FROM users u "
        "WHERE EXISTS (SELECT id FROM orders o WHERE o.user_id = u.id)"
    )
    parts = parse_sql(sql)
    assert parts.operation == "aggregate"
    assert any("$lookup" in stage for stage in parts.pipeline or [])
    assert any("$match" in stage for stage in parts.pipeline or [])


def test_correlated_exists_with_join_is_rejected_translation():
    sql = (
        "SELECT u.id "
        "FROM users u "
        "WHERE EXISTS ("
        "SELECT o.id FROM orders o JOIN users x ON o.user_id = x.id WHERE o.user_id = u.id"
        ")"
    )
    with pytest.raises(MongoDbApiError) as exc:
        parse_sql(sql)
    assert "[mdb][E2]" in str(exc.value)
    assert "CORRELATED_SUBQUERY" in str(exc.value)


def test_acceptance_a12_join_composite_on_translation():
    sql = (
        "SELECT o.id, o.created_at, u.name "
        "FROM orders o JOIN users u ON o.user_id = u.id AND o.tenant_id = u.tenant_id "
        "WHERE o.tenant_id = %s "
        "ORDER BY o.created_at DESC, o.id ASC LIMIT %s OFFSET %s"
    )
    parts = parse_sql(sql, params=(1, 10, 0))
    assert parts.operation == "aggregate"
    lookups = [st.get("$lookup") for st in (parts.pipeline or []) if isinstance(st, dict) and "$lookup" in st]
    assert lookups
    # Composite ON should use $lookup pipeline / 複合 ON は $lookup pipeline を使う
    assert "pipeline" in lookups[0]


def test_acceptance_a13_union_all_with_offset_translation():
    sql = (
        "SELECT id FROM users WHERE tenant_id = %s "
        "UNION ALL SELECT id FROM archived_users WHERE tenant_id = %s "
        "ORDER BY id LIMIT %s OFFSET %s"
    )
    parts = parse_sql(sql, params=(1, 1, 10, 5))
    assert parts.operation == "union_all"
    assert parts.sort == [("id", 1)]
    assert parts.limit == 10
    assert parts.skip == 5


def test_acceptance_a14_distinct_multi_columns_translation():
    sql = (
        "SELECT DISTINCT tenant_id, status "
        "FROM orders "
        "WHERE tenant_id IN (%s, %s) "
        "ORDER BY tenant_id, status LIMIT %s OFFSET %s"
    )
    parts = parse_sql(sql, params=(1, 2, 20, 0))
    assert parts.operation == "aggregate"
    assert parts.projection_paths == [("tenant_id", "tenant_id"), ("status", "status")]
    assert any("$group" in stage for stage in parts.pipeline or [])
    assert any("$project" in stage for stage in parts.pipeline or [])


def test_acceptance_a15_count_distinct_translation():
    sql_total = "SELECT COUNT(DISTINCT user_id) AS uniq_users FROM orders WHERE tenant_id = %s"
    parts_total = parse_sql(sql_total, params=(1,))
    assert parts_total.operation == "aggregate"
    assert any("$group" in stage for stage in parts_total.pipeline or [])
    assert any("$project" in stage for stage in parts_total.pipeline or [])

    sql_group = (
        "SELECT tenant_id, COUNT(DISTINCT user_id) AS uniq_users "
        "FROM orders GROUP BY tenant_id ORDER BY tenant_id LIMIT %s"
    )
    parts_group = parse_sql(sql_group, params=(10,))
    assert parts_group.operation == "aggregate"
    assert any("$group" in stage for stage in parts_group.pipeline or [])
    assert any("$project" in stage for stage in parts_group.pipeline or [])


def test_acceptance_a16_join_group_by_translation():
    sql = (
        "SELECT u.id, COUNT(o.id) AS order_cnt "
        "FROM users u LEFT JOIN orders o ON u.id = o.user_id AND u.tenant_id = o.tenant_id "
        "WHERE u.tenant_id = %s "
        "GROUP BY u.id HAVING order_cnt >= %s ORDER BY order_cnt DESC, u.id ASC LIMIT %s OFFSET %s"
    )
    parts = parse_sql(sql, params=(1, 1, 10, 0))
    assert parts.operation == "aggregate"
    assert parts.projection_paths == [("id", "id"), ("order_cnt", "order_cnt")]
    assert any("$lookup" in stage for stage in parts.pipeline or [])
    assert any("$group" in stage for stage in parts.pipeline or [])
    assert any("$project" in stage for stage in parts.pipeline or [])
    assert any("$match" in stage for stage in parts.pipeline or [])
    assert any("$sort" in stage for stage in parts.pipeline or [])
    optimize_on = os.environ.get("MONGO_DBAPI_OPTIMIZE", "1").lower() not in ("0", "false", "no")
    if optimize_on:
        assert any("$addFields" in stage for stage in parts.pipeline or [])
        assert not any("$unwind" in stage for stage in parts.pipeline or [])


def test_acceptance_a17_correlated_exists_translation():
    sql = (
        "SELECT u.id "
        "FROM users u "
        "WHERE EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id AND o.tenant_id = u.tenant_id) "
        "ORDER BY u.id LIMIT %s"
    )
    parts = parse_sql(sql, params=(20,))
    assert parts.operation == "aggregate"
    assert any("$lookup" in stage for stage in parts.pipeline or [])
    assert any("$match" in stage for stage in parts.pipeline or [])


def test_acceptance_a18_union_distinct_translation():
    sql = (
        "SELECT tenant_id FROM users WHERE tenant_id IN (%s, %s) "
        "UNION "
        "SELECT tenant_id FROM archived_users WHERE tenant_id IN (%s, %s) "
        "ORDER BY tenant_id LIMIT %s OFFSET %s"
    )
    parts = parse_sql(sql, params=(1, 2, 1, 2, 10, 0))
    assert parts.operation == "union"
    assert parts.sort == [("tenant_id", 1)]
    assert parts.limit == 10
    assert parts.skip == 0


def test_acceptance_a19_non_equi_join_translation():
    sql = (
        "SELECT u.id, o.id "
        "FROM users u JOIN orders o ON u.id >= o.user_id "
        "WHERE u.tenant_id = %s "
        "ORDER BY u.id, o.id LIMIT %s OFFSET %s"
    )
    parts = parse_sql(sql, params=(1, 20, 0))
    assert parts.operation == "aggregate"
    lookups = [stage.get("$lookup") for stage in (parts.pipeline or []) if isinstance(stage, dict) and "$lookup" in stage]
    assert lookups
    assert "pipeline" in lookups[0]


def test_acceptance_a20_with_cte_translation():
    sql = (
        "WITH active_users AS ("
        "SELECT id, tenant_id FROM users WHERE tenant_id = %s"
        ") "
        "SELECT id FROM active_users WHERE id >= %s ORDER BY id LIMIT %s OFFSET %s"
    )
    parts = parse_sql(sql, params=(1, 2, 10, 0))
    assert parts.operation == "from_subquery"
    assert parts.inline_token is not None
    assert parts.subqueries
    assert parts.limit == 10
    assert parts.skip == 0


def test_acceptance_a21_scalar_subquery_translation():
    sql = (
        "SELECT id "
        "FROM users "
        "WHERE tenant_id = %s AND score >= (SELECT AVG(score) FROM users WHERE tenant_id = %s) "
        "ORDER BY id LIMIT %s"
    )
    parts = parse_sql(sql, params=(1, 1, 20))
    assert parts.operation == "find"
    assert parts.filter is not None
    assert parts.subqueries
    assert parts.limit == 20


def test_acceptance_a22_union_all_three_way_translation():
    sql = (
        "SELECT id FROM users WHERE tenant_id = %s "
        "UNION ALL SELECT id FROM archived_users WHERE tenant_id = %s "
        "UNION ALL SELECT id FROM users WHERE tenant_id = %s "
        "ORDER BY id LIMIT %s OFFSET %s"
    )
    parts = parse_sql(sql, params=(1, 1, 2, 20, 0))
    assert parts.operation == "union_all"
    assert parts.union_parts and len(parts.union_parts) == 3
    assert parts.sort == [("id", 1)]
    assert parts.limit == 20
    assert parts.skip == 0


def test_acceptance_a23_right_outer_join_translation():
    sql = (
        "SELECT u.id, o.id "
        "FROM users u RIGHT OUTER JOIN orders o ON u.id = o.user_id "
        "ORDER BY o.id LIMIT %s OFFSET %s"
    )
    parts = parse_sql(sql, params=(20, 0))
    assert parts.operation == "aggregate"
    lookups = [stage.get("$lookup") for stage in (parts.pipeline or []) if isinstance(stage, dict) and "$lookup" in stage]
    assert lookups
    unwind = next((stage.get("$unwind") for stage in parts.pipeline or [] if isinstance(stage, dict) and "$unwind" in stage), None)
    assert isinstance(unwind, dict)
    assert unwind.get("preserveNullAndEmptyArrays") is True


def test_acceptance_a24_right_outer_join_composite_on_translation():
    sql = (
        "SELECT u.id, o.id "
        "FROM users u RIGHT OUTER JOIN orders o ON u.id = o.user_id AND u.tenant_id = o.tenant_id "
        "WHERE o.tenant_id = %s "
        "ORDER BY o.id LIMIT %s OFFSET %s"
    )
    parts = parse_sql(sql, params=(1, 20, 0))
    assert parts.operation == "aggregate"
    lookups = [stage.get("$lookup") for stage in (parts.pipeline or []) if isinstance(stage, dict) and "$lookup" in stage]
    assert lookups
    assert "pipeline" in lookups[0]
    unwind = next((stage.get("$unwind") for stage in parts.pipeline or [] if isinstance(stage, dict) and "$unwind" in stage), None)
    assert isinstance(unwind, dict)
    assert unwind.get("preserveNullAndEmptyArrays") is True


def test_acceptance_a25_full_outer_join_translation():
    sql = (
        "SELECT u.id AS uid, o.id AS oid "
        "FROM users u FULL OUTER JOIN orders o ON u.id = o.user_id "
        "ORDER BY oid LIMIT %s OFFSET %s"
    )
    parts = parse_sql(sql, params=(20, 0))
    assert parts.operation == "union_all"
    assert parts.union_parts and len(parts.union_parts) == 2
    assert parts.sort == [("oid", 1)]
    assert parts.limit == 20
    assert parts.skip == 0


def test_acceptance_a26_full_outer_join_composite_translation():
    sql = (
        "SELECT u.id AS uid, o.id AS oid "
        "FROM users u FULL OUTER JOIN orders o ON u.id = o.user_id AND u.tenant_id = o.tenant_id "
        "ORDER BY uid, oid LIMIT %s OFFSET %s"
    )
    parts = parse_sql(sql, params=(20, 0))
    assert parts.operation == "union_all"
    assert parts.union_parts and len(parts.union_parts) == 2
    assert parts.sort == [("uid", 1), ("oid", 1)]
    assert parts.limit == 20
    assert parts.skip == 0


def test_acceptance_a27_full_outer_join_group_by_translation():
    sql = (
        "SELECT COALESCE(u.tenant_id, o.tenant_id) AS tenant_id, COUNT(*) AS cnt "
        "FROM users u FULL OUTER JOIN orders o ON u.id = o.user_id "
        "GROUP BY COALESCE(u.tenant_id, o.tenant_id) "
        "ORDER BY tenant_id LIMIT %s"
    )
    parts = parse_sql(sql, params=(20,))
    assert parts.operation == "aggregate"
    assert parts.pipeline
    assert any("$unionWith" in stage for stage in parts.pipeline)
    assert any("$group" in stage for stage in parts.pipeline)
    assert parts.projection_paths == [("tenant_id", "tenant_id"), ("cnt", "cnt")]


def test_acceptance_a27_full_outer_join_group_by_having_translation():
    sql = (
        "SELECT COALESCE(u.tenant_id, o.tenant_id) AS tenant_id, COUNT(*) AS cnt "
        "FROM users u FULL OUTER JOIN orders o ON u.id = o.user_id "
        "GROUP BY COALESCE(u.tenant_id, o.tenant_id) "
        "HAVING cnt >= %s "
        "ORDER BY tenant_id LIMIT %s"
    )
    parts = parse_sql(sql, params=(1, 20))
    assert parts.operation == "aggregate"
    assert parts.pipeline
    assert any("$group" in stage for stage in parts.pipeline)
    assert any("$match" in stage for stage in parts.pipeline)
