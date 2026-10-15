"""Numbering map and Newznab query handling, without Telegram or Sonarr."""
from app import indexer


def eps(*seasons):
    out, n = [], 0
    for s, count in enumerate(seasons, 1):
        for e in range(1, count + 1):
            n += 1
            out.append({"s": s, "e": e, "abs": n, "title": f"S{s}E{e}"})
    return out


def vid(season, episode, msg, size=500, height=1080):
    return {"kind": "video", "season": season, "episode": episode, "msg_id": msg, "size": size, "height": height,
            "name": f"s{season}e{episode}.mp4", "date": "2026-01-01T00:00:00+00:00"}


def test_posted_numbering_when_seasons_fit():
    src = {"season_sizes": {"1": 12, "2": 12}}
    m = indexer.map_files(src, [vid(1, 5, 1), vid(2, 3, 2)], eps(12, 12))
    assert [(x["s"], x["e"]) for x in m] == [(1, 5), (2, 3)]


def test_continuous_numbering_when_channel_merges_seasons():
    # Demon Slayer: the channel's "season 2" (18 episodes) is Sonarr's seasons 2 (7) and 3 (11)
    src = {"season_sizes": {"1": 26, "2": 18, "3": 11}}
    files = [vid(2, 1, 1), vid(2, 7, 2), vid(2, 8, 3), vid(3, 1, 4)]
    m = indexer.map_files(src, files, eps(26, 7, 11, 11))
    assert [(x["s"], x["e"]) for x in m] == [(2, 1), (2, 7), (3, 1), (4, 1)]


def test_missing_episode_does_not_shift_numbering():
    # a channel season with FEWER episodes than Sonarr's must not trigger continuous counting
    src = {"season_sizes": {"1": 11, "2": 13}}
    m = indexer.map_files(src, [vid(2, 12, 1)], eps(12, 13))
    assert [(x["s"], x["e"]) for x in m] == [(2, 12)]


def test_best_file_per_episode_and_extras_skipped():
    src = {"season_sizes": {"1": 12}}
    files = [vid(1, 1, 1, height=720), vid(1, 1, 2, height=1080), {**vid(1, 2, 3), "extra": "trailer"},
             {**vid(1, 3, 4), "kind": "subtitle"}, vid(1, None, 5)]
    m = indexer.map_files(src, files, eps(12))
    assert [(x["s"], x["e"], x["file"]["msg_id"]) for x in m] == [(1, 1, 2)]


def test_release_name():
    assert indexer.release_name("Demon Slayer: Kimetsu no Yaiba", 3, 1, {"height": 1080}) == \
        "Demon.Slayer.Kimetsu.no.Yaiba.S03E01.1080p.WEB-DL.HebSub-TG"


def rel(tvdb, s, e, a):
    return {"tvdb": tvdb, "season": s, "episode": e, "abs": a, "posted": a, "title": f"x.S{s:02}E{e:02}"}


def setup_index():
    indexer.index.clear()
    indexer.index["a"] = {"tvdb": 405920, "releases": [rel(405920, 1, 13, 13), rel(405920, 2, 1, 26), rel(405920, 2, 5, 30)],
                          "titles": [["SPY x FAMILY", None], ["Spy x Family S2", 2], ["Spy x Family Part 2", None]]}
    indexer.index["b"] = {"tvdb": 402474, "releases": [rel(402474, 2, 1, 14)],
                          "titles": [["Hell's Paradise", None], ["Jigokuraku", None]]}


def found(*args):
    return [(r["season"], r["episode"]) for r in indexer.search(*args)[0]]


def test_search_shapes_sonarr_uses():
    setup_index()
    assert found(405920, "", 2, 5) == [(2, 5)]                    # standard: tvdbid + season + ep
    assert found(405920, "13", None, None) == [(1, 13)]           # anime: tvdbid + absolute number
    assert found(None, "SPY x FAMILY 26", None, None) == [(2, 1)]  # anime: title + absolute number
    assert found(None, "Spy x Family S2 5", None, None) == [(2, 5)]  # season-specific alternate title
    assert found(None, "Hell's Paradise 14", None, None) == [(2, 1)]
    assert found(None, "Jigokuraku 14", None, None) == [(2, 1)]
    assert found(None, "Some Other Show 3", None, None) == []


def test_rss_poll_is_not_a_search():
    setup_index()
    rels, is_search = indexer.search(None, "", None, None)
    assert len(rels) == 4 and is_search is False


def test_nzb_round_trip():
    r = {"source_id": "abc", "msg_id": 16, "tvdb": 1, "series": "Show", "poster": None, "title": "Show.S01E05.1080p.WEB-DL.HebSub-TG",
         "season": 1, "episode": 5, "size": 123, "height": 1080, "ext": ".mp4", "name": "פרק 5.mp4", "posted": 0, "guid": "tgarr-abc-16"}
    ref = indexer.parse_nzb(indexer.nzb_xml(r).encode())
    assert ref["source_id"] == "abc" and ref["msg_id"] == 16 and ref["name"] == "פרק 5.mp4"


def test_continuous_numbering_when_channel_counts_straight_through():
    # Kaiju No. 8: the channel's "season 1" runs 1-18 and its "season 2" carries on at 19; Sonarr has 12 + 11
    src = {"season_sizes": {"1": 18, "2": 23}, "season_mins": {"1": 1, "2": 19}}
    files = [vid(1, 12, 1), vid(1, 13, 2), vid(1, 18, 3), vid(2, 19, 4), vid(2, 23, 5)]
    m = indexer.map_files(src, files, eps(12, 11))
    assert [(x["s"], x["e"]) for x in m] == [(1, 12), (2, 1), (2, 6), (2, 7), (2, 11)]


def test_season_offsets():
    assert indexer.season_offsets({"1": 26, "2": 18, "3": 11}, {"1": 1, "2": 1, "3": 1}) == {1: 0, 2: 26, 3: 44}
    assert indexer.season_offsets({"1": 18, "2": 23}, {"1": 1, "2": 19}) == {1: 0, 2: 0}
    # a season whose first episodes are just missing (starts at 3) still restarts its numbering
    assert indexer.season_offsets({"1": 12, "2": 12}, {"1": 1, "2": 3}) == {1: 0, 2: 12}
