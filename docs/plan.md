# 進行用チェックリスト (拡張: JOIN/DDL/集計/SQLAlchemy)
テストはローカル同梱の MongoDB (`mongodb-3.6`) を起動して実通信で行い、完了した項目のみ `[x]` に更新する。依存追加・テスト準備も含め、すべてのチェックを網羅する。

- [x] 接続 API・例外クラスのスケルトンを実装する（F1, F5）
- [x] SQLGlot ベースのパーサー/翻訳ユーティリティを実装し、SELECT/DML を Mongo クエリへ変換する（F2, F3）
- [x] SQLGlot を依存に追加し、最小構文のユニットテストを用意する
- [x] パラメータバインドの検証と適用を実装する（F4）
- [x] 未対応構文/ガードのエラーを Error ID でマッピングする（F5）
- [x] ローカル mongodb-3.6 を使った CRUD の実通信テストを実行し、結果を確認する（必要環境変数: MONGODB_URI, MONGODB_DB）
- [x] DBAPI 互換の fetch 系挙動（rowcount/lastrowid/description 含む）をテストする
- [x] ログ出力の最小実装（DEBUG のみ）を行い、ログ内容を目視確認する
- [x] トランザクション対応可否を接続時または begin 時に判定し、非対応サーバーで [mdb][E6] が返ることをテストする（3.6 で確認）
- [x] JOIN 拡張（INNER/LEFT 等価、複合キー/2 段まで）を実装し、実通信テストを追加する（F8）
- [x] WHERE 拡張（OR/BETWEEN/LIKE）、LIMIT/OFFSET、GROUP BY + 集計（COUNT/SUM/AVG/MIN/MAX、HAVING は未対応）を実装し、実通信テストを追加する（F8）
- [x] DDL/インデックス（CREATE/DROP TABLE/INDEX、UNIQUE/複合、存在時は無視）の実装とテストを行う（F9）
- [x] SQLAlchemy 方言と DBAPI モジュール属性（apilevel/threadsafety/paramstyle=pyformat、スキーム `mongodb+dbapi://`）を実装し、SQLAlchemy からの CRUD/SELECT/JOIN/DDL を結合テストする（F10）
- [x] README に利用例と対応構文の制限を記載する（JOIN/集計/DDL/SQLAlchemy も反映）※最後に実施

## 品質ギャップ解消計画（MongoDB 3.6 前提）
- [x] JOIN 網羅テストを追加（複合キー、2 段 JOIN、LEFT JOIN + WHERE/ORDER/LIMIT、NULL ケース）
- [x] GROUP BY/集計の拡張テスト（複合 GROUP BY、複数集計列、ORDER + LIMIT/OFFSET の組み合わせ）
- [x] LIKE/BETWEEN/OR のネスト・エスケープ・境界値テスト（`%`/`_`/`\` 含む）
- [x] OFFSET/ORDER/LIMIT の組み合わせテストを拡充（カラム数/列順の確認含む）
- [x] DDL/INDEX の追加テスト（複合インデックス、UNIQUE 既存時の挙動、DROP INDEX 非存在時）
- [x] 型変換テスト（datetime の往復、ObjectId 文字列化確認、その他型の扱い方針を決めてテスト）
- [x] SQLAlchemy 方言の実装方針を固め、最小 CRUD/SELECT/JOIN/DDL の結合テストを追加

## 次フェーズ対応 TODO（優先順位順・トランザクション除外）
- [ ] SQLAlchemy Core 強化（Table/Column ベースの CRUD/DDL/Index）とテスト  
    - [ ] 方言で Table/Column を受け取り、`select()/insert()/update()/delete()` を Mongo クエリに変換する  
    - [ ] `metadata.create_all(engine)` でコレクション作成/インデックス作成をサポートする  
    - [ ] Core API 用の実通信テストを追加（CRUD/DDL/Index）
- [ ] ORM 最小 CRUD（単一 PK/単一テーブル相当）の実装とテスト  
    - [ ] ORM モデルの単一 PK を `_id` にマッピングし、add/get/select を通す  
    - [ ] relationship なしのケースで INSERT/SELECT/UPDATE/DELETE が動くことを確認する  
    - [ ] 実通信テストを追加（単一モデルの CRUD）
- [ ] async dialect 検討（Core ベースの CRUD を async で通す）  
    - [ ] async dialect の雛形を用意し、Mongo 5+ 環境で CRUD を確認する  
    - [ ] 実通信テスト（async/await）を追加
- [x] サブクエリ対応（WHERE IN/EXISTS、FROM サブクエリ）
- [x] UNION/UNION ALL をサポート
- [x] HAVING を GROUP BY 後に適用できるようにする
- [x] JOIN 拡張（非等価/多段、RIGHT/FULL OUTER を含むか判断し、必要なら対応）
- [ ] ウィンドウ関数（OVER/PARTITION BY/ORDER BY）を対応するか決めて実装（Mongo 5+ 前提）
- [x] ILIKE/正規表現リテラル、名前付きパラメータを対応（方針を決めて実装）
- [x] 型拡張（Decimal/UUID/タイムゾーン付き datetime など）の変換ルールとテストを追加

## ドキュメント整備計画
- [x] README/README_ja にユースケース（Core 基盤差し替え、レポートバッチ移行、ORM 小規模実験、async 方針）を追記
- [x] spec にウィンドウ関数の挙動（Mongo 5.0 未満は [mdb][E2]）と将来の async 方針を明記
- [x] architecture に Core/ORM/async の対応範囲と制約を追記（statement cache 無効、トランザクションは 3.6 で no-op）
- [x] concept にロードマップ（Core 強化→ORM 最小 CRUD→async 検討）を追加し、フェーズを整理
