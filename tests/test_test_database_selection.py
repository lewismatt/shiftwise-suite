"""Database-free regression checks for the pre-import pytest target selection.

Can also run with pytest --noconftest when PostgreSQL is unavailable.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

CONFTEXT = Path(__file__).with_name("conftest.py")


@pytest.mark.parametrize(
    "test_url,password_file,expected,success",
    [
        ("postgresql://test/disposable", None, "postgresql://test/disposable", True),
        (" postgres://test/disposable ", "/production/secret", "postgres://test/disposable", True),
        (None, None, "postgresql://production/working", True),
        ("", None, None, False),
        ("   ", "/production/secret", None, False),
        (None, "/production/secret", None, False),
    ],
)
def test_database_selected_before_app_import(tmp_path, test_url, password_file, expected, success):
    # A fake application makes any attempt to import it with the working URL
    # fail. Rejected configurations must never reach application import.
    (tmp_path / "app.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "Path(os.environ['IMPORT_MARKER']).write_text('imported')\n"
        "assert os.environ['DATABASE_URL'] == os.environ['EXPECTED_URL']\n"
        "assert not os.environ.get('POSTGRES_PASSWORD_FILE')\n"
        "app = object()\n"
    )
    marker = tmp_path / "imported"
    env = os.environ.copy()
    for key in ("TEST_DATABASE_URL", "POSTGRES_PASSWORD_FILE"):
        env.pop(key, None)
    env.update(
        DATABASE_URL="postgresql://production/working",
        EXPECTED_URL=expected or "must-not-import",
        IMPORT_MARKER=str(marker),
    )
    if test_url is not None:
        env["TEST_DATABASE_URL"] = test_url
    if password_file is not None:
        env["POSTGRES_PASSWORD_FILE"] = password_file
    script = (
        "import builtins, runpy, sys, types\n"
        "security = types.ModuleType('shiftwise.security')\n"
        "security.use_csrf_aware_test_client = lambda app: None\n"
        "sys.modules['shiftwise.security'] = security\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "sys.path.insert(0, sys.argv[2])\n"
        "original_import = builtins.__import__\n"
        "def guarded_import(name, *args, **kwargs):\n"
        "    if name == 'app' and name not in sys.modules:\n"
        "        module = types.ModuleType('app')\n"
        "        exec(open(sys.argv[2] + '/app.py').read(), module.__dict__)\n"
        "        sys.modules['app'] = module\n"
        "    return original_import(name, *args, **kwargs)\n"
        "builtins.__import__ = guarded_import\n"
        "runpy.run_path(sys.argv[3])\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(CONFTEXT.parent), str(tmp_path), str(CONFTEXT)],
        env=env,
        capture_output=True,
        text=True,
    )
    if success:
        assert result.returncode == 0, result.stderr
        assert marker.exists()
    else:
        assert result.returncode != 0
        assert "TEST_DATABASE_URL" in result.stderr
        assert not marker.exists()
