from tools import batch_generate_gallery as gallery


def test_contact_sheet_font_can_reuse_windows_host_font(monkeypatch):
    sentinel = object()
    attempted = []

    def fake_truetype(path, size):
        attempted.append((path, size))
        if path == "/mnt/c/Windows/Fonts/msyh.ttc":
            return sentinel
        raise OSError(path)

    monkeypatch.setattr(gallery.ImageFont, "truetype", fake_truetype)

    assert gallery._load_font(22) is sentinel
    assert attempted == [("/mnt/c/Windows/Fonts/msyh.ttc", 22)]
