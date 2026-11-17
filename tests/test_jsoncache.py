import threading

from jsoncache import JsonCache


def test_concurrent_writes_keep_all_keys(tmp_path):
    path = tmp_path / "c.json"
    cache = JsonCache(str(path))

    def worker(n):
        for i in range(50):
            cache.set(f"{n}-{i}", i)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    reloaded = JsonCache(str(path))
    assert len(reloaded.snapshot()) == 8 * 50


def test_corrupted_file_starts_empty(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{quebrado", encoding="utf-8")
    assert JsonCache(str(path)).get("x") is None
