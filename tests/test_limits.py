from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from utils.limits import should_block_issue


def test_should_not_block_when_under_limit():
    users = [
        {"username": "alice", "active": True},
        {"username": "bob", "active": False},
    ]

    assert should_block_issue(users, "charlie", 5) is False


def test_should_block_when_limit_reached_for_new_user():
    users = [
        {"username": "alice", "active": True},
        {"username": "bob", "active": True},
    ]

    assert should_block_issue(users, "charlie", 2) is True


def test_should_not_block_existing_user_even_if_limit_reached():
    users = [
        {"username": "alice", "active": True},
        {"username": "bob", "active": True},
    ]

    assert should_block_issue(users, "bob", 2) is False


def test_should_ignore_inactive_users():
    users = [
        {"username": "alice", "active": True},
        {"username": "bob", "active": False},
    ]

    assert should_block_issue(users, "charlie", 1) is True


def test_should_handle_usernames_with_at_and_case():
    users = [
        {"username": "@Alice", "active": True},
        {"username": "Bob", "active": True},
    ]

    assert should_block_issue(users, "@ALICE", 2) is False
