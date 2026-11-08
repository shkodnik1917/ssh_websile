import asyncio
import sqlite3
from types import SimpleNamespace

from server_access_bot.app import deliver_request_reminders
from server_access_bot.domain import Application
from server_access_bot.storage import Storage

ADMIN = 999
LEAD = 555


def server_application(servers: tuple[str, ...] = ("brain_lab",)) -> Application:
    return Application(
        telegram_user_id=101,
        telegram_username="alice_tg",
        full_name="Alice Example",
        server_username="alice",
        access_term="бессрочно",
        recommender="Lead Example",
        activity="training",
        servers=servers,
    )


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, dict]] = []

        self.retired: list[tuple[int, int]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=1000 + len(self.messages))

    async def edit_message_reply_markup(self, *, chat_id, message_id, reply_markup):
        assert reply_markup is None
        self.retired.append((chat_id, message_id))


def remind(bot, storage, now):
    return asyncio.run(
        deliver_request_reminders(bot, storage, frozenset({ADMIN}), now=now)
    )


def test_server_request_reminds_original_recipients_daily_until_resolved(
    tmp_path,
) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    storage.save_pending_request("r1", server_application(), remind_at=100)
    storage.save_request_message("r1", ADMIN, 1)
    storage.save_request_message("r1", LEAD, 2)
    bot = FakeBot()

    assert remind(bot, storage, 99) == ()
    assert remind(bot, storage, 100) == ("r1",)
    assert [chat for chat, _, _ in bot.messages] == [ADMIN, LEAD]
    assert "ждёт решения" in bot.messages[-1][1]
    keyboard = bot.messages[-1][2]["reply_markup"].inline_keyboard[0]
    assert keyboard[0].callback_data == "admin:approve:r1"
    assert keyboard[1].callback_data == "admin:reject:r1"
    # one live copy per person: the old buttons are retired, the new copy is
    # registered so it gets retired once someone handles the request
    assert sorted(bot.retired) == sorted([(ADMIN, 1), (LEAD, 2)])
    assert sorted(storage.request_messages("r1")) == sorted([(ADMIN, 1001), (LEAD, 1002)])

    assert remind(bot, storage, 100 + 86399) == ()
    assert remind(bot, storage, 100 + 86400) == ("r1",)

    assert storage.claim_pending_request("r1") is not None
    storage.resolve_pending_request("r1", "approved")
    assert remind(bot, storage, 100 + 2 * 86400) == ()


def test_alibaba_request_is_left_to_the_alibaba_reminder(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    storage.save_pending_request(
        "a1", server_application(("alibaba_ascend",)), remind_at=100
    )
    bot = FakeBot()

    assert remind(bot, storage, 10**10) == ()
    assert bot.messages == []


def test_request_saved_before_reminders_is_reminded_once_a_day_old(
    tmp_path,
) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    storage.save_pending_request("old", server_application())  # no remind_at
    created = 1_767_225_600  # 2026-01-01 00:00:00 UTC
    with sqlite3.connect(tmp_path / "bot.sqlite3") as db:
        db.execute(
            "UPDATE pending_requests SET created_at = '2026-01-01 00:00:00' "
            "WHERE request_id = 'old'"
        )
    bot = FakeBot()

    assert remind(bot, storage, created + 86399) == ()
    assert remind(bot, storage, created + 86400) == ("old",)
    # from now on it follows the normal daily schedule
    assert remind(bot, storage, created + 86400 + 60) == ()
    assert remind(bot, storage, created + 2 * 86400) == ("old",)


def test_failed_delivery_releases_the_reminder_for_a_retry(tmp_path) -> None:
    storage = Storage(tmp_path / "bot.sqlite3", b"x" * 32)
    storage.save_pending_request("r1", server_application(), remind_at=100)

    class FailingBot:
        async def send_message(self, chat_id, text, **kwargs):
            raise RuntimeError("telegram down")

    assert remind(FailingBot(), storage, 100) == ()
    bot = FakeBot()
    assert remind(bot, storage, 101) == ("r1",)
