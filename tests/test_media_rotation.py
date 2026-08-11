from app.media_info import _rotation


def test_rotation_from_display_matrix():
    assert _rotation({"side_data_list": [{"rotation": -90}]}) == -90


def test_rotation_from_tag():
    assert _rotation({"tags": {"rotate": "90"}}) == 90


def test_missing_rotation():
    assert _rotation({}) == 0
