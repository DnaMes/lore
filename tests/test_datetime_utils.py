from annuli.utils.datetime import parse_timestamp


def test_parse_timestamp_invalid_is_epoch():
    dt = parse_timestamp("this-is-not-a-date")
    assert dt.year == 1970
    assert dt.month == 1
    assert dt.day == 1
