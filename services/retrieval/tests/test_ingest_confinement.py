import pytest
from fastapi import HTTPException

from retrieval.main import resolve_corpus_path


def test_a_directory_inside_the_root_is_accepted(tmp_path):
    inside = tmp_path / "en" / "docs"
    inside.mkdir(parents=True)

    assert resolve_corpus_path(str(inside), tmp_path) == inside.resolve()


def test_the_root_itself_is_accepted(tmp_path):
    assert resolve_corpus_path(str(tmp_path), tmp_path) == tmp_path.resolve()


@pytest.mark.parametrize("escape", ["..", "../../etc", "sub/../.."])
def test_a_relative_path_climbing_out_of_the_root_is_rejected(tmp_path, escape):
    (tmp_path / "sub").mkdir()

    with pytest.raises(HTTPException) as raised:
        resolve_corpus_path(str(tmp_path / escape), tmp_path)

    assert raised.value.status_code == 400


def test_an_absolute_path_outside_the_root_is_rejected(tmp_path):
    with pytest.raises(HTTPException) as raised:
        resolve_corpus_path("/etc", tmp_path)

    assert raised.value.status_code == 400


def test_a_symlink_pointing_out_of_the_root_is_rejected(tmp_path):
    """Rejection happens after resolve(), so a symlink escape is caught too."""
    outside = tmp_path.parent / "outside-the-corpus"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "escape"
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(HTTPException) as raised:
        resolve_corpus_path(str(link), tmp_path)

    assert raised.value.status_code == 400


def test_the_rejection_never_echoes_the_path_back(tmp_path):
    """Echoing it would let the endpoint be used to map the container filesystem."""
    with pytest.raises(HTTPException) as raised:
        resolve_corpus_path("/etc/passwd", tmp_path)

    assert "passwd" not in raised.value.detail
    assert "/etc" not in raised.value.detail


def test_a_prefix_collision_is_not_treated_as_containment(tmp_path):
    """/corpus-secrets must not pass because it starts with /corpus."""
    root = tmp_path / "corpus"
    root.mkdir()
    sibling = tmp_path / "corpus-secrets"
    sibling.mkdir()

    with pytest.raises(HTTPException) as raised:
        resolve_corpus_path(str(sibling), root)

    assert raised.value.status_code == 400
