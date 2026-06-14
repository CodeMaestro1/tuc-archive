"""State queue semantics + atomic save/load round-trip."""

from tuc_archive.state import CompletedEntry, CrawlState


def test_dedup_and_fifo():
    s = CrawlState()
    assert s.add("a")
    assert s.add("b")
    assert not s.add("a")  # already seen
    assert s.next_url() == "a"
    assert s.next_url() == "b"
    assert s.next_url() is None


def test_complete_and_fail_tracking():
    s = CrawlState()
    s.add("a")
    s.add("b")
    s.complete(CompletedEntry("a", 200, etag="W/123"))
    s.fail("b", "timeout")
    stats = s.stats()
    assert stats["completed"] == 1
    assert stats["errors"] == 1


def test_claim_batch_marks_in_flight_and_requeue_stale():
    s = CrawlState()
    s.add_many(["a", "b", "c"])
    batch = s.claim_batch(2)
    assert batch == ["a", "b"]
    assert s.stats()["in_flight"] == 2
    # nothing stale yet
    assert s.requeue_stale(timeout=999) == 0
    # everything stale
    assert s.requeue_stale(timeout=-1) == 2
    assert s.stats()["in_flight"] == 0


def test_requeue_in_flight_recovers_interrupted_work():
    # simulate: claimed a batch, process killed before completing
    s = CrawlState()
    s.add_many(["a", "b", "c"])
    s.claim_batch(2)            # a,b -> in_flight
    s.complete(CompletedEntry("a", 200))  # a finished, b still in_flight
    assert s.stats()["in_flight"] == 1
    moved = s.requeue_in_flight()
    assert moved == 1
    assert s.stats()["in_flight"] == 0
    assert "b" in s.pending     # b is crawlable again
    assert "a" not in s.pending  # already completed, not requeued


def test_requeue_errors_reattempts_failures():
    s = CrawlState()
    s.add_many(["a", "b", "c"])
    s.claim_batch(3)
    s.complete(CompletedEntry("a", 200))
    s.fail("b", "http-500")
    s.fail("c", "http-500")
    assert s.stats()["errors"] == 2
    moved = s.requeue_errors()
    assert moved == 2
    assert s.stats()["errors"] == 0
    assert "b" in s.pending and "c" in s.pending  # re-queued for retry
    assert "a" not in s.pending                   # completed stays done
    # a re-failure lands back in errors (still reachable for a later retry)
    s.claim_batch(2)
    s.fail("b", "http-500")
    assert "b" in s.errors


def test_save_load_roundtrip(tmp_path):
    s = CrawlState(seeds=["https://x.gr/seed"])
    s.add_many(["https://x.gr/a", "https://x.gr/b"])
    s.complete(CompletedEntry("https://x.gr/a", 200, etag="E1", stored_as="k1"))
    path = tmp_path / "state.yml"
    s.save(path)

    loaded = CrawlState.load(path)
    assert loaded.seeds == ["https://x.gr/seed"]
    assert "https://x.gr/a" in loaded.completed
    assert loaded.completed["https://x.gr/a"].etag == "E1"
    assert "https://x.gr/b" in loaded.pending
    # dedup set rebuilt: re-adding a completed url is a no-op
    assert not loaded.add("https://x.gr/a")


def test_save_is_atomic_no_partial_file(tmp_path):
    # an existing good snapshot must survive a fresh save
    path = tmp_path / "state.yml"
    s = CrawlState()
    s.add("a")
    s.save(path)
    first = path.read_text(encoding="utf-8")
    s.add("b")
    s.save(path)
    assert path.exists()
    assert "b" in path.read_text(encoding="utf-8")
    assert first  # first write was complete
