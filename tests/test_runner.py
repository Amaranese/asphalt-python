import asyncio
import logging
import sys
from unittest.mock import patch

import pytest
from pkg_resources import DistributionNotFound

from asphalt.core.component import Component, CLIApplicationComponent
from asphalt.core.context import Context
from asphalt.core.runner import run_application, sigterm_handler


class ShutdownComponent(Component):
    def __init__(self, method: str = 'stop'):
        self.method = method
        self.teardown_callback_called = False
        self.exception = None

    def teardown_callback(self, exception):
        self.teardown_callback_called = True
        self.exception = exception

    def press_ctrl_c(self):
        raise KeyboardInterrupt

    async def start(self, ctx: Context):
        ctx.add_teardown_callback(self.teardown_callback, pass_exception=True)

        if self.method == 'stop':
            ctx.loop.call_later(0.1, ctx.loop.stop)
        elif self.method == 'exit':
            ctx.loop.call_later(0.1, sys.exit)
        elif self.method == 'keyboard':
            ctx.loop.call_later(0.1, self.press_ctrl_c)
        elif self.method == 'sigterm':
            ctx.loop.call_later(0.1, sigterm_handler, logging.getLogger(__name__), ctx.loop)
        elif self.method == 'exception':
            raise RuntimeError('this should crash the application')
        elif self.method == 'timeout':
            await asyncio.sleep(1)


class DummyCLIApp(CLIApplicationComponent):
    async def run(self, ctx: Context):
        return 20


@pytest.fixture(autouse=True)
def prevent_logging_shutdown(monkeypatch):
    monkeypatch.setattr('asphalt.core.runner.shutdown', lambda: None)


def test_sigterm_handler_loop_not_running(event_loop):
    """Test that the SIGTERM handler does nothing if the event loop is not running."""
    sigterm_handler(logging.getLogger(__name__), event_loop)


@pytest.mark.parametrize('logging_config', [
    None,
    logging.INFO,
    {'version': 1, 'loggers': {'asphalt': {'level': 'INFO'}}}
], ids=['disabled', 'loglevel', 'dictconfig'])
def test_run_logging_config(event_loop, logging_config):
    """Test that logging initialization happens as expected."""
    with patch('asphalt.core.runner.basicConfig') as basicConfig,\
            patch('asphalt.core.runner.dictConfig') as dictConfig:
        run_application(ShutdownComponent(), logging=logging_config)

    assert basicConfig.call_count == (1 if logging_config == logging.INFO else 0)
    assert dictConfig.call_count == (1 if isinstance(logging_config, dict) else 0)


@pytest.mark.parametrize('max_threads', [None, 3])
def test_run_max_threads(event_loop, max_threads):
    """
    Test that a new default executor is installed if and only if the max_threads argument is given.

    """
    component = ShutdownComponent()
    with patch('asphalt.core.runner.ThreadPoolExecutor') as mock_executor:
        run_application(component, max_threads=max_threads)

    if max_threads:
        mock_executor.assert_called_once_with(max_threads)
    else:
        assert not mock_executor.called


@pytest.mark.parametrize('policy, policy_name', [
    ('uvloop', 'uvloop.EventLoopPolicy'),
    ('tokio', 'tokio.TokioLoopPolicy'),
], ids=['uvloop', 'tokio'])
def test_event_loop_policy(caplog, policy, policy_name):
    """Test that a the runner switches to a different event loop policy when instructed to."""
    caplog.set_level(logging.INFO)
    component = ShutdownComponent()
    old_policy = asyncio.get_event_loop_policy()
    try:
        run_application(component, event_loop_policy=policy)
    except DistributionNotFound as e:
        pytest.skip(str(e))
    finally:
        asyncio.set_event_loop_policy(old_policy)

    records = [record for record in caplog.records if record.name == 'asphalt.core.runner']
    assert len(records) == 6
    assert records[0].message == 'Running in development mode'
    assert records[1].message == 'Switched event loop policy to %s' % policy_name
    assert records[2].message == 'Starting application'
    assert records[3].message == 'Application started'
    assert records[4].message == 'Stopping application'
    assert records[5].message == 'Application stopped'


def test_run_callbacks(event_loop, caplog):
    """
    Test that the teardown callbacks are run when the application is started and shut down properly
    and that the proper logging messages are emitted.

    """
    caplog.set_level(logging.INFO)
    component = ShutdownComponent()
    run_application(component)

    assert component.teardown_callback_called
    records = [record for record in caplog.records if record.name == 'asphalt.core.runner']
    assert len(records) == 5
    assert records[0].message == 'Running in development mode'
    assert records[1].message == 'Starting application'
    assert records[2].message == 'Application started'
    assert records[3].message == 'Stopping application'
    assert records[4].message == 'Application stopped'


@pytest.mark.parametrize('method', ['exit', 'keyboard', 'sigterm'])
def test_clean_exit(event_loop, caplog, method):
    """
    Test that when Ctrl+C is pressed during event_loop.run_forever(), run_application() exits
    cleanly.

    """
    caplog.set_level(logging.INFO)
    component = ShutdownComponent(method=method)
    run_application(component)

    records = [record for record in caplog.records if record.name == 'asphalt.core.runner']
    assert len(records) == 5
    assert records[0].message == 'Running in development mode'
    assert records[1].message == 'Starting application'
    assert records[2].message == 'Application started'
    assert records[3].message == 'Stopping application'
    assert records[4].message == 'Application stopped'


def test_run_start_exception(event_loop, caplog):
    """
    Test that an exception caught during the application initialization is put into the
    application context and made available to teardown callbacks.

    """
    caplog.set_level(logging.INFO)
    component = ShutdownComponent(method='exception')
    pytest.raises(SystemExit, run_application, component)

    assert str(component.exception) == 'this should crash the application'
    records = [record for record in caplog.records if record.name == 'asphalt.core.runner']
    assert len(records) == 5
    assert records[0].message == 'Running in development mode'
    assert records[1].message == 'Starting application'
    assert records[2].message == 'Error during application startup'
    assert records[3].message == 'Stopping application'
    assert records[4].message == 'Application stopped'


def test_run_start_timeout(event_loop, caplog):
    """
    Test that when the root component takes too long to start up, the runner exits and logs the
    appropriate error message.

    """
    caplog.set_level(logging.INFO)
    component = ShutdownComponent(method='timeout')
    pytest.raises(SystemExit, run_application, component, start_timeout=1)

    records = [record for record in caplog.records if record.name == 'asphalt.core.runner']
    assert len(records) == 5
    assert records[0].message == 'Running in development mode'
    assert records[1].message == 'Starting application'
    assert records[2].message == 'Timeout waiting for the root component to start'
    assert records[3].message == 'Stopping application'
    assert records[4].message == 'Application stopped'


def test_dict_config(event_loop, caplog):
    """Test that component configuration passed as a dictionary works."""
    caplog.set_level(logging.INFO)
    component_class = '{0.__module__}:{0.__name__}'.format(ShutdownComponent)
    run_application(component={'type': component_class})

    records = [record for record in caplog.records if record.name == 'asphalt.core.runner']
    assert len(records) == 5
    assert records[0].message == 'Running in development mode'
    assert records[1].message == 'Starting application'
    assert records[2].message == 'Application started'
    assert records[3].message == 'Stopping application'
    assert records[4].message == 'Application stopped'


def test_run_cli_application(event_loop, caplog):
    caplog.set_level(logging.INFO)
    with pytest.raises(SystemExit) as exc:
        run_application(DummyCLIApp())

    assert exc.value.code == 20

    records = [record for record in caplog.records if record.name == 'asphalt.core.runner']
    assert len(records) == 5
    assert records[0].message == 'Running in development mode'
    assert records[1].message == 'Starting application'
    assert records[2].message == 'Application started'
    assert records[3].message == 'Stopping application'
    assert records[4].message == 'Application stopped'
