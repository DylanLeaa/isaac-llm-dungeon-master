"""The app installs the mod it was built with into the game's mods folder."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import modinstall  # noqa: E402
from modinstall import MOD_DIR, install  # noqa: E402


def _mod(root, files):
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


def _tree(root):
    return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8") for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def src(tmp_path):
    return _mod(tmp_path / "src", {"metadata.xml": "<metadata/>", "main.lua": "-- v2", "dm/bridge.lua": "PROTO = 2"})


def test_installs_into_a_game_without_it_then_leaves_it_be(tmp_path, src):
    game = tmp_path / "game"
    game.mkdir()
    assert install(game, src) == "installed"
    assert _tree(game / "mods" / MOD_DIR) == _tree(src)
    assert install(game, src) == "current"


def test_updates_an_old_copy_and_keeps_the_players_disable_mark(tmp_path, src):
    game = tmp_path / "game"
    old = _mod(game / "mods" / MOD_DIR, {"metadata.xml": "<metadata/>", "main.lua": "-- v1",
                                         "dm/dropcatcher.lua": "gone", "old/x.lua": "gone", "disable.it": ""})
    assert install(game, src) == "updated"
    assert _tree(old) == {**_tree(src), "disable.it": ""}
    assert not (old / "old").exists()


def test_leaves_a_linked_folder_alone(tmp_path, src):
    checkout = _mod(tmp_path / "checkout", {"main.lua": "-- dev"})
    (tmp_path / "game" / "mods").mkdir(parents=True)
    link = tmp_path / "game" / "mods" / MOD_DIR
    try:
        if sys.platform == "win32":
            import _winapi
            _winapi.CreateJunction(str(checkout), str(link))
        else:
            link.symlink_to(checkout, target_is_directory=True)
    except (OSError, ImportError, AttributeError):
        pytest.skip("can't make a link here")
    assert install(tmp_path / "game", src) == "linked"
    assert _tree(checkout) == {"main.lua": "-- dev"}


def test_leaves_a_game_with_the_workshop_copy_alone(tmp_path, src):
    game = tmp_path / "game"
    _mod(game / "mods" / f"{MOD_DIR}_3701683951", {"metadata.xml": "<metadata/>"})
    assert install(game, src) == "workshop"
    assert not (game / "mods" / MOD_DIR).exists()


def test_the_repo_mod_is_what_gets_installed_from_source():
    src = modinstall.bundled()
    assert src is not None and (src / "main.lua").is_file()
