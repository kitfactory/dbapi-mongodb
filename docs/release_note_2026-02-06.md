# リリースノート（2026-02-06 / v0.2.0 / Join-first）

## 概要
- ライブラリバージョンを `0.2.0` に更新しました。
- リリース方針を **Join-first（MongoDB 4.4 ターゲット）** とし、JOIN 系の実運用互換を正式保証します。
- 非対応構文は `"[mdb][E2] Unsupported SQL construct: <keyword>"` で明示エラーを返します。

## v0.2.0 の主な変更
- RIGHT OUTER JOIN をサポート（単一 JOIN・等価 ON）。
- FULL OUTER JOIN をサポート（単一 JOIN・等価 ON（単一/複合））。
- FULL OUTER JOIN + 集計の A27 パターンをサポート（`COALESCE(...) + COUNT(*)`）。
- FULL/RIGHT の `ORDER BY/LIMIT/OFFSET` を含む経路で、`$unionWith` を使ったサーバー側連結を適用。
- 非対応組み合わせを明示エラー化:
  - `RIGHT_JOIN_NON_EQ`
  - `FULL_JOIN_NON_EQ`
  - `RIGHT_JOIN_CHAIN`
  - `FULL_JOIN_CHAIN`

## 正式保証（Join-first）
- INNER/LEFT JOIN（最大 3 段、複合 ON、非等価 ON）
- RIGHT/FULL OUTER JOIN（単一 JOIN・等価 ON 制約）
- JOIN 後の `ORDER BY` / `LIMIT` / `OFFSET`
- JOIN + `GROUP BY` / `HAVING`（対応形）

## 併用可能（best effort）
- `UNION ALL` / `UNION`（3 項以上の多段連結）
- `WITH`（非再帰 CTE）
- 比較式の非相関スカラサブクエリ
- 相関 `EXISTS/NOT EXISTS`（単一サブクエリ・単一テーブル）
- MongoDB 5.x+ のウィンドウ関数（`ROW_NUMBER`/`RANK`/`DENSE_RANK`）

## 未対応（future）
- `WITH RECURSIVE`
- RIGHT/FULL OUTER + 非等価 ON
- RIGHT/FULL JOIN 連鎖
- FULL OUTER 集計の複雑形（A27 パターン以外）
- 複雑な相関サブクエリ（複数テーブル・多段ネスト・相関列投影）
- `UNION` と `UNION ALL` の混在連結
- ORM リレーション

## ベンチマーク（MongoDB 4.4 / users=10000, orders=50000, repeat=3）
- A23（RIGHT OUTER JOIN）: `81.2ms` → `74.9ms`
- A24（RIGHT OUTER JOIN 複合ON）: `68.2ms` → `67.2ms`
- A25（FULL OUTER JOIN）: `5018.6ms` → `3322.4ms`
- A26（FULL OUTER JOIN 複合ON）: `6180.2ms` → `5366.2ms`
- A27（FULL OUTER JOIN + GROUP BY）: `3067.3ms` → `3093.1ms`

## 検証結果
- `tests/test_translation.py -q`: `62 passed`
- `tests/test_dbapi.py -q`（MongoDB 4.4, `127.0.0.1:27019`）: `77 passed`

## ドキュメント更新
- `README.md` / `README_ja.md` を v0.2.0 のサポート範囲に同期。
- `docs/concept.md` / `docs/spec.md` / `docs/architecture.md` / `docs/plan.md` を JOIN 実装状態に同期。
