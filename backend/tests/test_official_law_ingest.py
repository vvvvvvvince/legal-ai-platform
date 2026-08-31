from scripts import ingest_official_laws


def test_law_topic_mapping_is_conservative() -> None:
    assert "付款与发票" in ingest_official_laws._risk_topics("中华人民共和国民法典")
    assert ingest_official_laws._risk_topics("无关文件") == []


def test_official_records_have_source_metadata(tmp_path) -> None:
    source = tmp_path / "中华人民共和国民法典.html"
    source.write_text(
        """<html><head>
        <meta name="SiteDomain" content="www.samr.gov.cn">
        <meta name="ArticleTitle" content="中华人民共和国民法典">
        <meta name="Url" content="https://www.samr.gov.cn/test-law">
        </head><body>第一条 为了保护民事主体的合法权益，制定本法。</body></html>""",
        encoding="utf-8",
    )
    records = ingest_official_laws._official_records(tmp_path, 6000)
    assert records
    assert all(record["official_url"] or record["source_domain"] for record in records)
