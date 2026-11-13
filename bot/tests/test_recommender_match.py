from server_access_bot.approvers import match_recommenders

LABELS = {
    1001: "Иван Петров (@ivan_petrov)",
    1002: "Ольга Смирнова (@olga_sm)",
    7: "Сидоров Павел (@pavel_sid)",
}


def test_legacy_free_text_recommender_is_matched_by_name_or_nick():
    assert match_recommenders("Иван Петров", LABELS) == (1001,)
    assert match_recommenders("Петров Иван", LABELS) == (1001,)
    assert match_recommenders("@ivan_petrov", LABELS) == (1001,)
    assert match_recommenders("Сидоров Павел (@pavel_sid)", LABELS) == (7,)


def test_unknown_or_partial_names_match_nobody():
    assert match_recommenders("Иван", LABELS) == ()
    assert match_recommenders("Пётр Кузнецов", LABELS) == ()
    assert match_recommenders("", LABELS) == ()
