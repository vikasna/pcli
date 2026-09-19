"""Coverage for memory/store.py: on-disk persistence for the global,
cross-session user-memory store. Isolated automatically by tests/conftest.py's
autouse _isolated_pcli_paths fixture (redirects data_dir() to a per-test
tmp_path), so no explicit path override is needed here."""

from pcli.config.paths import memory_file
from pcli.memory.store import add_entry, clear_memory, read_memory, remove_entry


def test_add_entry_persists_to_disk():
    entry = add_entry("Works as a backend Python dev", category="profile", source="derived", max_entries=40)

    assert memory_file().exists()
    stored = read_memory()
    assert len(stored.entries) == 1
    assert stored.entries[0].id == entry.id
    assert stored.entries[0].content == "Works as a backend Python dev"
    assert stored.entries[0].category == "profile"
    assert stored.entries[0].source == "derived"


def test_read_memory_with_no_file_yet_returns_empty_store():
    store = read_memory()
    assert store.entries == []


def test_add_entry_merges_case_insensitive_duplicate_in_same_category():
    first = add_entry("Prefers pytest", category="preference", source="derived", max_entries=40)
    second = add_entry("PREFERS PYTEST", category="preference", source="derived", max_entries=40)

    assert first.id == second.id
    assert len(read_memory().entries) == 1


def test_add_entry_does_not_merge_across_different_categories():
    add_entry("Prefers pytest", category="preference", source="derived", max_entries=40)
    add_entry("Prefers pytest", category="common_ask", source="derived", max_entries=40)

    assert len(read_memory().entries) == 2


def test_add_entry_promotes_a_derived_duplicate_to_explicit():
    """A fact seen implicitly, then later stated directly by the user,
    should stop being auto-evictable - not sit forever as 'derived'."""
    add_entry("Prefers concise commits", category="preference", source="derived", max_entries=40)
    add_entry("Prefers concise commits", category="preference", source="explicit", max_entries=40)

    entries = read_memory().entries
    assert len(entries) == 1
    assert entries[0].source == "explicit"


def test_add_entry_evicts_oldest_derived_entry_once_over_the_cap():
    add_entry("fact one", category="profile", source="derived", max_entries=2)
    add_entry("fact two", category="preference", source="derived", max_entries=2)
    add_entry("fact three", category="style", source="derived", max_entries=2)

    entries = read_memory().entries
    assert len(entries) == 2
    assert [e.content for e in entries] == ["fact two", "fact three"]


def test_add_entry_never_evicts_explicit_entries():
    add_entry("explicit fact", category="profile", source="explicit", max_entries=2)
    add_entry("oldest derived", category="preference", source="derived", max_entries=2)
    add_entry("newest derived", category="style", source="derived", max_entries=2)

    entries = read_memory().entries
    # The explicit entry always survives; between the two derived ones, the
    # older is evicted to make room, never the explicit one.
    assert {e.content for e in entries} == {"explicit fact", "newest derived"}


def test_add_entry_evicts_a_just_added_derived_entry_if_it_is_the_only_one():
    """max_entries=1 with an explicit entry already occupying the only slot:
    a new derived entry has nowhere to go and nothing else derived to evict
    in its place, so it's evicted immediately - the explicit entry is still
    never touched."""
    add_entry("explicit fact", category="profile", source="explicit", max_entries=1)
    add_entry("derived fact", category="preference", source="derived", max_entries=1)

    entries = read_memory().entries
    assert [e.content for e in entries] == ["explicit fact"]


def test_remove_entry_removes_by_full_id():
    entry = add_entry("some fact", category="profile", source="derived", max_entries=40)

    removed = remove_entry(entry.id)

    assert removed is True
    assert read_memory().entries == []


def test_remove_entry_returns_false_for_unknown_id():
    assert remove_entry("mem_does_not_exist") is False


def test_clear_memory_wipes_everything():
    add_entry("a", category="profile", source="derived", max_entries=40)
    add_entry("b", category="preference", source="explicit", max_entries=40)

    clear_memory()

    assert read_memory().entries == []
