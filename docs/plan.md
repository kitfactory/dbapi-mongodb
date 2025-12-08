# 拡張機能対応チェックリスト（P1→P6）
旧版の詳細タスクは `docs/plan1.bck` に退避済み。拡張機能（P1: Core 強化 → P2: ORM 最小 CRUD → P3: async dialect → P4: Mongo 5+ ウィンドウ関数 → P5: JOIN/CASE/HAVING alias 強化 → P6: ウィンドウ関数拡張）を進めるための作業計画。

## 共通準備
- [x] 4.4 レプリカセットを起動（`PORT=27019 ./start4xdb.sh`）し、`MONGODB_URI`/`MONGODB_DB` を環境変数で指定
- [x] テスト環境の LD_LIBRARY_PATH（`mongodb-4.4/libssl1.1/usr/lib/x86_64-linux-gnu`）を確認

## P1: SQLAlchemy Core 強化（Table/Column CRUD/DDL/Index）
- [x] Core 向け CRUD/DDL/Index の翻訳パスを実装（Table/Column ベース）
- [x] サブクエリ（WHERE IN/EXISTS）を先行実行し結果を置換する実通信テストを追加
- [x] FROM サブクエリを先行実行する経路と実通信テストを追加
- [x] UNION ALL、HAVING、等価 JOIN 多段（最大 3 段）の実通信テストを追加
- [x] ILIKE/正規表現リテラル、名前付きパラメータ（dict）、型拡張（Decimal/UUID/tz datetime/Binary）の実装とテスト
- [x] SQLAlchemy Core 経由の CRUD/DDL/Index 結合テストを 4.4 環境で通す

## P2: ORM 最小 CRUD
- [x] 単一テーブル相当で PK を `_id` にマッピングし、INSERT/SELECT/UPDATE/DELETE が動く ORM 経路を実装
- [x] ORM 経由の実通信テスト（リレーションなし）を追加

## P3: async dialect（Core CRUD 相当）
- [x] Core CRUD/DDL/Index を async でラップする dialect を設計・実装（現行 sync をスレッドプールラップで提供し、将来 motor 等のネイティブ async を検討）
- [x] 非同期実通信テスト（CRUD/DDL/Index）を 4.4 環境で追加（トランザクションは 4.x のみ有効、3.6 は no-op と明記）
- [x] README に async の対応範囲/注意点（スレッドプールラップ・トランザクション期待値・保証レベル）を追記し、FastAPI サンプルを掲載

## P4: Mongo 5+ ウィンドウ関数（低優先度）
- [x] Mongo 5+ 環境で `ROW_NUMBER() OVER (PARTITION BY ... ORDER BY ...)` を `$setWindowFields` に変換する経路を実装
- [x] Mongo 5 未満では `[mdb][E2]` を返すことをテスト
- [x] MongoDB 7.x を `mongodb-7.x` に配置し、`start7xdb.sh`（レプリカセット・libssl 対応）で起動する
- [x] PORT=27029 などで 7.x を起動し、ウィンドウ関数テスト（ROW_NUMBER など）を追加して 7.x で成功、5 未満では `[mdb][E2]` を確認する
- [x] README/spec に「ウィンドウ関数は MongoDB 5.x+ 対応、7.x で動作確認済み」を明記する

## P5: JOIN/CASE/HAVING alias 強化
- [x] JOIN 投影を解禁する（JOIN_PROJECTION の制限を緩和し、JOIN 先列を別名含めて投影可能にする）。安全性のため制限事項を明文化。
- [x] JOIN + WHERE/HAVING で alias 解決を強化する（JOIN したテーブルの別名をフィルタ/集約で扱えるようにする）。
- [x] CASE を含む単純な集計（`SUM(CASE WHEN status='done' THEN 1 ELSE 0 END)` など）を `$cond` でサポートする。
- [ ] HAVING で集計 alias を解決する（例: `HAVING SUM(total) >= 100` を集計結果に対して評価）。
- [ ] 仕様/spec/README に対応範囲と制限（複雑な CASE/ネストは対象外など）を追記し、テストを追加する。

## P6: ウィンドウ関数拡張（検討）
- [ ] ROW_NUMBER 以外の基本ウィンドウ関数（`RANK/DENSE_RANK` など）について、MongoDB 5+ 前提で実装可否を調査・方針化する。
- [ ] 実装する場合は `$setWindowFields` で変換し、非対応の場合は `[mdb][E2]` で明示する。

## ドキュメント/仕様の更新
- [x] 実装に合わせて `docs/spec.md` / `docs/architecture.md` / `README*` を更新
- [x] 新規エラーメッセージが増える場合は Error ID を追記しテストで固定（今回は追加なしを確認）
