from agent import classifier


def test_classification_has_strict_gateway_schema():
    schema = classifier.Classification.model_json_schema()

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
