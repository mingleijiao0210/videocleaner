import json

from app.user_preferences import UserPreferences


def test_last_input_directory_is_saved_and_restored(tmp_path):
    settings_path = tmp_path / "settings" / "user_preferences.json"
    video_directory = tmp_path / "中文 视频目录"
    video_directory.mkdir()

    UserPreferences(settings_path).set_last_input_directory(video_directory)

    restored = UserPreferences(settings_path).last_input_directory(tmp_path)
    assert restored == video_directory.resolve()
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert data["last_input_directory"] == str(video_directory.resolve())


def test_missing_saved_directory_falls_back_safely(tmp_path):
    settings_path = tmp_path / "user_preferences.json"
    settings_path.write_text(
        json.dumps(
            {"last_input_directory": str(tmp_path / "已经不存在")}
        ),
        encoding="utf-8",
    )

    assert (
        UserPreferences(settings_path).last_input_directory(tmp_path)
        == tmp_path
    )


def test_invalid_settings_file_falls_back_safely(tmp_path):
    settings_path = tmp_path / "user_preferences.json"
    settings_path.write_text("{not valid json", encoding="utf-8")

    assert (
        UserPreferences(settings_path).last_input_directory(tmp_path)
        == tmp_path
    )
