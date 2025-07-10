from pathlib import Path

import pytest

RESOURCES = Path(__file__).parent / "tests/resources"


@pytest.fixture
def rako_xml() -> str:
    with (RESOURCES / "rako.xml").open() as f:
        xml = f.read()

    return xml


@pytest.fixture
def rako_xml2() -> str:
    with (RESOURCES / "rako2.xml").open() as f:
        xml = f.read()

    return xml


@pytest.fixture
def rako_xml3() -> str:
    with (RESOURCES / "rako3.xml").open() as f:
        xml = f.read()

    return xml
