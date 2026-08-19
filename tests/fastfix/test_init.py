from pathlib import Path

import fastfix
import minisweagent


def test_fastfix_and_upstream_packages_coexist():
    assert minisweagent.__version__ == "2.4.6"
    assert fastfix is not minisweagent
    assert Path(fastfix.__file__).resolve().parent.name == "fastfix"
    assert Path(minisweagent.__file__).resolve().parent.name == "minisweagent"
