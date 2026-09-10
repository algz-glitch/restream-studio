def test_package_exposes_version() -> None:
    import restream_studio

    assert restream_studio.__version__ == "0.1.0"
