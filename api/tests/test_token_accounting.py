from vibecanvas_api.agents.token_accounting import count_tokens


def test_unknown_model_uses_stable_approximation() -> None:
    assert count_tokens("x" * 400, "unknown:model") == 100


def test_empty_content_is_zero() -> None:
    assert count_tokens("", "unknown:model") == 0


def test_token_count_is_monotonic() -> None:
    assert count_tokens("a" * 40, "unknown:model") < count_tokens(
        "a" * 4000,
        "unknown:model",
    )
