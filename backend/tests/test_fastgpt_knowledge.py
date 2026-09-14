from app.services.fastgpt_knowledge import (
    format_fastgpt_knowledge_for_prompt,
    is_fastgpt_knowledge_enabled,
    retrieve_fastgpt_knowledge,
)


def test_fastgpt_is_disabled_without_private_key(monkeypatch) -> None:
    monkeypatch.setenv("FASTGPT_BASE_URL", "https://fastgpt.example/api")
    monkeypatch.setenv("FASTGPT_LEGAL_DATASET_ID", "legal")
    monkeypatch.setenv("FASTGPT_LAWS_DATASET_ID", "laws")
    monkeypatch.delenv("FASTGPT_API_KEY", raising=False)

    assert is_fastgpt_knowledge_enabled() is False
    assert retrieve_fastgpt_knowledge("合同验收") == []


def test_fastgpt_context_labels_dataset_and_source() -> None:
    context = format_fastgpt_knowledge_for_prompt(
        [{"dataset_name": "法规法条知识库", "title": "民法典", "content": "当事人应当按照约定履行义务。"}]
    )

    assert "法规法条知识库" in context
    assert "民法典" in context
