"""Season/episode guessing, on file name + caption pairs taken from real channels."""
import pytest

from app.parse import classify, guess

CASES = [
    # caption pair wins; the trailing "_1" in the name is an upload counter, not the episode
    ("Campfire_Cooking_In_Another_World_With_My_Absurd_Skill_05_1.m4v", "בישול מודרני - עונה 1 פרק 5", (1, 5)),
    ("Subsplease_Tondemo_Skill_De_Isekai_Hourou_Meshi_03_1080P_E6d3efbc.m4v", "בישול מודרני - עונה 1 פרק 3", (1, 3)),
    # the caption teases "season 2"; the file name's pair is the real season
    ("AnimeIL_TV_עולה_ברמות_לבדי_עונה_1_פרק_12.mp4",
     "עולה ברמות לבדי / סולו לבלינג פרק 12 - אחרון לעונה! (ממליץ לכם לפרגן אם בא לכם עונה 2)", (1, 12)),
    # upload artifacts in the name ("פרק_72" is episode 7); the caption's episode wins
    ("Animeil_Tv_עולה_ברמות_לבדי_עונה_1_פרק_72.mp4", "עולה ברמות לבדי / סולו לבלינג - פרק 7!", (1, 7)),
    ("Animeil_Tv_עולה_ברמות_לבדי_עונה_1_פרק_81.mp4", "עולה ברמות לבדי / סולו לבלינג - פרק 8!", (1, 8)),
    ("[AnimeIL-TV] Solo leveling S02E13.mp4", "עולה ברמות לבדי / סולו לבלינג עונה 2 - פרק 13 - סיום העונה!", (2, 13)),
    ("x.mp4", "דימון סלייר / הלהב קוטל השדים עונה 2 - פרק 5", (2, 5)),
    ("[AnimeBlood] Campfire Cooking S2 - E1.mp4", "", (2, 1)),
    ("Show.S02E05.mkv", "", (2, 5)),
    ("Tondemo_Skill_S2_-_08.mp4", "", (2, 8)),
]


@pytest.mark.parametrize("name,caption,want", CASES)
def test_guess(name, caption, want):
    g = guess(name, caption)
    assert (g["season"], g["episode"]) == want


def test_loose_guess_is_flagged():
    g = guess("[SubsPlease] Tondemo Skill - 07 (1080p) [ABCD1234].mkv", "")
    assert g["episode"] == 7 and g["episode_src"] == "guess" and g["season"] is None


def test_classify():
    assert classify("a.rar", None) == "archive"
    assert classify("", "video/mp4") == "video"
    assert classify("x.srt", None) == "subtitle"
    assert classify("notes.txt", "text/plain") is None
