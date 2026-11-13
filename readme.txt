测试：python3 -m unittest discover -s tests/index_vintages -p 'test_*.py'
构建：python3 -m compileall -q market_index_bureau tests/index_vintages
验收：python3 -m market_index_bureau.publisher.cli fixtures/demo.json --database /tmp/market_index_bureau-acceptance.sqlite3
所有输入均为本地合成数据，运行不需要网络。
