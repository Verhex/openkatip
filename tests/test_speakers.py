from katip.pipeline.speakers import SpeakerMapper


def test_label_maps_to_name_active_during_segment() -> None:
    m = SpeakerMapper()
    for at in range(10, 20):
        m.observe(["Ayşe"], at=at)
    for at in range(20, 30):
        m.observe(["Mehmet"], at=at)

    m.learn("1", 11.0, 14.0)  # yalnız Ayşe konuşuyor görünüyordu
    m.learn("2", 21.0, 23.0)  # yalnız Mehmet
    m.learn("1", 15, 17)
    m.learn("1", 17, 19)
    m.learn("2", 24, 26)
    m.learn("2", 27, 29)

    assert m.name_for("1") == "Ayşe"
    assert m.name_for("2") == "Mehmet"


def test_unknown_label_falls_back_to_generic_name() -> None:
    m = SpeakerMapper()
    assert m.name_for("3") == "Konuşmacı 3"
    assert m.name_for(None) is None


def test_overlapping_or_balanced_speakers_are_not_assigned_arbitrarily() -> None:
    m = SpeakerMapper()
    m.observe(["Ali", "Veli"], at=0)
    m.learn("1", 1, 3)
    m.learn("1", 4, 6)
    assert m.name_for("1") == "Konuşmacı 1"
    m.observe(["Ali"], at=10)
    m.learn("2", 11, 12)
    m.observe(["Veli"], at=20)
    m.learn("2", 21, 22)
    assert m.name_for("2") == "Konuşmacı 2"


def test_crowded_moments_weigh_less_than_solo_moments() -> None:
    m = SpeakerMapper()
    # 1. etiket: bir kez kalabalık anda (Ali+Veli), iki kez Veli tek başına.
    m.observe(["Ali", "Veli"], at=0.0)
    m.learn("1", 0.5, 1.0)
    m.observe(["Veli"], at=5.0)
    m.learn("1", 5.5, 6.0)
    m.learn("1", 6.5, 7.0)
    for at in range(8, 18):
        m.observe(["Veli"], at=at)
    for at in (9, 12, 15):
        m.learn("1", at, at + 2)
    assert m.name_for("1") == "Veli"


def test_stale_state_does_not_count_after_heartbeat_expires() -> None:
    m = SpeakerMapper()
    m.observe(["Zeynep"], at=0.0)  # heartbeat kesildi → eski isimle eşleştirilmez
    m.learn("1", 30.0, 33.0)
    assert m.name_for("1") == "Konuşmacı 1"


def test_name_at_uses_who_was_speaking_during_the_sentence() -> None:
    from katip.pipeline.speakers import SpeakerMapper

    m = SpeakerMapper()
    for at in range(0, 5):
        m.observe(["Ayşe"], at=at)
    for at in range(5, 10):
        m.observe(["Mehmet"], at=at)
    m.observe([], at=10.0)
    assert m.name_at(1.0, 4.0) == "Ayşe"
    assert m.name_at(6.0, 9.0) == "Mehmet"
    assert m.name_at(3.5, 6.5) is None  # yarı yarıya: karar verilmez
    assert m.name_at(11.0, 14.0) is None  # kimse konuşuyor görünmüyor


def _train(m, label, name, start):
    for at in range(start, start + 10):
        m.observe([name], at=at)
    for at in (start + 1, start + 4, start + 7):
        m.learn(label, at, at + 2)


def test_one_or_two_sentences_cannot_establish_identity():
    m = SpeakerMapper()
    for at in range(10):
        m.observe(["Ayşe"], at=at)
    m.learn("1", 1, 3)
    m.learn("1", 4, 6)
    assert m.name_for("1") == "Konuşmacı 1"
    m.learn("1", 7, 9)
    assert m.name_for("1") == "Ayşe"


def test_two_labels_cannot_claim_same_person():
    m = SpeakerMapper()
    _train(m, "1", "Deniz", 0)
    _train(m, "2", "Deniz", 20)
    assert m.name_for("1") == "Konuşmacı 1"
    assert m.name_for("2") == "Konuşmacı 2"


def test_merged_label_with_two_people_is_ambiguous():
    m = SpeakerMapper()
    _train(m, "1", "Ayşe", 0)
    _train(m, "1", "Deniz", 20)
    assert m.name_for("1") == "Konuşmacı 1"


def test_update_frequency_does_not_change_vote_weight():
    slow, fast = SpeakerMapper(), SpeakerMapper()
    for at in range(100):
        fast.observe(["Ayşe"], at=at / 10)
    for at in range(10):
        slow.observe(["Ayşe"], at=at)
    for at in (1, 4, 7):
        for m in (slow, fast):
            m.learn("1", at, at + 2)
    assert slow.name_for("1") == fast.name_for("1") == "Ayşe"
    assert abs(slow._votes["1"]["Ayşe"] - fast._votes["1"]["Ayşe"]) < 1e-6


def test_new_stream_does_not_compete_with_old_labels():
    m = SpeakerMapper()
    _train(m, "1", "Ayşe", 0)
    m.reset_labels()
    _train(m, "3", "Ayşe", 20)
    assert m.name_for("3") == "Ayşe"


def test_recent_evidence_replaces_old_votes():
    m = SpeakerMapper()
    _train(m, "1", "Ayşe", 0)
    for at in range(20, 80, 10):
        _train(m, "1", "Deniz", at)
    assert m.name_for("1") == "Deniz"
