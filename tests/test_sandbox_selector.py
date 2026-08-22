"""Coverage for select_sandbox's explicit backend_override branches, plus
the error message for an invalid one - previously just "Unknown
sandbox_backend '<value>'" with no indication of what a valid value looks
like or where to fix it."""

import pytest

from pcli.sandbox.docker_backend import DockerSandbox
from pcli.sandbox.null_backend import NullSandbox
from pcli.sandbox.selector import select_sandbox
from pcli.sandbox.subprocess_backend import RestrictedSubprocessSandbox


@pytest.mark.asyncio
async def test_explicit_docker_override():
    sandbox = await select_sandbox(backend_override="docker")
    assert isinstance(sandbox, DockerSandbox)


@pytest.mark.asyncio
async def test_explicit_subprocess_override():
    sandbox = await select_sandbox(backend_override="subprocess")
    assert isinstance(sandbox, RestrictedSubprocessSandbox)


@pytest.mark.asyncio
async def test_explicit_none_override():
    sandbox = await select_sandbox(backend_override="none")
    assert isinstance(sandbox, NullSandbox)


@pytest.mark.asyncio
async def test_unknown_backend_names_the_bad_value_and_valid_options():
    with pytest.raises(ValueError) as exc_info:
        await select_sandbox(backend_override="dckr")  # typo

    message = str(exc_info.value)
    assert "'dckr'" in message
    assert "'auto'" in message and "'docker'" in message and "'subprocess'" in message and "'none'" in message
    assert "sandbox_backend" in message
    assert "PCLI_SANDBOX_BACKEND" in message
