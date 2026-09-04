import copy

import pytest

from scripts import backfill_existing_chats


def test_select_owner_uses_common_chat_owner():
    owner = backfill_existing_chats.select_owner(
        [
            {"id": "user_other", "name": "Other"},
            {"id": "user_1", "name": "Ada", "email": "ada@example.com"},
        ],
        {"user_1"},
    )

    assert owner == backfill_existing_chats.Owner("user_1", "Ada")


def test_select_owner_uses_sole_user_for_unowned_chats():
    owner = backfill_existing_chats.select_owner(
        [{"id": "user_1", "username": "ada"}], set()
    )

    assert owner == backfill_existing_chats.Owner("user_1", "ada")


@pytest.mark.parametrize(
    ("users", "owners", "message"),
    [
        (
            [{"id": "user_1", "name": "Ada"}, {"id": "user_2", "name": "Grace"}],
            set(),
            "owner is ambiguous",
        ),
        (
            [{"id": "user_1", "name": "Ada"}, {"id": "user_2", "name": "Grace"}],
            {"user_1", "user_2"},
            "multiple owners",
        ),
        ([{"id": "user_1", "name": "Ada"}], {"missing"}, "missing"),
    ],
)
def test_select_owner_rejects_ambiguity(users, owners, message):
    with pytest.raises(ValueError, match=message):
        backfill_existing_chats.select_owner(users, owners)


@pytest.mark.parametrize(
    "fragment",
    ["'s cron jobs work", "wants to rewire", "fixes api2", "reviews the pr"],
)
def test_validate_fragment_accepts_natural_lowercase_suffix(fragment):
    backfill_existing_chats.validate_fragment(fragment)


@pytest.mark.parametrize(
    "fragment",
    ["", "Wants to rewire", "wants  two spaces", "wants punctuation!", "x" * 21],
)
def test_validate_fragment_rejects_invalid_suffix(fragment):
    with pytest.raises(ValueError):
        backfill_existing_chats.validate_fragment(fragment)


def test_validate_plan_requires_one_owner_and_valid_fragments():
    plan = {
        "version": 1,
        "owner": {"id": "user_1", "display_name": "Ada"},
        "chat_count": 1,
        "changes": [
            {
                "id": "chat_1",
                "before": {
                    "user_id": None,
                    "author_display_name": None,
                    "topic": "Old topic",
                },
                "after": {
                    "user_id": "user_1",
                    "author_display_name": "Ada",
                    "topic": "wants to rewire",
                },
            }
        ],
    }

    backfill_existing_chats.validate_plan(plan)
    invalid = copy.deepcopy(plan)
    invalid["changes"][0]["after"]["topic"] = "Too Long And Uppercase"
    with pytest.raises(ValueError):
        backfill_existing_chats.validate_plan(invalid)
