from tools.gen_area_gallery import gallery_succeeded


def test_gallery_success_requires_every_requested_style_and_both_renders():
    styles = ["a", "b"]
    complete = {
        "styles": {
            "a": {"renders": {"topdown": "a.png", "height": "ah.png"}},
            "b": {"renders": {"topdown": "b.png", "height": "bh.png"}},
        },
    }

    assert gallery_succeeded(complete, styles) is True
    assert gallery_succeeded({
        "styles": {"a": complete["styles"]["a"], "b": {"error": "boom"}},
    }, styles) is False
    assert gallery_succeeded({
        "styles": {"a": complete["styles"]["a"],
                   "b": {"renders": {"topdown": "b.png"}}},
    }, styles) is False
