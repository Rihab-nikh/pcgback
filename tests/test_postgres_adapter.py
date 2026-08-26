from app.core.db import _postgres_sql, _split_sql_script


def test_qmark_and_literal_percent_translation():
    sql = _postgres_sql("SELECT * FROM t WHERE id=? AND account LIKE '2%'")
    assert "id=%s" in sql
    assert "LIKE '2%%'" in sql


def test_null_safe_is_translation():
    sql = _postgres_sql("SELECT * FROM t WHERE client_id IS ? AND month=?")
    assert "client_id IS NOT DISTINCT FROM %s" in sql
    assert "month=%s" in sql


def test_json_extract_translation():
    sql = _postgres_sql("SELECT json_extract(response_json, '$.step1_identification.due_date') AS due FROM invoices WHERE id=?")
    assert "jsonb_extract_path_text((response_json)::jsonb, 'step1_identification', 'due_date')" in sql
    assert "id=%s" in sql


def test_insert_or_ignore_translation():
    sql = _postgres_sql("INSERT OR IGNORE INTO x(id,name) VALUES(?,?)")
    assert sql.startswith("INSERT INTO x")
    assert sql.endswith("ON CONFLICT DO NOTHING")
    assert "VALUES(%s,%s)" in sql


def test_schema_splitter_ignores_semicolons_in_comments_and_literals():
    script = "-- comment; still comment\nCREATE TABLE a(x TEXT DEFAULT ';');\n/* x;y */ CREATE TABLE b(y INTEGER);"
    statements = _split_sql_script(script)
    assert len(statements) == 2
    assert statements[0].startswith("CREATE TABLE a")
    assert statements[1].startswith("CREATE TABLE b")
